import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.monitor import TradeMonitor
from copytrade.risk import RiskManager
from copytrade.polymarket_public_api import extract_position_fields, extract_trade_fields


class _StaticMonitor(TradeMonitor):
    def __init__(self, db: CopyTradeDB, leader: str, trades, *, account_name: str):
        super().__init__(requests.Session(), db, [leader], account_name=account_name)
        self._trades = [dict(t) for t in trades]

    def _fetch_activity(self, address: str, start_ts: int, cutoff_ts: int):
        return [dict(t) for t in self._trades]


class MonitorAccountScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def test_multi_account_monitors_do_not_consume_each_others_fills(self):
        leader = "0xabc"
        ts = int(time.time())
        trade = {
            "tx": "0xtx1",
            "fill_key": "fill-1",
            "ts": str(ts),
            "side": "BUY",
            "token_id": "tok-1",
            "market": "cond-1",
            "price": 0.5,
            "size": 20.0,
            "usd": 10.0,
            "outcome_index": 0,
            "slug": "market-1-total-8pt5",
        }

        db = CopyTradeDB(str(self._db_path("multi_account")))
        try:
            monitor_a = _StaticMonitor(db, leader, [trade], account_name="acct_a")
            monitor_b = _StaticMonitor(db, leader, [trade], account_name="acct_b")

            first_a = monitor_a.poll_once()
            first_b = monitor_b.poll_once()
            second_a = monitor_a.poll_once()

            self.assertEqual(len(first_a), 1)
            self.assertEqual(len(first_b), 1)
            self.assertEqual(len(second_a), 0)
            self.assertEqual(db.get_last_seen_ts(leader, "acct_a"), ts)
            self.assertEqual(db.get_last_seen_ts(leader, "acct_b"), ts)
        finally:
            db.close()

    def test_legacy_monitor_state_falls_back_without_cross_account_blocking_new_fills(self):
        db_path = self._db_path("legacy_scope")
        raw = sqlite3.connect(db_path)
        try:
            raw.executescript("""
                CREATE TABLE ct_leader_state (
                    address TEXT PRIMARY KEY,
                    last_seen_ts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE ct_seen_fills (
                    fill_key TEXT PRIMARY KEY,
                    seen_at TEXT NOT NULL
                );
            """)
            raw.execute(
                "INSERT INTO ct_leader_state(address, last_seen_ts, updated_at) VALUES(?, ?, ?)",
                ("0xabc", 123, "2026-04-05T00:00:00+00:00"),
            )
            raw.execute(
                "INSERT INTO ct_seen_fills(fill_key, seen_at) VALUES(?, ?)",
                ("legacy-fill", "2026-04-05T00:00:00+00:00"),
            )
            raw.commit()
        finally:
            raw.close()

        db = CopyTradeDB(str(db_path))
        try:
            self.assertEqual(db.get_last_seen_ts("0xabc", "acct_a"), 123)
            self.assertTrue(db.is_fill_seen("legacy-fill", account_name="acct_a"))

            db.update_last_seen_ts("0xabc", 456, account_name="acct_a")
            db.mark_fill_seen("new-fill", account_name="acct_a")

            self.assertEqual(db.get_last_seen_ts("0xabc", "acct_a"), 456)
            self.assertEqual(db.get_last_seen_ts("0xabc", "acct_b"), 123)
            self.assertTrue(db.is_fill_seen("new-fill", account_name="acct_a"))
            self.assertFalse(db.is_fill_seen("new-fill", account_name="acct_b"))
        finally:
            db.close()

    def test_trade_and_position_extractors_prefer_market_slug_over_event_slug(self):
        market_slug = "mlb-bal-pit-2026-04-05-total-8pt5"
        event_slug = "mlb-bal-pit-2026-04-05"

        trade = extract_trade_fields(
            {
                "side": "BUY",
                "transactionHash": "0xtx",
                "timestamp": 1775411463,
                "conditionId": "0x75eb",
                "slug": market_slug,
                "eventSlug": event_slug,
                "asset": "tok-1",
                "price": 0.43,
                "size": 97.49,
                "usdcSize": 41.92,
                "outcomeIndex": 0,
            }
        )
        position = extract_position_fields(
            {
                "asset": "tok-1",
                "conditionId": "0x75eb",
                "slug": market_slug,
                "eventSlug": event_slug,
                "outcome": "Over",
                "size": 97.49,
                "avgPrice": 0.43,
                "currentValue": 41.92,
                "cashPnl": 0.0,
            }
        )

        self.assertIsNotNone(trade)
        self.assertIsNotNone(position)
        self.assertEqual(trade["slug"], market_slug)
        self.assertEqual(position["slug"], market_slug)

    def test_risk_manager_can_resolve_submarket_metadata_by_market_slug(self):
        cid = "0x75eb"
        market_slug = "mlb-bal-pit-2026-04-05-total-8pt5"

        def fake_http_get_json(session, url, params=None, **kwargs):
            params = params or {}
            if url.endswith("/markets") and params.get("conditionId") == cid:
                return [{"conditionId": "0xother", "slug": "wrong"}]
            if url.endswith("/markets") and params.get("slug") == market_slug:
                return [{"conditionId": cid, "slug": market_slug, "question": "Baltimore Orioles vs. Pittsburgh Pirates: O/U 8.5"}]
            raise AssertionError(f"unexpected request: {url} {params}")

        db = CopyTradeDB(str(self._db_path("risk_slug_lookup")))
        try:
            risk = RiskManager(requests.Session(), CopyTradeConfig(), db, account_name="acct")
            with patch("copytrade.risk.http_get_json", side_effect=fake_http_get_json):
                meta = risk._get_market_meta(cid, market_slug)
        finally:
            db.close()

        self.assertIsNotNone(meta)
        self.assertEqual(meta["slug"], market_slug)
        self.assertEqual(meta["conditionId"], cid)


if __name__ == "__main__":
    unittest.main()
