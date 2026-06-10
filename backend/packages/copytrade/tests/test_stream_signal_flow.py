import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.monitor import LeaderTrade, TradeMonitor
from copytrade.signal_hub import LeaderSignalHub
from copytrade.worker import AccountWorker


class _StaticMonitor:
    def __init__(self, trades):
        self._trades = list(trades)

    def poll_once(self):
        return list(self._trades)


class _RecoveringMonitor(TradeMonitor):
    def __init__(self, db: CopyTradeDB, meta_by_token):
        super().__init__(
            requests.Session(),
            db,
            ["0xabc"],
            account_name="acct",
            signal_source="stream",
        )
        self._meta_by_token = meta_by_token

    def _get_token_market_meta(self, token_id: str, *, session=None):
        meta = self._meta_by_token.get(str(token_id))
        return dict(meta) if isinstance(meta, dict) else None


class _PendingRetryHub(LeaderSignalHub):
    def __init__(self, db: CopyTradeDB):
        super().__init__("ws://127.0.0.1:1", db)

    def _get_token_market_meta(self, token_id: str):
        return None

    def _get_block_timestamp(self, block_number):
        return 1_700_000_000


class _NoRisk:
    def check_all(self, leader_trade, our_usd):
        return True, "ok"


class _ExitRecorder:
    def __init__(self):
        self.seen = []

    def process_exits(self, new_trades, skip_verification=False):
        self.seen = list(new_trades)
        return []


class StreamSignalFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def _trade(self, *, fill_key: str = "fill-1", side: str = "BUY") -> LeaderTrade:
        return LeaderTrade(
            leader_address="0xabc",
            tx_hash=f"tx-{fill_key}",
            fill_key=fill_key,
            timestamp="1700000000",
            side=side,
            token_id="tok-1",
            condition_id="cond-1",
            price=0.5,
            size=100.0,
            usd_amount=50.0,
            outcome="YES",
            market_slug="market-1",
            ts_int=1_700_000_000,
        )

    def test_stream_monitor_recovers_detected_rows_without_waiting_for_stale_cutoff(self):
        db = CopyTradeDB(str(self._db_path("stream_pending")))
        try:
            db.claim_leader_fill(
                {
                    "leader_address": "0xabc",
                    "leader_tx_hash": "tx-pending",
                    "leader_fill_key": "fill-pending",
                    "leader_side": "BUY",
                    "leader_price": 0.5,
                    "leader_size": 100.0,
                    "leader_usd": 50.0,
                    "token_id": "tok-1",
                    "condition_id": "cond-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "status": "detected",
                },
                account_name="acct",
            )
            monitor = TradeMonitor(
                requests.Session(),
                db,
                ["0xabc"],
                account_name="acct",
                signal_source="stream",
                fetch_workers=1,
                signal_queue=queue.Queue(),
            )
            trades = monitor.poll_once()
        finally:
            db.close()

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].fill_key, "fill-pending")

    def test_stream_monitor_marks_recovered_detected_rows_seen(self):
        db = CopyTradeDB(str(self._db_path("stream_pending_seen")))
        try:
            db.claim_leader_fill(
                {
                    "leader_address": "0xabc",
                    "leader_tx_hash": "tx-pending",
                    "leader_fill_key": "fill-pending",
                    "leader_side": "BUY",
                    "leader_price": 0.5,
                    "leader_size": 100.0,
                    "leader_usd": 50.0,
                    "token_id": "tok-1",
                    "condition_id": "cond-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "status": "detected",
                },
                account_name="acct",
            )
            monitor = TradeMonitor(
                requests.Session(),
                db,
                ["0xabc"],
                account_name="acct",
                signal_source="stream",
                fetch_workers=1,
                signal_queue=queue.Queue(),
            )
            first = monitor.poll_once()
            second = monitor.poll_once()
        finally:
            db.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].fill_key, "fill-pending")
        self.assertEqual(second, [])

    def test_stream_monitor_ignores_stale_detected_rows_on_restart_recovery(self):
        db = CopyTradeDB(str(self._db_path("stream_pending_stale")))
        try:
            trade_id = db.claim_leader_fill(
                {
                    "leader_address": "0xabc",
                    "leader_tx_hash": "tx-stale",
                    "leader_fill_key": "fill-stale",
                    "leader_side": "BUY",
                    "leader_price": 0.5,
                    "leader_size": 100.0,
                    "leader_usd": 50.0,
                    "token_id": "tok-1",
                    "condition_id": "cond-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "status": "detected",
                },
                account_name="acct",
            )
            self.assertIsNotNone(trade_id)
            db.conn.execute(
                "UPDATE ct_signal_attempts SET created_at=?, updated_at=? WHERE id=?",
                ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", int(trade_id)),
            )
            db.conn.commit()

            monitor = TradeMonitor(
                requests.Session(),
                db,
                ["0xabc"],
                account_name="acct",
                signal_source="stream",
                fetch_workers=1,
                signal_queue=queue.Queue(),
            )
            trades = monitor.poll_once()
        finally:
            db.close()

        self.assertEqual(trades, [])

    def test_stream_worker_does_not_wait_for_large_poll_interval_when_queue_has_signal(self):
        db = CopyTradeDB(str(self._db_path("stream_worker")))
        q = queue.Queue()
        q.put(self._trade())
        account = SimpleNamespace(
            name="acct",
            env_suffix="",
            config=CopyTradeConfig(
                leader_addresses=["0xabc"],
                signal_source="stream",
                poll_interval_s=999.0,
                dry_run=True,
            ),
        )
        worker = AccountWorker(account, db, dry_run=True, once=False, signal_queue=q)
        fired = threading.Event()
        elapsed_holder = {}
        start = time.time()

        def fake_poll_cycle(self, monitor, risk, executor, exit_mgr, cfg):
            elapsed_holder["elapsed"] = time.time() - start
            fired.set()
            self.stop()

        try:
            with patch.object(AccountWorker, "_poll_cycle", new=fake_poll_cycle):
                worker.start()
                self.assertTrue(fired.wait(2.0))
                worker.join(timeout=2.0)
        finally:
            worker.stop()
            worker.join(timeout=2.0)
            db.close()

        self.assertIn("elapsed", elapsed_holder)
        self.assertLess(elapsed_holder["elapsed"], 2.0)

    def test_sell_signal_reaches_exit_manager_in_same_cycle(self):
        db = CopyTradeDB(str(self._db_path("sell_exit")))
        account = SimpleNamespace(
            name="acct",
            env_suffix="",
            config=CopyTradeConfig(leader_addresses=["0xabc"], dry_run=True),
        )
        worker = AccountWorker(account, db, dry_run=True, once=True)
        exit_recorder = _ExitRecorder()
        sell_trade = self._trade(fill_key="fill-sell", side="SELL")

        try:
            worker._poll_cycle(
                _StaticMonitor([sell_trade]),
                _NoRisk(),
                executor=SimpleNamespace(_client=None),
                exit_mgr=exit_recorder,
                cfg=worker.account.config,
            )
        finally:
            db.close()

        self.assertEqual(exit_recorder.seen, [sell_trade])

    def test_pending_retry_stream_fill_recovers_after_meta_becomes_available(self):
        db = CopyTradeDB(str(self._db_path("pending_retry_recover")))
        hub = _PendingRetryHub(db)
        hub.register_account("acct", ["0xabc"])
        args = {
            "maker": "0xabc",
            "taker": "0xdef",
            "orderHash": "0x" + "aa" * 32,
            "side": 0,
            "tokenId": 123,
            "makerAmountFilled": 250_000_000,
            "takerAmountFilled": 1_000_000_000,
        }
        meta_by_token = {}

        try:
            hub._handle_leader_fill(
                "0xabc",
                "maker",
                args,
                tx_hash="0xtx",
                ts_int=1_700_000_000,
                ts_str="1700000000",
                raw={},
            )
            row = db.conn.execute(
                "SELECT status, token_id, condition_id, market_slug, source FROM ct_signal_attempts WHERE account_name='acct'"
            ).fetchone()
            self.assertEqual(tuple(row), ("pending_retry", "123", "", None, "stream"))
            self.assertEqual(db.get_last_seen_ts("0xabc", "acct"), 1_700_000_000)

            monitor = _RecoveringMonitor(db, meta_by_token)
            with patch("copytrade.monitor.PENDING_RETRY_RETRY_DELAY_S", 0):
                self.assertEqual(monitor.poll_once(), [])
                meta_by_token["123"] = {
                    "condition_id": "cond-1",
                    "market_slug": "market-1",
                    "outcome_index": 0,
                    "outcome": "YES",
                }
                trades = monitor.poll_once()

            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].token_id, "123")
            self.assertEqual(trades[0].condition_id, "cond-1")
            self.assertEqual(trades[0].source, "stream")
            rows = db.conn.execute(
                "SELECT status, token_id, condition_id, market_slug, source FROM ct_signal_attempts WHERE account_name='acct'"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(tuple(rows[0]), ("detected", "123", "cond-1", "market-1", "stream"))
        finally:
            db.close()

    def test_monitor_negative_token_meta_cache_expires_and_retries(self):
        db = CopyTradeDB(str(self._db_path("token_meta_retry")))
        monitor = TradeMonitor(requests.Session(), db, ["0xabc"], account_name="acct")
        calls = {"count": 0}

        def fake_http_get_json(session, url, params=None, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("transient")
            return [
                {
                    "conditionId": "cond-1",
                    "slug": "market-1",
                    "clobTokenIds": "[\"123\"]",
                    "outcomes": "[\"YES\"]",
                }
            ]

        try:
            with patch("copytrade.monitor.TOKEN_META_NEGATIVE_TTL_S", 0.01), patch(
                "copytrade.monitor.http_get_json",
                side_effect=fake_http_get_json,
            ):
                self.assertIsNone(monitor._get_token_market_meta("123"))
                time.sleep(0.02)
                meta = monitor._get_token_market_meta("123")
                again = monitor._get_token_market_meta("123")
        finally:
            db.close()

        self.assertEqual(calls["count"], 2)
        self.assertIsNotNone(meta)
        self.assertEqual(meta["condition_id"], "cond-1")
        self.assertEqual(again["market_slug"], "market-1")


if __name__ == "__main__":
    unittest.main()
