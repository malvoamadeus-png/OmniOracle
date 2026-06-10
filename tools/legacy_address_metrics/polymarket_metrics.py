import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"
USER_PNL_METRICS_INTERVAL = "all"
USER_PNL_METRICS_FIDELITY = "12h"
USER_PNL_METRICS_COMPAT_VERSION = "user_pnl_curve_all_12h_v1"


class _RateLimiter:
    def __init__(self, qps: float):
        self.qps = float(qps)
        self._next_ts = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.qps <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_s = self._next_ts - now
            if wait_s > 0:
                time.sleep(wait_s)
                now = time.monotonic()
            step = 1.0 / self.qps
            self._next_ts = max(self._next_ts, now) + step


_LIMITERS: Dict[str, _RateLimiter] = {
    "gamma": _RateLimiter(10.0),
    "data": _RateLimiter(10.0),
    "clob": _RateLimiter(10.0),
    "user_pnl": _RateLimiter(10.0),
}

_PROGRESS = False


def _progress(msg: str) -> None:
    if not _PROGRESS:
        return
    try:
        sys.stderr.write(str(msg).rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _limit_for_url(url: str) -> Optional[_RateLimiter]:
    if url.startswith(GAMMA_API):
        return _LIMITERS["gamma"]
    if url.startswith(DATA_API):
        return _LIMITERS["data"]
    if url.startswith(CLOB_API):
        return _LIMITERS["clob"]
    if url.startswith(USER_PNL_API):
        return _LIMITERS["user_pnl"]
    return None


@dataclass(frozen=True)
class Config:
    address: str
    db_path: str
    min_usd: float
    price_history_days: int
    mdd_mode: str
    debug: bool
    progress: bool
    source_tag: str


PNL_SKIP_THRESHOLD = 80000.0
RESOLUTION_EPSILON = 0.05
MAX_OPEN_POSITIONS_ROWS = 100_000
MAX_CLOSED_POSITIONS_ROWS = 7500
DEFAULT_CLOSED_PAGE_LIMIT = 50
CLOSED_FETCH_MAX_WORKERS = 8
CLOSED_FETCH_BATCH_PAGES = 8
CLOSED_INCREMENTAL_OVERLAP_ROWS = 1000
CLOSED_FULL_RESYNC_DAYS = 7
IGNORED_SOURCE_TAGS = {"BACKFILL"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def _sleep_backoff(attempt: int, base_s: float) -> None:
    time.sleep(base_s * (2**attempt))


def http_get_json(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout_s: float = 25.0,
    max_retries: int = 3,
) -> Any:
    last_err: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            lim = _limit_for_url(url)
            if lim is not None:
                lim.acquire()
            r = session.get(url, params=params, timeout=timeout_s, headers={"accept": "application/json"})
            if r.status_code in (429, 500, 502, 503, 504):
                _sleep_backoff(attempt, 1.0)
                continue
            if 400 <= r.status_code < 500:
                raise requests.HTTPError(f"{r.status_code} {r.text[:500]}", response=r)
            r.raise_for_status()
            return r.json()
        except BaseException as e:
            last_err = e
            if isinstance(e, requests.HTTPError) and e.response is not None:
                code = e.response.status_code
                if code in (400, 401, 403, 404):
                    raise
            _sleep_backoff(attempt, 1.0)
    raise RuntimeError(f"GET {url} failed after retries: {last_err}")


def fetch_user_pnl_series(
    session: requests.Session,
    address: str,
    interval: str = USER_PNL_METRICS_INTERVAL,
    fidelity: str = USER_PNL_METRICS_FIDELITY,
) -> List[Tuple[str, float]]:
    data = http_get_json(
        session,
        f"{USER_PNL_API}/user-pnl",
        params={"user_address": address, "interval": interval, "fidelity": fidelity},
        timeout_s=20.0,
        max_retries=3,
    )
    if not isinstance(data, list):
        return []
    out: List[Tuple[str, float]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        p = row.get("p")
        if not isinstance(t, (int, float)) or not isinstance(p, (int, float)):
            continue
        dt = datetime.fromtimestamp(float(t), tz=timezone.utc)
        out.append((dt.isoformat(), float(p)))
    out.sort(key=lambda item: item[0])
    return out



def fetch_positions(session: requests.Session, address: str) -> List[Dict[str, Any]]:
    url = f"{DATA_API}/positions"
    limit = 500
    offset = 0
    out: List[Dict[str, Any]] = []
    while True:
        if len(out) >= MAX_OPEN_POSITIONS_ROWS:
            _progress(f"    ↳ positions reached cap={MAX_OPEN_POSITIONS_ROWS}, stop paging")
            break
        _progress(f"    ↳ positions page offset={offset}")
        data = http_get_json(session, url, params={"user": address, "sizeThreshold": 0, "limit": limit, "offset": offset})
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if isinstance(row, dict):
                out.append(row)
                if len(out) >= MAX_OPEN_POSITIONS_ROWS:
                    break
        if len(data) < limit:
            break
        if len(out) >= MAX_OPEN_POSITIONS_ROWS:
            _progress(f"    ↳ positions reached cap={MAX_OPEN_POSITIONS_ROWS}, stop paging")
            break
        offset += limit
    return out


def fetch_closed_positions(
    session: requests.Session,
    address: str,
    *,
    limit: int = DEFAULT_CLOSED_PAGE_LIMIT,
    start_offset: int = 0,
    sort_by: str = "TIMESTAMP",
    sort_direction: str = "ASC",
) -> List[Dict[str, Any]]:
    url = f"{DATA_API}/closed-positions"
    page_limit = max(1, min(50, int(limit)))
    offset = max(0, int(start_offset))
    batch_pages = max(1, int(CLOSED_FETCH_BATCH_PAGES))
    max_workers = max(1, int(CLOSED_FETCH_MAX_WORKERS))
    out: List[Dict[str, Any]] = []

    def _fetch_one_page(page_offset: int) -> List[Dict[str, Any]]:
        data = http_get_json(
            session,
            url,
            params={
                "user": address,
                "limit": page_limit,
                "offset": int(page_offset),
                "sortBy": str(sort_by),
                "sortDirection": str(sort_direction),
            },
        )
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while True:
            batch_offsets = [offset + i * page_limit for i in range(batch_pages)]
            _progress(f"    ↳ closed-positions batch offsets={batch_offsets[0]}..{batch_offsets[-1]}")
            futures = {off: pool.submit(_fetch_one_page, off) for off in batch_offsets}
            should_stop = False
            for off in batch_offsets:
                rows = futures[off].result()
                _progress(f"      · page offset={off} rows={len(rows)}")
                if not rows:
                    should_stop = True
                    break
                out.extend(rows)
                if len(out) >= MAX_CLOSED_POSITIONS_ROWS:
                    out = out[:MAX_CLOSED_POSITIONS_ROWS]
                    should_stop = True
                    break
                if len(rows) < page_limit:
                    should_stop = True
                    break
            if should_stop:
                break
            offset = batch_offsets[-1] + page_limit
    return out


def fetch_total_value(session: requests.Session, address: str) -> Optional[float]:
    data = http_get_json(session, f"{DATA_API}/value", params={"user": address}, timeout_s=15.0, max_retries=2)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        v = _as_float(data[0].get("value"))
        return float(v) if v is not None else None
    return None


def fetch_total_markets_traded(session: requests.Session, address: str) -> Optional[int]:
    data = http_get_json(session, f"{DATA_API}/traded", params={"user": address}, timeout_s=15.0, max_retries=2)
    if isinstance(data, dict):
        t = data.get("traded")
        if isinstance(t, int):
            return t
        if isinstance(t, str) and t.isdigit():
            return int(t)
    return None


def fetch_leaderboard_pnl(session: requests.Session, address: str, time_period: str) -> Optional[float]:
    data = http_get_json(
        session,
        f"{DATA_API}/v1/leaderboard",
        params={"user": address, "category": "OVERALL", "timePeriod": time_period, "orderBy": "PNL", "limit": 1, "offset": 0},
        timeout_s=15.0,
        max_retries=2,
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        pnl = _as_float(data[0].get("pnl"))
        return float(pnl) if pnl is not None else None
    return None


def try_midpoints_batch(session: requests.Session, token_ids: List[str]) -> Optional[Dict[str, float]]:
    if not token_ids:
        return {}
    url = f"{CLOB_API}/midpoints"
    params = {"token_ids": ",".join(token_ids)}
    try:
        data = http_get_json(session, url, params=params, timeout_s=20.0, max_retries=2)
    except BaseException:
        return None
    if isinstance(data, dict):
        out: Dict[str, float] = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out
    return None


def fetch_midpoint(session: requests.Session, token_id: str) -> Optional[float]:
    url = f"{CLOB_API}/midpoint"
    try:
        data = http_get_json(session, url, params={"token_id": token_id}, timeout_s=20.0, max_retries=2)
    except BaseException:
        return None
    if isinstance(data, dict):
        for k in ("mid", "midpoint", "price"):
            if k in data:
                try:
                    return float(data[k])
                except Exception:
                    return None
    return None


def fetch_orderbook(session: requests.Session, token_id: str) -> Optional[Dict[str, Any]]:
    if not token_id:
        return None
    try:
        data = http_get_json(
            session,
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            timeout_s=15.0,
            max_retries=2,
        )
    except BaseException:
        return None
    return data if isinstance(data, dict) else None


def fetch_market_by_slug(session: requests.Session, slug: str, cache: Dict[str, Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    key = str(slug or "").strip()
    if not key:
        return None
    if key in cache:
        return cache[key]

    out: Optional[Dict[str, Any]] = None
    try:
        data = http_get_json(
            session,
            f"{GAMMA_API}/markets",
            params={"slug": key, "limit": 1},
            timeout_s=15.0,
            max_retries=2,
        )
        if isinstance(data, list) and data and isinstance(data[0], dict):
            out = data[0]
    except BaseException:
        out = None

    if out is None:
        try:
            data = http_get_json(
                session,
                f"{GAMMA_API}/events",
                params={"slug": key, "limit": 1},
                timeout_s=15.0,
                max_retries=2,
            )
            if isinstance(data, list) and data and isinstance(data[0], dict):
                event = data[0]
                markets = event.get("markets")
                if isinstance(markets, list) and markets and isinstance(markets[0], dict):
                    out = markets[0]
                else:
                    out = event
        except BaseException:
            out = None

    cache[key] = out
    return out


def fetch_fee_rate_bps(session: requests.Session, token_id: str) -> Optional[float]:
    url = f"{CLOB_API}/fee-rate"
    variants = [
        (url, {"token_id": token_id}),
        (url, {"tokenId": token_id}),
        (f"{CLOB_API}/fee-rate/{token_id}", None),
    ]
    for u, params in variants:
        try:
            data = http_get_json(session, u, params=params, timeout_s=15.0, max_retries=2)
        except BaseException:
            continue
        if isinstance(data, (int, float)):
            return float(data)
        if isinstance(data, dict):
            for k in ("fee_rate_bps", "feeRateBps", "feeRate", "bps", "fee_rate"):
                v = _as_float(data.get(k))
                if v is not None:
                    return float(v)
    return None


def load_prices_for_tokens(session: requests.Session, token_ids: List[str]) -> Dict[str, float]:
    uniq = [t for t in dict.fromkeys(token_ids) if t]
    out: Dict[str, float] = {}
    batch = try_midpoints_batch(session, uniq)
    if batch is not None and batch:
        out.update(batch)
        missing = [t for t in uniq if t not in out]
    else:
        missing = uniq
    for t in missing:
        p = fetch_midpoint(session, t)
        if p is not None:
            out[t] = p
    return out


def _as_float(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except Exception:
            return None
    return None


def _order_level_usd(level: Any) -> Optional[float]:
    if not isinstance(level, dict):
        return None
    price = _as_float(level.get("price"))
    size = _as_float(level.get("size"))
    if price is None or size is None or price < 0 or size < 0:
        return None
    return float(price) * float(size)


def compute_orderbook_top5_depth_usd(book: Dict[str, Any]) -> Optional[Dict[str, float]]:
    bids_raw = book.get("bids")
    asks_raw = book.get("asks")
    bids = bids_raw if isinstance(bids_raw, list) else []
    asks = asks_raw if isinstance(asks_raw, list) else []
    bid_depth = sum(v for v in (_order_level_usd(level) for level in bids[:5]) if v is not None)
    ask_depth = sum(v for v in (_order_level_usd(level) for level in asks[:5]) if v is not None)
    if bid_depth <= 0 and ask_depth <= 0:
        return None
    return {
        "bid_top5_depth_usd": float(bid_depth),
        "ask_top5_depth_usd": float(ask_depth),
        "top5_depth_usd": float(bid_depth + ask_depth),
    }


def _parse_market_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            raw = float(value)
            if raw > 10_000_000_000:
                raw = raw / 1000.0
            return datetime.fromtimestamp(raw, tz=timezone.utc)
        except Exception:
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def market_days_to_settlement(info: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Optional[float]:
    if not isinstance(info, dict):
        return None
    base = now or utc_now()
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    fields = (
        "end_date_iso",
        "endDate",
        "end_date",
        "resolution_date",
        "resolutionDate",
        "closed_at",
        "closedAt",
        "game_start_time",
        "startDate",
        "start_date_iso",
    )
    for field in fields:
        dt = _parse_market_datetime(info.get(field))
        if dt is None:
            continue
        days = (dt - base).total_seconds() / 86400.0
        if days > 0:
            return days
    return None


def is_market_resolved(info: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get("closed") is True or info.get("resolved") is True:
        return True
    if info.get("active") is False:
        return True
    status = str(info.get("market_status") or info.get("status") or "").strip().lower()
    return status in {"resolved", "closed", "settled"}


def extract_trade_fields(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    side = row.get("side")
    if not isinstance(side, str):
        return None
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        return None

    tx = row.get("transaction_hash") or row.get("transactionHash") or row.get("txHash") or row.get("hash")
    ts = row.get("timestamp") or row.get("time") or row.get("createdAt") or row.get("ts")
    market = row.get("market") or row.get("conditionId") or row.get("condition_id")
    # Prefer the concrete market slug (for O/U, spreads, NRFI, etc.) over the parent event slug.
    slug = row.get("slug") or row.get("market_slug") or row.get("marketSlug") or row.get("eventSlug")
    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")

    usd = None
    for k in ("usdcSize", "amountUSD", "amountUsd", "usdc", "usd", "value", "amount"):
        usd = _as_float(row.get(k))
        if usd is not None:
            break

    price = None
    for k in ("price", "avgPrice", "avg_price"):
        price = _as_float(row.get(k))
        if price is not None:
            break

    size = None
    for k in ("size", "shares", "amount", "qty", "quantity"):
        size = _as_float(row.get(k))
        if size is not None:
            break

    outcome_index = row.get("outcome_index") if isinstance(row.get("outcome_index"), int) else row.get("outcomeIndex")
    if not isinstance(outcome_index, int):
        outcome_index = None

    return {
        "tx": str(tx) if tx else None,
        "ts": str(ts) if ts else None,
        "side": side,
        "usd": usd,
        "price": price,
        "size": size,
        "market": str(market) if market else None,
        "slug": str(slug) if slug else None,
        "token_id": str(token_id) if token_id else None,
        "outcome_index": outcome_index,
        "raw": row,
    }


def extract_position_fields(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")
    market = row.get("market") or row.get("conditionId") or row.get("condition_id")
    slug = row.get("slug") or row.get("market_slug") or row.get("eventSlug")
    outcome = row.get("outcome") or row.get("title") or row.get("name")

    size = None
    for k in ("size", "shares", "balance", "quantity", "qty"):
        size = _as_float(row.get(k))
        if size is not None:
            break

    total_bought = _as_float(row.get("totalBought") or row.get("total_bought"))
    initial_value = _as_float(row.get("initialValue") or row.get("initial_value"))
    current_value = _as_float(row.get("currentValue") or row.get("current_value"))
    avg_price = _as_float(row.get("avgPrice") or row.get("avg_price"))

    cost_basis = None
    for k in ("initialValue", "initial_value", "costBasis", "cost_basis", "avgCost", "avg_cost", "cost"):
        cost_basis = _as_float(row.get(k))
        if cost_basis is not None:
            break
    if cost_basis is None:
        if avg_price is not None and size is not None:
            cost_basis = abs(size) * avg_price

    cash_pnl = None
    for k in ("cashPnl", "cash_pnl", "pnl", "profit"):
        cash_pnl = _as_float(row.get(k))
        if cash_pnl is not None:
            break

    realized_pnl = _as_float(row.get("realizedPnl") or row.get("realized_pnl"))
    cur_price = _as_float(row.get("curPrice") or row.get("cur_price"))
    closed = bool(row.get("redeemed") or row.get("closed") or (row.get("redeemable") is True and row.get("size") in (0, "0", 0.0)))

    return {
        "market": str(market) if market else None,
        "slug": str(slug) if slug else None,
        "token_id": str(token_id) if token_id else None,
        "outcome": str(outcome) if isinstance(outcome, str) else None,
        "size": size,
        "total_bought": total_bought,
        "initial_value": initial_value,
        "current_value": current_value,
        "avg_price": avg_price,
        "cost_basis": cost_basis,
        "cash_pnl": cash_pnl,
        "realized_pnl": realized_pnl,
        "cur_price": cur_price,
        "closed": closed,
        "raw": row,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS address_metrics ("
        "address TEXT PRIMARY KEY,"
        "snapshot_utc TEXT NOT NULL,"
        "total_pnl REAL,"
        "realized_pnl REAL,"
        "unrealized_pnl REAL,"
        "profit_factor REAL,"
        "roi REAL,"
        "max_drawdown REAL,"
        "sharpe REAL,"
        "current_position_value_usd REAL,"
        "total_trades INTEGER,"
        "winning_trades INTEGER,"
        "losing_trades INTEGER,"
        "win_rate REAL,"
        "avg_trade_price REAL,"
        "realized_edge_score REAL,"
        "confidence TEXT,"
        "details_json TEXT NOT NULL,"
        "updated_at TEXT NOT NULL"
        ")"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(address_metrics)").fetchall()}
    alters = [
        ("current_position_value_usd", "REAL"),
        ("total_trades", "INTEGER"),
        ("winning_trades", "INTEGER"),
        ("losing_trades", "INTEGER"),
        ("win_rate", "REAL"),
        ("avg_trade_price", "REAL"),
        ("source_tags", "TEXT"),
        ("ulcer_index", "REAL"),
        ("equity_r2", "REAL"),
        ("realized_edge_score", "REAL"),
        ("avg_open_top5_depth_usd", "REAL"),
        ("avg_open_settlement_days", "REAL"),
        ("ct_score_total_100", "REAL"),
        ("ct_score_roi", "REAL"),
        ("ct_score_pf", "REAL"),
        ("ct_score_mdd", "REAL"),
        ("ct_score_sharpe", "REAL"),
        ("ct_score_ui", "REAL"),
        ("ct_score_r2", "REAL"),
        ("copytrade_value_score", "REAL"),
        ("copytrade_value_level", "TEXT"),
        ("copytrade_value_exclusion_reason", "TEXT"),
        ("copytrade_value_score_version", "TEXT"),
    ]
    for name, typ in alters:
        if name in cols:
            continue
        conn.execute(f"ALTER TABLE address_metrics ADD COLUMN {name} {typ}")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS pm_open_positions_cache ("
        "address TEXT NOT NULL,"
        "token_id TEXT NOT NULL,"
        "condition_id TEXT,"
        "market_slug TEXT,"
        "outcome TEXT,"
        "outcome_index INTEGER,"
        "avg_price REAL,"
        "cur_price REAL,"
        "total_bought REAL,"
        "cost_basis_usd REAL,"
        "cash_pnl REAL,"
        "realized_pnl REAL,"
        "size REAL,"
        "ts_epoch INTEGER,"
        "updated_at TEXT NOT NULL,"
        "PRIMARY KEY(address, token_id)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pm_closed_positions_cache ("
        "address TEXT NOT NULL,"
        "row_key TEXT NOT NULL,"
        "token_id TEXT,"
        "condition_id TEXT,"
        "market_slug TEXT,"
        "outcome TEXT,"
        "outcome_index INTEGER,"
        "avg_price REAL,"
        "cur_price REAL,"
        "total_bought REAL,"
        "cost_basis_usd REAL,"
        "realized_pnl REAL,"
        "size REAL,"
        "ts_epoch INTEGER,"
        "updated_at TEXT NOT NULL,"
        "PRIMARY KEY(address, row_key)"
        ")"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pm_closed_sync_state ("
        "address TEXT PRIMARY KEY,"
        "cached_rows INTEGER NOT NULL DEFAULT 0,"
        "last_sync_utc TEXT,"
        "last_full_sync_utc TEXT,"
        "updated_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_closed_address_ts "
        "ON pm_closed_positions_cache(address, ts_epoch)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pm_open_address_ts "
        "ON pm_open_positions_cache(address, ts_epoch)"
    )
    open_cols = {r[1] for r in conn.execute("PRAGMA table_info(pm_open_positions_cache)").fetchall()}
    if "cost_basis_usd" not in open_cols:
        conn.execute("ALTER TABLE pm_open_positions_cache ADD COLUMN cost_basis_usd REAL")
    closed_cols = {r[1] for r in conn.execute("PRAGMA table_info(pm_closed_positions_cache)").fetchall()}
    if "cost_basis_usd" not in closed_cols:
        conn.execute("ALTER TABLE pm_closed_positions_cache ADD COLUMN cost_basis_usd REAL")
    conn.commit()


def write_raw_trades(conn: sqlite3.Connection, run_id: int, trades: List[Dict[str, Any]]) -> None:
    return


def write_raw_positions(conn: sqlite3.Connection, run_id: int, positions: List[Dict[str, Any]]) -> None:
    return


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    v = sum((x - m) ** 2 for x in xs) / len(xs)
    return math.sqrt(v)


def parse_ts_to_dt(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        if s.isdigit():
            try:
                n = int(s)
                if n > 10_000_000_000:
                    n = int(n / 1000)
                return datetime.fromtimestamp(n, tz=timezone.utc)
            except Exception:
                return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def _position_ts_epoch(row: Dict[str, Any]) -> Optional[int]:
    for key in ("timestamp", "time", "createdAt", "updatedAt", "lastUpdated", "ts"):
        dt = parse_ts_to_dt(row.get(key))
        if dt is not None:
            return int(dt.timestamp())
    return None


def _to_position_cache_row(row: Dict[str, Any], *, is_closed: bool) -> Optional[Dict[str, Any]]:
    p = extract_position_fields(row)
    if p is None:
        return None
    token_id = p.get("token_id")
    if not isinstance(token_id, str) or not token_id:
        return None
    total_bought = p.get("total_bought")
    if not isinstance(total_bought, (int, float)):
        total_bought = p.get("initial_value")
    cost_basis_usd = p.get("cost_basis")
    if not isinstance(cost_basis_usd, (int, float)):
        avg_price = p.get("avg_price")
        size = p.get("size")
        if isinstance(avg_price, (int, float)) and isinstance(size, (int, float)):
            cost_basis_usd = abs(float(size)) * float(avg_price)
    ts_epoch = _position_ts_epoch(row)
    return {
        "token_id": token_id,
        "condition_id": p.get("market"),
        "market_slug": p.get("slug"),
        "outcome": p.get("outcome"),
        "outcome_index": row.get("outcomeIndex") if isinstance(row.get("outcomeIndex"), int) else row.get("outcome_index"),
        "avg_price": float(p["avg_price"]) if isinstance(p.get("avg_price"), (int, float)) else None,
        "cur_price": float(p["cur_price"]) if isinstance(p.get("cur_price"), (int, float)) else None,
        "total_bought": float(total_bought) if isinstance(total_bought, (int, float)) else None,
        "cost_basis_usd": float(cost_basis_usd) if isinstance(cost_basis_usd, (int, float)) else None,
        "cash_pnl": float(p["cash_pnl"]) if isinstance(p.get("cash_pnl"), (int, float)) else None,
        "realized_pnl": float(p["realized_pnl"]) if isinstance(p.get("realized_pnl"), (int, float)) else None,
        "size": float(p["size"]) if isinstance(p.get("size"), (int, float)) else None,
        "ts_epoch": ts_epoch,
        "is_closed": bool(is_closed),
    }


def _closed_row_key(payload: Dict[str, Any]) -> str:
    sig = "|".join(
        [
            str(payload.get("token_id") or ""),
            str(payload.get("condition_id") or ""),
            str(payload.get("outcome_index") or ""),
            str(payload.get("ts_epoch") or ""),
            str(payload.get("total_bought") or ""),
            str(payload.get("cost_basis_usd") or ""),
            str(payload.get("realized_pnl") or ""),
            str(payload.get("avg_price") or ""),
        ]
    )
    return hashlib.sha1(sig.encode("utf-8")).hexdigest()


def _days_between_now(iso_ts: Optional[str]) -> Optional[float]:
    if not isinstance(iso_ts, str) or not iso_ts.strip():
        return None
    dt = parse_ts_to_dt(iso_ts)
    if dt is None:
        return None
    return (utc_now() - dt).total_seconds() / 86400.0


def save_open_positions_cache(
    conn: sqlite3.Connection,
    address: str,
    positions_raw: List[Dict[str, Any]],
) -> int:
    now_iso = iso(utc_now())
    by_token: Dict[str, Tuple[Any, ...]] = {}
    for row in positions_raw:
        if not isinstance(row, dict):
            continue
        payload = _to_position_cache_row(row, is_closed=False)
        if payload is None:
            continue
        token_id = str(payload.get("token_id") or "")
        if not token_id:
            continue
        row_tuple = (
            address.lower(),
            token_id,
            payload.get("condition_id"),
            payload.get("market_slug"),
            payload.get("outcome"),
            payload.get("outcome_index"),
            payload.get("avg_price"),
            payload.get("cur_price"),
            payload.get("total_bought"),
            payload.get("cost_basis_usd"),
            payload.get("cash_pnl"),
            payload.get("realized_pnl"),
            payload.get("size"),
            payload.get("ts_epoch"),
            now_iso,
        )
        # Data API may return duplicate token rows across pages; keep the latest timestamp.
        prev = by_token.get(token_id)
        if prev is None:
            by_token[token_id] = row_tuple
            continue
        prev_ts = prev[13] if isinstance(prev[13], int) else -1
        cur_ts = row_tuple[13] if isinstance(row_tuple[13], int) else -1
        if cur_ts >= prev_ts:
            by_token[token_id] = row_tuple

    rows = list(by_token.values())
    conn.execute("DELETE FROM pm_open_positions_cache WHERE address=?", (address.lower(),))
    if rows:
        conn.executemany(
            "INSERT INTO pm_open_positions_cache("
            "address, token_id, condition_id, market_slug, outcome, outcome_index, "
            "avg_price, cur_price, total_bought, cost_basis_usd, cash_pnl, realized_pnl, size, ts_epoch, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.commit()
    return len(rows)


def _replace_closed_positions_cache_full(
    conn: sqlite3.Connection,
    address: str,
    closed_rows: List[Dict[str, Any]],
) -> int:
    now_iso = iso(utc_now())
    rows: List[Tuple[Any, ...]] = []
    for row in closed_rows:
        if not isinstance(row, dict):
            continue
        payload = _to_position_cache_row(row, is_closed=True)
        if payload is None:
            continue
        row_key = _closed_row_key(payload)
        rows.append(
            (
                address.lower(),
                row_key,
                payload.get("token_id"),
                payload.get("condition_id"),
                payload.get("market_slug"),
                payload.get("outcome"),
                payload.get("outcome_index"),
                payload.get("avg_price"),
                payload.get("cur_price"),
                payload.get("total_bought"),
                payload.get("cost_basis_usd"),
                payload.get("realized_pnl"),
                payload.get("size"),
                payload.get("ts_epoch"),
                now_iso,
            )
        )

    conn.execute("DELETE FROM pm_closed_positions_cache WHERE address=?", (address.lower(),))
    if rows:
        conn.executemany(
            "INSERT INTO pm_closed_positions_cache("
            "address, row_key, token_id, condition_id, market_slug, outcome, outcome_index, "
            "avg_price, cur_price, total_bought, cost_basis_usd, realized_pnl, size, ts_epoch, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    conn.execute(
        "INSERT INTO pm_closed_sync_state(address, cached_rows, last_sync_utc, last_full_sync_utc, updated_at) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(address) DO UPDATE SET "
        "cached_rows=excluded.cached_rows, "
        "last_sync_utc=excluded.last_sync_utc, "
        "last_full_sync_utc=excluded.last_full_sync_utc, "
        "updated_at=excluded.updated_at",
        (address.lower(), len(rows), now_iso, now_iso, now_iso),
    )
    conn.commit()
    return len(rows)


def _upsert_closed_positions_cache_incremental(
    conn: sqlite3.Connection,
    address: str,
    closed_rows: List[Dict[str, Any]],
) -> int:
    now_iso = iso(utc_now())
    rows: List[Tuple[Any, ...]] = []
    for row in closed_rows:
        if not isinstance(row, dict):
            continue
        payload = _to_position_cache_row(row, is_closed=True)
        if payload is None:
            continue
        row_key = _closed_row_key(payload)
        rows.append(
            (
                address.lower(),
                row_key,
                payload.get("token_id"),
                payload.get("condition_id"),
                payload.get("market_slug"),
                payload.get("outcome"),
                payload.get("outcome_index"),
                payload.get("avg_price"),
                payload.get("cur_price"),
                payload.get("total_bought"),
                payload.get("cost_basis_usd"),
                payload.get("realized_pnl"),
                payload.get("size"),
                payload.get("ts_epoch"),
                now_iso,
            )
        )
    if rows:
        conn.executemany(
            "INSERT INTO pm_closed_positions_cache("
            "address, row_key, token_id, condition_id, market_slug, outcome, outcome_index, "
            "avg_price, cur_price, total_bought, cost_basis_usd, realized_pnl, size, ts_epoch, updated_at"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(address, row_key) DO UPDATE SET "
            "token_id=excluded.token_id, "
            "condition_id=excluded.condition_id, "
            "market_slug=excluded.market_slug, "
            "outcome=excluded.outcome, "
            "outcome_index=excluded.outcome_index, "
            "avg_price=excluded.avg_price, "
            "cur_price=excluded.cur_price, "
            "total_bought=excluded.total_bought, "
            "cost_basis_usd=excluded.cost_basis_usd, "
            "realized_pnl=excluded.realized_pnl, "
            "size=excluded.size, "
            "ts_epoch=excluded.ts_epoch, "
            "updated_at=excluded.updated_at",
            rows,
        )
    total = conn.execute(
        "SELECT COUNT(*) FROM pm_closed_positions_cache WHERE address=?",
        (address.lower(),),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO pm_closed_sync_state(address, cached_rows, last_sync_utc, updated_at) "
        "VALUES(?, ?, ?, ?) "
        "ON CONFLICT(address) DO UPDATE SET "
        "cached_rows=excluded.cached_rows, "
        "last_sync_utc=excluded.last_sync_utc, "
        "updated_at=excluded.updated_at",
        (address.lower(), int(total), now_iso, now_iso),
    )
    conn.commit()
    return int(total)


def sync_closed_positions_cache(
    conn: sqlite3.Connection,
    session: requests.Session,
    address: str,
) -> Dict[str, Any]:
    state_row = conn.execute(
        "SELECT cached_rows, last_full_sync_utc FROM pm_closed_sync_state WHERE address=? LIMIT 1",
        (address.lower(),),
    ).fetchone()
    cached_rows = int(state_row[0]) if state_row and isinstance(state_row[0], (int, float)) else 0
    last_full_sync_utc = state_row[1] if state_row else None
    full_age_days = _days_between_now(last_full_sync_utc)
    need_full = state_row is None or full_age_days is None or full_age_days >= float(CLOSED_FULL_RESYNC_DAYS)

    if need_full:
        _progress("  - syncing closed positions (full)")
        full_rows = fetch_closed_positions(
            session,
            address,
            limit=DEFAULT_CLOSED_PAGE_LIMIT,
            start_offset=0,
            sort_by="TIMESTAMP",
            sort_direction="ASC",
        )
        total = _replace_closed_positions_cache_full(conn, address, full_rows)
        return {"mode": "full", "cachedRows": total, "fetchedRows": len(full_rows)}

    start_offset = max(0, cached_rows - CLOSED_INCREMENTAL_OVERLAP_ROWS)
    _progress(f"  - syncing closed positions (incremental from offset={start_offset})")
    incremental_rows = fetch_closed_positions(
        session,
        address,
        limit=DEFAULT_CLOSED_PAGE_LIMIT,
        start_offset=start_offset,
        sort_by="TIMESTAMP",
        sort_direction="ASC",
    )
    total = _upsert_closed_positions_cache_incremental(conn, address, incremental_rows)
    return {"mode": "incremental", "cachedRows": total, "fetchedRows": len(incremental_rows), "startOffset": start_offset}


def load_cached_positions_for_address(conn: sqlite3.Connection, address: str) -> Tuple[List[Dict[str, Any]], int, int]:
    open_rows = conn.execute(
        "SELECT token_id, condition_id, market_slug, outcome, outcome_index, avg_price, cur_price, "
        "total_bought, cost_basis_usd, cash_pnl, realized_pnl, size, ts_epoch "
        "FROM pm_open_positions_cache WHERE address=?",
        (address.lower(),),
    ).fetchall()
    closed_rows = conn.execute(
        "SELECT token_id, condition_id, market_slug, outcome, outcome_index, avg_price, cur_price, "
        "total_bought, cost_basis_usd, realized_pnl, size, ts_epoch "
        "FROM pm_closed_positions_cache WHERE address=?",
        (address.lower(),),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for row in open_rows:
        out.append(
            {
                "token_id": row[0],
                "market": row[1],
                "slug": row[2],
                "outcome": row[3],
                "outcome_index": row[4],
                "avg_price": row[5],
                "cur_price": row[6],
                "total_bought": row[7],
                "cost_basis_usd": row[8],
                "cash_pnl": row[9],
                "realized_pnl": row[10],
                "size": row[11],
                "ts_epoch": row[12],
                "closed": False,
            }
        )

    for row in closed_rows:
        realized = row[9] if isinstance(row[9], (int, float)) else None
        out.append(
            {
                "token_id": row[0],
                "market": row[1],
                "slug": row[2],
                "outcome": row[3],
                "outcome_index": row[4],
                "avg_price": row[5],
                "cur_price": row[6],
                "total_bought": row[7],
                "cost_basis_usd": row[8],
                "cash_pnl": realized,
                "realized_pnl": realized,
                "size": row[10],
                "ts_epoch": row[11],
                "closed": True,
            }
        )

    return out, len(open_rows), len(closed_rows)


def _derive_cost_basis_usd(position: Dict[str, Any]) -> Optional[float]:
    cost_basis = position.get("cost_basis_usd")
    if isinstance(cost_basis, (int, float)) and float(cost_basis) > 0:
        return float(cost_basis)
    total_bought = position.get("total_bought")
    if isinstance(total_bought, (int, float)) and float(total_bought) > 0:
        return float(total_bought)
    avg_price = position.get("avg_price")
    size = position.get("size")
    if isinstance(avg_price, (int, float)) and isinstance(size, (int, float)) and abs(float(size)) > 0:
        return abs(float(size)) * float(avg_price)
    return None


def _derive_position_size(position: Dict[str, Any]) -> Optional[float]:
    size = position.get("size")
    if isinstance(size, (int, float)) and abs(float(size)) > 0:
        return abs(float(size))
    avg_price = position.get("avg_price")
    cost_basis_usd = _derive_cost_basis_usd(position)
    if isinstance(avg_price, (int, float)) and float(avg_price) > 0 and isinstance(cost_basis_usd, (int, float)):
        return float(cost_basis_usd) / float(avg_price)
    return None


def _quantize_binary_resolution(value: float, epsilon: float = RESOLUTION_EPSILON) -> Optional[float]:
    if not math.isfinite(value):
        return None
    if abs(value - 0.0) <= epsilon:
        return 0.0
    if abs(value - 1.0) <= epsilon:
        return 1.0
    return None


def _resolve_position_payout(position: Dict[str, Any]) -> Tuple[Optional[float], str]:
    cur_price = position.get("cur_price")
    if isinstance(cur_price, (int, float)):
        resolved = _quantize_binary_resolution(float(cur_price))
        if resolved is not None:
            return resolved, "cur_price"
    avg_price = position.get("avg_price")
    realized_pnl = position.get("realized_pnl")
    size_abs = _derive_position_size(position)
    if isinstance(avg_price, (int, float)) and isinstance(realized_pnl, (int, float)) and isinstance(size_abs, (int, float)):
        if size_abs > 0:
            inferred = float(avg_price) + float(realized_pnl) / size_abs
            resolved = _quantize_binary_resolution(inferred)
            if resolved is not None:
                return resolved, "economics"
    return None, "ambiguous"


def compute_realized_edge_score(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    edge_weighted_num = 0.0
    edge_weighted_den = 0.0
    edge_count = 0
    skipped_open_positions = 0
    skipped_missing_fields = 0
    skipped_ambiguous_resolution = 0
    resolution_sources = {"cur_price": 0, "economics": 0}

    for p in positions:
        if not bool(p.get("closed")):
            skipped_open_positions += 1
            continue
        entry_price = p.get("avg_price")
        if not isinstance(entry_price, (int, float)):
            skipped_missing_fields += 1
            continue
        cost_basis_usd = _derive_cost_basis_usd(p)
        if cost_basis_usd is None or cost_basis_usd <= 0:
            skipped_missing_fields += 1
            continue
        resolution, source = _resolve_position_payout(p)
        if resolution is None:
            skipped_ambiguous_resolution += 1
            continue
        edge = float(resolution) - float(entry_price)
        edge_weighted_num += edge * cost_basis_usd
        edge_weighted_den += cost_basis_usd
        edge_count += 1
        resolution_sources[source] = resolution_sources.get(source, 0) + 1

    realized_edge_score = None
    if edge_weighted_den > 0:
        realized_edge_score = edge_weighted_num / edge_weighted_den

    return {
        "realized_edge_score": realized_edge_score,
        "details": {
            "edgeSamples": edge_count,
            "edgeWeightUsd": edge_weighted_den if edge_weighted_den > 0 else None,
            "skippedOpenPositions": skipped_open_positions,
            "skippedMissingFields": skipped_missing_fields,
            "skippedAmbiguousResolution": skipped_ambiguous_resolution,
            "resolutionSources": resolution_sources,
            "resolutionEpsilon": RESOLUTION_EPSILON,
        },
    }


def fetch_prices_history(
    session: requests.Session, token_id: str, start_ts: int, end_ts: int, alt_market: Optional[str] = None
) -> List[Tuple[int, float]]:
    url = f"{CLOB_API}/prices-history"
    param_variants = [
        {"market": token_id, "startTs": start_ts, "endTs": end_ts, "interval": "1d"},
        {"token_id": token_id, "startTs": start_ts, "endTs": end_ts, "interval": "1d"},
    ]
    if alt_market:
        param_variants.append({"market": alt_market, "startTs": start_ts, "endTs": end_ts, "interval": "1d"})

    raw = None
    for params in param_variants:
        data = http_get_json(session, url, params=params, timeout_s=25.0, max_retries=2)
        if isinstance(data, dict) and isinstance(data.get("history"), list):
            raw = data["history"]
        elif isinstance(data, list):
            raw = data
        else:
            raw = None
        if raw:
            break
    if not raw:
        return []
    out: List[Tuple[int, float]] = []
    for row in raw:
        if isinstance(row, dict):
            t = row.get("t")
            p = row.get("p")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            t, p = row[0], row[1]
        else:
            continue
        t_i = int(t) if isinstance(t, (int, float, str)) and str(t).isdigit() else None
        p_f = _as_float(p)
        if t_i is None or p_f is None:
            continue
        out.append((t_i, float(p_f)))
    out.sort(key=lambda x: x[0])
    return out


def build_daily_price_map(points: List[Tuple[int, float]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for t, p in points:
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
        key = dt.date().isoformat()
        out[key] = p
    return out


def compute_equity_series(
    trades: List[Dict[str, Any]],
    prices_by_token_by_day: Dict[str, Dict[str, float]],
    start_day: datetime,
    end_day: datetime,
) -> List[Tuple[str, float]]:
    start_d = start_day.date()
    end_d = end_day.date()

    txs: List[Tuple[datetime, Dict[str, Any]]] = []
    for t in trades:
        dt = parse_ts_to_dt(t.get("ts"))
        if dt is None:
            continue
        txs.append((dt, t))
    txs.sort(key=lambda x: x[0])

    cash = 0.0
    bal: Dict[str, float] = {}
    i = 0
    series: List[Tuple[str, float]] = []
    last_price: Dict[str, float] = {}

    day = start_d
    while day <= end_d:
        day_end = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
        while i < len(txs) and txs[i][0] < day_end:
            t = txs[i][1]
            side = t.get("side")
            token_id = t.get("token_id")
            usd = t.get("usd")
            size = t.get("size")
            price = t.get("price")
            if token_id and side in {"BUY", "SELL"}:
                if not isinstance(size, (int, float)):
                    if isinstance(usd, (int, float)) and isinstance(price, (int, float)) and price > 0:
                        size = float(usd) / float(price)
                if isinstance(size, (int, float)):
                    size_f = float(size)
                    if isinstance(usd, (int, float)):
                        usd_f = float(usd)
                    elif isinstance(price, (int, float)):
                        usd_f = float(price) * abs(size_f)
                    else:
                        usd_f = 0.0
                    if side == "BUY":
                        cash -= usd_f
                        bal[str(token_id)] = bal.get(str(token_id), 0.0) + size_f
                    else:
                        cash += usd_f
                        bal[str(token_id)] = bal.get(str(token_id), 0.0) - size_f
            i += 1

        eq = cash
        day_key = day.isoformat()
        for token_id, amount in bal.items():
            if amount == 0:
                continue
            price_map = prices_by_token_by_day.get(token_id) or {}
            p = price_map.get(day_key)
            if p is None:
                p = last_price.get(token_id)
            else:
                last_price[token_id] = p
            if p is None:
                continue
            eq += amount * p
        series.append((day_key, eq))
        day = day + timedelta(days=1)

    return series


def compute_cashflow_series(trades: List[Dict[str, Any]], start_day: datetime, end_day: datetime) -> List[Tuple[str, float]]:
    start_d = start_day.date()
    end_d = end_day.date()

    txs: List[Tuple[datetime, Dict[str, Any]]] = []
    for t in trades:
        dt = parse_ts_to_dt(t.get("ts"))
        if dt is None:
            continue
        txs.append((dt, t))
    txs.sort(key=lambda x: x[0])

    cash = 0.0
    i = 0
    series: List[Tuple[str, float]] = []
    day = start_d
    while day <= end_d:
        day_end = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(days=1)
        while i < len(txs) and txs[i][0] < day_end:
            t = txs[i][1]
            side = t.get("side")
            usd = t.get("usd")
            if side in {"BUY", "SELL"} and isinstance(usd, (int, float)):
                if side == "BUY":
                    cash -= float(usd)
                else:
                    cash += float(usd)
            i += 1
        series.append((day.isoformat(), cash))
        day = day + timedelta(days=1)
    return series


def compute_mdd_sharpe(equity_series: List[Tuple[str, float]], annualization_periods: float = 365.0) -> Tuple[Optional[float], Optional[float]]:
    if len(equity_series) < 3:
        return None, None

    vals = [v for _, v in equity_series]
    min_v = min(vals)
    if min_v <= 0:
        shift = -min_v + 1.0
        vals = [v + shift for v in vals]
    peak = None
    max_dd = 0.0
    for v in vals:
        if peak is None or v > peak:
            peak = v
        if peak is None or peak <= 0:
            continue
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    changes = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    s = std(changes)
    sharpe = None
    if s > 0:
        sharpe = (mean(changes) / s) * math.sqrt(float(annualization_periods))
    return max_dd if max_dd > 0 else 0.0, sharpe


def compute_pnl_drawdown_sharpe(
    pnl_series: List[Tuple[str, float]], annualization_periods: float = 365.0
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if len(pnl_series) < 3:
        return None, None, None, None
    vals = [v for _, v in pnl_series]
    peak = None
    peak_at_max_dd = None
    max_dd_usd = 0.0
    max_dd_ratio: Optional[float] = None
    for v in vals:
        if peak is None or v > peak:
            peak = v
        if peak is None:
            continue
        dd_usd = float(peak) - float(v)
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
            peak_at_max_dd = float(peak)
            if float(peak) > 0:
                max_dd_ratio = dd_usd / float(peak)
            else:
                max_dd_ratio = None
    changes = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    s = std(changes)
    sharpe = None
    if s > 0:
        sharpe = (mean(changes) / s) * math.sqrt(float(annualization_periods))
    if max_dd_usd <= 0:
        return 0.0, 0.0, float(peak) if isinstance(peak, (int, float)) else None, sharpe
    return max_dd_ratio, max_dd_usd, peak_at_max_dd, sharpe


def is_user_pnl_metrics_compatible(details: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(details, dict):
        return False
    return str(details.get("userPnlCompatVersion") or "").strip() == USER_PNL_METRICS_COMPAT_VERSION


def compute_ulcer_index(pnl_series: List[Tuple[str, float]], total_pnl: float = 0.0) -> Optional[float]:
    """溃疡指数: UI = sqrt(mean(D_i^2)), D_i = (p_i - HWM_i) / HWM_i * 100
    HWM 门槛 = 总盈利的千分之五，避免 PnL 曲线早期从 0 附近起步时百分比被极端放大。
    """
    if len(pnl_series) < 3:
        return None
    vals = [v for _, v in pnl_series]
    hwm = vals[0]
    dd_sq_sum = 0.0
    counted = 0
    hwm_floor = max(abs(total_pnl) * 0.005, 100.0)
    for v in vals:
        if v > hwm:
            hwm = v
        if hwm >= hwm_floor:
            d = (v - hwm) / hwm * 100.0
            dd_sq_sum += d * d
            counted += 1
    if counted < 3:
        return None
    return math.sqrt(dd_sq_sum / counted)


def compute_equity_r_squared(pnl_series: List[Tuple[str, float]]) -> Optional[float]:
    """决定系数 R²: 净值曲线与完美直线的拟合度"""
    if len(pnl_series) < 3:
        return None
    vals = [v for _, v in pnl_series]
    n = len(vals)
    x_mean = (n - 1) / 2.0
    y_mean = sum(vals) / n
    ss_xy = sum((i - x_mean) * (yi - y_mean) for i, yi in enumerate(vals))
    ss_xx = sum((i - x_mean) ** 2 for i in range(n))
    if ss_xx == 0:
        return None
    beta1 = ss_xy / ss_xx
    beta0 = y_mean - beta1 * x_mean
    ss_res = sum((yi - (beta0 + beta1 * i)) ** 2 for i, yi in enumerate(vals))
    ss_tot = sum((yi - y_mean) ** 2 for yi in vals)
    if ss_tot == 0:
        return 1.0
    return 1.0 - ss_res / ss_tot


def compute_metrics_snapshot(
    positions: List[Dict[str, Any]],
    min_usd: float,
    max_drawdown: Optional[float],
    sharpe: Optional[float],
) -> Tuple[Dict[str, Any], str]:
    per_market_pnl = {}
    realized = 0.0
    total_raw = 0.0

    for p in positions:
        mkey = p.get("market") or p.get("slug") or "unknown"
        realized_field = p.get("realized_pnl")
        if isinstance(realized_field, (int, float)):
            realized += float(realized_field)
        cash_pnl = p.get("cash_pnl")
        if isinstance(cash_pnl, (int, float)):
            pnl = float(cash_pnl)
            total_raw += pnl
            per_market_pnl[mkey] = per_market_pnl.get(mkey, 0.0) + pnl

    total_by_market = sum(per_market_pnl.values())
    unrealized = total_by_market - realized
    gp = sum(v for v in per_market_pnl.values() if v > 0)
    gl = sum(-v for v in per_market_pnl.values() if v < 0)
    pf = None
    if gl > 0:
        pf = gp / gl

    cost_basis_total = 0.0
    cost_basis_known = False
    for p in positions:
        tb = p.get("total_bought")
        if isinstance(tb, (int, float)) and tb > 0:
            cost_basis_total += float(tb)
            cost_basis_known = True
    roi = None
    if cost_basis_known and cost_basis_total > 0:
        roi = total_by_market / cost_basis_total

    details = {
        "rawPositionsCashPnLSum": total_raw,
        "rawPositionsCashPnLSumByMarket": total_by_market,
    }

    confidence = "high"
    if not per_market_pnl:
        confidence = "low"
    elif not cost_basis_known:
        confidence = "medium"

    return (
        {
            "total_pnl": total_by_market,
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "profit_factor": pf,
            "roi": roi,
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "details": details,
        },
        confidence,
    )


def compute_position_based_stats(
    positions: List[Dict[str, Any]], positions_raw_count: int, closed_positions_raw_count: int
) -> Dict[str, Any]:
    total_trades = int(positions_raw_count) + int(closed_positions_raw_count)
    winning = 0
    losing = 0
    considered = 0

    w_sum = 0.0
    wx_sum = 0.0

    for p in positions:
        cash_pnl = p.get("cash_pnl")
        if isinstance(cash_pnl, (int, float)):
            considered += 1
            if float(cash_pnl) > 0:
                winning += 1
            elif float(cash_pnl) < 0:
                losing += 1

        avg_price = p.get("avg_price")
        cost_basis_usd = _derive_cost_basis_usd(p)
        if isinstance(avg_price, (int, float)) and isinstance(cost_basis_usd, (int, float)) and float(cost_basis_usd) > 0:
            w = float(cost_basis_usd)
            w_sum += w
            wx_sum += float(avg_price) * w

    win_rate = None
    if considered > 0:
        win_rate = winning / float(considered)
    avg_trade_price = None
    if w_sum > 0:
        avg_trade_price = wx_sum / w_sum

    return {
        "current_position_value_usd": None,
        "total_trades": total_trades,
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": win_rate,
        "avg_trade_price": avg_trade_price,
        "avg_trade_price_weight_usd": w_sum if w_sum > 0 else None,
        "cash_pnl_considered_trades": considered,
    }


def compute_open_position_execution_stats(
    session: requests.Session,
    positions: List[Dict[str, Any]],
    *,
    book_cache: Optional[Dict[str, Optional[Dict[str, float]]]] = None,
    market_cache: Optional[Dict[str, Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    depths: List[float] = []
    settle_days: List[float] = []
    details: List[Dict[str, Any]] = []
    effective_book_cache: Dict[str, Optional[Dict[str, float]]] = book_cache if book_cache is not None else {}
    effective_market_cache: Dict[str, Optional[Dict[str, Any]]] = market_cache if market_cache is not None else {}
    open_positions_analyzed = 0
    missing_book = 0
    missing_settlement = 0
    resolved_skipped = 0

    for pos in positions:
        if bool(pos.get("closed")):
            continue
        token_id = str(pos.get("token_id") or "").strip()
        slug = str(pos.get("slug") or "").strip()
        if not token_id:
            continue

        market_info = fetch_market_by_slug(session, slug, effective_market_cache) if slug else None
        if is_market_resolved(market_info):
            resolved_skipped += 1
            continue

        open_positions_analyzed += 1
        if token_id in effective_book_cache:
            book_depth = effective_book_cache[token_id]
        else:
            book_depth = None
            book = fetch_orderbook(session, token_id)
            if book is not None:
                book_depth = compute_orderbook_top5_depth_usd(book)
            effective_book_cache[token_id] = book_depth
        if book_depth is None:
            missing_book += 1
        else:
            depths.append(float(book_depth["top5_depth_usd"]))

        days = market_days_to_settlement(market_info)
        if days is None:
            missing_settlement += 1
        else:
            settle_days.append(float(days))

        details.append(
            {
                "token_id": token_id,
                "slug": slug or None,
                "top5_depth_usd": book_depth.get("top5_depth_usd") if book_depth else None,
                "bid_top5_depth_usd": book_depth.get("bid_top5_depth_usd") if book_depth else None,
                "ask_top5_depth_usd": book_depth.get("ask_top5_depth_usd") if book_depth else None,
                "settlement_days": days,
            }
        )

    return {
        "avg_open_top5_depth_usd": mean(depths) if depths else None,
        "avg_open_settlement_days": mean(settle_days) if settle_days else None,
        "open_positions_analyzed": open_positions_analyzed,
        "open_positions_missing_book": missing_book,
        "open_positions_missing_settlement": missing_settlement,
        "open_positions_resolved_skipped": resolved_skipped,
        "open_positions_with_book": len(depths),
        "open_positions_with_settlement": len(settle_days),
        "open_position_execution_details": details[:100],
    }


def write_metrics_result(conn: sqlite3.Connection, run_id: int, metrics: Dict[str, Any], confidence: str) -> None:
    raise RuntimeError("write_metrics_result is deprecated")


def _normalize_source_tag(tag: str) -> str:
    return (tag or "").strip().upper()


def _merge_source_tags(existing: Optional[str], new_tag: str) -> Optional[str]:
    nt = _normalize_source_tag(new_tag)
    tags: List[str] = []
    if isinstance(existing, str) and existing.strip():
        tags = [t.strip().upper() for t in existing.split(",") if t.strip()]
    tags = [t for t in tags if t not in IGNORED_SOURCE_TAGS]
    if nt in IGNORED_SOURCE_TAGS:
        return ",".join(tags) if tags else None
    if not nt:
        return ",".join(tags) if tags else None
    if nt not in tags:
        tags.append(nt)
    return ",".join(tags)


def upsert_address_metrics(
    conn: sqlite3.Connection,
    address: str,
    metrics: Dict[str, Any],
    confidence: str,
    source_tag: str = "",
) -> None:
    now_iso = iso(utc_now())
    existing_row = conn.execute(
        "SELECT source_tags FROM address_metrics WHERE address=? LIMIT 1",
        (address.lower(),),
    ).fetchone()
    existing_source_tags = existing_row[0] if existing_row else None
    merged_source_tags = _merge_source_tags(existing_source_tags, source_tag)
    conn.execute(
        "INSERT INTO address_metrics(address, snapshot_utc, total_pnl, realized_pnl, unrealized_pnl, profit_factor, roi, "
        "max_drawdown, sharpe, current_position_value_usd, total_trades, winning_trades, "
        "losing_trades, win_rate, avg_trade_price, realized_edge_score, "
        "confidence, details_json, source_tags, ulcer_index, equity_r2, "
        "avg_open_top5_depth_usd, avg_open_settlement_days, "
        "copytrade_value_score, copytrade_value_level, copytrade_value_exclusion_reason, "
        "copytrade_value_score_version, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(address) DO UPDATE SET "
        "snapshot_utc=excluded.snapshot_utc, "
        "total_pnl=excluded.total_pnl, "
        "realized_pnl=excluded.realized_pnl, "
        "unrealized_pnl=excluded.unrealized_pnl, "
        "profit_factor=excluded.profit_factor, "
        "roi=excluded.roi, "
        "max_drawdown=excluded.max_drawdown, "
        "sharpe=excluded.sharpe, "
        "current_position_value_usd=excluded.current_position_value_usd, "
        "total_trades=excluded.total_trades, "
        "winning_trades=excluded.winning_trades, "
        "losing_trades=excluded.losing_trades, "
        "win_rate=excluded.win_rate, "
        "avg_trade_price=excluded.avg_trade_price, "
        "realized_edge_score=excluded.realized_edge_score, "
        "confidence=excluded.confidence, "
        "details_json=excluded.details_json, "
        "source_tags=excluded.source_tags, "
        "ulcer_index=excluded.ulcer_index, "
        "equity_r2=excluded.equity_r2, "
        "avg_open_top5_depth_usd=excluded.avg_open_top5_depth_usd, "
        "avg_open_settlement_days=excluded.avg_open_settlement_days, "
        "copytrade_value_score=excluded.copytrade_value_score, "
        "copytrade_value_level=excluded.copytrade_value_level, "
        "copytrade_value_exclusion_reason=excluded.copytrade_value_exclusion_reason, "
        "copytrade_value_score_version=excluded.copytrade_value_score_version, "
        "updated_at=excluded.updated_at",
        (
            address.lower(),
            now_iso,
            metrics.get("total_pnl"),
            metrics.get("realized_pnl"),
            metrics.get("unrealized_pnl"),
            metrics.get("profit_factor"),
            metrics.get("roi"),
            metrics.get("max_drawdown"),
            metrics.get("sharpe"),
            metrics.get("current_position_value_usd"),
            metrics.get("total_trades"),
            metrics.get("winning_trades"),
            metrics.get("losing_trades"),
            metrics.get("win_rate"),
            metrics.get("avg_trade_price"),
            metrics.get("realized_edge_score"),
            confidence,
            json.dumps(metrics.get("details") or {}, ensure_ascii=False),
            merged_source_tags,
            metrics.get("ulcer_index"),
            metrics.get("equity_r2"),
            metrics.get("avg_open_top5_depth_usd"),
            metrics.get("avg_open_settlement_days"),
            metrics.get("copytrade_value_score"),
            metrics.get("copytrade_value_level"),
            metrics.get("copytrade_value_exclusion_reason"),
            metrics.get("copytrade_value_score_version"),
            now_iso,
        ),
    )
    conn.commit()


def parse_args(argv: Sequence[str]) -> Config:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", type=str, required=True)
    ap.add_argument("--db", type=str, default="metrics.sqlite")
    ap.add_argument("--min-usd", type=float, default=1.0)
    ap.add_argument("--price-history-days", type=int, default=90)
    ap.add_argument("--mdd-mode", type=str, default="auto", choices=["auto", "cashflow"])
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--progress", action="store_true")
    ap.add_argument("--source-tag", type=str, default="")
    args = ap.parse_args(list(argv))
    return Config(
        address=str(args.address).lower(),
        db_path=str(args.db),
        min_usd=float(args.min_usd),
        price_history_days=int(args.price_history_days),
        mdd_mode=str(args.mdd_mode),
        debug=bool(args.debug),
        progress=bool(args.progress),
        source_tag=str(args.source_tag or ""),
    )


def compute_and_save_metrics(cfg: Config) -> Dict[str, Any]:
    global _PROGRESS
    _PROGRESS = bool(cfg.progress)
    session = requests.Session()

    conn = sqlite3.connect(cfg.db_path, timeout=60.0)
    try:
        ensure_schema(conn)

        _progress(f"- address={cfg.address}")
        _progress("  - fetching open positions")
        positions_raw = fetch_positions(session, cfg.address)
        open_cached_rows = save_open_positions_cache(conn, cfg.address, positions_raw)
        _progress(f"  - open positions cached: {open_cached_rows}")

        closed_sync_info: Dict[str, Any] = {}
        try:
            closed_sync_info = sync_closed_positions_cache(conn, session, cfg.address)
        except Exception as e:
            cached_closed_rows = conn.execute(
                "SELECT COUNT(*) FROM pm_closed_positions_cache WHERE address=?",
                (cfg.address.lower(),),
            ).fetchone()[0]
            # 新地址必须先完整闭仓回补；只有已有缓存时才允许回退。
            if int(cached_closed_rows) <= 0:
                raise
            _progress(f"  - closed sync failed, fallback to existing cache: {e}")
            closed_sync_info = {"mode": "stale_cache", "error": str(e)}

        pos_norm, positions_raw_count, closed_positions_raw_count = load_cached_positions_for_address(conn, cfg.address)
        _progress(
            f"  - positions ready from cache: open={positions_raw_count} closed={closed_positions_raw_count} total={len(pos_norm)}"
        )
        pos_stats = compute_position_based_stats(pos_norm, positions_raw_count, closed_positions_raw_count)
        _progress("  - fetching current value")
        pos_stats["current_position_value_usd"] = fetch_total_value(session, cfg.address)
        edge_stats = compute_realized_edge_score(pos_norm)
        positions_debug = None
        if cfg.debug and positions_raw:
            sample = positions_raw[0] if isinstance(positions_raw[0], dict) else {}
            candidates = ["cashPnl", "pnl", "profit", "initialValue", "costBasis", "avgPrice", "size", "asset"]
            counts = {k: 0 for k in candidates}
            for r in positions_raw[:100]:
                if not isinstance(r, dict):
                    continue
                for k in candidates:
                    if r.get(k) is not None:
                        counts[k] += 1
            positions_debug = {"sampleKeys": sorted(list(sample.keys())), "nonNullCounts": counts}

        total_pnl_early = 0.0
        for p in pos_norm:
            cash_pnl = p.get("cash_pnl")
            if isinstance(cash_pnl, (int, float)):
                total_pnl_early += float(cash_pnl)

        if abs(total_pnl_early) < PNL_SKIP_THRESHOLD:
            metrics = {
                "total_pnl": total_pnl_early,
                "realized_pnl": None,
                "unrealized_pnl": None,
                "profit_factor": 0.0,
                "roi": 0.0,
                "max_drawdown": None,
                "sharpe": None,
                "current_position_value_usd": pos_stats.get("current_position_value_usd"),
                "total_trades": pos_stats.get("total_trades"),
                "winning_trades": pos_stats.get("winning_trades"),
                "losing_trades": pos_stats.get("losing_trades"),
                "win_rate": pos_stats.get("win_rate"),
                "avg_trade_price": pos_stats.get("avg_trade_price"),
                "realized_edge_score": edge_stats.get("realized_edge_score"),
                "avg_open_top5_depth_usd": None,
                "avg_open_settlement_days": None,
                "details": {
                    "skipped": True,
                    "skipReason": "abs_total_pnl_below_threshold",
                    "threshold": PNL_SKIP_THRESHOLD,
                    "absTotalPnl": abs(total_pnl_early),
                    "rawPositionsCashPnLSum": total_pnl_early,
                    "userPnlCompatVersion": USER_PNL_METRICS_COMPAT_VERSION,
                    "pnlCurveInterval": USER_PNL_METRICS_INTERVAL,
                    "pnlCurveFidelity": USER_PNL_METRICS_FIDELITY,
                    "positionBased": pos_stats,
                    "positionEdge": edge_stats.get("details"),
                    "openExecution": {
                        "skipped": True,
                        "skipReason": "abs_total_pnl_below_threshold",
                        "threshold": PNL_SKIP_THRESHOLD,
                        "absTotalPnl": abs(total_pnl_early),
                    },
                    "open_positions_analyzed": 0,
                    "open_positions_missing_book": None,
                    "open_positions_missing_settlement": None,
                    "closedSync": closed_sync_info,
                },
            }
            upsert_address_metrics(conn, cfg.address, metrics, "skipped_low_pnl", source_tag=cfg.source_tag)
            return {
                "address": cfg.address,
                "rawPositions": len(pos_norm),
                "confidence": "skipped_low_pnl",
                "metrics": metrics,
                "details": None,
                "positionsDebug": positions_debug,
            }

        if total_pnl_early >= PNL_SKIP_THRESHOLD:
            _progress("  - computing open liquidity and settlement stats")
            open_exec_stats = compute_open_position_execution_stats(session, pos_norm)
        else:
            open_exec_stats = {
                "skipped": True,
                "skipReason": "total_pnl_below_profit_threshold",
                "threshold": PNL_SKIP_THRESHOLD,
                "totalPnl": total_pnl_early,
            }

        max_drawdown = None
        sharpe = None
        _progress("  - fetching pnl curve")
        pnl_series = fetch_user_pnl_series(
            session,
            cfg.address,
            interval=USER_PNL_METRICS_INTERVAL,
            fidelity=USER_PNL_METRICS_FIDELITY,
        )
        if pnl_series:
            max_drawdown, dd_usd, dd_peak, sharpe = compute_pnl_drawdown_sharpe(
                pnl_series,
                annualization_periods=730.0,
            )

        ulcer_index = None
        equity_r2 = compute_equity_r_squared(pnl_series) if pnl_series else None

        _progress("  - computing metrics")
        metrics, confidence = compute_metrics_snapshot(
            positions=pos_norm,
            min_usd=cfg.min_usd,
            max_drawdown=max_drawdown,
            sharpe=sharpe,
        )
        final_pnl = float(pnl_series[-1][1]) if pnl_series else float(metrics.get("total_pnl") or 0.0)
        if pnl_series:
            ulcer_index = compute_ulcer_index(pnl_series, total_pnl=final_pnl)
        if isinstance(metrics.get("details"), dict):
            metrics["details"]["userPnlCompatVersion"] = USER_PNL_METRICS_COMPAT_VERSION
            metrics["details"]["pnlCurvePoints"] = len(pnl_series) if pnl_series else 0
            metrics["details"]["pnlCurveInterval"] = USER_PNL_METRICS_INTERVAL
            metrics["details"]["pnlCurveFidelity"] = USER_PNL_METRICS_FIDELITY
            metrics["details"]["pnlCurveAnnualizationPeriods"] = 730.0 if pnl_series else None
            if pnl_series:
                metrics["details"]["pnlCurveLast"] = pnl_series[-1]
                metrics["details"]["maxDrawdownUsd"] = dd_usd
                metrics["details"]["drawdownPeakPnlUsd"] = dd_peak
            metrics["details"]["positionBased"] = pos_stats
            metrics["details"]["ulcerIndex"] = ulcer_index
            metrics["details"]["equityR2"] = equity_r2
            metrics["details"]["positionEdge"] = edge_stats.get("details")
            metrics["details"]["openExecution"] = open_exec_stats
            metrics["details"]["open_positions_analyzed"] = open_exec_stats.get("open_positions_analyzed", 0)
            metrics["details"]["open_positions_missing_book"] = open_exec_stats.get("open_positions_missing_book")
            metrics["details"]["open_positions_missing_settlement"] = open_exec_stats.get("open_positions_missing_settlement")
            metrics["details"]["closedSync"] = closed_sync_info

        metrics["ulcer_index"] = ulcer_index
        metrics["equity_r2"] = equity_r2
        metrics["avg_open_top5_depth_usd"] = (
            open_exec_stats.get("avg_open_top5_depth_usd")
            if total_pnl_early >= PNL_SKIP_THRESHOLD
            else None
        )
        metrics["avg_open_settlement_days"] = (
            open_exec_stats.get("avg_open_settlement_days")
            if total_pnl_early >= PNL_SKIP_THRESHOLD
            else None
        )
        metrics["current_position_value_usd"] = pos_stats.get("current_position_value_usd")
        metrics["total_trades"] = pos_stats.get("total_trades")
        metrics["winning_trades"] = pos_stats.get("winning_trades")
        metrics["losing_trades"] = pos_stats.get("losing_trades")
        metrics["win_rate"] = pos_stats.get("win_rate")
        metrics["avg_trade_price"] = pos_stats.get("avg_trade_price")
        metrics["realized_edge_score"] = edge_stats.get("realized_edge_score")
        upsert_address_metrics(conn, cfg.address, metrics, confidence, source_tag=cfg.source_tag)

        payload = {
            "address": cfg.address,
            "rawPositions": len(pos_norm),
            "confidence": confidence,
            "metrics": {k: v for k, v in metrics.items() if k != "details"},
            "details": metrics.get("details") if cfg.debug else None,
            "positionsDebug": positions_debug,
        }
        return payload
    finally:
        session.close()
        conn.close()


def main(argv: Sequence[str]) -> int:
    cfg = parse_args(argv)
    payload = compute_and_save_metrics(cfg)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
