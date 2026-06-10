import asyncio
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests
from eth_abi import encode
from web3 import Web3

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.db import CopyTradeDB
from copytrade.monitor import build_leader_fill_key
from copytrade.signal_hub import (
    CTF_EXCHANGE_ADDRESSES,
    CTF_EXCHANGE_ADDRESS,
    HTTP_RPC_ACCEPT_ENCODING,
    HTTP_RPC_TIMEOUT_S,
    NEG_RISK_CTF_EXCHANGE_ADDRESS,
    ORDER_FILLED_EVENT_ABI,
    LeaderSignalHub,
)

try:
    import websockets
except ImportError:  # pragma: no cover - dependency should exist locally
    websockets = None


_W3 = Web3()
_EVENT = _W3.eth.contract(address=Web3.to_checksum_address(CTF_EXCHANGE_ADDRESS), abi=[ORDER_FILLED_EVENT_ABI]).events.OrderFilled()
_TOPIC0 = str(_EVENT.topic)


def _pad_topic_address(address: str) -> str:
    normalized = str(address).lower()
    if normalized.startswith("0x"):
        normalized = normalized[2:]
    return "0x" + ("0" * 24) + normalized.rjust(40, "0")


def _make_order_filled_log(
    *,
    maker: str,
    taker: str,
    side: int,
    token_id: int,
    maker_amount: int,
    taker_amount: int,
    exchange_address: str = CTF_EXCHANGE_ADDRESS,
    log_index: int = 0,
    tx_hash: str = "0x" + "11" * 32,
    block_number: int = 12345,
    order_hash: str = "0x" + "aa" * 32,
    fee: int = 0,
) -> dict:
    data = "0x" + encode(
        ["uint8", "uint256", "uint256", "uint256", "uint256", "bytes32", "bytes32"],
        [
            int(side),
            int(token_id),
            maker_amount,
            taker_amount,
            fee,
            b"\x00" * 32,
            b"\x00" * 32,
        ],
    ).hex()
    return {
        "address": exchange_address,
        "blockNumber": hex(block_number),
        "transactionHash": tx_hash,
        "logIndex": hex(log_index),
        "transactionIndex": hex(0),
        "blockHash": "0x" + "22" * 32,
        "removed": False,
        "topics": [
            _TOPIC0,
            order_hash,
            _pad_topic_address(maker),
            _pad_topic_address(taker),
        ],
        "data": data,
    }


class _TestHub(LeaderSignalHub):
    def __init__(self, wss_url: str, db: CopyTradeDB, *, token_meta=None):
        super().__init__(wss_url, db)
        self._meta_map = {str(k): dict(v) for k, v in (token_meta or {}).items()}

    def _get_token_market_meta(self, token_id: str):
        meta = self._meta_map.get(str(token_id))
        return dict(meta) if isinstance(meta, dict) else None

    def _get_block_timestamp(self, block_number):
        return 1_700_000_000


class SignalHubTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def test_order_filled_side_mapping_uses_v2_event_side_for_maker(self):
        leader = "0x0000000000000000000000000000000000000abc"
        token_id = "123"
        db = CopyTradeDB(str(self._db_path("direction_map")))
        try:
            hub = _TestHub(
                "ws://127.0.0.1:1",
                db,
                token_meta={
                    token_id: {
                        "condition_id": "cond-1",
                        "market_slug": "market-1",
                        "outcome_index": 0,
                        "outcome": "YES",
                    }
                },
            )
            cases = [
                (0, "BUY", 250.0, 1000.0),
                (1, "SELL", 1000.0, 250.0),
            ]
            for event_side, expected_side, expected_usd, expected_size in cases:
                with self.subTest(event_side=event_side, expected=expected_side):
                    parsed, status = hub._build_parsed_trade(
                        leader,
                        "maker",
                        {
                            "maker": leader,
                            "taker": "0x0000000000000000000000000000000000000def",
                            "side": event_side,
                            "tokenId": int(token_id),
                            "makerAmountFilled": 250_000_000,
                            "takerAmountFilled": 1_000_000_000,
                        },
                        tx_hash="0xtest",
                        ts_str="1700000000",
                        raw={},
                    )
                    self.assertEqual(status, "detected")
                    self.assertIsNotNone(parsed)
                    self.assertEqual(parsed["side"], expected_side)
                    self.assertEqual(parsed["token_id"], token_id)
                    self.assertEqual(parsed["market"], "cond-1")
                    self.assertAlmostEqual(parsed["usd"], expected_usd, places=6)
                    self.assertAlmostEqual(parsed["size"], expected_size, places=6)
        finally:
            db.close()

    def test_subscribe_all_uses_maker_topic_for_both_v2_exchanges(self):
        leader = "0x0000000000000000000000000000000000000abc"
        db = CopyTradeDB(str(self._db_path("subscribe_topics")))

        class _CaptureWs:
            def __init__(self):
                self.payloads = []

            def send(self, raw):
                self.payloads.append(json.loads(raw))

        try:
            hub = LeaderSignalHub("ws://127.0.0.1:1", db)
            ws = _CaptureWs()

            hub._subscribe_all(ws, [leader])

            self.assertEqual(len(ws.payloads), len(CTF_EXCHANGE_ADDRESSES))
            addresses = {p["params"][1]["address"].lower() for p in ws.payloads}
            self.assertEqual(addresses, CTF_EXCHANGE_ADDRESSES)
            for payload in ws.payloads:
                topics = payload["params"][1]["topics"]
                self.assertEqual(topics[0], _TOPIC0)
                self.assertIsNone(topics[1])
                self.assertEqual(topics[2], _pad_topic_address(leader))
                self.assertEqual(len(topics), 3)
        finally:
            db.close()

    def test_http_rpc_provider_disables_zstd_accept_encoding(self):
        db = CopyTradeDB(str(self._db_path("http_rpc_headers")))
        try:
            hub = LeaderSignalHub("ws://127.0.0.1:1", db)
            provider = hub._http_w3.provider
            request_kwargs = dict(provider.get_request_kwargs())
            headers = dict(request_kwargs.get("headers") or {})

            self.assertEqual(request_kwargs.get("timeout"), HTTP_RPC_TIMEOUT_S)
            self.assertEqual(headers.get("Accept-Encoding"), HTTP_RPC_ACCEPT_ENCODING)
            self.assertEqual(headers.get("Content-Type"), "application/json")
            self.assertNotIn("zstd", str(headers.get("Accept-Encoding", "")).lower())
        finally:
            db.close()

    def test_single_event_fans_out_to_multiple_accounts_and_dedupes_replays(self):
        leader = "0x0000000000000000000000000000000000000abc"
        token_id = "123"
        db = CopyTradeDB(str(self._db_path("fanout")))
        try:
            hub = _TestHub(
                "ws://127.0.0.1:1",
                db,
                token_meta={
                    token_id: {
                        "condition_id": "cond-1",
                        "market_slug": "market-1",
                        "outcome_index": 0,
                        "outcome": "YES",
                    }
                },
            )
            q_a = hub.register_account("acct_a", [leader])
            q_b = hub.register_account("acct_b", [leader])

            log = _make_order_filled_log(
                maker=leader,
                taker="0x0000000000000000000000000000000000000def",
                side=0,
                token_id=int(token_id),
                maker_amount=250_000_000,
                taker_amount=1_000_000_000,
                log_index=7,
            )

            hub.handle_log(log)
            hub.handle_log(log)

            trade_a = q_a.get_nowait()
            trade_b = q_b.get_nowait()
            self.assertEqual(trade_a.fill_key, trade_b.fill_key)
            self.assertEqual(trade_a.side, "BUY")
            self.assertEqual(trade_b.side, "BUY")
            parsed = {
                "exchange_address": CTF_EXCHANGE_ADDRESS.lower(),
                "order_hash": "0x" + "aa" * 32,
                "log_index": 7,
                "tx": "0x" + "11" * 32,
                "token_id": token_id,
                "market": "cond-1",
                "side": "BUY",
                "outcome_index": 0,
                "price": 0.25,
                "size": 1000.0,
                "usd": 250.0,
                "ts": "1700000000",
            }
            self.assertEqual(trade_a.fill_key, build_leader_fill_key(leader, parsed))
            with self.assertRaises(queue.Empty):
                q_a.get_nowait()
            with self.assertRaises(queue.Empty):
                q_b.get_nowait()

            rows = db.conn.execute(
                "SELECT account_name, status, leader_fill_key FROM ct_signal_attempts ORDER BY account_name"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row["account_name"] for row in rows], ["acct_a", "acct_b"])
            self.assertTrue(all(row["status"] == "detected" for row in rows))
        finally:
            db.close()

    def test_neg_risk_exchange_logs_are_accepted(self):
        leader = "0x0000000000000000000000000000000000000abc"
        token_id = "123"
        db = CopyTradeDB(str(self._db_path("neg_risk")))
        try:
            hub = _TestHub(
                "ws://127.0.0.1:1",
                db,
                token_meta={
                    token_id: {
                        "condition_id": "cond-1",
                        "market_slug": "market-1",
                        "outcome_index": 0,
                        "outcome": "YES",
                    }
                },
            )
            q = hub.register_account("acct", [leader])

            hub.handle_log(
                _make_order_filled_log(
                    maker=leader,
                    taker="0x0000000000000000000000000000000000000def",
                    side=1,
                    token_id=int(token_id),
                    maker_amount=1_000_000_000,
                    taker_amount=250_000_000,
                    exchange_address=NEG_RISK_CTF_EXCHANGE_ADDRESS,
                )
            )

            trade = q.get_nowait()
            self.assertEqual(trade.side, "SELL")
            self.assertEqual(trade.token_id, token_id)
        finally:
            db.close()

    @unittest.skipIf(websockets is None, "websockets not installed")
    def test_fake_websocket_server_drives_end_to_end_signal_delivery(self):
        leader = "0x0000000000000000000000000000000000000abc"
        token_id = "123"
        db = CopyTradeDB(str(self._db_path("ws_integration")))
        stop_event = threading.Event()
        sent_event = threading.Event()

        log = _make_order_filled_log(
            maker=leader,
            taker="0x0000000000000000000000000000000000000def",
            side=0,
            token_id=int(token_id),
            maker_amount=250_000_000,
            taker_amount=1_000_000_000,
        )

        async def handler(websocket, *args):
            subscriptions = []
            try:
                while not stop_event.is_set():
                    raw = await asyncio.wait_for(websocket.recv(), timeout=0.2)
                    payload = json.loads(raw)
                    subscriptions.append(payload["id"])
                    await websocket.send(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": f"sub-{payload['id']}"}))
                    if len(subscriptions) >= 2 and not sent_event.is_set():
                        await websocket.send(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "method": "eth_subscription",
                                    "params": {
                                        "subscription": "sub-1",
                                        "result": log,
                                    },
                                }
                            )
                        )
                        sent_event.set()
            except asyncio.TimeoutError:
                if not stop_event.is_set():
                    await asyncio.sleep(0.05)
            except Exception:
                return

        async def server_main(info_holder):
            server = await websockets.serve(handler, "127.0.0.1", 0)
            info_holder["port"] = server.sockets[0].getsockname()[1]
            while not stop_event.is_set():
                await asyncio.sleep(0.05)
            server.close()
            await server.wait_closed()

        info = {}

        def run_server():
            asyncio.run(server_main(info))

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        while "port" not in info:
            time.sleep(0.05)

        try:
            hub = None
            hub = _TestHub(
                f"ws://127.0.0.1:{info['port']}",
                db,
                token_meta={
                    token_id: {
                        "condition_id": "cond-1",
                        "market_slug": "market-1",
                        "outcome_index": 0,
                        "outcome": "YES",
                    }
                },
            )
            q = hub.register_account("acct", [leader])
            hub.start()
            trade = q.get(timeout=5.0)
            self.assertEqual(trade.side, "BUY")
            status = hub.get_status_snapshot()
            self.assertTrue(status["connected"])
            self.assertTrue(status["ready"])
            self.assertEqual(status["subscription_acks"], status["subscription_expected"])
            self.assertGreaterEqual(status["received_logs"], 1)
            self.assertGreaterEqual(status["detected_fills"], 1)
            row = db.conn.execute(
                "SELECT status, leader_fill_key FROM ct_signal_attempts WHERE account_name='acct'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "detected")
        finally:
            try:
                if hub is not None:
                    hub.stop()
                    hub.join(timeout=5)
            except Exception:
                pass
            db.close()
            stop_event.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
