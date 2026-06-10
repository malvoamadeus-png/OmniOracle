import asyncio
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.db import CopyTradeDB
from copytrade.user_order_hub import UserOrderHub

try:
    import websockets
except ImportError:  # pragma: no cover - dependency should exist locally
    websockets = None


class UserOrderHubTests(unittest.TestCase):
    @unittest.skipIf(websockets is None, "websockets not installed")
    def test_hub_auth_subscribe_unsubscribe_and_event_normalization(self):
        stop_event = threading.Event()
        auth_event = threading.Event()
        dynamic_subscribe_event = threading.Event()
        unsubscribe_event = threading.Event()
        heartbeat_event = threading.Event()
        info = {}
        auth_payload_holder = {}

        order_payload = {
            "event_type": "order",
            "id": "order-1",
            "market": "cond-1",
            "status": "LIVE",
            "size_matched": "3.0",
            "original_size": "10.0",
            "price": "0.84",
        }
        trade_payload = {
            "event_type": "trade",
            "id": "trade-1",
            "market": "cond-1",
            "status": "MATCHED",
            "size": "1.5",
            "price": "0.84",
            "taker_order_id": "order-1",
            "maker_orders": [
                {
                    "order_id": "maker-1",
                    "matched_amount": "1.5",
                    "price": "0.84",
                }
            ],
        }

        async def handler(websocket, *args):
            try:
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=0.2)
                    except asyncio.TimeoutError:
                        continue
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = raw

                    if isinstance(payload, dict) and payload.get("type") == "user" and payload.get("auth"):
                        auth_payload_holder["payload"] = payload
                        auth_event.set()
                        await websocket.send(json.dumps({}))
                        if "cond-1" in (payload.get("markets") or []):
                            await websocket.send(json.dumps(order_payload))
                            await websocket.send(json.dumps(trade_payload))
                        continue
                    if isinstance(payload, dict) and payload.get("operation") == "subscribe":
                        if "cond-2" in (payload.get("markets") or []):
                            dynamic_subscribe_event.set()
                            await websocket.send(json.dumps({}))
                        continue
                    if isinstance(payload, dict) and payload.get("operation") == "unsubscribe":
                        if "cond-1" in (payload.get("markets") or []):
                            unsubscribe_event.set()
                            await websocket.send(json.dumps({}))
                        continue
                    if payload == "PING":
                        heartbeat_event.set()
            except Exception:
                return

        async def server_main():
            server = await websockets.serve(handler, "127.0.0.1", 0)
            info["port"] = server.sockets[0].getsockname()[1]
            try:
                while not stop_event.is_set():
                    await asyncio.sleep(0.05)
            finally:
                server.close()
                await server.wait_closed()

        def run_server():
            asyncio.run(server_main())

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        while "port" not in info:
            time.sleep(0.05)

        hub = None
        try:
            with patch("copytrade.user_order_hub.PING_INTERVAL_S", 0.1):
                hub = UserOrderHub(
                    "acct",
                    api_key="api-key",
                    api_secret="api-secret",
                    api_passphrase="api-passphrase",
                    wss_url=f"ws://127.0.0.1:{info['port']}",
                )
                hub.ensure_market("cond-1")
                hub.start()

                events = []
                deadline = time.time() + 5.0
                while time.time() < deadline:
                    events.extend(hub.drain_events())
                    if auth_event.is_set() and len(events) >= 3:
                        break
                    time.sleep(0.05)

                self.assertTrue(auth_event.is_set())
                self.assertGreaterEqual(len(events), 3)
                auth_payload = auth_payload_holder["payload"]
                self.assertEqual(auth_payload.get("markets"), ["cond-1"])
                self.assertNotIn("assets_ids", auth_payload)
                self.assertNotIn("initial_dump", auth_payload)

                order_events = [event for event in events if event.channel_event == "order"]
                trade_events = [event for event in events if event.channel_event == "trade"]
                self.assertEqual(len(order_events), 1)
                self.assertEqual(order_events[0].order_id, "order-1")
                self.assertEqual(order_events[0].exchange_order_status, "live")
                self.assertAlmostEqual(order_events[0].matched_size, 3.0, places=6)
                self.assertTrue(any(event.order_id == "order-1" for event in trade_events))
                self.assertTrue(any(event.order_id == "maker-1" for event in trade_events))
                self.assertTrue(all(event.is_delta for event in trade_events))

                hub.replace_markets(["cond-2"])
                deadline = time.time() + 3.0
                while time.time() < deadline and not (unsubscribe_event.is_set() and dynamic_subscribe_event.is_set()):
                    time.sleep(0.05)
                self.assertTrue(unsubscribe_event.is_set())
                self.assertTrue(dynamic_subscribe_event.is_set())

                deadline = time.time() + 3.0
                while time.time() < deadline and not heartbeat_event.is_set():
                    time.sleep(0.05)
                self.assertTrue(heartbeat_event.is_set())
        finally:
            if hub is not None:
                hub.stop()
                hub.join(timeout=5.0)
            stop_event.set()
            thread.join(timeout=5.0)


class UserOrderSubscriptionSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = CopyTradeDB(str(Path(self.tmpdir.name) / "copytrade.sqlite"))

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _set_created_at(self, table: str, row_id: int, when: datetime) -> None:
        self.db.conn.execute(
            f"UPDATE {table} SET created_at=?, updated_at=? WHERE id=?",
            (when.isoformat(), when.isoformat(), int(row_id)),
        )
        self.db.conn.commit()

    def _insert_trade(self, condition_id: str, *, status: str, exchange_status: str) -> int:
        return self.db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": f"tx-{condition_id}",
                "leader_fill_key": f"fill-{condition_id}",
                "leader_side": "BUY",
                "our_order_id": f"order-{condition_id}",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 10.0,
                "our_usd": 5.0,
                "token_id": f"tok-{condition_id}",
                "condition_id": condition_id,
                "market_slug": condition_id,
                "outcome": "YES",
                "status": status,
                "exchange_order_status": exchange_status,
            }
        )

    def test_active_user_order_markets_exclude_old_gtd_but_keep_auto_tp_gtc_rows(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        old = datetime.now(timezone.utc) - timedelta(days=4)

        failed_id = self._insert_trade(
            "cond-failed",
            status="failed",
            exchange_status="submitted",
        )
        self._set_created_at("ct_trades", failed_id, recent)

        old_trade_id = self._insert_trade(
            "cond-old-trade",
            status="submitted",
            exchange_status="live",
        )
        self._set_created_at("ct_trades", old_trade_id, old)

        recent_trade_id = self._insert_trade(
            "cond-recent-trade",
            status="partially_filled",
            exchange_status="live",
        )
        self._set_created_at("ct_trades", recent_trade_id, recent)

        exit_trade_id = self._insert_trade(
            "cond-recent-exit",
            status="filled",
            exchange_status="matched",
        )
        exit_id = self.db.insert_exit_order(
            {
                "trade_id": exit_trade_id,
                "account_name": "acct",
                "reason": "mirror_sell",
                "order_id": "exit-order-1",
                "token_id": "tok-exit",
                "side": "SELL",
                "requested_price": 0.6,
                "requested_size": 10.0,
                "requested_usd": 6.0,
                "status": "submitted",
                "exchange_order_status": "live",
            }
        )
        self._set_created_at("ct_exit_orders", exit_id, recent)

        bucket_id = self.db.insert_auto_tp_bucket_order(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "token_id": "tok-bucket",
                "condition_id": "cond-old-bucket-gtc",
                "market_slug": "bucket",
                "outcome": "YES",
                "kind": "tp_sell",
                "side": "SELL",
                "bucket_price": 0.85,
                "requested_size": 3.0,
                "requested_usd": 2.55,
                "order_id": "bucket-order-1",
                "status": "submitted",
                "exchange_order_status": "live",
            }
        )
        self._set_created_at("ct_auto_tp_bucket_orders", bucket_id, old)

        active_bucket_id = self.db.insert_auto_tp_bucket_order(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "token_id": "tok-active-bucket",
                "condition_id": "cond-bucket-gtc",
                "market_slug": "bucket-active",
                "outcome": "YES",
                "kind": "tp_sell",
                "side": "SELL",
                "bucket_price": 0.85,
                "requested_size": 3.0,
                "requested_usd": 2.55,
                "order_id": "bucket-order-2",
                "status": "submitted",
                "exchange_order_status": "live",
            }
        )
        self._set_created_at("ct_auto_tp_bucket_orders", active_bucket_id, recent)

        active = self.db.get_active_user_order_condition_ids(
            account_name="acct",
            recent_gtd_hours=12,
        )

        self.assertEqual(
            active,
            ["cond-bucket-gtc", "cond-old-bucket-gtc", "cond-recent-exit", "cond-recent-trade"],
        )


if __name__ == "__main__":
    unittest.main()
