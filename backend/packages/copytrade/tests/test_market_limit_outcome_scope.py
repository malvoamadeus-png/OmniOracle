import tempfile
import unittest
from pathlib import Path

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.monitor import LeaderTrade
from copytrade.risk import RiskManager


class MarketLimitOutcomeScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def _trade(
        self,
        *,
        leader: str = "0xabc",
        outcome: str = "YES",
        token_id: str = "tok-shared",
    ) -> LeaderTrade:
        return LeaderTrade(
            leader_address=leader,
            tx_hash=f"tx-{leader}-{outcome}",
            fill_key=f"fill-{leader}-{outcome}",
            timestamp="1700000000",
            side="BUY",
            token_id=token_id,
            condition_id="cond-1",
            price=0.5,
            size=200.0,
            usd_amount=100.0,
            outcome=outcome,
            market_slug="market-1",
            ts_int=1_700_000_000,
        )

    def _insert_buy_attempt(
        self,
        db: CopyTradeDB,
        *,
        account_name: str = "acct",
        leader: str = "0xabc",
        outcome: str = "YES",
        token_id: str = "tok-shared",
        status: str = "filled",
    ) -> None:
        db.insert_trade(
            {
                "account_name": account_name,
                "leader_address": leader,
                "leader_tx_hash": f"leader-tx-{leader}-{outcome}",
                "leader_fill_key": f"leader-fill-{leader}-{outcome}-{status}",
                "leader_side": "BUY",
                "leader_price": 0.5,
                "leader_size": 200.0,
                "leader_usd": 100.0,
                "our_order_id": f"our-order-{leader}-{outcome}-{status}",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 10.0,
                "our_usd": 5.0,
                "token_id": token_id,
                "condition_id": "cond-1",
                "market_slug": "market-1",
                "outcome": outcome,
                "status": status,
            }
        )

    def test_global_once_is_scoped_by_token(self):
        db = CopyTradeDB(str(self._db_path("global_once")))
        try:
            self._insert_buy_attempt(db, leader="0xaaa", outcome="YES", token_id="tok-shared")
            risk = RiskManager(
                requests.Session(),
                CopyTradeConfig(
                    leader_addresses=["0xabc"],
                    market_limit_mode="global_once",
                    settlement_days_max=0,
                    min_trade_size_usd=10,
                ),
                db,
                account_name="acct",
            )

            ok_same_token, reason_same_token = risk.check_all(
                self._trade(leader="0xabc", outcome="NO", token_id="tok-shared"),
                50.0,
            )
            ok_other_token, reason_other_token = risk.check_all(
                self._trade(leader="0xabc", outcome="YES", token_id="tok-other"),
                50.0,
            )
        finally:
            db.close()

        self.assertFalse(ok_same_token)
        self.assertIn("global_once", reason_same_token)
        self.assertTrue(ok_other_token, reason_other_token)

    def test_per_address_once_is_scoped_by_token(self):
        db = CopyTradeDB(str(self._db_path("per_address_once")))
        try:
            self._insert_buy_attempt(db, leader="0xabc", outcome="YES", token_id="tok-shared")
            risk = RiskManager(
                requests.Session(),
                CopyTradeConfig(
                    leader_addresses=["0xabc"],
                    market_limit_mode="per_address_once",
                    settlement_days_max=0,
                    min_trade_size_usd=10,
                ),
                db,
                account_name="acct",
            )

            ok_same_token, reason_same_token = risk.check_all(
                self._trade(outcome="NO", token_id="tok-shared"),
                50.0,
            )
            ok_other_token, reason_other_token = risk.check_all(
                self._trade(outcome="YES", token_id="tok-other"),
                50.0,
            )
        finally:
            db.close()

        self.assertFalse(ok_same_token)
        self.assertIn("per_address_once", reason_same_token)
        self.assertTrue(ok_other_token, reason_other_token)

    def test_max_entries_per_market_counts_per_token(self):
        db = CopyTradeDB(str(self._db_path("max_entries")))
        try:
            self._insert_buy_attempt(db, leader="0xabc", outcome="YES", token_id="tok-shared")
            risk = RiskManager(
                requests.Session(),
                CopyTradeConfig(
                    leader_addresses=["0xabc"],
                    market_limit_mode="unlimited",
                    max_entries_per_market=1,
                    settlement_days_max=0,
                    min_trade_size_usd=10,
                ),
                db,
                account_name="acct",
            )

            ok_same_token, reason_same_token = risk.check_all(
                self._trade(outcome="NO", token_id="tok-shared"),
                50.0,
            )
            ok_other_token, reason_other_token = risk.check_all(
                self._trade(outcome="YES", token_id="tok-other"),
                50.0,
            )
        finally:
            db.close()

        self.assertFalse(ok_same_token)
        self.assertIn("max_entries", reason_same_token)
        self.assertTrue(ok_other_token, reason_other_token)


if __name__ == "__main__":
    unittest.main()
