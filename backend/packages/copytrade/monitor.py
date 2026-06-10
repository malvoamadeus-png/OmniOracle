"""交易监控 - 轮询/增量抓取 leader 新交易信号."""

import hashlib
import json
import os
from queue import Empty
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from copytrade.polymarket_public_api import DATA_API, GAMMA_API, extract_trade_fields, http_get_json

from copytrade.db import CopyTradeDB
from copytrade.domain import LeaderSignal, SIGNAL_PENDING_MAX_AGE_S, STREAM_HYBRID_BACKFILL_LOOKBACK_S


ORDERBOOK_SUBGRAPH_API = os.getenv(
    "POLYMARKET_ORDERBOOK_SUBGRAPH_API",
    "",
).strip()
SIGNAL_SOURCE_ACTIVITY = "activity"
SIGNAL_SOURCE_SUBGRAPH = "subgraph"
SIGNAL_SOURCE_HYBRID = "hybrid"
SIGNAL_SOURCE_STREAM = "stream"
SIGNAL_SOURCE_STREAM_HYBRID = "stream_hybrid"
VALID_SIGNAL_SOURCES = {
    SIGNAL_SOURCE_ACTIVITY,
    SIGNAL_SOURCE_SUBGRAPH,
    SIGNAL_SOURCE_HYBRID,
    SIGNAL_SOURCE_STREAM,
    SIGNAL_SOURCE_STREAM_HYBRID,
}
SUBGRAPH_PAGE_SIZE = 200
SUBGRAPH_MAX_PAGES = 20
SUBGRAPH_AMOUNT_SCALE = 1_000_000.0
TOKEN_META_NEGATIVE_TTL_S = 5.0
TOKEN_META_POSITIVE_TTL_S = 6 * 60 * 60.0
PENDING_RETRY_RETRY_DELAY_S = 5
STREAM_PENDING_RECOVERY_MIN_AGE_S = 90
STREAM_PENDING_RECOVERY_MAX_AGE_S = SIGNAL_PENDING_MAX_AGE_S
MONITOR_ERROR_LOG_THROTTLE_S = 300.0
_MONITOR_ERROR_LOG_TS: Dict[Tuple[str, str], float] = {}


def _monitor_log(scope: str, key: str, message: str, *, throttle_s: float = MONITOR_ERROR_LOG_THROTTLE_S) -> None:
    cache_key = (str(scope or ""), str(key or ""))
    now = time.time()
    last_ts = float(_MONITOR_ERROR_LOG_TS.get(cache_key, 0.0) or 0.0)
    if last_ts and (now - last_ts) < throttle_s:
        return
    _MONITOR_ERROR_LOG_TS[cache_key] = now
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


@dataclass
class LeaderTrade:
    leader_address: str
    tx_hash: str
    fill_key: str
    timestamp: str
    side: str
    token_id: str
    condition_id: str
    price: Optional[float]
    size: Optional[float]
    usd_amount: Optional[float]
    outcome: Optional[str]
    market_slug: Optional[str]
    ts_int: Optional[int] = None
    is_maker_like_aggregated: bool = False
    maker_like_score: Optional[float] = None
    trade_id: Optional[int] = None
    execution_price_hint: Optional[float] = None
    aggregation_source_count: Optional[int] = None
    aggregation_kind: Optional[str] = None
    signal_attempt_id: Optional[int] = None
    source: str = SIGNAL_SOURCE_ACTIVITY

    def to_leader_signal(self, account_name: str) -> LeaderSignal:
        return LeaderSignal(
            account_name=str(account_name or "default"),
            leader_address=self.leader_address,
            side=self.side,
            token_id=self.token_id,
            condition_id=self.condition_id,
            leader_fill_key=self.fill_key,
            tx_hash=self.tx_hash,
            price=self.price,
            size=self.size,
            usd=self.usd_amount,
            outcome=self.outcome,
            market_slug=self.market_slug,
            timestamp=self.timestamp,
            source=str(self.source or SIGNAL_SOURCE_ACTIVITY).strip().lower() or SIGNAL_SOURCE_ACTIVITY,
            raw={
                "trade_id": self.trade_id,
                "signal_attempt_id": self.signal_attempt_id,
                "is_maker_like_aggregated": self.is_maker_like_aggregated,
                "maker_like_score": self.maker_like_score,
                "aggregation_source_count": self.aggregation_source_count,
                "aggregation_kind": self.aggregation_kind,
                "execution_price_hint": self.execution_price_hint,
            },
        )


def _norm_num(v: Any) -> str:
    if v is None:
        return ""
    try:
        n = float(v)
    except Exception:
        return str(v).strip()
    s = f"{n:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _scale_subgraph_amount(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        raw = int(str(value))
    except Exception:
        return None
    return raw / SUBGRAPH_AMOUNT_SCALE


def build_leader_fill_key(leader_address: str, parsed_trade: Dict[str, Any]) -> str:
    """Build an activity-level stable key so same-tx multi-fills are not collapsed."""
    parts = [
        str(leader_address or "").strip().lower(),
        str(parsed_trade.get("exchange_address") or "").strip().lower(),
        str(parsed_trade.get("order_hash") or "").strip().lower(),
        str(parsed_trade.get("log_index") if parsed_trade.get("log_index") is not None else ""),
        str(parsed_trade.get("tx") or "").strip().lower(),
        str(parsed_trade.get("token_id") or "").strip(),
        str(parsed_trade.get("market") or "").strip().lower(),
        str(parsed_trade.get("side") or "").strip().upper(),
        str(parsed_trade.get("outcome_index") if parsed_trade.get("outcome_index") is not None else ""),
        _norm_num(parsed_trade.get("price")),
        _norm_num(parsed_trade.get("size")),
        _norm_num(parsed_trade.get("usd")),
        str(parsed_trade.get("ts") or "").strip(),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_claim_trade_payload(
    leader_address: str,
    parsed_trade: Dict[str, Any],
    *,
    status: str = "detected",
) -> Dict[str, Any]:
    return {
        "leader_address": str(leader_address or "").strip().lower(),
        "leader_tx_hash": str(parsed_trade.get("tx") or ""),
        "leader_fill_key": str(parsed_trade.get("fill_key") or "").strip().lower()
        or build_leader_fill_key(leader_address, parsed_trade),
        "leader_side": str(parsed_trade.get("side") or ""),
        "leader_price": parsed_trade.get("price"),
        "leader_size": parsed_trade.get("size"),
        "leader_usd": parsed_trade.get("usd"),
        "token_id": str(parsed_trade.get("token_id") or ""),
        "condition_id": str(parsed_trade.get("market") or ""),
        "market_slug": parsed_trade.get("slug"),
        "outcome": (
            str(parsed_trade.get("outcome_index"))
            if parsed_trade.get("outcome_index") is not None
            else parsed_trade.get("outcome")
        ),
        "source": str(parsed_trade.get("source") or SIGNAL_SOURCE_ACTIVITY).strip().lower()
        or SIGNAL_SOURCE_ACTIVITY,
        "status": status,
    }


def build_leader_trade(
    leader_address: str,
    parsed_trade: Dict[str, Any],
    *,
    trade_id: Optional[int] = None,
    signal_attempt_id: Optional[int] = None,
) -> Optional[LeaderTrade]:
    fill_key = str(parsed_trade.get("fill_key") or "").strip().lower()
    if not fill_key:
        fill_key = build_leader_fill_key(leader_address, parsed_trade)
    if not fill_key:
        return None

    ts_str = str(parsed_trade.get("ts") or "")
    return LeaderTrade(
        leader_address=str(leader_address or "").strip().lower(),
        tx_hash=str(parsed_trade.get("tx") or ""),
        fill_key=fill_key,
        timestamp=ts_str,
        side=str(parsed_trade.get("side") or ""),
        token_id=str(parsed_trade.get("token_id") or ""),
        condition_id=str(parsed_trade.get("market") or ""),
        price=parsed_trade.get("price"),
        size=parsed_trade.get("size"),
        usd_amount=parsed_trade.get("usd"),
        outcome=(
            str(parsed_trade.get("outcome_index"))
            if parsed_trade.get("outcome_index") is not None
            else (str(parsed_trade.get("outcome")) if parsed_trade.get("outcome") is not None else None)
        ),
        market_slug=parsed_trade.get("slug"),
        ts_int=TradeMonitor._parse_ts_int(ts_str),
        trade_id=trade_id,
        signal_attempt_id=signal_attempt_id,
        source=str(parsed_trade.get("source") or SIGNAL_SOURCE_ACTIVITY).strip().lower() or SIGNAL_SOURCE_ACTIVITY,
    )


class TradeMonitor:
    def __init__(
        self,
        session: requests.Session,
        db: CopyTradeDB,
        leader_addresses: List[str],
        *,
        account_name: str = "default",
        signal_source: str = SIGNAL_SOURCE_ACTIVITY,
        fetch_workers: int = 4,
        signal_queue=None,
        signal_reconcile_interval_s: int = 60,
    ):
        self.session = session
        self.db = db
        self.leader_addresses = [a.lower() for a in leader_addresses]
        self.account_name = account_name
        self.signal_source = self._normalize_signal_source(signal_source)
        self.fetch_workers = max(1, int(fetch_workers or 1))
        self._seen_fill_keys: Set[str] = set()
        self._token_market_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._token_market_cache_lock = threading.Lock()
        self._signal_queue = signal_queue
        self._buffered_signal_trades: List[LeaderTrade] = []
        self.signal_reconcile_interval_s = max(1, int(signal_reconcile_interval_s or 60))
        self._next_reconcile_ts = 0.0

    @staticmethod
    def _normalize_signal_source(signal_source: str) -> str:
        value = str(signal_source or SIGNAL_SOURCE_ACTIVITY).strip().lower()
        return value if value in VALID_SIGNAL_SOURCES else SIGNAL_SOURCE_ACTIVITY

    def is_stream_mode(self) -> bool:
        return self.signal_source in {SIGNAL_SOURCE_STREAM, SIGNAL_SOURCE_STREAM_HYBRID}

    def _active_sources(self) -> Tuple[str, ...]:
        if self.signal_source == SIGNAL_SOURCE_SUBGRAPH:
            return (SIGNAL_SOURCE_SUBGRAPH,)
        if self.signal_source == SIGNAL_SOURCE_HYBRID:
            if ORDERBOOK_SUBGRAPH_API:
                return (SIGNAL_SOURCE_SUBGRAPH, SIGNAL_SOURCE_ACTIVITY)
            return (SIGNAL_SOURCE_ACTIVITY,)
        if self.signal_source == SIGNAL_SOURCE_STREAM_HYBRID:
            return (SIGNAL_SOURCE_ACTIVITY,)
        if self.signal_source == SIGNAL_SOURCE_STREAM:
            return ()
        return (SIGNAL_SOURCE_ACTIVITY,)

    def wait_for_signal(self, timeout: float = 1.0) -> bool:
        if not self.is_stream_mode() or self._signal_queue is None:
            return False
        if self._buffered_signal_trades:
            return True
        try:
            item = self._signal_queue.get(timeout=max(0.0, float(timeout)))
        except Empty:
            return False
        if isinstance(item, LeaderTrade):
            self._buffered_signal_trades.append(item)
            return True
        return False

    def _drain_signal_queue(self) -> List[LeaderTrade]:
        out: List[LeaderTrade] = []
        if self._buffered_signal_trades:
            out.extend(self._buffered_signal_trades)
            self._buffered_signal_trades = []
        if self._signal_queue is None:
            return out
        while True:
            try:
                item = self._signal_queue.get_nowait()
            except Empty:
                break
            if isinstance(item, LeaderTrade):
                out.append(item)
        return out

    def _should_run_reconcile(self) -> bool:
        if self.signal_source == SIGNAL_SOURCE_STREAM:
            return False
        if self.signal_source != SIGNAL_SOURCE_STREAM_HYBRID:
            return True
        now = time.time()
        if self._next_reconcile_ts == 0.0 or now >= self._next_reconcile_ts:
            self._next_reconcile_ts = now + float(self.signal_reconcile_interval_s)
            return True
        return False

    def _pending_recovery_max_age_s(self) -> Optional[int]:
        if not self.is_stream_mode():
            return None
        base = max(1, int(self.signal_reconcile_interval_s or 60))
        return max(
            STREAM_PENDING_RECOVERY_MIN_AGE_S,
            min(STREAM_PENDING_RECOVERY_MAX_AGE_S, base * 3),
        )

    def poll_once(self) -> List[LeaderTrade]:
        """抓取最近窗口内 leader 新成交，并和 pending detected 行合并处理."""
        all_new: List[LeaderTrade] = []
        cutoff_ts = int(datetime.now(timezone.utc).timestamp()) - STREAM_HYBRID_BACKFILL_LOOKBACK_S
        delivered_fill_keys: Set[str] = set()

        for lt in self._drain_signal_queue():
            if not lt.fill_key or lt.fill_key in delivered_fill_keys:
                continue
            if self.db.is_fill_seen(lt.fill_key, account_name=self.account_name):
                continue
            delivered_fill_keys.add(lt.fill_key)
            self.db.mark_fill_seen(lt.fill_key, account_name=self.account_name)
            all_new.append(lt)

        pending_rows = self.db.get_pending_leader_trades(
            account_name=self.account_name,
            stale_after_s=0 if self.is_stream_mode() else 30,
            pending_retry_stale_after_s=(
                PENDING_RETRY_RETRY_DELAY_S if self.is_stream_mode() else 30
            ),
            max_age_s=self._pending_recovery_max_age_s(),
        )
        for row in pending_rows:
            status = str(row.get("status") or "").strip().lower()
            if status == "pending_retry":
                lt = self._recover_pending_trade_row(row)
                if lt is None:
                    if row.get("id") is not None:
                        self._touch_pending_row(row, "pending_retry")
                    continue
            else:
                lt = self._leader_trade_from_row(row)
            if lt is None:
                continue
            if lt.fill_key in delivered_fill_keys:
                continue
            delivered_fill_keys.add(lt.fill_key)
            self.db.mark_fill_seen(lt.fill_key, account_name=self.account_name)
            all_new.append(lt)

        if not self._should_run_reconcile():
            return all_new

        fetch_plan: Dict[str, Dict[str, Any]] = {}
        for addr in self.leader_addresses:
            last_ts = self.db.get_last_seen_ts(addr, account_name=self.account_name)
            effective_start = max(last_ts, cutoff_ts)
            fetch_plan[addr] = {
                "last_ts": last_ts,
                "effective_start": effective_start,
                "sources": self._active_sources(),
            }

        trades_by_addr = (
            self._fetch_sources_parallel(fetch_plan, cutoff_ts)
            if self._can_parallel_fetch()
            else self._fetch_sources_sequential(fetch_plan, cutoff_ts)
        )

        for addr in self.leader_addresses:
            plan = fetch_plan.get(addr, {})
            last_ts = int(plan.get("last_ts") or 0)
            source_rows = trades_by_addr.get(addr, {})
            trades = self._merge_source_trades(addr, source_rows)
            if not trades:
                continue

            max_ts = last_ts
            for t in trades:
                tx_hash = str(t.get("tx") or "")
                fill_key = str(t.get("fill_key") or "").strip().lower()
                if not fill_key:
                    fill_key = build_leader_fill_key(addr, t)
                if not fill_key:
                    continue
                if fill_key in delivered_fill_keys:
                    continue

                ts_str = str(t.get("ts") or "")
                ts_int = self._parse_ts_int(ts_str)
                if ts_int and ts_int > max_ts:
                    max_ts = ts_int

                t["fill_key"] = fill_key
                attempt_id = self.db.claim_leader_fill(
                    build_claim_trade_payload(addr, t),
                    account_name=self.account_name,
                )
                if attempt_id is None:
                    continue

                delivered_fill_keys.add(fill_key)
                if tx_hash:
                    self.db.mark_tx_seen(tx_hash)

                leader_trade = build_leader_trade(addr, t, signal_attempt_id=attempt_id)
                if leader_trade is None:
                    continue
                leader_trade.ts_int = ts_int
                self.db.mark_fill_seen(fill_key, account_name=self.account_name)
                all_new.append(leader_trade)

            if max_ts > last_ts:
                self.db.update_last_seen_ts(addr, max_ts, account_name=self.account_name)

        return all_new

    def _touch_pending_row(self, row: Dict[str, Any], status: str) -> None:
        row_id = row.get("id")
        if row_id is None:
            return
        if str(row.get("source_table") or "") == "ct_trades":
            self.db.update_trade_status(int(row_id), status)
        else:
            self.db.update_signal_attempt_status(int(row_id), status)

    def _can_parallel_fetch(self) -> bool:
        if self.fetch_workers <= 1:
            return False
        if type(self)._fetch_activity is not TradeMonitor._fetch_activity:
            return False
        if type(self)._fetch_subgraph_trades is not TradeMonitor._fetch_subgraph_trades:
            return False
        return True

    def _fetch_sources_sequential(
        self,
        fetch_plan: Dict[str, Dict[str, Any]],
        cutoff_ts: int,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for addr, plan in fetch_plan.items():
            source_rows: Dict[str, List[Dict[str, Any]]] = {}
            effective_start = int(plan.get("effective_start") or 0)
            for source in plan.get("sources", ()):
                source_rows[source] = self._fetch_source(source, addr, effective_start, cutoff_ts)
            out[addr] = source_rows
        return out

    def _fetch_sources_parallel(
        self,
        fetch_plan: Dict[str, Dict[str, Any]],
        cutoff_ts: int,
    ) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
        out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            addr: {} for addr in fetch_plan
        }
        jobs: List[Tuple[str, str, int]] = []
        for addr, plan in fetch_plan.items():
            effective_start = int(plan.get("effective_start") or 0)
            for source in plan.get("sources", ()):
                jobs.append((addr, source, effective_start))

        if not jobs:
            return out

        max_workers = min(self.fetch_workers, len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(self._fetch_source_threadsafe, source, addr, start_ts, cutoff_ts): (addr, source)
                for addr, source, start_ts in jobs
            }
            for future in as_completed(future_map):
                addr, source = future_map[future]
                try:
                    rows = future.result()
                except Exception as e:
                    _monitor_log(
                        "fetch_source",
                        f"{source}:{addr}",
                        f"[monitor] fetch {source} failed for {addr[:10]}...: {e}",
                    )
                    rows = []
                out.setdefault(addr, {})[source] = rows
        return out

    def _fetch_source(
        self,
        source: str,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        if source == SIGNAL_SOURCE_SUBGRAPH:
            return self._fetch_subgraph_trades(address, start_ts, cutoff_ts)
        return self._fetch_activity(address, start_ts, cutoff_ts)

    def _fetch_source_threadsafe(
        self,
        source: str,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        if source == SIGNAL_SOURCE_SUBGRAPH:
            return self._fetch_subgraph_trades_threadsafe(address, start_ts, cutoff_ts)
        return self._fetch_activity_threadsafe(address, start_ts, cutoff_ts)

    def _merge_source_trades(
        self,
        leader_address: str,
        source_rows: Dict[str, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        merged: Dict[str, Dict[str, Any]] = {}
        for source in (SIGNAL_SOURCE_SUBGRAPH, SIGNAL_SOURCE_ACTIVITY):
            for trade in source_rows.get(source, []):
                row = dict(trade)
                row["source"] = str(row.get("source") or source).strip().lower() or source
                fill_key = str(row.get("fill_key") or "").strip().lower()
                if not fill_key:
                    fill_key = build_leader_fill_key(leader_address, row)
                    row["fill_key"] = fill_key
                if not fill_key:
                    continue
                prev = merged.get(fill_key)
                if prev is None:
                    merged[fill_key] = row
                    continue
                merged[fill_key] = self._merge_trade_rows(
                    prev,
                    row,
                    prefer_new=(source == SIGNAL_SOURCE_SUBGRAPH),
                )
        return sorted(
            merged.values(),
            key=lambda item: (self._parse_ts_int(item.get("ts")) or 0, str(item.get("tx") or "")),
        )

    @staticmethod
    def _merge_trade_rows(
        base: Dict[str, Any],
        new: Dict[str, Any],
        *,
        prefer_new: bool,
    ) -> Dict[str, Any]:
        merged = dict(base)
        for key, value in new.items():
            if key not in merged or merged[key] in (None, "", [], {}):
                merged[key] = value
                continue
            if prefer_new and value not in (None, "", [], {}):
                merged[key] = value
        return merged

    @staticmethod
    def _leader_trade_from_row(row: Dict[str, Any]) -> Optional[LeaderTrade]:
        fill_key = str(row.get("leader_fill_key") or "").strip().lower()
        if not fill_key:
            return None
        token_id = str(row.get("token_id") or "")
        condition_id = str(row.get("condition_id") or "")
        side = str(row.get("leader_side") or "")
        status = str(row.get("status") or "").strip().lower()
        if not token_id or side not in {"BUY", "SELL"}:
            return None
        if not condition_id and status != "pending_retry":
            return None
        timestamp = str(row.get("created_at") or row.get("updated_at") or "")
        source_table = str(row.get("source_table") or "")
        return LeaderTrade(
            leader_address=str(row.get("leader_address") or ""),
            tx_hash=str(row.get("leader_tx_hash") or ""),
            fill_key=fill_key,
            timestamp=timestamp,
            side=side,
            token_id=token_id,
            condition_id=condition_id,
            price=row.get("leader_price"),
            size=row.get("leader_size"),
            usd_amount=row.get("leader_usd"),
            outcome=str(row.get("outcome")) if row.get("outcome") is not None else None,
            market_slug=row.get("market_slug"),
            ts_int=TradeMonitor._parse_ts_int(timestamp),
            trade_id=(
                int(row["id"])
                if row.get("id") is not None and source_table == "ct_trades"
                else None
            ),
            signal_attempt_id=(
                int(row["id"])
                if row.get("id") is not None and source_table != "ct_trades"
                else None
            ),
            source=str(row.get("source") or source_table or SIGNAL_SOURCE_ACTIVITY).strip().lower()
            or SIGNAL_SOURCE_ACTIVITY,
        )

    def _recover_pending_trade_row(self, row: Dict[str, Any]) -> Optional[LeaderTrade]:
        lt = self._leader_trade_from_row(row)
        if lt is None:
            return None

        meta = self._get_token_market_meta(lt.token_id)
        if not isinstance(meta, dict) or not meta.get("condition_id"):
            return None

        market_slug = (
            row.get("market_slug")
            or meta.get("market_slug")
            or self.db.find_latest_market_slug(
                condition_id=str(meta.get("condition_id") or ""),
                token_id=lt.token_id,
            )
        )
        parsed = {
            "tx": lt.tx_hash,
            "ts": lt.timestamp,
            "side": lt.side,
            "usd": lt.usd_amount,
            "price": lt.price,
            "size": lt.size,
            "market": meta.get("condition_id"),
            "slug": market_slug,
            "token_id": lt.token_id,
            "outcome_index": meta.get("outcome_index"),
            "outcome": meta.get("outcome") or lt.outcome,
            "fill_key": lt.fill_key,
            "source": lt.source,
        }
        attempt_id = self.db.claim_leader_fill(
            build_claim_trade_payload(lt.leader_address, parsed, status="detected"),
            account_name=self.account_name,
        )
        recovered = build_leader_trade(
            lt.leader_address,
            parsed,
            trade_id=lt.trade_id,
            signal_attempt_id=attempt_id or lt.signal_attempt_id,
        )
        if recovered is None:
            return None
        if recovered.ts_int is None:
            recovered.ts_int = self._parse_ts_int(lt.timestamp)
        return recovered

    def _fetch_activity(self, address: str, start_ts: int, cutoff_ts: int) -> List[Dict[str, Any]]:
        return self._fetch_activity_impl(self.session, address, start_ts, cutoff_ts)

    def _fetch_activity_threadsafe(
        self,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        session = requests.Session()
        try:
            return self._fetch_activity_impl(session, address, start_ts, cutoff_ts)
        finally:
            session.close()

    def _fetch_activity_impl(
        self,
        session: requests.Session,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        all_trades: List[Dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            params: Dict[str, Any] = {
                "user": address,
                "type": "TRADE",
                "limit": limit,
                "offset": offset,
            }
            if start_ts > 0:
                params["startTs"] = start_ts + 1

            try:
                data = http_get_json(session, f"{DATA_API}/activity", params=params)
            except Exception as e:
                _monitor_log(
                    "activity",
                    address,
                    f"[monitor] 获取 {address[:10]}... activity 失败: {e}",
                )
                break

            if not isinstance(data, list) or not data:
                break

            hit_old = False
            for row in data:
                if not isinstance(row, dict):
                    continue
                parsed = extract_trade_fields(row)
                if parsed is None:
                    continue
                token_id = parsed.get("token_id")
                if not token_id:
                    tx_hash = parsed.get("tx", "unknown")
                    _monitor_log(
                        "activity_missing_token",
                        str(tx_hash)[:16],
                        f"[monitor] 跳过无 token_id 的 activity: tx={str(tx_hash)[:16]}...",
                    )
                    continue

                row_ts = self._parse_ts_int(parsed.get("ts"))
                if row_ts is not None and row_ts < cutoff_ts:
                    hit_old = True
                    continue

                parsed["source"] = SIGNAL_SOURCE_ACTIVITY
                parsed["fill_key"] = build_leader_fill_key(address, parsed)
                all_trades.append(parsed)

            if hit_old or len(data) < limit:
                break
            offset += limit

        return all_trades

    def _fetch_subgraph_trades(
        self,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        return self._fetch_subgraph_trades_impl(self.session, address, start_ts, cutoff_ts)

    def _fetch_subgraph_trades_threadsafe(
        self,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        session = requests.Session()
        try:
            return self._fetch_subgraph_trades_impl(session, address, start_ts, cutoff_ts)
        finally:
            session.close()

    def _fetch_subgraph_trades_impl(
        self,
        session: requests.Session,
        address: str,
        start_ts: int,
        cutoff_ts: int,
    ) -> List[Dict[str, Any]]:
        try:
            maker_rows = self._fetch_subgraph_role_rows(session, address, "maker", start_ts)
            taker_rows = self._fetch_subgraph_role_rows(session, address, "taker", start_ts)
        except Exception as e:
            _monitor_log(
                "subgraph_fetch",
                address,
                f"[monitor] 获取 {address[:10]}... subgraph 失败: {e}",
            )
            return []

        out: List[Dict[str, Any]] = []
        for row in maker_rows + taker_rows:
            parsed = self._parse_subgraph_fill(session, address, row)
            if parsed is None:
                continue
            row_ts = self._parse_ts_int(parsed.get("ts"))
            if row_ts is not None and row_ts < cutoff_ts:
                continue
            parsed["fill_key"] = build_leader_fill_key(address, parsed)
            out.append(parsed)

        return sorted(
            out,
            key=lambda item: (self._parse_ts_int(item.get("ts")) or 0, str(item.get("tx") or "")),
        )

    def _fetch_subgraph_role_rows(
        self,
        session: requests.Session,
        address: str,
        role: str,
        start_ts: int,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        skip = 0
        for _ in range(SUBGRAPH_MAX_PAGES):
            page = self._query_subgraph_fills(
                session,
                address,
                role,
                start_ts,
                skip=skip,
                limit=SUBGRAPH_PAGE_SIZE,
            )
            if not page:
                break
            rows.extend(page)
            if len(page) < SUBGRAPH_PAGE_SIZE:
                break
            skip += SUBGRAPH_PAGE_SIZE
        return rows

    def _query_subgraph_fills(
        self,
        session: requests.Session,
        address: str,
        role: str,
        start_ts: int,
        *,
        skip: int = 0,
        limit: int = SUBGRAPH_PAGE_SIZE,
    ) -> List[Dict[str, Any]]:
        if role not in {"maker", "taker"}:
            return []
        if not ORDERBOOK_SUBGRAPH_API:
            _monitor_log(
                "subgraph_disabled",
                "missing_url",
                "[monitor] subgraph signal source disabled: set POLYMARKET_ORDERBOOK_SUBGRAPH_API to a v2 subgraph URL to enable it",
            )
            return []

        query = f"""
        query($address: String!, $start: BigInt!, $first: Int!, $skip: Int!) {{
          events: orderFilledEvents(
            first: $first,
            skip: $skip,
            orderBy: timestamp,
            orderDirection: asc,
            where: {{ {role}: $address, timestamp_gt: $start }}
          ) {{
            id
            timestamp
            maker
            taker
            makerAssetId
            takerAssetId
            makerAmountFilled
            takerAmountFilled
            transactionHash
          }}
        }}
        """
        payload = self._post_graphql_json(
            session,
            ORDERBOOK_SUBGRAPH_API,
            query,
            {
                "address": address.lower(),
                "start": str(max(0, int(start_ts or 0))),
                "first": int(limit),
                "skip": int(skip),
            },
        )
        rows = payload.get("events")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _parse_subgraph_fill(
        self,
        session: requests.Session,
        leader_address: str,
        row: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        leader = str(leader_address or "").strip().lower()
        maker = str(row.get("maker") or "").strip().lower()
        taker = str(row.get("taker") or "").strip().lower()
        if maker == leader:
            leader_role = "maker"
        elif taker == leader:
            leader_role = "taker"
        else:
            return None

        maker_asset_id = str(row.get("makerAssetId") or "").strip()
        taker_asset_id = str(row.get("takerAssetId") or "").strip()
        maker_amount = _scale_subgraph_amount(row.get("makerAmountFilled"))
        taker_amount = _scale_subgraph_amount(row.get("takerAmountFilled"))
        if maker_amount is None or taker_amount is None:
            return None

        token_id: Optional[str] = None
        token_amount: Optional[float] = None
        usd_amount: Optional[float] = None
        maker_side: Optional[str] = None
        token_meta: Optional[Dict[str, Any]] = None

        if maker_asset_id and maker_asset_id != "0":
            token_meta = self._get_token_market_meta(maker_asset_id, session=session)
            if token_meta is not None:
                token_id = maker_asset_id
                token_amount = maker_amount
                usd_amount = taker_amount
                maker_side = "SELL"

        if token_meta is None and taker_asset_id and taker_asset_id != "0":
            token_meta = self._get_token_market_meta(taker_asset_id, session=session)
            if token_meta is not None:
                token_id = taker_asset_id
                token_amount = taker_amount
                usd_amount = maker_amount
                maker_side = "BUY"

        if token_meta is None or not token_id or token_amount is None or usd_amount is None or not maker_side:
            return None

        side = maker_side if leader_role == "maker" else ("SELL" if maker_side == "BUY" else "BUY")
        price = None
        if token_amount > 0 and usd_amount >= 0:
            price = usd_amount / token_amount

        return {
            "tx": str(row.get("transactionHash") or "").strip().lower() or None,
            "ts": str(row.get("timestamp") or ""),
            "side": side,
            "usd": usd_amount,
            "price": price,
            "size": token_amount,
            "market": token_meta.get("condition_id"),
            "slug": token_meta.get("market_slug"),
            "token_id": token_id,
            "outcome_index": token_meta.get("outcome_index"),
            "outcome": token_meta.get("outcome"),
            "raw": row,
            "source": SIGNAL_SOURCE_SUBGRAPH,
        }

    def _get_token_market_meta(
        self,
        token_id: str,
        *,
        session: Optional[requests.Session] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized = str(token_id or "").strip()
        if not normalized:
            return None

        now = time.monotonic()
        with self._token_market_cache_lock:
            cached = self._token_market_cache.get(normalized)
            if cached is not None and now < cached[0]:
                value = cached[1]
                return dict(value) if isinstance(value, dict) else None

        sess = session or self.session
        try:
            data = http_get_json(
                sess,
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": normalized, "limit": 1},
                timeout_s=15.0,
                max_retries=2,
            )
        except Exception as e:
            _monitor_log(
                "token_market",
                normalized[:16],
                f"[monitor] 解析 token {normalized[:12]}... 的 market 失败: {e}",
            )
            data = None

        meta_row = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else data
            if isinstance(data, dict)
            else None
        )
        meta: Optional[Dict[str, Any]] = None
        if isinstance(meta_row, dict):
            token_ids = [str(x) for x in _parse_json_list(meta_row.get("clobTokenIds"))]
            outcomes = _parse_json_list(meta_row.get("outcomes"))
            outcome_index = token_ids.index(normalized) if normalized in token_ids else None
            outcome = None
            if outcome_index is not None and outcome_index < len(outcomes):
                val = outcomes[outcome_index]
                if isinstance(val, str):
                    outcome = val
            meta = {
                "condition_id": str(meta_row.get("conditionId") or "").strip().lower() or None,
                "market_slug": str(meta_row.get("slug") or "").strip() or None,
                "outcome_index": outcome_index,
                "outcome": outcome,
            }
            if not meta["condition_id"]:
                meta = None

        with self._token_market_cache_lock:
            self._token_market_cache[normalized] = (
                time.monotonic()
                + (TOKEN_META_POSITIVE_TTL_S if isinstance(meta, dict) else TOKEN_META_NEGATIVE_TTL_S),
                dict(meta) if isinstance(meta, dict) else None,
            )
        return dict(meta) if isinstance(meta, dict) else None

    @staticmethod
    def _post_graphql_json(
        session: requests.Session,
        url: str,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        last_err: Optional[BaseException] = None
        body = {
            "query": query,
            "variables": variables or {},
        }
        for attempt in range(max_retries):
            try:
                resp = session.post(
                    url,
                    json=body,
                    timeout=timeout_s,
                    headers={"accept": "application/json"},
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.0 * (2**attempt))
                    continue
                resp.raise_for_status()
                payload = resp.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("graphql response is not a dict")
                errors = payload.get("errors")
                if isinstance(errors, list) and errors:
                    raise RuntimeError(str(errors[0]))
                data = payload.get("data")
                if isinstance(data, dict):
                    return data
                raise RuntimeError("graphql response missing data")
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (2**attempt))
        raise RuntimeError(f"POST {url} failed after retries: {last_err}")

    @staticmethod
    def _parse_ts_int(ts: Any) -> Optional[int]:
        if isinstance(ts, (int, float)):
            return int(ts)
        if isinstance(ts, str):
            s = ts.strip()
            if s.isdigit():
                n = int(s)
                if n > 10_000_000_000:
                    n = n // 1000
                return n
            try:
                iso = s.replace("Z", "+00:00")
                return int(datetime.fromisoformat(iso).timestamp())
            except Exception:
                pass
        return None
