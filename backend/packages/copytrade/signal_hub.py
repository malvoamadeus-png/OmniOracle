"""Leader signal hub backed by Polygon WSS OrderFilled subscriptions."""

import json
import os
import queue
import sys
import threading
import time
from urllib.parse import urlparse
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
from websocket import WebSocketTimeoutException, create_connection
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from copytrade.polymarket_public_api import GAMMA_API, http_get_json

from copytrade.db import CopyTradeDB
from copytrade.monitor import LeaderTrade, build_claim_trade_payload, build_leader_fill_key, build_leader_trade
from copytrade.ws_proxy import resolve_websocket_proxy_options


CTF_EXCHANGE_ADDRESS = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE_ADDRESS = "0xe2222d279d744050d28e00520010520000310F59"
CTF_EXCHANGE_ADDRESSES = {
    _addr.lower()
    for _addr in (
        CTF_EXCHANGE_ADDRESS,
        NEG_RISK_CTF_EXCHANGE_ADDRESS,
    )
}
DEFAULT_POLYGON_HTTP_RPC = "https://polygon-bor-rpc.publicnode.com"
DEFAULT_QUEUE_SIZE = 1024
TOKEN_META_NEGATIVE_TTL_S = 30.0
TOKEN_META_POSITIVE_TTL_S = 6 * 60 * 60.0
BLOCK_TS_CACHE_LIMIT = 2048
HTTP_RPC_TIMEOUT_S = 20
HTTP_RPC_ACCEPT_ENCODING = "gzip, deflate"

ORDER_FILLED_EVENT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "internalType": "bytes32", "name": "orderHash", "type": "bytes32"},
        {"indexed": True, "internalType": "address", "name": "maker", "type": "address"},
        {"indexed": True, "internalType": "address", "name": "taker", "type": "address"},
        {"indexed": False, "internalType": "uint8", "name": "side", "type": "uint8"},
        {"indexed": False, "internalType": "uint256", "name": "tokenId", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "makerAmountFilled", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "takerAmountFilled", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "fee", "type": "uint256"},
        {"indexed": False, "internalType": "bytes32", "name": "builder", "type": "bytes32"},
        {"indexed": False, "internalType": "bytes32", "name": "metadata", "type": "bytes32"},
    ],
    "name": "OrderFilled",
    "type": "event",
}


def _log(msg: str) -> None:
    sys.stderr.write(f"[signal_hub] {msg.rstrip()}\n")
    sys.stderr.flush()


def _normalize_address(value: str) -> str:
    return str(value or "").strip().lower()


def _topic_address(address: str) -> str:
    normalized = _normalize_address(address)
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    return "0x" + ("0" * 24) + normalized.rjust(40, "0")


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.startswith("0x"):
            return int(text, 16)
        return int(text)
    except Exception:
        return None


def _asset_id_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _hex_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    hex_fn = getattr(value, "hex", None)
    if callable(hex_fn):
        try:
            text = str(hex_fn()).strip().lower()
            return text if text.startswith("0x") else "0x" + text
        except Exception:
            pass
    return str(value or "").strip().lower()


def _derive_http_rpc_url(wss_url: str) -> str:
    explicit = (
        os.getenv("POLYGON_HTTP_RPC_URL")
        or os.getenv("POLYGON_RPC_URL")
        or os.getenv("POLYGON_HTTP_URL")
        or ""
    ).strip()
    if explicit:
        return explicit
    url = str(wss_url or "").strip()
    if url.startswith("wss://"):
        return "https://" + url[len("wss://") :]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://") :]
    return DEFAULT_POLYGON_HTTP_RPC


def _http_rpc_request_kwargs() -> Dict[str, Any]:
    # Avoid zstd decoding on block timestamp lookups; this path only needs plain JSON RPC.
    return {
        "timeout": HTTP_RPC_TIMEOUT_S,
        "headers": {
            "Content-Type": "application/json",
            "Accept-Encoding": HTTP_RPC_ACCEPT_ENCODING,
        },
    }


class LeaderSignalHub:
    def __init__(
        self,
        wss_url: str,
        db: CopyTradeDB,
        *,
        http_rpc_url: Optional[str] = None,
        session: Optional[requests.Session] = None,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ):
        self.wss_url = str(wss_url or "").strip()
        if not self.wss_url:
            raise ValueError("POLYGON_WSS_URL is required for stream signal modes")

        self.db = db
        self.http_rpc_url = str(http_rpc_url or _derive_http_rpc_url(self.wss_url)).strip()
        self._session = session or requests.Session()
        self._default_queue_size = max(1, int(queue_size or DEFAULT_QUEUE_SIZE))

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="leader-signal-hub", daemon=True)
        self._started = False

        self._account_queues: Dict[str, "queue.Queue[LeaderTrade]"] = {}
        self._leader_accounts: Dict[str, Set[str]] = {}
        self._token_meta_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
        self._block_ts_cache: Dict[int, int] = {}
        self._started_at = time.time()
        self._connected = False
        self._ready = False
        self._connected_at = 0.0
        self._last_message_ts = 0.0
        self._last_event_ts = 0.0
        self._received_log_count = 0
        self._detected_fill_count = 0
        self._pending_retry_count = 0
        self._queue_full_count = 0
        self._subscription_expected = 0
        self._subscription_acks = 0
        self._pending_subscription_ids: Set[int] = set()
        self._connect_count = 0
        self._reconnect_count = 0
        self._last_error: Optional[str] = None

        self._web3 = Web3()
        contract = self._web3.eth.contract(address=Web3.to_checksum_address(CTF_EXCHANGE_ADDRESS), abi=[ORDER_FILLED_EVENT_ABI])
        self._order_filled_event = contract.events.OrderFilled()
        self._order_filled_topic = str(self._order_filled_event.topic)
        self._http_w3 = Web3(
            Web3.HTTPProvider(
                self.http_rpc_url,
                request_kwargs=_http_rpc_request_kwargs(),
            )
        )
        self._http_w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self._rpc_request_id = 0

    @property
    def provider_host(self) -> str:
        try:
            parsed = urlparse(self.wss_url)
            return parsed.netloc or self.wss_url
        except Exception:
            return self.wss_url

    def register_account(
        self,
        account_name: str,
        leader_addresses: Iterable[str],
        *,
        queue_size: Optional[int] = None,
    ) -> "queue.Queue[LeaderTrade]":
        normalized_account = str(account_name or "default").strip() or "default"
        normalized_leaders = {
            _normalize_address(addr)
            for addr in (leader_addresses or [])
            if _normalize_address(addr)
        }
        with self._lock:
            q = self._account_queues.get(normalized_account)
            if q is None:
                q = queue.Queue(maxsize=max(1, int(queue_size or self._default_queue_size)))
                self._account_queues[normalized_account] = q
            for leader in normalized_leaders:
                self._leader_accounts.setdefault(leader, set()).add(normalized_account)
        return q

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._started:
            self._thread.join(timeout=timeout)

    def _next_id(self) -> int:
        with self._lock:
            self._rpc_request_id += 1
            return self._rpc_request_id

    def _run(self) -> None:
        backoff_s = 1.0
        while not self._stop_event.is_set():
            try:
                self._run_connection()
                backoff_s = 1.0
            except Exception as e:
                if self._stop_event.is_set():
                    break
                with self._lock:
                    self._last_error = str(e)
                _log(f"connection error: {e}")
                self._stop_event.wait(backoff_s)
                backoff_s = min(backoff_s * 2.0, 15.0)

    def _run_connection(self) -> None:
        leaders = self._registered_leaders()
        if not leaders:
            while not self._stop_event.wait(1.0):
                leaders = self._registered_leaders()
                if leaders:
                    break
        if not leaders:
            return

        ws = create_connection(
            self.wss_url,
            timeout=20,
            enable_multithread=True,
            **resolve_websocket_proxy_options(self.wss_url),
        )
        ws.settimeout(5.0)
        try:
            self._note_connection_open(leaders)
            _log(
                f"wss connected provider={self.provider_host} leaders={len(leaders)} "
                f"accounts={self._account_count()} exchange={CTF_EXCHANGE_ADDRESS[:10]}..."
            )
            self._subscribe_all(ws, leaders)
            while not self._stop_event.is_set():
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    try:
                        ws.ping()
                    except Exception as e:
                        raise RuntimeError(f"ping failed: {e}") from e
                    continue
                if raw in (None, ""):
                    raise RuntimeError("websocket closed")
                self._handle_ws_message(raw)
        finally:
            self._note_connection_closed()
            try:
                ws.close()
            except Exception:
                pass

    def _registered_leaders(self) -> List[str]:
        with self._lock:
            return sorted(self._leader_accounts.keys())

    def _account_count(self) -> int:
        with self._lock:
            return len(self._account_queues)

    def _subscribe_all(self, ws, leaders: Iterable[str]) -> None:
        for leader in leaders:
            for exchange_address in sorted(CTF_EXCHANGE_ADDRESSES):
                params = {
                    "address": exchange_address,
                    "topics": [self._order_filled_topic, None, _topic_address(leader)],
                }
                payload = {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "eth_subscribe",
                    "params": ["logs", params],
                }
                with self._lock:
                    self._pending_subscription_ids.add(int(payload["id"]))
                    self._subscription_expected = len(self._pending_subscription_ids)
                ws.send(json.dumps(payload))

    def _handle_ws_message(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except Exception as e:
            _log(f"invalid websocket payload: {e}")
            return

        if not isinstance(payload, dict):
            return
        with self._lock:
            self._last_message_ts = time.time()

        if "id" in payload and "result" in payload and payload.get("method") is None:
            self._handle_subscription_ack(payload)
            return
        if payload.get("method") != "eth_subscription":
            if payload.get("error"):
                _log(f"rpc error: {payload.get('error')}")
            return

        params = payload.get("params")
        if not isinstance(params, dict):
            return
        result = params.get("result")
        if not isinstance(result, dict):
            return
        self.handle_log(result)

    def _handle_subscription_ack(self, payload: Dict[str, Any]) -> None:
        sub_id = _coerce_int(payload.get("id"))
        ready_log = None
        with self._lock:
            if sub_id is not None and sub_id in self._pending_subscription_ids:
                self._pending_subscription_ids.remove(sub_id)
                self._subscription_acks += 1
            if (
                not self._ready
                and self._subscription_expected > 0
                and self._subscription_acks >= self._subscription_expected
            ):
                self._ready = True
                ready_log = (
                    f"stream ready connected=1 ready=1 provider={self.provider_host} "
                    f"accounts={len(self._account_queues)} leaders={len(self._leader_accounts)} "
                    f"subscriptions={self._subscription_acks}/{self._subscription_expected} "
                    f"exchange={CTF_EXCHANGE_ADDRESS[:10]}..."
                )
        if ready_log:
            _log(ready_log)

    def handle_log(self, log: Dict[str, Any]) -> None:
        decoded = self._decode_log(log)
        if decoded is None:
            return

        with self._lock:
            self._received_log_count += 1
            self._last_event_ts = time.time()
        args = decoded["args"]
        tx_hash = _hex_text(log.get("transactionHash"))
        block_number = _coerce_int(log.get("blockNumber"))
        ts_int = self._get_block_timestamp(block_number) or int(time.time())
        ts_str = str(ts_int)

        matches: List[Tuple[str, str]] = []
        maker = _normalize_address(args.get("maker"))
        with self._lock:
            if maker in self._leader_accounts:
                matches.append((maker, "maker"))

        for leader_address, leader_role in matches:
            self._handle_leader_fill(
                leader_address,
                leader_role,
                args,
                tx_hash=tx_hash,
                ts_int=ts_int,
                ts_str=ts_str,
                raw=log,
            )

    def _decode_log(self, log: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(log, dict):
            return None
        if _normalize_address(log.get("address")) not in CTF_EXCHANGE_ADDRESSES:
            return None
        if bool(log.get("removed")):
            return None
        try:
            return self._order_filled_event.process_log(log)
        except Exception as e:
            _log(f"log decode failed: {e}")
            return None

    def _handle_leader_fill(
        self,
        leader_address: str,
        leader_role: str,
        args: Dict[str, Any],
        *,
        tx_hash: str,
        ts_int: int,
        ts_str: str,
        raw: Dict[str, Any],
    ) -> None:
        parsed, status = self._build_parsed_trade(
            leader_address,
            leader_role,
            args,
            tx_hash=tx_hash,
            ts_str=ts_str,
            raw=raw,
        )
        if parsed is None:
            return

        fill_key = build_leader_fill_key(leader_address, parsed)
        parsed["fill_key"] = fill_key

        with self._lock:
            account_names = list(self._leader_accounts.get(leader_address, ()))
            if status == "detected":
                self._detected_fill_count += 1
                should_log_first_fill = self._detected_fill_count == 1
            else:
                self._pending_retry_count += 1
                should_log_first_fill = False
        for account_name in account_names:
            attempt_id = self.db.claim_leader_fill(
                build_claim_trade_payload(leader_address, parsed, status=status),
                account_name=account_name,
            )
            if attempt_id is None:
                continue

            self.db.update_last_seen_ts(leader_address, ts_int, account_name=account_name)
            if tx_hash:
                self.db.mark_tx_seen(tx_hash)

            if status != "detected":
                continue

            lt = build_leader_trade(leader_address, parsed, signal_attempt_id=attempt_id)
            if lt is None:
                continue
            self._enqueue(account_name, lt)

        if should_log_first_fill:
            _log(
                f"first live fill leader={leader_address[:10]}... side={parsed.get('side')} "
                f"accounts={len(account_names)} tx={tx_hash[:14]}..."
            )

    def _build_parsed_trade(
        self,
        leader_address: str,
        leader_role: str,
        args: Dict[str, Any],
        *,
        tx_hash: str,
        ts_str: str,
        raw: Dict[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        token_id = _asset_id_text(args.get("tokenId"))
        side_value = _coerce_int(args.get("side"))
        maker_amount = self._scale_amount(args.get("makerAmountFilled"))
        taker_amount = self._scale_amount(args.get("takerAmountFilled"))
        if not token_id or side_value not in (0, 1) or maker_amount is None or taker_amount is None:
            return None, "pending_retry"

        token_meta = self._get_token_market_meta(token_id)
        if side_value == 0:
            side = "BUY"
            usd_amount = maker_amount
            token_amount = taker_amount
        else:
            side = "SELL"
            token_amount = maker_amount
            usd_amount = taker_amount

        if token_amount is None or usd_amount is None:
            return None, "pending_retry"

        price = usd_amount / token_amount if token_amount > 0 else None
        order_hash = _hex_text(args.get("orderHash"))
        log_index = _coerce_int(raw.get("logIndex"))

        parsed = {
            "tx": tx_hash,
            "ts": ts_str,
            "side": side,
            "usd": usd_amount,
            "price": price,
            "size": token_amount,
            "market": token_meta.get("condition_id") if isinstance(token_meta, dict) else None,
            "slug": token_meta.get("market_slug") if isinstance(token_meta, dict) else None,
            "token_id": token_id,
            "outcome_index": token_meta.get("outcome_index") if isinstance(token_meta, dict) else None,
            "outcome": token_meta.get("outcome") if isinstance(token_meta, dict) else None,
            "order_hash": order_hash or None,
            "log_index": log_index,
            "exchange_address": _normalize_address(raw.get("address")),
            "raw": raw,
            "source": "stream",
        }
        return parsed, ("detected" if isinstance(token_meta, dict) else "pending_retry")

    @staticmethod
    def _scale_amount(value: Any) -> Optional[float]:
        ivalue = _coerce_int(value)
        if ivalue is None:
            return None
        return ivalue / 1_000_000.0

    def _get_token_market_meta(self, token_id: str) -> Optional[Dict[str, Any]]:
        normalized = str(token_id or "").strip()
        if not normalized:
            return None

        now = time.monotonic()
        with self._lock:
            cached = self._token_meta_cache.get(normalized)
            if cached is not None and now < cached[0]:
                value = cached[1]
                return dict(value) if isinstance(value, dict) else None

        try:
            data = http_get_json(
                self._session,
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": normalized, "limit": 1},
                timeout_s=15.0,
                max_retries=2,
            )
        except Exception as e:
            _log(f"token meta lookup failed for {normalized[:12]}...: {e}")
            self._cache_token_meta(normalized, None, TOKEN_META_NEGATIVE_TTL_S)
            return None

        meta_row = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else data
            if isinstance(data, dict)
            else None
        )
        meta: Optional[Dict[str, Any]] = None
        if isinstance(meta_row, dict):
            token_ids = self._parse_json_list(meta_row.get("clobTokenIds"))
            outcomes = self._parse_json_list(meta_row.get("outcomes"))
            outcome_index = token_ids.index(normalized) if normalized in token_ids else None
            outcome = None
            if outcome_index is not None and outcome_index < len(outcomes):
                val = outcomes[outcome_index]
                if isinstance(val, str):
                    outcome = val
            condition_id = str(meta_row.get("conditionId") or "").strip().lower() or None
            if condition_id:
                meta = {
                    "condition_id": condition_id,
                    "market_slug": str(meta_row.get("slug") or "").strip() or None,
                    "outcome_index": outcome_index,
                    "outcome": outcome,
                }

        self._cache_token_meta(
            normalized,
            meta,
            TOKEN_META_POSITIVE_TTL_S if meta is not None else TOKEN_META_NEGATIVE_TTL_S,
        )
        return dict(meta) if isinstance(meta, dict) else None

    def _cache_token_meta(self, token_id: str, meta: Optional[Dict[str, Any]], ttl_s: float) -> None:
        with self._lock:
            self._token_meta_cache[token_id] = (time.monotonic() + max(1.0, float(ttl_s)), dict(meta) if isinstance(meta, dict) else None)

    def _get_block_timestamp(self, block_number: Optional[int]) -> Optional[int]:
        if block_number is None:
            return None
        with self._lock:
            cached = self._block_ts_cache.get(int(block_number))
            if cached is not None:
                return int(cached)
        try:
            block = self._http_w3.eth.get_block(int(block_number))
            ts_int = int(block["timestamp"])
        except Exception as e:
            _log(f"block timestamp lookup failed for block={block_number}: {e}")
            return None
        with self._lock:
            self._block_ts_cache[int(block_number)] = ts_int
            while len(self._block_ts_cache) > BLOCK_TS_CACHE_LIMIT:
                self._block_ts_cache.pop(next(iter(self._block_ts_cache)))
        return ts_int

    def _enqueue(self, account_name: str, trade: LeaderTrade) -> None:
        with self._lock:
            q = self._account_queues.get(account_name)
        if q is None:
            return
        try:
            q.put_nowait(trade)
        except queue.Full:
            with self._lock:
                self._queue_full_count += 1
            _log(
                f"queue full for account={account_name}, fill={trade.fill_key[:12]}..., "
                "trade kept in DB pending queue"
            )

    def _note_connection_open(self, leaders: Iterable[str]) -> None:
        now = time.time()
        with self._lock:
            if self._connect_count > 0:
                self._reconnect_count += 1
            self._connect_count += 1
            self._connected = True
            self._ready = False
            self._connected_at = now
            self._last_message_ts = now
            self._subscription_expected = 0
            self._subscription_acks = 0
            self._pending_subscription_ids.clear()
            self._last_error = None

    def _note_connection_closed(self) -> None:
        with self._lock:
            self._connected = False
            self._ready = False

    def get_status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "provider_host": self.provider_host,
                "exchange_address": CTF_EXCHANGE_ADDRESS,
                "started_at": self._started_at,
                "connected": self._connected,
                "ready": self._ready,
                "connected_at": self._connected_at,
                "last_message_ts": self._last_message_ts,
                "last_event_ts": self._last_event_ts,
                "received_logs": self._received_log_count,
                "detected_fills": self._detected_fill_count,
                "pending_retry_fills": self._pending_retry_count,
                "queue_full": self._queue_full_count,
                "subscription_expected": self._subscription_expected,
                "subscription_acks": self._subscription_acks,
                "leaders": len(self._leader_accounts),
                "accounts": len(self._account_queues),
                "connect_count": self._connect_count,
                "reconnect_count": self._reconnect_count,
                "last_error": self._last_error,
            }

    @staticmethod
    def _format_age(ts: float) -> str:
        if not ts:
            return "never"
        delta = max(0, int(time.time() - float(ts)))
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        return f"{delta // 3600}h ago"

    def format_status_line(self) -> str:
        snapshot = self.get_status_snapshot()
        return (
            f"connected={1 if snapshot['connected'] else 0} "
            f"ready={1 if snapshot['ready'] else 0} "
            f"provider={snapshot['provider_host']} "
            f"accounts={snapshot['accounts']} leaders={snapshot['leaders']} "
            f"subs={snapshot['subscription_acks']}/{snapshot['subscription_expected']} "
            f"logs={snapshot['received_logs']} detected={snapshot['detected_fills']} "
            f"pending_retry={snapshot['pending_retry_fills']} queue_full={snapshot['queue_full']} "
            f"last_msg={self._format_age(snapshot['last_message_ts'])} "
            f"last_fill={self._format_age(snapshot['last_event_ts'])} "
            f"reconnects={snapshot['reconnect_count']}"
        )

    @staticmethod
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
