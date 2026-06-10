"""Authenticated user-order websocket hub for account-specific order updates."""

import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

from websocket import WebSocketTimeoutException, create_connection

from copytrade.ws_proxy import resolve_websocket_proxy_options


DEFAULT_USER_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
DEFAULT_QUEUE_SIZE = 4096
PING_INTERVAL_S = 10.0
TRADE_DEDUPE_TTL_S = 60 * 60.0
TRADE_DEDUPE_LIMIT = 10_000
EPS = 1e-9
NON_JSON_LOG_THROTTLE_S = 60.0


def _log(account_name: str, msg: str) -> None:
    sys.stderr.write(f"[{account_name}] [user_order_hub] {msg.rstrip()}\n")
    sys.stderr.flush()


def _coerce_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text == "canceled":
        return "cancelled"
    return text


@dataclass
class UserOrderEvent:
    channel_event: str
    order_id: str
    condition_id: str
    exchange_order_status: str
    matched_size: float
    price: Optional[float]
    is_delta: bool
    raw_id: str
    raw_payload: Dict[str, Any]


class UserOrderHub:
    def __init__(
        self,
        account_name: str,
        *,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        wss_url: str = DEFAULT_USER_WS_URL,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ):
        self.account_name = str(account_name or "default").strip() or "default"
        self.wss_url = str(wss_url or "").strip() or DEFAULT_USER_WS_URL
        self._auth = {
            "apiKey": str(api_key or "").strip(),
            "secret": str(api_secret or "").strip(),
            "passphrase": str(api_passphrase or "").strip(),
        }
        if not all(self._auth.values()):
            raise ValueError("api_key/api_secret/api_passphrase are required")

        self._queue: "queue.Queue[UserOrderEvent]" = queue.Queue(
            maxsize=max(1, int(queue_size or DEFAULT_QUEUE_SIZE))
        )
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"user-order-hub-{self.account_name}",
            daemon=True,
        )
        self._started = False

        self._desired_markets: Set[str] = set()
        self._subscribed_markets: Set[str] = set()
        self._connected = False
        self._ready = False
        self._connect_count = 0
        self._reconnect_count = 0
        self._last_message_ts = 0.0
        self._last_event_ts = 0.0
        self._last_error: Optional[str] = None
        self._force_reconcile = False
        self._trade_event_seen: Dict[str, float] = {}
        self._last_non_json_log_ts = 0.0

    @property
    def provider_host(self) -> str:
        try:
            parsed = urlparse(self.wss_url)
            return parsed.netloc or self.wss_url
        except Exception:
            return self.wss_url

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

    def is_ready(self) -> bool:
        with self._lock:
            return bool(self._connected and self._ready)

    def consume_force_reconcile(self) -> bool:
        with self._lock:
            value = bool(self._force_reconcile)
            self._force_reconcile = False
            return value

    def replace_markets(self, condition_ids: Iterable[str]) -> None:
        normalized = {
            str(value or "").strip().lower()
            for value in (condition_ids or [])
            if str(value or "").strip()
        }
        with self._lock:
            self._desired_markets = normalized

    def ensure_market(self, condition_id: str) -> None:
        normalized = str(condition_id or "").strip().lower()
        if not normalized:
            return
        with self._lock:
            self._desired_markets.add(normalized)

    def drain_events(self) -> List[UserOrderEvent]:
        out: List[UserOrderEvent] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def get_status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "provider_host": self.provider_host,
                "connected": self._connected,
                "ready": self._ready,
                "desired_markets": len(self._desired_markets),
                "subscribed_markets": len(self._subscribed_markets),
                "connect_count": self._connect_count,
                "reconnect_count": self._reconnect_count,
                "last_message_ts": self._last_message_ts,
                "last_event_ts": self._last_event_ts,
                "last_error": self._last_error,
            }

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
                _log(self.account_name, f"connection error: {e}")
                self._stop_event.wait(backoff_s)
                backoff_s = min(backoff_s * 2.0, 30.0)

    def _run_connection(self) -> None:
        desired = self._desired_snapshot(wait=True)
        if not desired:
            return

        ws = create_connection(
            self.wss_url,
            timeout=20,
            enable_multithread=True,
            **resolve_websocket_proxy_options(self.wss_url),
        )
        ws.settimeout(1.0)
        last_ping_ts = 0.0
        try:
            self._note_connection_open()
            auth_payload = {
                "auth": dict(self._auth),
                "type": "user",
                "markets": sorted(desired),
            }
            ws.send(json.dumps(auth_payload))
            with self._lock:
                self._subscribed_markets.update(desired)
            self._sync_subscriptions(ws)
            self._mark_ready()
            _log(
                self.account_name,
                f"connected provider={self.provider_host} markets={len(desired)}",
            )
            while not self._stop_event.is_set():
                self._sync_subscriptions(ws)
                now = time.time()
                if now - last_ping_ts >= PING_INTERVAL_S:
                    ws.send("PING")
                    last_ping_ts = now
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
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

    def _desired_snapshot(self, *, wait: bool = False) -> Set[str]:
        while True:
            with self._lock:
                snapshot = set(self._desired_markets)
            if snapshot or not wait or self._stop_event.is_set():
                return snapshot
            self._stop_event.wait(1.0)

    def _sync_subscriptions(self, ws) -> None:
        with self._lock:
            desired = set(self._desired_markets)
            subscribed = set(self._subscribed_markets)
        to_subscribe = sorted(desired - subscribed)
        to_unsubscribe = sorted(subscribed - desired)
        if to_subscribe:
            ws.send(json.dumps({"operation": "subscribe", "markets": to_subscribe}))
            with self._lock:
                self._subscribed_markets.update(to_subscribe)
        if to_unsubscribe:
            ws.send(json.dumps({"operation": "unsubscribe", "markets": to_unsubscribe}))
            with self._lock:
                self._subscribed_markets.difference_update(to_unsubscribe)

    def _handle_ws_message(self, raw: Any) -> None:
        with self._lock:
            self._last_message_ts = time.time()

        text_payload: Optional[str] = None
        if isinstance(raw, bytes):
            try:
                text_payload = raw.decode("utf-8", errors="ignore")
            except Exception:
                text_payload = None
        elif isinstance(raw, str):
            text_payload = raw

        stripped = str(text_payload or "").strip()
        if stripped.upper() in {"PONG", "PING"}:
            return
        if not stripped:
            return
        if stripped in {"{}", "[]"}:
            return
        if stripped[:1] not in {"{", "["}:
            self._log_non_json_payload(stripped)
            return
        try:
            payload = json.loads(text_payload if text_payload is not None else raw)
        except Exception as e:
            self._log_non_json_payload(stripped, error=e)
            return
        if not isinstance(payload, dict):
            return
        if not payload:
            return
        if payload.get("error"):
            raise RuntimeError(f"server error: {payload.get('error')}")
        event_type = str(payload.get("event_type") or "").strip().lower()
        if event_type == "order":
            event = self._normalize_order_event(payload)
            if event is not None:
                self._enqueue(event)
            return
        if event_type == "trade":
            for event in self._normalize_trade_events(payload):
                self._enqueue(event)

    def _log_non_json_payload(self, payload: str, *, error: Optional[Exception] = None) -> None:
        now = time.time()
        with self._lock:
            last_ts = float(self._last_non_json_log_ts or 0.0)
            if last_ts and (now - last_ts) < NON_JSON_LOG_THROTTLE_S:
                return
            self._last_non_json_log_ts = now
        preview = str(payload or "").replace("\r", "\\r").replace("\n", "\\n")[:80]
        if error is not None:
            _log(self.account_name, f"ignored non-json payload preview={preview!r} error={error}")
        else:
            _log(self.account_name, f"ignored non-json payload preview={preview!r}")

    def _normalize_order_event(self, payload: Dict[str, Any]) -> Optional[UserOrderEvent]:
        order_id = str(payload.get("id") or "").strip()
        condition_id = str(payload.get("market") or "").strip().lower()
        if not order_id or not condition_id:
            return None

        matched = max(0.0, _coerce_float(payload.get("size_matched")) or 0.0)
        original_size = max(0.0, _coerce_float(payload.get("original_size")) or 0.0)
        price = _coerce_float(
            payload.get("avg_price")
            or payload.get("avgPrice")
            or payload.get("average_price")
            or payload.get("averagePrice")
        )
        status = _normalize_status(payload.get("status"))
        event_kind = str(payload.get("type") or "").strip().upper()

        if status not in {"live", "matched", "expired", "cancelled"}:
            if event_kind == "CANCELLATION":
                status = "cancelled"
            elif original_size > 0 and matched + EPS >= original_size:
                status = "matched"
            else:
                status = "live"

        return UserOrderEvent(
            channel_event="order",
            order_id=order_id,
            condition_id=condition_id,
            exchange_order_status=status,
            matched_size=matched,
            price=price,
            is_delta=False,
            raw_id=order_id,
            raw_payload=dict(payload),
        )

    def _normalize_trade_events(self, payload: Dict[str, Any]) -> List[UserOrderEvent]:
        status = _normalize_status(payload.get("status"))
        if status != "matched":
            return []

        trade_id = str(payload.get("id") or "").strip()
        condition_id = str(payload.get("market") or "").strip().lower()
        if not trade_id or not condition_id:
            return []

        events: List[UserOrderEvent] = []
        taker_order_id = str(payload.get("taker_order_id") or "").strip()
        taker_delta = max(0.0, _coerce_float(payload.get("size")) or 0.0)
        taker_price = _coerce_float(payload.get("price"))
        if taker_order_id and taker_delta > EPS:
            event = self._build_trade_event(
                trade_id=trade_id,
                order_id=taker_order_id,
                condition_id=condition_id,
                matched_size=taker_delta,
                price=taker_price,
                payload=payload,
            )
            if event is not None:
                events.append(event)

        maker_orders = payload.get("maker_orders") or []
        if isinstance(maker_orders, list):
            for item in maker_orders:
                if not isinstance(item, dict):
                    continue
                order_id = str(item.get("order_id") or "").strip()
                matched_size = max(0.0, _coerce_float(item.get("matched_amount")) or 0.0)
                price = _coerce_float(item.get("price"))
                if not order_id or matched_size <= EPS:
                    continue
                event = self._build_trade_event(
                    trade_id=trade_id,
                    order_id=order_id,
                    condition_id=condition_id,
                    matched_size=matched_size,
                    price=price,
                    payload=payload,
                )
                if event is not None:
                    events.append(event)
        return events

    def _build_trade_event(
        self,
        *,
        trade_id: str,
        order_id: str,
        condition_id: str,
        matched_size: float,
        price: Optional[float],
        payload: Dict[str, Any],
    ) -> Optional[UserOrderEvent]:
        dedupe_key = f"{trade_id}:{order_id}:matched"
        if self._is_duplicate_trade_event(dedupe_key):
            return None
        return UserOrderEvent(
            channel_event="trade",
            order_id=order_id,
            condition_id=condition_id,
            exchange_order_status="matched",
            matched_size=max(0.0, float(matched_size or 0.0)),
            price=price,
            is_delta=True,
            raw_id=trade_id,
            raw_payload=dict(payload),
        )

    def _is_duplicate_trade_event(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            existing = self._trade_event_seen.get(key)
            if existing is not None and (now - existing) < TRADE_DEDUPE_TTL_S:
                return True
            self._trade_event_seen[key] = now
            if len(self._trade_event_seen) > TRADE_DEDUPE_LIMIT:
                cutoff = now - TRADE_DEDUPE_TTL_S
                stale = [item for item, ts in self._trade_event_seen.items() if ts < cutoff]
                for item in stale:
                    self._trade_event_seen.pop(item, None)
                while len(self._trade_event_seen) > TRADE_DEDUPE_LIMIT:
                    self._trade_event_seen.pop(next(iter(self._trade_event_seen)))
        return False

    def _enqueue(self, event: UserOrderEvent) -> None:
        with self._lock:
            self._last_event_ts = time.time()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            _log(
                self.account_name,
                f"queue full, dropped {event.channel_event} event order={event.order_id[:14]}...",
            )

    def _mark_ready(self) -> None:
        with self._lock:
            self._ready = True
            self._force_reconcile = True
            self._last_error = None

    def _note_connection_open(self) -> None:
        with self._lock:
            if self._connect_count > 0:
                self._reconnect_count += 1
            self._connect_count += 1
            self._connected = True
            self._ready = False
            self._subscribed_markets = set()

    def _note_connection_closed(self) -> None:
        with self._lock:
            self._connected = False
            self._ready = False
