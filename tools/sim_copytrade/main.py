from __future__ import annotations

import argparse
import bisect
import csv
import functools
import gzip
import html
import json
import math
import shutil
import sqlite3
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for _path in (SIM_ROOT, SIM_PARENT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from polymarket_public_api import USER_PNL_METRICS_FIDELITY, USER_PNL_METRICS_INTERVAL, fetch_user_pnl_series

DATA_API = "https://data-api.polymarket.com/activity"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"
CLOB_MIDPOINT_API = "https://clob.polymarket.com/midpoint"

MAKER_LIKE_MIN_TRADE_SIZE_USD = 500.0
MAKER_LIKE_WINDOW_S = 360 * 60
MAKER_LIKE_MAX_GAP_S = 30 * 60
MAKER_LIKE_SCORE_THRESHOLD = 0.60
MIRROR_SELL_SLIPPAGE_PCT = 0.01
MAIN_BUY_PREMIUM_PCT = 0.03
MAIN_SELL_SLIPPAGE_PCT = MIRROR_SELL_SLIPPAGE_PCT
SPORTS_FEE_RATE = 0.03
SPORTS_FEE_EXPONENT = 1.0
WINDOW_SPLIT_COUNT = 4
WINDOW_REPORT_TOP_N = 10
PRICE_CACHE_DB_PATH = Path(__file__).resolve().parent / "output" / "price_cache.sqlite"
PRICE_CACHE_MIDPOINT_TTL_S = 15 * 60
PRICE_CACHE_MISSING_TTL_S = 2 * 60
RUN_CACHE_MAX_AGE_S = 24 * 60 * 60

OPT_TOP_BOUNDARY_N = 10
OPT_TOP_BOUNDARY_RATIO = 0.60
OPT_MAX_FIXED_USD = 400.0
OPT_MAX_PROP_CAP_USD = 400.0
OPT_MAX_PROP_PCT = 0.12
OPT_MAX_ENTRIES = 20
OPT_DEFAULT_ROI_TIE_THRESHOLD = 0.001
ONLY_EXIT_MODE = "mirror_sell"
AI_IMPROVE_TOP_CANDIDATES_PER_ROUND = 64
AI_IMPROVE_MAX_NO_IMPROVE_ROUNDS = 3

AI_IMPROVE_BOUND_PROFILES: Dict[str, Dict[str, float]] = {
    "conservative": {
        "max_fixed_usd": 200.0,
        "max_proportional_cap_usd": 100.0,
        "max_proportional_pct": 0.03,
        "max_entries": 15.0,
    },
    "moderate": {
        "max_fixed_usd": 350.0,
        "max_proportional_cap_usd": 200.0,
        "max_proportional_pct": 0.06,
        "max_entries": 24.0,
    },
    "aggressive": {
        "max_fixed_usd": 500.0,
        "max_proportional_cap_usd": 300.0,
        "max_proportional_pct": 0.08,
        "max_entries": 30.0,
    },
}


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


class StrategyState:
    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.positions: Dict[str, Position] = {}
        self.buy_counts: Dict[str, int] = {}
        self.leader_open_sizes: Dict[str, float] = {}
        self.leader_market_buy_usd: Dict[str, float] = {}
        self.our_market_buy_usd: Dict[str, float] = {}
        self.market_realized_pnl: Dict[str, float] = {}
        self.market_follow_buys: Dict[str, int] = {}
        self.market_buy_cost: Dict[str, float] = {}

        self.total_buy_cost: float = 0.0
        self.realized_pnl: float = 0.0

        self.copied_buys: int = 0
        self.mirrored_sells: int = 0
        self.skipped_entry_limit: int = 0
        self.skipped_buy_price: int = 0
        self.skipped_sell_price: int = 0
        self.skipped_missing_value: int = 0
        self.guard_trimmed_count: int = 0
        self.guard_trimmed_usd: float = 0.0
        self.guard_skipped_count: int = 0
        self.oversize_before_guard_count: int = 0
        self.oversize_after_guard_count: int = 0


COPY_MODE_CN = {
    "fixed_usd": "固定金额跟单",
    "proportional": "比例跟单",
}

EXIT_MODE_CN = {
    "hold_to_resolution": "持有到结算",
    "mirror_sell": "镜像卖出",
}

RESULT_FLOAT_FIELDS = {
    "fixed_usd",
    "proportional_pct",
    "proportional_cap_usd",
    "total_buy_cost",
    "realized_pnl",
    "settlement_pnl",
    "unrealized_pnl",
    "total_pnl",
    "roi",
    "capital_ratio_vs_leader_buy_flow",
    "scaled_benchmark_pnl",
    "normalized_gap",
    "capture_rate",
    "guard_trimmed_usd",
    "oversize_event_rate_before_guard",
    "oversize_event_rate_after_guard",
}

RESULT_INT_FIELDS = {
    "max_entries_per_market",
    "copied_buys",
    "mirrored_sells",
    "open_positions",
    "unresolved_positions",
    "missing_price_positions",
    "skipped_entry_limit",
    "skipped_buy_price",
    "skipped_sell_price",
    "skipped_missing_value",
    "guard_trimmed_count",
    "guard_skipped_count",
    "oversize_before_guard_count",
    "oversize_after_guard_count",
}


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


def parse_epoch(ts_raw: Any) -> Optional[int]:
    if isinstance(ts_raw, (int, float)):
        n = int(ts_raw)
        if n > 10_000_000_000:
            n = n // 1000
        return n if n > 0 else None
    if isinstance(ts_raw, str):
        s = ts_raw.strip()
        if not s:
            return None
        if s.isdigit():
            n = int(s)
            if n > 10_000_000_000:
                n = n // 1000
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
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def as_int(value: Any) -> Optional[int]:
    n = as_float(value)
    if n is None:
        return None
    return int(round(n))


def safe_result_float(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
    n = as_float(row.get(key))
    return n if n is not None else default


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


def sort_results_for_report(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(results, key=strategy_sort_key, reverse=True)


def strategy_normalized_gap_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float]:
    normalized_gap = as_float(row.get("normalized_gap"))
    roi = as_float(row.get("roi"))
    total_pnl = as_float(row.get("total_pnl"))
    return (
        normalized_gap if normalized_gap is not None else float("-inf"),
        roi if roi is not None else float("-inf"),
        total_pnl if total_pnl is not None else float("-inf"),
    )


def strategy_capture_rate_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float]:
    capture_rate = as_float(row.get("capture_rate"))
    normalized_gap = as_float(row.get("normalized_gap"))
    total_pnl = as_float(row.get("total_pnl"))
    return (
        capture_rate if capture_rate is not None else float("-inf"),
        normalized_gap if normalized_gap is not None else float("-inf"),
        total_pnl if total_pnl is not None else float("-inf"),
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

    ts_list = [p[0] for p in points]
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
        value = left_val
    else:
        ratio = float(target_ts - left_ts) / float(right_ts - left_ts)
        value = left_val + ratio * (right_val - left_val)
    nearest_delta = min(abs(target_ts - left_ts), abs(target_ts - right_ts))
    return {
        "value": value,
        "extrapolated": False,
        "nearest_point_delta_s": int(nearest_delta),
        "segment_span_s": int(max(0, right_ts - left_ts)),
        "left_ts": left_ts,
        "right_ts": right_ts,
    }


def compute_tracked_window_benchmark(
    session: requests.Session,
    address: str,
    *,
    first_ts: Optional[int],
    last_ts: Optional[int],
) -> Dict[str, Any]:
    benchmark: Dict[str, Any] = {
        "mode": "tracked_window",
        "actual_window_pnl_delta": None,
        "series_points": 0,
        "series_first_ts": None,
        "series_last_ts": None,
        "series_first_utc": None,
        "series_last_utc": None,
        "interpolation_quality": {
            "start": None,
            "end": None,
        },
    }

    if first_ts is None or last_ts is None or last_ts < first_ts:
        benchmark["error"] = "invalid tracked window timestamps"
        return benchmark

    try:
        series = fetch_user_pnl_series(
            session,
            address,
            interval=USER_PNL_METRICS_INTERVAL,
            fidelity=USER_PNL_METRICS_FIDELITY,
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
    parsed.sort(key=lambda x: x[0])

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

    s_val = as_float(start_info.get("value"))
    e_val = as_float(end_info.get("value"))
    if s_val is not None and e_val is not None:
        benchmark["window_start_pnl"] = s_val
        benchmark["window_end_pnl"] = e_val
        benchmark["actual_window_pnl_delta"] = e_val - s_val
    else:
        benchmark["error"] = "insufficient user-pnl series for interpolation"

    return benchmark


def compute_scaled_metrics(
    *,
    total_pnl: Optional[float],
    total_buy_cost: Optional[float],
    leader_buy_signal_total_usd: Optional[float],
    actual_window_pnl_delta: Optional[float],
) -> Dict[str, Optional[float]]:
    pnl = as_float(total_pnl)
    cost = as_float(total_buy_cost)
    leader_total = as_float(leader_buy_signal_total_usd)
    actual_delta = as_float(actual_window_pnl_delta)

    capital_ratio: Optional[float] = None
    if cost is not None and leader_total is not None and leader_total > 0:
        capital_ratio = cost / leader_total

    scaled_benchmark: Optional[float] = None
    if capital_ratio is not None and actual_delta is not None:
        scaled_benchmark = actual_delta * capital_ratio

    normalized_gap: Optional[float] = None
    if pnl is not None and scaled_benchmark is not None:
        normalized_gap = pnl - scaled_benchmark

    capture_rate: Optional[float] = None
    if pnl is not None and scaled_benchmark is not None and scaled_benchmark > 0:
        capture_rate = pnl / scaled_benchmark

    return {
        "capital_ratio_vs_leader_buy_flow": capital_ratio,
        "scaled_benchmark_pnl": scaled_benchmark,
        "normalized_gap": normalized_gap,
        "capture_rate": capture_rate,
    }


def apply_scaled_metrics_to_result_row(
    row: Dict[str, Any],
    *,
    leader_buy_signal_total_usd: Optional[float],
    actual_window_pnl_delta: Optional[float],
) -> Dict[str, Any]:
    metrics = compute_scaled_metrics(
        total_pnl=as_float(row.get("total_pnl")),
        total_buy_cost=as_float(row.get("total_buy_cost")),
        leader_buy_signal_total_usd=leader_buy_signal_total_usd,
        actual_window_pnl_delta=actual_window_pnl_delta,
    )
    for key, value in metrics.items():
        row[key] = round(float(value), 8) if isinstance(value, (int, float)) else None
    return row


def _leader_buy_total_from_meta(meta: Dict[str, Any]) -> Optional[float]:
    signal_stats = meta.get("buy_signal_stats")
    if isinstance(signal_stats, dict):
        n = as_float(signal_stats.get("buy_signal_total_usd"))
        if n is not None:
            return n
    return as_float(meta.get("leader_buy_signal_total_usd"))


def apply_scaled_metrics_to_results(
    results: List[Dict[str, Any]],
    *,
    meta: Dict[str, Any],
) -> List[Dict[str, Any]]:
    leader_buy_total = _leader_buy_total_from_meta(meta)
    actual_delta = as_float(meta.get("actual_window_pnl_delta"))
    for row in results:
        apply_scaled_metrics_to_result_row(
            row,
            leader_buy_signal_total_usd=leader_buy_total,
            actual_window_pnl_delta=actual_delta,
        )
    return results


def roi_tie_threshold_from_meta(meta: Dict[str, Any]) -> float:
    summary = meta.get("optimization_summary") if isinstance(meta.get("optimization_summary"), dict) else {}
    tie = as_float(summary.get("roi_tie_threshold"))
    if tie is None:
        tie = as_float(meta.get("optimizer_roi_tie_threshold"))
    if tie is None:
        tie = OPT_DEFAULT_ROI_TIE_THRESHOLD
    return max(0.0, float(tie))


def find_row_by_strategy(rows: List[Dict[str, Any]], strategy_name: Any) -> Optional[Dict[str, Any]]:
    target = str(strategy_name or "").strip()
    if not target:
        return None
    for row in rows:
        if str(row.get("strategy") or "") == target:
            return row
    return None


def pick_objective_best_for_display(
    rows: List[Dict[str, Any]],
    *,
    meta: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not rows:
        return None
    if isinstance(meta, dict):
        best_meta = meta.get("best_by_objective")
        if isinstance(best_meta, dict):
            match = find_row_by_strategy(rows, best_meta.get("strategy"))
            if isinstance(match, dict):
                return match
    tie = roi_tie_threshold_from_meta(meta or {})
    return pick_best_row_by_objective(rows, roi_tie_threshold=tie)


def format_money(value: Any) -> str:
    n = as_float(value)
    if n is None:
        return "N/A"
    return f"{n:,.2f}"


def format_ratio_pct(value: Any) -> str:
    n = as_float(value)
    if n is None:
        return "N/A"
    return f"{n * 100:.2f}%"


def format_count(value: Any) -> str:
    n = as_int(value)
    if n is None:
        return "N/A"
    return f"{n:,d}"


def format_decimal(value: Any, *, ndigits: int = 2) -> str:
    n = as_float(value)
    if n is None:
        return "N/A"
    return f"{n:.{ndigits}f}"


def parse_float_options(raw: str, *, arg_name: str) -> List[float]:
    chunks = [part.strip() for part in str(raw).split(",")]
    chunks = [part for part in chunks if part]
    if not chunks:
        raise SystemExit(f"{arg_name} must not be empty")

    out: List[float] = []
    seen: set = set()
    for chunk in chunks:
        value = as_float(chunk)
        if value is None or value <= 0:
            raise SystemExit(f"{arg_name} contains invalid positive number: {chunk}")
        key = f"{value:.8f}"
        if key in seen:
            continue
        seen.add(key)
        out.append(float(value))

    if not out:
        raise SystemExit(f"{arg_name} produced no valid values")
    return out


def parse_pct_options(raw: str, *, arg_name: str) -> List[float]:
    values = parse_float_options(raw, arg_name=arg_name)
    for value in values:
        if value <= 0 or value >= 1:
            raise SystemExit(f"{arg_name} must be in (0,1): {value}")
    return values


def format_usd_options(values: List[float]) -> str:
    return "[" + ", ".join(f"${value:.2f}" for value in values) + "]"


def format_pct_options(values: List[float]) -> str:
    return "[" + ", ".join(f"{value * 100:.2f}%" for value in values) + "]"


def format_meta_usd_options(value: Any) -> str:
    if isinstance(value, list):
        values: List[float] = []
        for item in value:
            n = as_float(item)
            if n is not None:
                values.append(float(n))
        if values:
            return format_usd_options(values)

    n = as_float(value)
    if n is not None:
        return f"[${n:.2f}]"
    return "N/A"


def format_meta_pct_options(value: Any) -> str:
    if isinstance(value, list):
        values: List[float] = []
        for item in value:
            n = as_float(item)
            if n is None:
                continue
            if n <= 0:
                continue
            values.append(float(n))
        if values:
            return format_pct_options(values)

    n = as_float(value)
    if n is not None and n > 0:
        return f"[{n * 100:.2f}%]"
    return "N/A"


def short_address(value: Any) -> str:
    if not isinstance(value, str):
        return "offline"
    s = value.strip()
    if not s:
        return "offline"
    if len(s) >= 14:
        return f"{s[:8]}_{s[-6:]}"
    compact = "".join(ch for ch in s if ch.isalnum())
    return compact or "offline"


def normalize_result_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)

    for key in RESULT_FLOAT_FIELDS:
        if key in out:
            out[key] = as_float(out.get(key))

    for key in RESULT_INT_FIELDS:
        if key in out:
            out[key] = as_int(out.get(key))

    for key in ("strategy", "copy_mode", "exit_mode"):
        if key in out and out.get(key) is not None:
            out[key] = str(out.get(key))

    if not str(out.get("exit_mode") or "").strip():
        out["exit_mode"] = ONLY_EXIT_MODE

    if not out.get("strategy"):
        copy_mode = str(out.get("copy_mode") or "unknown")
        exit_mode = str(out.get("exit_mode") or ONLY_EXIT_MODE)
        entries = as_int(out.get("max_entries_per_market")) or 0
        if copy_mode == "fixed_usd":
            fixed = as_float(out.get("fixed_usd")) or 0.0
            sizing = f"fixed${fixed:.2f}"
        elif copy_mode == "proportional":
            prop = as_float(out.get("proportional_pct")) or 0.0
            cap = as_float(out.get("proportional_cap_usd"))
            if cap is not None:
                sizing = f"prop{prop * 100:.1f}%+cap${cap:.2f}"
            else:
                sizing = f"prop{prop * 100:.1f}%"
        else:
            sizing = copy_mode
        out["strategy"] = f"{sizing}|entries{entries}|{exit_mode}"

    return out


def strategy_name_cn(row: Dict[str, Any]) -> str:
    copy_mode = str(row.get("copy_mode") or "")
    exit_mode = str(row.get("exit_mode") or ONLY_EXIT_MODE)
    entries = as_int(row.get("max_entries_per_market"))
    fixed_usd = as_float(row.get("fixed_usd"))
    proportional_pct = as_float(row.get("proportional_pct"))
    proportional_cap_usd = as_float(row.get("proportional_cap_usd"))

    if copy_mode == "fixed_usd":
        sizing_cn = f"固定金额 ${fixed_usd:.2f}" if fixed_usd is not None else "固定金额"
    elif copy_mode == "proportional":
        if proportional_pct is not None and proportional_cap_usd is not None:
            sizing_cn = f"比例 {proportional_pct * 100:.2f}% + 上限 ${proportional_cap_usd:.2f}"
        elif proportional_pct is not None:
            sizing_cn = f"比例 {proportional_pct * 100:.2f}%"
        else:
            sizing_cn = "比例"
    else:
        sizing_cn = copy_mode or "未知模式"

    copy_mode_cn = COPY_MODE_CN.get(copy_mode, copy_mode or "未知")
    exit_mode_cn = EXIT_MODE_CN.get(exit_mode, exit_mode or "未知")
    entries_cn = f"每市场最多 {entries} 次" if entries is not None else "每市场最多 N/A 次"

    return f"{copy_mode_cn} | {sizing_cn} | {entries_cn} | {exit_mode_cn}"


def strategy_name_cn_short(row: Dict[str, Any]) -> str:
    copy_mode = str(row.get("copy_mode") or "")
    exit_mode = str(row.get("exit_mode") or ONLY_EXIT_MODE)
    entries = as_int(row.get("max_entries_per_market"))
    fixed_usd = as_float(row.get("fixed_usd"))
    proportional_pct = as_float(row.get("proportional_pct"))
    proportional_cap_usd = as_float(row.get("proportional_cap_usd"))

    if copy_mode == "fixed_usd":
        left = f"固定${fixed_usd:.2f}" if fixed_usd is not None else "固定金额"
    elif copy_mode == "proportional":
        if proportional_pct is not None and proportional_cap_usd is not None:
            left = f"比例{proportional_pct * 100:.2f}%+上限${proportional_cap_usd:.2f}"
        elif proportional_pct is not None:
            left = f"比例{proportional_pct * 100:.2f}%"
        else:
            left = "比例"
    else:
        left = copy_mode or "未知"

    mid = f"入场{entries}" if entries is not None else "入场N/A"
    right = EXIT_MODE_CN.get(exit_mode, exit_mode or "未知")
    return f"{left} | {mid} | {right}"


def http_get_json(
    session: requests.Session,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout_s: float = 30.0,
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

    tx_hash = (
        row.get("transaction_hash")
        or row.get("transactionHash")
        or row.get("txHash")
        or row.get("hash")
        or ""
    )

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
    if usd is None and size is not None and price is not None:
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


def trade_event_to_dict(event: TradeEvent) -> Dict[str, Any]:
    return {
        "tx_hash": event.tx_hash,
        "ts": int(event.ts),
        "side": event.side,
        "token_id": event.token_id,
        "condition_id": event.condition_id,
        "market_slug": event.market_slug,
        "price": event.price,
        "size": event.size,
        "usd": event.usd,
        "copy_signal": bool(event.copy_signal),
        "is_leader_position_event": bool(event.is_leader_position_event),
        "is_maker_like_aggregated": bool(event.is_maker_like_aggregated),
        "maker_like_score": event.maker_like_score,
        "aggregation_source_count": event.aggregation_source_count,
    }


def trade_event_from_dict(row: Dict[str, Any]) -> Optional[TradeEvent]:
    if not isinstance(row, dict):
        return None
    ts = parse_epoch(row.get("ts"))
    side = str(row.get("side") or "").upper()
    token_id = str(row.get("token_id") or "")
    if ts is None or side not in {"BUY", "SELL"} or not token_id:
        return None
    return TradeEvent(
        tx_hash=str(row.get("tx_hash") or ""),
        ts=int(ts),
        side=side,
        token_id=token_id,
        condition_id=str(row.get("condition_id") or ""),
        market_slug=str(row.get("market_slug") or ""),
        price=as_float(row.get("price")),
        size=as_float(row.get("size")),
        usd=as_float(row.get("usd")),
        copy_signal=bool(row.get("copy_signal", True)),
        is_leader_position_event=bool(row.get("is_leader_position_event", True)),
        is_maker_like_aggregated=bool(row.get("is_maker_like_aggregated", False)),
        maker_like_score=as_float(row.get("maker_like_score")),
        aggregation_source_count=as_int(row.get("aggregation_source_count")),
    )


def save_events_to_temp_cache(cache_path: Path, events: List[TradeEvent]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_path, "wt", encoding="utf-8") as fp:
        for event in events:
            fp.write(json.dumps(trade_event_to_dict(event), ensure_ascii=False))
            fp.write("\n")


def load_events_from_temp_cache(cache_path: Path) -> List[TradeEvent]:
    if not cache_path.exists():
        return []
    out: List[TradeEvent] = []
    with gzip.open(cache_path, "rt", encoding="utf-8") as fp:
        for line in fp:
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except json.JSONDecodeError:
                continue
            event = trade_event_from_dict(row)
            if event is not None:
                out.append(event)
    out.sort(key=lambda x: (x.ts, x.tx_hash, x.token_id, x.side))
    return out


def cleanup_stale_run_cache_dirs(run_cache_root: Path, *, max_age_s: int = RUN_CACHE_MAX_AGE_S) -> int:
    if not run_cache_root.exists():
        return 0
    now_ts = int(time.time())
    removed = 0
    for path in run_cache_root.iterdir():
        if not path.is_dir():
            continue
        try:
            age_s = now_ts - int(path.stat().st_mtime)
        except OSError:
            continue
        if age_s <= max(1, int(max_age_s)):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception:
            continue
    return removed


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
            ts_candidates = [parse_epoch(r.get("timestamp")) for r in data if isinstance(r, dict)]
            ts_candidates = [t for t in ts_candidates if t is not None]
            if not ts_candidates:
                break
            oldest_ts = min(ts_candidates)

        end_cursor = oldest_ts - 1
        if len(data) < current_limit:
            break

    if len(all_events_desc) > max_activities:
        all_events_desc = all_events_desc[:max_activities]

    seen: set = set()
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

    deduped.sort(key=lambda x: (x.ts, x.tx_hash, x.token_id, x.side))
    return deduped


def split_activity_windows(
    events: List[TradeEvent],
    *,
    window_count: int,
) -> List[Dict[str, Any]]:
    count = max(1, int(window_count))
    total = len(events)
    if total <= 0:
        return []

    windows: List[Dict[str, Any]] = []
    for idx in range(count):
        start_idx = (idx * total) // count
        end_idx = ((idx + 1) * total) // count
        if start_idx >= end_idx:
            continue
        chunk = events[start_idx:end_idx]
        if not chunk:
            continue
        windows.append(
            {
                "index": idx + 1,
                "start_pos": start_idx + 1,
                "end_pos": end_idx,
                "events": chunk,
                "count": len(chunk),
                "start_ts": chunk[0].ts,
                "end_ts": chunk[-1].ts,
                "start_utc": format_utc_from_epoch(chunk[0].ts),
                "end_utc": format_utc_from_epoch(chunk[-1].ts),
            }
        )
    return windows


def generate_strategies(
    fixed_usd_options: List[float],
    proportional_pct_options: List[float],
    proportional_cap_usd_options: List[float],
    max_entries: int,
) -> List[Strategy]:
    strategies: List[Strategy] = []
    for fixed_usd in fixed_usd_options:
        for entries in range(1, max_entries + 1):
            sizing = f"fixed${fixed_usd:.2f}"
            name = f"{sizing}|entries{entries}|{ONLY_EXIT_MODE}"
            strategies.append(
                Strategy(
                    name=name,
                    copy_mode="fixed_usd",
                    fixed_usd=fixed_usd,
                    proportional_pct=0.0,
                    proportional_cap_usd=None,
                    max_entries_per_market=entries,
                    exit_mode=ONLY_EXIT_MODE,
                )
            )

    for proportional_pct in proportional_pct_options:
        for proportional_cap_usd in proportional_cap_usd_options:
            for entries in range(1, max_entries + 1):
                sizing = f"prop{proportional_pct * 100:.1f}%+cap${proportional_cap_usd:.2f}"
                name = f"{sizing}|entries{entries}|{ONLY_EXIT_MODE}"
                strategies.append(
                    Strategy(
                        name=name,
                        copy_mode="proportional",
                        fixed_usd=None,
                        proportional_pct=proportional_pct,
                        proportional_cap_usd=proportional_cap_usd,
                        max_entries_per_market=entries,
                        exit_mode=ONLY_EXIT_MODE,
                    )
                )
    return strategies


def _round_float_key(value: Any) -> Optional[float]:
    n = as_float(value)
    if n is None:
        return None
    return round(float(n), 8)


def _float_equal(a: Any, b: Any, *, tol: float = 1e-8) -> bool:
    x = as_float(a)
    y = as_float(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= tol


def optimizer_sort_key(row: Dict[str, Any]) -> Tuple[float, float, float]:
    roi = as_float(row.get("roi"))
    total_buy_cost = as_float(row.get("total_buy_cost"))
    total_pnl = as_float(row.get("total_pnl"))
    return (
        roi if roi is not None else float("-inf"),
        total_buy_cost if total_buy_cost is not None else float("-inf"),
        total_pnl if total_pnl is not None else float("-inf"),
    )


def _compare_optimizer_rows(
    a: Dict[str, Any],
    b: Dict[str, Any],
    *,
    roi_tie_threshold: float,
) -> int:
    a_roi = as_float(a.get("roi"))
    b_roi = as_float(b.get("roi"))
    if a_roi is None and b_roi is None:
        a_roi = b_roi = float("-inf")
    elif a_roi is None:
        a_roi = float("-inf")
    elif b_roi is None:
        b_roi = float("-inf")

    delta_roi = float(a_roi - b_roi)
    tie = max(0.0, float(roi_tie_threshold))
    if abs(delta_roi) > tie:
        return -1 if delta_roi > 0 else 1

    a_cost = as_float(a.get("total_buy_cost")) or float("-inf")
    b_cost = as_float(b.get("total_buy_cost")) or float("-inf")
    if a_cost != b_cost:
        return -1 if a_cost > b_cost else 1

    a_pnl = as_float(a.get("total_pnl")) or float("-inf")
    b_pnl = as_float(b.get("total_pnl")) or float("-inf")
    if a_pnl != b_pnl:
        return -1 if a_pnl > b_pnl else 1
    return 0


def sort_optimizer_rows(
    rows: List[Dict[str, Any]],
    *,
    roi_tie_threshold: float,
) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=functools.cmp_to_key(
            lambda a, b: _compare_optimizer_rows(a, b, roi_tie_threshold=roi_tie_threshold)
        ),
    )


def _select_optimizer_pool(
    rows: List[Dict[str, Any]],
    *,
    min_capital_ratio: float,
    min_copied_buys: int,
    roi_tie_threshold: float,
) -> Dict[str, Any]:
    base_cap = max(0.0, float(min_capital_ratio))
    base_buys = max(0, int(min_copied_buys))
    relaxed_cap = base_cap * 0.5
    relaxed_buys = max(50, int(round(base_buys * 2.0 / 3.0)))

    def _filter(cap_threshold: float, buy_threshold: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows:
            roi = as_float(row.get("roi"))
            cap_ratio = as_float(row.get("capital_ratio_vs_leader_buy_flow"))
            copied_buys = as_int(row.get("copied_buys"))
            if roi is None or cap_ratio is None or copied_buys is None:
                continue
            if cap_ratio >= cap_threshold and copied_buys >= buy_threshold:
                out.append(row)
        return sort_optimizer_rows(out, roi_tie_threshold=roi_tie_threshold)

    base_pool = _filter(base_cap, base_buys)
    if base_pool:
        return {
            "rows": base_pool,
            "mode": "base",
            "used_min_capital_ratio": base_cap,
            "used_min_copied_buys": base_buys,
        }

    relaxed_pool = _filter(relaxed_cap, relaxed_buys)
    if relaxed_pool:
        return {
            "rows": relaxed_pool,
            "mode": "relaxed",
            "used_min_capital_ratio": relaxed_cap,
            "used_min_copied_buys": relaxed_buys,
        }

    fallback = [row for row in rows if as_float(row.get("roi")) is not None]
    fallback = sort_optimizer_rows(fallback, roi_tie_threshold=roi_tie_threshold)
    return {
        "rows": fallback,
        "mode": "fallback_all_roi",
        "used_min_capital_ratio": None,
        "used_min_copied_buys": None,
    }


def _median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _local_slope_score(
    rows: List[Dict[str, Any]],
    *,
    value_getter: Any,
    max_value: float,
    actual_window_pnl_delta: Optional[float],
    roi_tie_threshold: float,
) -> Dict[str, Any]:
    values = sorted({_round_float_key(value_getter(row)) for row in rows if _round_float_key(value_getter(row)) is not None})
    if len(values) < 2:
        return {
            "has_slope": False,
            "positive": True,
            "score": None,
            "delta_roi": None,
            "delta_total_buy_cost": None,
            "delta_normalized_gap": None,
            "reason": "insufficient_points",
        }

    max_key = _round_float_key(max_value)
    if max_key is None or max_key not in values:
        return {
            "has_slope": False,
            "positive": True,
            "score": None,
            "delta_roi": None,
            "delta_total_buy_cost": None,
            "delta_normalized_gap": None,
            "reason": "max_not_in_points",
        }

    max_idx = values.index(max_key)
    if max_idx <= 0:
        return {
            "has_slope": False,
            "positive": True,
            "score": None,
            "delta_roi": None,
            "delta_total_buy_cost": None,
            "delta_normalized_gap": None,
            "reason": "no_previous_bucket",
        }

    prev_key = values[max_idx - 1]
    max_rows = [row for row in rows if _float_equal(value_getter(row), max_key)]
    prev_rows = [row for row in rows if _float_equal(value_getter(row), prev_key)]
    if not max_rows or not prev_rows:
        return {
            "has_slope": False,
            "positive": True,
            "score": None,
            "delta_roi": None,
            "delta_total_buy_cost": None,
            "delta_normalized_gap": None,
            "reason": "bucket_empty",
        }

    max_roi = _median([as_float(row.get("roi")) for row in max_rows if as_float(row.get("roi")) is not None])
    prev_roi = _median([as_float(row.get("roi")) for row in prev_rows if as_float(row.get("roi")) is not None])
    max_cost = _median(
        [as_float(row.get("total_buy_cost")) for row in max_rows if as_float(row.get("total_buy_cost")) is not None]
    )
    prev_cost = _median(
        [as_float(row.get("total_buy_cost")) for row in prev_rows if as_float(row.get("total_buy_cost")) is not None]
    )
    max_gap = _median(
        [as_float(row.get("normalized_gap")) for row in max_rows if as_float(row.get("normalized_gap")) is not None]
    )
    prev_gap = _median(
        [as_float(row.get("normalized_gap")) for row in prev_rows if as_float(row.get("normalized_gap")) is not None]
    )

    delta_roi = (max_roi - prev_roi) if (max_roi is not None and prev_roi is not None) else 0.0
    delta_cost = (max_cost - prev_cost) if (max_cost is not None and prev_cost is not None) else 0.0
    delta_gap = (max_gap - prev_gap) if (max_gap is not None and prev_gap is not None) else 0.0

    scale_base = as_float(actual_window_pnl_delta)
    scale = max(abs(scale_base) if scale_base is not None else 0.0, abs(max_gap or 0.0), abs(prev_gap or 0.0), 1.0)
    tie = max(0.0, float(roi_tie_threshold))
    if abs(delta_roi) > tie:
        positive = delta_roi > 0
    elif abs(delta_cost) > 1e-12:
        positive = delta_cost > 0
    else:
        positive = delta_gap > 0
    score = delta_roi + (delta_cost / max(abs(max_cost or 0.0), abs(prev_cost or 0.0), 1.0)) + (delta_gap / scale)
    return {
        "has_slope": True,
        "positive": bool(positive),
        "score": score,
        "delta_roi": delta_roi,
        "delta_total_buy_cost": delta_cost,
        "delta_normalized_gap": delta_gap,
        "reason": "ok",
        "max_bucket_value": max_key,
        "prev_bucket_value": prev_key,
    }


def propose_strategy_space_expansion(
    *,
    optimizer_rows: List[Dict[str, Any]],
    winner_row: Dict[str, Any],
    fixed_usd_options: List[float],
    proportional_pct_options: List[float],
    proportional_cap_usd_options: List[float],
    max_entry_times: int,
    actual_window_pnl_delta: Optional[float],
    roi_tie_threshold: float,
) -> Dict[str, Any]:
    top_n = max(1, int(OPT_TOP_BOUNDARY_N))
    top_rows = optimizer_rows[:top_n]

    fixed_max = max(fixed_usd_options) if fixed_usd_options else None
    pct_max = max(proportional_pct_options) if proportional_pct_options else None
    cap_max = max(proportional_cap_usd_options) if proportional_cap_usd_options else None
    entries_max = int(max_entry_times)

    def _top_ratio(rows: List[Dict[str, Any]], checker: Any) -> Dict[str, Any]:
        if not rows:
            return {"hits": 0, "count": 0, "ratio": 0.0}
        hits = sum(1 for row in rows if checker(row))
        ratio = hits / len(rows)
        return {"hits": hits, "count": len(rows), "ratio": ratio}

    winner_copy_mode = str(winner_row.get("copy_mode") or "")
    winner_fixed = as_float(winner_row.get("fixed_usd"))
    winner_pct = as_float(winner_row.get("proportional_pct"))
    winner_cap = as_float(winner_row.get("proportional_cap_usd"))
    winner_entries = as_int(winner_row.get("max_entries_per_market"))

    fixed_top_rows = [row for row in top_rows if str(row.get("copy_mode") or "") == "fixed_usd"]
    prop_top_rows = [row for row in top_rows if str(row.get("copy_mode") or "") == "proportional"]

    pressure: Dict[str, Dict[str, Any]] = {}

    if fixed_max is not None:
        fixed_ratio = _top_ratio(fixed_top_rows, lambda row: _float_equal(row.get("fixed_usd"), fixed_max))
        fixed_winner_hit = winner_copy_mode == "fixed_usd" and _float_equal(winner_fixed, fixed_max)
        fixed_trigger = fixed_winner_hit or fixed_ratio["ratio"] >= OPT_TOP_BOUNDARY_RATIO
        slope = _local_slope_score(
            [row for row in optimizer_rows if str(row.get("copy_mode") or "") == "fixed_usd"],
            value_getter=lambda row: row.get("fixed_usd"),
            max_value=fixed_max,
            actual_window_pnl_delta=actual_window_pnl_delta,
            roi_tie_threshold=roi_tie_threshold,
        )
        pressure["fixed_usd"] = {
            "max_value": fixed_max,
            "winner_hit": fixed_winner_hit,
            "top_ratio": fixed_ratio["ratio"],
            "top_hits": fixed_ratio["hits"],
            "top_count": fixed_ratio["count"],
            "triggered": fixed_trigger,
            "slope": slope,
        }

    if cap_max is not None:
        cap_ratio = _top_ratio(prop_top_rows, lambda row: _float_equal(row.get("proportional_cap_usd"), cap_max))
        cap_winner_hit = winner_copy_mode == "proportional" and _float_equal(winner_cap, cap_max)
        cap_trigger = cap_winner_hit or cap_ratio["ratio"] >= OPT_TOP_BOUNDARY_RATIO
        slope = _local_slope_score(
            [row for row in optimizer_rows if str(row.get("copy_mode") or "") == "proportional"],
            value_getter=lambda row: row.get("proportional_cap_usd"),
            max_value=cap_max,
            actual_window_pnl_delta=actual_window_pnl_delta,
            roi_tie_threshold=roi_tie_threshold,
        )
        pressure["proportional_cap_usd"] = {
            "max_value": cap_max,
            "winner_hit": cap_winner_hit,
            "top_ratio": cap_ratio["ratio"],
            "top_hits": cap_ratio["hits"],
            "top_count": cap_ratio["count"],
            "triggered": cap_trigger,
            "slope": slope,
        }

    if pct_max is not None:
        pct_ratio = _top_ratio(prop_top_rows, lambda row: _float_equal(row.get("proportional_pct"), pct_max))
        pct_winner_hit = winner_copy_mode == "proportional" and _float_equal(winner_pct, pct_max)
        pct_trigger = pct_winner_hit or pct_ratio["ratio"] >= OPT_TOP_BOUNDARY_RATIO
        slope = _local_slope_score(
            [row for row in optimizer_rows if str(row.get("copy_mode") or "") == "proportional"],
            value_getter=lambda row: row.get("proportional_pct"),
            max_value=pct_max,
            actual_window_pnl_delta=actual_window_pnl_delta,
            roi_tie_threshold=roi_tie_threshold,
        )
        pressure["proportional_pct"] = {
            "max_value": pct_max,
            "winner_hit": pct_winner_hit,
            "top_ratio": pct_ratio["ratio"],
            "top_hits": pct_ratio["hits"],
            "top_count": pct_ratio["count"],
            "triggered": pct_trigger,
            "slope": slope,
        }

    entries_ratio = _top_ratio(top_rows, lambda row: as_int(row.get("max_entries_per_market")) == entries_max)
    entries_winner_hit = winner_entries == entries_max
    entries_trigger = entries_winner_hit or entries_ratio["ratio"] >= OPT_TOP_BOUNDARY_RATIO
    entries_slope = _local_slope_score(
        optimizer_rows,
        value_getter=lambda row: row.get("max_entries_per_market"),
        max_value=float(entries_max),
        actual_window_pnl_delta=actual_window_pnl_delta,
        roi_tie_threshold=roi_tie_threshold,
    )
    pressure["max_entries_per_market"] = {
        "max_value": entries_max,
        "winner_hit": entries_winner_hit,
        "top_ratio": entries_ratio["ratio"],
        "top_hits": entries_ratio["hits"],
        "top_count": entries_ratio["count"],
        "triggered": entries_trigger,
        "slope": entries_slope,
    }

    new_fixed = list(fixed_usd_options)
    new_pct = list(proportional_pct_options)
    new_cap = list(proportional_cap_usd_options)
    new_entries = int(max_entry_times)
    changes: List[Dict[str, Any]] = []

    fixed_info = pressure.get("fixed_usd")
    if (
        fixed_info
        and bool(fixed_info.get("triggered"))
        and bool((fixed_info.get("slope") or {}).get("positive"))
        and fixed_max is not None
        and fixed_max < OPT_MAX_FIXED_USD
    ):
        next_val = min(OPT_MAX_FIXED_USD, fixed_max + 50.0)
        if not any(_float_equal(v, next_val) for v in new_fixed):
            new_fixed.append(next_val)
            changes.append({"dimension": "fixed_usd", "from": fixed_max, "to": next_val})

    cap_info = pressure.get("proportional_cap_usd")
    if (
        cap_info
        and bool(cap_info.get("triggered"))
        and bool((cap_info.get("slope") or {}).get("positive"))
        and cap_max is not None
        and cap_max < OPT_MAX_PROP_CAP_USD
    ):
        next_val = min(OPT_MAX_PROP_CAP_USD, cap_max + 50.0)
        if not any(_float_equal(v, next_val) for v in new_cap):
            new_cap.append(next_val)
            changes.append({"dimension": "proportional_cap_usd", "from": cap_max, "to": next_val})

    pct_info = pressure.get("proportional_pct")
    if (
        pct_info
        and bool(pct_info.get("triggered"))
        and bool((pct_info.get("slope") or {}).get("positive"))
        and pct_max is not None
        and pct_max < OPT_MAX_PROP_PCT
    ):
        next_val = min(OPT_MAX_PROP_PCT, round(pct_max + 0.01, 4))
        if not any(_float_equal(v, next_val) for v in new_pct):
            new_pct.append(next_val)
            changes.append({"dimension": "proportional_pct", "from": pct_max, "to": next_val})

    entries_info = pressure.get("max_entries_per_market")
    if (
        entries_info
        and bool(entries_info.get("triggered"))
        and bool((entries_info.get("slope") or {}).get("positive"))
        and new_entries < OPT_MAX_ENTRIES
    ):
        next_entries = min(OPT_MAX_ENTRIES, new_entries + 2)
        if next_entries > new_entries:
            changes.append({"dimension": "max_entries_per_market", "from": new_entries, "to": next_entries})
            new_entries = next_entries

    return {
        "expanded": bool(changes),
        "changes": changes,
        "boundary_pressure": pressure,
        "new_space": {
            "fixed_usd_options": sorted(new_fixed),
            "proportional_pct_options": sorted(new_pct),
            "proportional_cap_usd_options": sorted(new_cap),
            "max_entry_times": int(new_entries),
        },
    }


def get_ai_improve_bounds(profile: str) -> Dict[str, float]:
    key = str(profile or "aggressive").strip().lower()
    bounds = AI_IMPROVE_BOUND_PROFILES.get(key) or AI_IMPROVE_BOUND_PROFILES["aggressive"]
    return {
        "profile": key,
        "max_fixed_usd": float(bounds["max_fixed_usd"]),
        "max_proportional_cap_usd": float(bounds["max_proportional_cap_usd"]),
        "max_proportional_pct": float(bounds["max_proportional_pct"]),
        "max_entries": float(bounds["max_entries"]),
    }


def _build_strategy_name(
    *,
    copy_mode: str,
    fixed_usd: Optional[float],
    proportional_pct: Optional[float],
    proportional_cap_usd: Optional[float],
    entries: int,
) -> str:
    if copy_mode == "fixed_usd":
        sizing = f"fixed${float(fixed_usd or 0.0):.2f}"
    else:
        sizing = f"prop{float(proportional_pct or 0.0) * 100:.1f}%+cap${float(proportional_cap_usd or 0.0):.2f}"
    return f"{sizing}|entries{int(entries)}|{ONLY_EXIT_MODE}"


def _build_strategy(
    *,
    copy_mode: str,
    fixed_usd: Optional[float],
    proportional_pct: Optional[float],
    proportional_cap_usd: Optional[float],
    entries: int,
) -> Strategy:
    name = _build_strategy_name(
        copy_mode=copy_mode,
        fixed_usd=fixed_usd,
        proportional_pct=proportional_pct,
        proportional_cap_usd=proportional_cap_usd,
        entries=entries,
    )
    if copy_mode == "fixed_usd":
        return Strategy(
            name=name,
            copy_mode=copy_mode,
            fixed_usd=float(fixed_usd or 0.0),
            proportional_pct=0.0,
            proportional_cap_usd=None,
            max_entries_per_market=int(entries),
            exit_mode=ONLY_EXIT_MODE,
        )
    return Strategy(
        name=name,
        copy_mode="proportional",
        fixed_usd=None,
        proportional_pct=float(proportional_pct or 0.0),
        proportional_cap_usd=float(proportional_cap_usd or 0.0),
        max_entries_per_market=int(entries),
        exit_mode=ONLY_EXIT_MODE,
    )


def _next_float_values(
    *,
    current: float,
    steps: List[float],
    max_value: float,
    ndigits: int,
) -> List[float]:
    out: List[float] = []
    for step in steps:
        nxt = round(current + float(step), ndigits)
        if nxt <= current:
            continue
        if nxt > max_value:
            nxt = round(float(max_value), ndigits)
        if nxt <= current:
            continue
        if any(_float_equal(nxt, existing) for existing in out):
            continue
        out.append(float(nxt))
    return out


def _next_int_values(
    *,
    current: int,
    steps: List[int],
    max_value: int,
) -> List[int]:
    out: List[int] = []
    for step in steps:
        nxt = min(max_value, current + int(step))
        if nxt <= current:
            continue
        if nxt in out:
            continue
        out.append(int(nxt))
    return out


def build_ai_improve_candidates(
    *,
    optimizer_rows: List[Dict[str, Any]],
    existing_strategy_names: set[str],
    bounds: Dict[str, float],
    max_candidates: int,
) -> List[Dict[str, Any]]:
    max_fixed = float(bounds.get("max_fixed_usd") or AI_IMPROVE_BOUND_PROFILES["aggressive"]["max_fixed_usd"])
    max_cap = float(
        bounds.get("max_proportional_cap_usd")
        or AI_IMPROVE_BOUND_PROFILES["aggressive"]["max_proportional_cap_usd"]
    )
    max_pct = float(bounds.get("max_proportional_pct") or AI_IMPROVE_BOUND_PROFILES["aggressive"]["max_proportional_pct"])
    max_entries = int(round(bounds.get("max_entries") or AI_IMPROVE_BOUND_PROFILES["aggressive"]["max_entries"]))

    specs_by_name: Dict[str, Dict[str, Any]] = {}
    top_rows = optimizer_rows[: max(1, min(len(optimizer_rows), 10))]

    def _add_candidate(
        *,
        strategy_obj: Strategy,
        rank: int,
        penalty: int,
        parent_strategy: str,
        mutation: str,
    ) -> None:
        if strategy_obj.name in existing_strategy_names:
            return
        prev = specs_by_name.get(strategy_obj.name)
        priority = int(rank) * 100 + int(penalty)
        spec = {
            "strategy": strategy_obj,
            "priority": priority,
            "parent_strategy": parent_strategy,
            "mutation": mutation,
        }
        if prev is None or priority < int(prev.get("priority") or 10**9):
            specs_by_name[strategy_obj.name] = spec

    for rank, row in enumerate(top_rows, start=1):
        copy_mode = str(row.get("copy_mode") or "")
        parent_strategy = str(row.get("strategy") or f"rank_{rank}")
        entries = max(1, as_int(row.get("max_entries_per_market")) or 1)
        next_entries_values = _next_int_values(current=entries, steps=[2, 4], max_value=max_entries)

        if copy_mode == "fixed_usd":
            fixed = as_float(row.get("fixed_usd")) or 0.0
            if fixed <= 0:
                continue
            next_fixed_values = _next_float_values(
                current=float(fixed),
                steps=[50.0, 100.0],
                max_value=max_fixed,
                ndigits=2,
            )
            for next_fixed in next_fixed_values:
                _add_candidate(
                    strategy_obj=_build_strategy(
                        copy_mode="fixed_usd",
                        fixed_usd=next_fixed,
                        proportional_pct=None,
                        proportional_cap_usd=None,
                        entries=entries,
                    ),
                    rank=rank,
                    penalty=10,
                    parent_strategy=parent_strategy,
                    mutation=f"fixed_usd->{next_fixed:.2f}",
                )
            for next_entries in next_entries_values:
                _add_candidate(
                    strategy_obj=_build_strategy(
                        copy_mode="fixed_usd",
                        fixed_usd=fixed,
                        proportional_pct=None,
                        proportional_cap_usd=None,
                        entries=next_entries,
                    ),
                    rank=rank,
                    penalty=20,
                    parent_strategy=parent_strategy,
                    mutation=f"entries->{next_entries}",
                )
            if next_fixed_values and next_entries_values:
                _add_candidate(
                    strategy_obj=_build_strategy(
                        copy_mode="fixed_usd",
                        fixed_usd=next_fixed_values[0],
                        proportional_pct=None,
                        proportional_cap_usd=None,
                        entries=next_entries_values[0],
                    ),
                    rank=rank,
                    penalty=30,
                    parent_strategy=parent_strategy,
                    mutation=f"fixed+entries->{next_fixed_values[0]:.2f}/{next_entries_values[0]}",
                )
            continue

        if copy_mode != "proportional":
            continue

        pct = as_float(row.get("proportional_pct")) or 0.0
        cap = as_float(row.get("proportional_cap_usd")) or 0.0
        if pct <= 0 or cap <= 0:
            continue

        next_pct_values = _next_float_values(
            current=float(pct),
            steps=[0.01, 0.02],
            max_value=max_pct,
            ndigits=4,
        )
        next_cap_values = _next_float_values(
            current=float(cap),
            steps=[50.0, 100.0],
            max_value=max_cap,
            ndigits=2,
        )
        for next_pct in next_pct_values:
            _add_candidate(
                strategy_obj=_build_strategy(
                    copy_mode="proportional",
                    fixed_usd=None,
                    proportional_pct=next_pct,
                    proportional_cap_usd=cap,
                    entries=entries,
                ),
                rank=rank,
                penalty=10,
                parent_strategy=parent_strategy,
                mutation=f"proportional_pct->{next_pct:.4f}",
            )
        for next_cap in next_cap_values:
            _add_candidate(
                strategy_obj=_build_strategy(
                    copy_mode="proportional",
                    fixed_usd=None,
                    proportional_pct=pct,
                    proportional_cap_usd=next_cap,
                    entries=entries,
                ),
                rank=rank,
                penalty=12,
                parent_strategy=parent_strategy,
                mutation=f"proportional_cap_usd->{next_cap:.2f}",
            )
        for next_entries in next_entries_values:
            _add_candidate(
                strategy_obj=_build_strategy(
                    copy_mode="proportional",
                    fixed_usd=None,
                    proportional_pct=pct,
                    proportional_cap_usd=cap,
                    entries=next_entries,
                ),
                rank=rank,
                penalty=20,
                parent_strategy=parent_strategy,
                mutation=f"entries->{next_entries}",
            )
        if next_pct_values and next_entries_values:
            _add_candidate(
                strategy_obj=_build_strategy(
                    copy_mode="proportional",
                    fixed_usd=None,
                    proportional_pct=next_pct_values[0],
                    proportional_cap_usd=cap,
                    entries=next_entries_values[0],
                ),
                rank=rank,
                penalty=30,
                parent_strategy=parent_strategy,
                mutation=f"pct+entries->{next_pct_values[0]:.4f}/{next_entries_values[0]}",
            )
        if next_cap_values and next_entries_values:
            _add_candidate(
                strategy_obj=_build_strategy(
                    copy_mode="proportional",
                    fixed_usd=None,
                    proportional_pct=pct,
                    proportional_cap_usd=next_cap_values[0],
                    entries=next_entries_values[0],
                ),
                rank=rank,
                penalty=32,
                parent_strategy=parent_strategy,
                mutation=f"cap+entries->{next_cap_values[0]:.2f}/{next_entries_values[0]}",
            )

    specs = sorted(specs_by_name.values(), key=lambda x: int(x.get("priority") or 10**9))
    return specs[: max(1, int(max_candidates))]


def _pick_optimizer_winner(
    rows: List[Dict[str, Any]],
    *,
    min_capital_ratio: float,
    min_copied_buys: int,
    roi_tie_threshold: float,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    pool = _select_optimizer_pool(
        rows,
        min_capital_ratio=min_capital_ratio,
        min_copied_buys=min_copied_buys,
        roi_tie_threshold=roi_tie_threshold,
    )
    optimizer_rows = pool.get("rows") if isinstance(pool.get("rows"), list) else []
    winner = optimizer_rows[0] if optimizer_rows else None
    if winner is None and rows:
        sorted_rows = sort_optimizer_rows(rows, roi_tie_threshold=roi_tie_threshold)
        if sorted_rows:
            winner = sorted_rows[0]
            optimizer_rows = sorted_rows
    return winner, pool, optimizer_rows


def run_ai_execute_improvement_loop(
    *,
    replay_events: List[TradeEvent],
    strategy_map: Dict[str, Strategy],
    base_results: List[Dict[str, Any]],
    price_map: Dict[str, PriceInfo],
    leader_buy_signal_total_usd: Optional[float],
    actual_window_pnl_delta: Optional[float],
    buy_price_premium_pct: float,
    buy_min_price: float,
    buy_max_price: float,
    sell_min_price: float,
    sell_max_price: float,
    sell_slippage_pct: float,
    fee_enabled: bool,
    fee_rate: float,
    fee_exponent: float,
    anti_amplification_guard_enabled: bool,
    max_our_vs_leader_per_trade: float,
    max_our_vs_leader_per_market: float,
    min_capital_ratio: float,
    min_copied_buys: int,
    roi_tie_threshold: float,
    rounds: int,
    budget_minutes: float,
    bound_profile: str,
    top_candidates_per_round: int,
) -> Dict[str, Any]:
    bounds = get_ai_improve_bounds(bound_profile)
    round_limit = max(1, int(rounds))
    budget_s = max(1.0, float(budget_minutes) * 60.0)
    max_candidates = max(1, int(top_candidates_per_round))
    start_ts = time.time()
    no_improve_rounds = 0
    stop_reason = "round_limit_reached"

    result_by_strategy: Dict[str, Dict[str, Any]] = {}
    for row in base_results:
        key = str(row.get("strategy") or "")
        if not key:
            continue
        result_by_strategy[key] = normalize_result_row(row)

    executed_experiments: List[Dict[str, Any]] = []

    for round_idx in range(1, round_limit + 1):
        elapsed_before = time.time() - start_ts
        if elapsed_before >= budget_s:
            stop_reason = "time_budget_reached"
            break

        all_rows = list(result_by_strategy.values())
        before_winner, before_pool, before_optimizer_rows = _pick_optimizer_winner(
            all_rows,
            min_capital_ratio=min_capital_ratio,
            min_copied_buys=min_copied_buys,
            roi_tie_threshold=roi_tie_threshold,
        )
        if before_winner is None:
            stop_reason = "no_baseline_winner"
            break

        candidate_specs = build_ai_improve_candidates(
            optimizer_rows=before_optimizer_rows if before_optimizer_rows else all_rows,
            existing_strategy_names=set(strategy_map.keys()),
            bounds=bounds,
            max_candidates=max_candidates,
        )
        if not candidate_specs:
            stop_reason = "no_candidates"
            break

        candidate_strategies = [spec["strategy"] for spec in candidate_specs if isinstance(spec.get("strategy"), Strategy)]
        if not candidate_strategies:
            stop_reason = "no_candidates"
            break

        round_t0 = time.time()
        candidate_states = run_simulation(
            replay_events,
            candidate_strategies,
            buy_price_premium_pct=buy_price_premium_pct,
            buy_min_price=buy_min_price,
            buy_max_price=buy_max_price,
            sell_min_price=sell_min_price,
            sell_max_price=sell_max_price,
            sell_slippage_pct=sell_slippage_pct,
            fee_enabled=fee_enabled,
            fee_rate=fee_rate,
            fee_exponent=fee_exponent,
            anti_amplification_guard_enabled=anti_amplification_guard_enabled,
            max_our_vs_leader_per_trade=max_our_vs_leader_per_trade,
            max_our_vs_leader_per_market=max_our_vs_leader_per_market,
        )
        candidate_results = build_results_with_scaled_metrics(
            candidate_states,
            price_map=price_map,
            leader_buy_signal_total_usd=leader_buy_signal_total_usd,
            actual_window_pnl_delta=actual_window_pnl_delta,
        )
        candidate_ranked = sort_optimizer_rows(candidate_results, roi_tie_threshold=roi_tie_threshold)
        best_candidate = candidate_ranked[0] if candidate_ranked else None

        for strategy in candidate_strategies:
            strategy_map[strategy.name] = strategy
        for row in candidate_results:
            name = str(row.get("strategy") or "")
            if not name:
                continue
            result_by_strategy[name] = normalize_result_row(row)

        after_winner, after_pool, _after_optimizer_rows = _pick_optimizer_winner(
            list(result_by_strategy.values()),
            min_capital_ratio=min_capital_ratio,
            min_copied_buys=min_copied_buys,
            roi_tie_threshold=roi_tie_threshold,
        )
        if after_winner is None:
            after_winner = before_winner

        improved = _compare_optimizer_rows(
            after_winner,
            before_winner,
            roi_tie_threshold=roi_tie_threshold,
        ) < 0
        if improved:
            no_improve_rounds = 0
        else:
            no_improve_rounds += 1

        executed_experiments.append(
            {
                "round": round_idx,
                "candidates_executed": len(candidate_strategies),
                "candidate_samples": [str(spec["strategy"].name) for spec in candidate_specs[:8] if isinstance(spec.get("strategy"), Strategy)],
                "candidate_mutations": [
                    {
                        "strategy": str(spec["strategy"].name),
                        "parent_strategy": spec.get("parent_strategy"),
                        "mutation": spec.get("mutation"),
                    }
                    for spec in candidate_specs[:8]
                    if isinstance(spec.get("strategy"), Strategy)
                ],
                "before": {
                    "strategy": before_winner.get("strategy"),
                    "roi": as_float(before_winner.get("roi")),
                    "total_buy_cost": as_float(before_winner.get("total_buy_cost")),
                    "total_pnl": as_float(before_winner.get("total_pnl")),
                    "pool_mode": before_pool.get("mode"),
                },
                "best_candidate": (
                    {
                        "strategy": best_candidate.get("strategy"),
                        "roi": as_float(best_candidate.get("roi")),
                        "total_buy_cost": as_float(best_candidate.get("total_buy_cost")),
                        "total_pnl": as_float(best_candidate.get("total_pnl")),
                    }
                    if isinstance(best_candidate, dict)
                    else None
                ),
                "after": {
                    "strategy": after_winner.get("strategy"),
                    "roi": as_float(after_winner.get("roi")),
                    "total_buy_cost": as_float(after_winner.get("total_buy_cost")),
                    "total_pnl": as_float(after_winner.get("total_pnl")),
                    "pool_mode": after_pool.get("mode"),
                },
                "delta": {
                    "roi": (as_float(after_winner.get("roi")) or 0.0) - (as_float(before_winner.get("roi")) or 0.0),
                    "total_buy_cost": (as_float(after_winner.get("total_buy_cost")) or 0.0)
                    - (as_float(before_winner.get("total_buy_cost")) or 0.0),
                    "total_pnl": (as_float(after_winner.get("total_pnl")) or 0.0)
                    - (as_float(before_winner.get("total_pnl")) or 0.0),
                },
                "improved": improved,
                "elapsed_s": round(time.time() - round_t0, 3),
            }
        )

        print(
            "[ai-improve] "
            f"round={round_idx} candidates={len(candidate_strategies)} "
            f"improved={improved} "
            f"winner={after_winner.get('strategy')}"
        )

        if no_improve_rounds >= AI_IMPROVE_MAX_NO_IMPROVE_ROUNDS:
            stop_reason = f"no_improvement_{AI_IMPROVE_MAX_NO_IMPROVE_ROUNDS}_rounds"
            break

    final_rows = list(result_by_strategy.values())
    final_winner, final_pool, _final_optimizer_rows = _pick_optimizer_winner(
        final_rows,
        min_capital_ratio=min_capital_ratio,
        min_copied_buys=min_copied_buys,
        roi_tie_threshold=roi_tie_threshold,
    )

    if stop_reason == "round_limit_reached" and len(executed_experiments) < round_limit:
        stop_reason = "no_candidates"

    return {
        "enabled": True,
        "objective": "roi_then_scale",
        "roi_tie_threshold": float(roi_tie_threshold),
        "bound_profile": str(bounds.get("profile") or bound_profile),
        "bounds": bounds,
        "budget_minutes": float(budget_minutes),
        "rounds_requested": int(round_limit),
        "rounds_executed": len(executed_experiments),
        "improved_rounds": sum(1 for row in executed_experiments if bool(row.get("improved"))),
        "top_candidates_per_round": max_candidates,
        "executed_experiments": executed_experiments,
        "stop_reason": stop_reason,
        "final_winner": (
            {
                "strategy": final_winner.get("strategy"),
                "roi": as_float(final_winner.get("roi")),
                "total_buy_cost": as_float(final_winner.get("total_buy_cost")),
                "total_pnl": as_float(final_winner.get("total_pnl")),
                "pool_mode": final_pool.get("mode"),
            }
            if isinstance(final_winner, dict)
            else None
        ),
        "final_results": sort_results_for_report(final_rows),
    }


def pick_best_row_by_objective(
    rows: List[Dict[str, Any]],
    *,
    roi_tie_threshold: float,
) -> Optional[Dict[str, Any]]:
    ranked = sort_optimizer_rows(rows, roi_tie_threshold=roi_tie_threshold)
    if not ranked:
        return None
    return dict(ranked[0])


def pick_best_row_by_raw_roi(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ranked = [row for row in rows if as_float(row.get("roi")) is not None]
    if not ranked:
        return None
    ranked = sorted(ranked, key=strategy_roi_sort_key, reverse=True)
    return dict(ranked[0])


def pick_best_row_by_total_pnl(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    ranked = sort_results_for_report(rows)
    if not ranked:
        return None
    return dict(ranked[0])


def _result_row_brief(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {
        "strategy": row.get("strategy"),
        "copy_mode": row.get("copy_mode"),
        "fixed_usd": as_float(row.get("fixed_usd")),
        "proportional_pct": as_float(row.get("proportional_pct")),
        "proportional_cap_usd": as_float(row.get("proportional_cap_usd")),
        "max_entries_per_market": as_int(row.get("max_entries_per_market")),
        "roi": as_float(row.get("roi")),
        "total_buy_cost": as_float(row.get("total_buy_cost")),
        "total_pnl": as_float(row.get("total_pnl")),
        "copied_buys": as_int(row.get("copied_buys")),
        "mirrored_sells": as_int(row.get("mirrored_sells")),
        "guard_trimmed_count": as_int(row.get("guard_trimmed_count")),
        "guard_trimmed_usd": as_float(row.get("guard_trimmed_usd")),
        "guard_skipped_count": as_int(row.get("guard_skipped_count")),
        "oversize_before_guard_count": as_int(row.get("oversize_before_guard_count")),
        "oversize_after_guard_count": as_int(row.get("oversize_after_guard_count")),
        "oversize_event_rate_before_guard": as_float(row.get("oversize_event_rate_before_guard")),
        "oversize_event_rate_after_guard": as_float(row.get("oversize_event_rate_after_guard")),
    }


def _top_rows_by_roi(rows: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    roi_rows = [row for row in rows if as_float(row.get("roi")) is not None]
    roi_rows = sorted(roi_rows, key=strategy_roi_sort_key, reverse=True)
    return roi_rows[: max(1, int(top_n))]


def _build_window_record(
    *,
    window_id: str,
    title: str,
    activity_range: str,
    count: int,
    start_utc: Optional[str],
    end_utc: Optional[str],
    rows: List[Dict[str, Any]],
    roi_tie_threshold: float,
    top_n: int,
    actual_window_pnl_delta: Optional[float],
    leader_buy_signal_total_usd: Optional[float],
) -> Dict[str, Any]:
    ranked = sort_results_for_report(rows)
    best_obj = pick_best_row_by_objective(ranked, roi_tie_threshold=roi_tie_threshold)
    return {
        "window_id": window_id,
        "title": title,
        "activity_range": activity_range,
        "count": int(count),
        "start_utc": start_utc,
        "end_utc": end_utc,
        "actual_window_pnl_delta": as_float(actual_window_pnl_delta),
        "leader_buy_signal_total_usd": as_float(leader_buy_signal_total_usd),
        "best_by_objective": _result_row_brief(best_obj),
        "top10_total_pnl": [_result_row_brief(row) for row in ranked[: max(1, int(top_n))]],
        "top10_roi": [_result_row_brief(row) for row in _top_rows_by_roi(ranked, top_n)],
        "rows": ranked,
    }


def build_amplification_guard_summary(
    *,
    results: List[Dict[str, Any]],
    guard_enabled: bool,
    per_trade_limit: float,
    per_market_limit: float,
    objective_best_row: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    total_copied_buys = sum(max(0, as_int(row.get("copied_buys")) or 0) for row in results)
    total_trimmed_count = sum(max(0, as_int(row.get("guard_trimmed_count")) or 0) for row in results)
    total_trimmed_usd = sum(max(0.0, as_float(row.get("guard_trimmed_usd")) or 0.0) for row in results)
    total_skipped = sum(max(0, as_int(row.get("guard_skipped_count")) or 0) for row in results)
    total_oversize_before = sum(max(0, as_int(row.get("oversize_before_guard_count")) or 0) for row in results)
    total_oversize_after = sum(max(0, as_int(row.get("oversize_after_guard_count")) or 0) for row in results)

    aggregate_rate_before = (
        (total_oversize_before / float(total_copied_buys))
        if total_copied_buys > 0
        else None
    )
    aggregate_rate_after = (
        (total_oversize_after / float(total_copied_buys))
        if total_copied_buys > 0
        else None
    )

    best_brief = _result_row_brief(objective_best_row)
    return {
        "enabled": bool(guard_enabled),
        "per_trade_limit": max(0.0, float(per_trade_limit)),
        "per_market_limit": max(0.0, float(per_market_limit)),
        "strategy_count": len(results),
        "aggregate": {
            "copied_buys": total_copied_buys,
            "trimmed_count": total_trimmed_count,
            "trimmed_usd": round(total_trimmed_usd, 6),
            "skipped_guard_count": total_skipped,
            "oversize_before_guard_count": total_oversize_before,
            "oversize_after_guard_count": total_oversize_after,
            "oversize_event_rate_before_guard": (
                round(aggregate_rate_before, 6) if aggregate_rate_before is not None else None
            ),
            "oversize_event_rate_after_guard": (
                round(aggregate_rate_after, 6) if aggregate_rate_after is not None else None
            ),
        },
        "objective_winner": best_brief,
    }


def summarize_leader_buy_signal_counts(events: List[TradeEvent]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for event in events:
        if str(event.side or "").upper() != "BUY" or not bool(event.copy_signal):
            continue
        market_key = event.condition_id or event.token_id
        if not market_key:
            continue
        counts[market_key] = counts.get(market_key, 0) + 1
    return counts


def build_entries_depth_evidence(
    *,
    results: List[Dict[str, Any]],
    avg_bets_per_market: Optional[float],
    market_bet_distribution: Optional[Dict[str, Any]],
    roi_tie_threshold: float,
    states_by_strategy: Optional[Dict[str, StrategyState]] = None,
    price_map: Optional[Dict[str, PriceInfo]] = None,
    leader_market_signal_counts: Optional[Dict[str, int]] = None,
    objective_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    best_by_entries: Dict[int, Dict[str, Any]] = {}
    for row in results:
        entries = as_int(row.get("max_entries_per_market"))
        if entries is None or entries <= 0:
            continue
        prev = best_by_entries.get(entries)
        if prev is None or _compare_optimizer_rows(row, prev, roi_tie_threshold=roi_tie_threshold) < 0:
            best_by_entries[entries] = row

    entries_curve: List[Dict[str, Any]] = []
    marginal_segments: List[Dict[str, Any]] = []
    sorted_entries = sorted(best_by_entries.keys())
    for entries in sorted_entries:
        row = best_by_entries[entries]
        entries_curve.append(
            {
                "entries": int(entries),
                "strategy": row.get("strategy"),
                "roi": as_float(row.get("roi")),
                "total_pnl": as_float(row.get("total_pnl")),
                "total_buy_cost": as_float(row.get("total_buy_cost")),
            }
        )

    for idx in range(1, len(entries_curve)):
        prev_row = entries_curve[idx - 1]
        cur_row = entries_curve[idx]
        prev_pnl = as_float(prev_row.get("total_pnl")) or 0.0
        cur_pnl = as_float(cur_row.get("total_pnl")) or 0.0
        prev_roi = as_float(prev_row.get("roi")) or 0.0
        cur_roi = as_float(cur_row.get("roi")) or 0.0
        marginal_segments.append(
            {
                "from_entries": prev_row.get("entries"),
                "to_entries": cur_row.get("entries"),
                "delta_total_pnl": cur_pnl - prev_pnl,
                "delta_roi": cur_roi - prev_roi,
                "from_strategy": prev_row.get("strategy"),
                "to_strategy": cur_row.get("strategy"),
            }
        )

    ranked = sort_optimizer_rows(results, roi_tie_threshold=roi_tie_threshold)
    best_overall = objective_row if isinstance(objective_row, dict) else (ranked[0] if ranked else None)
    best_entries = as_int(best_overall.get("max_entries_per_market")) if isinstance(best_overall, dict) else None

    avg_bets = as_float(avg_bets_per_market)
    threshold_entries = int(math.floor(avg_bets)) if avg_bets is not None else None
    low_zone_best_pnl: Optional[float] = None
    high_zone_best_pnl: Optional[float] = None
    if threshold_entries is not None:
        low_values = [
            as_float(best_by_entries[e].get("total_pnl"))
            for e in sorted_entries
            if e <= threshold_entries and as_float(best_by_entries[e].get("total_pnl")) is not None
        ]
        high_values = [
            as_float(best_by_entries[e].get("total_pnl"))
            for e in sorted_entries
            if e > threshold_entries and as_float(best_by_entries[e].get("total_pnl")) is not None
        ]
        if low_values:
            low_zone_best_pnl = max(low_values)
        if high_values:
            high_zone_best_pnl = max(high_values)

    high_zone_increment = None
    high_zone_contribution_ratio = None
    if (
        low_zone_best_pnl is not None
        and high_zone_best_pnl is not None
    ):
        high_zone_increment = high_zone_best_pnl - low_zone_best_pnl
        if high_zone_best_pnl > 0:
            high_zone_contribution_ratio = high_zone_increment / high_zone_best_pnl

    distribution = market_bet_distribution if isinstance(market_bet_distribution, dict) else {}
    why_entries_gt_avg = {
        "avg_bets_per_market": avg_bets,
        "best_optimizer_entries": best_entries,
        "entries_minus_avg": (float(best_entries) - avg_bets) if (best_entries is not None and avg_bets is not None) else None,
        "market_bet_count_distribution": {
            "p50": as_float(distribution.get("p50")),
            "p75": as_float(distribution.get("p75")),
            "p90": as_float(distribution.get("p90")),
            "p95": as_float(distribution.get("p95")),
            "max": as_float(distribution.get("max")),
        },
        "high_entries_zone_threshold": threshold_entries,
        "best_total_pnl_low_entries_zone": low_zone_best_pnl,
        "best_total_pnl_high_entries_zone": high_zone_best_pnl,
        "high_entries_incremental_pnl": high_zone_increment,
        "high_entries_contribution_ratio": high_zone_contribution_ratio,
    }

    top_market_contributors: List[Dict[str, Any]] = []
    comparison_baseline: Dict[str, Any] = {}
    if (
        isinstance(best_overall, dict)
        and best_entries is not None
        and isinstance(states_by_strategy, dict)
        and isinstance(price_map, dict)
    ):
        def _same_sizing(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
            if str(a.get("copy_mode") or "") != str(b.get("copy_mode") or ""):
                return False
            if str(a.get("copy_mode") or "") == "fixed_usd":
                return _float_equal(a.get("fixed_usd"), b.get("fixed_usd"))
            return (
                _float_equal(a.get("proportional_pct"), b.get("proportional_pct"))
                and _float_equal(a.get("proportional_cap_usd"), b.get("proportional_cap_usd"))
            )

        baseline_row: Optional[Dict[str, Any]] = None
        baseline_entries = -1
        for candidate in results:
            if not isinstance(candidate, dict):
                continue
            if not _same_sizing(best_overall, candidate):
                continue
            entries = as_int(candidate.get("max_entries_per_market"))
            if entries is None or entries <= 0 or entries >= best_entries:
                continue
            if entries > baseline_entries:
                baseline_entries = entries
                baseline_row = candidate
            elif entries == baseline_entries and baseline_row is not None:
                if _compare_optimizer_rows(candidate, baseline_row, roi_tie_threshold=roi_tie_threshold) < 0:
                    baseline_row = candidate

        if baseline_row is not None:
            comparison_baseline = {
                "current_strategy": best_overall.get("strategy"),
                "baseline_strategy": baseline_row.get("strategy"),
                "current_entries": best_entries,
                "baseline_entries": as_int(baseline_row.get("max_entries_per_market")),
            }
            current_state = states_by_strategy.get(str(best_overall.get("strategy") or ""))
            baseline_state = states_by_strategy.get(str(baseline_row.get("strategy") or ""))
            if current_state is not None and baseline_state is not None:
                current_market = build_strategy_market_breakdown(current_state, price_map)
                baseline_market = build_strategy_market_breakdown(baseline_state, price_map)
                keys = set(current_market.keys()) | set(baseline_market.keys())
                leader_counts = leader_market_signal_counts if isinstance(leader_market_signal_counts, dict) else {}
                merged_rows: List[Dict[str, Any]] = []
                for market_key in keys:
                    cur = current_market.get(market_key, {})
                    base = baseline_market.get(market_key, {})
                    cur_pnl = as_float(cur.get("total_pnl")) or 0.0
                    base_pnl = as_float(base.get("total_pnl")) or 0.0
                    merged_rows.append(
                        {
                            "market_key": market_key,
                            "leader_buy_signals": int(leader_counts.get(market_key, 0) or 0),
                            "copied_buys_current": int(as_int(cur.get("copied_buys")) or 0),
                            "copied_buys_baseline": int(as_int(base.get("copied_buys")) or 0),
                            "cost_current": round(float(as_float(cur.get("buy_cost")) or 0.0), 6),
                            "cost_baseline": round(float(as_float(base.get("buy_cost")) or 0.0), 6),
                            "pnl_current": round(cur_pnl, 6),
                            "pnl_baseline": round(base_pnl, 6),
                            "delta_pnl": round(cur_pnl - base_pnl, 6),
                        }
                    )

                merged_rows.sort(
                    key=lambda row: (
                        as_float(row.get("delta_pnl")) if as_float(row.get("delta_pnl")) is not None else float("-inf"),
                        as_float(row.get("pnl_current")) if as_float(row.get("pnl_current")) is not None else float("-inf"),
                    ),
                    reverse=True,
                )
                top_market_contributors = merged_rows[:10]

    return {
        "market_bet_count_distribution": {
            "p50": as_float(distribution.get("p50")),
            "p75": as_float(distribution.get("p75")),
            "p90": as_float(distribution.get("p90")),
            "p95": as_float(distribution.get("p95")),
            "max": as_float(distribution.get("max")),
        },
        "entries_curve": entries_curve,
        "marginal_segments": marginal_segments,
        "comparison_baseline": comparison_baseline,
        "top_market_contributors": top_market_contributors,
        "why_entries_gt_avg": why_entries_gt_avg,
    }


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
    ordered = sorted(events, key=lambda x: (x.ts, x.tx_hash, x.token_id, x.side))
    states: Dict[Tuple[str, str, float], Dict[str, Any]] = {}
    out: List[TradeEvent] = []

    for event in ordered:
        ts = int(event.ts)

        stale_keys = []
        for key, st in states.items():
            window_s = max(1, int(st.get("window_s", MAKER_LIKE_WINDOW_S)))
            if int(st.get("last_ts", 0)) < (ts - window_s):
                stale_keys.append(key)
        for key in stale_keys:
            states.pop(key, None)

        if event.side != "BUY":
            out.append(event)
            continue

        usd = float(event.usd) if isinstance(event.usd, (int, float)) else None
        if usd is None or usd >= MAKER_LIKE_MIN_TRADE_SIZE_USD:
            out.append(event)
            continue

        if not event.token_id or event.price is None or event.price <= 0:
            out.append(event)
            continue

        out.append(replace(event, copy_signal=False))

        price_bucket = round(float(event.price), 4)
        state_key = (
            event.token_id,
            event.condition_id or "",
            price_bucket,
        )
        st = states.get(state_key)
        if st is None:
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
                "last_tx": event.tx_hash,
            }
            continue

        too_far_gap = ts - int(st["last_ts"]) > int(st.get("max_gap_s", MAKER_LIKE_MAX_GAP_S))
        out_of_window = ts - int(st["first_ts"]) > int(st.get("window_s", MAKER_LIKE_WINDOW_S))
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
                "last_tx": event.tx_hash,
            }
            continue

        st["last_ts"] = ts
        st["cum_usd"] = float(st["cum_usd"]) + usd
        st["cum_size"] = float(st["cum_size"]) + (
            float(event.size) if isinstance(event.size, (int, float)) else 0.0
        )
        st["count"] = int(st["count"]) + 1
        st["price_sum"] = float(st["price_sum"]) + float(event.price)
        st["max_piece_usd"] = max(float(st["max_piece_usd"]), usd)
        st["last_slug"] = event.market_slug
        st["last_tx"] = event.tx_hash

        cum_usd = float(st["cum_usd"])
        if cum_usd < MAKER_LIKE_MIN_TRADE_SIZE_USD:
            continue

        score = _compute_maker_like_score(
            count=int(st["count"]),
            span_s=max(1, int(st["last_ts"]) - int(st["first_ts"])),
            max_piece_usd=float(st["max_piece_usd"]),
            min_trade_size_usd=MAKER_LIKE_MIN_TRADE_SIZE_USD,
            window_s=int(st.get("window_s", MAKER_LIKE_WINDOW_S)),
        )
        if score < float(st.get("score_threshold", MAKER_LIKE_SCORE_THRESHOLD)):
            continue

        avg_price = float(st["price_sum"]) / max(1, int(st["count"]))
        cum_size = float(st["cum_size"])
        agg_size: Optional[float] = cum_size if cum_size > 0 else None
        if agg_size is None and avg_price > 0:
            agg_size = cum_usd / avg_price

        agg_tx_hash = (
            f"agg-{event.token_id[:12]}-{int(st['last_ts'])}-{int(st['count'])}"
        )
        agg_event = TradeEvent(
            tx_hash=agg_tx_hash,
            ts=int(st["last_ts"]),
            side="BUY",
            token_id=event.token_id,
            condition_id=event.condition_id,
            market_slug=st.get("last_slug") or event.market_slug,
            price=avg_price,
            size=agg_size,
            usd=cum_usd,
            copy_signal=True,
            is_leader_position_event=False,
            is_maker_like_aggregated=True,
            maker_like_score=score,
            aggregation_source_count=int(st["count"]),
        )
        out.append(agg_event)
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
    avg_bets_per_market = (
        buy_count / unique_market_count if buy_count > 0 and unique_market_count > 0 else None
    )
    avg_usd_per_market = (
        total_buy_usd / unique_market_count if total_buy_usd > 0 and unique_market_count > 0 else None
    )
    avg_usd_per_bet = (
        total_buy_usd / buy_count if total_buy_usd > 0 and buy_count > 0 else None
    )
    market_bet_counts = sorted(float(stat.get("count", 0.0)) for stat in market_stats.values())
    distribution = {
        "p50": _quantile_from_sorted(market_bet_counts, 0.50),
        "p75": _quantile_from_sorted(market_bet_counts, 0.75),
        "p90": _quantile_from_sorted(market_bet_counts, 0.90),
        "p95": _quantile_from_sorted(market_bet_counts, 0.95),
        "max": (market_bet_counts[-1] if market_bet_counts else None),
    }

    return {
        "buy_signal_count": buy_count,
        "aggregated_buy_signal_count": aggregated_buy_count,
        "buy_signal_market_count": unique_market_count,
        "buy_signal_total_usd": total_buy_usd,
        "avg_bets_per_market": avg_bets_per_market,
        "avg_usd_per_market": avg_usd_per_market,
        "avg_usd_per_bet": avg_usd_per_bet,
        "market_bet_count_distribution": distribution,
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


def compute_trade_fee_usdc(
    *,
    share_qty: float,
    price: float,
    fee_rate: float,
    fee_exponent: float,
) -> float:
    if share_qty <= 0 or price <= 0 or fee_rate <= 0:
        return 0.0
    shape = price * (1.0 - price)
    if shape < 0:
        shape = 0.0
    factor = fee_rate * (shape ** fee_exponent)
    if factor <= 0:
        return 0.0
    return share_qty * price * factor


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
    states = [StrategyState(s) for s in strategies]
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
                    allowed_by_trade = float("inf")
                    if event_usd is not None and event_usd > 0:
                        allowed_by_trade = event_usd * guard_trade_limit

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
                fee_buy_shares = (fee_buy_usdc / our_buy_price) if fee_buy_usdc > 0 and our_buy_price > 0 else 0.0
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
                state.market_follow_buys[market_key] = state.market_follow_buys.get(market_key, 0) + 1
                state.market_buy_cost[market_key] = state.market_buy_cost.get(market_key, 0.0) + our_usd

            else:  # SELL
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

                sell_size = pos.size * sell_ratio
                sell_size = min(sell_size, pos.size)
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
                realized = sell_size * (actual_sell_price - avg_cost) - fee_sell_usdc
                state.realized_pnl += realized
                realized_market_key = pos.market_key or event.condition_id or event.token_id
                state.market_realized_pnl[realized_market_key] = (
                    state.market_realized_pnl.get(realized_market_key, 0.0) + realized
                )

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
            return loaded if isinstance(loaded, list) else None
        except json.JSONDecodeError:
            return None
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
    cached_tokens: set = set()
    expired = 0

    for start in range(0, len(token_ids), 500):
        chunk = token_ids[start : start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        sql = (
            "SELECT token_id, price, resolved, source, expires_at_ts "
            f"FROM price_cache WHERE token_id IN ({placeholders})"
        )
        rows = conn.execute(sql, chunk).fetchall()
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
                source=str(source_raw) if source_raw else "missing",
            )

    to_fetch = [token_id for token_id in token_ids if token_id not in cached]
    miss = max(0, len(token_ids) - len(cached_tokens))
    stats = {
        "total": len(token_ids),
        "hit": len(cached),
        "expired": expired,
        "miss": miss,
        "online_fetch": len(to_fetch),
    }
    return cached, to_fetch, stats


def _save_cached_prices(
    conn: sqlite3.Connection,
    price_map: Dict[str, PriceInfo],
    *,
    now_ts: int,
) -> None:
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
            market = data[0]
            closed = bool(market.get("closed"))
            clob_ids = _parse_json_list(market.get("clobTokenIds"))
            outcome_prices = _parse_json_list(market.get("outcomePrices"))
            if closed and clob_ids and outcome_prices:
                for idx, cid in enumerate(clob_ids):
                    if str(cid) == str(token_id) and idx < len(outcome_prices):
                        p = as_float(outcome_prices[idx])
                        if p is not None:
                            return PriceInfo(token_id=token_id, price=p, resolved=True, source="resolution")
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
                p = as_float(data.get(key))
                if p is not None:
                    return PriceInfo(token_id=token_id, price=p, resolved=False, source="midpoint")
    except Exception:
        pass

    return PriceInfo(token_id=token_id, price=None, resolved=False, source="missing")


def fetch_prices_for_tokens(
    token_ids: List[str],
    *,
    timeout_s: float,
    workers: int,
) -> Dict[str, PriceInfo]:
    if not token_ids:
        return {}

    uniq_tokens = sorted(set(t for t in token_ids if t))
    if not uniq_tokens:
        return {}

    now_ts = int(time.time())
    out: Dict[str, PriceInfo] = {}
    to_fetch = list(uniq_tokens)
    cache_stats = {
        "total": len(uniq_tokens),
        "hit": 0,
        "expired": 0,
        "miss": len(uniq_tokens),
        "online_fetch": len(uniq_tokens),
    }

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
            future_map = {}
            for token_id in to_fetch:
                session = requests.Session()
                future = executor.submit(fetch_token_price_info, session, token_id, timeout_s=timeout_s)
                future_map[future] = token_id

            completed = 0
            total = len(future_map)
            for future in as_completed(future_map):
                token_id = future_map[future]
                try:
                    fetched_online[token_id] = future.result()
                except Exception:
                    fetched_online[token_id] = PriceInfo(
                        token_id=token_id,
                        price=None,
                        resolved=False,
                        source="missing",
                    )
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
    open_positions = 0
    unresolved_positions = 0
    missing_price_positions = 0

    for token_id, pos in state.positions.items():
        if pos.size <= 1e-12:
            continue
        open_positions += 1

        price_info = price_map.get(token_id)
        if price_info is None or price_info.price is None:
            missing_price_positions += 1
            continue

        pnl = pos.size * price_info.price - pos.cost
        if price_info.resolved:
            settlement_pnl += pnl
        else:
            unrealized_pnl += pnl
            unresolved_positions += 1

    total_pnl = state.realized_pnl + settlement_pnl + unrealized_pnl
    roi = (total_pnl / state.total_buy_cost) if state.total_buy_cost > 0 else None
    oversize_rate_before = (
        state.oversize_before_guard_count / state.copied_buys
        if state.copied_buys > 0
        else None
    )
    oversize_rate_after = (
        state.oversize_after_guard_count / state.copied_buys
        if state.copied_buys > 0
        else None
    )

    return {
        "strategy": state.strategy.name,
        "copy_mode": state.strategy.copy_mode,
        "max_entries_per_market": state.strategy.max_entries_per_market,
        "exit_mode": state.strategy.exit_mode,
        "fixed_usd": state.strategy.fixed_usd,
        "proportional_pct": state.strategy.proportional_pct,
        "proportional_cap_usd": state.strategy.proportional_cap_usd,
        "copied_buys": state.copied_buys,
        "mirrored_sells": state.mirrored_sells,
        "total_buy_cost": round(state.total_buy_cost, 6),
        "realized_pnl": round(state.realized_pnl, 6),
        "settlement_pnl": round(settlement_pnl, 6),
        "unrealized_pnl": round(unrealized_pnl, 6),
        "total_pnl": round(total_pnl, 6),
        "roi": round(roi, 6) if roi is not None else None,
        "open_positions": open_positions,
        "unresolved_positions": unresolved_positions,
        "missing_price_positions": missing_price_positions,
        "skipped_entry_limit": state.skipped_entry_limit,
        "skipped_buy_price": state.skipped_buy_price,
        "skipped_sell_price": state.skipped_sell_price,
        "skipped_missing_value": state.skipped_missing_value,
        "guard_trimmed_count": state.guard_trimmed_count,
        "guard_trimmed_usd": round(state.guard_trimmed_usd, 6),
        "guard_skipped_count": state.guard_skipped_count,
        "oversize_before_guard_count": state.oversize_before_guard_count,
        "oversize_after_guard_count": state.oversize_after_guard_count,
        "oversize_event_rate_before_guard": round(oversize_rate_before, 6) if oversize_rate_before is not None else None,
        "oversize_event_rate_after_guard": round(oversize_rate_after, 6) if oversize_rate_after is not None else None,
    }


def build_strategy_market_breakdown(
    state: StrategyState,
    price_map: Dict[str, PriceInfo],
) -> Dict[str, Dict[str, Any]]:
    market_rows: Dict[str, Dict[str, Any]] = {}

    for market_key, follow_buys in state.market_follow_buys.items():
        market_rows[market_key] = {
            "market_key": market_key,
            "copied_buys": int(follow_buys),
            "buy_cost": float(state.market_buy_cost.get(market_key, 0.0)),
            "realized_pnl": float(state.market_realized_pnl.get(market_key, 0.0)),
            "settlement_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "open_positions": 0,
            "unresolved_positions": 0,
            "missing_price_positions": 0,
        }

    for market_key, realized in state.market_realized_pnl.items():
        row = market_rows.setdefault(
            market_key,
            {
                "market_key": market_key,
                "copied_buys": 0,
                "buy_cost": 0.0,
                "realized_pnl": 0.0,
                "settlement_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_positions": 0,
                "unresolved_positions": 0,
                "missing_price_positions": 0,
            },
        )
        row["realized_pnl"] = float(row.get("realized_pnl", 0.0)) + float(realized)

    for token_id, pos in state.positions.items():
        if pos.size <= 1e-12:
            continue
        market_key = pos.market_key or token_id
        row = market_rows.setdefault(
            market_key,
            {
                "market_key": market_key,
                "copied_buys": 0,
                "buy_cost": 0.0,
                "realized_pnl": 0.0,
                "settlement_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_positions": 0,
                "unresolved_positions": 0,
                "missing_price_positions": 0,
            },
        )
        row["open_positions"] = int(row.get("open_positions", 0)) + 1

        info = price_map.get(token_id)
        if info is None or info.price is None:
            row["missing_price_positions"] = int(row.get("missing_price_positions", 0)) + 1
            continue

        pnl = pos.size * info.price - pos.cost
        if info.resolved:
            row["settlement_pnl"] = float(row.get("settlement_pnl", 0.0)) + pnl
        else:
            row["unrealized_pnl"] = float(row.get("unrealized_pnl", 0.0)) + pnl
            row["unresolved_positions"] = int(row.get("unresolved_positions", 0)) + 1

    for row in market_rows.values():
        realized = float(row.get("realized_pnl", 0.0))
        settlement = float(row.get("settlement_pnl", 0.0))
        unrealized = float(row.get("unrealized_pnl", 0.0))
        row["total_pnl"] = realized + settlement + unrealized

    return market_rows


def write_outputs(
    out_dir: Path,
    address: str,
    meta: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_addr = f"{address[:8]}_{address[-6:]}"

    json_path = out_dir / f"sim_results_{short_addr}_{ts}.json"
    csv_path = out_dir / f"sim_results_{short_addr}_{ts}.csv"

    payload = {
        "generated_at": now_utc_iso(),
        "meta": meta,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    fieldnames = [
        "strategy",
        "copy_mode",
        "max_entries_per_market",
        "exit_mode",
        "fixed_usd",
        "proportional_pct",
        "proportional_cap_usd",
        "copied_buys",
        "mirrored_sells",
        "total_buy_cost",
        "realized_pnl",
        "settlement_pnl",
        "unrealized_pnl",
        "total_pnl",
        "roi",
        "capital_ratio_vs_leader_buy_flow",
        "scaled_benchmark_pnl",
        "normalized_gap",
        "capture_rate",
        "open_positions",
        "unresolved_positions",
        "missing_price_positions",
        "skipped_entry_limit",
        "skipped_buy_price",
        "skipped_sell_price",
        "skipped_missing_value",
        "guard_trimmed_count",
        "guard_trimmed_usd",
        "guard_skipped_count",
        "oversize_before_guard_count",
        "oversize_after_guard_count",
        "oversize_event_rate_before_guard",
        "oversize_event_rate_after_guard",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({k: row.get(k) for k in fieldnames})

    return json_path, csv_path


def load_results_from_json(json_path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    raw = json_path.read_text(encoding="utf-8")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError(f"JSON payload must be an object: {json_path}")

    meta = loaded.get("meta")
    if not isinstance(meta, dict):
        meta = {}

    results_raw = loaded.get("results")
    if not isinstance(results_raw, list):
        raise RuntimeError(f"JSON payload missing results list: {json_path}")

    results = [normalize_result_row(row) for row in results_raw if isinstance(row, dict)]
    if not results:
        raise RuntimeError(f"No valid strategy rows found in JSON: {json_path}")

    return meta, sort_results_for_report(results)


def load_results_from_csv(
    csv_path: Path,
    *,
    meta_json_path: Optional[Path] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    results: List[Dict[str, Any]] = []
    with csv_path.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            if not isinstance(row, dict):
                continue
            results.append(normalize_result_row(row))

    if not results:
        raise RuntimeError(f"No strategy rows found in CSV: {csv_path}")

    meta: Dict[str, Any] = {}
    if meta_json_path is not None:
        loaded = json.loads(meta_json_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            if isinstance(loaded.get("meta"), dict):
                meta = loaded.get("meta") or {}
            else:
                meta = loaded

    if not meta:
        meta = {
            "address": "N/A",
            "fetched_events": "N/A",
            "strategies": len(results),
            "source_csv": str(csv_path),
        }
    elif "source_csv" not in meta:
        meta["source_csv"] = str(csv_path)

    return meta, sort_results_for_report(results)


def resolve_report_pdf_path(
    *,
    report_pdf_arg: Optional[str],
    out_dir: Path,
    meta: Dict[str, Any],
) -> Path:
    if report_pdf_arg:
        return Path(report_pdf_arg)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = short_address(meta.get("address"))
    return out_dir / f"report_{suffix}_{ts}.pdf"


def resolve_report_html_path(
    *,
    report_html_arg: Optional[str],
    out_dir: Path,
    meta: Dict[str, Any],
    pdf_path: Optional[Path] = None,
) -> Path:
    if report_html_arg:
        return Path(report_html_arg)
    if pdf_path is not None:
        return pdf_path.with_suffix(".html")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = short_address(meta.get("address"))
    return out_dir / f"report_{suffix}_{ts}.html"


def _configure_matplotlib_chinese() -> str:
    from matplotlib import font_manager, rcParams

    preferred = ["Microsoft YaHei", "SimHei"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    selected = next((font for font in preferred if font in available), None)

    if selected:
        rcParams["font.sans-serif"] = [selected, "DejaVu Sans"]
    else:
        rcParams["font.sans-serif"] = ["DejaVu Sans"]
        selected = "DejaVu Sans"
    rcParams["axes.unicode_minus"] = False
    return selected


def _split_lines_two_columns(lines: List[str]) -> Tuple[List[str], List[str]]:
    if not lines:
        return [], []
    mid = (len(lines) + 1) // 2
    return lines[:mid], lines[mid:]


def _cover_meta_lines(meta: Dict[str, Any], strategies_count: int) -> List[str]:
    buy_limits = meta.get("buy_price_limits")
    sell_limits = meta.get("sell_price_limits")
    buy_limits_text = (
        f"{buy_limits[0]} - {buy_limits[1]}"
        if isinstance(buy_limits, list) and len(buy_limits) >= 2
        else "N/A"
    )
    sell_limits_text = (
        f"{sell_limits[0]} - {sell_limits[1]}"
        if isinstance(sell_limits, list) and len(sell_limits) >= 2
        else "N/A"
    )
    signal_stats = meta.get("buy_signal_stats") if isinstance(meta.get("buy_signal_stats"), dict) else {}
    avg_bets_per_market = signal_stats.get("avg_bets_per_market", meta.get("avg_bets_per_market"))
    avg_usd_per_market = signal_stats.get("avg_usd_per_market", meta.get("avg_usd_per_market"))
    avg_usd_per_bet = signal_stats.get("avg_usd_per_bet", meta.get("avg_usd_per_bet"))
    tracked_first_utc = str(meta.get("tracked_first_trade_utc") or "").strip() or (
        format_utc_from_epoch(meta.get("tracked_first_trade_ts")) or "N/A"
    )
    tracked_last_utc = str(meta.get("tracked_last_trade_utc") or "").strip() or (
        format_utc_from_epoch(meta.get("tracked_last_trade_ts")) or "N/A"
    )
    tracked_span_days = as_float(meta.get("tracked_span_days"))
    if tracked_span_days is None:
        first_ts = parse_epoch(meta.get("tracked_first_trade_ts"))
        last_ts = parse_epoch(meta.get("tracked_last_trade_ts"))
        if first_ts is not None and last_ts is not None and last_ts >= first_ts:
            tracked_span_days = (last_ts - first_ts) / 86400.0

    tracked_window_text = "N/A"
    if tracked_first_utc != "N/A" and tracked_last_utc != "N/A":
        span_text = format_decimal(tracked_span_days, ndigits=3) if tracked_span_days is not None else "N/A"
        tracked_window_text = f"{tracked_first_utc} ~ {tracked_last_utc} (跨度 {span_text} 天)"
    benchmark_delta = as_float(meta.get("actual_window_pnl_delta"))
    leader_buy_total = _leader_buy_total_from_meta(meta)
    opt_summary = meta.get("optimization_summary") if isinstance(meta.get("optimization_summary"), dict) else {}
    opt_rounds = as_int(opt_summary.get("rounds_executed"))
    opt_expands = as_int(opt_summary.get("expansion_rounds"))
    opt_enabled = bool(opt_summary.get("enabled", False))
    guard_summary = (
        meta.get("amplification_guard_summary")
        if isinstance(meta.get("amplification_guard_summary"), dict)
        else {}
    )
    guard_enabled = bool(guard_summary.get("enabled", False))
    guard_per_trade = as_float(guard_summary.get("per_trade_limit"))
    guard_per_market = as_float(guard_summary.get("per_market_limit"))
    guard_aggregate = (
        guard_summary.get("aggregate")
        if isinstance(guard_summary.get("aggregate"), dict)
        else {}
    )
    guard_trimmed_count = as_int(guard_aggregate.get("trimmed_count"))
    guard_trimmed_usd = as_float(guard_aggregate.get("trimmed_usd"))
    oversize_before = as_float(guard_aggregate.get("oversize_event_rate_before_guard"))
    oversize_after = as_float(guard_aggregate.get("oversize_event_rate_after_guard"))
    fee_cfg = meta.get("fee_config") if isinstance(meta.get("fee_config"), dict) else {}
    fee_enabled = bool(fee_cfg.get("enabled", False))
    fee_rate = as_float(fee_cfg.get("fee_rate"))
    fee_exponent = as_float(fee_cfg.get("fee_exponent"))

    lines = [
        f"领单地址: {meta.get('address', 'N/A')}",
        f"交易样本数: {meta.get('fetched_events', 'N/A')}",
        f"回放事件数(含聚合信号): {meta.get('replay_events', 'N/A')}",
        f"追踪窗口(UTC): {tracked_window_text}",
        "样本范围受 max-activities 截断",
        (
            "窗口真实收益(USDC): "
            f"{format_money(benchmark_delta) if benchmark_delta is not None else 'N/A'}"
        ),
        (
            "领单BUY总流量(USDC): "
            f"{format_money(leader_buy_total) if leader_buy_total is not None else 'N/A'}"
        ),
        f"策略数量: {meta.get('strategies', strategies_count)}",
        (
            "固定金额档位: "
            f"{format_meta_usd_options(meta.get('fixed_usd_options', meta.get('fixed_usd')))}"
        ),
        (
            "比例金额上限档位: "
            f"{format_meta_usd_options(meta.get('proportional_cap_usd_options'))}"
        ),
        (
            "比例跟单档位: "
            f"{format_meta_pct_options(meta.get('proportional_pct_options', meta.get('proportional_pct')))}"
        ),
        (
            "买入溢价: "
            f"{format_ratio_pct(meta.get('buy_price_premium_pct')) if meta.get('buy_price_premium_pct') is not None else 'N/A'}"
        ),
        (
            "镜像卖出滑点: "
            f"{format_ratio_pct(meta.get('mirror_sell_slippage_pct')) if meta.get('mirror_sell_slippage_pct') is not None else 'N/A'}"
        ),
        f"买入价格区间: {buy_limits_text}",
        f"卖出价格区间: {sell_limits_text}",
        (
            "聚合后BUY信号: "
            f"{format_count(signal_stats.get('buy_signal_count', meta.get('buy_signal_count')))} "
            f"(其中聚合信号 {format_count(signal_stats.get('aggregated_buy_signal_count', meta.get('aggregated_buy_signal_count')))})"
        ),
        f"平均每市场下注次数: {format_decimal(avg_bets_per_market, ndigits=3)}",
        f"平均每市场下注金额(USDC): {format_money(avg_usd_per_market)}",
        f"平均每笔下注金额(USDC): {format_money(avg_usd_per_bet)}",
        (
            "防放大约束: "
            f"{'开启' if guard_enabled else '关闭'} "
            f"(单笔<= {format_decimal(guard_per_trade, ndigits=3)}x, "
            f"单市场<= {format_decimal(guard_per_market, ndigits=3)}x)"
        ),
        (
            "防放大统计: "
            f"裁剪 {format_count(guard_trimmed_count)} 次 / {format_money(guard_trimmed_usd)} USDC, "
            f"超限率(前→后) {format_ratio_pct(oversize_before)} → {format_ratio_pct(oversize_after)}"
        ),
        (
            "自动调参: "
            f"{'开启' if opt_enabled else '关闭'}"
            f" (轮次 {opt_rounds if opt_rounds is not None else 'N/A'}, 扩网 {opt_expands if opt_expands is not None else 'N/A'} 次)"
        ),
    ]
    if fee_cfg:
        lines.insert(12, "主口径固定: 有手续费 + 买3卖1滑点")
        lines.insert(
            13,
            (
                "手续费参数: "
                f"{'开启' if fee_enabled else '关闭'} "
                f"(feeRate={format_ratio_pct(fee_rate)}, exponent={format_decimal(fee_exponent, ndigits=3)})"
            ),
        )
    window_analysis = meta.get("window_analysis") if isinstance(meta.get("window_analysis"), dict) else {}
    windows = window_analysis.get("windows") if isinstance(window_analysis.get("windows"), list) else []
    if windows:
        lines.append(
            f"窗口切片: 按 activity 数量等分 {as_int(window_analysis.get('window_count')) or len(windows)} 段（Top{as_int(window_analysis.get('top_n')) or WINDOW_REPORT_TOP_N}）"
        )
        for row in windows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or row.get("window_id") or "N/A")
            r = str(row.get("activity_range") or "N/A")
            s = str(row.get("start_utc") or "N/A")
            e = str(row.get("end_utc") or "N/A")
            lines.append(f"{title}: {r} | {s} ~ {e}")
    return lines


def _add_cover_page(
    pdf: Any,
    meta: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    import matplotlib.pyplot as plt

    best = pick_objective_best_for_display(results, meta=meta) or results[0]
    top5 = sort_optimizer_rows(results, roi_tie_threshold=roi_tie_threshold_from_meta(meta))[:5]

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle("跟单策略可视化报告", fontsize=23, fontweight="bold", y=0.98)

    ax_info = fig.add_axes([0.04, 0.56, 0.92, 0.38])
    ax_info.axis("off")
    ax_info.text(0.0, 0.98, f"报告生成时间(UTC): {now_utc_iso()}", fontsize=11, va="top")
    meta_lines = _cover_meta_lines(meta, len(results))
    left_lines, right_lines = _split_lines_two_columns(meta_lines)
    max_lines = max(len(left_lines), len(right_lines), 1)
    start_y = 0.88
    end_y = 0.06
    line_step = (start_y - end_y) / max(1, max_lines - 1) if max_lines > 1 else 0.0
    info_fontsize = 11.0 if max_lines <= 8 else (10.2 if max_lines <= 12 else (9.4 if max_lines <= 16 else 8.8))
    for idx, line in enumerate(left_lines):
        y = start_y - idx * line_step
        ax_info.text(0.00, y, line, fontsize=info_fontsize, va="top")
    for idx, line in enumerate(right_lines):
        y = start_y - idx * line_step
        ax_info.text(0.50, y, line, fontsize=info_fontsize, va="top")

    ax_best = fig.add_axes([0.04, 0.33, 0.92, 0.20])
    ax_best.axis("off")
    ax_best.text(0.0, 0.95, "最佳参数设计（ROI优先 + 规模次优）", fontsize=15, fontweight="bold", va="top")
    ax_best.text(0.0, 0.70, strategy_name_cn(best), fontsize=11, va="top")
    ax_best.text(
        0.0,
        0.46,
        (
            f"最终总收益: {format_money(best.get('total_pnl'))} USDC    "
            f"收益率: {format_ratio_pct(best.get('roi'))}    "
            f"总投入: {format_money(best.get('total_buy_cost'))} USDC"
        ),
        fontsize=12,
        fontweight="bold",
        va="top",
    )
    ax_best.text(
        0.0,
        0.23,
        (
            f"跟随买入次数: {format_count(best.get('copied_buys'))}    "
            f"镜像卖出次数: {format_count(best.get('mirrored_sells'))}    "
            f"开仓数量: {format_count(best.get('open_positions'))}"
        ),
        fontsize=11,
        va="top",
    )
    ax_best.text(
        0.0,
        0.03,
        (
            "规模归一对照: "
            f"缩放后对照收益 {format_money(best.get('scaled_benchmark_pnl'))} USDC    "
            f"归一差距 {format_money(best.get('normalized_gap'))} USDC    "
            f"捕获率 {format_decimal(best.get('capture_rate'), ndigits=3)}"
        ),
        fontsize=10.5,
        va="top",
    )

    table_rows = []
    for idx, row in enumerate(top5, start=1):
        table_rows.append(
            [
                str(idx),
                strategy_name_cn_short(row),
                format_money(row.get("total_pnl")),
                format_ratio_pct(row.get("roi")),
                format_money(row.get("total_buy_cost")),
                format_count(row.get("copied_buys")),
            ]
        )

    ax_table = fig.add_axes([0.04, 0.05, 0.92, 0.24])
    ax_table.axis("off")
    ax_table.set_title("Top5 策略摘要", fontsize=14, pad=8)
    table = ax_table.table(
        cellText=table_rows,
        colLabels=["排名", "参数设计", "总收益(USDC)", "收益率", "总投入(USDC)", "跟随买入次数"],
        cellLoc="center",
        loc="center",
        colWidths=[0.06, 0.46, 0.14, 0.10, 0.14, 0.10],
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.0, 1.5)
    for (row_idx, _), cell in table.get_celld().items():
        if row_idx == 0:
            cell.set_text_props(weight="bold")
            cell.set_facecolor("#EFEFEF")

    pdf.savefig(fig)
    plt.close(fig)


def _add_rank_chart_page(
    pdf: Any,
    *,
    ranked_rows: List[Dict[str, Any]],
    sampled_rows: List[Dict[str, Any]],
    title: str,
    value_key: str,
    x_label: str,
    is_percent: bool,
    positive_color: str,
    negative_color: str,
) -> None:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11.69, 8.27))
    if not sampled_rows:
        ax.axis("off")
        ax.text(0.5, 0.5, "没有可用数据", ha="center", va="center", fontsize=16)
        pdf.savefig(fig)
        plt.close(fig)
        return

    rank_map = {str(row.get("strategy")): idx for idx, row in enumerate(ranked_rows, start=1)}
    labels = [
        f"{rank_map.get(str(row.get('strategy')), idx)}. {strategy_name_cn_short(row)}"
        for idx, row in enumerate(sampled_rows, start=1)
    ]
    values = [safe_result_float(row, value_key, 0.0) for row in sampled_rows]
    if is_percent:
        values = [value * 100.0 for value in values]

    colors = [positive_color if value >= 0 else negative_color for value in values]
    y_pos = list(range(len(labels)))
    bars = ax.barh(y_pos, values, color=colors, alpha=0.9)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.axvline(0, color="#333333", linewidth=1)
    ax.set_title(title, fontsize=16, pad=14)
    ax.set_xlabel(x_label)

    max_abs = max(abs(value) for value in values) if values else 1.0
    shift = max(0.02 * max_abs, 0.2 if not is_percent else 0.05)
    for bar, value in zip(bars, values):
        y = bar.get_y() + bar.get_height() / 2.0
        if value >= 0:
            x = value + shift
            ha = "left"
        else:
            x = value - shift
            ha = "right"
        text = f"{value:.2f}%" if is_percent else f"{value:,.2f}"
        ax.text(x, y, text, va="center", ha=ha, fontsize=9)

    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def _add_total_pnl_chart_pages(
    pdf: Any,
    rows: List[Dict[str, Any]],
    per_side: int,
    *,
    title_prefix: str = "",
    include_bottom: bool = True,
) -> None:
    ranked_rows = sorted(rows, key=strategy_sort_key, reverse=True)
    n = max(1, int(per_side))
    top_rows = ranked_rows[:n]
    bottom_rows = list(reversed(ranked_rows[-n:])) if ranked_rows else []
    prefix = f"{title_prefix} " if title_prefix else ""

    _add_rank_chart_page(
        pdf,
        ranked_rows=ranked_rows,
        sampled_rows=top_rows,
        title=f"{prefix}策略总收益 Top{n}（USDC）",
        value_key="total_pnl",
        x_label="总收益 (USDC)",
        is_percent=False,
        positive_color="#2E8B57",
        negative_color="#C0392B",
    )
    if include_bottom:
        _add_rank_chart_page(
            pdf,
            ranked_rows=ranked_rows,
            sampled_rows=bottom_rows,
            title=f"{prefix}策略总收益 Bottom{n}（USDC）",
            value_key="total_pnl",
            x_label="总收益 (USDC)",
            is_percent=False,
            positive_color="#2E8B57",
            negative_color="#C0392B",
        )


def _add_roi_chart_pages(
    pdf: Any,
    rows: List[Dict[str, Any]],
    per_side: int,
    *,
    title_prefix: str = "",
    include_bottom: bool = True,
) -> None:
    roi_rows = [row for row in rows if as_float(row.get("roi")) is not None]
    roi_rows = sorted(roi_rows, key=strategy_roi_sort_key, reverse=True)
    n = max(1, int(per_side))
    top_rows = roi_rows[:n]
    bottom_rows = list(reversed(roi_rows[-n:])) if roi_rows else []
    prefix = f"{title_prefix} " if title_prefix else ""

    _add_rank_chart_page(
        pdf,
        ranked_rows=roi_rows,
        sampled_rows=top_rows,
        title=f"{prefix}策略 ROI Top{n}（%）",
        value_key="roi",
        x_label="ROI (%)",
        is_percent=True,
        positive_color="#1F77B4",
        negative_color="#8E44AD",
    )
    if include_bottom:
        _add_rank_chart_page(
            pdf,
            ranked_rows=roi_rows,
            sampled_rows=bottom_rows,
            title=f"{prefix}策略 ROI Bottom{n}（%）",
            value_key="roi",
            x_label="ROI (%)",
            is_percent=True,
            positive_color="#1F77B4",
            negative_color="#8E44AD",
        )


def _add_normalized_gap_chart_pages(
    pdf: Any,
    rows: List[Dict[str, Any]],
    per_side: int,
    *,
    title_prefix: str = "",
) -> None:
    gap_rows = [row for row in rows if as_float(row.get("normalized_gap")) is not None]
    gap_rows = sorted(gap_rows, key=strategy_normalized_gap_sort_key, reverse=True)
    n = max(1, int(per_side))
    top_rows = gap_rows[:n]
    bottom_rows = list(reversed(gap_rows[-n:])) if gap_rows else []
    prefix = f"{title_prefix} " if title_prefix else ""

    _add_rank_chart_page(
        pdf,
        ranked_rows=gap_rows,
        sampled_rows=top_rows,
        title=f"{prefix}规模归一差距 Top{n}（USDC）",
        value_key="normalized_gap",
        x_label="归一差距 (USDC)",
        is_percent=False,
        positive_color="#0E8A6A",
        negative_color="#D35400",
    )
    _add_rank_chart_page(
        pdf,
        ranked_rows=gap_rows,
        sampled_rows=bottom_rows,
        title=f"{prefix}规模归一差距 Bottom{n}（USDC）",
        value_key="normalized_gap",
        x_label="归一差距 (USDC)",
        is_percent=False,
        positive_color="#0E8A6A",
        negative_color="#D35400",
    )


def _add_capture_rate_chart_pages(
    pdf: Any,
    rows: List[Dict[str, Any]],
    per_side: int,
    *,
    title_prefix: str = "",
) -> None:
    capture_rows = [row for row in rows if as_float(row.get("capture_rate")) is not None]
    capture_rows = sorted(capture_rows, key=strategy_capture_rate_sort_key, reverse=True)
    n = max(1, int(per_side))
    top_rows = capture_rows[:n]
    bottom_rows = list(reversed(capture_rows[-n:])) if capture_rows else []
    prefix = f"{title_prefix} " if title_prefix else ""

    _add_rank_chart_page(
        pdf,
        ranked_rows=capture_rows,
        sampled_rows=top_rows,
        title=f"{prefix}规模收益捕获率 Top{n}",
        value_key="capture_rate",
        x_label="捕获率 (x)",
        is_percent=False,
        positive_color="#34495E",
        negative_color="#8E44AD",
    )
    _add_rank_chart_page(
        pdf,
        ranked_rows=capture_rows,
        sampled_rows=bottom_rows,
        title=f"{prefix}规模收益捕获率 Bottom{n}",
        value_key="capture_rate",
        x_label="捕获率 (x)",
        is_percent=False,
        positive_color="#34495E",
        negative_color="#8E44AD",
    )


def _window_top_rows_for_metric(
    window_row: Dict[str, Any],
    *,
    metric_key: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    if metric_key == "total_pnl":
        rows_raw = window_row.get("top10_total_pnl")
    elif metric_key == "roi":
        rows_raw = window_row.get("top10_roi")
    else:
        rows_raw = []
    if not isinstance(rows_raw, list):
        return []
    rows: List[Dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        rows.append(dict(row))
    return rows[: max(1, int(top_n))]


def _add_window_metric_matrix_page(
    pdf: Any,
    *,
    windows: List[Dict[str, Any]],
    metric_key: str,
    top_n: int,
    page_title: str,
) -> None:
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(11.69, 8.27))
    fig.suptitle(page_title, fontsize=17, fontweight="bold", y=0.98)

    if not windows:
        ax = fig.add_axes([0.04, 0.08, 0.92, 0.84])
        ax.axis("off")
        ax.text(0.5, 0.5, "没有可用窗口数据", ha="center", va="center", fontsize=16)
        pdf.savefig(fig)
        plt.close(fig)
        return

    ax_meta = fig.add_axes([0.04, 0.74, 0.92, 0.18])
    ax_meta.axis("off")
    meta_lines: List[str] = []
    for idx, win in enumerate(windows, start=1):
        title = str(win.get("title") or f"窗口{idx}")
        activity_range = str(win.get("activity_range") or "N/A")
        start_utc = str(win.get("start_utc") or "N/A")
        end_utc = str(win.get("end_utc") or "N/A")
        meta_lines.append(f"{title}: {activity_range} | {start_utc} ~ {end_utc}")
    for idx, line in enumerate(meta_lines):
        ax_meta.text(0.0, 1.0 - idx * 0.20, line, fontsize=8.8, va="top")

    ax_table = fig.add_axes([0.03, 0.05, 0.94, 0.66])
    ax_table.axis("off")
    top_n_int = max(1, int(top_n))
    headers = ["排名"] + [str(win.get("title") or f"窗口{idx}") for idx, win in enumerate(windows, start=1)]
    per_window_top = [_window_top_rows_for_metric(win, metric_key=metric_key, top_n=top_n_int) for win in windows]
    table_rows: List[List[str]] = []
    for rank_idx in range(top_n_int):
        row_cells = [str(rank_idx + 1)]
        for sampled_rows in per_window_top:
            if rank_idx >= len(sampled_rows):
                row_cells.append("-")
                continue
            item = sampled_rows[rank_idx]
            strategy_label = strategy_name_cn_short(item)
            metric_value = (
                format_money(item.get("total_pnl"))
                if metric_key == "total_pnl"
                else format_ratio_pct(item.get("roi"))
            )
            row_cells.append(f"{strategy_label}\n{metric_value}")
        table_rows.append(row_cells)

    first_col_w = 0.06
    per_col_w = (1.0 - first_col_w) / max(1, len(windows))
    col_widths = [first_col_w] + [per_col_w] * len(windows)
    table = ax_table.table(
        cellText=table_rows,
        colLabels=headers,
        cellLoc="left",
        colLoc="center",
        loc="upper left",
        colWidths=col_widths,
    )
    table.auto_set_font_size(False)
    table.set_fontsize(7.5)
    table.scale(1.0, 2.2)
    for (row_idx, col_idx), cell in table.get_celld().items():
        if row_idx == 0:
            cell.set_text_props(weight="bold")
            cell.set_facecolor("#EFEFEF")
            continue
        if col_idx == 0:
            cell.set_text_props(ha="center", weight="bold")

    pdf.savefig(fig)
    plt.close(fig)


def _normalize_report_section_rows(rows_raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(rows_raw, list):
        return []
    rows: List[Dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            continue
        rows.append(normalize_result_row(dict(row)))
    return rows


def _resolve_report_sections(
    *,
    meta: Dict[str, Any],
    main_results: List[Dict[str, Any]],
    no_slip_results: Optional[List[Dict[str, Any]]] = None,
    report_sections: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    sections_source = report_sections
    if not isinstance(sections_source, list) or not sections_source:
        sections_source = meta.get("report_sections") if isinstance(meta.get("report_sections"), list) else None

    resolved: List[Dict[str, Any]] = []
    if isinstance(sections_source, list):
        for idx, raw in enumerate(sections_source):
            if not isinstance(raw, dict):
                continue
            title = str(raw.get("title") or "").strip()
            if not title:
                continue
            rows = _normalize_report_section_rows(raw.get("rows"))
            if not rows and idx == 0:
                rows = [normalize_result_row(dict(row)) for row in main_results]
            if not rows and isinstance(no_slip_results, list) and no_slip_results and ("无滑点" in title):
                rows = [normalize_result_row(dict(row)) for row in no_slip_results]
            if not rows:
                continue
            rows = sort_results_for_report(apply_scaled_metrics_to_results(rows, meta=meta))
            resolved.append(
                {
                    "id": str(raw.get("id") or f"section_{len(resolved) + 1}"),
                    "title": title,
                    "summary_only": bool(raw.get("summary_only")),
                    "is_main": bool(raw.get("is_main", len(resolved) == 0)),
                    "rows": rows,
                }
            )

    if resolved:
        return resolved

    fallback: List[Dict[str, Any]] = [
        {
            "id": "default_main",
            "title": "【有滑点】" if isinstance(no_slip_results, list) and no_slip_results else "【结果】",
            "summary_only": False,
            "is_main": True,
            "rows": sort_results_for_report(
                apply_scaled_metrics_to_results(
                    [normalize_result_row(dict(row)) for row in main_results],
                    meta=meta,
                )
            ),
        }
    ]
    if isinstance(no_slip_results, list) and no_slip_results:
        fallback.append(
            {
                "id": "default_no_slip",
                "title": "【无滑点】",
                "summary_only": False,
                "is_main": False,
                "rows": sort_results_for_report(
                    apply_scaled_metrics_to_results(
                        [normalize_result_row(dict(row)) for row in no_slip_results],
                        meta=meta,
                    )
                ),
            }
        )
    return fallback


def write_pdf_report(
    pdf_path: Path,
    meta: Dict[str, Any],
    results: List[Dict[str, Any]],
    *,
    report_top: int,
    no_slip_results: Optional[List[Dict[str, Any]]] = None,
    report_sections: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    from matplotlib.backends.backend_pdf import PdfPages

    if not results:
        raise RuntimeError("No strategy results available for report generation.")

    sorted_results = sort_results_for_report(results)
    sorted_no_slip_results = (
        sort_results_for_report(no_slip_results)
        if isinstance(no_slip_results, list) and no_slip_results
        else None
    )
    window_analysis = meta.get("window_analysis") if isinstance(meta.get("window_analysis"), dict) else {}
    top_n = as_int(window_analysis.get("top_n")) or WINDOW_REPORT_TOP_N
    resolved_windows: List[Dict[str, Any]] = []
    windows_raw = window_analysis.get("windows") if isinstance(window_analysis.get("windows"), list) else []
    for idx, raw in enumerate(windows_raw[:5], start=1):
        if not isinstance(raw, dict):
            continue
        rows = sort_results_for_report(
            apply_scaled_metrics_to_results(
                _normalize_report_section_rows(raw.get("rows")),
                meta=meta,
            )
        )
        top_total_pnl = raw.get("top10_total_pnl")
        top_roi = raw.get("top10_roi")
        if not isinstance(top_total_pnl, list):
            top_total_pnl = [_result_row_brief(row) for row in rows[: max(1, int(top_n))]]
        if not isinstance(top_roi, list):
            top_roi = [_result_row_brief(row) for row in _top_rows_by_roi(rows, top_n)]
        resolved_windows.append(
            {
                "window_id": str(raw.get("window_id") or f"window_{idx}"),
                "title": str(raw.get("title") or f"窗口{idx}"),
                "activity_range": str(raw.get("activity_range") or "N/A"),
                "count": as_int(raw.get("count")),
                "start_utc": str(raw.get("start_utc") or "N/A"),
                "end_utc": str(raw.get("end_utc") or "N/A"),
                "rows": rows,
                "top10_total_pnl": top_total_pnl,
                "top10_roi": top_roi,
            }
        )

    if resolved_windows:
        main_rows = resolved_windows[0].get("rows") if isinstance(resolved_windows[0].get("rows"), list) else []
        if not main_rows:
            main_rows = sorted_results
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        selected_font = _configure_matplotlib_chinese()
        print(f"[report] font={selected_font}")
        with PdfPages(pdf_path) as pdf:
            _add_cover_page(pdf, meta, list(main_rows))
            _add_window_metric_matrix_page(
                pdf,
                windows=resolved_windows,
                metric_key="total_pnl",
                top_n=max(1, int(top_n)),
                page_title=f"策略总收益 Top{max(1, int(top_n))}（全量 + 4切片）",
            )
            _add_window_metric_matrix_page(
                pdf,
                windows=resolved_windows,
                metric_key="roi",
                top_n=max(1, int(top_n)),
                page_title=f"策略 ROI Top{max(1, int(top_n))}（全量 + 4切片）",
            )
        return pdf_path

    sections = _resolve_report_sections(
        meta=meta,
        main_results=sorted_results,
        no_slip_results=sorted_no_slip_results,
        report_sections=report_sections,
    )
    main_section = next((section for section in sections if bool(section.get("is_main"))), sections[0])
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    selected_font = _configure_matplotlib_chinese()
    print(f"[report] font={selected_font}")

    with PdfPages(pdf_path) as pdf:
        _add_cover_page(pdf, meta, list(main_section.get("rows") or sorted_results))
        for section in sections:
            rows = section.get("rows") if isinstance(section.get("rows"), list) else []
            if not rows:
                continue
            title = str(section.get("title") or "【结果】")
            summary_only = bool(section.get("summary_only"))
            _add_total_pnl_chart_pages(
                pdf,
                rows,
                max(1, int(report_top)),
                title_prefix=title,
                include_bottom=False,
            )
            _add_roi_chart_pages(
                pdf,
                rows,
                max(1, int(report_top)),
                title_prefix=title,
                include_bottom=False,
            )
            if not summary_only:
                _add_normalized_gap_chart_pages(
                    pdf,
                    rows,
                    max(1, int(report_top)),
                    title_prefix=title,
                )
                _add_capture_rate_chart_pages(
                    pdf,
                    rows,
                    max(1, int(report_top)),
                    title_prefix=title,
                )

    return pdf_path


def _html_escape(value: Any) -> str:
    return html.escape(str(value))


def _html_format_metric(metric_key: str, value: Any) -> str:
    n = as_float(value)
    if n is None:
        return "N/A"
    if metric_key in {"total_pnl", "total_buy_cost", "normalized_gap", "scaled_benchmark_pnl"}:
        return f"{n:,.2f}"
    if metric_key == "roi":
        return f"{n * 100:.2f}%"
    if metric_key == "capture_rate":
        return f"{n:.3f}x"
    return f"{n:.4f}"


def _sorted_rows_for_metric(rows: List[Dict[str, Any]], metric_key: str) -> List[Dict[str, Any]]:
    if metric_key == "total_pnl":
        return sorted(rows, key=strategy_sort_key, reverse=True)
    if metric_key == "roi":
        roi_rows = [row for row in rows if as_float(row.get("roi")) is not None]
        return sorted(roi_rows, key=strategy_roi_sort_key, reverse=True)
    if metric_key == "normalized_gap":
        gap_rows = [row for row in rows if as_float(row.get("normalized_gap")) is not None]
        return sorted(gap_rows, key=strategy_normalized_gap_sort_key, reverse=True)
    if metric_key == "capture_rate":
        capture_rows = [row for row in rows if as_float(row.get("capture_rate")) is not None]
        return sorted(capture_rows, key=strategy_capture_rate_sort_key, reverse=True)
    return list(rows)


def _build_html_rank_block(
    *,
    rows: List[Dict[str, Any]],
    metric_key: str,
    title: str,
    top_n: int,
) -> str:
    ranked = _sorted_rows_for_metric(rows, metric_key)
    if not ranked:
        return f"<section class='panel'><h3>{_html_escape(title)}</h3><p>无可用数据</p></section>"

    n = max(1, int(top_n))
    top_rows = ranked[:n]
    bottom_rows = list(reversed(ranked[-n:])) if ranked else []
    all_vals = [abs(as_float(row.get(metric_key)) or 0.0) for row in top_rows + bottom_rows]
    max_abs = max(all_vals) if all_vals else 1.0
    max_abs = max(max_abs, 1e-9)

    def _rows_html(sampled_rows: List[Dict[str, Any]]) -> str:
        items: List[str] = []
        for row in sampled_rows:
            val = as_float(row.get(metric_key)) or 0.0
            width = min(100.0, (abs(val) / max_abs) * 100.0)
            bar_cls = "bar-pos" if val >= 0 else "bar-neg"
            items.append(
                "<tr>"
                f"<td>{_html_escape(strategy_name_cn_short(row))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric(metric_key, row.get(metric_key)))}</td>"
                "<td class='bar-cell'>"
                f"<div class='bar-wrap'><div class='bar {bar_cls}' style='width:{width:.2f}%'></div></div>"
                "</td>"
                "</tr>"
            )
        return "".join(items)

    return (
        "<section class='panel'>"
        f"<h3>{_html_escape(title)}</h3>"
        "<div class='grid2'>"
        "<div><h4>Top</h4><table><thead><tr><th>策略</th><th>值</th><th>可视化</th></tr></thead><tbody>"
        f"{_rows_html(top_rows)}"
        "</tbody></table></div>"
        "<div><h4>Bottom</h4><table><thead><tr><th>策略</th><th>值</th><th>可视化</th></tr></thead><tbody>"
        f"{_rows_html(bottom_rows)}"
        "</tbody></table></div>"
        "</div>"
        "</section>"
    )


def _build_html_strategy_table(rows: List[Dict[str, Any]], *, compact: bool = False) -> str:
    table_rows: List[str] = []
    for idx, row in enumerate(rows, start=1):
        if compact:
            table_rows.append(
                "<tr>"
                f"<td>{idx}</td>"
                f"<td>{_html_escape(strategy_name_cn_short(row))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('total_pnl', row.get('total_pnl')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('roi', row.get('roi')))}</td>"
                "</tr>"
            )
        else:
            table_rows.append(
                "<tr>"
                f"<td>{idx}</td>"
                f"<td>{_html_escape(strategy_name_cn_short(row))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('total_pnl', row.get('total_pnl')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('roi', row.get('roi')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('total_buy_cost', row.get('total_buy_cost')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('scaled_benchmark_pnl', row.get('scaled_benchmark_pnl')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('normalized_gap', row.get('normalized_gap')))}</td>"
                f"<td class='num'>{_html_escape(_html_format_metric('capture_rate', row.get('capture_rate')))}</td>"
                "</tr>"
            )
    return "".join(table_rows)


def _build_html_report_section(
    *,
    title: str,
    rows: List[Dict[str, Any]],
    meta: Dict[str, Any],
    report_top: int,
    summary_only: bool = False,
) -> str:
    ranked = sort_results_for_report(rows)
    best = pick_objective_best_for_display(ranked, meta=meta) or (ranked[0] if ranked else {})
    actual_delta = as_float(meta.get("actual_window_pnl_delta"))
    leader_buy_total = _leader_buy_total_from_meta(meta)

    cards = (
        "<section class='cards'>"
        f"<div class='card'><div class='label'>窗口真实收益</div><div class='value'>{_html_escape(format_money(actual_delta))} USDC</div></div>"
        f"<div class='card'><div class='label'>领单BUY总流量</div><div class='value'>{_html_escape(format_money(leader_buy_total))} USDC</div></div>"
        f"<div class='card'><div class='label'>冠军ROI</div><div class='value'>{_html_escape(format_ratio_pct(best.get('roi')))}</div></div>"
        f"<div class='card'><div class='label'>冠军捕获率</div><div class='value'>{_html_escape(_html_format_metric('capture_rate', best.get('capture_rate')))}</div></div>"
        "</section>"
    )
    if summary_only:
        blocks = [
            _build_html_rank_block(rows=ranked, metric_key="total_pnl", title=f"{title} 策略总收益", top_n=report_top),
            _build_html_rank_block(rows=ranked, metric_key="roi", title=f"{title} 策略 ROI", top_n=report_top),
        ]
        table = (
            "<section class='panel'>"
            f"<h3>{_html_escape(title)} 全策略汇总</h3>"
            "<div class='table-wrap'><table><thead><tr>"
            "<th>#</th><th>策略</th><th>总收益</th><th>ROI</th>"
            "</tr></thead><tbody>"
            f"{_build_html_strategy_table(ranked, compact=True)}"
            "</tbody></table></div>"
            "</section>"
        )
        return f"<section class='report-section'><h2>{_html_escape(title)}</h2>{''.join(blocks)}{table}</section>"

    blocks = [
        _build_html_rank_block(rows=ranked, metric_key="total_pnl", title=f"{title} 策略总收益", top_n=report_top),
        _build_html_rank_block(rows=ranked, metric_key="roi", title=f"{title} 策略 ROI", top_n=report_top),
        _build_html_rank_block(rows=ranked, metric_key="normalized_gap", title=f"{title} 规模归一差距", top_n=report_top),
        _build_html_rank_block(rows=ranked, metric_key="capture_rate", title=f"{title} 规模收益捕获率", top_n=report_top),
    ]
    table = (
        "<section class='panel'>"
        f"<h3>{_html_escape(title)} 全策略汇总</h3>"
        "<div class='table-wrap'><table><thead><tr>"
        "<th>#</th><th>策略</th><th>总收益</th><th>ROI</th><th>总投入</th>"
        "<th>缩放后对照收益</th><th>归一差距</th><th>捕获率</th>"
        "</tr></thead><tbody>"
        f"{_build_html_strategy_table(ranked, compact=False)}"
        "</tbody></table></div>"
        "</section>"
    )
    return f"<section class='report-section'><h2>{_html_escape(title)}</h2>{cards}{''.join(blocks)}{table}</section>"


def write_html_report(
    html_path: Path,
    meta: Dict[str, Any],
    results: List[Dict[str, Any]],
    *,
    report_top: int,
    no_slip_results: Optional[List[Dict[str, Any]]] = None,
    report_sections: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    if not results:
        raise RuntimeError("No strategy results available for HTML report generation.")

    html_path.parent.mkdir(parents=True, exist_ok=True)
    base_results = sort_results_for_report(results)
    no_slip_sorted = sort_results_for_report(no_slip_results) if isinstance(no_slip_results, list) and no_slip_results else None
    sections = _resolve_report_sections(
        meta=meta,
        main_results=base_results,
        no_slip_results=no_slip_sorted,
        report_sections=report_sections,
    )

    doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>sim_copytrade report</title>"
        "<style>"
        "body{font-family:'Microsoft YaHei','Segoe UI',sans-serif;margin:0;background:#f7f9fc;color:#111827;}"
        ".container{max-width:1400px;margin:0 auto;padding:22px 20px 40px;}"
        "h1{margin:0 0 14px;font-size:30px;}"
        "h2{margin:14px 0;font-size:24px;}"
        "h3{margin:0 0 10px;font-size:18px;}"
        "h4{margin:0 0 8px;font-size:14px;color:#4b5563;}"
        ".meta{margin:0 0 14px;color:#374151;font-size:14px;}"
        ".cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:0 0 16px;}"
        ".card{background:white;border:1px solid #e5e7eb;border-radius:10px;padding:12px;}"
        ".label{font-size:12px;color:#6b7280;margin-bottom:4px;}"
        ".value{font-size:22px;font-weight:700;line-height:1.2;}"
        ".panel{background:white;border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin:0 0 14px;}"
        ".grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}"
        "table{width:100%;border-collapse:collapse;}"
        "th,td{padding:8px;border-bottom:1px solid #eef2f7;font-size:12px;vertical-align:middle;}"
        "th{text-align:left;background:#f8fafc;position:sticky;top:0;z-index:1;}"
        ".num{text-align:right;white-space:nowrap;}"
        ".table-wrap{max-height:560px;overflow:auto;border:1px solid #eef2f7;border-radius:8px;}"
        ".bar-cell{width:38%;}"
        ".bar-wrap{height:10px;background:#f3f4f6;border-radius:999px;overflow:hidden;}"
        ".bar{height:100%;border-radius:999px;}"
        ".bar-pos{background:linear-gradient(90deg,#16a34a,#22c55e);}"
        ".bar-neg{background:linear-gradient(90deg,#f97316,#ef4444);}"
        "@media (max-width:960px){.grid2{grid-template-columns:1fr;}.value{font-size:18px;}}"
        "</style></head><body><div class='container'>"
        "<h1>跟单策略可视化报告（HTML）</h1>"
        f"<p class='meta'>生成时间(UTC): {_html_escape(now_utc_iso())} | 地址: {_html_escape(meta.get('address', 'N/A'))}</p>"
    )

    for section in sections:
        rows = section.get("rows") if isinstance(section.get("rows"), list) else []
        if not rows:
            continue
        doc += _build_html_report_section(
            title=str(section.get("title") or "【结果】"),
            rows=rows,
            meta=meta,
            report_top=report_top,
            summary_only=bool(section.get("summary_only")),
        )

    doc += "</div></body></html>"
    html_path.write_text(doc, encoding="utf-8")
    return html_path


def build_results_with_scaled_metrics(
    states: List[StrategyState],
    *,
    price_map: Dict[str, PriceInfo],
    leader_buy_signal_total_usd: Optional[float],
    actual_window_pnl_delta: Optional[float],
) -> List[Dict[str, Any]]:
    rows = [build_strategy_result(state, price_map) for state in states]
    for row in rows:
        apply_scaled_metrics_to_result_row(
            row,
            leader_buy_signal_total_usd=leader_buy_signal_total_usd,
            actual_window_pnl_delta=actual_window_pnl_delta,
        )
    return sort_results_for_report(rows)


def print_top_results(results: List[Dict[str, Any]], top_n: int) -> None:
    print("\n=== Top Strategies ===")
    for idx, row in enumerate(results[:top_n], start=1):
        roi = row.get("roi")
        roi_str = f"{roi * 100:.2f}%" if isinstance(roi, (int, float)) else "n/a"
        print(
            f"[{idx:02d}] {row['strategy']} | pnl={row['total_pnl']:.2f} | "
            f"roi={roi_str} | cost={row['total_buy_cost']:.2f} | "
            f"buys={row['copied_buys']} | open={row['open_positions']}"
        )


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Polymarket leader multi-strategy copytrade simulator")
    ap.add_argument("--address", type=str, default="", help="Leader wallet address (required for report-mode=live)")
    ap.add_argument("--max-activities", type=int, default=5000, help="Max TRADE activities to fetch")
    ap.add_argument("--page-limit", type=int, default=1000, help="Per-request page limit")
    ap.add_argument(
        "--fixed-usd-options",
        type=str,
        default="10,50,100",
        help="Fixed copy USD options (comma separated)",
    )
    ap.add_argument(
        "--proportional-cap-usd-options",
        type=str,
        default="10,50,100",
        help="Proportional copy per-buy USD cap options (comma separated)",
    )
    ap.add_argument(
        "--proportional-pct-options",
        type=str,
        default="0.01,0.03,0.05",
        help="Proportional copy ratio options (comma separated, in (0,1))",
    )
    ap.add_argument("--proportional-pct", type=float, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--max-entry-times", type=int, default=10, help="Max follow-buy times per market")
    ap.add_argument("--buy-price-premium-pct", type=float, default=0.03, help="Default buy premium over leader price")
    ap.add_argument("--buy-min-price", type=float, default=0.01, help="Buy min price")
    ap.add_argument("--buy-max-price", type=float, default=0.99, help="Buy max price")
    ap.add_argument("--sell-min-price", type=float, default=0.01, help="Sell min price for mirror_sell")
    ap.add_argument("--sell-max-price", type=float, default=0.99, help="Sell max price for mirror_sell")
    ap.add_argument(
        "--anti-amplification-guard",
        action="store_true",
        default=True,
        help="Enable anti-amplification guard (our size cannot exceed leader caps)",
    )
    ap.add_argument(
        "--no-anti-amplification-guard",
        action="store_false",
        dest="anti_amplification_guard",
        help="Disable anti-amplification guard",
    )
    ap.add_argument(
        "--max-our-vs-leader-per-trade",
        type=float,
        default=1.0,
        help="Per-trade cap multiplier: our_usd <= leader_usd * value",
    )
    ap.add_argument(
        "--max-our-vs-leader-per-market",
        type=float,
        default=1.0,
        help="Per-market cumulative cap multiplier: our_market_usd <= leader_market_usd * value",
    )
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    ap.add_argument("--price-workers", type=int, default=16, help="Parallel workers for token price fetch")
    ap.add_argument("--top", type=int, default=10, help="Top N strategies to print")
    ap.add_argument("--out-dir", type=str, default="", help="Output directory (default: <script_dir>/output)")
    ap.add_argument("--report-mode", choices=("live", "json", "csv"), default="live", help="Report source mode")
    ap.add_argument("--report-input", type=str, default="", help="Input file for report-mode=json/csv")
    ap.add_argument("--report-meta-json", type=str, default="", help="Optional meta JSON for report-mode=csv")
    ap.add_argument("--report-pdf", type=str, default="", help="Output PDF report path")
    ap.add_argument("--report-html", type=str, default="", help="Output HTML report path")
    ap.add_argument(
        "--report-top",
        type=int,
        default=25,
        help="Top N strategies per chart page",
    )
    ap.add_argument("--auto-optimize", action="store_true", default=True, help="Enable ROI+scale auto optimization")
    ap.add_argument("--no-auto-optimize", action="store_false", dest="auto_optimize", help="Disable auto optimization")
    ap.add_argument(
        "--optimizer-roi-tie-threshold",
        type=float,
        default=OPT_DEFAULT_ROI_TIE_THRESHOLD,
        help="ROI absolute tie threshold for scale tie-break (0.001 == 0.10%%)",
    )
    ap.add_argument("--opt-budget-minutes", type=float, default=45.0, help="Auto optimization time budget (minutes)")
    ap.add_argument("--opt-rounds", type=int, default=3, help="Maximum optimization rounds")
    ap.add_argument(
        "--opt-min-capital-ratio",
        type=float,
        default=0.001,
        help="Minimum capital ratio vs leader buy flow for ROI ranking",
    )
    ap.add_argument(
        "--opt-min-copied-buys",
        type=int,
        default=150,
        help="Minimum copied buys for ROI ranking",
    )
    ap.add_argument("--ai-report", action="store_true", default=True, help="Generate AI text report for each run")
    ap.add_argument("--no-ai-report", action="store_false", dest="ai_report", help="Disable AI text report")
    ap.add_argument("--ai-report-path", type=str, default="", help="Optional output markdown path for AI report")
    ap.add_argument(
        "--ai-execute-improve",
        action="store_true",
        default=True,
        help="Run AI-driven improvement experiment rounds after baseline optimization",
    )
    ap.add_argument(
        "--no-ai-execute-improve",
        action="store_false",
        dest="ai_execute_improve",
        help="Disable AI-driven improvement experiment rounds",
    )
    ap.add_argument(
        "--ai-improve-rounds",
        type=int,
        default=8,
        help="Maximum rounds for AI-driven improvement experiments",
    )
    ap.add_argument(
        "--ai-improve-budget-minutes",
        type=float,
        default=45.0,
        help="Time budget (minutes) for AI-driven improvement experiments",
    )
    ap.add_argument(
        "--ai-improve-top-candidates",
        type=int,
        default=AI_IMPROVE_TOP_CANDIDATES_PER_ROUND,
        help="Maximum candidate experiments per AI-improve round",
    )
    ap.add_argument(
        "--ai-improve-bound-profile",
        choices=("conservative", "moderate", "aggressive"),
        default="aggressive",
        help="Bound profile for AI-driven experiment expansion",
    )
    return ap.parse_args()


# ---------------------------------------------------------------------------
# Passive follow probe — triggered when all standard strategies have negative ROI
# ---------------------------------------------------------------------------

PASSIVE_PROBE_CAPS: List[Optional[float]] = [5.0, 10.0, 15.0, 20.0, 50.0]
PASSIVE_PROBE_FILL_RATES: List[float] = [0.3, 0.5, 0.7]
PASSIVE_PROBE_SEEDS: int = 3


def run_passive_follow_probe(
    raw_events: List[TradeEvent],
    price_map: Dict[str, PriceInfo],
    best_standard_roi: float,
) -> Dict[str, Any]:
    """Run passive follow simulation when standard strategies all have negative ROI."""
    try:
        from passive_follow_sim import (
            run_passive_simulation,
            generate_passive_strategies,
            prepare_raw_events,
            build_summary,
        )
    except ImportError:
        try:
            from sim_copytrade.passive_follow_sim import (
                run_passive_simulation,
                generate_passive_strategies,
                prepare_raw_events,
                build_summary,
            )
        except ImportError:
            return {"triggered": True, "error": "passive_follow_sim not found", "recommendation": "error"}

    events = prepare_raw_events(raw_events)
    strategies = generate_passive_strategies(PASSIVE_PROBE_CAPS)
    all_rows: List[Dict[str, Any]] = []

    for fr in PASSIVE_PROBE_FILL_RATES:
        for seed_idx in range(PASSIVE_PROBE_SEEDS):
            seed = 1000 + seed_idx
            states = run_passive_simulation(
                events, strategies,
                fill_probability=fr,
                random_seed=seed,
            )
            for state in states:
                result = build_strategy_result(state, price_map)
                result["fill_probability"] = fr
                result["random_seed"] = seed
                all_rows.append(result)

    summary = build_summary(all_rows)

    # Find breakeven per cap
    breakeven: Dict[str, Any] = {}
    for strat_name in sorted(set(r["strategy"] for r in summary)):
        cap_rows = [r for r in summary if r["strategy"] == strat_name]
        cap_rows.sort(key=lambda x: x["fill_probability"])
        positive = [r for r in cap_rows if r["avg_roi"] is not None and r["avg_roi"] > 0]
        if positive:
            best = min(positive, key=lambda x: x["fill_probability"])
            breakeven[strat_name] = {
                "min_fill_rate": best["fill_probability"],
                "roi": best["avg_roi"],
                "avg_pnl": best["avg_total_pnl"],
            }

    viable = len(breakeven) > 0
    recommendation = "passive_follow_viable" if viable else "no_viable_strategy"

    return {
        "triggered": True,
        "trigger_reason": f"best_standard_roi={best_standard_roi:.6f}",
        "caps": PASSIVE_PROBE_CAPS,
        "fill_rates": PASSIVE_PROBE_FILL_RATES,
        "seeds": PASSIVE_PROBE_SEEDS,
        "summary": summary,
        "breakeven": breakeven,
        "recommendation": recommendation,
    }


def run_live_mode(args: argparse.Namespace, out_dir: Path) -> int:
    address = str(args.address or "").lower().strip()
    if not address:
        raise SystemExit("--address is required when --report-mode=live")
    main_buy_premium_pct = float(MAIN_BUY_PREMIUM_PCT)
    main_sell_slippage_pct = float(MAIN_SELL_SLIPPAGE_PCT)
    main_fee_enabled = True
    main_fee_rate = float(SPORTS_FEE_RATE)
    main_fee_exponent = float(SPORTS_FEE_EXPONENT)

    fixed_usd_options = parse_float_options(args.fixed_usd_options, arg_name="--fixed-usd-options")
    proportional_cap_usd_options = parse_float_options(
        args.proportional_cap_usd_options,
        arg_name="--proportional-cap-usd-options",
    )
    if args.proportional_pct is not None:
        proportional_pct_options = [float(args.proportional_pct)]
    else:
        proportional_pct_options = parse_pct_options(
            args.proportional_pct_options,
            arg_name="--proportional-pct-options",
        )

    print("=== Simulator Start ===")
    print(f"address={address}")
    print(
        "combination base: "
        f"fixed_usd_options={format_usd_options(fixed_usd_options)}, "
        f"proportional_pct_options={format_pct_options(proportional_pct_options)}, "
        f"proportional_cap_usd_options={format_usd_options(proportional_cap_usd_options)}, "
        f"max_entries=1..{args.max_entry_times}, exit=[{ONLY_EXIT_MODE}], "
        f"buy_premium={main_buy_premium_pct * 100:.2f}%(fixed), "
        f"sell_slippage(mirror_sell)={main_sell_slippage_pct * 100:.2f}%(fixed), "
        f"fee_rate={main_fee_rate * 100:.2f}%(fixed), exponent={main_fee_exponent:.2f}"
    )
    print(f"[sim] note: --buy-price-premium-pct is ignored in live simulation, main scenario fixed at 3%/1% + fee")
    print(
        "auto-optimize: "
        f"{'on' if args.auto_optimize else 'off'} "
        f"(objective=ROI+scale, tie={float(args.optimizer_roi_tie_threshold) * 100:.2f}%, "
        f"guardrail cap_ratio>={args.opt_min_capital_ratio}, "
        f"copied_buys>={args.opt_min_copied_buys}, rounds<={args.opt_rounds}, "
        f"budget={args.opt_budget_minutes:.1f}m)"
    )
    print(
        "ai-execute-improve: "
        f"{'on' if args.ai_execute_improve else 'off'} "
        f"(profile={args.ai_improve_bound_profile}, rounds<={args.ai_improve_rounds}, "
        f"budget={args.ai_improve_budget_minutes:.1f}m, top_candidates/round={max(1, int(args.ai_improve_top_candidates))})"
    )
    print(
        "anti-amplification-guard: "
        f"{'on' if args.anti_amplification_guard else 'off'} "
        f"(per_trade<={float(args.max_our_vs_leader_per_trade):.3f}x, "
        f"per_market<={float(args.max_our_vs_leader_per_market):.3f}x)"
    )

    session = requests.Session()
    t0 = time.time()
    run_cache_root = out_dir / ".run_cache"
    stale_removed = cleanup_stale_run_cache_dirs(run_cache_root, max_age_s=RUN_CACHE_MAX_AGE_S)
    if stale_removed > 0:
        print(f"[run-cache] cleaned stale runs={stale_removed}")
    run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_cache_dir = run_cache_root / run_id
    events_cache_path = run_cache_dir / "events.jsonl.gz"
    run_cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"[run-cache] path={events_cache_path}")

    try:
        events = fetch_activity_events(
            session,
            address,
            max_activities=max(1, int(args.max_activities)),
            page_limit=max(1, int(args.page_limit)),
            timeout_s=float(args.timeout),
        )
        if not events:
            raise SystemExit("No activity fetched. Check address or network.")
        print(f"[fetch] done events={len(events)} elapsed={time.time() - t0:.2f}s")

        save_events_to_temp_cache(events_cache_path, events)
        cached_events = load_events_from_temp_cache(events_cache_path)
        if cached_events:
            events = cached_events
        print(f"[run-cache] cached events ready count={len(events)}")

        tracked_first_trade_ts = int(events[0].ts) if events else None
        tracked_last_trade_ts = int(events[-1].ts) if events else None
        tracked_first_trade_utc = format_utc_from_epoch(tracked_first_trade_ts)
        tracked_last_trade_utc = format_utc_from_epoch(tracked_last_trade_ts)
        tracked_span_days: Optional[float] = None
        if (
            tracked_first_trade_ts is not None
            and tracked_last_trade_ts is not None
            and tracked_last_trade_ts >= tracked_first_trade_ts
        ):
            tracked_span_days = (tracked_last_trade_ts - tracked_first_trade_ts) / 86400.0

        benchmark = compute_tracked_window_benchmark(
            session,
            address,
            first_ts=tracked_first_trade_ts,
            last_ts=tracked_last_trade_ts,
        )
        actual_window_pnl_delta = as_float(benchmark.get("actual_window_pnl_delta"))
        print(
            "[benchmark] "
            f"actual_window_pnl_delta={format_money(actual_window_pnl_delta)} "
            f"series_points={benchmark.get('series_points', 0)}"
        )

        replay_events = build_replay_events_with_maker_like(events)
        buy_signal_stats = summarize_buy_signal_stats(replay_events)
        leader_buy_signal_total_usd = as_float(buy_signal_stats.get("buy_signal_total_usd"))
        print(
            "[sim] maker-like replay: "
            f"raw_events={len(events)} replay_events={len(replay_events)} "
            f"buy_signals={buy_signal_stats.get('buy_signal_count', 0)} "
            f"aggregated_buy_signals={buy_signal_stats.get('aggregated_buy_signal_count', 0)}"
        )

        price_token_ids = sorted(
            {
                str(event.token_id)
                for event in replay_events
                if event.side == "BUY" and bool(event.copy_signal) and event.token_id
            }
        )
        print(f"[price] unique candidate tokens={len(price_token_ids)}")
        t_price = time.time()
        price_map = fetch_prices_for_tokens(
            price_token_ids,
            timeout_s=float(args.timeout),
            workers=max(1, int(args.price_workers)),
        )
        print(f"[price] done elapsed={time.time() - t_price:.2f}s")

        initial_space = {
            "fixed_usd_options": sorted(set(float(v) for v in fixed_usd_options)),
            "proportional_pct_options": sorted(set(float(v) for v in proportional_pct_options)),
            "proportional_cap_usd_options": sorted(set(float(v) for v in proportional_cap_usd_options)),
            "max_entry_times": max(1, int(args.max_entry_times)),
        }
        current_space = {
            "fixed_usd_options": list(initial_space["fixed_usd_options"]),
            "proportional_pct_options": list(initial_space["proportional_pct_options"]),
            "proportional_cap_usd_options": list(initial_space["proportional_cap_usd_options"]),
            "max_entry_times": int(initial_space["max_entry_times"]),
        }

        strategy_map: Dict[str, Strategy] = {}
        for strategy in generate_strategies(
            fixed_usd_options=current_space["fixed_usd_options"],
            proportional_pct_options=current_space["proportional_pct_options"],
            proportional_cap_usd_options=current_space["proportional_cap_usd_options"],
            max_entries=current_space["max_entry_times"],
        ):
            strategy_map[strategy.name] = strategy
        default_strategy_list = list(strategy_map.values())
        print(f"[sim] initial strategy count={len(strategy_map)}")

        optimization_rounds: List[Dict[str, Any]] = []
        best_winner_row: Optional[Dict[str, Any]] = None
        no_improve_rounds = 0
        expansion_rounds = 0
        final_states: List[StrategyState] = []
        final_results: List[Dict[str, Any]] = []
        optimization_start = time.time()
        max_rounds = max(1, int(args.opt_rounds)) if args.auto_optimize else 1

        for round_idx in range(1, max_rounds + 1):
            round_t0 = time.time()
            strategy_list = list(strategy_map.values())
            states = run_simulation(
                replay_events,
                strategy_list,
                buy_price_premium_pct=main_buy_premium_pct,
                buy_min_price=float(args.buy_min_price),
                buy_max_price=float(args.buy_max_price),
                sell_min_price=float(args.sell_min_price),
                sell_max_price=float(args.sell_max_price),
                sell_slippage_pct=main_sell_slippage_pct,
                fee_enabled=main_fee_enabled,
                fee_rate=main_fee_rate,
                fee_exponent=main_fee_exponent,
                anti_amplification_guard_enabled=bool(args.anti_amplification_guard),
                max_our_vs_leader_per_trade=float(args.max_our_vs_leader_per_trade),
                max_our_vs_leader_per_market=float(args.max_our_vs_leader_per_market),
            )
            results = build_results_with_scaled_metrics(
                states,
                price_map=price_map,
                leader_buy_signal_total_usd=leader_buy_signal_total_usd,
                actual_window_pnl_delta=actual_window_pnl_delta,
            )
            pool = _select_optimizer_pool(
                results,
                min_capital_ratio=float(args.opt_min_capital_ratio),
                min_copied_buys=max(0, int(args.opt_min_copied_buys)),
                roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
            )
            optimizer_rows = pool["rows"] if isinstance(pool.get("rows"), list) else []
            winner = optimizer_rows[0] if optimizer_rows else (results[0] if results else {})

            improved = False
            if best_winner_row is None:
                best_winner_row = dict(winner)
                improved = True
                no_improve_rounds = 0
            else:
                cmp = _compare_optimizer_rows(
                    winner,
                    best_winner_row,
                    roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
                )
                if cmp < 0:
                    best_winner_row = dict(winner)
                    improved = True
                    no_improve_rounds = 0
                else:
                    no_improve_rounds += 1

            expansion = propose_strategy_space_expansion(
                optimizer_rows=optimizer_rows if optimizer_rows else results,
                winner_row=winner,
                fixed_usd_options=current_space["fixed_usd_options"],
                proportional_pct_options=current_space["proportional_pct_options"],
                proportional_cap_usd_options=current_space["proportional_cap_usd_options"],
                max_entry_times=current_space["max_entry_times"],
                actual_window_pnl_delta=actual_window_pnl_delta,
                roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
            )

            elapsed_minutes = (time.time() - optimization_start) / 60.0
            expanded = False
            added_strategies = 0
            stop_reason = ""

            if not args.auto_optimize:
                stop_reason = "auto_optimize_disabled"
            elif round_idx >= max_rounds:
                stop_reason = "round_limit_reached"
            elif elapsed_minutes >= float(args.opt_budget_minutes):
                stop_reason = "time_budget_reached"
            elif no_improve_rounds >= 2:
                stop_reason = "no_improvement_two_rounds"
            elif not bool(expansion.get("expanded")):
                stop_reason = "no_expansion_signal"
            else:
                new_space = expansion.get("new_space") if isinstance(expansion.get("new_space"), dict) else {}
                new_strategies = generate_strategies(
                    fixed_usd_options=list(new_space.get("fixed_usd_options") or current_space["fixed_usd_options"]),
                    proportional_pct_options=list(
                        new_space.get("proportional_pct_options") or current_space["proportional_pct_options"]
                    ),
                    proportional_cap_usd_options=list(
                        new_space.get("proportional_cap_usd_options") or current_space["proportional_cap_usd_options"]
                    ),
                    max_entries=int(new_space.get("max_entry_times") or current_space["max_entry_times"]),
                )
                for strategy in new_strategies:
                    if strategy.name in strategy_map:
                        continue
                    strategy_map[strategy.name] = strategy
                    added_strategies += 1

                if added_strategies > 0:
                    current_space = {
                        "fixed_usd_options": sorted(set(float(v) for v in (new_space.get("fixed_usd_options") or []))),
                        "proportional_pct_options": sorted(
                            set(float(v) for v in (new_space.get("proportional_pct_options") or []))
                        ),
                        "proportional_cap_usd_options": sorted(
                            set(float(v) for v in (new_space.get("proportional_cap_usd_options") or []))
                        ),
                        "max_entry_times": int(new_space.get("max_entry_times") or current_space["max_entry_times"]),
                    }
                    expanded = True
                    expansion_rounds += 1
                else:
                    stop_reason = "no_new_strategies"

            optimization_rounds.append(
                {
                    "round": round_idx,
                    "strategies_evaluated": len(strategy_list),
                    "optimizer_pool_mode": pool.get("mode"),
                    "optimizer_pool_size": len(optimizer_rows),
                    "used_min_capital_ratio": pool.get("used_min_capital_ratio"),
                    "used_min_copied_buys": pool.get("used_min_copied_buys"),
                    "winner_strategy": winner.get("strategy"),
                    "winner_roi": as_float(winner.get("roi")),
                    "winner_total_pnl": as_float(winner.get("total_pnl")),
                    "winner_capture_rate": as_float(winner.get("capture_rate")),
                    "improved": improved,
                    "expanded": expanded,
                    "added_strategies": added_strategies,
                    "boundary": expansion.get("boundary_pressure"),
                    "changes": expansion.get("changes"),
                    "elapsed_s": round(time.time() - round_t0, 3),
                    "stop_reason": stop_reason if not expanded else None,
                }
            )

            final_states = states
            final_results = results
            print(
                "[opt] "
                f"round={round_idx} evaluated={len(strategy_list)} "
                f"pool={pool.get('mode')}({len(optimizer_rows)}) "
                f"winner_roi={format_ratio_pct(winner.get('roi'))} "
                f"expanded={expanded} added={added_strategies} "
                f"elapsed={time.time() - round_t0:.2f}s"
            )
            if not expanded:
                break

        ai_improvement_summary: Dict[str, Any] = {
            "enabled": bool(args.ai_execute_improve),
            "objective": "roi_then_scale",
            "roi_tie_threshold": float(args.optimizer_roi_tie_threshold),
            "bound_profile": str(args.ai_improve_bound_profile),
            "bounds": get_ai_improve_bounds(str(args.ai_improve_bound_profile)),
            "budget_minutes": float(args.ai_improve_budget_minutes),
            "rounds_requested": int(max(1, int(args.ai_improve_rounds))),
            "rounds_executed": 0,
            "improved_rounds": 0,
            "top_candidates_per_round": max(1, int(args.ai_improve_top_candidates)),
            "executed_experiments": [],
            "stop_reason": "disabled",
            "final_winner": None,
        }
        if bool(args.ai_execute_improve):
            t_ai = time.time()
            ai_improvement_summary = run_ai_execute_improvement_loop(
                replay_events=replay_events,
                strategy_map=strategy_map,
                base_results=final_results,
                price_map=price_map,
                leader_buy_signal_total_usd=leader_buy_signal_total_usd,
                actual_window_pnl_delta=actual_window_pnl_delta,
                buy_price_premium_pct=main_buy_premium_pct,
                buy_min_price=float(args.buy_min_price),
                buy_max_price=float(args.buy_max_price),
                sell_min_price=float(args.sell_min_price),
                sell_max_price=float(args.sell_max_price),
                sell_slippage_pct=main_sell_slippage_pct,
                fee_enabled=main_fee_enabled,
                fee_rate=main_fee_rate,
                fee_exponent=main_fee_exponent,
                anti_amplification_guard_enabled=bool(args.anti_amplification_guard),
                max_our_vs_leader_per_trade=float(args.max_our_vs_leader_per_trade),
                max_our_vs_leader_per_market=float(args.max_our_vs_leader_per_market),
                min_capital_ratio=float(args.opt_min_capital_ratio),
                min_copied_buys=max(0, int(args.opt_min_copied_buys)),
                roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
                rounds=max(1, int(args.ai_improve_rounds)),
                budget_minutes=float(args.ai_improve_budget_minutes),
                bound_profile=str(args.ai_improve_bound_profile),
                top_candidates_per_round=max(1, int(args.ai_improve_top_candidates)),
            )
            print(
                "[ai-improve] "
                f"rounds={ai_improvement_summary.get('rounds_executed', 0)} "
                f"improved_rounds={ai_improvement_summary.get('improved_rounds', 0)} "
                f"stop={ai_improvement_summary.get('stop_reason')} "
                f"elapsed={time.time() - t_ai:.2f}s"
            )

        final_strategy_list = list(strategy_map.values())
        print(f"[sim] final strategy count={len(final_strategy_list)}")
        t_main_replay = time.time()
        final_states = run_simulation(
            replay_events,
            final_strategy_list,
            buy_price_premium_pct=main_buy_premium_pct,
            buy_min_price=float(args.buy_min_price),
            buy_max_price=float(args.buy_max_price),
            sell_min_price=float(args.sell_min_price),
            sell_max_price=float(args.sell_max_price),
            sell_slippage_pct=main_sell_slippage_pct,
            fee_enabled=main_fee_enabled,
            fee_rate=main_fee_rate,
            fee_exponent=main_fee_exponent,
            anti_amplification_guard_enabled=bool(args.anti_amplification_guard),
            max_our_vs_leader_per_trade=float(args.max_our_vs_leader_per_trade),
            max_our_vs_leader_per_market=float(args.max_our_vs_leader_per_market),
        )
        final_results = build_results_with_scaled_metrics(
            final_states,
            price_map=price_map,
            leader_buy_signal_total_usd=leader_buy_signal_total_usd,
            actual_window_pnl_delta=actual_window_pnl_delta,
        )
        print(f"[sim] replay done main scenario elapsed={time.time() - t_main_replay:.2f}s")

        # --- Passive follow probe: auto-trigger when best standard ROI < 0 ---
        passive_probe_result: Optional[Dict[str, Any]] = None
        best_standard_roi_val = as_float((best_winner_row or {}).get("roi"))
        if best_standard_roi_val is not None and best_standard_roi_val < 0:
            print(f"[passive-probe] triggered: best standard ROI={best_standard_roi_val*100:.2f}% < 0")
            t_passive = time.time()
            try:
                passive_probe_result = run_passive_follow_probe(
                    raw_events=events,
                    price_map=price_map,
                    best_standard_roi=best_standard_roi_val,
                )
                rec = passive_probe_result.get("recommendation", "unknown")
                be = passive_probe_result.get("breakeven", {})
                print(
                    f"[passive-probe] done: recommendation={rec}, "
                    f"breakeven_caps={len(be)}, elapsed={time.time() - t_passive:.1f}s"
                )
                if be:
                    for cap_name, info in sorted(be.items()):
                        print(f"  {cap_name}: fill>={info['min_fill_rate']:.0%} → ROI={info['roi']*100:+.2f}%")
            except Exception as exc:  # noqa: BLE001
                print(f"[passive-probe] failed: {exc}")
                passive_probe_result = {"triggered": True, "error": str(exc), "recommendation": "error"}

        window_records: List[Dict[str, Any]] = []
        full_activity_range = f"1~{len(events)}"
        window_records.append(
            _build_window_record(
                window_id="full",
                title="全量窗口",
                activity_range=full_activity_range,
                count=len(events),
                start_utc=tracked_first_trade_utc,
                end_utc=tracked_last_trade_utc,
                rows=final_results,
                roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
                top_n=WINDOW_REPORT_TOP_N,
                actual_window_pnl_delta=actual_window_pnl_delta,
                leader_buy_signal_total_usd=leader_buy_signal_total_usd,
            )
        )

        split_windows = split_activity_windows(events, window_count=WINDOW_SPLIT_COUNT)
        if split_windows:
            print(
                f"[window] split by deduped trade activities into {len(split_windows)} windows "
                f"(top_n={WINDOW_REPORT_TOP_N}, strategies={len(default_strategy_list)})"
            )
        for win in split_windows:
            win_idx = as_int(win.get("index")) or (len(window_records))
            win_events = win.get("events") if isinstance(win.get("events"), list) else []
            if not win_events:
                continue
            win_t0 = time.time()
            win_replay = build_replay_events_with_maker_like(win_events)
            win_buy_signal_stats = summarize_buy_signal_stats(win_replay)
            win_leader_buy_signal_total_usd = as_float(win_buy_signal_stats.get("buy_signal_total_usd"))
            win_states = run_simulation(
                win_replay,
                default_strategy_list,
                buy_price_premium_pct=main_buy_premium_pct,
                buy_min_price=float(args.buy_min_price),
                buy_max_price=float(args.buy_max_price),
                sell_min_price=float(args.sell_min_price),
                sell_max_price=float(args.sell_max_price),
                sell_slippage_pct=main_sell_slippage_pct,
                fee_enabled=main_fee_enabled,
                fee_rate=main_fee_rate,
                fee_exponent=main_fee_exponent,
                anti_amplification_guard_enabled=bool(args.anti_amplification_guard),
                max_our_vs_leader_per_trade=float(args.max_our_vs_leader_per_trade),
                max_our_vs_leader_per_market=float(args.max_our_vs_leader_per_market),
            )
            win_rows = build_results_with_scaled_metrics(
                win_states,
                price_map=price_map,
                leader_buy_signal_total_usd=win_leader_buy_signal_total_usd,
                actual_window_pnl_delta=None,
            )
            activity_range = f"{as_int(win.get('start_pos')) or 1}~{as_int(win.get('end_pos')) or len(win_events)}"
            window_records.append(
                _build_window_record(
                    window_id=f"slice_{win_idx}",
                    title=f"切片{win_idx}",
                    activity_range=activity_range,
                    count=int(len(win_events)),
                    start_utc=str(win.get("start_utc") or ""),
                    end_utc=str(win.get("end_utc") or ""),
                    rows=win_rows,
                    roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
                    top_n=WINDOW_REPORT_TOP_N,
                    actual_window_pnl_delta=None,
                    leader_buy_signal_total_usd=win_leader_buy_signal_total_usd,
                )
            )
            print(
                f"[window] done slice_{win_idx} range={activity_range} "
                f"events={len(win_events)} replay={len(win_replay)} elapsed={time.time() - win_t0:.2f}s"
            )

        open_token_ids: set = set()
        for state in final_states:
            for token_id, pos in state.positions.items():
                if pos.size > 1e-12:
                    open_token_ids.add(token_id)

        final_state_by_strategy = {state.strategy.name: state for state in final_states}
        objective_sorted = sort_optimizer_rows(
            final_results,
            roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
        )
        objective_best_row = objective_sorted[0] if objective_sorted else (final_results[0] if final_results else {})
        raw_roi_best_row = sorted(
            [row for row in final_results if as_float(row.get("roi")) is not None],
            key=strategy_roi_sort_key,
            reverse=True,
        )
        raw_roi_best_row = raw_roi_best_row[0] if raw_roi_best_row else objective_best_row
        best_total_pnl_row = sort_results_for_report(final_results)[0] if final_results else objective_best_row

        resolved_count = sum(1 for v in price_map.values() if v.resolved and v.price is not None)
        midpoint_count = sum(1 for v in price_map.values() if (not v.resolved) and v.price is not None)
        missing_count = sum(1 for v in price_map.values() if v.price is None)
        final_fixed_options = sorted(
            {
                float(strategy.fixed_usd)
                for strategy in final_strategy_list
                if strategy.copy_mode == "fixed_usd" and strategy.fixed_usd is not None
            }
        )
        final_pct_options = sorted(
            {
                float(strategy.proportional_pct)
                for strategy in final_strategy_list
                if strategy.copy_mode == "proportional" and strategy.proportional_pct > 0
            }
        )
        final_cap_options = sorted(
            {
                float(strategy.proportional_cap_usd)
                for strategy in final_strategy_list
                if strategy.copy_mode == "proportional" and strategy.proportional_cap_usd is not None
            }
        )
        final_max_entries = max(
            (int(strategy.max_entries_per_market) for strategy in final_strategy_list),
            default=max(1, int(args.max_entry_times)),
        )
        leader_market_signal_counts = summarize_leader_buy_signal_counts(replay_events)
        entries_depth_evidence = build_entries_depth_evidence(
            results=final_results,
            avg_bets_per_market=as_float(buy_signal_stats.get("avg_bets_per_market")),
            market_bet_distribution=(
                buy_signal_stats.get("market_bet_count_distribution")
                if isinstance(buy_signal_stats.get("market_bet_count_distribution"), dict)
                else None
            ),
            roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
            states_by_strategy=final_state_by_strategy,
            price_map=price_map,
            leader_market_signal_counts=leader_market_signal_counts,
            objective_row=objective_best_row,
        )
        why_entries_gt_avg = (
            entries_depth_evidence.get("why_entries_gt_avg")
            if isinstance(entries_depth_evidence.get("why_entries_gt_avg"), dict)
            else {}
        )
        amplification_guard_summary = build_amplification_guard_summary(
            results=final_results,
            guard_enabled=bool(args.anti_amplification_guard),
            per_trade_limit=float(args.max_our_vs_leader_per_trade),
            per_market_limit=float(args.max_our_vs_leader_per_market),
            objective_best_row=objective_best_row,
        )
        oversize_event_rate = {
            "objective_before_guard": as_float(objective_best_row.get("oversize_event_rate_before_guard")),
            "objective_after_guard": as_float(objective_best_row.get("oversize_event_rate_after_guard")),
            "aggregate_before_guard": as_float(
                ((amplification_guard_summary.get("aggregate") or {}).get("oversize_event_rate_before_guard"))
            ),
            "aggregate_after_guard": as_float(
                ((amplification_guard_summary.get("aggregate") or {}).get("oversize_event_rate_after_guard"))
            ),
        }

        meta = {
            "address": address,
            "max_activities": int(args.max_activities),
            "fetched_events": len(events),
            "replay_events": len(replay_events),
            "tracked_first_trade_ts": tracked_first_trade_ts,
            "tracked_last_trade_ts": tracked_last_trade_ts,
            "tracked_first_trade_utc": tracked_first_trade_utc,
            "tracked_last_trade_utc": tracked_last_trade_utc,
            "tracked_span_days": tracked_span_days,
            "strategies": len(final_strategy_list),
            "fixed_usd_options": final_fixed_options,
            "proportional_cap_usd_options": final_cap_options,
            "proportional_pct_options": final_pct_options,
            "max_entry_times": int(final_max_entries),
            "buy_price_premium_pct": main_buy_premium_pct,
            "mirror_sell_slippage_pct": main_sell_slippage_pct,
            "fee_config": {
                "enabled": main_fee_enabled,
                "fee_rate": main_fee_rate,
                "fee_exponent": main_fee_exponent,
                "formula": "fee = C × p × feeRate × (p × (1 - p))^exponent",
                "buy_side_fee_mode": "shares",
                "sell_side_fee_mode": "usdc",
            },
            "actual_window_pnl_delta": actual_window_pnl_delta,
            "benchmark": benchmark,
            "leader_buy_signal_total_usd": leader_buy_signal_total_usd,
            "buy_price_limits": [float(args.buy_min_price), float(args.buy_max_price)],
            "sell_price_limits": [float(args.sell_min_price), float(args.sell_max_price)],
            "maker_like_params": {
                "min_trade_size_usd": MAKER_LIKE_MIN_TRADE_SIZE_USD,
                "window_minutes": MAKER_LIKE_WINDOW_S // 60,
                "max_gap_minutes": MAKER_LIKE_MAX_GAP_S // 60,
                "score_threshold": MAKER_LIKE_SCORE_THRESHOLD,
            },
            "buy_signal_stats": {
                "buy_signal_count": int(buy_signal_stats.get("buy_signal_count", 0) or 0),
                "aggregated_buy_signal_count": int(
                    buy_signal_stats.get("aggregated_buy_signal_count", 0) or 0
                ),
                "buy_signal_market_count": int(
                    buy_signal_stats.get("buy_signal_market_count", 0) or 0
                ),
                "buy_signal_total_usd": round(float(buy_signal_stats.get("buy_signal_total_usd", 0.0) or 0.0), 6),
                "avg_bets_per_market": buy_signal_stats.get("avg_bets_per_market"),
                "avg_usd_per_market": buy_signal_stats.get("avg_usd_per_market"),
                "avg_usd_per_bet": buy_signal_stats.get("avg_usd_per_bet"),
                "market_bet_count_distribution": (
                    buy_signal_stats.get("market_bet_count_distribution")
                    if isinstance(buy_signal_stats.get("market_bet_count_distribution"), dict)
                    else {}
                ),
            },
            "avg_bets_per_market": buy_signal_stats.get("avg_bets_per_market"),
            "avg_usd_per_market": buy_signal_stats.get("avg_usd_per_market"),
            "avg_usd_per_bet": buy_signal_stats.get("avg_usd_per_bet"),
            "entries_depth_evidence": entries_depth_evidence,
            "why_entries_gt_avg": why_entries_gt_avg,
            "best_by_objective": _result_row_brief(objective_best_row),
            "best_by_raw_roi": _result_row_brief(raw_roi_best_row),
            "best_by_total_pnl": _result_row_brief(best_total_pnl_row),
            "amplification_guard_summary": amplification_guard_summary,
            "oversize_event_rate": oversize_event_rate,
            "price_coverage": {
                "resolved": resolved_count,
                "midpoint": midpoint_count,
                "missing": missing_count,
                "total_open_tokens": len(open_token_ids),
                "total_priced_tokens": len(price_map),
            },
            "optimization_summary": {
                "enabled": bool(args.auto_optimize),
                "objective": "roi_then_scale",
                "roi_tie_threshold": float(args.optimizer_roi_tie_threshold),
                "guardrails": {
                    "min_capital_ratio": float(args.opt_min_capital_ratio),
                    "min_copied_buys": int(args.opt_min_copied_buys),
                },
                "amplification_guard": {
                    "enabled": bool(args.anti_amplification_guard),
                    "max_our_vs_leader_per_trade": float(args.max_our_vs_leader_per_trade),
                    "max_our_vs_leader_per_market": float(args.max_our_vs_leader_per_market),
                },
                "budget_minutes": float(args.opt_budget_minutes),
                "round_limit": int(max_rounds),
                "rounds_executed": len(optimization_rounds),
                "expansion_rounds": int(expansion_rounds),
                "rounds": optimization_rounds,
            },
            "ai_improvement_summary": ai_improvement_summary,
            "window_analysis": {
                "window_count": WINDOW_SPLIT_COUNT,
                "split_basis": "deduped_trade_activities",
                "top_n": WINDOW_REPORT_TOP_N,
                "windows": window_records,
            },
            "report_sections": [],
            "passive_follow_probe": passive_probe_result,
        }

        final_results = sort_results_for_report(apply_scaled_metrics_to_results(final_results, meta=meta))
        if window_records:
            window_records[0] = _build_window_record(
                window_id="full",
                title="全量窗口",
                activity_range=full_activity_range,
                count=len(events),
                start_utc=tracked_first_trade_utc,
                end_utc=tracked_last_trade_utc,
                rows=final_results,
                roi_tie_threshold=float(args.optimizer_roi_tie_threshold),
                top_n=WINDOW_REPORT_TOP_N,
                actual_window_pnl_delta=actual_window_pnl_delta,
                leader_buy_signal_total_usd=leader_buy_signal_total_usd,
            )
            meta["window_analysis"]["windows"] = window_records

        json_path, csv_path = write_outputs(out_dir, address, meta, final_results)
        report_pdf_path = resolve_report_pdf_path(
            report_pdf_arg=args.report_pdf or None,
            out_dir=out_dir,
            meta=meta,
        )
        write_pdf_report(
            report_pdf_path,
            meta,
            final_results,
            report_top=max(1, int(args.report_top)),
        )

        ai_report_md_path: Optional[Path] = None
        ai_report_json_path: Optional[Path] = None
        if bool(args.ai_report):
            try:
                try:
                    from sim_copytrade.ai_report import generate_ai_reports  # type: ignore
                except Exception:
                    from ai_report import generate_ai_reports  # type: ignore

                ai_out = generate_ai_reports(
                    sim_json_path=json_path,
                    out_md=Path(args.ai_report_path).expanduser() if str(args.ai_report_path or "").strip() else None,
                    out_json=None,
                    gap_json_path=None,
                    language="zh-CN",
                )
                md_raw = ai_out.get("md_path")
                json_raw = ai_out.get("json_path")
                if isinstance(md_raw, str) and md_raw.strip():
                    ai_report_md_path = Path(md_raw)
                if isinstance(json_raw, str) and json_raw.strip():
                    ai_report_json_path = Path(json_raw)
                print(
                    "[ai-report] "
                    f"status={ai_out.get('status')} provider={ai_out.get('provider')} "
                    f"model={ai_out.get('model')}"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[ai-report] failed: {exc}")

        best = pick_objective_best_for_display(final_results, meta=meta) or final_results[0]
        print_top_results(final_results, top_n=max(1, int(args.top)))
        print("\n=== Output ===")
        print(f"json: {json_path}")
        print(f"csv : {csv_path}")
        print(f"pdf : {report_pdf_path}")
        if ai_report_md_path is not None:
            print(f"ai-md: {ai_report_md_path}")
        if ai_report_json_path is not None:
            print(f"ai-json: {ai_report_json_path}")
        print(
            "best(by ROI+scale): "
            f"{strategy_name_cn(best)} | total_pnl={format_money(best.get('total_pnl'))} "
            f"| roi={format_ratio_pct(best.get('roi'))} "
            f"| cost={format_money(best.get('total_buy_cost'))} "
            f"| capture={_html_format_metric('capture_rate', best.get('capture_rate'))}"
        )
        if passive_probe_result and passive_probe_result.get("recommendation") == "passive_follow_viable":
            be = passive_probe_result.get("breakeven", {})
            best_cap = min(be.items(), key=lambda x: x[1].get("min_fill_rate", 1.0)) if be else None
            if best_cap:
                print(
                    f"passive-follow-probe: VIABLE — {best_cap[0]} "
                    f"breakeven fill>={best_cap[1]['min_fill_rate']:.0%}, "
                    f"ROI={best_cap[1]['roi']*100:+.2f}%"
                )
        elif passive_probe_result and passive_probe_result.get("triggered"):
            print(f"passive-follow-probe: {passive_probe_result.get('recommendation', 'N/A')}")
        print(f"total elapsed: {time.time() - t0:.2f}s")
        return 0
    finally:
        try:
            shutil.rmtree(run_cache_dir, ignore_errors=True)
            print(f"[run-cache] cleaned {run_cache_dir}")
        except Exception as exc:  # noqa: BLE001
            print(f"[run-cache] cleanup failed: {exc}")


def main() -> int:
    args = parse_args()
    report_mode = str(args.report_mode).lower()
    out_dir_raw = str(args.out_dir or "").strip()
    if out_dir_raw:
        out_dir = Path(out_dir_raw)
    else:
        out_dir = Path(__file__).resolve().parent / "output"

    if report_mode == "live":
        return run_live_mode(args, out_dir)

    report_input = str(args.report_input or "").strip()
    if not report_input:
        raise SystemExit("--report-input is required when --report-mode is json or csv")

    report_input_path = Path(report_input)
    if not report_input_path.exists():
        raise SystemExit(f"report input file not found: {report_input_path}")

    t0 = time.time()
    print("=== Report Generator Start ===")
    print(f"mode={report_mode}")
    print(f"input={report_input_path}")

    if report_mode == "json":
        meta, results = load_results_from_json(report_input_path)
    else:
        meta_json_path: Optional[Path] = None
        if args.report_meta_json:
            meta_json_path = Path(args.report_meta_json)
            if not meta_json_path.exists():
                raise SystemExit(f"report meta json file not found: {meta_json_path}")
        meta, results = load_results_from_csv(report_input_path, meta_json_path=meta_json_path)

    results = apply_scaled_metrics_to_results(results, meta=meta)
    results = sort_results_for_report(results)
    if "best_by_objective" not in meta:
        objective_best = pick_best_row_by_objective(
            results,
            roi_tie_threshold=roi_tie_threshold_from_meta(meta),
        )
        meta["best_by_objective"] = _result_row_brief(objective_best)
    if "best_by_raw_roi" not in meta:
        meta["best_by_raw_roi"] = _result_row_brief(pick_best_row_by_raw_roi(results))
    if "best_by_total_pnl" not in meta:
        meta["best_by_total_pnl"] = _result_row_brief(pick_best_row_by_total_pnl(results))

    report_pdf_path = resolve_report_pdf_path(
        report_pdf_arg=args.report_pdf or None,
        out_dir=out_dir,
        meta=meta,
    )
    write_pdf_report(
        report_pdf_path,
        meta,
        results,
        report_top=max(1, int(args.report_top)),
        report_sections=meta.get("report_sections") if isinstance(meta.get("report_sections"), list) else None,
    )

    best = pick_objective_best_for_display(results, meta=meta) or results[0]
    print_top_results(results, top_n=max(1, int(args.top)))
    print("\n=== Output ===")
    print(f"pdf : {report_pdf_path}")
    print(
        "best(by ROI+scale): "
        f"{strategy_name_cn(best)} | total_pnl={format_money(best.get('total_pnl'))} "
        f"| roi={format_ratio_pct(best.get('roi'))} "
        f"| cost={format_money(best.get('total_buy_cost'))}"
    )
    print(f"total elapsed: {time.time() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
