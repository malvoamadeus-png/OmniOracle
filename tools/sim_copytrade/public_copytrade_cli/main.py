from __future__ import annotations

import argparse
import bisect
import json
import math
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

DATA_API = "https://data-api.polymarket.com/activity"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS_API = "https://gamma-api.polymarket.com/events"
CLOB_MIDPOINT_API = "https://clob.polymarket.com/midpoint"
USER_PNL_API = "https://user-pnl-api.polymarket.com/user-pnl"
USER_PNL_INTERVAL = "all"
USER_PNL_FIDELITY = "12h"

FIXED_USD_OPTIONS = [5.0, 20.0, 50.0, 100.0]
PROPORTIONAL_PCT_OPTIONS = [0.005, 0.01, 0.03, 0.05]
PROPORTIONAL_CAP_USD_OPTIONS = [5.0, 20.0, 50.0, 100.0]
MAX_ENTRIES_PER_MARKET = 20

MAKER_LIKE_MIN_TRADE_SIZE_USD = 500.0
MAKER_LIKE_WINDOW_S = 360 * 60
MAKER_LIKE_MAX_GAP_S = 30 * 60
MAKER_LIKE_SCORE_THRESHOLD = 0.60

DEFAULT_MAX_ACTIVITIES = 50_000
DEFAULT_PREMIUM = 0.03
DEFAULT_MIRROR_SELL_SLIPPAGE = 0.01
DEFAULT_PAGE_LIMIT = 1000
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_PRICE_WORKERS = 16

BUY_MIN_PRICE = 0.01
BUY_MAX_PRICE = 0.99
SELL_MIN_PRICE = 0.01
SELL_MAX_PRICE = 0.99
FEE_ENABLED = True
FEE_RATE = 0.03
FEE_EXPONENT = 1.0
ANTI_AMPLIFICATION_GUARD_ENABLED = True
MAX_OUR_VS_LEADER_PER_TRADE = 1.0
MAX_OUR_VS_LEADER_PER_MARKET = 1.0
EXIT_MODE = "mirror_sell"

PRICE_CACHE_MIDPOINT_TTL_S = 15 * 60
PRICE_CACHE_MISSING_TTL_S = 2 * 60

MODULE_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = MODULE_ROOT / "output"
PRICE_CACHE_DB_PATH = MODULE_ROOT / ".cache" / "price_cache.sqlite"

OUTPUT_BRIEF_FIELDS = (
    "strategy",
    "copy_mode",
    "fixed_usd",
    "proportional_pct",
    "proportional_cap_usd",
    "max_entries_per_market",
    "total_pnl",
    "roi",
    "total_buy_cost",
    "copied_buys",
    "mirrored_sells",
)


@dataclass
class TradeEvent:
    tx_hash: str
    ts: int
    side: str
    token_id: str
    condition_id: str
    market_slug: str
    price: Optional[float]
    size: Optional[float]
    usd: Optional[float]
    copy_signal: bool = True
    is_leader_position_event: bool = True
    is_maker_like_aggregated: bool = False
    maker_like_score: Optional[float] = None
    aggregation_source_count: Optional[int] = None


@dataclass
class Strategy:
    name: str
    copy_mode: str
    fixed_usd: Optional[float]
    proportional_pct: float
    proportional_cap_usd: Optional[float]
    max_entries_per_market: int
    exit_mode: str


@dataclass
class Position:
    size: float = 0.0
    cost: float = 0.0
    market_key: str = ""


@dataclass
class PriceInfo:
    token_id: str
    price: Optional[float]
    resolved: bool
    source: str


@dataclass
class PriceLookupContext:
    market_slug: str = ""
    condition_id: str = ""


class StrategyState:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.positions: Dict[str, Position] = {}
        self.buy_counts: Dict[str, int] = {}
        self.leader_open_sizes: Dict[str, float] = {}
        self.leader_market_buy_usd: Dict[str, float] = {}
        self.our_market_buy_usd: Dict[str, float] = {}

        self.total_buy_cost = 0.0
        self.realized_pnl = 0.0

        self.copied_buys = 0
        self.mirrored_sells = 0
        self.skipped_entry_limit = 0
        self.skipped_buy_price = 0
        self.skipped_sell_price = 0
        self.skipped_missing_value = 0
        self.guard_trimmed_count = 0
        self.guard_trimmed_usd = 0.0
        self.guard_skipped_count = 0
        self.oversize_before_guard_count = 0
        self.oversize_after_guard_count = 0


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def as_int(value: Any) -> Optional[int]:
    n = as_float(value)
    return None if n is None else int(round(n))


def parse_epoch(ts_raw: Any) -> Optional[int]:
    if isinstance(ts_raw, (int, float)):
        n = int(ts_raw)
        if n > 10_000_000_000:
            n //= 1000
        return n if n > 0 else None
    if isinstance(ts_raw, str):
        s = ts_raw.strip()
        if not s:
            return None
        if s.isdigit():
            n = int(s)
            if n > 10_000_000_000:
                n //= 1000
            return n if n > 0 else None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


def format_utc_from_epoch(ts_raw: Any) -> Optional[str]:
    ts = parse_epoch(ts_raw)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def short_address(value: Any) -> str:
    s = str(value or "").strip()
    if len(s) >= 14:
        return f"{s[:8]}_{s[-6:]}"
    return s or "unknown"


def strategy_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float]:
    total_pnl = as_float(row.get("total_pnl"))
    roi = as_float(row.get("roi"))
    total_buy_cost = as_float(row.get("total_buy_cost"))
    return (
        total_pnl if total_pnl is not None else float("-inf"),
        roi if roi is not None else float("-inf"),
        total_buy_cost if total_buy_cost is not None else float("-inf"),
    )


def strategy_roi_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float]:
    roi = as_float(row.get("roi"))
    total_pnl = as_float(row.get("total_pnl"))
    total_buy_cost = as_float(row.get("total_buy_cost"))
    return (
        roi if roi is not None else float("-inf"),
        total_pnl if total_pnl is not None else float("-inf"),
        total_buy_cost if total_buy_cost is not None else float("-inf"),
    )


def interpolate_series_at(points: Sequence[Tuple[int, float]], target_ts: int) -> Dict[str, Any]:
    if not points:
        return {
            "value": None,
            "extrapolated": True,
            "nearest_point_delta_s": None,
            "segment_span_s": None,
            "left_ts": None,
            "right_ts": None,
        }

    ts_list = [point[0] for point in points]
    if target_ts <= ts_list[0]:
        return {
            "value": points[0][1],
            "extrapolated": True,
            "nearest_point_delta_s": int(abs(ts_list[0] - target_ts)),
            "segment_span_s": None,
            "left_ts": ts_list[0],
            "right_ts": ts_list[0],
        }
    if target_ts >= ts_list[-1]:
        return {
            "value": points[-1][1],
            "extrapolated": True,
            "nearest_point_delta_s": int(abs(target_ts - ts_list[-1])),
            "segment_span_s": None,
            "left_ts": ts_list[-1],
            "right_ts": ts_list[-1],
        }

    idx = bisect.bisect_left(ts_list, target_ts)
    left_ts, left_val = points[idx - 1]
    right_ts, right_val = points[idx]
    if right_ts <= left_ts:
        return {
            "value": left_val,
            "extrapolated": False,
            "nearest_point_delta_s": 0,
            "segment_span_s": 0,
            "left_ts": left_ts,
            "right_ts": right_ts,
        }

    ratio = (target_ts - left_ts) / (right_ts - left_ts)
    return {
        "value": left_val + (right_val - left_val) * ratio,
        "extrapolated": False,
        "nearest_point_delta_s": int(min(abs(target_ts - left_ts), abs(right_ts - target_ts))),
        "segment_span_s": int(right_ts - left_ts),
        "left_ts": left_ts,
        "right_ts": right_ts,
    }


def http_get_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_retries: int = 4,
) -> Any:
    last_err: Optional[str] = None
    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=timeout_s, headers={"accept": "application/json"})
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"{resp.status_code} {resp.text[:200]}"
                time.sleep(0.5 * (2**attempt))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"{resp.status_code} {resp.text[:300]}")
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            if attempt == max_retries - 1:
                break
            time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"GET {url} failed: {last_err}")


def fetch_user_pnl_series(
    session: requests.Session,
    address: str,
    *,
    interval: str = USER_PNL_INTERVAL,
    fidelity: str = USER_PNL_FIDELITY,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Tuple[str, float]]:
    data = http_get_json(
        session,
        USER_PNL_API,
        params={"user_address": address, "interval": interval, "fidelity": fidelity},
        timeout_s=timeout_s,
        max_retries=3,
    )
    if not isinstance(data, list):
        return []
    out: List[Tuple[str, float]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        ts_raw = row.get("t")
        pnl_raw = row.get("p")
        if not isinstance(ts_raw, (int, float)) or not isinstance(pnl_raw, (int, float)):
            continue
        out.append((datetime.fromtimestamp(float(ts_raw), tz=timezone.utc).isoformat(), float(pnl_raw)))
    return out


def compute_tracked_window_benchmark(
    session: requests.Session,
    address: str,
    *,
    first_ts: Optional[int],
    last_ts: Optional[int],
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    benchmark: Dict[str, Any] = {
        "mode": "tracked_window",
        "actual_window_pnl_delta": None,
        "series_points": 0,
        "series_first_ts": None,
        "series_last_ts": None,
        "series_first_utc": None,
        "series_last_utc": None,
        "interpolation_quality": {"start": None, "end": None},
    }

    if first_ts is None or last_ts is None or last_ts < first_ts:
        benchmark["error"] = "invalid tracked window timestamps"
        return benchmark

    try:
        series = fetch_user_pnl_series(
            session,
            address,
            interval=USER_PNL_INTERVAL,
            fidelity=USER_PNL_FIDELITY,
            timeout_s=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001
        benchmark["error"] = f"fetch_user_pnl_series failed: {exc}"
        return benchmark

    parsed: List[Tuple[int, float]] = []
    for iso_ts, pnl in series:
        ts = parse_epoch(iso_ts)
        pnl_val = as_float(pnl)
        if ts is None or pnl_val is None:
            continue
        parsed.append((ts, pnl_val))
    parsed.sort(key=lambda item: item[0])

    benchmark["series_points"] = len(parsed)
    if parsed:
        benchmark["series_first_ts"] = parsed[0][0]
        benchmark["series_last_ts"] = parsed[-1][0]
        benchmark["series_first_utc"] = format_utc_from_epoch(parsed[0][0])
        benchmark["series_last_utc"] = format_utc_from_epoch(parsed[-1][0])

    start_info = interpolate_series_at(parsed, int(first_ts))
    end_info = interpolate_series_at(parsed, int(last_ts))
    benchmark["interpolation_quality"]["start"] = start_info
    benchmark["interpolation_quality"]["end"] = end_info

    start_val = as_float(start_info.get("value"))
    end_val = as_float(end_info.get("value"))
    if start_val is None or end_val is None:
        benchmark["error"] = "insufficient user-pnl series for interpolation"
        return benchmark

    benchmark["window_start_pnl"] = start_val
    benchmark["window_end_pnl"] = end_val
    benchmark["actual_window_pnl_delta"] = end_val - start_val
    return benchmark


def parse_trade_event(row: Dict[str, Any]) -> Optional[TradeEvent]:
    side_raw = row.get("side")
    if not isinstance(side_raw, str):
        return None
    side = side_raw.upper()
    if side not in {"BUY", "SELL"}:
        return None

    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")
    if not token_id:
        return None

    condition_id = row.get("market") or row.get("conditionId") or row.get("condition_id") or ""
    market_slug = row.get("eventSlug") or row.get("slug") or row.get("market_slug") or ""
    tx_hash = row.get("transaction_hash") or row.get("transactionHash") or row.get("txHash") or row.get("hash") or ""

    ts_raw = row.get("timestamp") or row.get("time") or row.get("createdAt") or row.get("ts")
    ts = parse_epoch(ts_raw)
    if ts is None:
        return None

    price = None
    for key in ("price", "avgPrice", "avg_price"):
        price = as_float(row.get(key))
        if price is not None:
            break

    size = None
    for key in ("size", "shares", "amount", "qty", "quantity"):
        size = as_float(row.get(key))
        if size is not None:
            break

    usd = None
    for key in ("usdcSize", "amountUSD", "amountUsd", "usdc", "usd", "value", "amount"):
        usd = as_float(row.get(key))
        if usd is not None:
            break

    if size is None and usd is not None and price is not None and price > 0:
        size = usd / price
    if usd is None and size is not None and price is not None and price > 0:
        usd = size * price

    return TradeEvent(
        tx_hash=str(tx_hash),
        ts=ts,
        side=side,
        token_id=str(token_id),
        condition_id=str(condition_id),
        market_slug=str(market_slug),
        price=price,
        size=size,
        usd=usd,
    )


def fetch_activity_events(
    session: requests.Session,
    address: str,
    *,
    max_activities: int,
    page_limit: int,
    timeout_s: float,
) -> List[TradeEvent]:
    all_events_desc: List[TradeEvent] = []
    end_cursor: Optional[int] = None
    page = 0

    while len(all_events_desc) < max_activities:
        remaining = max_activities - len(all_events_desc)
        current_limit = min(page_limit, remaining)
        if current_limit <= 0:
            break

        params: Dict[str, Any] = {
            "user": address,
            "type": "TRADE",
            "limit": current_limit,
            "offset": 0,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if end_cursor is not None:
            params["end"] = end_cursor

        data = http_get_json(session, DATA_API, params=params, timeout_s=timeout_s, max_retries=4)
        if not isinstance(data, list) or not data:
            break

        page += 1
        parsed_page: List[TradeEvent] = []
        oldest_ts: Optional[int] = None
        for row in data:
            if not isinstance(row, dict):
                continue
            event = parse_trade_event(row)
            if event is None:
                continue
            parsed_page.append(event)
            oldest_ts = event.ts if oldest_ts is None else min(oldest_ts, event.ts)

        all_events_desc.extend(parsed_page)
        print(f"[fetch] page={page} parsed={len(parsed_page)} total={len(all_events_desc)}")

        if oldest_ts is None:
            ts_candidates = [parse_epoch(item.get("timestamp")) for item in data if isinstance(item, dict)]
            ts_candidates = [ts for ts in ts_candidates if ts is not None]
            if not ts_candidates:
                break
            oldest_ts = min(ts_candidates)

        end_cursor = oldest_ts - 1
        if len(data) < current_limit:
            break

    if len(all_events_desc) > max_activities:
        all_events_desc = all_events_desc[:max_activities]

    seen = set()
    deduped: List[TradeEvent] = []
    for event in all_events_desc:
        key = (
            event.tx_hash,
            event.token_id,
            event.side,
            event.ts,
            round(event.price, 8) if event.price is not None else None,
            round(event.size, 8) if event.size is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    deduped.sort(key=lambda item: (item.ts, item.tx_hash, item.token_id, item.side))
    return deduped


def generate_strategies(
    fixed_usd_options: List[float],
    proportional_pct_options: List[float],
    proportional_cap_usd_options: List[float],
    max_entries: int,
) -> List[Strategy]:
    strategies: List[Strategy] = []
    for fixed_usd in fixed_usd_options:
        for entries in range(1, max_entries + 1):
            strategies.append(
                Strategy(
                    name=f"fixed${fixed_usd:.2f}|entries{entries}|{EXIT_MODE}",
                    copy_mode="fixed_usd",
                    fixed_usd=fixed_usd,
                    proportional_pct=0.0,
                    proportional_cap_usd=None,
                    max_entries_per_market=entries,
                    exit_mode=EXIT_MODE,
                )
            )

    for proportional_pct in proportional_pct_options:
        for proportional_cap_usd in proportional_cap_usd_options:
            for entries in range(1, max_entries + 1):
                strategies.append(
                    Strategy(
                        name=f"prop{proportional_pct * 100:.1f}%+cap${proportional_cap_usd:.2f}|entries{entries}|{EXIT_MODE}",
                        copy_mode="proportional",
                        fixed_usd=None,
                        proportional_pct=proportional_pct,
                        proportional_cap_usd=proportional_cap_usd,
                        max_entries_per_market=entries,
                        exit_mode=EXIT_MODE,
                    )
                )
    return strategies


def _compute_maker_like_score(
    *,
    count: int,
    span_s: int,
    max_piece_usd: float,
    min_trade_size_usd: float,
    window_s: int,
) -> float:
    frag = min(1.0, max(0.0, (count - 1) / 4.0))
    piece_ratio = max_piece_usd / max(min_trade_size_usd, 1e-9)
    small_piece = 1.0 - min(1.0, piece_ratio)
    continuity = 1.0 - min(1.0, span_s / max(window_s, 1))
    score = 0.45 * frag + 0.35 * small_piece + 0.20 * continuity
    return max(0.0, min(1.0, score))


def build_replay_events_with_maker_like(events: List[TradeEvent]) -> List[TradeEvent]:
    ordered = sorted(events, key=lambda item: (item.ts, item.tx_hash, item.token_id, item.side))
    states: Dict[Tuple[str, str, float], Dict[str, Any]] = {}
    out: List[TradeEvent] = []

    for event in ordered:
        ts = int(event.ts)

        stale_keys = []
        for key, state in states.items():
            window_s = max(1, int(state.get("window_s", MAKER_LIKE_WINDOW_S)))
            if int(state.get("last_ts", 0)) < (ts - window_s):
                stale_keys.append(key)
        for key in stale_keys:
            states.pop(key, None)

        if event.side != "BUY":
            out.append(event)
            continue

        usd = float(event.usd) if isinstance(event.usd, (int, float)) else None
        if usd is None or usd >= MAKER_LIKE_MIN_TRADE_SIZE_USD or not event.token_id or event.price is None or event.price <= 0:
            out.append(event)
            continue

        out.append(replace(event, copy_signal=False))

        price_bucket = round(float(event.price), 4)
        state_key = (event.token_id, event.condition_id or "", price_bucket)
        state = states.get(state_key)
        if state is None:
            states[state_key] = {
                "first_ts": ts,
                "last_ts": ts,
                "cum_usd": usd,
                "cum_size": float(event.size) if isinstance(event.size, (int, float)) else 0.0,
                "count": 1,
                "price_sum": float(event.price),
                "max_piece_usd": usd,
                "window_s": MAKER_LIKE_WINDOW_S,
                "max_gap_s": MAKER_LIKE_MAX_GAP_S,
                "score_threshold": MAKER_LIKE_SCORE_THRESHOLD,
                "last_slug": event.market_slug,
            }
            continue

        too_far_gap = ts - int(state["last_ts"]) > int(state.get("max_gap_s", MAKER_LIKE_MAX_GAP_S))
        out_of_window = ts - int(state["first_ts"]) > int(state.get("window_s", MAKER_LIKE_WINDOW_S))
        if too_far_gap or out_of_window:
            states[state_key] = {
                "first_ts": ts,
                "last_ts": ts,
                "cum_usd": usd,
                "cum_size": float(event.size) if isinstance(event.size, (int, float)) else 0.0,
                "count": 1,
                "price_sum": float(event.price),
                "max_piece_usd": usd,
                "window_s": MAKER_LIKE_WINDOW_S,
                "max_gap_s": MAKER_LIKE_MAX_GAP_S,
                "score_threshold": MAKER_LIKE_SCORE_THRESHOLD,
                "last_slug": event.market_slug,
            }
            continue

        state["last_ts"] = ts
        state["cum_usd"] = float(state["cum_usd"]) + usd
        state["cum_size"] = float(state["cum_size"]) + (
            float(event.size) if isinstance(event.size, (int, float)) else 0.0
        )
        state["count"] = int(state["count"]) + 1
        state["price_sum"] = float(state["price_sum"]) + float(event.price)
        state["max_piece_usd"] = max(float(state["max_piece_usd"]), usd)
        state["last_slug"] = event.market_slug

        cum_usd = float(state["cum_usd"])
        if cum_usd < MAKER_LIKE_MIN_TRADE_SIZE_USD:
            continue

        score = _compute_maker_like_score(
            count=int(state["count"]),
            span_s=max(1, int(state["last_ts"]) - int(state["first_ts"])),
            max_piece_usd=float(state["max_piece_usd"]),
            min_trade_size_usd=MAKER_LIKE_MIN_TRADE_SIZE_USD,
            window_s=int(state.get("window_s", MAKER_LIKE_WINDOW_S)),
        )
        if score < float(state.get("score_threshold", MAKER_LIKE_SCORE_THRESHOLD)):
            continue

        avg_price = float(state["price_sum"]) / max(1, int(state["count"]))
        cum_size = float(state["cum_size"])
        agg_size = cum_size if cum_size > 0 else (cum_usd / avg_price if avg_price > 0 else None)
        out.append(
            TradeEvent(
                tx_hash=f"agg-{event.token_id[:12]}-{int(state['last_ts'])}-{int(state['count'])}",
                ts=int(state["last_ts"]),
                side="BUY",
                token_id=event.token_id,
                condition_id=event.condition_id,
                market_slug=str(state.get("last_slug") or event.market_slug),
                price=avg_price,
                size=agg_size,
                usd=cum_usd,
                copy_signal=True,
                is_leader_position_event=False,
                is_maker_like_aggregated=True,
                maker_like_score=score,
                aggregation_source_count=int(state["count"]),
            )
        )
        states.pop(state_key, None)

    return out


def _quantile_from_sorted(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    qq = min(1.0, max(0.0, float(q)))
    if len(values) == 1:
        return float(values[0])
    idx = (len(values) - 1) * qq
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(values[lo])
    frac = idx - lo
    return float(values[lo] + (values[hi] - values[lo]) * frac)


def summarize_buy_signal_stats(events: List[TradeEvent]) -> Dict[str, Any]:
    buy_signals = [event for event in events if event.side == "BUY" and bool(event.copy_signal)]
    buy_count = len(buy_signals)
    aggregated_buy_count = sum(1 for event in buy_signals if bool(event.is_maker_like_aggregated))

    total_buy_usd = 0.0
    market_stats: Dict[str, Dict[str, float]] = {}
    for event in buy_signals:
        market_key = event.condition_id or event.token_id
        stat = market_stats.setdefault(market_key, {"count": 0.0, "usd": 0.0})
        stat["count"] += 1.0
        if isinstance(event.usd, (int, float)) and event.usd > 0:
            usd = float(event.usd)
            stat["usd"] += usd
            total_buy_usd += usd

    unique_market_count = len(market_stats)
    market_bet_counts = sorted(float(stat.get("count", 0.0)) for stat in market_stats.values())
    return {
        "buy_signal_count": buy_count,
        "aggregated_buy_signal_count": aggregated_buy_count,
        "buy_signal_market_count": unique_market_count,
        "buy_signal_total_usd": total_buy_usd,
        "avg_bets_per_market": buy_count / unique_market_count if buy_count > 0 and unique_market_count > 0 else None,
        "avg_usd_per_market": total_buy_usd / unique_market_count if total_buy_usd > 0 and unique_market_count > 0 else None,
        "market_bet_count_distribution": {
            "p50": _quantile_from_sorted(market_bet_counts, 0.50),
            "p75": _quantile_from_sorted(market_bet_counts, 0.75),
            "p90": _quantile_from_sorted(market_bet_counts, 0.90),
            "p95": _quantile_from_sorted(market_bet_counts, 0.95),
            "max": market_bet_counts[-1] if market_bet_counts else None,
        },
    }


def leader_trade_size(event: TradeEvent) -> Optional[float]:
    if event.size is not None and event.size > 0:
        return float(event.size)
    if event.usd is not None and event.price is not None and event.price > 0:
        return float(event.usd) / float(event.price)
    return None


def leader_trade_usd(event: TradeEvent) -> Optional[float]:
    if event.usd is not None and event.usd > 0:
        return float(event.usd)
    if event.size is not None and event.size > 0 and event.price is not None and event.price > 0:
        return float(event.size) * float(event.price)
    return None


def compute_trade_fee_usdc(*, share_qty: float, price: float, fee_rate: float, fee_exponent: float) -> float:
    if share_qty <= 0 or price <= 0 or fee_rate <= 0:
        return 0.0
    shape = price * (1.0 - price)
    factor = fee_rate * (max(0.0, shape) ** fee_exponent)
    return share_qty * price * factor if factor > 0 else 0.0


def run_simulation(
    events: List[TradeEvent],
    strategies: List[Strategy],
    *,
    buy_price_premium_pct: float,
    buy_min_price: float,
    buy_max_price: float,
    sell_min_price: float,
    sell_max_price: float,
    sell_slippage_pct: float,
    anti_amplification_guard_enabled: bool,
    max_our_vs_leader_per_trade: float,
    max_our_vs_leader_per_market: float,
    fee_enabled: bool = False,
    fee_rate: float = 0.0,
    fee_exponent: float = 1.0,
) -> List[StrategyState]:
    states = [StrategyState(strategy) for strategy in strategies]
    guard_trade_limit = max(0.0, float(max_our_vs_leader_per_trade))
    guard_market_limit = max(0.0, float(max_our_vs_leader_per_market))
    effective_fee_rate = max(0.0, float(fee_rate))
    effective_fee_exponent = max(0.0, float(fee_exponent))

    for event in events:
        if not event.token_id:
            continue

        event_size = leader_trade_size(event)
        event_usd = leader_trade_usd(event)
        for state in states:
            leader_open = state.leader_open_sizes.get(event.token_id, 0.0)

            if event.side == "BUY":
                if event.is_leader_position_event and event_size is not None and event_size > 0:
                    state.leader_open_sizes[event.token_id] = leader_open + event_size

                if not event.copy_signal:
                    continue

                market_key = event.condition_id or event.token_id
                if event_usd is not None and event_usd > 0:
                    state.leader_market_buy_usd[market_key] = state.leader_market_buy_usd.get(market_key, 0.0) + event_usd
                current_entries = state.buy_counts.get(market_key, 0)
                if current_entries >= state.strategy.max_entries_per_market:
                    state.skipped_entry_limit += 1
                    continue
                if event.price is None or event.price <= 0:
                    state.skipped_missing_value += 1
                    continue
                if event.price < buy_min_price or event.price > buy_max_price:
                    state.skipped_buy_price += 1
                    continue

                our_buy_price = event.price * (1.0 + buy_price_premium_pct)
                if our_buy_price >= 1.0:
                    our_buy_price = 0.999999
                if our_buy_price < buy_min_price or our_buy_price > buy_max_price:
                    state.skipped_buy_price += 1
                    continue

                if state.strategy.copy_mode == "fixed_usd":
                    our_usd = state.strategy.fixed_usd or 0.0
                else:
                    if event.usd is None or event.usd <= 0:
                        state.skipped_missing_value += 1
                        continue
                    our_usd = event.usd * state.strategy.proportional_pct
                    cap_usd = state.strategy.proportional_cap_usd
                    if cap_usd is not None and cap_usd > 0:
                        our_usd = min(our_usd, cap_usd)

                if our_usd <= 0:
                    state.skipped_missing_value += 1
                    continue

                original_our_usd = float(our_usd)
                if event_usd is not None and event_usd > 0 and original_our_usd > event_usd:
                    state.oversize_before_guard_count += 1

                if anti_amplification_guard_enabled:
                    allowed_by_trade = event_usd * guard_trade_limit if event_usd is not None and event_usd > 0 else float("inf")
                    allowed_by_market = float("inf")
                    leader_market_total = state.leader_market_buy_usd.get(market_key, 0.0)
                    if leader_market_total > 0:
                        market_cap_total = leader_market_total * guard_market_limit
                        allowed_by_market = market_cap_total - state.our_market_buy_usd.get(market_key, 0.0)

                    allowed = min(original_our_usd, allowed_by_trade, allowed_by_market)
                    if allowed <= 1e-12:
                        state.guard_skipped_count += 1
                        state.guard_trimmed_usd += max(0.0, original_our_usd)
                        continue
                    if allowed + 1e-12 < original_our_usd:
                        state.guard_trimmed_count += 1
                        state.guard_trimmed_usd += max(0.0, original_our_usd - allowed)
                        our_usd = allowed

                if event_usd is not None and event_usd > 0 and our_usd > event_usd:
                    state.oversize_after_guard_count += 1

                gross_buy_size = our_usd / our_buy_price
                fee_buy_usdc = (
                    compute_trade_fee_usdc(
                        share_qty=gross_buy_size,
                        price=our_buy_price,
                        fee_rate=effective_fee_rate,
                        fee_exponent=effective_fee_exponent,
                    )
                    if fee_enabled and effective_fee_rate > 0
                    else 0.0
                )
                fee_buy_shares = fee_buy_usdc / our_buy_price if fee_buy_usdc > 0 and our_buy_price > 0 else 0.0
                our_size = gross_buy_size - fee_buy_shares
                if our_size <= 1e-12:
                    state.skipped_missing_value += 1
                    continue

                pos = state.positions.get(event.token_id)
                if pos is None:
                    pos = Position(market_key=market_key)
                    state.positions[event.token_id] = pos
                pos.size += our_size
                pos.cost += our_usd
                if not pos.market_key:
                    pos.market_key = market_key

                state.total_buy_cost += our_usd
                state.buy_counts[market_key] = current_entries + 1
                state.copied_buys += 1
                state.our_market_buy_usd[market_key] = state.our_market_buy_usd.get(market_key, 0.0) + our_usd

            else:
                sell_ratio = None
                if event.is_leader_position_event and event_size is not None and event_size > 0:
                    if leader_open > 1e-12:
                        sell_ratio = min(1.0, event_size / leader_open)
                    state.leader_open_sizes[event.token_id] = max(0.0, leader_open - event_size)
                if event.price is None or event.price <= 0:
                    state.skipped_sell_price += 1
                    continue

                actual_sell_price = event.price * (1.0 - sell_slippage_pct)
                if actual_sell_price <= 0:
                    state.skipped_sell_price += 1
                    continue
                if actual_sell_price < sell_min_price or actual_sell_price > sell_max_price:
                    state.skipped_sell_price += 1
                    continue
                if sell_ratio is None or sell_ratio <= 0:
                    continue

                pos = state.positions.get(event.token_id)
                if pos is None or pos.size <= 1e-12:
                    continue

                sell_size = min(pos.size * sell_ratio, pos.size)
                if sell_size <= 1e-12:
                    continue

                avg_cost = pos.cost / pos.size if pos.size > 0 else 0.0
                fee_sell_usdc = (
                    compute_trade_fee_usdc(
                        share_qty=sell_size,
                        price=actual_sell_price,
                        fee_rate=effective_fee_rate,
                        fee_exponent=effective_fee_exponent,
                    )
                    if fee_enabled and effective_fee_rate > 0
                    else 0.0
                )
                state.realized_pnl += sell_size * (actual_sell_price - avg_cost) - fee_sell_usdc
                pos.size -= sell_size
                pos.cost -= avg_cost * sell_size
                state.mirrored_sells += 1
                if pos.size <= 1e-12:
                    state.positions.pop(event.token_id, None)

    return states


def _parse_json_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, list) else None
    return None


def _extract_resolved_price_from_market(market: Dict[str, Any], token_id: str) -> Optional[PriceInfo]:
    closed = bool(market.get("closed"))
    clob_ids = _parse_json_list(market.get("clobTokenIds"))
    outcome_prices = _parse_json_list(market.get("outcomePrices"))
    if not closed or not clob_ids or not outcome_prices:
        return None
    for idx, clob_id in enumerate(clob_ids):
        if str(clob_id) != str(token_id) or idx >= len(outcome_prices):
            continue
        price = as_float(outcome_prices[idx])
        if price is not None:
            return PriceInfo(token_id=token_id, price=price, resolved=True, source="resolution")
    return None


def _fetch_resolved_price_from_event_lookup(
    session: requests.Session,
    token_id: str,
    *,
    market_slug: str,
    condition_id: str,
    timeout_s: float,
) -> Optional[PriceInfo]:
    slug = str(market_slug or "").strip()
    if not slug:
        return None

    data = http_get_json(
        session,
        GAMMA_EVENTS_API,
        params={"slug": slug, "limit": 1},
        timeout_s=timeout_s,
        max_retries=3,
    )
    if not isinstance(data, list):
        return None

    wanted_condition = str(condition_id or "").strip().lower()
    for event in data:
        if not isinstance(event, dict):
            continue
        markets = event.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            market_condition = str(market.get("conditionId") or "").strip().lower()
            market_slug_value = str(market.get("slug") or "").strip()
            clob_ids = _parse_json_list(market.get("clobTokenIds")) or []
            token_match = any(str(clob_id) == str(token_id) for clob_id in clob_ids)
            condition_match = bool(wanted_condition) and market_condition == wanted_condition
            slug_match = bool(slug) and market_slug_value == slug
            if not token_match and not condition_match and not slug_match:
                continue
            price_info = _extract_resolved_price_from_market(market, token_id)
            if price_info is not None:
                return price_info
    return None


def _open_price_cache_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS price_cache (
            token_id TEXT PRIMARY KEY,
            price REAL,
            resolved INTEGER NOT NULL,
            source TEXT NOT NULL,
            fetched_at_ts INTEGER NOT NULL,
            expires_at_ts INTEGER
        )
        """
    )
    return conn


def _cache_expiry_ts(info: PriceInfo, *, now_ts: int) -> Optional[int]:
    if info.source == "resolution" and info.resolved and info.price is not None:
        return None
    if info.source == "midpoint" and info.price is not None:
        return now_ts + PRICE_CACHE_MIDPOINT_TTL_S
    return now_ts + PRICE_CACHE_MISSING_TTL_S


def _load_cached_prices(
    conn: sqlite3.Connection,
    token_ids: List[str],
    *,
    now_ts: int,
) -> Tuple[Dict[str, PriceInfo], List[str], Dict[str, int]]:
    cached: Dict[str, PriceInfo] = {}
    cached_tokens = set()
    expired = 0

    for start in range(0, len(token_ids), 500):
        chunk = token_ids[start : start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT token_id, price, resolved, source, expires_at_ts FROM price_cache WHERE token_id IN ({placeholders})",
            chunk,
        ).fetchall()
        for token_id, price_raw, resolved_raw, source_raw, expires_raw in rows:
            token_str = str(token_id)
            cached_tokens.add(token_str)
            expires_at_ts = as_int(expires_raw)
            if expires_at_ts is not None and expires_at_ts <= now_ts:
                expired += 1
                continue
            cached[token_str] = PriceInfo(
                token_id=token_str,
                price=as_float(price_raw),
                resolved=bool(as_int(resolved_raw) or 0),
                source=str(source_raw or "missing"),
            )

    to_fetch = [token_id for token_id in token_ids if token_id not in cached]
    miss = max(0, len(token_ids) - len(cached_tokens))
    return cached, to_fetch, {
        "total": len(token_ids),
        "hit": len(cached),
        "expired": expired,
        "miss": miss,
        "online_fetch": len(to_fetch),
    }


def _save_cached_prices(conn: sqlite3.Connection, price_map: Dict[str, PriceInfo], *, now_ts: int) -> None:
    if not price_map:
        return
    rows = []
    for token_id, info in price_map.items():
        rows.append(
            (
                str(token_id),
                as_float(info.price),
                1 if info.resolved else 0,
                str(info.source or "missing"),
                now_ts,
                _cache_expiry_ts(info, now_ts=now_ts),
            )
        )
    conn.executemany(
        """
        INSERT INTO price_cache (token_id, price, resolved, source, fetched_at_ts, expires_at_ts)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(token_id) DO UPDATE SET
            price=excluded.price,
            resolved=excluded.resolved,
            source=excluded.source,
            fetched_at_ts=excluded.fetched_at_ts,
            expires_at_ts=excluded.expires_at_ts
        """,
        rows,
    )
    conn.commit()


def fetch_token_price_info(
    session: requests.Session,
    token_id: str,
    *,
    timeout_s: float,
    lookup_context: Optional[PriceLookupContext] = None,
) -> PriceInfo:
    try:
        data = http_get_json(
            session,
            GAMMA_MARKETS_API,
            params={"clob_token_ids": token_id, "limit": 1},
            timeout_s=timeout_s,
            max_retries=3,
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            price_info = _extract_resolved_price_from_market(data[0], token_id)
            if price_info is not None:
                return price_info
    except Exception:
        pass

    try:
        data = http_get_json(
            session,
            CLOB_MIDPOINT_API,
            params={"token_id": token_id},
            timeout_s=timeout_s,
            max_retries=3,
        )
        if isinstance(data, dict):
            for key in ("mid", "midpoint", "price"):
                price = as_float(data.get(key))
                if price is not None:
                    return PriceInfo(token_id=token_id, price=price, resolved=False, source="midpoint")
    except Exception:
        pass

    if lookup_context is not None:
        try:
            price_info = _fetch_resolved_price_from_event_lookup(
                session,
                token_id,
                market_slug=lookup_context.market_slug,
                condition_id=lookup_context.condition_id,
                timeout_s=timeout_s,
            )
            if price_info is not None:
                return price_info
        except Exception:
            pass

    return PriceInfo(token_id=token_id, price=None, resolved=False, source="missing")


def fetch_prices_for_tokens(
    token_ids: List[str],
    *,
    timeout_s: float,
    workers: int,
    lookup_context_by_token: Optional[Dict[str, PriceLookupContext]] = None,
) -> Dict[str, PriceInfo]:
    uniq_tokens = sorted(set(token_id for token_id in token_ids if token_id))
    if not uniq_tokens:
        return {}

    now_ts = int(time.time())
    out: Dict[str, PriceInfo] = {}
    to_fetch = list(uniq_tokens)
    cache_stats = {"total": len(uniq_tokens), "hit": 0, "expired": 0, "miss": len(uniq_tokens), "online_fetch": len(uniq_tokens)}

    cache_available = True
    try:
        conn = _open_price_cache_db(PRICE_CACHE_DB_PATH)
        try:
            cached_map, to_fetch, cache_stats = _load_cached_prices(conn, uniq_tokens, now_ts=now_ts)
            out.update(cached_map)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        cache_available = False
        print(f"[price-cache] disabled due to error: {exc}")

    print(
        "[price-cache] "
        f"total={cache_stats['total']} hit={cache_stats['hit']} "
        f"expired={cache_stats['expired']} miss={cache_stats['miss']} "
        f"online_fetch={cache_stats['online_fetch']}"
    )

    fetched_online: Dict[str, PriceInfo] = {}
    if to_fetch:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            future_map = {
                executor.submit(
                    fetch_token_price_info,
                    requests.Session(),
                    token_id,
                    timeout_s=timeout_s,
                    lookup_context=(lookup_context_by_token or {}).get(token_id),
                ): token_id
                for token_id in to_fetch
            }
            completed = 0
            total = len(future_map)
            for future in as_completed(future_map):
                token_id = future_map[future]
                try:
                    fetched_online[token_id] = future.result()
                except Exception:
                    fetched_online[token_id] = PriceInfo(token_id=token_id, price=None, resolved=False, source="missing")
                completed += 1
                if completed % 50 == 0 or completed == total:
                    print(f"[price] fetched {completed}/{total}")
        out.update(fetched_online)

    if cache_available and fetched_online:
        try:
            conn = _open_price_cache_db(PRICE_CACHE_DB_PATH)
            try:
                _save_cached_prices(conn, fetched_online, now_ts=int(time.time()))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[price-cache] save skipped due to error: {exc}")

    return out


def build_strategy_result(state: StrategyState, price_map: Dict[str, PriceInfo]) -> Dict[str, Any]:
    settlement_pnl = 0.0
    unrealized_pnl = 0.0
    for token_id, pos in state.positions.items():
        if pos.size <= 1e-12:
            continue
        info = price_map.get(token_id)
        if info is None or info.price is None:
            continue
        pnl = pos.size * info.price - pos.cost
        if info.resolved:
            settlement_pnl += pnl
        else:
            unrealized_pnl += pnl

    total_pnl = state.realized_pnl + settlement_pnl + unrealized_pnl
    roi = (total_pnl / state.total_buy_cost) if state.total_buy_cost > 0 else None
    return {
        "strategy": state.strategy.name,
        "copy_mode": state.strategy.copy_mode,
        "fixed_usd": state.strategy.fixed_usd,
        "proportional_pct": state.strategy.proportional_pct,
        "proportional_cap_usd": state.strategy.proportional_cap_usd,
        "max_entries_per_market": state.strategy.max_entries_per_market,
        "total_pnl": round(total_pnl, 6),
        "roi": round(roi, 6) if roi is not None else None,
        "total_buy_cost": round(state.total_buy_cost, 6),
        "copied_buys": state.copied_buys,
        "mirrored_sells": state.mirrored_sells,
    }


def brief_strategy_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {field: row.get(field) for field in OUTPUT_BRIEF_FIELDS}


def build_output_payload(
    *,
    address: str,
    max_activities: int,
    premium: float,
    mirror_sell_slippage: float,
    events: List[TradeEvent],
    replay_events: List[TradeEvent],
    benchmark: Dict[str, Any],
    buy_signal_stats: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not results:
        raise RuntimeError("No strategy results available.")

    roi_ranked = sorted(results, key=strategy_roi_sort_key, reverse=True)
    pnl_ranked = sorted(results, key=strategy_sort_key, reverse=True)

    first_ts = int(events[0].ts) if events else None
    last_ts = int(events[-1].ts) if events else None
    span_days = None
    if first_ts is not None and last_ts is not None and last_ts >= first_ts:
        span_days = round((last_ts - first_ts) / 86400.0, 6)

    return {
        "generated_at": now_utc_iso(),
        "input": {
            "address": address,
            "max_activities": int(max_activities),
            "premium": round(float(premium), 6),
            "mirror_sell_slippage": round(float(mirror_sell_slippage), 6),
        },
        "summary": {
            "address": address,
            "backtest_span_days": span_days,
            "trade_count": len(events),
            "window_real_pnl": as_float(benchmark.get("actual_window_pnl_delta")),
            "avg_bets_per_market": as_float(buy_signal_stats.get("avg_bets_per_market")),
            "avg_usd_per_market": as_float(buy_signal_stats.get("avg_usd_per_market")),
            "fetched_events": len(events),
            "replay_events": len(replay_events),
            "buy_signal_count": int(buy_signal_stats.get("buy_signal_count", 0) or 0),
            "aggregated_buy_signal_count": int(buy_signal_stats.get("aggregated_buy_signal_count", 0) or 0),
            "benchmark_error": str(benchmark.get("error") or "") or None,
        },
        "best_returns": {
            "best_by_roi": brief_strategy_row(roi_ranked[0]),
            "best_by_total_pnl": brief_strategy_row(pnl_ranked[0]),
        },
        "top_strategies": {
            "top5_by_roi": [brief_strategy_row(row) for row in roi_ranked[:5]],
            "top5_by_total_pnl": [brief_strategy_row(row) for row in pnl_ranked[:5]],
        },
    }


def write_output(payload: Dict[str, Any], *, out_dir: Path, address: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"public_copytrade_{short_address(address)}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Public-facing Polymarket copytrade CLI")
    ap.add_argument("--address", required=True, help="Leader wallet address")
    ap.add_argument("--max-activities", type=int, default=DEFAULT_MAX_ACTIVITIES, help=f"Max TRADE activities to fetch (default: {DEFAULT_MAX_ACTIVITIES})")
    ap.add_argument("--premium", type=float, default=DEFAULT_PREMIUM, help=f"Buy price premium as a decimal (default: {DEFAULT_PREMIUM})")
    ap.add_argument("--mirror-sell-slippage", type=float, default=DEFAULT_MIRROR_SELL_SLIPPAGE, help=f"Mirror sell slippage as a decimal (default: {DEFAULT_MIRROR_SELL_SLIPPAGE})")
    return ap.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    address = str(args.address or "").strip().lower()
    if not address:
        raise SystemExit("--address is required")
    args.address = address
    if int(args.max_activities) <= 0:
        raise SystemExit("--max-activities must be > 0")
    if float(args.premium) < 0 or float(args.premium) >= 1:
        raise SystemExit("--premium must be in [0, 1)")
    if float(args.mirror_sell_slippage) < 0 or float(args.mirror_sell_slippage) >= 1:
        raise SystemExit("--mirror-sell-slippage must be in [0, 1)")


def run_analysis(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    validate_args(args)
    print("=== Public Copytrade CLI Start ===")
    print(f"address={args.address}")
    print(
        f"max_activities={int(args.max_activities)} "
        f"premium={float(args.premium):.4f} "
        f"mirror_sell_slippage={float(args.mirror_sell_slippage):.4f}"
    )

    strategies = generate_strategies(
        FIXED_USD_OPTIONS,
        PROPORTIONAL_PCT_OPTIONS,
        PROPORTIONAL_CAP_USD_OPTIONS,
        MAX_ENTRIES_PER_MARKET,
    )
    print(f"[sim] strategy_count={len(strategies)}")

    session = requests.Session()
    events = fetch_activity_events(
        session,
        args.address,
        max_activities=max(1, int(args.max_activities)),
        page_limit=DEFAULT_PAGE_LIMIT,
        timeout_s=DEFAULT_TIMEOUT_S,
    )
    if not events:
        raise SystemExit("No activity fetched. Check address or network.")
    print(f"[fetch] deduped_events={len(events)}")

    replay_events = build_replay_events_with_maker_like(events)
    buy_signal_stats = summarize_buy_signal_stats(replay_events)
    print(
        "[sim] maker-like replay: "
        f"replay_events={len(replay_events)} "
        f"buy_signals={buy_signal_stats.get('buy_signal_count', 0)} "
        f"aggregated_buy_signals={buy_signal_stats.get('aggregated_buy_signal_count', 0)}"
    )

    benchmark = compute_tracked_window_benchmark(
        session,
        args.address,
        first_ts=int(events[0].ts) if events else None,
        last_ts=int(events[-1].ts) if events else None,
        timeout_s=DEFAULT_TIMEOUT_S,
    )
    if benchmark.get("error"):
        print(f"[benchmark] warning: {benchmark['error']}")
    else:
        print(f"[benchmark] actual_window_pnl_delta={benchmark.get('actual_window_pnl_delta')}")

    price_token_ids = sorted(
        {
            str(event.token_id)
            for event in replay_events
            if event.side == "BUY" and bool(event.copy_signal) and event.token_id
        }
    )
    lookup_context_by_token: Dict[str, PriceLookupContext] = {}
    for event in replay_events:
        if event.side != "BUY" or not bool(event.copy_signal) or not event.token_id:
            continue
        token_id = str(event.token_id)
        context = lookup_context_by_token.get(token_id)
        if context is None:
            lookup_context_by_token[token_id] = PriceLookupContext(
                market_slug=str(event.market_slug or ""),
                condition_id=str(event.condition_id or ""),
            )
            continue
        if not context.market_slug and event.market_slug:
            context.market_slug = str(event.market_slug)
        if not context.condition_id and event.condition_id:
            context.condition_id = str(event.condition_id)
    print(f"[price] unique_candidate_tokens={len(price_token_ids)}")
    price_map = fetch_prices_for_tokens(
        price_token_ids,
        timeout_s=DEFAULT_TIMEOUT_S,
        workers=DEFAULT_PRICE_WORKERS,
        lookup_context_by_token=lookup_context_by_token,
    )

    states = run_simulation(
        replay_events,
        strategies,
        buy_price_premium_pct=float(args.premium),
        buy_min_price=BUY_MIN_PRICE,
        buy_max_price=BUY_MAX_PRICE,
        sell_min_price=SELL_MIN_PRICE,
        sell_max_price=SELL_MAX_PRICE,
        sell_slippage_pct=float(args.mirror_sell_slippage),
        anti_amplification_guard_enabled=ANTI_AMPLIFICATION_GUARD_ENABLED,
        max_our_vs_leader_per_trade=MAX_OUR_VS_LEADER_PER_TRADE,
        max_our_vs_leader_per_market=MAX_OUR_VS_LEADER_PER_MARKET,
        fee_enabled=FEE_ENABLED,
        fee_rate=FEE_RATE,
        fee_exponent=FEE_EXPONENT,
    )
    results = [build_strategy_result(state, price_map) for state in states]

    payload = build_output_payload(
        address=args.address,
        max_activities=int(args.max_activities),
        premium=float(args.premium),
        mirror_sell_slippage=float(args.mirror_sell_slippage),
        events=events,
        replay_events=replay_events,
        benchmark=benchmark,
        buy_signal_stats=buy_signal_stats,
        results=results,
    )
    out_path = write_output(payload, out_dir=OUTPUT_DIR, address=args.address)
    return payload, out_path


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    payload, out_path = run_analysis(args)
    print("\n=== Output ===")
    print(f"json: {out_path}")
    print(
        "best_by_roi: "
        f"{payload['best_returns']['best_by_roi']['strategy']} "
        f"| roi={payload['best_returns']['best_by_roi']['roi']} "
        f"| total_pnl={payload['best_returns']['best_by_roi']['total_pnl']}"
    )
    print(
        "best_by_total_pnl: "
        f"{payload['best_returns']['best_by_total_pnl']['strategy']} "
        f"| roi={payload['best_returns']['best_by_total_pnl']['roi']} "
        f"| total_pnl={payload['best_returns']['best_by_total_pnl']['total_pnl']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
