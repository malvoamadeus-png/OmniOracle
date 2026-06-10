"""鏋勫缓鎸?leader 鍦板潃褰掑洜鐨勭泩浜忓揩鐓э紙宸插疄鐜?+ 鏈疄鐜帮級.

杈撳嚭鍒版湰鍦?SQLite:
- ct_leader_summary
- ct_leader_market_pnl
"""

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGES_DIR = _PACKAGE_DIR.parent
_PROJECT_ROOT = _PACKAGE_DIR.parents[2]
for _path in (str(_PROJECT_ROOT), str(_PACKAGES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from copytrade.polymarket_public_api import (  # noqa: E402
    GAMMA_API,
    DATA_API,
    fetch_midpoint,
    http_get_json,
    try_midpoints_batch,
)

from copytrade.account_config import ACCOUNTS_DIR, load_single_account  # noqa: E402
from copytrade.db import CopyTradeDB  # noqa: E402
from copytrade.paths import (  # noqa: E402
    DEFAULT_DB_PATH,
    DOTENV_PATH,
    PACKAGE_DIR,
    PROJECT_ROOT,
    ROOT_METRICS_DB_PATH,
    SUPABASE_SYNC_SCRIPT,
    ensure_import_paths,
)

ensure_import_paths()

ROOT = PROJECT_ROOT
_SCRIPT_DIR = PACKAGE_DIR
SYNC_SCRIPT = SUPABASE_SYNC_SCRIPT

_t0 = time.monotonic()


def _log(msg: str) -> None:
    elapsed = time.monotonic() - _t0
    sys.stderr.write(f"[snapshot +{elapsed:.1f}s] {msg}\n")
    sys.stderr.flush()


@contextmanager
def _snapshot_db_lock(db_path: str):
    lock_path = Path(db_path).with_suffix(Path(db_path).suffix + ".snapshot.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                acquired = False
        if acquired:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()).encode("ascii"))
            lock_file.flush()
        yield acquired
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _parse_datetime_utc(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.replace(".", "", 1).lstrip("+-").isdigit():
        try:
            ts_value = float(raw)
            if abs(ts_value) > 1e12:
                ts_value /= 1000.0
            return datetime.fromtimestamp(ts_value, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(str(value), fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _json_list(value: Any) -> Optional[List[Any]]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(parsed, list):
            return parsed
    return None


def _extract_market_resolution_price(market: Dict[str, Any], token_id: str) -> Optional[float]:
    clob_ids = _json_list(market.get("clobTokenIds"))
    outcome_prices = _json_list(market.get("outcomePrices"))
    if not clob_ids or not outcome_prices:
        return None
    for i, cid in enumerate(clob_ids):
        if str(cid) != str(token_id):
            continue
        if i >= len(outcome_prices):
            continue
        try:
            return float(outcome_prices[i])
        except (ValueError, TypeError):
            return None
    return None


def _extract_market_settlement_time(market: Dict[str, Any]) -> Optional[str]:
    for key in (
        "closedTime",
        "closedTimeIso",
        "umaEndDate",
        "umaEndDateIso",
        "endDate",
        "endDateIso",
    ):
        parsed = _parse_datetime_utc(market.get(key))
        if parsed:
            return parsed
    return None


def _extract_closed_market_settlement_time(market: Dict[str, Any]) -> Optional[str]:
    if not bool(market.get("closed")):
        return None
    return _extract_market_settlement_time(market)


def _normalize_condition_id(value: Any) -> str:
    return str(value or "").strip().lower()


def _build_token_resolution_context_map(rows: List[Any]) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        src = row if isinstance(row, dict) else dict(row)
        token_id = str(src.get("token_id") or "").strip()
        if not token_id:
            continue
        ctx = out.setdefault(token_id, {})
        market_slug = str(src.get("market_slug") or "").strip()
        condition_id = _normalize_condition_id(src.get("condition_id"))
        if market_slug and not ctx.get("market_slug"):
            ctx["market_slug"] = market_slug
        if condition_id and not ctx.get("condition_id"):
            ctx["condition_id"] = condition_id
    return out


def _fetch_market_resolution_info(
    session: requests.Session, token_id: str
) -> Dict[str, Any]:
    """Fetch resolution state for one token from Gamma markets API."""
    out: Dict[str, Any] = {
        "resolution_price": None,
        "market_closed": False,
        "settlement_time": None,
    }
    if not token_id:
        return out
    try:
        data = http_get_json(
            session,
            f"{GAMMA_API}/markets",
            params={"clob_token_ids": token_id, "limit": 1},
            timeout_s=6.0,
            max_retries=1,
        )
    except BaseException:
        return out
    if not isinstance(data, list) or not data:
        return out
    market = None
    for row in data:
        if not isinstance(row, dict):
            continue
        clob_ids = _json_list(row.get("clobTokenIds")) or []
        if any(str(cid or "").strip() == str(token_id) for cid in clob_ids):
            market = row
            break
    if not isinstance(market, dict):
        return out

    out["market_closed"] = bool(market.get("closed"))
    out["settlement_time"] = _extract_closed_market_settlement_time(market)
    if out["market_closed"]:
        out["resolution_price"] = _extract_market_resolution_price(market, token_id)
    return out


def _event_slug_candidates(market_slug: str) -> List[str]:
    market_slug = str(market_slug or "").strip()
    if not market_slug:
        return []

    parts = [part for part in market_slug.split("-") if part]
    if not parts:
        return []

    out: List[str] = []
    seen = set()

    def add(candidate: str) -> None:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)

    add(market_slug)

    trim_idx = len(parts)
    while trim_idx > 1 and parts[trim_idx - 1].isdigit():
        trim_idx -= 1
        add("-".join(parts[:trim_idx]))

    for end in range(len(parts) - 1, 1, -1):
        add("-".join(parts[:end]))

    return out


def _fetch_event_resolution_info(
    session: requests.Session,
    market_slug: str,
    *,
    expected_condition_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Fetch exact-market resolution data for all tokens via Gamma events API."""
    out: Dict[str, Dict[str, Any]] = {}
    market_slug = str(market_slug or "").strip()
    if not market_slug:
        return out

    expected_condition_id = _normalize_condition_id(expected_condition_id)
    for event_slug in _event_slug_candidates(market_slug):
        try:
            data = http_get_json(
                session,
                f"{GAMMA_API}/events",
                params={"slug": event_slug, "limit": 1},
                timeout_s=8.0,
                max_retries=1,
            )
        except BaseException:
            continue

        event_row: Optional[Dict[str, Any]] = None
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and str(row.get("slug") or "").strip() == event_slug:
                    event_row = row
                    break
        elif isinstance(data, dict) and str(data.get("slug") or "").strip() == event_slug:
            event_row = data
        if not isinstance(event_row, dict):
            continue

        exact_market: Optional[Dict[str, Any]] = None
        markets = event_row.get("markets")
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, dict) and str(market.get("slug") or "").strip() == market_slug:
                    exact_market = market
                    break
            if exact_market is None and expected_condition_id:
                for market in markets:
                    if (
                        isinstance(market, dict)
                        and _normalize_condition_id(market.get("conditionId")) == expected_condition_id
                    ):
                        exact_market = market
                        break
        if not isinstance(exact_market, dict):
            continue

        market_condition_id = _normalize_condition_id(exact_market.get("conditionId"))
        if expected_condition_id and market_condition_id and market_condition_id != expected_condition_id:
            continue

        if not bool(exact_market.get("closed")):
            continue
        settlement_time = _extract_market_settlement_time(exact_market)
        if not settlement_time:
            continue

        clob_ids = _json_list(exact_market.get("clobTokenIds"))
        outcome_prices = _json_list(exact_market.get("outcomePrices"))
        if not clob_ids or not outcome_prices:
            continue

        resolved_by_token: Dict[str, Dict[str, Any]] = {}
        for i, cid in enumerate(clob_ids):
            if i >= len(outcome_prices):
                continue
            try:
                price = float(outcome_prices[i])
            except (ValueError, TypeError):
                continue
            token_id = str(cid or "").strip()
            if not token_id:
                continue
            resolved_by_token[token_id] = {
                "resolution_price": price,
                "market_closed": True,
                "settlement_time": settlement_time,
                "market_slug": str(exact_market.get("slug") or "").strip() or market_slug,
                "condition_id": market_condition_id,
            }
        if resolved_by_token:
            return resolved_by_token
    return out


def _fetch_resolution_price(
    session: requests.Session, condition_id: str, token_id: str
) -> Optional[float]:
    """Backward-compatible wrapper used by analytics helpers."""
    _ = condition_id
    info = _fetch_market_resolution_info(session, token_id)
    price = info.get("resolution_price")
    return float(price) if isinstance(price, (int, float)) else None


def _fetch_onchain_positions(session: requests.Session, address: str) -> Dict[str, Dict[str, Any]]:
    """鏌ヨ Data API 鑾峰彇閾句笂鎵€鏈夋寔浠撶殑瀹屾暣鏁版嵁.

    杩斿洖 {token_id: {pnl, cash_pnl, realized_pnl, size, avg_price,
                     initial_value, current_value, condition_id, slug, redeemable}}
    """
    positions: Dict[str, Dict[str, Any]] = {}
    offset = 0
    limit = 200
    while True:
        try:
            data = http_get_json(
                session,
                f"{DATA_API}/positions",
                params={"user": address, "sizeThreshold": "0", "limit": limit, "offset": offset},
                timeout_s=15.0,
                max_retries=2,
            )
        except Exception:
            break
        if not isinstance(data, list) or not data:
            break
        for p in data:
            if not isinstance(p, dict):
                continue
            asset = str(p.get("asset") or p.get("token_id") or "")
            if not asset:
                continue
            size = float(p.get("size") or 0)
            cash_pnl = float(p.get("cashPnl") or 0)
            realized_pnl = float(p.get("realizedPnl") or 0)
            initial_value = float(p.get("initialValue") or 0)
            current_value = float(p.get("currentValue") or 0)
            # Prefer current-initial for unrealized PnL; fallback to cashPnl only when value fields are absent.
            unrealized_pnl = current_value - initial_value
            if abs(current_value) < 1e-12 and abs(initial_value) < 1e-12:
                unrealized_pnl = cash_pnl
            positions[asset] = {
                "pnl": cash_pnl,
                "cash_pnl": cash_pnl,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "size": size,
                "avg_price": float(p.get("avgPrice") or 0),
                "initial_value": initial_value,
                "current_value": current_value,
                "condition_id": str(p.get("conditionId") or ""),
                "slug": str(p.get("slug") or ""),
                "outcome": str(p.get("outcome") or p.get("title") or p.get("name") or ""),
                "redeemable": bool(p.get("redeemable")),
            }
        if len(data) < limit:
            break
        offset += limit
    return positions


def _build_token_leader_map(
    db: CopyTradeDB, account_name: str,
) -> Dict[str, List[Tuple[str, float]]]:
    """鏋勫缓 {token_id: [(leader_address, cost_weight)]} 鏄犲皠.

    cost_weight 鏄 leader 鍦ㄦ token 涓婄殑涔板叆鎴愭湰鍗犳瘮銆?    """
    rows = db.conn.execute(
        "SELECT token_id, leader_address, our_price, our_size, our_usd, "
        "filled_size_actual FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='open' "
        "AND our_size > 0 AND account_name=?",
        (account_name,),
    ).fetchall()

    # token_id -> leader -> total_cost
    token_leader_cost: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in rows:
        tid = str(r["token_id"] or "")
        leader = str(r["leader_address"] or "").lower()
        if not tid or not leader:
            continue
        price = float(r["our_price"] or 0)
        size = float(r["our_size"] or 0) or float(r["filled_size_actual"] or 0)
        # Prefer remaining cost basis (our_usd) for open attribution weights.
        cost = float(r["our_usd"] or 0)
        if cost <= 0:
            cost = price * size
        if cost > 0:
            token_leader_cost[tid][leader] += cost

    result: Dict[str, List[Tuple[str, float]]] = {}
    for tid, leader_costs in token_leader_cost.items():
        total = sum(leader_costs.values())
        if total <= 0:
            continue
        result[tid] = [(leader, cost / total) for leader, cost in leader_costs.items()]
    return result


def _attribute_open_pnl(
    chain_positions: Dict[str, Dict[str, Any]],
    token_leader_map: Dict[str, List[Tuple[str, float]]],
) -> Tuple[Dict[str, Dict[str, float]], float]:
    """褰掑洜 open positions 鐨?PnL 鍒?leader.

    杩斿洖 (leader_pnl: {leader: {realized, unrealized}}, unattributed_pnl)
    """
    leader_pnl: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"realized": 0.0, "unrealized": 0.0}
    )
    unattributed = 0.0

    for tid, pos in chain_positions.items():
        pnl = float(pos.get("unrealized_pnl") or 0.0)
        mapping = token_leader_map.get(tid)
        if not mapping:
            unattributed += pnl
            continue
        for leader, weight in mapping:
            leader_pnl[leader]["unrealized"] += pnl * weight

    return dict(leader_pnl), unattributed


def _load_realized_market_pnl(
    db: CopyTradeDB, account_name: str,
) -> Tuple[Dict[Tuple[str, str], Dict[str, Any]], Dict[str, float]]:
    """璇诲彇绱宸插疄鐜扮泩浜忥紙鎸?leader/market 鑱氬悎锛?"""
    strict_realized_filter_sql = _strict_realized_profit_filter_sql()
    rows = db.conn.execute(
        "SELECT "
        "LOWER(COALESCE(leader_address, '')) AS leader_address, "
        "COALESCE(NULLIF(condition_id, ''), NULLIF(token_id, ''), 'unknown_market') AS condition_key, "
        "COALESCE(NULLIF(market_slug, ''), "
        "         COALESCE(NULLIF(condition_id, ''), NULLIF(token_id, ''), 'unknown_market')) AS market_key, "
        "COALESCE(SUM(COALESCE(profit, 0)), 0) AS realized_pnl "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND account_name=? "
        f"AND {strict_realized_filter_sql} "
        "GROUP BY "
        "LOWER(COALESCE(leader_address, '')), "
        "COALESCE(NULLIF(condition_id, ''), NULLIF(token_id, ''), 'unknown_market'), "
        "COALESCE(NULLIF(market_slug, ''), "
        "         COALESCE(NULLIF(condition_id, ''), NULLIF(token_id, ''), 'unknown_market'))",
        (account_name,),
    ).fetchall()

    market_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    realized_by_leader: Dict[str, float] = defaultdict(float)
    for r in rows:
        leader = str(r["leader_address"] or "").lower().strip()
        condition_id = str(r["condition_key"] or "").strip()
        market_slug = str(r["market_key"] or "").strip()
        if not leader:
            continue
        if not condition_id:
            condition_id = "unknown_market"
        if not market_slug:
            market_slug = condition_id[:16]
        realized = float(r["realized_pnl"] or 0.0)

        key = (leader, condition_id)
        item = market_map.get(key)
        if item is None:
            item = {
                "leader_address": leader,
                "condition_id": condition_id,
                "account_name": account_name,
                "market_slug": market_slug,
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": 0.0,
            }
            market_map[key] = item
        item["total_realized_pnl"] += realized
        if not item.get("market_slug") and market_slug:
            item["market_slug"] = market_slug
        realized_by_leader[leader] += realized

    return market_map, dict(realized_by_leader)


def _attribute_closed_pnl(
    db: CopyTradeDB,
    account_name: str,
    closed_total: float,
    chain_token_ids: set,
) -> Dict[str, Dict[str, float]]:
    """鎸?exited 璁板綍鐨勬瘮渚嬪垎閰?closed PnL 鍒?leader.

    鍙湅 token_id 涓嶅湪閾句笂鐨?exited 璁板綍锛堝凡 redeem 鐨勪粨浣嶏級銆?    """
    rows = db.conn.execute(
        "SELECT leader_address, profit FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='exited' "
        "AND account_name=? AND profit IS NOT NULL",
        (account_name,),
    ).fetchall()

    # 鎸?leader 姹囨€?|profit| 浣滀负鏉冮噸
    leader_abs_profit: Dict[str, float] = defaultdict(float)
    for r in rows:
        leader = str(r["leader_address"] or "").lower()
        profit = float(r["profit"] or 0)
        if leader:
            leader_abs_profit[leader] += abs(profit)

    total_weight = sum(leader_abs_profit.values())
    leader_pnl: Dict[str, Dict[str, float]] = {}
    if total_weight > 0:
        for leader, weight in leader_abs_profit.items():
            share = closed_total * (weight / total_weight)
            leader_pnl[leader] = {"realized": share, "unrealized": 0.0}

    return leader_pnl


def _resolve_tokens_with_cache_and_live(
    db: CopyTradeDB,
    session: requests.Session,
    token_ids: List[str],
    *,
    token_context_map: Optional[Dict[str, Dict[str, str]]] = None,
    fetch_live_for_cached: bool = False,
    max_workers: int = 10,
) -> Tuple[Dict[str, float], Dict[str, str], set, int, int]:
    """Resolve token settlement prices from cache first, then Gamma API."""
    token_ids = sorted({str(tid or "").strip() for tid in token_ids if str(tid or "").strip()})
    if not token_ids:
        return {}, {}, set(), 0, 0

    prices: Dict[str, float] = {}
    settlement_times: Dict[str, str] = {}
    for i in range(0, len(token_ids), 500):
        batch = token_ids[i : i + 500]
        placeholders = ",".join("?" * len(batch))
        rows = db.conn.execute(
            f"SELECT token_id, resolution_price, settlement_time FROM ct_resolved_prices WHERE token_id IN ({placeholders})",
            batch,
        ).fetchall()
        for r in rows:
            tid = str(r["token_id"] or "").strip()
            if not tid:
                continue
            try:
                prices[tid] = float(r["resolution_price"])
            except (ValueError, TypeError):
                continue
            settlement_time = _parse_datetime_utc(r["settlement_time"])
            if settlement_time:
                settlement_times[tid] = settlement_time

    normalized_context_map: Dict[str, Dict[str, str]] = {}
    for tid, ctx in (token_context_map or {}).items():
        token_id = str(tid or "").strip()
        if not token_id or not isinstance(ctx, dict):
            continue
        market_slug = str(ctx.get("market_slug") or "").strip()
        condition_id = _normalize_condition_id(ctx.get("condition_id"))
        if market_slug or condition_id:
            normalized_context_map[token_id] = {
                "market_slug": market_slug,
                "condition_id": condition_id,
            }

    live_candidates = [
        tid
        for tid in token_ids
        if tid not in prices or tid not in settlement_times or fetch_live_for_cached
    ]
    live_resolved_prices: Dict[str, float] = {}
    live_settlement_times: Dict[str, str] = {}
    live_resolved = 0
    token_lookup_info: Dict[str, Dict[str, Any]] = {}
    if live_candidates:
        workers = max(1, min(max_workers, len(live_candidates)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {
                pool.submit(_fetch_market_resolution_info, session, tid): tid
                for tid in live_candidates
            }
            done = 0
            total = len(future_map)
            for fut in as_completed(future_map):
                done += 1
                tid = future_map[fut]
                info: Dict[str, Any]
                try:
                    info = fut.result()
                except Exception:
                    info = {}
                token_lookup_info[tid] = info
                settle = _parse_datetime_utc(info.get("settlement_time"))
                if settle:
                    settlement_times[tid] = settle
                    live_settlement_times[tid] = settle
                price = info.get("resolution_price")
                if isinstance(price, (int, float)):
                    p = float(price)
                    prices[tid] = p
                    live_resolved_prices[tid] = p
                    live_resolved += 1
                elif tid not in prices:
                    # Keep unresolved; caller decides whether to retain as open/pending.
                    pass
                if done == total or done % 200 == 0:
                    _log(f"[resolve] progress {done}/{total}, live_resolved={live_resolved}")

    fallback_by_slug: Dict[str, Dict[str, Any]] = {}
    for tid in live_candidates:
        if tid in prices and tid in settlement_times:
            continue
        ctx = normalized_context_map.get(tid) or {}
        market_slug = str(ctx.get("market_slug") or "").strip()
        if not market_slug:
            continue
        bucket = fallback_by_slug.setdefault(
            market_slug,
            {"token_ids": set(), "condition_ids": set()},
        )
        bucket["token_ids"].add(tid)
        condition_id = _normalize_condition_id(ctx.get("condition_id"))
        if condition_id:
            bucket["condition_ids"].add(condition_id)

    fallback_resolved = 0
    if fallback_by_slug:
        workers = max(1, min(max_workers, len(fallback_by_slug)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {}
            for market_slug, payload in fallback_by_slug.items():
                condition_ids = payload.get("condition_ids") or set()
                expected_condition_id = next(iter(condition_ids)) if len(condition_ids) == 1 else None
                future_map[
                    pool.submit(
                        _fetch_event_resolution_info,
                        session,
                        market_slug,
                        expected_condition_id=expected_condition_id,
                    )
                ] = market_slug

            for fut in as_completed(future_map):
                market_slug = future_map[fut]
                try:
                    resolution_map = fut.result()
                except Exception:
                    resolution_map = {}
                payload = fallback_by_slug.get(market_slug) or {}
                for tid in payload.get("token_ids") or set():
                    info = resolution_map.get(tid) or {}
                    settle = _parse_datetime_utc(info.get("settlement_time"))
                    if settle:
                        settlement_times[tid] = settle
                        live_settlement_times[tid] = settle
                    price = info.get("resolution_price")
                    if isinstance(price, (int, float)):
                        p = float(price)
                        prices[tid] = p
                        live_resolved_prices[tid] = p
                        prior_price = token_lookup_info.get(tid, {}).get("resolution_price")
                        if not isinstance(prior_price, (int, float)):
                            live_resolved += 1
                            fallback_resolved += 1

        if fallback_resolved:
            _log(
                f"[resolve] event fallback resolved={fallback_resolved} across {len(fallback_by_slug)} slugs"
            )

    if live_resolved_prices or live_settlement_times:
        save_prices = {
            tid: prices[tid]
            for tid in set(live_resolved_prices.keys()) | set(live_settlement_times.keys())
            if tid in prices
        }
        if save_prices:
            db.save_resolution_prices(save_prices, settlement_times=live_settlement_times)

    unresolved = {tid for tid in token_ids if tid not in prices}
    return prices, settlement_times, unresolved, live_resolved, len(live_candidates)


def _load_account_addresses() -> Dict[str, str]:
    from dotenv import load_dotenv

    load_dotenv(DOTENV_PATH)

    accounts_dir = Path(ACCOUNTS_DIR)
    account_addrs: Dict[str, str] = {}
    if accounts_dir.is_dir():
        for toml_file in accounts_dir.glob("*.toml"):
            if toml_file.name.startswith("_"):
                continue
            acct_name = toml_file.stem
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(toml_file, "rb") as f:
                data = tomllib.load(f)
            suffix = str(data.get("env_suffix") or "").strip()
            if not suffix:
                continue
            addr = os.environ.get(f"FUNDER_ADDRESS_{suffix}", "").strip()
            if addr:
                account_addrs[acct_name] = addr

    if not account_addrs:
        addr = os.environ.get("FUNDER_ADDRESS", "").strip()
        if addr:
            account_addrs["default"] = addr
    return account_addrs


_PENDING_SETTLEMENT_UNRESOLVED = "pending_settlement: not on chain and unresolved"
_PENDING_SETTLEMENT_MISSING_TIME = "pending_settlement: official time missing"
_PENDING_SETTLEMENT_FUTURE_TIME = "pending_settlement: official time in future"
_PENDING_SETTLEMENT_MISSING_COST = "pending_settlement: missing cost basis"
_PENDING_SETTLEMENT_MISSING_SIZE = "pending_settlement: missing position size"
_SETTLEMENT_EFFECTIVE_GRACE = timedelta(minutes=10)


def _set_pending_skip_reason(
    db: CopyTradeDB,
    *,
    trade_id: int,
    current_reason: Any,
    pending_reason: str,
    now_iso: str,
) -> None:
    if str(current_reason or "") == pending_reason:
        return
    db.conn.execute(
        "UPDATE ct_trades SET skip_reason=?, updated_at=? WHERE id=?",
        (pending_reason, now_iso, trade_id),
    )


def _resolution_pending_reason(
    *,
    resolution_price: Optional[float],
    settlement_time: Optional[str],
    buy_price: Optional[float] = None,
    size: Optional[float] = None,
) -> Optional[str]:
    parsed_settlement = _parse_datetime_utc(settlement_time)
    if resolution_price is None:
        return _PENDING_SETTLEMENT_UNRESOLVED
    if not parsed_settlement:
        return _PENDING_SETTLEMENT_MISSING_TIME
    if not _settlement_time_is_effective(parsed_settlement):
        return _PENDING_SETTLEMENT_FUTURE_TIME
    if buy_price is not None and buy_price <= 0:
        return _PENDING_SETTLEMENT_MISSING_COST
    if size is not None and size <= 0:
        return _PENDING_SETTLEMENT_MISSING_SIZE
    return None


def _settlement_time_is_effective(
    settlement_time: Optional[str],
    *,
    now_utc: Optional[datetime] = None,
) -> bool:
    parsed = _parse_datetime_utc(settlement_time)
    if not parsed:
        return False
    effective_now = now_utc or datetime.now(timezone.utc)
    return datetime.fromisoformat(parsed) <= effective_now + _SETTLEMENT_EFFECTIVE_GRACE


def reopen_future_resolution_exits(
    db: CopyTradeDB,
    *,
    now_utc: Optional[datetime] = None,
) -> int:
    """Reopen resolution exits that were incorrectly attributed to a future settlement date."""
    effective_now = now_utc or datetime.now(timezone.utc)
    cutoff_iso = (effective_now + _SETTLEMENT_EFFECTIVE_GRACE).isoformat()
    rows = db.conn.execute(
        "SELECT id, our_price, our_filled_price, our_size, our_usd, "
        "requested_price, requested_size, requested_usd, "
        "filled_size_actual, filled_usd_actual, official_settlement_at "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='exited' "
        "AND COALESCE(exit_usd, 0) <= ? "
        "AND COALESCE(NULLIF(official_settlement_at, ''), '') <> '' "
        "AND official_settlement_at > ?",
        (_LEG_EPS, cutoff_iso),
    ).fetchall()
    if not rows:
        return 0

    now_iso = effective_now.isoformat()
    reopened = 0
    for row in rows:
        size = max(
            _safe_float(row["filled_size_actual"]),
            _safe_float(row["our_size"]),
            _safe_float(row["requested_size"]),
        )
        usd = max(
            _safe_float(row["filled_usd_actual"]),
            _safe_float(row["our_usd"]),
            _safe_float(row["requested_usd"]),
        )
        if usd <= _LEG_EPS and size > _LEG_EPS:
            price = max(
                _safe_float(row["our_price"]),
                _safe_float(row["our_filled_price"]),
                _safe_float(row["requested_price"]),
            )
            if price > _LEG_EPS:
                usd = size * price
        if size <= _LEG_EPS:
            continue

        db.conn.execute(
            "UPDATE ct_trades SET exit_status='open', exit_price=NULL, exit_usd=NULL, "
            "exit_at=NULL, official_settlement_at=NULL, profit=NULL, "
            "our_size=?, our_usd=?, skip_reason=?, updated_at=? WHERE id=?",
            (
                size,
                usd,
                _PENDING_SETTLEMENT_FUTURE_TIME,
                now_iso,
                int(row["id"]),
            ),
        )
        reopened += 1

    if reopened:
        db.conn.commit()
    _log(f"[reopen-future-settlement] reopened={reopened}")
    return reopened


def repair_phantom_positions(db: CopyTradeDB) -> Dict[str, int]:
    """Recover previously zeroed phantom rows if settlement data is now available."""
    rows = db.conn.execute(
        "SELECT id, token_id, condition_id, market_slug, our_price, our_size, filled_size_actual, updated_at "
        "FROM ct_trades "
        "WHERE status='expired' AND our_side='BUY' AND exit_status='exited' "
        "AND skip_reason LIKE 'phantom:%'"
    ).fetchall()
    if not rows:
        return {
            "total": 0,
            "repaired": 0,
            "unresolved": 0,
            "missing_settlement_time": 0,
            "future_settlement_time": 0,
            "missing_price": 0,
            "missing_size": 0,
            "live_resolved": 0,
            "live_attempted": 0,
        }

    token_ids = [str(r["token_id"] or "").strip() for r in rows]
    token_context_map = _build_token_resolution_context_map(rows)
    session = requests.Session()
    prices, settlement_times, _unresolved_tokens, live_resolved, live_attempted = _resolve_tokens_with_cache_and_live(
        db,
        session,
        token_ids,
        token_context_map=token_context_map,
        fetch_live_for_cached=True,
    )

    now_iso = datetime.now(timezone.utc).isoformat()
    repaired = 0
    unresolved = 0
    missing_settlement_time = 0
    future_settlement_time = 0
    missing_price = 0
    missing_size = 0

    for r in rows:
        tid = str(r["token_id"] or "").strip()
        if not tid:
            unresolved += 1
            continue

        res_price = prices.get(tid)
        if res_price is None:
            unresolved += 1
            continue

        settlement_time = _parse_datetime_utc(settlement_times.get(tid))
        if not settlement_time:
            missing_settlement_time += 1
            continue
        if not _settlement_time_is_effective(settlement_time, now_utc=datetime.now(timezone.utc)):
            future_settlement_time += 1
            continue

        buy_price = float(r["our_price"] or 0.0)
        if buy_price <= 0:
            missing_price += 1
            continue

        size = float(r["filled_size_actual"] or 0.0)
        if size <= 0:
            size = float(r["our_size"] or 0.0)
        if size <= 0:
            missing_size += 1
            continue

        profit = (res_price - buy_price) * size
        db.conn.execute(
            "UPDATE ct_trades SET status='filled', exit_status='exited', "
            "exit_price=?, exit_at=?, official_settlement_at=?, profit=?, our_size=0, our_usd=0, "
            "skip_reason='repaired: resolved after phantom', updated_at=? WHERE id=?",
            (res_price, settlement_time, settlement_time, profit, now_iso, r["id"]),
        )
        repaired += 1

    if repaired:
        db.conn.commit()

    stats = {
        "total": len(rows),
        "repaired": repaired,
        "unresolved": unresolved,
        "missing_settlement_time": missing_settlement_time,
        "future_settlement_time": future_settlement_time,
        "missing_price": missing_price,
        "missing_size": missing_size,
        "live_resolved": live_resolved,
        "live_attempted": live_attempted,
    }
    _log(
        "[repair-phantom] total={total} repaired={repaired} unresolved={unresolved} "
        "missing_settlement_time={missing_settlement_time} "
        "future_settlement_time={future_settlement_time} "
        "missing_price={missing_price} missing_size={missing_size} "
        "live_resolved={live_resolved}/{live_attempted}".format(**stats)
    )
    return stats


def reconcile_redeemed_positions(db: CopyTradeDB) -> int:
    """Mark missing-on-chain open trades as exited only when settlement is known."""
    account_addrs = _load_account_addresses()

    if not account_addrs:
        _log("[reconcile] FUNDER_ADDRESS not found, skip")
        return 0

    session = requests.Session()
    total_marked = 0
    total_pending = 0

    for acct_name, address in account_addrs.items():
        open_rows = db.conn.execute(
            "SELECT id, token_id, condition_id, market_slug, our_price, our_size, our_usd, "
            "filled_size_actual, skip_reason FROM ct_trades "
            "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='open' "
            "AND our_size > 0 AND account_name=?",
            (acct_name,),
        ).fetchall()
        if not open_rows:
            continue

        _log(f"[reconcile] {acct_name}: fetch on-chain positions ({address[:10]}...)")
        onchain_positions = _fetch_onchain_positions(session, address)
        onchain_tokens = set(onchain_positions.keys())

        missing_rows = [dict(r) for r in open_rows if str(r["token_id"] or "") not in onchain_tokens]
        if not missing_rows:
            _log(f"[reconcile] {acct_name}: all open trades still on chain")
            continue

        token_ids = [str(r["token_id"] or "").strip() for r in missing_rows]
        token_context_map = _build_token_resolution_context_map(missing_rows)
        prices, settlement_times, unresolved_tokens, live_resolved, live_attempted = _resolve_tokens_with_cache_and_live(
            db,
            session,
            token_ids,
            token_context_map=token_context_map,
            fetch_live_for_cached=False,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        marked = 0
        pending = 0
        for trade in missing_rows:
            tid = str(trade["token_id"] or "").strip()
            if not tid:
                pending += 1
                continue

            buy_price = float(trade["our_price"] or 0.0)
            size = float(trade["our_size"] or 0.0)
            if size <= 0:
                size = float(trade["filled_size_actual"] or 0.0)
            res_price = prices.get(tid)
            settlement_time = _parse_datetime_utc(settlement_times.get(tid))
            pending_reason = _resolution_pending_reason(
                resolution_price=res_price,
                settlement_time=settlement_time,
                buy_price=buy_price,
                size=size,
            )
            if pending_reason:
                _set_pending_skip_reason(
                    db,
                    trade_id=int(trade["id"]),
                    current_reason=trade.get("skip_reason"),
                    pending_reason=pending_reason,
                    now_iso=now_iso,
                )
                pending += 1
                continue

            profit = (float(res_price) - buy_price) * size
            db.conn.execute(
                "UPDATE ct_trades SET status='filled', exit_status='exited', exit_price=?, "
                "exit_at=?, official_settlement_at=?, profit=?, our_size=0, our_usd=0, "
                "skip_reason=NULL, updated_at=? WHERE id=?",
                (res_price, settlement_time, settlement_time, profit, now_iso, trade["id"]),
            )
            marked += 1

        if marked or pending:
            db.conn.commit()

        _log(
            f"[reconcile] {acct_name}: onchain_tokens={len(onchain_tokens)} missing={len(missing_rows)} "
            f"marked={marked} pending={pending} unresolved_tokens={len(unresolved_tokens)} "
            f"live_resolved={live_resolved}/{live_attempted}"
        )
        total_marked += marked
        total_pending += pending

    if total_pending:
        _log(f"[reconcile] pending unresolved trades kept open: {total_pending}")

    return total_marked


def backfill_resolution_exit_settlement_times(
    db: CopyTradeDB,
    *,
    now_utc: Optional[datetime] = None,
) -> int:
    """Backfill official settlement time onto resolution exits discovered late."""
    effective_now = now_utc or datetime.now(timezone.utc)
    cutoff_iso = (effective_now + _SETTLEMENT_EFFECTIVE_GRACE).isoformat()
    rows = db.conn.execute(
        "SELECT id, token_id, condition_id, market_slug, official_settlement_at, exit_at "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='exited' "
        "AND COALESCE(exit_usd, 0) <= ? "
        "AND COALESCE(NULLIF(token_id, ''), '') <> '' "
        "AND ("
        "COALESCE(NULLIF(official_settlement_at, ''), '') = '' "
        "OR COALESCE(NULLIF(exit_at, ''), '') = '' "
        "OR exit_at <> official_settlement_at "
        "OR official_settlement_at > ?"
        ")",
        (_LEG_EPS, cutoff_iso),
    ).fetchall()
    if not rows:
        _log("[settlement-backfill] rows=0 updated=0 live_resolved=0/0")
        return 0

    token_ids = [str(r["token_id"] or "").strip() for r in rows]
    token_context_map = _build_token_resolution_context_map(rows)
    session = requests.Session()
    try:
        _prices, settlement_times, _unresolved_tokens, live_resolved, live_attempted = _resolve_tokens_with_cache_and_live(
            db,
            session,
            token_ids,
            token_context_map=token_context_map,
            fetch_live_for_cached=True,
        )
    finally:
        session.close()

    now_iso = effective_now.isoformat()
    updated = 0
    for row in rows:
        tid = str(row["token_id"] or "").strip()
        if not tid:
            continue
        settlement_time = _parse_datetime_utc(settlement_times.get(tid))
        if not settlement_time:
            continue
        if not _settlement_time_is_effective(settlement_time):
            continue
        current_official = _parse_datetime_utc(row["official_settlement_at"])
        current_exit_at = _parse_datetime_utc(row["exit_at"])
        if current_official == settlement_time and current_exit_at == settlement_time:
            continue
        db.conn.execute(
            "UPDATE ct_trades SET official_settlement_at=?, exit_at=?, updated_at=? WHERE id=?",
            (settlement_time, settlement_time, now_iso, row["id"]),
        )
        updated += 1

    if updated:
        db.conn.commit()

    _log(
        f"[settlement-backfill] rows={len(rows)} updated={updated} "
        f"live_resolved={live_resolved}/{live_attempted}"
    )
    return updated


_COMPARE_TZ = timezone(timedelta(hours=8))
_COMPARE_RETENTION_DAYS = 14
_COMPARE_LONG_DATED_DAYS = 365
_COMPARE_BASELINE_MINUTE = 5
_COMPARE_SCOPE_LEADER = "leader"
_COMPARE_SCOPE_OUR = "our"
_COMPARE_EXCLUDED_LONG_DATED = "excluded_long_dated"
_DAILY_COMPARE_MODE = "follow_period_cumulative_v3"


def _parse_compare_accounts(raw: str) -> List[str]:
    if not raw:
        return []
    out: List[str] = []
    seen = set()
    for part in str(raw).split(","):
        account_name = part.strip()
        if not account_name or account_name in seen:
            continue
        seen.add(account_name)
        out.append(account_name)
    return out


def _discover_compare_accounts(accounts_dir: Optional[Path] = None) -> List[str]:
    base_dir = Path(accounts_dir or ACCOUNTS_DIR)
    if not base_dir.is_dir():
        return []
    names: List[str] = []
    seen = set()
    for toml_file in sorted(base_dir.glob("*.toml")):
        if toml_file.name.startswith("_"):
            continue
        account_name = toml_file.stem.strip()
        if not account_name or account_name in seen:
            continue
        seen.add(account_name)
        names.append(account_name)
    return names


def _resolve_compare_accounts(raw: str, *, accounts_dir: Optional[Path] = None) -> List[str]:
    explicit = _parse_compare_accounts(raw)
    if explicit:
        return explicit
    return _discover_compare_accounts(accounts_dir=accounts_dir)


def _compare_now_utc8(now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(_COMPARE_TZ)


def _compare_date_key(now: Optional[datetime] = None) -> str:
    return _compare_now_utc8(now).strftime("%Y-%m-%d")


def _compare_now_from_date_key(date_key: str) -> Optional[datetime]:
    raw = str(date_key or "").strip()
    if not raw:
        return None
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"--compare-date must use YYYY-MM-DD, got: {raw}") from exc
    return datetime.fromisoformat(f"{raw}T12:00:00+08:00").astimezone(timezone.utc)


def _compare_baseline_dt_utc(date_key: str) -> datetime:
    local_dt = datetime.fromisoformat(f"{date_key}T00:00:00+08:00")
    local_dt = local_dt.replace(minute=_COMPARE_BASELINE_MINUTE)
    return local_dt.astimezone(timezone.utc)


def _compare_cutoff_date_key(now: Optional[datetime] = None) -> str:
    cutoff = _compare_now_utc8(now) - timedelta(days=_COMPARE_RETENTION_DAYS - 1)
    return cutoff.strftime("%Y-%m-%d")


def _compare_safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if out != out:
        return 0.0
    return out


def _compare_safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_COMPARE_CUMULATIVE_FIELDS = (
    "buy_fill_count",
    "buy_size",
    "buy_usd",
    "sell_fill_count",
    "sell_size",
    "sell_usd",
)


def _compare_cumulative_key(metric: str) -> str:
    return f"cumulative_{metric}"


def _compare_bod_cumulative_key(metric: str) -> str:
    return f"bod_cumulative_{metric}"


def _compare_metric_value(value: Any, metric: str) -> Any:
    if metric.endswith("fill_count"):
        return _compare_safe_int(value)
    return _compare_safe_float(value)


def _compare_load_cumulative_metrics_from_row(
    state: Dict[str, Any],
    row: Dict[str, Any],
    *,
    use_bod: bool,
) -> None:
    for metric in _COMPARE_CUMULATIVE_FIELDS:
        source_key = _compare_bod_cumulative_key(metric) if use_bod else _compare_cumulative_key(metric)
        value = row.get(source_key)
        if value is None:
            value = row.get(_compare_cumulative_key(metric))
        state[_compare_cumulative_key(metric)] = _compare_metric_value(value, metric)
        bod_value = row.get(_compare_bod_cumulative_key(metric))
        if bod_value is None:
            bod_value = value
        state[_compare_bod_cumulative_key(metric)] = _compare_metric_value(bod_value, metric)


def _compare_copy_current_cumulative_to_bod(state: Dict[str, Any]) -> None:
    for metric in _COMPARE_CUMULATIVE_FIELDS:
        state[_compare_bod_cumulative_key(metric)] = _compare_metric_value(
            state.get(_compare_cumulative_key(metric)),
            metric,
        )


def _compare_event_before_baseline(event_ts: Any, baseline_dt: datetime) -> bool:
    parsed = _parse_datetime_utc(event_ts)
    if not parsed:
        return False
    return datetime.fromisoformat(parsed) < baseline_dt


def _compare_avg(size: float, usd: float) -> Optional[float]:
    if size <= _LEG_EPS:
        return None
    return usd / size


def _compare_rel_diff(left: float, right: float) -> float:
    denom = max(abs(left), abs(right), _LEG_EPS)
    return abs(left - right) / denom


def _compare_leg_key(condition_id: Any, token_id: Any) -> Tuple[str, str]:
    return _normalize_condition_id(condition_id), str(token_id or "").strip()


def _compare_clean_outcome(value: Any) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"yes", "y"}:
        return "YES"
    if lowered in {"no", "n"}:
        return "NO"
    return raw.upper() if len(raw) <= 4 else raw


def _compare_upsert_meta(state: Dict[str, Any], *, market_slug: Any = None, outcome: Any = None) -> None:
    market_slug_text = str(market_slug or "").strip()
    if market_slug_text and not str(state.get("market_slug") or "").strip():
        state["market_slug"] = market_slug_text
    outcome_text = _compare_clean_outcome(outcome)
    current_outcome = _compare_clean_outcome(state.get("outcome"))
    if outcome_text and (not current_outcome or current_outcome in {"0", "1"}):
        state["outcome"] = outcome_text


def _make_compare_state(
    condition_id: Any,
    token_id: Any,
    *,
    market_slug: Any = None,
    outcome: Any = None,
) -> Dict[str, Any]:
    return {
        "condition_id": _normalize_condition_id(condition_id),
        "token_id": str(token_id or "").strip(),
        "market_slug": str(market_slug or "").strip() or None,
        "outcome": _compare_clean_outcome(outcome),
        "bod_open_size": 0.0,
        "bod_open_cost": 0.0,
        "bod_avg_open_price": 0.0,
        "bod_mark_price": None,
        "unrealized_bod": 0.0,
        "open_size": 0.0,
        "open_cost": 0.0,
        "realized_pnl": 0.0,
        "buy_fill_count": 0,
        "buy_size": 0.0,
        "buy_usd": 0.0,
        "sell_fill_count": 0,
        "sell_size": 0.0,
        "sell_usd": 0.0,
        "today_event_count": 0,
        "bod_cumulative_buy_fill_count": 0,
        "bod_cumulative_buy_size": 0.0,
        "bod_cumulative_buy_usd": 0.0,
        "bod_cumulative_sell_fill_count": 0,
        "bod_cumulative_sell_size": 0.0,
        "bod_cumulative_sell_usd": 0.0,
        "cumulative_buy_fill_count": 0,
        "cumulative_buy_size": 0.0,
        "cumulative_buy_usd": 0.0,
        "cumulative_sell_fill_count": 0,
        "cumulative_sell_size": 0.0,
        "cumulative_sell_usd": 0.0,
        "mark_price_now": None,
        "mark_price_source": None,
        "unrealized_now": 0.0,
        "status": "open",
        "exclusion_reason": None,
        "settlement_time": None,
        "last_event_ts": None,
        "_previous_mark_price": None,
    }


def _copy_compare_baseline_row(row: Dict[str, Any]) -> Dict[str, Any]:
    state = _make_compare_state(
        row.get("condition_id"),
        row.get("token_id"),
        market_slug=row.get("market_slug"),
        outcome=row.get("outcome"),
    )
    state["bod_open_size"] = _compare_safe_float(row.get("bod_open_size"))
    state["bod_open_cost"] = _compare_safe_float(row.get("bod_open_cost"))
    state["bod_avg_open_price"] = _compare_safe_float(row.get("bod_avg_open_price"))
    state["bod_mark_price"] = row.get("bod_mark_price")
    derived_unrealized_bod = None
    bod_mark_price = state.get("bod_mark_price")
    if bod_mark_price is not None:
        derived_unrealized_bod = (
            state["bod_open_size"] * _compare_safe_float(bod_mark_price) - state["bod_open_cost"]
        )
    if derived_unrealized_bod is None:
        state["unrealized_bod"] = _compare_safe_float(row.get("unrealized_bod"))
    else:
        state["unrealized_bod"] = derived_unrealized_bod
    state["open_size"] = state["bod_open_size"]
    state["open_cost"] = state["bod_open_cost"]
    _compare_load_cumulative_metrics_from_row(state, row, use_bod=True)
    state["status"] = row.get("status") or "open"
    state["settlement_time"] = row.get("settlement_time")
    state["exclusion_reason"] = row.get("exclusion_reason")
    state["_previous_mark_price"] = row.get("mark_price_now")
    return state


def _compare_state_avg_open_price(state: Dict[str, Any]) -> float:
    open_size = _compare_safe_float(state.get("open_size"))
    if open_size <= _LEG_EPS:
        return 0.0
    return _compare_safe_float(state.get("open_cost")) / open_size


def _compare_resolve_size_usd(size: Any, usd: Any, price: Any) -> Tuple[float, float]:
    resolved_size = _compare_safe_float(size)
    resolved_usd = _compare_safe_float(usd)
    resolved_price = _compare_safe_float(price)
    if resolved_size <= _LEG_EPS and resolved_usd > _LEG_EPS and resolved_price > _LEG_EPS:
        resolved_size = resolved_usd / resolved_price
    if resolved_usd <= _LEG_EPS and resolved_size > _LEG_EPS and resolved_price > _LEG_EPS:
        resolved_usd = resolved_size * resolved_price
    return resolved_size, resolved_usd


def _compare_apply_buy(
    state_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    condition_id: Any,
    token_id: Any,
    market_slug: Any,
    outcome: Any,
    size: Any,
    usd: Any,
    price: Any,
    event_ts: Any,
    collect_metrics: bool,
) -> None:
    key = _compare_leg_key(condition_id, token_id)
    state = state_map.get(key)
    if state is None:
        state = _make_compare_state(condition_id, token_id, market_slug=market_slug, outcome=outcome)
        state_map[key] = state
    _compare_upsert_meta(state, market_slug=market_slug, outcome=outcome)
    resolved_size, resolved_usd = _compare_resolve_size_usd(size, usd, price)
    if resolved_size <= _LEG_EPS or resolved_usd < -_LEG_EPS:
        return
    state["open_size"] = _compare_safe_float(state.get("open_size")) + resolved_size
    state["open_cost"] = _compare_safe_float(state.get("open_cost")) + resolved_usd
    state["cumulative_buy_fill_count"] = _compare_safe_int(state.get("cumulative_buy_fill_count")) + 1
    state["cumulative_buy_size"] = _compare_safe_float(state.get("cumulative_buy_size")) + resolved_size
    state["cumulative_buy_usd"] = _compare_safe_float(state.get("cumulative_buy_usd")) + resolved_usd
    state["status"] = "open"
    state["last_event_ts"] = _parse_datetime_utc(event_ts)
    if collect_metrics:
        state["buy_fill_count"] = int(state.get("buy_fill_count") or 0) + 1
        state["buy_size"] = _compare_safe_float(state.get("buy_size")) + resolved_size
        state["buy_usd"] = _compare_safe_float(state.get("buy_usd")) + resolved_usd
        state["today_event_count"] = _compare_safe_int(state.get("today_event_count")) + 1


def _compare_apply_sell_like(
    state_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    condition_id: Any,
    token_id: Any,
    market_slug: Any,
    outcome: Any,
    size: Any,
    usd: Any,
    price: Any,
    event_ts: Any,
    collect_metrics: bool,
    count_as_sell: bool,
    status_when_flat: str,
) -> None:
    key = _compare_leg_key(condition_id, token_id)
    state = state_map.get(key)
    if state is None:
        return
    _compare_upsert_meta(state, market_slug=market_slug, outcome=outcome)
    resolved_size, resolved_usd = _compare_resolve_size_usd(size, usd, price)
    if resolved_size <= _LEG_EPS:
        return
    open_size_before = _compare_safe_float(state.get("open_size"))
    open_cost_before = _compare_safe_float(state.get("open_cost"))
    executable_size = min(open_size_before, resolved_size)
    if executable_size > _LEG_EPS and open_size_before > _LEG_EPS:
        avg_before = open_cost_before / open_size_before
        matched_usd = resolved_usd
        if resolved_size > _LEG_EPS and executable_size + _LEG_EPS < resolved_size:
            matched_usd = resolved_usd * (executable_size / resolved_size)
        if collect_metrics:
            state["realized_pnl"] = _compare_safe_float(state.get("realized_pnl")) + (
                matched_usd - executable_size * avg_before
            )
        state["open_size"] = max(0.0, open_size_before - executable_size)
        state["open_cost"] = max(0.0, open_cost_before - executable_size * avg_before)
    if collect_metrics and count_as_sell:
        state["sell_fill_count"] = int(state.get("sell_fill_count") or 0) + 1
        state["sell_size"] = _compare_safe_float(state.get("sell_size")) + resolved_size
        state["sell_usd"] = _compare_safe_float(state.get("sell_usd")) + resolved_usd
    if count_as_sell:
        state["cumulative_sell_fill_count"] = _compare_safe_int(state.get("cumulative_sell_fill_count")) + 1
        state["cumulative_sell_size"] = _compare_safe_float(state.get("cumulative_sell_size")) + resolved_size
        state["cumulative_sell_usd"] = _compare_safe_float(state.get("cumulative_sell_usd")) + resolved_usd
    state["last_event_ts"] = _parse_datetime_utc(event_ts)
    if collect_metrics:
        state["today_event_count"] = _compare_safe_int(state.get("today_event_count")) + 1
    state["status"] = "open" if _compare_safe_float(state.get("open_size")) > _LEG_EPS else status_when_flat
    if _compare_safe_float(state.get("open_size")) <= _LEG_EPS:
        state["open_size"] = 0.0
        state["open_cost"] = 0.0


def _compare_apply_sell(
    state_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    condition_id: Any,
    token_id: Any,
    market_slug: Any,
    outcome: Any,
    size: Any,
    usd: Any,
    price: Any,
    event_ts: Any,
    collect_metrics: bool,
) -> None:
    _compare_apply_sell_like(
        state_map,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        outcome=outcome,
        size=size,
        usd=usd,
        price=price,
        event_ts=event_ts,
        collect_metrics=collect_metrics,
        count_as_sell=True,
        status_when_flat="sold",
    )


def _compare_apply_settlement(
    state_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    condition_id: Any,
    token_id: Any,
    market_slug: Any,
    outcome: Any,
    size: Any,
    price: Any,
    settlement_time: Any,
    collect_metrics: bool,
) -> None:
    resolved_size = _compare_safe_float(size)
    settlement_price = _compare_safe_float(price)
    _compare_apply_sell_like(
        state_map,
        condition_id=condition_id,
        token_id=token_id,
        market_slug=market_slug,
        outcome=outcome,
        size=resolved_size,
        usd=resolved_size * settlement_price,
        price=settlement_price,
        event_ts=settlement_time,
        collect_metrics=collect_metrics,
        count_as_sell=False,
        status_when_flat="settled",
    )
    key = _compare_leg_key(condition_id, token_id)
    state = state_map.get(key)
    if state is not None:
        state["settlement_time"] = _parse_datetime_utc(settlement_time)
        state["status"] = "settled" if _compare_safe_float(state.get("open_size")) <= _LEG_EPS else "open"


def _load_compare_account_leaders(
    account_names: List[str],
    *,
    accounts_dir: Optional[Path] = None,
) -> Dict[str, List[str]]:
    base_dir = str(accounts_dir or ACCOUNTS_DIR)
    out: Dict[str, List[str]] = {}
    for account_name in account_names:
        info = load_single_account(base_dir, account_name)
        leaders: List[str] = []
        seen = set()
        for raw in info.config.leader_addresses:
            leader = str(raw or "").strip().lower()
            if not leader or leader in seen:
                continue
            seen.add(leader)
            leaders.append(leader)
        out[account_name] = leaders
    return out


def _fetch_token_market_meta_batch(
    session: requests.Session,
    token_ids: List[str],
) -> Dict[str, Dict[str, Any]]:
    requested = [str(t or "").strip() for t in token_ids if str(t or "").strip()]
    if not requested:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    seen = set()
    unique = []
    for tid in requested:
        if tid in seen:
            continue
        seen.add(tid)
        unique.append(tid)

    chunk_size = 100
    for idx in range(0, len(unique), chunk_size):
        chunk = unique[idx : idx + chunk_size]
        try:
            data = http_get_json(
                session,
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": ",".join(chunk), "limit": max(1, len(chunk))},
                timeout_s=12.0,
                max_retries=1,
            )
        except BaseException:
            data = None
        markets: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            markets = [data]
        elif isinstance(data, list):
            markets = [row for row in data if isinstance(row, dict)]
        for market in markets:
            clob_ids = _json_list(market.get("clobTokenIds")) or []
            outcomes = _json_list(market.get("outcomes")) or []
            outcome_prices = _json_list(market.get("outcomePrices")) or []
            market_slug = str(market.get("slug") or "").strip() or None
            condition_id = _normalize_condition_id(market.get("conditionId"))
            market_closed = bool(market.get("closed"))
            settlement_time = _extract_closed_market_settlement_time(market)
            for pos, clob_id in enumerate(clob_ids):
                token_id = str(clob_id or "").strip()
                if token_id not in seen:
                    continue
                meta = out.setdefault(token_id, {})
                if market_slug and not meta.get("market_slug"):
                    meta["market_slug"] = market_slug
                if condition_id and not meta.get("condition_id"):
                    meta["condition_id"] = condition_id
                if settlement_time and not meta.get("settlement_time"):
                    meta["settlement_time"] = settlement_time
                meta["market_closed"] = market_closed
                if pos < len(outcomes) and isinstance(outcomes[pos], str) and not meta.get("outcome"):
                    meta["outcome"] = _compare_clean_outcome(outcomes[pos])
                if market_closed and pos < len(outcome_prices):
                    try:
                        meta["resolution_price"] = float(outcome_prices[pos])
                    except (TypeError, ValueError):
                        pass
    return out


def _fetch_midpoints_with_fallback(
    session: requests.Session,
    token_ids: List[str],
) -> Dict[str, float]:
    unique = []
    seen = set()
    for token_id in token_ids:
        tid = str(token_id or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        unique.append(tid)
    if not unique:
        return {}
    out: Dict[str, float] = {}
    chunk_size = 120
    for idx in range(0, len(unique), chunk_size):
        chunk = unique[idx : idx + chunk_size]
        batch = try_midpoints_batch(session, chunk)
        if batch:
            out.update(batch)
        missing = [tid for tid in chunk if tid not in out]
        for tid in missing:
            price = fetch_midpoint(session, tid)
            if price is None:
                continue
            out[tid] = float(price)
    return out


def _fetch_midpoints_with_cache(
    session: requests.Session,
    token_ids: List[str],
    *,
    midpoint_cache: Optional[Dict[str, Optional[float]]] = None,
) -> Dict[str, float]:
    unique: List[str] = []
    seen = set()
    for token_id in token_ids:
        tid = str(token_id or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        unique.append(tid)
    if not unique:
        return {}
    if midpoint_cache is None:
        return _fetch_midpoints_with_fallback(session, unique)

    missing = [tid for tid in unique if tid not in midpoint_cache]
    if missing:
        fetched = _fetch_midpoints_with_fallback(session, missing)
        for tid in missing:
            midpoint_cache[tid] = fetched.get(tid)

    return {
        tid: float(price)
        for tid in unique
        for price in [midpoint_cache.get(tid)]
        if isinstance(price, (int, float))
    }


def _group_compare_open_state_rows(
    rows: List[Dict[str, Any]],
) -> Tuple[
    Dict[Tuple[str, str, str], List[Dict[str, Any]]],
    Dict[str, List[Dict[str, Any]]],
]:
    by_pair_scope: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    leader_scope_by_leader: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        account_name = str(row.get("account_name") or "default")
        leader_address = str(row.get("leader_address") or "").lower()
        scope_kind = str(row.get("scope_kind") or "")
        grouped = by_pair_scope[(account_name, leader_address, scope_kind)]
        grouped.append(row)
        if scope_kind == _COMPARE_SCOPE_LEADER and leader_address not in leader_scope_by_leader:
            leader_scope_by_leader[leader_address] = grouped
    return by_pair_scope, leader_scope_by_leader


def _repair_compare_existing_baseline_rows(
    current_rows: List[Dict[str, Any]],
    previous_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not current_rows or not previous_rows:
        return current_rows

    previous_by_key = {
        (
            str(row.get("account_name") or "default"),
            str(row.get("leader_address") or "").lower(),
            str(row.get("scope_kind") or ""),
            str(row.get("condition_id") or ""),
            str(row.get("token_id") or ""),
        ): row
        for row in previous_rows
    }
    repaired_rows: List[Dict[str, Any]] = []
    repaired = 0
    for row in current_rows:
        item = dict(row)
        key = (
            str(item.get("account_name") or "default"),
            str(item.get("leader_address") or "").lower(),
            str(item.get("scope_kind") or ""),
            str(item.get("condition_id") or ""),
            str(item.get("token_id") or ""),
        )
        previous = previous_by_key.get(key)
        if previous is None:
            repaired_rows.append(item)
            continue

        same_open_position = (
            abs(_compare_safe_float(item.get("open_size")) - _compare_safe_float(previous.get("open_size"))) <= _LEG_EPS
            and abs(_compare_safe_float(item.get("open_cost")) - _compare_safe_float(previous.get("open_cost"))) <= _LEG_EPS
        )
        same_cumulative = (
            _compare_safe_int(item.get("cumulative_buy_fill_count"))
            == _compare_safe_int(previous.get("cumulative_buy_fill_count"))
            and abs(_compare_safe_float(item.get("cumulative_buy_size")) - _compare_safe_float(previous.get("cumulative_buy_size"))) <= _LEG_EPS
            and abs(_compare_safe_float(item.get("cumulative_buy_usd")) - _compare_safe_float(previous.get("cumulative_buy_usd"))) <= _LEG_EPS
            and _compare_safe_int(item.get("cumulative_sell_fill_count"))
            == _compare_safe_int(previous.get("cumulative_sell_fill_count"))
            and abs(_compare_safe_float(item.get("cumulative_sell_size")) - _compare_safe_float(previous.get("cumulative_sell_size"))) <= _LEG_EPS
            and abs(_compare_safe_float(item.get("cumulative_sell_usd")) - _compare_safe_float(previous.get("cumulative_sell_usd"))) <= _LEG_EPS
        )
        previous_mark_price = previous.get("mark_price_now")
        previous_unrealized = _compare_safe_float(previous.get("unrealized_now"))
        if (
            not same_open_position
            or not same_cumulative
            or previous_mark_price is None
            or abs(previous_unrealized) <= _LEG_EPS
        ):
            repaired_rows.append(item)
            continue

        current_mark_price = item.get("bod_mark_price")
        current_unrealized = _compare_safe_float(item.get("unrealized_bod"))
        if (
            abs(_compare_safe_float(current_mark_price) - _compare_safe_float(previous_mark_price)) <= _LEG_EPS
            and abs(current_unrealized - previous_unrealized) <= _LEG_EPS
        ):
            repaired_rows.append(item)
            continue

        item["bod_open_size"] = _compare_safe_float(previous.get("open_size"))
        item["bod_open_cost"] = _compare_safe_float(previous.get("open_cost"))
        item["bod_avg_open_price"] = _compare_safe_float(previous.get("avg_open_price"))
        item["bod_mark_price"] = previous_mark_price
        item["unrealized_bod"] = previous_unrealized
        if not item.get("last_event_ts"):
            item["last_event_ts"] = previous.get("last_event_ts")
        repaired += 1
        repaired_rows.append(item)

    if repaired:
        _log(f"[compare] repaired {repaired} existing baseline row(s) from previous-day carryover")
    return repaired_rows


def _compare_prev_date_key(date_key: str) -> str:
    prev_dt = datetime.strptime(date_key, "%Y-%m-%d") - timedelta(days=1)
    return prev_dt.strftime("%Y-%m-%d")


def _build_carryover_state_from_previous_day_rows(
    rows: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    state_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in rows:
        open_size = _compare_safe_float(row.get("open_size"))
        open_cost = _compare_safe_float(row.get("open_cost"))
        if open_size <= _LEG_EPS or open_cost < -_LEG_EPS:
            continue
        key = _compare_leg_key(row.get("condition_id"), row.get("token_id"))
        state = _make_compare_state(
            row.get("condition_id"),
            row.get("token_id"),
            market_slug=row.get("market_slug"),
            outcome=row.get("outcome"),
        )
        state["open_size"] = open_size
        state["open_cost"] = open_cost
        state["bod_open_size"] = open_size
        state["bod_open_cost"] = open_cost
        state["bod_avg_open_price"] = _compare_state_avg_open_price(state)
        state["bod_mark_price"] = row.get("mark_price_now")
        if state["bod_mark_price"] is None:
            state["bod_mark_price"] = row.get("bod_mark_price")
        if state["bod_mark_price"] is not None:
            state["unrealized_bod"] = (
                open_size * _compare_safe_float(state["bod_mark_price"]) - open_cost
            )
        else:
            state["unrealized_bod"] = _compare_safe_float(row.get("unrealized_now"))
            if abs(state["unrealized_bod"]) <= _LEG_EPS:
                state["unrealized_bod"] = _compare_safe_float(row.get("unrealized_bod"))
        _compare_load_cumulative_metrics_from_row(state, row, use_bod=False)
        _compare_copy_current_cumulative_to_bod(state)
        state["status"] = "open"
        state["settlement_time"] = row.get("settlement_time")
        state["exclusion_reason"] = row.get("exclusion_reason")
        state["last_event_ts"] = row.get("last_event_ts")
        state["_previous_mark_price"] = row.get("mark_price_now")
        state_map[key] = state
    return state_map


def _load_compare_trade_rows_for_pairs(
    db: CopyTradeDB,
    *,
    allowed_pairs: set[Tuple[str, str]],
    baseline_dt: Optional[datetime] = None,
    filled_only: bool = True,
) -> List[Dict[str, Any]]:
    if not allowed_pairs:
        return []
    accounts = sorted({account_name for account_name, _leader in allowed_pairs})
    leaders = sorted({leader for _account_name, leader in allowed_pairs})
    acct_placeholders = ",".join("?" * len(accounts))
    leader_placeholders = ",".join("?" * len(leaders))
    sql = (
        "SELECT account_name, leader_address, status, leader_side, our_side, leader_fill_key, leader_tx_hash, "
        "leader_price, leader_size, leader_usd, token_id, condition_id, market_slug, outcome, "
        "created_at, exit_status, exit_at, official_settlement_at, exit_price, exit_usd, "
        "filled_size_actual, filled_usd_actual, requested_size, requested_usd, our_price, our_usd "
        "FROM ct_trades "
        f"WHERE leader_side='BUY' AND account_name IN ({acct_placeholders}) "
        f"AND leader_address IN ({leader_placeholders})"
    )
    params: List[Any] = [*accounts, *leaders]
    if filled_only:
        sql += " AND status IN ('filled','partially_filled') AND our_side='BUY'"
    if baseline_dt is not None:
        sql += " AND ((created_at IS NOT NULL AND created_at >= ?) OR (exit_at IS NOT NULL AND exit_at >= ?))"
        baseline_iso = baseline_dt.isoformat()
        params.extend([baseline_iso, baseline_iso])
    rows = db.conn.execute(sql, params).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        pair = (str(item.get("account_name") or "default"), str(item.get("leader_address") or "").lower())
        if pair not in allowed_pairs:
            continue
        out.append(item)
    return out


def _group_compare_trade_rows_by_pair(
    rows: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    out: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        pair = (
            str(row.get("account_name") or "default"),
            str(row.get("leader_address") or "").lower(),
        )
        out[pair].append(row)
    return out


def _derive_compare_follow_start_by_pair(
    rows: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], datetime]:
    out: Dict[Tuple[str, str], datetime] = {}
    for row in rows:
        created_at = _parse_datetime_utc(row.get("created_at"))
        if not created_at:
            continue
        created_dt = datetime.fromisoformat(created_at)
        pair = (
            str(row.get("account_name") or "default"),
            str(row.get("leader_address") or "").lower(),
        )
        previous = out.get(pair)
        if previous is None or created_dt < previous:
            out[pair] = created_dt
    return out


def _build_leader_bod_states_from_activities(
    events: List[Dict[str, Any]],
    *,
    follow_start_dt: datetime,
    baseline_dt: datetime,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    state_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    ordered_events: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for event in events:
        ts_raw = _parse_datetime_utc(event.get("ts"))
        if not ts_raw:
            continue
        event_dt = datetime.fromisoformat(ts_raw)
        if event_dt < follow_start_dt or event_dt >= baseline_dt:
            continue
        side = str(event.get("kind") or "").upper()
        if side not in {"BUY", "SELL"}:
            continue
        ordered_events.append(
            (
                event_dt,
                0 if side == "BUY" else 2,
                {
                    "kind": side,
                    "condition_id": event.get("condition_id"),
                    "token_id": event.get("token_id"),
                    "market_slug": event.get("market_slug"),
                    "outcome": event.get("outcome"),
                    "size": event.get("size"),
                    "usd": event.get("usd"),
                    "price": event.get("price"),
                    "ts": ts_raw,
                },
            )
        )

    ordered_events.sort(key=lambda item: (item[0], item[1]))
    for _event_dt, _order, event in ordered_events:
        if event["kind"] == "BUY":
            _compare_apply_buy(
                state_map,
                condition_id=event["condition_id"],
                token_id=event["token_id"],
                market_slug=event.get("market_slug"),
                outcome=event.get("outcome"),
                size=event.get("size"),
                usd=event.get("usd"),
                price=event.get("price"),
                event_ts=event.get("ts"),
                collect_metrics=False,
            )
        else:
            _compare_apply_sell(
                state_map,
                condition_id=event["condition_id"],
                token_id=event["token_id"],
                market_slug=event.get("market_slug"),
                outcome=event.get("outcome"),
                size=event.get("size"),
                usd=event.get("usd"),
                price=event.get("price"),
                event_ts=event.get("ts"),
                collect_metrics=False,
            )

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, state in state_map.items():
        if _compare_safe_float(state.get("open_size")) <= _LEG_EPS:
            continue
        state["bod_open_size"] = _compare_safe_float(state.get("open_size"))
        state["bod_open_cost"] = _compare_safe_float(state.get("open_cost"))
        state["bod_avg_open_price"] = _compare_state_avg_open_price(state)
        _compare_copy_current_cumulative_to_bod(state)
        state["status"] = "open"
        out[key] = state
    return out


def _compare_trade_matches_activity(
    trade: Dict[str, Any],
    activity_event: Dict[str, Any],
) -> bool:
    if str(activity_event.get("kind") or "").upper() != "BUY":
        return False
    trade_tx = str(trade.get("leader_tx_hash") or "").strip().lower()
    activity_tx = str(activity_event.get("tx_hash") or "").strip().lower()
    trade_token = str(trade.get("token_id") or "").strip()
    activity_token = str(activity_event.get("token_id") or "").strip()
    if trade_tx and activity_tx and trade_tx == activity_tx and trade_token == activity_token:
        return True

    if trade_token != activity_token:
        return False

    trade_time = _parse_datetime_utc(trade.get("created_at"))
    activity_time = _parse_datetime_utc(activity_event.get("ts"))
    if not trade_time or not activity_time:
        return False
    trade_dt = datetime.fromisoformat(trade_time)
    activity_dt = datetime.fromisoformat(activity_time)
    if abs((trade_dt - activity_dt).total_seconds()) > 600:
        return False

    trade_usd = _compare_safe_float(trade.get("leader_usd"))
    activity_usd = _compare_safe_float(activity_event.get("usd"))
    if trade_usd > _LEG_EPS and activity_usd > _LEG_EPS and _compare_rel_diff(trade_usd, activity_usd) > 0.10:
        return False

    trade_size = _compare_safe_float(trade.get("leader_size"))
    activity_size = _compare_safe_float(activity_event.get("size"))
    if trade_size > _LEG_EPS and activity_size > _LEG_EPS and _compare_rel_diff(trade_size, activity_size) > 0.10:
        return False

    return True


def _build_leader_events_for_pair(
    *,
    leader_address: str,
    activities: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    buy_activity_by_token: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for activity in activities:
        side = str(activity.get("side") or "").upper()
        ts_raw = _parse_datetime_utc(activity.get("timestamp_utc"))
        if side not in {"BUY", "SELL"} or not ts_raw:
            continue
        event = {
            "kind": side,
            "condition_id": activity.get("condition_id"),
            "token_id": activity.get("token_id"),
            "market_slug": activity.get("market_slug"),
            "outcome": activity.get("outcome"),
            "size": activity.get("size"),
            "usd": activity.get("usd"),
            "price": activity.get("price"),
            "ts": ts_raw,
            "tx_hash": activity.get("tx_hash"),
            "source": "activity",
        }
        events.append(event)
        if side == "BUY":
            buy_activity_by_token[str(activity.get("token_id") or "").strip()].append(event)

    for trade in trades:
        trade_time = _parse_datetime_utc(trade.get("created_at"))
        token_id = str(trade.get("token_id") or "").strip()
        if not trade_time or not token_id:
            continue
        synthetic = {
            "kind": "BUY",
            "condition_id": trade.get("condition_id"),
            "token_id": token_id,
            "market_slug": trade.get("market_slug"),
            "outcome": trade.get("outcome"),
            "size": trade.get("leader_size") or trade.get("filled_size_actual") or trade.get("requested_size"),
            "usd": trade.get("leader_usd") or trade.get("filled_usd_actual") or trade.get("requested_usd") or trade.get("our_usd"),
            "price": trade.get("leader_price") or trade.get("our_price"),
            "ts": trade_time,
            "tx_hash": trade.get("leader_tx_hash"),
            "source": "trade_fallback",
        }
        resolved_size, resolved_usd = _compare_resolve_size_usd(
            synthetic.get("size"),
            synthetic.get("usd"),
            synthetic.get("price"),
        )
        if resolved_size <= _LEG_EPS or resolved_usd <= _LEG_EPS:
            continue
        candidates = buy_activity_by_token.get(token_id) or []
        if any(_compare_trade_matches_activity(trade, activity_event) for activity_event in candidates):
            continue
        events.append(synthetic)

    events.sort(
        key=lambda item: (
            datetime.fromisoformat(_parse_datetime_utc(item.get("ts")) or "1970-01-01T00:00:00+00:00"),
            0 if str(item.get("kind") or "").upper() == "BUY" else 2,
            str(item.get("source") or ""),
            str(item.get("tx_hash") or ""),
        )
    )
    return events


def _build_our_bod_states_from_trades(
    trades: List[Dict[str, Any]],
    *,
    baseline_dt: datetime,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    state_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    events: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for trade in trades:
        condition_id = trade.get("condition_id")
        token_id = trade.get("token_id")
        market_slug = trade.get("market_slug")
        outcome = trade.get("outcome")
        buy_time_raw = _parse_datetime_utc(trade.get("created_at"))
        if buy_time_raw:
            buy_dt = datetime.fromisoformat(buy_time_raw)
            if buy_dt < baseline_dt:
                events.append(
                    (
                        buy_dt,
                        0,
                        {
                            "kind": "BUY",
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "market_slug": market_slug,
                            "outcome": outcome,
                            "size": trade.get("filled_size_actual") or trade.get("requested_size"),
                            "usd": trade.get("filled_usd_actual") or trade.get("requested_usd") or trade.get("our_usd"),
                            "price": trade.get("our_price"),
                            "ts": buy_time_raw,
                        },
                    )
                )
        exit_time_raw = _parse_datetime_utc(trade.get("exit_at"))
        if not exit_time_raw or str(trade.get("exit_status") or "") != "exited":
            continue
        exit_dt = datetime.fromisoformat(exit_time_raw)
        if exit_dt >= baseline_dt:
            continue
        settle_raw = _parse_datetime_utc(trade.get("official_settlement_at"))
        size_value = trade.get("filled_size_actual") or trade.get("requested_size")
        if settle_raw and settle_raw == exit_time_raw:
            events.append(
                (
                    exit_dt,
                    2,
                    {
                        "kind": "SETTLE",
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "market_slug": market_slug,
                        "outcome": outcome,
                        "size": size_value,
                        "price": trade.get("exit_price"),
                        "ts": exit_time_raw,
                    },
                )
            )
        else:
            events.append(
                (
                    exit_dt,
                    2,
                    {
                        "kind": "SELL",
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "market_slug": market_slug,
                        "outcome": outcome,
                        "size": size_value,
                        "usd": trade.get("exit_usd"),
                        "price": trade.get("exit_price"),
                        "ts": exit_time_raw,
                    },
                )
            )

    events.sort(key=lambda item: (item[0], item[1]))
    for _event_dt, _order, event in events:
        if event["kind"] == "BUY":
            _compare_apply_buy(
                state_map,
                condition_id=event["condition_id"],
                token_id=event["token_id"],
                market_slug=event.get("market_slug"),
                outcome=event.get("outcome"),
                size=event.get("size"),
                usd=event.get("usd"),
                price=event.get("price"),
                event_ts=event.get("ts"),
                collect_metrics=False,
            )
        elif event["kind"] == "SELL":
            _compare_apply_sell(
                state_map,
                condition_id=event["condition_id"],
                token_id=event["token_id"],
                market_slug=event.get("market_slug"),
                outcome=event.get("outcome"),
                size=event.get("size"),
                usd=event.get("usd"),
                price=event.get("price"),
                event_ts=event.get("ts"),
                collect_metrics=False,
            )
        else:
            _compare_apply_settlement(
                state_map,
                condition_id=event["condition_id"],
                token_id=event["token_id"],
                market_slug=event.get("market_slug"),
                outcome=event.get("outcome"),
                size=event.get("size"),
                price=event.get("price"),
                settlement_time=event.get("ts"),
                collect_metrics=False,
            )

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for key, state in state_map.items():
        if _compare_safe_float(state.get("open_size")) <= _LEG_EPS:
            continue
        state["bod_open_size"] = _compare_safe_float(state.get("open_size"))
        state["bod_open_cost"] = _compare_safe_float(state.get("open_cost"))
        state["bod_avg_open_price"] = _compare_state_avg_open_price(state)
        _compare_copy_current_cumulative_to_bod(state)
        state["status"] = "open"
        out[key] = state
    return out


def _apply_previous_mark_as_bod(
    state: Dict[str, Any],
    previous_row: Dict[str, Any],
) -> bool:
    previous_mark_price = previous_row.get("mark_price_now")
    if previous_mark_price is None:
        return False
    open_size = _compare_safe_float(state.get("open_size"))
    open_cost = _compare_safe_float(state.get("open_cost"))
    if open_size <= _LEG_EPS or open_cost < -_LEG_EPS:
        return False
    state["bod_open_size"] = open_size
    state["bod_open_cost"] = open_cost
    state["bod_avg_open_price"] = _compare_state_avg_open_price(state)
    state["bod_mark_price"] = previous_mark_price
    state["unrealized_bod"] = open_size * _compare_safe_float(previous_mark_price) - open_cost
    _compare_copy_current_cumulative_to_bod(state)
    state["_preserve_bod_mark_price"] = True
    return True


def _build_leader_bootstrap_states_from_positions(
    positions: Dict[str, Dict[str, Any]],
    *,
    allowed_token_ids: Optional[set[str]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    state_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    restrict_tokens = allowed_token_ids is not None
    allowed = {
        str(token_id or "").strip()
        for token_id in (allowed_token_ids or set())
        if str(token_id or "").strip()
    }
    for token_id, position in positions.items():
        tid = str(token_id or "").strip()
        if not tid:
            continue
        if restrict_tokens and tid not in allowed:
            continue
        if bool(position.get("redeemable")):
            continue
        open_size = _compare_safe_float(position.get("size"))
        open_cost = _compare_safe_float(position.get("initial_value"))
        if open_size <= _LEG_EPS:
            continue
        key = _compare_leg_key(position.get("condition_id"), tid)
        state = _make_compare_state(
            position.get("condition_id"),
            tid,
            market_slug=position.get("slug"),
            outcome=position.get("outcome"),
        )
        state["open_size"] = open_size
        state["open_cost"] = max(0.0, open_cost)
        state["status"] = "open"
        state_map[key] = state
    return state_map


def _build_our_bootstrap_states_from_open_trades(
    trades: List[Dict[str, Any]],
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    state_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for trade in trades:
        token_id = str(trade.get("token_id") or "").strip()
        if not token_id:
            continue
        condition_id = trade.get("condition_id")
        key = _compare_leg_key(condition_id, token_id)
        state = state_map.get(key)
        if state is None:
            state = _make_compare_state(
                condition_id,
                token_id,
                market_slug=trade.get("market_slug"),
                outcome=trade.get("outcome"),
            )
            state_map[key] = state
        _compare_upsert_meta(state, market_slug=trade.get("market_slug"), outcome=trade.get("outcome"))
        open_size = _compare_safe_float(trade.get("our_size"))
        open_cost = _compare_safe_float(trade.get("our_usd"))
        if open_size <= _LEG_EPS:
            open_size = _compare_safe_float(trade.get("filled_size_actual")) or _compare_safe_float(trade.get("requested_size"))
        if open_cost <= _LEG_EPS:
            open_cost = (
                _compare_safe_float(trade.get("filled_usd_actual"))
                or _compare_safe_float(trade.get("requested_usd"))
                or _compare_safe_float(trade.get("our_usd"))
            )
        if open_size <= _LEG_EPS:
            continue
        state["open_size"] = _compare_safe_float(state.get("open_size")) + open_size
        state["open_cost"] = _compare_safe_float(state.get("open_cost")) + max(0.0, open_cost)
        state["status"] = "open"
    return state_map


def _build_our_compare_baseline_state(
    *,
    cached_rows: List[Dict[str, Any]],
    previous_rows: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
    baseline_dt: datetime,
    bootstrap_rows: Optional[List[Dict[str, Any]]] = None,
) -> Dict[Tuple[str, str], Dict[str, Any]]:
    previous_by_key = {
        _compare_leg_key(row.get("condition_id"), row.get("token_id")): row
        for row in previous_rows
    }
    if cached_rows:
        state_map = {
            _compare_leg_key(row.get("condition_id"), row.get("token_id")): _copy_compare_baseline_row(row)
            for row in cached_rows
        }
    elif previous_rows:
        state_map = _build_carryover_state_from_previous_day_rows(previous_rows)
    elif bootstrap_rows:
        state_map = _build_our_bootstrap_states_from_open_trades(bootstrap_rows)
    else:
        state_map = {}

    rebuilt_from_trades = _build_our_bod_states_from_trades(trades, baseline_dt=baseline_dt)
    for key, trade_state in rebuilt_from_trades.items():
        existing = state_map.get(key)
        if existing is not None:
            if trade_state.get("_previous_mark_price") is None:
                trade_state["_previous_mark_price"] = (
                    existing.get("_previous_mark_price")
                    if existing.get("_previous_mark_price") is not None
                    else existing.get("mark_price_now")
                )
            if not trade_state.get("settlement_time"):
                trade_state["settlement_time"] = existing.get("settlement_time")
            if not trade_state.get("exclusion_reason"):
                trade_state["exclusion_reason"] = existing.get("exclusion_reason")
        previous = previous_by_key.get(key)
        if previous is not None and _compare_event_before_baseline(
            trade_state.get("last_event_ts"),
            baseline_dt,
        ):
            _apply_previous_mark_as_bod(trade_state, previous)
        state_map[key] = trade_state
    return state_map


def _build_our_intraday_events(
    trades: List[Dict[str, Any]],
    *,
    baseline_dt: datetime,
    start_dt: Optional[datetime] = None,
) -> List[Tuple[datetime, int, Dict[str, Any]]]:
    floor_dt = start_dt or baseline_dt
    events: List[Tuple[datetime, int, Dict[str, Any]]] = []
    for trade in trades:
        condition_id = trade.get("condition_id")
        token_id = trade.get("token_id")
        market_slug = trade.get("market_slug")
        outcome = trade.get("outcome")
        buy_time_raw = _parse_datetime_utc(trade.get("created_at"))
        if buy_time_raw:
            buy_dt = datetime.fromisoformat(buy_time_raw)
            if buy_dt >= floor_dt:
                events.append(
                    (
                        buy_dt,
                        0,
                        {
                            "kind": "BUY",
                            "condition_id": condition_id,
                            "token_id": token_id,
                            "market_slug": market_slug,
                            "outcome": outcome,
                            "size": trade.get("filled_size_actual") or trade.get("requested_size"),
                            "usd": trade.get("filled_usd_actual") or trade.get("requested_usd") or trade.get("our_usd"),
                            "price": trade.get("our_price"),
                            "ts": buy_time_raw,
                        },
                    )
                )
        exit_time_raw = _parse_datetime_utc(trade.get("exit_at"))
        if not exit_time_raw or str(trade.get("exit_status") or "") != "exited":
            continue
        exit_dt = datetime.fromisoformat(exit_time_raw)
        if exit_dt < floor_dt:
            continue
        settle_raw = _parse_datetime_utc(trade.get("official_settlement_at"))
        size_value = trade.get("filled_size_actual") or trade.get("requested_size")
        if settle_raw and settle_raw == exit_time_raw:
            events.append(
                (
                    exit_dt,
                    2,
                    {
                        "kind": "SETTLE",
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "market_slug": market_slug,
                        "outcome": outcome,
                        "size": size_value,
                        "price": trade.get("exit_price"),
                        "ts": exit_time_raw,
                    },
                )
            )
        else:
            events.append(
                (
                    exit_dt,
                    2,
                    {
                        "kind": "SELL",
                        "condition_id": condition_id,
                        "token_id": token_id,
                        "market_slug": market_slug,
                        "outcome": outcome,
                        "size": size_value,
                        "usd": trade.get("exit_usd"),
                        "price": trade.get("exit_price"),
                        "ts": exit_time_raw,
                    },
                )
            )
    events.sort(key=lambda item: (item[0], item[1]))
    return events


def _seed_bod_marks_for_states(
    session: requests.Session,
    state_map: Dict[Tuple[str, str], Dict[str, Any]],
    *,
    midpoint_cache: Optional[Dict[str, Optional[float]]] = None,
) -> None:
    open_tokens = [
        state["token_id"]
        for state in state_map.values()
        if _compare_safe_float(state.get("open_size")) > _LEG_EPS
    ]
    prices = _fetch_midpoints_with_cache(
        session,
        open_tokens,
        midpoint_cache=midpoint_cache,
    )
    for state in state_map.values():
        open_size = _compare_safe_float(state.get("open_size"))
        if open_size <= _LEG_EPS:
            continue
        price = None
        if state.get("_preserve_bod_mark_price") and state.get("bod_mark_price") is not None:
            price = _compare_safe_float(state.get("bod_mark_price"))
        if price is None:
            price = prices.get(state["token_id"])
        if price is None:
            if state.get("bod_mark_price") is not None:
                price = _compare_safe_float(state.get("bod_mark_price"))
            elif state.get("_previous_mark_price") is not None:
                price = _compare_safe_float(state.get("_previous_mark_price"))
            else:
                price = _compare_state_avg_open_price(state)
        state["bod_mark_price"] = price
        state["unrealized_bod"] = open_size * _compare_safe_float(price) - _compare_safe_float(state.get("open_cost"))
        state["bod_open_size"] = open_size
        state["bod_open_cost"] = _compare_safe_float(state.get("open_cost"))
        state["bod_avg_open_price"] = _compare_state_avg_open_price(state)


def _determine_primary_gap_reason(row: Dict[str, Any]) -> str:
    if row.get("exclusion_reason"):
        return "excluded"
    if int(row.get("leader_buy_fill_count") or 0) != int(row.get("our_buy_fill_count") or 0):
        return "count_gap"
    if int(row.get("leader_sell_fill_count") or 0) != int(row.get("our_sell_fill_count") or 0):
        return "count_gap"
    leader_buy_usd = _compare_safe_float(row.get("leader_buy_usd"))
    our_buy_usd = _compare_safe_float(row.get("our_buy_usd"))
    leader_sell_usd = _compare_safe_float(row.get("leader_sell_usd"))
    our_sell_usd = _compare_safe_float(row.get("our_sell_usd"))
    if (
        (leader_buy_usd > _LEG_EPS or our_buy_usd > _LEG_EPS)
        and _compare_rel_diff(leader_buy_usd, our_buy_usd) > 0.10
    ) or (
        (leader_sell_usd > _LEG_EPS or our_sell_usd > _LEG_EPS)
        and _compare_rel_diff(leader_sell_usd, our_sell_usd) > 0.10
    ):
        return "sizing_gap"
    leader_buy_avg = _compare_safe_float(row.get("leader_buy_avg_price"))
    our_buy_avg = _compare_safe_float(row.get("our_buy_avg_price"))
    leader_sell_avg = _compare_safe_float(row.get("leader_sell_avg_price"))
    our_sell_avg = _compare_safe_float(row.get("our_sell_avg_price"))
    if (
        leader_buy_avg > _LEG_EPS
        and our_buy_avg > _LEG_EPS
        and _compare_rel_diff(leader_buy_avg, our_buy_avg) > 0.005
    ) or (
        leader_sell_avg > _LEG_EPS
        and our_sell_avg > _LEG_EPS
        and _compare_rel_diff(leader_sell_avg, our_sell_avg) > 0.005
    ):
        return "price_gap"
    return "none"


def _compare_should_emit_market_row(row: Dict[str, Any]) -> bool:
    return any(
        (
            int(row.get("leader_today_event_count") or 0) > 0,
            int(row.get("our_today_event_count") or 0) > 0,
            abs(_compare_safe_float(row.get("leader_total_pnl"))) > _LEG_EPS,
            abs(_compare_safe_float(row.get("our_total_pnl"))) > _LEG_EPS,
        )
    )


def _compare_should_emit_summary_row(
    *,
    leader_total: float,
    our_total: float,
    leader_excluded: float,
    our_excluded: float,
    emitted_market_count: int,
) -> bool:
    return bool(
        emitted_market_count
        or abs(leader_total) > _LEG_EPS
        or abs(our_total) > _LEG_EPS
        or abs(leader_excluded) > _LEG_EPS
        or abs(our_excluded) > _LEG_EPS
    )


def _compare_state_needs_context(state: Dict[str, Any]) -> bool:
    return bool(
        _compare_safe_float(state.get("open_size")) > _LEG_EPS
        or _compare_safe_float(state.get("bod_open_size")) > _LEG_EPS
        or _compare_safe_int(state.get("today_event_count")) > 0
    )


def _compare_should_persist_open_state(state: Dict[str, Any]) -> bool:
    return _compare_state_needs_context(state)


def _compare_resolution_is_effective(
    *,
    settlement_time: Any,
    compare_now_utc: datetime,
    market_closed: bool,
) -> bool:
    parsed_settlement = _parse_datetime_utc(settlement_time)
    if parsed_settlement:
        return datetime.fromisoformat(parsed_settlement) <= compare_now_utc + timedelta(minutes=10)
    return bool(market_closed)


def _compare_short_leader(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"


def _compare_short_market(value: Any, *, width: int = 28) -> str:
    raw = " ".join(str(value or "").split())
    if len(raw) <= width:
        return raw
    if width <= 3:
        return raw[:width]
    return raw[: width - 3] + "..."


def _compare_fmt_money(value: Any, *, width: int = 12) -> str:
    amount = _compare_safe_float(value)
    return f"{amount:>{width},.2f}"


def _compare_fmt_reason(value: Any) -> str:
    reason = str(value or "none").strip().lower()
    mapping = {
        "none": "-",
        "count_gap": "count",
        "sizing_gap": "size",
        "price_gap": "price",
        "excluded": "excluded",
    }
    return mapping.get(reason, reason[:8] or "-")


def _log_daily_compare_report(
    db: CopyTradeDB,
    *,
    date_key: str,
    account_names: List[str],
    summary_limit: int = 12,
    market_limit: int = 14,
) -> None:
    selected_accounts = [str(name or "").strip() for name in account_names if str(name or "").strip()]
    if not selected_accounts:
        return

    placeholders = ",".join("?" for _ in selected_accounts)
    summary_rows = db.conn.execute(
        f"""
        SELECT *
        FROM ct_compare_daily_summary
        WHERE date_key=? AND account_name IN ({placeholders})
        ORDER BY ABS(delta_pnl) DESC, ABS(visible_our_pnl - visible_leader_pnl) DESC, account_name, leader_address
        LIMIT ?
        """,
        [date_key, *selected_accounts, int(max(1, summary_limit))],
    ).fetchall()
    market_rows = db.conn.execute(
        f"""
        SELECT *
        FROM ct_compare_daily_market_leg
        WHERE date_key=? AND account_name IN ({placeholders})
        ORDER BY ABS(our_total_pnl - leader_total_pnl) DESC, account_name, leader_address, market_slug, token_id
        LIMIT ?
        """,
        [date_key, *selected_accounts, int(max(1, market_limit))],
    ).fetchall()

    if not summary_rows and not market_rows:
        return

    _log(f"[compare] {date_key} daily pnl compare")
    if summary_rows:
        _log("  Summary")
        header = (
            f"  {'Acct':<6} {'Leader':<17} "
            f"{'Our':>12} {'Leader':>12} {'Delta':>12} {'Excluded':>12}"
        )
        _log(header)
        _log(f"  {'-' * (len(header) - 2)}")
        for row in summary_rows:
            excluded_gap = _compare_safe_float(row["our_excluded_pnl"]) - _compare_safe_float(row["leader_excluded_pnl"])
            line = (
                f"  {str(row['account_name'] or 'default')[:6]:<6} "
                f"{_compare_short_leader(row['leader_address']):<17} "
                f"{_compare_fmt_money(row['visible_our_pnl'])} "
                f"{_compare_fmt_money(row['visible_leader_pnl'])} "
                f"{_compare_fmt_money(row['delta_pnl'])} "
                f"{_compare_fmt_money(excluded_gap)}"
            )
            _log(line)

    if market_rows:
        _log("  Top Markets")
        header = (
            f"  {'Acct':<6} {'Leader':<17} {'Market':<28} {'Out':<5} {'Gap':<6} "
            f"{'Our':>12} {'Leader':>12} {'Delta':>12}"
        )
        _log(header)
        _log(f"  {'-' * (len(header) - 2)}")
        for row in market_rows:
            market_name = row["market_slug"] or row["condition_id"] or row["token_id"] or "-"
            line = (
                f"  {str(row['account_name'] or 'default')[:6]:<6} "
                f"{_compare_short_leader(row['leader_address']):<17} "
                f"{_compare_short_market(market_name, width=28):<28} "
                f"{str(row['outcome'] or '-')[:5]:<5} "
                f"{_compare_fmt_reason(row['primary_gap_reason']):<6} "
                f"{_compare_fmt_money(row['our_total_pnl'])} "
                f"{_compare_fmt_money(row['leader_total_pnl'])} "
                f"{_compare_fmt_money(_compare_safe_float(row['our_total_pnl']) - _compare_safe_float(row['leader_total_pnl']))}"
            )
            _log(line)
        _log("  note: Delta = Our - Leader; Summary uses visible pnl after long-dated exclusions.")


def build_daily_compare(
    db: CopyTradeDB,
    *,
    account_names: List[str],
    now: Optional[datetime] = None,
    accounts_dir: Optional[Path] = None,
    sync_leader_activity: bool = False,
) -> Dict[str, int]:
    selected_accounts = _parse_compare_accounts(",".join(account_names))
    if not selected_accounts:
        return {"open_leg_rows": 0, "market_leg_rows": 0, "summary_rows": 0}

    _ensure_ct_meta_table(db)
    compare_mode = _get_ct_meta_value(db, "daily_compare_mode")
    carryover_ok = compare_mode == _DAILY_COMPARE_MODE
    existing_open_rows = (
        db.get_compare_open_leg_state_rows(_compare_date_key(now), selected_accounts)
        if carryover_ok
        else []
    )
    previous_open_rows = (
        db.get_compare_open_leg_state_rows(_compare_prev_date_key(_compare_date_key(now)), selected_accounts)
        if carryover_ok
        else []
    )
    if carryover_ok and existing_open_rows and previous_open_rows:
        existing_open_rows = _repair_compare_existing_baseline_rows(existing_open_rows, previous_open_rows)
    bootstrap_on_mode_mismatch = bool(compare_mode) and not carryover_ok
    bootstrap_on_empty_state = carryover_ok and not existing_open_rows and not previous_open_rows
    bootstrap_from_current_state = bootstrap_on_mode_mismatch or bootstrap_on_empty_state
    if not carryover_ok:
        db.purge_compare_accounts(selected_accounts)
    if compare_mode and not carryover_ok:
        _log(
            f"[compare] ignore stale carryover mode={compare_mode} "
            f"expected={_DAILY_COMPARE_MODE}"
        )
        _log("[compare] bootstrap from current open positions; historical compare counts will restart from now")
    elif bootstrap_on_empty_state:
        _log("[compare] no carryover rows found; bootstrap from current open positions")

    account_leaders = _load_compare_account_leaders(selected_accounts, accounts_dir=accounts_dir)
    date_key = _compare_date_key(now)
    compare_now = _compare_now_utc8(now)
    compare_now_utc = compare_now.astimezone(timezone.utc)
    baseline_dt = _compare_baseline_dt_utc(date_key)
    baseline_epoch = int(baseline_dt.timestamp())

    selected_pairs = {
        (account_name, leader_address)
        for account_name, leaders in account_leaders.items()
        for leader_address in leaders
    }
    baseline_by_pair_scope, _unused_baseline = _group_compare_open_state_rows(existing_open_rows)
    previous_by_pair_scope, _unused_previous = _group_compare_open_state_rows(previous_open_rows)
    observed_trade_rows = _load_compare_trade_rows_for_pairs(
        db,
        allowed_pairs=selected_pairs,
        baseline_dt=None,
        filled_only=False,
    )
    all_trade_rows = _load_compare_trade_rows_for_pairs(
        db,
        allowed_pairs=selected_pairs,
        baseline_dt=None,
        filled_only=True,
    )
    observed_trade_rows_by_pair = _group_compare_trade_rows_by_pair(observed_trade_rows)
    trade_rows_by_pair = _group_compare_trade_rows_by_pair(all_trade_rows)
    follow_start_by_pair = _derive_compare_follow_start_by_pair(observed_trade_rows)
    pair_follow_start = {
        pair: follow_start_by_pair.get(pair, baseline_dt)
        for pair in selected_pairs
    }

    unique_leaders = sorted({leader for leaders in account_leaders.values() for leader in leaders})
    session = requests.Session()
    try:
        if sync_leader_activity and unique_leaders:
            _log("[compare] leader activity sync disabled; analytics module removed")

        leader_since_by_address: Dict[str, int] = {}
        for pair, follow_start_dt in pair_follow_start.items():
            leader_address = pair[1]
            since_ts = int(follow_start_dt.timestamp())
            previous_since = leader_since_by_address.get(leader_address)
            if previous_since is None or since_ts < previous_since:
                leader_since_by_address[leader_address] = since_ts
        leader_activity_cache = {
            leader_address: db.get_leader_activity(
                leader_address,
                since_ts=leader_since_by_address.get(leader_address),
            )
            for leader_address in unique_leaders
        }
        open_trade_rows_by_pair: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        if bootstrap_from_current_state:
            for account_name in selected_accounts:
                for trade in db.get_all_open_trades(account_name=account_name):
                    pair = (
                        account_name,
                        str(trade.get("leader_address") or "").lower(),
                    )
                    if pair not in selected_pairs:
                        continue
                    open_trade_rows_by_pair[pair].append(trade)

        leader_state_cache: Dict[Tuple[str, str], Dict[Tuple[str, str], Dict[str, Any]]] = {}
        our_state_cache: Dict[Tuple[str, str], Dict[Tuple[str, str], Dict[str, Any]]] = {}
        leader_bootstrap_positions_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        midpoint_cache: Dict[str, Optional[float]] = {}
        for account_name, leaders in account_leaders.items():
            for leader_address in leaders:
                pair = (account_name, leader_address)
                pair_follow_start_dt = pair_follow_start.get(pair, baseline_dt)
                pair_intraday_floor_dt = baseline_dt
                leader_events = _build_leader_events_for_pair(
                    leader_address=leader_address,
                    activities=leader_activity_cache.get(leader_address, []),
                    trades=observed_trade_rows_by_pair.get(pair, []),
                )
                leader_cached_rows = baseline_by_pair_scope.get(
                    (account_name, leader_address, _COMPARE_SCOPE_LEADER)
                ) or []
                if leader_cached_rows:
                    leader_state = {
                        _compare_leg_key(row.get("condition_id"), row.get("token_id")): _copy_compare_baseline_row(row)
                        for row in leader_cached_rows
                    }
                elif previous_by_pair_scope.get((account_name, leader_address, _COMPARE_SCOPE_LEADER)):
                    leader_state = _build_carryover_state_from_previous_day_rows(
                        previous_by_pair_scope.get((account_name, leader_address, _COMPARE_SCOPE_LEADER)) or []
                    )
                    _seed_bod_marks_for_states(
                        session,
                        leader_state,
                        midpoint_cache=midpoint_cache,
                    )
                elif bootstrap_from_current_state:
                    leader_positions = leader_bootstrap_positions_cache.get(leader_address)
                    if leader_positions is None:
                        leader_positions = _fetch_onchain_positions(session, leader_address)
                        leader_bootstrap_positions_cache[leader_address] = leader_positions
                    leader_state = _build_leader_bootstrap_states_from_positions(
                        leader_positions,
                        allowed_token_ids={
                            str(row.get("token_id") or "").strip()
                            for row in observed_trade_rows_by_pair.get(pair, [])
                            if str(row.get("token_id") or "").strip()
                        },
                    )
                    _seed_bod_marks_for_states(
                        session,
                        leader_state,
                        midpoint_cache=midpoint_cache,
                    )
                    pair_intraday_floor_dt = compare_now_utc
                else:
                    leader_state = _build_leader_bod_states_from_activities(
                        leader_events,
                        follow_start_dt=pair_follow_start_dt,
                        baseline_dt=baseline_dt,
                    )
                    _seed_bod_marks_for_states(
                        session,
                        leader_state,
                        midpoint_cache=midpoint_cache,
                    )

                for event in leader_events:
                    event_ts = _parse_datetime_utc(event.get("ts"))
                    if not event_ts:
                        continue
                    event_dt = datetime.fromisoformat(event_ts)
                    if event_dt < pair_follow_start_dt or event_dt < pair_intraday_floor_dt:
                        continue
                    side = str(event.get("kind") or "").upper()
                    if side == "BUY":
                        _compare_apply_buy(
                            leader_state,
                            condition_id=event.get("condition_id"),
                            token_id=event.get("token_id"),
                            market_slug=event.get("market_slug"),
                            outcome=event.get("outcome"),
                            size=event.get("size"),
                            usd=event.get("usd"),
                            price=event.get("price"),
                            event_ts=event_ts,
                            collect_metrics=True,
                        )
                    elif side == "SELL":
                        _compare_apply_sell(
                            leader_state,
                            condition_id=event.get("condition_id"),
                            token_id=event.get("token_id"),
                            market_slug=event.get("market_slug"),
                            outcome=event.get("outcome"),
                            size=event.get("size"),
                            usd=event.get("usd"),
                            price=event.get("price"),
                            event_ts=event_ts,
                            collect_metrics=True,
                        )
                leader_state_cache[pair] = leader_state

                our_cached_rows = baseline_by_pair_scope.get((account_name, leader_address, _COMPARE_SCOPE_OUR)) or []
                state_map = _build_our_compare_baseline_state(
                    cached_rows=our_cached_rows,
                    previous_rows=previous_by_pair_scope.get((account_name, leader_address, _COMPARE_SCOPE_OUR)) or [],
                    trades=trade_rows_by_pair.get(pair, []),
                    baseline_dt=baseline_dt,
                    bootstrap_rows=open_trade_rows_by_pair.get(pair, []) if bootstrap_from_current_state else None,
                )
                _seed_bod_marks_for_states(
                    session,
                    state_map,
                    midpoint_cache=midpoint_cache,
                )
                for _event_dt, _order, event in _build_our_intraday_events(
                    trade_rows_by_pair.get(pair, []),
                    baseline_dt=baseline_dt,
                    start_dt=compare_now_utc if bootstrap_from_current_state else None,
                ):
                    if event["kind"] == "BUY":
                        _compare_apply_buy(
                            state_map,
                            condition_id=event["condition_id"],
                            token_id=event["token_id"],
                            market_slug=event.get("market_slug"),
                            outcome=event.get("outcome"),
                            size=event.get("size"),
                            usd=event.get("usd"),
                            price=event.get("price"),
                            event_ts=event.get("ts"),
                            collect_metrics=True,
                        )
                    elif event["kind"] == "SELL":
                        _compare_apply_sell(
                            state_map,
                            condition_id=event["condition_id"],
                            token_id=event["token_id"],
                            market_slug=event.get("market_slug"),
                            outcome=event.get("outcome"),
                            size=event.get("size"),
                            usd=event.get("usd"),
                            price=event.get("price"),
                            event_ts=event.get("ts"),
                            collect_metrics=True,
                        )
                    else:
                        _compare_apply_settlement(
                            state_map,
                            condition_id=event["condition_id"],
                            token_id=event["token_id"],
                            market_slug=event.get("market_slug"),
                            outcome=event.get("outcome"),
                            size=event.get("size"),
                            price=event.get("price"),
                            settlement_time=event.get("ts"),
                            collect_metrics=True,
                        )
                our_state_cache[pair] = state_map

        compare_state_maps = list(leader_state_cache.values()) + list(our_state_cache.values())
        token_ids = sorted(
            {
                str(state.get("token_id") or "").strip()
                for state_map in compare_state_maps
                for state in state_map.values()
                if (
                    str(state.get("token_id") or "").strip()
                    and _compare_state_needs_context(state)
                )
            }
        )
        token_meta = _fetch_token_market_meta_batch(session, token_ids)
        resolution_context_rows: List[Dict[str, Any]] = []
        for state_map in compare_state_maps:
            for state in state_map.values():
                if not _compare_state_needs_context(state):
                    continue
                resolution_context_rows.append(
                    {
                        "token_id": state.get("token_id"),
                        "condition_id": state.get("condition_id"),
                        "market_slug": state.get("market_slug"),
                    }
                )
                meta = token_meta.get(str(state.get("token_id") or "").strip()) or {}
                _compare_upsert_meta(state, market_slug=meta.get("market_slug"), outcome=meta.get("outcome"))
                if meta.get("condition_id") and not state.get("condition_id"):
                    state["condition_id"] = meta["condition_id"]
                if meta.get("settlement_time") and not state.get("settlement_time"):
                    state["settlement_time"] = meta["settlement_time"]

        resolution_prices: Dict[str, float] = {}
        settlement_times: Dict[str, str] = {}
        resolution_candidate_ids: List[str] = []
        resolution_candidate_set = set()
        for state_map in compare_state_maps:
            for state in state_map.values():
                token_id = str(state.get("token_id") or "").strip()
                if not token_id:
                    continue
                meta = token_meta.get(token_id) or {}
                settlement_time = _parse_datetime_utc(meta.get("settlement_time") or state.get("settlement_time"))
                if settlement_time:
                    settlement_times[token_id] = settlement_time
                    state["settlement_time"] = settlement_time
                direct_price = meta.get("resolution_price")
                resolution_effective = _compare_resolution_is_effective(
                    settlement_time=settlement_time,
                    compare_now_utc=compare_now_utc,
                    market_closed=bool(meta.get("market_closed")),
                )
                if isinstance(direct_price, (int, float)) and resolution_effective:
                    resolution_prices[token_id] = float(direct_price)
                open_size = _compare_safe_float(state.get("open_size"))
                if open_size <= _LEG_EPS or token_id in resolution_candidate_set or token_id in resolution_prices:
                    continue
                if resolution_effective:
                    resolution_candidate_set.add(token_id)
                    resolution_candidate_ids.append(token_id)

        if resolution_candidate_ids:
            _log(
                f"[compare] resolution candidates={len(resolution_candidate_ids)} "
                f"from watchlist={len(token_ids)}"
            )
            live_prices, live_settlement_times, _unresolved_tokens, _live_resolved, _live_attempted = _resolve_tokens_with_cache_and_live(
                db,
                session,
                resolution_candidate_ids,
                token_context_map=_build_token_resolution_context_map(
                    [
                        row
                        for row in resolution_context_rows
                        if str(row.get("token_id") or "").strip() in resolution_candidate_set
                    ]
                ),
                fetch_live_for_cached=False,
            )
            resolution_prices.update(live_prices)
            settlement_times.update(live_settlement_times)
        midpoint_prices = _fetch_midpoints_with_cache(
            session,
            [
                state["token_id"]
                for state_map in compare_state_maps
                for state in state_map.values()
                if _compare_safe_float(state.get("open_size")) > _LEG_EPS
            ],
        )

        open_leg_rows: List[Dict[str, Any]] = []
        market_leg_rows: List[Dict[str, Any]] = []
        summary_rows: List[Dict[str, Any]] = []

        for account_name, leaders in account_leaders.items():
            for leader_address in leaders:
                pair = (account_name, leader_address)
                leader_states = leader_state_cache.get(pair, {})
                our_states = our_state_cache.get(pair, {})
                leg_keys = sorted(set(leader_states.keys()) | set(our_states.keys()))
                leader_total = 0.0
                our_total = 0.0
                leader_excluded = 0.0
                our_excluded = 0.0
                visible_leader = 0.0
                visible_our = 0.0
                emitted_market_count = 0

                for state_map in (leader_states, our_states):
                    for state in state_map.values():
                        token_id = str(state.get("token_id") or "").strip()
                        settlement_time = settlement_times.get(token_id) or state.get("settlement_time") or (token_meta.get(token_id) or {}).get("settlement_time")
                        if settlement_time:
                            state["settlement_time"] = settlement_time
                        if _compare_safe_float(state.get("open_size")) > _LEG_EPS and token_id in resolution_prices:
                            _compare_apply_settlement(
                                state_map,
                                condition_id=state.get("condition_id"),
                                token_id=token_id,
                                market_slug=state.get("market_slug") or (token_meta.get(token_id) or {}).get("market_slug"),
                                outcome=state.get("outcome") or (token_meta.get(token_id) or {}).get("outcome"),
                                size=state.get("open_size"),
                                price=resolution_prices[token_id],
                                settlement_time=settlement_time or compare_now_utc.isoformat(),
                                collect_metrics=True,
                            )
                        open_size = _compare_safe_float(state.get("open_size"))
                        if open_size > _LEG_EPS:
                            if token_id in resolution_prices and settlement_time:
                                mark_price = resolution_prices[token_id]
                                mark_source = "resolution"
                            elif token_id in midpoint_prices:
                                mark_price = midpoint_prices[token_id]
                                mark_source = "midpoint"
                            elif state.get("_previous_mark_price") is not None:
                                mark_price = _compare_safe_float(state.get("_previous_mark_price"))
                                mark_source = "carry"
                            elif state.get("bod_mark_price") is not None:
                                mark_price = _compare_safe_float(state.get("bod_mark_price"))
                                mark_source = "bod"
                            else:
                                mark_price = _compare_state_avg_open_price(state)
                                mark_source = "avg"
                            state["mark_price_now"] = mark_price
                            state["mark_price_source"] = mark_source
                            state["unrealized_now"] = open_size * _compare_safe_float(mark_price) - _compare_safe_float(state.get("open_cost"))
                            state["status"] = "open"
                        else:
                            state["unrealized_now"] = 0.0
                            if state.get("status") not in {"sold", "settled"}:
                                state["status"] = "sold"

                for leg_key in leg_keys:
                    leader_state = leader_states.get(leg_key)
                    our_state = our_states.get(leg_key)
                    base_state = leader_state or our_state
                    if base_state is None:
                        continue
                    token_id = str(base_state.get("token_id") or "").strip()
                    meta = token_meta.get(token_id) or {}
                    market_slug = str(
                        (leader_state or {}).get("market_slug")
                        or (our_state or {}).get("market_slug")
                        or meta.get("market_slug")
                        or ""
                    ).strip() or None
                    outcome = (
                        _compare_clean_outcome((leader_state or {}).get("outcome"))
                        or _compare_clean_outcome((our_state or {}).get("outcome"))
                        or _compare_clean_outcome(meta.get("outcome"))
                    )
                    settlement_time = (
                        (leader_state or {}).get("settlement_time")
                        or (our_state or {}).get("settlement_time")
                        or settlement_times.get(token_id)
                        or meta.get("settlement_time")
                    )
                    exclusion_reason = None
                    parsed_settlement = _parse_datetime_utc(settlement_time)
                    if parsed_settlement:
                        settlement_dt = datetime.fromisoformat(parsed_settlement)
                        if settlement_dt > compare_now_utc + timedelta(days=_COMPARE_LONG_DATED_DAYS):
                            exclusion_reason = _COMPARE_EXCLUDED_LONG_DATED

                    leader_buy_size = _compare_safe_float((leader_state or {}).get("cumulative_buy_size"))
                    leader_buy_usd = _compare_safe_float((leader_state or {}).get("cumulative_buy_usd"))
                    leader_sell_size = _compare_safe_float((leader_state or {}).get("cumulative_sell_size"))
                    leader_sell_usd = _compare_safe_float((leader_state or {}).get("cumulative_sell_usd"))
                    leader_realized = _compare_safe_float((leader_state or {}).get("realized_pnl"))
                    leader_unrealized_change = _compare_safe_float((leader_state or {}).get("unrealized_now")) - _compare_safe_float((leader_state or {}).get("unrealized_bod"))
                    leader_leg_total = leader_realized + leader_unrealized_change

                    our_buy_size = _compare_safe_float((our_state or {}).get("cumulative_buy_size"))
                    our_buy_usd = _compare_safe_float((our_state or {}).get("cumulative_buy_usd"))
                    our_sell_size = _compare_safe_float((our_state or {}).get("cumulative_sell_size"))
                    our_sell_usd = _compare_safe_float((our_state or {}).get("cumulative_sell_usd"))
                    our_realized = _compare_safe_float((our_state or {}).get("realized_pnl"))
                    our_unrealized_change = _compare_safe_float((our_state or {}).get("unrealized_now")) - _compare_safe_float((our_state or {}).get("unrealized_bod"))
                    our_leg_total = our_realized + our_unrealized_change

                    leader_total += leader_leg_total
                    our_total += our_leg_total
                    if exclusion_reason:
                        leader_excluded += leader_leg_total
                        our_excluded += our_leg_total
                    else:
                        visible_leader += leader_leg_total
                        visible_our += our_leg_total

                    row = {
                        "date_key": date_key,
                        "account_name": account_name,
                        "leader_address": leader_address,
                        "condition_id": leg_key[0],
                        "token_id": leg_key[1],
                        "market_slug": market_slug,
                        "outcome": outcome,
                        "exclusion_reason": exclusion_reason,
                        "leader_buy_fill_count": int((leader_state or {}).get("cumulative_buy_fill_count") or 0),
                        "leader_buy_usd": leader_buy_usd,
                        "leader_buy_avg_price": _compare_avg(leader_buy_size, leader_buy_usd),
                        "leader_sell_fill_count": int((leader_state or {}).get("cumulative_sell_fill_count") or 0),
                        "leader_sell_usd": leader_sell_usd,
                        "leader_sell_avg_price": _compare_avg(leader_sell_size, leader_sell_usd),
                        "leader_realized_pnl": leader_realized,
                        "leader_unrealized_change": leader_unrealized_change,
                        "leader_total_pnl": leader_leg_total,
                        "our_buy_fill_count": int((our_state or {}).get("cumulative_buy_fill_count") or 0),
                        "our_buy_usd": our_buy_usd,
                        "our_buy_avg_price": _compare_avg(our_buy_size, our_buy_usd),
                        "our_sell_fill_count": int((our_state or {}).get("cumulative_sell_fill_count") or 0),
                        "our_sell_usd": our_sell_usd,
                        "our_sell_avg_price": _compare_avg(our_sell_size, our_sell_usd),
                        "our_realized_pnl": our_realized,
                        "our_unrealized_change": our_unrealized_change,
                        "our_total_pnl": our_leg_total,
                        "leader_today_event_count": int((leader_state or {}).get("today_event_count") or 0),
                        "our_today_event_count": int((our_state or {}).get("today_event_count") or 0),
                    }
                    row["primary_gap_reason"] = _determine_primary_gap_reason(row)
                    if _compare_should_emit_market_row(row):
                        market_leg_rows.append(row)
                        emitted_market_count += 1

                if _compare_should_emit_summary_row(
                    leader_total=leader_total,
                    our_total=our_total,
                    leader_excluded=leader_excluded,
                    our_excluded=our_excluded,
                    emitted_market_count=emitted_market_count,
                ):
                    summary_rows.append(
                        {
                            "date_key": date_key,
                            "account_name": account_name,
                            "leader_address": leader_address,
                            "leader_total_pnl": leader_total,
                            "our_total_pnl": our_total,
                            "delta_pnl": our_total - leader_total,
                            "leader_excluded_pnl": leader_excluded,
                            "our_excluded_pnl": our_excluded,
                            "visible_leader_pnl": visible_leader,
                            "visible_our_pnl": visible_our,
                        }
                    )

                for scope_kind, state_map in ((_COMPARE_SCOPE_LEADER, leader_states), (_COMPARE_SCOPE_OUR, our_states)):
                    for leg_key in leg_keys:
                        state = state_map.get(leg_key)
                        if state is None:
                            continue
                        if not _compare_should_persist_open_state(state):
                            continue
                        token_id = str(state.get("token_id") or "").strip()
                        meta = token_meta.get(token_id) or {}
                        settlement_time = state.get("settlement_time") or settlement_times.get(token_id) or meta.get("settlement_time")
                        exclusion_reason = None
                        parsed_settlement = _parse_datetime_utc(settlement_time)
                        if parsed_settlement:
                            settlement_dt = datetime.fromisoformat(parsed_settlement)
                            if settlement_dt > compare_now_utc + timedelta(days=_COMPARE_LONG_DATED_DAYS):
                                exclusion_reason = _COMPARE_EXCLUDED_LONG_DATED
                        open_leg_rows.append(
                            {
                                "date_key": date_key,
                                "account_name": account_name,
                                "leader_address": leader_address,
                                "scope_kind": scope_kind,
                                "condition_id": leg_key[0],
                                "token_id": leg_key[1],
                                "market_slug": state.get("market_slug") or meta.get("market_slug"),
                                "outcome": state.get("outcome") or meta.get("outcome"),
                                "bod_open_size": _compare_safe_float(state.get("bod_open_size")),
                                "bod_open_cost": _compare_safe_float(state.get("bod_open_cost")),
                                "bod_avg_open_price": _compare_safe_float(state.get("bod_avg_open_price")),
                                "bod_mark_price": state.get("bod_mark_price"),
                                "open_size": _compare_safe_float(state.get("open_size")),
                                "open_cost": _compare_safe_float(state.get("open_cost")),
                                "avg_open_price": _compare_state_avg_open_price(state),
                                "unrealized_bod": _compare_safe_float(state.get("unrealized_bod")),
                                "bod_cumulative_buy_fill_count": _compare_safe_int(state.get("bod_cumulative_buy_fill_count")),
                                "bod_cumulative_buy_size": _compare_safe_float(state.get("bod_cumulative_buy_size")),
                                "bod_cumulative_buy_usd": _compare_safe_float(state.get("bod_cumulative_buy_usd")),
                                "bod_cumulative_sell_fill_count": _compare_safe_int(state.get("bod_cumulative_sell_fill_count")),
                                "bod_cumulative_sell_size": _compare_safe_float(state.get("bod_cumulative_sell_size")),
                                "bod_cumulative_sell_usd": _compare_safe_float(state.get("bod_cumulative_sell_usd")),
                                "cumulative_buy_fill_count": _compare_safe_int(state.get("cumulative_buy_fill_count")),
                                "cumulative_buy_size": _compare_safe_float(state.get("cumulative_buy_size")),
                                "cumulative_buy_usd": _compare_safe_float(state.get("cumulative_buy_usd")),
                                "cumulative_sell_fill_count": _compare_safe_int(state.get("cumulative_sell_fill_count")),
                                "cumulative_sell_size": _compare_safe_float(state.get("cumulative_sell_size")),
                                "cumulative_sell_usd": _compare_safe_float(state.get("cumulative_sell_usd")),
                                "mark_price_now": state.get("mark_price_now"),
                                "unrealized_now": _compare_safe_float(state.get("unrealized_now")),
                                "realized_pnl": _compare_safe_float(state.get("realized_pnl")),
                                "status": state.get("status") or "open",
                                "exclusion_reason": exclusion_reason,
                                "settlement_time": settlement_time,
                                "last_event_ts": state.get("last_event_ts"),
                                "mark_price_source": state.get("mark_price_source"),
                            }
                        )

        db.replace_compare_open_leg_state(date_key=date_key, account_names=selected_accounts, rows=open_leg_rows)
        db.replace_compare_daily_market_leg(date_key=date_key, account_names=selected_accounts, rows=market_leg_rows)
        db.replace_compare_daily_summary(date_key=date_key, account_names=selected_accounts, rows=summary_rows)
        db.purge_compare_before(_compare_cutoff_date_key(now))
        _upsert_ct_meta(db, "daily_compare_mode", _DAILY_COMPARE_MODE)
    finally:
        session.close()

    return {
        "open_leg_rows": len(open_leg_rows),
        "market_leg_rows": len(market_leg_rows),
        "summary_rows": len(summary_rows),
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Build leader PnL attribution snapshots"
    )
    ap.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_DB_PATH),
        help="Path to copytrade sqlite database",
    )
    ap.add_argument(
        "--no-sync-supabase",
        action="store_true",
        help="Skip syncing to Supabase",
    )
    ap.add_argument(
        "--force-rebuild-daily",
        action="store_true",
        help="Force rebuild of daily leader pnl history from baseline to yesterday",
    )
    ap.add_argument(
        "--accounts",
        type=str,
        default="",
        help="Comma-separated account names for the daily compare pipeline, e.g. main,pm-2",
    )
    ap.add_argument(
        "--compare-date",
        type=str,
        default="",
        help="Rebuild daily compare for a specific UTC+8 date, YYYY-MM-DD",
    )
    ap.add_argument(
        "--compare-only",
        action="store_true",
        help="Run only the daily compare pipeline and sync only compare tables",
    )
    return ap.parse_args()


def _market_key(row: Dict[str, Any]) -> Tuple[str, str]:
    cond = str(row.get("condition_id") or "").strip()
    slug = row.get("market_slug")
    slug_str = str(slug).strip() if isinstance(slug, str) else None
    if cond:
        return cond, slug_str or cond[:16]
    token = str(row.get("token_id") or "").strip()
    fallback = token or "unknown_market"
    return fallback, slug_str or fallback[:16]


def _build_snapshots_legacy(db: CopyTradeDB) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """閾句笂鏁版嵁椹卞姩鐨勫綊鍥犵郴缁燂紙鍙畻 open positions锛?

    1. 鑾峰彇閾句笂 open positions 鐨?per-token PnL锛坓round truth锛?    2. 鎸?ct_trades token鈫抣eader 鏄犲皠褰掑洜
    """
    from dotenv import load_dotenv
    load_dotenv(DOTENV_PATH)

    # 鍔犺浇璐﹀彿鍦板潃
    accounts_dir = Path(ACCOUNTS_DIR)
    account_addrs: Dict[str, str] = {}
    if accounts_dir.is_dir():
        for toml_file in accounts_dir.glob("*.toml"):
            if toml_file.name.startswith("_"):
                continue
            acct_name = toml_file.stem
            try:
                import tomllib
            except ImportError:
                import tomli as tomllib  # type: ignore[no-redef]
            with open(toml_file, "rb") as f:
                data = tomllib.load(f)
            suffix = data.get("env_suffix", "").strip()
            if suffix:
                addr = os.environ.get(f"FUNDER_ADDRESS_{suffix}", "").strip()
                if addr:
                    account_addrs[acct_name] = addr

    session = requests.Session()
    all_summary: List[Dict[str, Any]] = []
    all_market: List[Dict[str, Any]] = []

    for acct_name, address in account_addrs.items():
        _log(f"[{acct_name}] 寮€濮嬪綊鍥?({address[:10]}...)")

        # Step 1: fetch on-chain open positions (ground truth).
        chain_positions = _fetch_onchain_positions(session, address)
        open_total_pnl = sum(p["pnl"] for p in chain_positions.values())
        _log(f"[{acct_name}] 閾句笂浠撲綅: {len(chain_positions)} 涓? open PnL: {open_total_pnl:.2f}")

        # Step 2: 鏋勫缓 token鈫抣eader 鏄犲皠
        token_leader_map = _build_token_leader_map(db, acct_name)
        _log(f"[{acct_name}] token鈫抣eader 鏄犲皠: {len(token_leader_map)} 涓?token")

        # Step 3: 褰掑洜 open positions
        open_leader_pnl, unattributed_pnl = _attribute_open_pnl(
            chain_positions, token_leader_map
        )
        _log(f"[{acct_name}] open 褰掑洜: {len(open_leader_pnl)} 涓?leader, "
             f"鏈綊鍥?璺熷崟鍓?: {unattributed_pnl:.2f}")

        # Step 4: 鏋勫缓 per-leader per-market 鏄庣粏
        market_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for tid, pos in chain_positions.items():
            mapping = token_leader_map.get(tid)
            if not mapping:
                continue
            cond_id = pos["condition_id"] or tid[:16]
            slug = pos["slug"] or cond_id[:16]
            for leader, weight in mapping:
                key = (leader, cond_id)
                if key not in market_map:
                    market_map[key] = {
                        "leader_address": leader,
                        "condition_id": cond_id,
                        "account_name": acct_name,
                        "market_slug": slug,
                        "total_realized_pnl": 0.0,
                        "total_unrealized_pnl": 0.0,
                    }
                market_map[key]["total_unrealized_pnl"] += pos["pnl"] * weight

        # 鏋勫缓 market_rows
        for m in market_map.values():
            unrealized = float(m["total_unrealized_pnl"])
            result = "win" if unrealized > 1e-12 else ("loss" if unrealized < -1e-12 else "flat")
            all_market.append({
                "leader_address": m["leader_address"],
                "condition_id": m["condition_id"],
                "account_name": m["account_name"],
                "market_slug": m["market_slug"],
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": unrealized,
                "total_pnl": unrealized,
                "market_result": result,
            })

        # 缁熻姣忎釜 leader 鐨?market 鏁伴噺
        leader_market_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"win": 0, "loss": 0, "total": 0}
        )
        for m in all_market:
            if m["account_name"] != acct_name:
                continue
            leader = m["leader_address"]
            leader_market_counts[leader]["total"] += 1
            if m["total_pnl"] > 1e-12:
                leader_market_counts[leader]["win"] += 1
            elif m["total_pnl"] < -1e-12:
                leader_market_counts[leader]["loss"] += 1

        # 鏋勫缓 summary_rows
        for leader, pnl in open_leader_pnl.items():
            unrealized = pnl["unrealized"]
            counts = leader_market_counts.get(leader, {"win": 0, "loss": 0, "total": 0})
            total_markets = counts["total"]
            win_rate = (counts["win"] / total_markets) if total_markets > 0 else None
            all_summary.append({
                "leader_address": leader,
                "account_name": acct_name,
                "total_realized_pnl": 0.0,
                "total_unrealized_pnl": unrealized,
                "total_pnl": unrealized,
                "winning_markets": counts["win"],
                "losing_markets": counts["loss"],
                "total_markets": total_markets,
                "win_rate": win_rate,
            })

        attributed = sum(s["total_pnl"] for s in all_summary if s["account_name"] == acct_name)
        _log(f"[{acct_name}] 褰掑洜 PnL: {attributed:.2f}")

    all_summary.sort(key=lambda x: float(x.get("total_pnl") or 0), reverse=True)
    all_market.sort(key=lambda x: float(x.get("total_pnl") or 0), reverse=True)
    _log(f"legacy attribution done: leaders={len(all_summary)} markets={len(all_market)}")
    return all_summary, all_market


# Override legacy snapshot builder with realized+unrealized logic.
def build_snapshots(db: CopyTradeDB) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    account_addrs = _load_account_addresses()

    session = requests.Session()
    all_summary: List[Dict[str, Any]] = []
    all_market: List[Dict[str, Any]] = []

    for acct_name, address in account_addrs.items():
        _log(f"[{acct_name}] start attribution ({address[:10]}...)")

        chain_positions = _fetch_onchain_positions(session, address)
        open_total_unrealized = sum(float(p.get("unrealized_pnl") or 0.0) for p in chain_positions.values())
        _log(
            f"[{acct_name}] chain positions={len(chain_positions)} "
            f"open_unrealized={open_total_unrealized:.2f}"
        )

        token_leader_map = _build_token_leader_map(db, acct_name)
        open_leader_pnl, unattributed_pnl = _attribute_open_pnl(chain_positions, token_leader_map)
        _log(
            f"[{acct_name}] open attributed leaders={len(open_leader_pnl)} "
            f"unattributed={unattributed_pnl:.2f}"
        )

        market_map, realized_by_leader = _load_realized_market_pnl(db, acct_name)

        for tid, pos in chain_positions.items():
            mapping = token_leader_map.get(tid)
            if not mapping:
                continue
            cond_id = str(pos.get("condition_id") or tid[:16] or "unknown_market")
            slug = str(pos.get("slug") or cond_id[:16])
            open_unrealized = float(pos.get("unrealized_pnl") or 0.0)
            for leader, weight in mapping:
                key = (leader, cond_id)
                item = market_map.get(key)
                if item is None:
                    item = {
                        "leader_address": leader,
                        "condition_id": cond_id,
                        "account_name": acct_name,
                        "market_slug": slug,
                        "total_realized_pnl": 0.0,
                        "total_unrealized_pnl": 0.0,
                    }
                    market_map[key] = item
                item["total_unrealized_pnl"] += open_unrealized * weight
                if not item.get("market_slug") and slug:
                    item["market_slug"] = slug

        leader_market_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: {"win": 0, "loss": 0, "total": 0}
        )
        for m in market_map.values():
            realized = float(m.get("total_realized_pnl") or 0.0)
            unrealized = float(m.get("total_unrealized_pnl") or 0.0)
            total = realized + unrealized
            result = "win" if total > 1e-12 else ("loss" if total < -1e-12 else "flat")
            all_market.append(
                {
                    "leader_address": m.get("leader_address", ""),
                    "condition_id": m.get("condition_id", ""),
                    "account_name": acct_name,
                    "market_slug": m.get("market_slug"),
                    "total_realized_pnl": realized,
                    "total_unrealized_pnl": unrealized,
                    "total_pnl": total,
                    "market_result": result,
                }
            )
            leader = str(m.get("leader_address") or "").lower()
            if not leader:
                continue
            leader_market_counts[leader]["total"] += 1
            if total > 1e-12:
                leader_market_counts[leader]["win"] += 1
            elif total < -1e-12:
                leader_market_counts[leader]["loss"] += 1

        leaders = set(realized_by_leader.keys()) | set(open_leader_pnl.keys()) | set(leader_market_counts.keys())
        account_summary_rows: List[Dict[str, Any]] = []
        for leader in leaders:
            realized = float(realized_by_leader.get(leader, 0.0))
            unrealized = float(open_leader_pnl.get(leader, {}).get("unrealized", 0.0))
            total = realized + unrealized
            counts = leader_market_counts.get(leader, {"win": 0, "loss": 0, "total": 0})
            total_markets = int(counts["total"])
            win_rate = (counts["win"] / total_markets) if total_markets > 0 else None
            if (
                abs(total) < 1e-12
                and abs(realized) < 1e-12
                and abs(unrealized) < 1e-12
                and total_markets == 0
            ):
                continue

            row = {
                "leader_address": leader,
                "account_name": acct_name,
                "total_realized_pnl": realized,
                "total_unrealized_pnl": unrealized,
                "total_pnl": total,
                "winning_markets": counts["win"],
                "losing_markets": counts["loss"],
                "total_markets": total_markets,
                "win_rate": win_rate,
            }
            account_summary_rows.append(row)
            all_summary.append(row)

        attributed = sum(float(s.get("total_pnl") or 0.0) for s in account_summary_rows)
        _log(f"[{acct_name}] attributed={attributed:.2f}")

    all_summary.sort(key=lambda x: float(x.get("total_pnl") or 0.0), reverse=True)
    all_market.sort(key=lambda x: float(x.get("total_pnl") or 0.0), reverse=True)
    _log(f"attribution done: leaders={len(all_summary)} markets={len(all_market)}")
    return all_summary, all_market


def sync_supabase(copytrade_db: str, *, compare_only: bool = False) -> None:
    if not SYNC_SCRIPT.exists():
        raise RuntimeError(f"sync script not found: {SYNC_SCRIPT}")
    cmd = [
        sys.executable,
        str(SYNC_SCRIPT),
        "--sqlite",
        str(ROOT_METRICS_DB_PATH),
        "--copytrade-sqlite",
        copytrade_db,
    ]
    if compare_only:
        cmd.append("--copytrade-compare-only")
    try:
        max_attempts = max(1, int(float(os.getenv("COPYTRADE_SUPABASE_SYNC_ATTEMPTS") or "3")))
    except ValueError:
        max_attempts = 3
    last_returncode = 0
    for attempt in range(max_attempts):
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            check=False,
        )
        last_returncode = int(p.returncode)
        if p.returncode == 0:
            out = (p.stdout or "").strip()
            if out:
                sys.stdout.write(out + "\n")
            return
        if attempt + 1 < max_attempts:
            wait_s = 2**attempt
            _log(
                "supabase sync failed: "
                f"exit={p.returncode}; retry {attempt + 2}/{max_attempts} after {wait_s}s"
            )
            time.sleep(wait_s)
    raise RuntimeError(f"supabase sync failed: exit code {last_returncode}")


DAILY_LEADER_PNL_MODE = "dual_basis_v2"
DAILY_LEADER_REALIZED_BASELINE_DATE = "2026-03-18"
DAILY_LEADER_REALIZED_MIN_COVER_DAYS = 14
DAILY_LEADER_OPEN_CUTOVER_DATE = "2026-04-08"


def _ensure_ct_meta_table(db: CopyTradeDB) -> None:
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS ct_meta ("
        "key TEXT PRIMARY KEY, "
        "value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    db.conn.commit()


def _get_ct_meta_value(db: CopyTradeDB, key: str) -> Optional[str]:
    row = db.conn.execute("SELECT value FROM ct_meta WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    value = str(row["value"] or "").strip()
    return value or None


def _upsert_ct_meta(db: CopyTradeDB, key: str, value: str) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    db.conn.execute(
        "INSERT INTO ct_meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, now_iso),
    )
    db.conn.commit()


def _daily_leader_pnl_row_key(row: Dict[str, Any]) -> Optional[Tuple[str, str, str]]:
    date_key = str(row.get("date_key") or "").strip()
    leader_address = str(row.get("leader_address") or "").strip().lower()
    account_name = str(row.get("account_name") or "default").strip() or "default"
    if not date_key or not leader_address:
        return None
    return date_key, leader_address, account_name


def _daily_leader_leg_row_key(row: Dict[str, Any]) -> Optional[Tuple[str, str, str, str, str]]:
    date_key = str(row.get("date_key") or "").strip()
    leader_address = str(row.get("leader_address") or "").strip().lower()
    account_name = str(row.get("account_name") or "default").strip() or "default"
    condition_id = str(row.get("condition_id") or "unknown_market").strip() or "unknown_market"
    token_id = str(row.get("token_id") or "unknown_token").strip() or "unknown_token"
    if not date_key or not leader_address:
        return None
    return date_key, leader_address, account_name, condition_id, token_id


def _is_open_history_preserve_day(date_key: str, today_key: str) -> bool:
    return bool(date_key) and DAILY_LEADER_OPEN_CUTOVER_DATE <= date_key < today_key


def _load_preserved_daily_leader_open_rows(
    db: CopyTradeDB,
    today_key: str,
    preserve_until_by_leader: Optional[Dict[Tuple[str, str], str]] = None,
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    rows = db.conn.execute(
        "SELECT date_key, leader_address, account_name, unrealized_pnl, market_count "
        "FROM ct_daily_leader_pnl "
        "WHERE date_key >= ? AND date_key < ?",
        (DAILY_LEADER_OPEN_CUTOVER_DATE, today_key),
    ).fetchall()
    preserved: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        key = _daily_leader_pnl_row_key(item)
        if key is None:
            continue
        if preserve_until_by_leader is not None:
            leader_key = (key[2], key[1])
            preserve_until = preserve_until_by_leader.get(leader_key)
            if not preserve_until or key[0] > preserve_until:
                continue
        preserved[key] = {
            "date_key": key[0],
            "leader_address": key[1],
            "account_name": key[2],
            "unrealized_pnl": float(item.get("unrealized_pnl") or 0.0),
            "market_count": int(item.get("market_count") or 0),
        }
    return preserved


def _merge_preserved_daily_leader_open_rows(
    rebuilt_rows: List[Dict[str, Any]],
    preserved_rows: Dict[Tuple[str, str, str], Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if not preserved_rows:
        return rebuilt_rows

    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rebuilt_rows:
        key = _daily_leader_pnl_row_key(row)
        if key is None:
            continue
        realized = float(row.get("realized_pnl") or 0.0)
        unrealized = float(row.get("unrealized_pnl") or 0.0)
        market_count = int(row.get("market_count") or 0)
        merged[key] = {
            "date_key": key[0],
            "leader_address": key[1],
            "account_name": key[2],
            "realized_pnl": realized,
            "unrealized_pnl": unrealized,
            "total_pnl": realized + unrealized,
            "market_count": market_count,
        }

    for key, preserved in preserved_rows.items():
        row = merged.get(key)
        if row is None:
            row = {
                "date_key": key[0],
                "leader_address": key[1],
                "account_name": key[2],
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
                "market_count": 0,
            }
        row["unrealized_pnl"] = float(preserved.get("unrealized_pnl") or 0.0)
        row["market_count"] = max(
            int(row.get("market_count") or 0),
            int(preserved.get("market_count") or 0),
        )
        row["total_pnl"] = float(row.get("realized_pnl") or 0.0) + float(row["unrealized_pnl"])
        merged[key] = row

    out = [
        row for row in merged.values()
        if (
            abs(float(row.get("realized_pnl") or 0.0)) > _LEG_EPS
            or abs(float(row.get("unrealized_pnl") or 0.0)) > _LEG_EPS
            or int(row.get("market_count") or 0) > 0
        )
    ]
    out.sort(key=lambda row: (row["date_key"], row["account_name"], row["leader_address"]))
    return out


def _load_preserved_daily_leader_leg_rows(
    db: CopyTradeDB,
    today_key: str,
    preserve_until_by_leg: Optional[Dict[Tuple[str, str, str, str], str]] = None,
) -> Tuple[
    Dict[Tuple[str, str, str, str, str], Dict[str, Any]],
    Dict[Tuple[str, str, str], List[Dict[str, Any]]],
]:
    rows = db.conn.execute(
        "SELECT * FROM ct_daily_leader_market_leg_pnl "
        "WHERE date_key >= ? AND date_key < ? "
        "ORDER BY date_key, account_name, leader_address, condition_id, token_id",
        (DAILY_LEADER_OPEN_CUTOVER_DATE, today_key),
    ).fetchall()
    preserved_rows: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    rows_by_leader_day: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        key = _daily_leader_leg_row_key(item)
        if key is None:
            continue
        if preserve_until_by_leg is not None:
            leg_key = (key[2], key[1], key[3], key[4])
            preserve_until = preserve_until_by_leg.get(leg_key)
            if not preserve_until or key[0] > preserve_until:
                continue
        preserved_rows[key] = item
        rows_by_leader_day[(key[0], key[2], key[1])].append(item)
    return preserved_rows, rows_by_leader_day


def _previous_date_key(date_key: Optional[str]) -> Optional[str]:
    raw = str(date_key or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return (dt - timedelta(days=1)).strftime("%Y-%m-%d")


def _build_leg_keep_until_map(
    normalized_trades: List[Dict[str, Any]],
    *,
    current_live_leg_keys: Optional[set] = None,
    today_key: Optional[str] = None,
    include_today_for_live: bool = False,
    include_realized_day: bool = False,
) -> Dict[Tuple[str, str, str, str], str]:
    keep_until_by_leg: Dict[Tuple[str, str, str, str], str] = {}
    live_leg_keys = set(current_live_leg_keys or set())
    live_keep_until = today_key if include_today_for_live else _previous_date_key(today_key)

    for trade in normalized_trades:
        leg_key = trade["leg_key"]
        keep_until: Optional[str]
        if trade.get("exit_status") == "exited":
            realized_date = trade.get("realized_date")
            keep_until = realized_date if include_realized_day else _previous_date_key(realized_date)
        elif leg_key in live_leg_keys and live_keep_until:
            keep_until = live_keep_until
        else:
            keep_until = trade.get("updated_date") or trade.get("created_date")
        if not keep_until:
            continue
        prior = keep_until_by_leg.get(leg_key)
        if prior is None or keep_until > prior:
            keep_until_by_leg[leg_key] = keep_until

    return keep_until_by_leg


def _build_preserve_until_by_leg(
    normalized_trades: List[Dict[str, Any]],
    *,
    current_live_leg_keys: Optional[set] = None,
    today_key: Optional[str] = None,
) -> Dict[Tuple[str, str, str, str], str]:
    return _build_leg_keep_until_map(
        normalized_trades,
        current_live_leg_keys=current_live_leg_keys,
        today_key=today_key,
        include_today_for_live=False,
        include_realized_day=True,
    )


def _build_active_until_by_leg(
    normalized_trades: List[Dict[str, Any]],
    *,
    current_live_leg_keys: Optional[set] = None,
    today_key: Optional[str] = None,
) -> Dict[Tuple[str, str, str, str], str]:
    return _build_leg_keep_until_map(
        normalized_trades,
        current_live_leg_keys=current_live_leg_keys,
        today_key=today_key,
        include_today_for_live=True,
        include_realized_day=False,
    )


def _build_preserve_until_by_leader(
    preserve_until_by_leg: Dict[Tuple[str, str, str, str], str],
) -> Dict[Tuple[str, str], str]:
    preserve_until_by_leader: Dict[Tuple[str, str], str] = {}
    for (account_name, leader_address, _condition_id, _token_id), keep_until in preserve_until_by_leg.items():
        leader_key = (account_name, leader_address)
        prior = preserve_until_by_leader.get(leader_key)
        if prior is None or keep_until > prior:
            preserve_until_by_leader[leader_key] = keep_until
    return preserve_until_by_leader


def _is_leg_active_on_date(
    leg_key: Tuple[str, str, str, str],
    date_key: str,
    active_until_by_leg: Optional[Dict[Tuple[str, str, str, str], str]],
) -> bool:
    if not active_until_by_leg:
        return True
    active_until = active_until_by_leg.get(leg_key)
    return active_until is None or date_key <= active_until


def _build_preserved_daily_leader_open_rows_from_leg_rows(
    preserved_leg_rows: Dict[Tuple[str, str, str, str, str], Dict[str, Any]],
    base_open_rows: Optional[Dict[Tuple[str, str, str], Dict[str, Any]]] = None,
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    preserved_open_rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    distinct_market_keys: Dict[Tuple[str, str, str], set] = defaultdict(set)

    for key, row in preserved_leg_rows.items():
        leader_row_key = (key[0], key[1], key[2])
        item = preserved_open_rows.get(leader_row_key)
        if item is None:
            item = {
                "date_key": key[0],
                "leader_address": key[1],
                "account_name": key[2],
                "unrealized_pnl": 0.0,
                "market_count": 0,
            }
            preserved_open_rows[leader_row_key] = item
        item["unrealized_pnl"] += _safe_float(row.get("unrealized_pnl_delta"))
        distinct_market_keys[leader_row_key].add(
            str(row.get("condition_id") or "unknown_market").strip() or "unknown_market"
        )

    for leader_row_key, item in preserved_open_rows.items():
        base_count = int((base_open_rows or {}).get(leader_row_key, {}).get("market_count") or 0)
        item["market_count"] = max(base_count, len(distinct_market_keys.get(leader_row_key, set())))

    return preserved_open_rows


def _migrate_daily_leader_pnl_to_pure_attribution_once(
    db: CopyTradeDB,
    today_key: str,
    *,
    force_rebuild: bool = False,
) -> None:
    """Build/rebuild closed-history daily rows while preserving post-cutover open deltas."""
    _ensure_ct_meta_table(db)
    mode = _get_ct_meta_value(db, "daily_leader_pnl_mode")
    is_initial_migration = mode != DAILY_LEADER_PNL_MODE
    if not is_initial_migration and not force_rebuild:
        return

    today_dt = datetime.strptime(today_key, "%Y-%m-%d")
    rebuild_end = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    min_cover_start = (
        today_dt - timedelta(days=DAILY_LEADER_REALIZED_MIN_COVER_DAYS - 1)
    ).strftime("%Y-%m-%d")
    rebuild_start = min(DAILY_LEADER_REALIZED_BASELINE_DATE, min_cover_start)
    action = "migrating" if is_initial_migration else "rebuilding"
    _log(
        f"{action} ct_daily_leader_pnl to {DAILY_LEADER_PNL_MODE}, "
        f"history range={rebuild_start}..{rebuild_end}"
    )

    trade_rows = db.conn.execute(
        "SELECT id, account_name, leader_address, token_id, condition_id, market_slug, outcome, "
        "our_price, our_size, our_usd, filled_size_actual, filled_usd_actual, "
        "exit_status, exit_price, exit_usd, profit, created_at, updated_at, exit_at, official_settlement_at "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' "
        f"AND {_strict_realized_profit_filter_sql()} "
        "AND COALESCE(NULLIF(leader_address, ''), '') <> ''"
    ).fetchall()
    normalized_trades = [_normalize_trade_row(dict(row)) for row in trade_rows]
    current_live_leg_keys = set(_build_current_leg_unrealized(db).keys())
    preserve_until_by_leg = _build_preserve_until_by_leg(
        normalized_trades,
        current_live_leg_keys=current_live_leg_keys,
        today_key=today_key,
    )
    preserve_until_by_leader = _build_preserve_until_by_leader(preserve_until_by_leg)
    base_preserved_open_rows = _load_preserved_daily_leader_open_rows(
        db,
        today_key,
        preserve_until_by_leader=preserve_until_by_leader,
    )
    preserved_leg_rows, _preserved_leg_rows_by_day = _load_preserved_daily_leader_leg_rows(
        db,
        today_key,
        preserve_until_by_leg=preserve_until_by_leg,
    )
    preserved_open_rows = (
        _build_preserved_daily_leader_open_rows_from_leg_rows(
            preserved_leg_rows,
            base_open_rows=base_preserved_open_rows,
        )
        if preserved_leg_rows
        else base_preserved_open_rows
    )

    db.conn.execute("DELETE FROM ct_daily_leader_pnl")
    db.conn.commit()

    backfill_rows: List[Dict[str, Any]] = []
    if rebuild_end >= rebuild_start:
        realization_at_sql = _trade_realization_at_sql()
        realization_date_sql = f"date(datetime({realization_at_sql}, '+8 hours'))"
        strict_realized_filter_sql = _strict_realized_profit_filter_sql()
        rows = db.conn.execute(
            "SELECT "
            f"{realization_date_sql} AS date_key, "
            "LOWER(COALESCE(leader_address, '')) AS leader_address, "
            "COALESCE(NULLIF(account_name, ''), 'default') AS account_name, "
            "COALESCE(SUM(COALESCE(profit, 0)), 0) AS realized_pnl, "
            "COUNT(DISTINCT COALESCE(NULLIF(condition_id, ''), NULLIF(token_id, ''), 'unknown_market')) "
            "AS market_count "
            "FROM ct_trades "
            "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='exited' "
            "AND profit IS NOT NULL "
            f"AND {strict_realized_filter_sql} "
            f"AND {realization_at_sql} IS NOT NULL "
            "AND COALESCE(NULLIF(leader_address, ''), '') <> '' "
            f"AND {realization_date_sql} >= ? "
            f"AND {realization_date_sql} <= ? "
            "GROUP BY date_key, account_name, leader_address "
            "ORDER BY date_key, account_name, leader_address",
            (rebuild_start, rebuild_end),
        ).fetchall()

        for r in rows:
            date_key = str(r["date_key"] or "").strip()
            leader = str(r["leader_address"] or "").strip().lower()
            account_name = str(r["account_name"] or "default").strip() or "default"
            if not date_key or not leader:
                continue
            realized = float(r["realized_pnl"] or 0.0)
            backfill_rows.append(
                {
                    "date_key": date_key,
                    "leader_address": leader,
                    "account_name": account_name,
                    "realized_pnl": realized,
                    "unrealized_pnl": 0.0,
                    "total_pnl": realized,
                    "market_count": int(r["market_count"] or 0),
                }
            )

        open_rows = db.conn.execute(
            "SELECT "
            "token_id, condition_id, market_slug, updated_at, "
            "LOWER(COALESCE(leader_address, '')) AS leader_address, "
            "COALESCE(NULLIF(account_name, ''), 'default') AS account_name, "
            "COALESCE(profit, 0) AS realized_pnl "
            "FROM ct_trades "
            "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='open' "
            "AND profit IS NOT NULL "
            "AND ABS(COALESCE(profit, 0)) > ? "
            "AND COALESCE(NULLIF(leader_address, ''), '') <> '' "
            "AND COALESCE(NULLIF(token_id, ''), '') <> ''",
            (_LEG_EPS,),
        ).fetchall()

        if open_rows:
            token_ids = [str(r["token_id"] or "").strip() for r in open_rows]
            token_context_map = _build_token_resolution_context_map(open_rows)
            session = requests.Session()
            try:
                _prices, settlement_times, _unresolved, live_resolved, live_attempted = _resolve_tokens_with_cache_and_live(
                    db,
                    session,
                    token_ids,
                    token_context_map=token_context_map,
                    fetch_live_for_cached=True,
                )
            finally:
                session.close()

            open_realized_rows: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for row in open_rows:
                token_id = str(row["token_id"] or "").strip()
                settlement_date = _date_key_utc8(settlement_times.get(token_id))
                realized_date = settlement_date or _date_key_utc8(row["updated_at"])
                if not realized_date or realized_date < rebuild_start or realized_date > rebuild_end:
                    continue
                leader = str(row["leader_address"] or "").strip().lower()
                account_name = str(row["account_name"] or "default").strip() or "default"
                if not leader:
                    continue
                key = (realized_date, leader, account_name)
                item = open_realized_rows.get(key)
                if item is None:
                    item = {
                        "date_key": realized_date,
                        "leader_address": leader,
                        "account_name": account_name,
                        "realized_pnl": 0.0,
                        "unrealized_pnl": 0.0,
                        "total_pnl": 0.0,
                        "market_count": 0,
                        "_market_keys": set(),
                    }
                    open_realized_rows[key] = item
                item["realized_pnl"] += float(row["realized_pnl"] or 0.0)
                market_key = (
                    str(row["condition_id"] or "").strip()
                    or str(row["token_id"] or "").strip()
                    or "unknown_market"
                )
                item["_market_keys"].add(market_key)

            if open_realized_rows:
                for item in open_realized_rows.values():
                    item["market_count"] = len(item.pop("_market_keys", set()))
                    item["total_pnl"] = float(item["realized_pnl"] or 0.0)
                    backfill_rows.append(item)
                _log(
                    f"daily leader history included open realized rows: "
                    f"leaders={len(open_realized_rows)} live_resolved={live_resolved}/{live_attempted}"
                )

    if backfill_rows:
        merged_backfill: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in backfill_rows:
            key = _daily_leader_pnl_row_key(row)
            if key is None:
                continue
            item = merged_backfill.get(key)
            if item is None:
                item = {
                    "date_key": key[0],
                    "leader_address": key[1],
                    "account_name": key[2],
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "total_pnl": 0.0,
                    "market_count": 0,
                }
                merged_backfill[key] = item
            item["realized_pnl"] += float(row.get("realized_pnl") or 0.0)
            item["unrealized_pnl"] += float(row.get("unrealized_pnl") or 0.0)
            item["market_count"] += int(row.get("market_count") or 0)
            item["total_pnl"] = float(item["realized_pnl"]) + float(item["unrealized_pnl"])
        backfill_rows = list(merged_backfill.values())

    backfill_rows = _merge_preserved_daily_leader_open_rows(backfill_rows, preserved_open_rows)
    if backfill_rows:
        db.upsert_daily_leader_pnl(backfill_rows)

    _upsert_ct_meta(db, "daily_leader_pnl_mode", DAILY_LEADER_PNL_MODE)
    _upsert_ct_meta(db, "daily_leader_pure_baseline_date", DAILY_LEADER_REALIZED_BASELINE_DATE)
    _upsert_ct_meta(db, "daily_leader_pure_effective_start", rebuild_start)
    _upsert_ct_meta(db, "daily_leader_pure_last_rebuild", datetime.now(timezone.utc).isoformat())
    _upsert_ct_meta(db, "daily_leader_dual_basis_mode", DAILY_LEADER_PNL_MODE)
    _upsert_ct_meta(db, "daily_leader_open_cutover_date", DAILY_LEADER_OPEN_CUTOVER_DATE)
    _log(
        f"daily leader history rebuilt: rows={len(backfill_rows)} "
        f"range={rebuild_start}..{rebuild_end}"
    )


def _build_daily_leader_deltas(
    db: CopyTradeDB, date_key: str, summary_rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """灏嗙疮璁?leader PnL 杞崲涓哄綋鏃ュ閲忓悗鍐欏叆 ct_daily_leader_pnl."""
    _ensure_ct_meta_table(db)

    bootstrap_realized = False
    bootstrap_row = db.conn.execute(
        "SELECT value FROM ct_meta WHERE key='daily_leader_realized_bootstrap_done'"
    ).fetchone()
    if not bootstrap_row:
        bootstrap_realized = True

    rows_by_account: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in summary_rows:
        rows_by_account[str(r.get("account_name") or "default")].append(r)

    daily_rows: List[Dict[str, Any]] = []
    for account_name, rows in rows_by_account.items():
        prev_rows = db.conn.execute(
            "SELECT leader_address, "
            "COALESCE(SUM(realized_pnl), 0) AS realized_before, "
            "COALESCE(SUM(unrealized_pnl), 0) AS unrealized_before "
            "FROM ct_daily_leader_pnl "
            "WHERE account_name=? AND date_key < ? "
            "GROUP BY leader_address",
            (account_name, date_key),
        ).fetchall()
        prev_map: Dict[str, Dict[str, float]] = {}
        for p in prev_rows:
            leader = str(p["leader_address"] or "").lower()
            if not leader:
                continue
            prev_map[leader] = {
                "realized": float(p["realized_before"] or 0.0),
                "unrealized": float(p["unrealized_before"] or 0.0),
            }

        current_map: Dict[str, Dict[str, Any]] = {}
        for s in rows:
            leader = str(s.get("leader_address") or "").lower()
            if not leader:
                continue
            current_map[leader] = s

        leaders = set(prev_map.keys()) | set(current_map.keys())
        for leader in leaders:
            s = current_map.get(leader)
            if s is None:
                cur_realized = 0.0
                cur_unrealized = 0.0
                market_count = 0
            else:
                cur_realized = float(s.get("total_realized_pnl") or 0.0)
                cur_unrealized = float(s.get("total_unrealized_pnl") or 0.0)
                market_count = int(s.get("total_markets") or 0)

            prev = prev_map.get(leader, {"realized": 0.0, "unrealized": 0.0})
            prev_realized = prev["realized"]
            if bootstrap_realized:
                # First run after migration: avoid injecting pre-baseline realized into today.
                prev_realized = cur_realized

            realized_delta = cur_realized - prev_realized
            unrealized_delta = cur_unrealized - prev["unrealized"]

            if (
                abs(realized_delta) < 1e-12
                and abs(unrealized_delta) < 1e-12
                and market_count == 0
            ):
                continue

            daily_rows.append(
                {
                    "date_key": date_key,
                    "leader_address": leader,
                    "account_name": account_name,
                    "realized_pnl": realized_delta,
                    "unrealized_pnl": unrealized_delta,
                    "total_pnl": realized_delta + unrealized_delta,
                    "market_count": market_count,
                }
            )

    if bootstrap_realized:
        _upsert_ct_meta(db, "daily_leader_realized_bootstrap_done", "1")

    return daily_rows


def _build_daily_leader_rows_from_leg_rows(
    leg_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Aggregate the canonical per-leg daily ledger back into daily leader rows."""
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    market_keys_by_row: Dict[Tuple[str, str, str], set] = defaultdict(set)

    for row in leg_rows:
        key = _daily_leader_pnl_row_key(row)
        if key is None:
            continue

        item = merged.get(key)
        if item is None:
            item = {
                "date_key": key[0],
                "leader_address": key[1],
                "account_name": key[2],
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "total_pnl": 0.0,
                "market_count": 0,
            }
            merged[key] = item

        item["realized_pnl"] += _safe_float(row.get("realized_pnl_delta"))
        item["unrealized_pnl"] += _safe_float(row.get("unrealized_pnl_delta"))

        market_key = (
            str(row.get("condition_id") or "").strip()
            or str(row.get("market_slug") or "").strip()
            or str(row.get("token_id") or "").strip()
            or "unknown_market"
        )
        market_keys_by_row[key].add(market_key)

    out: List[Dict[str, Any]] = []
    for key, row in merged.items():
        row["market_count"] = len(market_keys_by_row.get(key, set()))
        row["total_pnl"] = float(row["realized_pnl"]) + float(row["unrealized_pnl"])
        if (
            abs(float(row["realized_pnl"])) <= _LEG_EPS
            and abs(float(row["unrealized_pnl"])) <= _LEG_EPS
            and int(row["market_count"]) <= 0
        ):
            continue
        out.append(row)

    out.sort(key=lambda item: (item["date_key"], item["account_name"], item["leader_address"]))
    return out


_LEG_EPS = 1e-9
_UTC8 = timezone(timedelta(hours=8))
_ENTRY_FILL_REPAIR_REL_GAP = 0.10
_ENTRY_FILL_REPAIR_ABS_GAP = 0.05
_ENTRY_FILL_REPAIR_LIMIT_GAP = 0.05


def _safe_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    if abs(out) < _LEG_EPS:
        return 0.0
    return out


def repair_overstated_entry_fill_costs(db: CopyTradeDB) -> Dict[str, Any]:
    """Repair entry fills that were overstated by using the buy limit price as cost basis."""
    rows = db.conn.execute(
        "SELECT id, account_name, requested_price, requested_size, requested_usd, "
        "filled_size_actual, filled_usd_actual, our_price, our_size, our_usd, "
        "our_limit_price, our_filled_price, exit_status, exit_price, exit_usd, profit "
        "FROM ct_trades "
        "WHERE our_order_id IS NOT NULL AND our_side='BUY' AND status IN ('filled','partially_filled')"
    ).fetchall()
    if not rows:
        return {"repaired": 0, "accounts": 0, "profit_delta": 0.0}

    repaired = 0
    profit_delta_total = 0.0
    touched_accounts: Dict[str, int] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    for raw_row in rows:
        row = dict(raw_row)
        requested_size = max(_safe_float(row.get("requested_size")), 0.0)
        requested_usd = max(_safe_float(row.get("requested_usd")), 0.0)
        if requested_size <= _LEG_EPS or requested_usd <= _LEG_EPS:
            continue

        requested_price = _safe_float(row.get("requested_price"))
        corrected_entry_price = requested_usd / requested_size
        request_reference_price = requested_price if requested_price > _LEG_EPS else corrected_entry_price
        filled_size_actual = max(_safe_float(row.get("filled_size_actual")), requested_size)
        filled_usd_actual = max(_safe_float(row.get("filled_usd_actual")), 0.0)
        our_limit_price = _safe_float(row.get("our_limit_price"))
        our_price = _safe_float(row.get("our_price"))
        our_filled_price = _safe_float(row.get("our_filled_price"))

        inflated_usd = filled_usd_actual > (
            requested_usd + max(_ENTRY_FILL_REPAIR_ABS_GAP, requested_usd * _ENTRY_FILL_REPAIR_REL_GAP)
        )
        limit_gap = (
            request_reference_price > _LEG_EPS
            and our_limit_price > request_reference_price + _ENTRY_FILL_REPAIR_LIMIT_GAP
        )
        price_stuck_to_limit = (
            (our_price > _LEG_EPS and abs(our_price - our_limit_price) <= 1e-6)
            or (our_filled_price > _LEG_EPS and abs(our_filled_price - our_limit_price) <= 1e-6)
        )
        if not (inflated_usd and limit_gap and price_stuck_to_limit):
            continue

        remaining_size = max(_safe_float(row.get("our_size")), 0.0)
        corrected_total_cost = filled_size_actual * corrected_entry_price
        corrected_remaining_cost = remaining_size * corrected_entry_price
        sold_size = max(filled_size_actual - remaining_size, 0.0)
        exit_status = str(row.get("exit_status") or "").strip().lower()
        exit_price = max(_safe_float(row.get("exit_price")), 0.0)
        exit_usd = max(_safe_float(row.get("exit_usd")), 0.0)
        proceeds = exit_usd
        if proceeds <= _LEG_EPS and exit_price > _LEG_EPS:
            realized_size = filled_size_actual if exit_status == "exited" else sold_size
            proceeds = realized_size * exit_price

        existing_profit = row.get("profit")
        corrected_profit: Optional[float]
        if exit_status == "exited":
            corrected_profit = proceeds - corrected_total_cost
        elif sold_size > _LEG_EPS or proceeds > _LEG_EPS or existing_profit is not None:
            corrected_profit = proceeds - (sold_size * corrected_entry_price)
        else:
            corrected_profit = None

        existing_profit_value = _safe_float(existing_profit) if existing_profit is not None else None
        profit_delta = 0.0
        if corrected_profit is not None:
            profit_delta = corrected_profit - (existing_profit_value or 0.0)

        db.conn.execute(
            "UPDATE ct_trades SET our_price=?, our_usd=?, filled_usd_actual=?, our_filled_price=?, "
            "profit=?, updated_at=? WHERE id=?",
            (
                corrected_entry_price,
                corrected_remaining_cost,
                corrected_total_cost,
                corrected_entry_price,
                corrected_profit,
                now_iso,
                int(row["id"]),
            ),
        )
        repaired += 1
        profit_delta_total += profit_delta
        account_name = str(row.get("account_name") or "default")
        touched_accounts[account_name] = touched_accounts.get(account_name, 0) + 1

    if repaired:
        db.conn.commit()
        _log(
            "[entry-fill-repair] "
            f"repaired={repaired} accounts={len(touched_accounts)} "
            f"profit_delta={profit_delta_total:.2f}"
        )

    return {
        "repaired": repaired,
        "accounts": len(touched_accounts),
        "profit_delta": profit_delta_total,
    }


def _date_key_utc8(value: Any) -> Optional[str]:
    parsed = _parse_datetime_utc(value)
    if not parsed:
        return None
    try:
        dt = datetime.fromisoformat(parsed)
    except ValueError:
        return None
    return dt.astimezone(_UTC8).strftime("%Y-%m-%d")


def _is_resolution_exit(*, exit_status: Any, exit_usd: Any) -> bool:
    status = str(exit_status or "").strip().lower()
    proceeds = max(_safe_float(exit_usd), 0.0)
    return status == "exited" and proceeds <= _LEG_EPS


def _effective_trade_realization_at(row: Dict[str, Any]) -> Optional[str]:
    if _is_resolution_exit(
        exit_status=row.get("exit_status"),
        exit_usd=row.get("exit_usd"),
    ):
        return _parse_datetime_utc(row.get("official_settlement_at"))
    return _parse_datetime_utc(row.get("exit_at"))


def _trade_realization_at_sql(prefix: str = "") -> str:
    def col(name: str) -> str:
        return f"{prefix}{name}" if prefix else name

    return (
        "CASE "
        f"WHEN {col('exit_status')}='exited' AND COALESCE({col('exit_usd')}, 0) <= {_LEG_EPS} "
        f"THEN {col('official_settlement_at')} "
        f"ELSE {col('exit_at')} END"
    )


def _strict_realized_profit_filter_sql(prefix: str = "") -> str:
    def col(name: str) -> str:
        return f"{prefix}{name}" if prefix else name

    return (
        "("
        f"{col('exit_status')} != 'exited' "
        f"OR COALESCE({col('exit_usd')}, 0) > {_LEG_EPS} "
        f"OR {col('official_settlement_at')} IS NOT NULL"
        ")"
    )


def _normalized_condition_id(row: Dict[str, Any]) -> str:
    condition_id = str(row.get("condition_id") or "").strip()
    if condition_id:
        return condition_id
    token_id = str(row.get("token_id") or "").strip()
    if token_id:
        return f"token:{token_id}"
    market_slug = str(row.get("market_slug") or "").strip()
    if market_slug:
        return f"slug:{market_slug}"
    return "unknown_market"


def _normalized_outcome(row: Dict[str, Any]) -> str:
    outcome = str(row.get("outcome") or "").strip()
    return outcome or "unknown"


def _normalized_token_id(row: Dict[str, Any]) -> str:
    token_id = str(row.get("token_id") or "").strip()
    if token_id:
        return token_id
    return f"{_normalized_condition_id(row)}:{_normalized_outcome(row)}"


def _estimate_original_size(
    buy_price: float,
    current_size: float,
    exit_usd: float,
    profit: float,
    filled_size_actual: float,
    filled_usd_actual: float,
    exit_price: float,
) -> float:
    if filled_size_actual > _LEG_EPS:
        return filled_size_actual

    sold_cost = exit_usd - profit
    if buy_price > _LEG_EPS and sold_cost > _LEG_EPS:
        size = current_size + (sold_cost / buy_price)
        if size > _LEG_EPS:
            return size

    if buy_price > _LEG_EPS and filled_usd_actual > _LEG_EPS:
        return filled_usd_actual / buy_price

    if buy_price > _LEG_EPS and abs(exit_price - buy_price) > _LEG_EPS and abs(profit) > _LEG_EPS:
        size = abs(profit / (exit_price - buy_price))
        if size > _LEG_EPS:
            return size

    return max(current_size, 0.0)


def _estimate_original_cost(
    buy_price: float,
    current_cost: float,
    exit_usd: float,
    profit: float,
    filled_usd_actual: float,
    original_size: float,
) -> float:
    if filled_usd_actual > _LEG_EPS:
        return filled_usd_actual

    sold_cost = exit_usd - profit
    if current_cost > _LEG_EPS or sold_cost > _LEG_EPS:
        total = max(current_cost, 0.0) + max(sold_cost, 0.0)
        if total > _LEG_EPS:
            return total

    if buy_price > _LEG_EPS and original_size > _LEG_EPS:
        return buy_price * original_size
    return 0.0


def _estimate_sold_size(
    buy_price: float,
    exit_usd: float,
    profit: float,
    exit_price: float,
    original_size: float,
) -> float:
    if exit_usd <= _LEG_EPS:
        return 0.0

    sold_cost = exit_usd - profit
    if buy_price > _LEG_EPS and sold_cost > _LEG_EPS:
        est = sold_cost / buy_price
        if est > _LEG_EPS:
            if original_size > _LEG_EPS:
                return min(est, original_size)
            return est

    if exit_price > _LEG_EPS:
        est = exit_usd / exit_price
        if est > _LEG_EPS:
            if original_size > _LEG_EPS:
                return min(est, original_size)
            return est

    return 0.0


def _normalize_trade_row(row: Dict[str, Any]) -> Dict[str, Any]:
    account_name = str(row.get("account_name") or "default").strip() or "default"
    leader_address = str(row.get("leader_address") or "").strip().lower()
    condition_id = _normalized_condition_id(row)
    token_id = _normalized_token_id(row)
    market_slug = str(row.get("market_slug") or "").strip() or condition_id
    outcome = _normalized_outcome(row)
    buy_price = _safe_float(row.get("our_price"))
    current_size = max(_safe_float(row.get("our_size")), 0.0)
    current_cost = max(_safe_float(row.get("our_usd")), 0.0)
    filled_size_actual = _safe_float(row.get("filled_size_actual"))
    filled_usd_actual = _safe_float(row.get("filled_usd_actual"))
    exit_usd = max(_safe_float(row.get("exit_usd")), 0.0)
    profit = _safe_float(row.get("profit"))
    exit_price = _safe_float(row.get("exit_price"))
    original_size = _estimate_original_size(
        buy_price=buy_price,
        current_size=current_size,
        exit_usd=exit_usd,
        profit=profit,
        filled_size_actual=filled_size_actual,
        filled_usd_actual=filled_usd_actual,
        exit_price=exit_price,
    )
    original_cost = _estimate_original_cost(
        buy_price=buy_price,
        current_cost=current_cost,
        exit_usd=exit_usd,
        profit=profit,
        filled_usd_actual=filled_usd_actual,
        original_size=original_size,
    )
    sold_size = _estimate_sold_size(
        buy_price=buy_price,
        exit_usd=exit_usd,
        profit=profit,
        exit_price=exit_price,
        original_size=original_size,
    )

    exit_status = str(row.get("exit_status") or "open").strip() or "open"
    settled_size = 0.0
    if exit_status == "exited":
        if exit_usd <= _LEG_EPS:
            settled_size = original_size
        elif original_size > sold_size + _LEG_EPS:
            settled_size = max(original_size - sold_size, 0.0)

    remaining_size_now = 0.0
    remaining_cost_now = 0.0
    if exit_status == "open":
        remaining_size_now = current_size if current_size > _LEG_EPS else max(original_size - sold_size, 0.0)
        remaining_cost_now = current_cost if current_cost > _LEG_EPS else max(original_cost - max(exit_usd - profit, 0.0), 0.0)

    created_date = _date_key_utc8(row.get("created_at"))
    updated_date = _date_key_utc8(row.get("updated_at"))
    exit_date = _date_key_utc8(row.get("exit_at"))
    official_settlement_at = _parse_datetime_utc(row.get("official_settlement_at"))
    is_resolution_exit = _is_resolution_exit(
        exit_status=exit_status,
        exit_usd=exit_usd,
    )
    realized_at = _effective_trade_realization_at(row) if exit_status == "exited" else None
    realized_date = _date_key_utc8(realized_at)

    return {
        "trade_id": row.get("id"),
        "account_name": account_name,
        "leader_address": leader_address,
        "condition_id": condition_id,
        "token_id": token_id,
        "market_slug": market_slug,
        "outcome": outcome,
        "buy_price": buy_price,
        "original_size": original_size,
        "original_cost": original_cost,
        "sold_size_total": sold_size,
        "sell_proceeds_total": exit_usd,
        "settled_size_total": settled_size,
        "remaining_size_now": remaining_size_now,
        "remaining_cost_now": remaining_cost_now,
        "profit_total": profit,
        "exit_status": exit_status,
        "created_date": created_date,
        "updated_date": updated_date,
        "exit_date": exit_date,
        "official_settlement_at": official_settlement_at,
        "realized_at": realized_at,
        "realized_date": realized_date,
        "is_resolution_exit": is_resolution_exit,
        "leg_key": (account_name, leader_address, condition_id, token_id),
        "leader_key": (account_name, leader_address),
    }


def _allocate_target_amount(
    current_values: Dict[Tuple[str, str, str, str], float],
    target_total: float,
    candidate_keys: List[Tuple[str, str, str, str]],
    fallback_weights: Dict[Tuple[str, str, str, str], float],
) -> Dict[Tuple[str, str, str, str], float]:
    out = dict(current_values)
    if not candidate_keys:
        return out

    keys = list(dict.fromkeys(candidate_keys))
    current_total = sum(out.get(key, 0.0) for key in keys)
    residual = target_total - current_total
    if abs(residual) <= _LEG_EPS:
        return out

    weights: Dict[Tuple[str, str, str, str], float] = {}
    for key in keys:
        weight = abs(out.get(key, 0.0))
        if weight <= _LEG_EPS:
            weight = abs(fallback_weights.get(key, 0.0))
        if weight <= _LEG_EPS:
            weight = 1.0
        weights[key] = weight

    total_weight = sum(weights.values()) or float(len(keys))
    allocated = 0.0
    for key in keys[:-1]:
        delta = residual * (weights[key] / total_weight)
        out[key] = out.get(key, 0.0) + delta
        allocated += delta
    last_key = keys[-1]
    out[last_key] = out.get(last_key, 0.0) + (residual - allocated)
    return out


def _close_state_from_metrics(
    open_size_eod: float,
    sell_size: float,
    settled_size: float,
    market_closed_by_eod: bool = False,
) -> str:
    has_open = open_size_eod > _LEG_EPS
    has_sell = sell_size > _LEG_EPS
    has_settled = settled_size > _LEG_EPS
    if (has_open and (has_sell or has_settled)) or (has_sell and has_settled):
        return "mixed"
    if has_open:
        if market_closed_by_eod:
            return "redeemable"
        return "open"
    if has_settled:
        return "settled"
    if has_sell:
        return "sold"
    return "flat"


def _build_current_leg_unrealized(db: CopyTradeDB) -> Dict[Tuple[str, str, str, str], float]:
    account_addrs = _load_account_addresses()
    if not account_addrs:
        return {}

    rows = db.conn.execute(
        "SELECT id, account_name, leader_address, token_id, condition_id, market_slug, outcome, "
        "our_price, our_size, our_usd, filled_size_actual, filled_usd_actual, "
        "exit_status, exit_price, exit_usd, profit, created_at, updated_at, exit_at, official_settlement_at "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='open' "
        "AND COALESCE(NULLIF(leader_address, ''), '') <> ''"
    ).fetchall()
    if not rows:
        return {}

    open_trades = [_normalize_trade_row(dict(row)) for row in rows]
    trades_by_account_token: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for trade in open_trades:
        if trade["remaining_size_now"] <= _LEG_EPS:
            continue
        token_id = str(trade.get("token_id") or "").strip()
        if not token_id or ":" in token_id:
            continue
        trades_by_account_token[(trade["account_name"], token_id)].append(trade)

    current_unrealized: Dict[Tuple[str, str, str, str], float] = defaultdict(float)
    session = requests.Session()
    for account_name, address in account_addrs.items():
        account_tokens = {
            token_id for acct, token_id in trades_by_account_token.keys() if acct == account_name
        }
        if not account_tokens:
            continue
        positions = _fetch_onchain_positions(session, address)
        for token_id in account_tokens:
            pos = positions.get(token_id)
            if pos is None:
                continue
            unrealized = _safe_float(pos.get("unrealized_pnl"))
            leg_trades = trades_by_account_token.get((account_name, token_id), [])
            if not leg_trades:
                continue
            total_weight = sum(
                max(trade["remaining_cost_now"], trade["remaining_size_now"] * trade["buy_price"], 0.0)
                for trade in leg_trades
            )
            if total_weight <= _LEG_EPS:
                total_weight = sum(max(trade["remaining_size_now"], 0.0) for trade in leg_trades)
            if total_weight <= _LEG_EPS:
                share = unrealized / len(leg_trades)
                for trade in leg_trades:
                    current_unrealized[trade["leg_key"]] += share
                continue
            for trade in leg_trades:
                weight = max(
                    trade["remaining_cost_now"],
                    trade["remaining_size_now"] * trade["buy_price"],
                    0.0,
                )
                if weight <= _LEG_EPS:
                    weight = max(trade["remaining_size_now"], 0.0)
                current_unrealized[trade["leg_key"]] += unrealized * (weight / total_weight)

    return dict(current_unrealized)


def _build_open_leg_snapshot(
    trades: List[Dict[str, Any]],
    date_key: str,
    current_date_key: str,
    *,
    active_until_by_leg: Optional[Dict[Tuple[str, str, str, str], str]] = None,
) -> Dict[Tuple[str, str, str, str], Dict[str, float]]:
    snapshot: Dict[Tuple[str, str, str, str], Dict[str, float]] = {}
    for trade in trades:
        created_date = trade.get("created_date")
        if not created_date or created_date > date_key:
            continue
        if not _is_leg_active_on_date(trade["leg_key"], date_key, active_until_by_leg):
            continue
        close_date = trade.get("realized_date") if trade.get("exit_status") == "exited" else None
        if close_date and close_date <= date_key:
            continue

        if date_key == current_date_key and trade.get("exit_status") == "open":
            open_size = trade["remaining_size_now"]
            open_cost = trade["remaining_cost_now"]
            if open_size <= _LEG_EPS:
                open_size = max(trade["original_size"] - trade["sold_size_total"], 0.0)
            if open_cost <= _LEG_EPS:
                open_cost = max(trade["original_cost"] - max(trade["sell_proceeds_total"] - trade["profit_total"], 0.0), 0.0)
        else:
            open_size = trade["original_size"]
            open_cost = trade["original_cost"]

        if open_size <= _LEG_EPS and open_cost <= _LEG_EPS:
            continue

        item = snapshot.setdefault(trade["leg_key"], {"open_size": 0.0, "open_cost": 0.0})
        item["open_size"] += max(open_size, 0.0)
        item["open_cost"] += max(open_cost, 0.0)
    return snapshot


def _rebuild_daily_leader_market_leg_pnl(
    db: CopyTradeDB,
    current_date_key: str,
) -> List[Dict[str, Any]]:
    daily_rows = db.get_daily_leader_pnl_history()
    if not daily_rows:
        return []

    trade_rows = db.conn.execute(
        "SELECT id, account_name, leader_address, token_id, condition_id, market_slug, outcome, "
        "our_price, our_size, our_usd, filled_size_actual, filled_usd_actual, "
        "exit_status, exit_price, exit_usd, profit, created_at, updated_at, exit_at, official_settlement_at "
        "FROM ct_trades "
        "WHERE status IN ('filled','partially_filled') AND our_side='BUY' "
        f"AND {_strict_realized_profit_filter_sql()} "
        "AND COALESCE(NULLIF(leader_address, ''), '') <> ''"
    ).fetchall()
    normalized_trades = [_normalize_trade_row(dict(row)) for row in trade_rows]
    current_exact_unrealized = _build_current_leg_unrealized(db)
    current_live_leg_keys = set(current_exact_unrealized.keys())
    preserve_until_by_leg = _build_preserve_until_by_leg(
        normalized_trades,
        current_live_leg_keys=current_live_leg_keys,
        today_key=current_date_key,
    )
    active_until_by_leg = _build_active_until_by_leg(
        normalized_trades,
        current_live_leg_keys=current_live_leg_keys,
        today_key=current_date_key,
    )
    preserved_leg_rows, preserved_leg_rows_by_day = _load_preserved_daily_leader_leg_rows(
        db,
        current_date_key,
        preserve_until_by_leg=preserve_until_by_leg,
    )
    open_token_ids = sorted(
        {
            str(trade.get("token_id") or "").strip()
            for trade in normalized_trades
            if trade.get("exit_status") == "open" and trade.get("remaining_size_now", 0.0) > _LEG_EPS
        }
    )
    token_context_map = _build_token_resolution_context_map(
        [
            trade
            for trade in normalized_trades
            if trade.get("exit_status") == "open" and trade.get("remaining_size_now", 0.0) > _LEG_EPS
        ]
    )
    settlement_date_by_token: Dict[str, str] = {}
    if open_token_ids:
        session = requests.Session()
        try:
            _prices, settlement_times, _unresolved, _live_resolved, _live_attempted = _resolve_tokens_with_cache_and_live(
                db,
                session,
                open_token_ids,
                token_context_map=token_context_map,
                fetch_live_for_cached=True,
            )
        finally:
            session.close()
        for token_id, settle in settlement_times.items():
            settlement_date = _date_key_utc8(settle)
            if settlement_date:
                settlement_date_by_token[token_id] = settlement_date

    leg_meta: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    trades_by_leader: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    leader_legs: Dict[Tuple[str, str], set] = defaultdict(set)
    buy_stats: Dict[Tuple[str, Tuple[str, str, str, str]], Dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "size": 0.0, "cost": 0.0}
    )
    sell_stats: Dict[Tuple[str, Tuple[str, str, str, str]], Dict[str, float]] = defaultdict(
        lambda: {"count": 0.0, "size": 0.0, "proceeds": 0.0}
    )
    settled_stats: Dict[Tuple[str, Tuple[str, str, str, str]], float] = defaultdict(float)
    realized_base: Dict[Tuple[str, Tuple[str, str, str, str]], float] = defaultdict(float)
    activity_dates_by_leader: Dict[Tuple[str, str], set] = defaultdict(set)
    realized_totals_by_day: Dict[Tuple[str, str, str], float] = defaultdict(float)

    for trade in normalized_trades:
        leg_key = trade["leg_key"]
        leader_key = trade["leader_key"]
        leg_meta[leg_key] = {
            "account_name": trade["account_name"],
            "leader_address": trade["leader_address"],
            "condition_id": trade["condition_id"],
            "token_id": trade["token_id"],
            "market_slug": trade["market_slug"],
            "outcome": trade["outcome"],
        }
        trades_by_leader[leader_key].append(trade)
        leader_legs[leader_key].add(leg_key)

        created_date = trade.get("created_date")
        if created_date:
            activity_dates_by_leader[leader_key].add(created_date)
            stats = buy_stats[(created_date, leg_key)]
            stats["count"] += 1
            stats["size"] += trade["original_size"]
            stats["cost"] += trade["original_cost"]

        if trade["sell_proceeds_total"] > _LEG_EPS:
            sell_date = trade.get("exit_date") if trade["exit_status"] == "exited" else trade.get("updated_date")
            if sell_date:
                activity_dates_by_leader[leader_key].add(sell_date)
                stats = sell_stats[(sell_date, leg_key)]
                stats["count"] += 1
                stats["size"] += trade["sold_size_total"]
                stats["proceeds"] += trade["sell_proceeds_total"]

        if trade["settled_size_total"] > _LEG_EPS and trade.get("realized_date"):
            activity_dates_by_leader[leader_key].add(trade["realized_date"])
            settled_stats[(trade["realized_date"], leg_key)] += trade["settled_size_total"]

        if trade["profit_total"] != 0:
            if trade["exit_status"] == "exited":
                realized_date = trade.get("realized_date")
            else:
                realized_date = settlement_date_by_token.get(str(trade.get("token_id") or "").strip()) or trade.get("updated_date")
            if realized_date:
                activity_dates_by_leader[leader_key].add(realized_date)
                realized_base[(realized_date, leg_key)] += trade["profit_total"]
                realized_totals_by_day[(realized_date, leader_key[0], leader_key[1])] += trade["profit_total"]

    current_exact_realized: Dict[Tuple[str, str, str, str], float] = defaultdict(float)
    for trade in normalized_trades:
        current_exact_realized[trade["leg_key"]] += trade["profit_total"]

    target_by_day: Dict[str, Dict[Tuple[str, str], Dict[str, float]]] = defaultdict(dict)
    for row in daily_rows:
        account_name = str(row.get("account_name") or "default").strip() or "default"
        leader_address = str(row.get("leader_address") or "").strip().lower()
        date_key = str(row.get("date_key") or "").strip()
        if not date_key or not leader_address:
            continue
        target_by_day[date_key][(account_name, leader_address)] = {
            "realized": _safe_float(row.get("realized_pnl")),
            "unrealized": _safe_float(row.get("unrealized_pnl")),
            "total": _safe_float(row.get("total_pnl")),
        }

    for leader_key, activity_dates in activity_dates_by_leader.items():
        account_name, leader_address = leader_key
        for date_key in activity_dates:
            default_realized = realized_totals_by_day.get((date_key, account_name, leader_address), 0.0)
            target_by_day[date_key].setdefault(
                leader_key,
                {
                    "realized": default_realized,
                    "unrealized": 0.0,
                    "total": default_realized,
                },
            )

    all_dates = sorted(target_by_day.keys())
    cumulative_state: Dict[Tuple[str, str, str, str], Dict[str, float]] = defaultdict(
        lambda: {"realized": 0.0, "unrealized": 0.0}
    )
    output_rows: List[Dict[str, Any]] = []

    for date_key in all_dates:
        leaders_for_day = target_by_day.get(date_key, {})
        for leader_key, target in leaders_for_day.items():
            account_name, leader_address = leader_key
            trades = trades_by_leader.get(leader_key, [])
            all_leg_keys = {
                leg_key
                for leg_key in leader_legs.get(leader_key, set())
                if _is_leg_active_on_date(leg_key, date_key, active_until_by_leg)
            }
            preserve_open_history = _is_open_history_preserve_day(date_key, current_date_key)
            preserved_day_rows = preserved_leg_rows_by_day.get((date_key, account_name, leader_address), [])
            if preserve_open_history:
                for preserved_row in preserved_day_rows:
                    preserved_leg_key = (
                        account_name,
                        leader_address,
                        str(preserved_row.get("condition_id") or "unknown_market").strip() or "unknown_market",
                        str(preserved_row.get("token_id") or "unknown_token").strip() or "unknown_token",
                    )
                    all_leg_keys.add(preserved_leg_key)
                    leg_meta.setdefault(
                        preserved_leg_key,
                        {
                            "account_name": account_name,
                            "leader_address": leader_address,
                            "condition_id": preserved_leg_key[2],
                            "token_id": preserved_leg_key[3],
                            "market_slug": preserved_row.get("market_slug") or preserved_leg_key[2],
                            "outcome": preserved_row.get("outcome") or "unknown",
                        },
                    )

            open_snapshot = _build_open_leg_snapshot(
                trades,
                date_key,
                current_date_key,
                active_until_by_leg=active_until_by_leg,
            )
            all_leg_keys.update(open_snapshot.keys())
            all_leg_keys.update(
                leg_key for (dkey, leg_key) in buy_stats.keys()
                if dkey == date_key and leg_key[:2] == leader_key
            )
            all_leg_keys.update(
                leg_key for (dkey, leg_key) in sell_stats.keys()
                if dkey == date_key and leg_key[:2] == leader_key
            )
            all_leg_keys.update(
                leg_key for (dkey, leg_key) in settled_stats.keys()
                if dkey == date_key and leg_key[:2] == leader_key
            )

            realized_delta_map: Dict[Tuple[str, str, str, str], float] = {}
            if date_key == current_date_key:
                exact_leg_keys = {
                    leg_key
                    for leg_key in current_exact_realized.keys()
                    if leg_key[:2] == leader_key
                }
                exact_leg_keys.update(
                    leg_key
                    for leg_key in cumulative_state.keys()
                    if leg_key[:2] == leader_key and _is_leg_active_on_date(leg_key, date_key, active_until_by_leg)
                )
                for leg_key in exact_leg_keys:
                    prev_realized = cumulative_state[leg_key]["realized"]
                    realized_delta = current_exact_realized.get(leg_key, 0.0) - prev_realized
                    if abs(realized_delta) > _LEG_EPS:
                        realized_delta_map[leg_key] = realized_delta
                        all_leg_keys.add(leg_key)
            else:
                realized_delta_map = {
                    leg_key: value
                    for (dkey, leg_key), value in realized_base.items()
                    if dkey == date_key and leg_key[:2] == leader_key and abs(value) > _LEG_EPS
                }
                realized_candidates = list(realized_delta_map.keys())
                if not realized_candidates:
                    realized_candidates = [
                        leg_key for leg_key in all_leg_keys
                        if buy_stats.get((date_key, leg_key))
                        or sell_stats.get((date_key, leg_key))
                        or settled_stats.get((date_key, leg_key), 0.0) > _LEG_EPS
                    ]
                if not realized_candidates:
                    realized_candidates = list(all_leg_keys)
                realized_weights = {
                    leg_key: open_snapshot.get(leg_key, {}).get("open_cost", 0.0)
                    + buy_stats.get((date_key, leg_key), {}).get("cost", 0.0)
                    + abs(realized_delta_map.get(leg_key, 0.0))
                    for leg_key in realized_candidates
                }
                realized_delta_map = _allocate_target_amount(
                    current_values=realized_delta_map,
                    target_total=target["realized"],
                    candidate_keys=realized_candidates,
                    fallback_weights=realized_weights,
                )

            use_exact_current_unrealized = date_key == current_date_key and bool(current_exact_unrealized)
            unrealized_delta_map: Dict[Tuple[str, str, str, str], float] = {}
            if use_exact_current_unrealized:
                exact_leg_keys = {
                    leg_key
                    for leg_key in current_exact_unrealized.keys()
                    if leg_key[:2] == leader_key
                }
                exact_leg_keys.update(
                    leg_key
                    for leg_key in cumulative_state.keys()
                    if leg_key[:2] == leader_key and _is_leg_active_on_date(leg_key, date_key, active_until_by_leg)
                )
                for leg_key in exact_leg_keys:
                    prev_unrealized = cumulative_state[leg_key]["unrealized"]
                    unrealized_delta = current_exact_unrealized.get(leg_key, 0.0) - prev_unrealized
                    if abs(unrealized_delta) > _LEG_EPS:
                        unrealized_delta_map[leg_key] = unrealized_delta
                        all_leg_keys.add(leg_key)
            else:
                unrealized_candidates = [
                    leg_key for leg_key, item in open_snapshot.items()
                    if leg_key[:2] == leader_key and (
                        item.get("open_cost", 0.0) > _LEG_EPS or item.get("open_size", 0.0) > _LEG_EPS
                    )
                ]
                if not unrealized_candidates:
                    unrealized_candidates = list(all_leg_keys)
                unrealized_weights = {
                    leg_key: open_snapshot.get(leg_key, {}).get("open_cost", 0.0)
                    or open_snapshot.get(leg_key, {}).get("open_size", 0.0)
                    or buy_stats.get((date_key, leg_key), {}).get("cost", 0.0)
                    or 1.0
                    for leg_key in unrealized_candidates
                }
                unrealized_delta_map = _allocate_target_amount(
                    current_values={},
                    target_total=target["unrealized"],
                    candidate_keys=unrealized_candidates,
                    fallback_weights=unrealized_weights,
                )
            if preserve_open_history and preserved_day_rows:
                unrealized_delta_map = {}

            all_leg_keys.update(realized_delta_map.keys())
            all_leg_keys.update(unrealized_delta_map.keys())

            emitted_rows: List[Dict[str, Any]] = []
            for leg_key in sorted(all_leg_keys):
                meta = leg_meta.get(leg_key)
                preserve_key = (date_key, leader_address, account_name, leg_key[2], leg_key[3])
                preserved_row = preserved_leg_rows.get(preserve_key) if preserve_open_history else None
                if not meta and preserved_row is not None:
                    meta = {
                        "account_name": account_name,
                        "leader_address": leader_address,
                        "condition_id": leg_key[2],
                        "token_id": leg_key[3],
                        "market_slug": preserved_row.get("market_slug") or leg_key[2],
                        "outcome": preserved_row.get("outcome") or "unknown",
                    }
                    leg_meta[leg_key] = meta
                if not meta:
                    continue
                buy_item = buy_stats.get((date_key, leg_key), {"count": 0.0, "size": 0.0, "cost": 0.0})
                sell_item = sell_stats.get((date_key, leg_key), {"count": 0.0, "size": 0.0, "proceeds": 0.0})
                settled_size = settled_stats.get((date_key, leg_key), 0.0)
                realized_delta = realized_delta_map.get(leg_key, 0.0)
                unrealized_delta = unrealized_delta_map.get(leg_key, 0.0)
                if preserved_row is not None and (
                    settled_size > _LEG_EPS
                    or sell_item["size"] > _LEG_EPS
                    or abs(realized_delta) > _LEG_EPS
                ):
                    preserved_row = None

                prev_state = cumulative_state[leg_key]
                realized_eod = prev_state["realized"] + realized_delta

                snapshot = open_snapshot.get(leg_key, {})
                open_size_eod = snapshot.get("open_size", 0.0)
                settlement_date = settlement_date_by_token.get(meta["token_id"])
                market_closed_by_eod = bool(settlement_date and settlement_date <= date_key)
                if use_exact_current_unrealized:
                    exact_unrealized_eod = _safe_float(current_exact_unrealized.get(leg_key, 0.0))
                    # Once a market was already settled before today, keep today's leg flat and
                    # trust the current exact EOD value instead of rolling historical drift into it.
                    if settlement_date and settlement_date < current_date_key:
                        unrealized_delta = 0.0
                    else:
                        unrealized_delta = exact_unrealized_eod - prev_state["unrealized"]
                    unrealized_eod = exact_unrealized_eod
                else:
                    unrealized_eod = prev_state["unrealized"] + unrealized_delta
                if preserved_row is not None:
                    open_size_eod = max(_safe_float(preserved_row.get("open_size_eod")), 0.0)
                    unrealized_delta = _safe_float(preserved_row.get("unrealized_pnl_delta"))
                    unrealized_eod = _safe_float(preserved_row.get("unrealized_pnl_eod"))
                if (
                    market_closed_by_eod
                    and preserved_row is None
                    and not use_exact_current_unrealized
                    and abs(prev_state["unrealized"]) > _LEG_EPS
                    and abs(unrealized_delta) <= _LEG_EPS
                ):
                    unrealized_delta = -prev_state["unrealized"]
                    unrealized_eod = 0.0
                close_state = _close_state_from_metrics(
                    open_size_eod=open_size_eod,
                    sell_size=sell_item["size"],
                    settled_size=settled_size,
                    market_closed_by_eod=market_closed_by_eod,
                )

                row = {
                    "date_key": date_key,
                    "leader_address": meta["leader_address"],
                    "account_name": meta["account_name"],
                    "condition_id": meta["condition_id"],
                    "token_id": meta["token_id"],
                    "market_slug": meta["market_slug"],
                    "outcome": meta["outcome"],
                    "buy_fill_count": int(round(buy_item["count"])),
                    "buy_size": buy_item["size"],
                    "buy_cost_usd": buy_item["cost"],
                    "sell_fill_count": int(round(sell_item["count"])),
                    "sell_size": sell_item["size"],
                    "sell_proceeds_usd": sell_item["proceeds"],
                    "settled_size": settled_size,
                    "open_size_eod": open_size_eod,
                    "close_state_eod": close_state,
                    "realized_pnl_delta": realized_delta,
                    "unrealized_pnl_delta": unrealized_delta,
                    "total_pnl_delta": realized_delta + unrealized_delta,
                    "realized_pnl_eod": realized_eod,
                    "unrealized_pnl_eod": unrealized_eod,
                    "total_pnl_eod": realized_eod + unrealized_eod,
                }

                has_activity = (
                    row["buy_fill_count"] > 0
                    or row["sell_fill_count"] > 0
                    or row["settled_size"] > _LEG_EPS
                    or abs(row["realized_pnl_delta"]) > _LEG_EPS
                    or abs(row["unrealized_pnl_delta"]) > _LEG_EPS
                    or row["open_size_eod"] > _LEG_EPS
                )
                if not has_activity:
                    continue

                emitted_rows.append(row)
                cumulative_state[leg_key]["realized"] = realized_eod
                cumulative_state[leg_key]["unrealized"] = unrealized_eod

            if emitted_rows:
                last_row = emitted_rows[-1]
                last_leg_key = (
                    last_row["account_name"],
                    last_row["leader_address"],
                    last_row["condition_id"],
                    last_row["token_id"],
                )

                realized_diff = target["realized"] - sum(
                    float(row["realized_pnl_delta"]) for row in emitted_rows
                )
                if abs(realized_diff) > _LEG_EPS:
                    last_row["realized_pnl_delta"] += realized_diff
                    last_row["realized_pnl_eod"] += realized_diff
                    cumulative_state[last_leg_key]["realized"] += realized_diff

                unrealized_diff = target["unrealized"] - sum(
                    float(row["unrealized_pnl_delta"]) for row in emitted_rows
                )
                if (
                    abs(unrealized_diff) > _LEG_EPS
                    and not (preserve_open_history and preserved_day_rows)
                    and not use_exact_current_unrealized
                ):
                    last_row["unrealized_pnl_delta"] += unrealized_diff
                    last_row["unrealized_pnl_eod"] += unrealized_diff
                    cumulative_state[last_leg_key]["unrealized"] += unrealized_diff

                last_row["total_pnl_delta"] = (
                    float(last_row["realized_pnl_delta"]) + float(last_row["unrealized_pnl_delta"])
                )
                last_row["total_pnl_eod"] = (
                    float(last_row["realized_pnl_eod"]) + float(last_row["unrealized_pnl_eod"])
                )

            output_rows.extend(emitted_rows)

    return output_rows


def main() -> int:
    global _t0
    _t0 = time.monotonic()
    _log("鍚姩 leader PnL 蹇収鏋勫缓")
    args = parse_args()
    lock_cm = _snapshot_db_lock(args.db)
    lock_acquired = lock_cm.__enter__()
    if not lock_acquired:
        _log(f"another snapshot process is already running for db={args.db}; skip")
        payload = {
            "db": args.db,
            "skipped": "snapshot_lock",
            "leader_count": 0,
            "leader_market_rows": 0,
            "daily_market_leg_rows": 0,
            "compare_summary_rows": 0,
            "compare_market_leg_rows": 0,
            "compare_open_leg_rows": 0,
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        lock_cm.__exit__(None, None, None)
        return 0

    compare_now = _compare_now_from_date_key(getattr(args, "compare_date", ""))
    db = None
    summary_rows: List[Dict[str, Any]] = []
    market_rows: List[Dict[str, Any]] = []
    leg_detail_rows: List[Dict[str, Any]] = []
    compare_stats: Dict[str, int] = {"open_leg_rows": 0, "market_leg_rows": 0, "summary_rows": 0}
    try:
        db = CopyTradeDB(args.db)
        if args.compare_only:
            compare_accounts = _resolve_compare_accounts(args.accounts)
            if compare_accounts:
                _log(f"building daily compare for accounts: {','.join(compare_accounts)}")
                compare_stats = build_daily_compare(db, account_names=compare_accounts, now=compare_now)
                _log(
                    "[compare] built "
                    f"summary={compare_stats.get('summary_rows', 0)} "
                    f"market_legs={compare_stats.get('market_leg_rows', 0)} "
                    f"open_legs={compare_stats.get('open_leg_rows', 0)}"
                )
                _log_daily_compare_report(
                    db,
                    date_key=_compare_date_key(compare_now),
                    account_names=compare_accounts,
                )
            else:
                _log("[compare] skip: no --accounts provided")
            _log("DB 鍐欏叆瀹屾垚")
        else:
            _log("repairing historical phantom rows...")
            repair_stats = repair_phantom_positions(db)
            force_rebuild_daily = bool(args.force_rebuild_daily or int(repair_stats.get("repaired", 0)) > 0)

            _log("repairing overstated entry fills...")
            entry_repair_stats = repair_overstated_entry_fill_costs(db)
            if int(entry_repair_stats.get("repaired", 0)) > 0:
                force_rebuild_daily = True

            # Reconcile redeemed positions still marked open locally.
            _log("reconciling redeemed positions...")
            n_reconciled = reconcile_redeemed_positions(db)
            if n_reconciled:
                _log(f"[reconcile] marked exited rows: {n_reconciled}")
                force_rebuild_daily = True

            _log("backfilling official settlement timestamps...")
            n_backfilled = backfill_resolution_exit_settlement_times(db)
            if n_backfilled:
                _log(f"[settlement-backfill] corrected historical rows: {n_backfilled}")
                force_rebuild_daily = True

            _log("reopening future-dated resolution exits...")
            n_reopened_future = reopen_future_resolution_exits(db)
            if n_reopened_future:
                _log(f"[reopen-future-settlement] corrected rows: {n_reopened_future}")
                force_rebuild_daily = True

            # One-time fix: merge any legacy default-account rows into main.
            cnt = db.conn.execute(
                "SELECT COUNT(*) as n FROM ct_daily_leader_pnl WHERE account_name='default'"
            ).fetchone()
            if cnt and int(cnt["n"]) > 0:
                # Add default rows into existing main rows when same day+leader already exists.
                db.conn.execute("""
                    UPDATE ct_daily_leader_pnl SET
                        realized_pnl = realized_pnl + COALESCE((
                            SELECT d.realized_pnl FROM ct_daily_leader_pnl d
                            WHERE d.date_key = ct_daily_leader_pnl.date_key
                              AND d.leader_address = ct_daily_leader_pnl.leader_address
                              AND d.account_name = 'default'
                        ), 0),
                        unrealized_pnl = unrealized_pnl + COALESCE((
                            SELECT d.unrealized_pnl FROM ct_daily_leader_pnl d
                            WHERE d.date_key = ct_daily_leader_pnl.date_key
                              AND d.leader_address = ct_daily_leader_pnl.leader_address
                              AND d.account_name = 'default'
                        ), 0),
                        total_pnl = total_pnl + COALESCE((
                            SELECT d.total_pnl FROM ct_daily_leader_pnl d
                            WHERE d.date_key = ct_daily_leader_pnl.date_key
                              AND d.leader_address = ct_daily_leader_pnl.leader_address
                              AND d.account_name = 'default'
                        ), 0),
                        market_count = market_count + COALESCE((
                            SELECT d.market_count FROM ct_daily_leader_pnl d
                            WHERE d.date_key = ct_daily_leader_pnl.date_key
                              AND d.leader_address = ct_daily_leader_pnl.leader_address
                              AND d.account_name = 'default'
                        ), 0)
                    WHERE account_name = 'main'
                      AND EXISTS (
                        SELECT 1 FROM ct_daily_leader_pnl d
                        WHERE d.date_key = ct_daily_leader_pnl.date_key
                          AND d.leader_address = ct_daily_leader_pnl.leader_address
                          AND d.account_name = 'default'
                      )
                """)
                # Rows without a matching main row can be moved directly.
                db.conn.execute("""
                    UPDATE ct_daily_leader_pnl SET account_name = 'main'
                    WHERE account_name = 'default'
                      AND NOT EXISTS (
                        SELECT 1 FROM ct_daily_leader_pnl m
                        WHERE m.date_key = ct_daily_leader_pnl.date_key
                          AND m.leader_address = ct_daily_leader_pnl.leader_address
                          AND m.account_name = 'main'
                      )
                """)
                db.conn.execute("DELETE FROM ct_daily_leader_pnl WHERE account_name='default'")
                db.conn.commit()
                _log(f"merged legacy default rows into main: {cnt['n']}")

            # ct_leader_summary / ct_leader_market_pnl: replace_leader_pnl_snapshots 浼氭竻绌洪噸鍐欙紝鐩存帴鍒?default 鍗冲彲
            for tbl in ("ct_leader_summary", "ct_leader_market_pnl"):
                db.conn.execute(f"DELETE FROM {tbl} WHERE account_name='default'")
                db.conn.commit()

            date_key = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
            _migrate_daily_leader_pnl_to_pure_attribution_once(
                db,
                date_key,
                force_rebuild=force_rebuild_daily,
            )

            summary_rows, market_rows = build_snapshots(db)
            _log("鍐欏叆蹇収鍒?DB...")
            db.replace_leader_pnl_snapshots(summary_rows=summary_rows, market_rows=market_rows)
            _log("蹇収鍐欏叆瀹屾垚")

            # --- 鍐欏叆姣忔棩杩借釜鏁版嵁 ---
            _log("鍐欏叆姣忔棩杩借釜鏁版嵁...")

            total_realized = sum(float(r.get("total_realized_pnl") or 0) for r in summary_rows)
            total_unrealized = sum(float(r.get("total_unrealized_pnl") or 0) for r in summary_rows)

            cost_row = db.conn.execute(
                "SELECT COALESCE(SUM(our_usd), 0) as total FROM ct_trades "
                "WHERE status IN ('filled','partially_filled') AND our_side='BUY' AND exit_status='open'"
            ).fetchone()
            total_cost_basis = float(cost_row["total"]) if cost_row else 0.0

            open_row = db.conn.execute(
                "SELECT COUNT(DISTINCT token_id) as cnt FROM ct_trades "
                "WHERE status IN ('filled','partially_filled') AND exit_status='open'"
            ).fetchone()
            open_count = int(open_row["cnt"]) if open_row else 0

            db.upsert_daily_equity({
                "date_key": date_key,
                "total_equity": total_realized + total_unrealized,
                "total_realized_pnl": total_realized,
                "total_unrealized_pnl": total_unrealized,
                "total_cost_basis": total_cost_basis,
                "open_position_count": open_count,
            })

            provisional_leader_daily = _build_daily_leader_deltas(db, date_key, summary_rows)
            if provisional_leader_daily:
                account_names = sorted(
                    {str(r.get("account_name") or "default") for r in provisional_leader_daily}
                )
                for account_name in account_names:
                    db.conn.execute(
                        "DELETE FROM ct_daily_leader_pnl WHERE date_key=? AND account_name=?",
                        (date_key, account_name),
                    )
                db.conn.commit()
                db.upsert_daily_leader_pnl(provisional_leader_daily)

            _log("rebuilding daily market-leg attribution detail...")
            leg_detail_rows = _rebuild_daily_leader_market_leg_pnl(db, date_key)
            db.replace_daily_leader_market_leg_pnl(leg_detail_rows)
            _log(f"daily market-leg attribution detail rebuilt: rows={len(leg_detail_rows)}")

            # Keep the dashboard summary table derived from the per-leg daily ledger
            # so the headline rows and drilldown always share the same source of truth.
            canonical_leader_daily = _build_daily_leader_rows_from_leg_rows(leg_detail_rows)
            db.replace_daily_leader_pnl(canonical_leader_daily)
            _log(
                "daily leader pnl rebuilt from market-leg attribution detail: "
                f"rows={len(canonical_leader_daily)}"
            )

            compare_accounts = _resolve_compare_accounts(args.accounts)
            if compare_accounts:
                _log(f"building daily compare for accounts: {','.join(compare_accounts)}")
                compare_stats = build_daily_compare(db, account_names=compare_accounts, now=compare_now)
                _log(
                    "[compare] built "
                    f"summary={compare_stats.get('summary_rows', 0)} "
                    f"market_legs={compare_stats.get('market_leg_rows', 0)} "
                    f"open_legs={compare_stats.get('open_leg_rows', 0)}"
                )
                _log_daily_compare_report(
                    db,
                    date_key=_compare_date_key(compare_now),
                    account_names=compare_accounts,
                )
            else:
                _log("[compare] skip: no --accounts provided")
            _log("DB 鍐欏叆瀹屾垚")
    finally:
        if db is not None:
            db.close()
        lock_cm.__exit__(*sys.exc_info())

    payload = {
        "db": args.db,
        "leader_count": len(summary_rows),
        "leader_market_rows": len(market_rows),
        "daily_market_leg_rows": len(leg_detail_rows),
        "compare_summary_rows": int(compare_stats.get("summary_rows", 0)),
        "compare_market_leg_rows": int(compare_stats.get("market_leg_rows", 0)),
        "compare_open_leg_rows": int(compare_stats.get("open_leg_rows", 0)),
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")

    if not args.no_sync_supabase:
        _log("鍚屾鍒?Supabase...")
        sync_supabase(args.db, compare_only=bool(args.compare_only))
        _log("Supabase 鍚屾瀹屾垚")

    _log("鍏ㄩ儴瀹屾垚")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



