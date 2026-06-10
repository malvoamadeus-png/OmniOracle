import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.executor import OrderParams
from copytrade.monitor import LeaderTrade
from copytrade.risk import RiskManager
from copytrade.worker import AccountWorker


class _StaticMonitor:
    def __init__(self, trades):
        self._trades = list(trades)

    def poll_once(self):
        return list(self._trades)


class _NoExitMgr:
    def process_exits(self, new_trades, skip_verification=False):
        return []


class _ExitRecorder:
    def __init__(self):
        self.seen = []

    def process_exits(self, new_trades, skip_verification=False):
        self.seen = list(new_trades)
        return []


class _CaptureExecutor:
    def __init__(self, cfg: CopyTradeConfig):
        self.config = cfg
        self.calls = []

    def compute_order_params(self, leader_trade, db=None, account_name=None):
        usd = float(getattr(self.config, "fixed_usd_amount", 0.0) or 0.0)
        return OrderParams(
            token_id=leader_trade.token_id,
            side="BUY",
            price=leader_trade.price,
            size=usd / leader_trade.price,
            usd=usd,
            condition_id=leader_trade.condition_id,
            market_slug=leader_trade.market_slug,
            outcome=leader_trade.outcome,
        )

    def execute_order(self, params):
        self.calls.append(params)
        return SimpleNamespace(
            success=True,
            order_id="order-1",
            filled_price=params.price,
            filled_size=params.size,
            filled_usd=params.usd,
            exchange_status="matched",
            limit_price=params.price,
            error=None,
        )


class _RejectCryptoOnlyRisk:
    def check_crypto_only(self, leader_trade):
        return False, "crypto_only:timeframe_5m_not_allowed allowed=15m/4h slug=btc-updown-5m-1775476800"

    def check_all(self, leader_trade, our_usd):
        raise AssertionError("BUY risk check should not run after crypto-only prefilter rejects")


class _RiskShouldNotRun:
    def check_crypto_only(self, leader_trade):
        raise AssertionError("SELL path should not invoke crypto-only checks")

    def check_all(self, leader_trade, our_usd):
        raise AssertionError("SELL path should not invoke BUY risk checks")


class CryptoOnlyFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workers = []
        self.risks = []
        self.leader = "0xabc"

    def tearDown(self):
        for risk in self.risks:
            risk.db.close()
            risk.session.close()
        for worker in self.workers:
            worker.db.close()
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def _trade(
        self,
        *,
        fill_key: str,
        market_slug: str,
        side: str = "BUY",
        condition_id: str = "cond",
        ts_int: int = 1_700_000_000,
    ) -> LeaderTrade:
        return LeaderTrade(
            leader_address=self.leader,
            tx_hash=f"tx-{fill_key}",
            fill_key=fill_key,
            timestamp=str(ts_int),
            side=side,
            token_id="tok",
            condition_id=condition_id,
            price=0.5,
            size=2000.0,
            usd_amount=1000.0,
            outcome="YES",
            market_slug=market_slug,
            ts_int=ts_int,
        )

    def _make_worker(self, cfg: CopyTradeConfig, name: str) -> AccountWorker:
        db = CopyTradeDB(str(self._db_path(name)))
        worker = AccountWorker(
            SimpleNamespace(name=name, env_suffix=name.upper(), config=cfg),
            db,
            dry_run=True,
            once=True,
        )
        self.workers.append(worker)
        return worker

    def _make_risk(self, cfg: CopyTradeConfig, name: str) -> RiskManager:
        db = CopyTradeDB(str(self._db_path(name)))
        session = requests.Session()
        risk = RiskManager(session, cfg, db, account_name=name)
        self.risks.append(risk)
        return risk

    def test_global_crypto_only_allows_selected_timeframes_and_skips_others(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["15m", "4h"],
            settlement_days_max=0,
        )
        risk = self._make_risk(cfg, "global_crypto")

        ok_15m, reason_15m = risk.check_crypto_only(
            self._trade(fill_key="allowed-15m", market_slug="btc-updown-15m-1775476800")
        )
        ok_5m, reason_5m = risk.check_crypto_only(
            self._trade(fill_key="blocked-5m", market_slug="sol-updown-5m-1775476800")
        )
        ok_other, reason_other = risk.check_crypto_only(
            self._trade(fill_key="blocked-other", market_slug="nba-finals-2026")
        )

        self.assertTrue(ok_15m)
        self.assertEqual(reason_15m, "ok")
        self.assertFalse(ok_5m)
        self.assertIn("crypto_only:timeframe_5m_not_allowed", reason_5m)
        self.assertFalse(ok_other)
        self.assertIn("crypto_only:not_crypto_market", reason_other)

    def test_leader_override_timeframes_replace_global_selection(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["15m"],
            settlement_days_max=0,
            leader_overrides={
                self.leader: {
                    "crypto_only_enabled": True,
                    "crypto_allowed_timeframes": ["4h"],
                }
            },
        )
        risk = self._make_risk(cfg, "leader_override")

        ok_15m, reason_15m = risk.check_crypto_only(
            self._trade(fill_key="override-15m", market_slug="btc-updown-15m-1775476800")
        )
        ok_4h, reason_4h = risk.check_crypto_only(
            self._trade(fill_key="override-4h", market_slug="eth-updown-4h-1775476800")
        )

        self.assertFalse(ok_15m)
        self.assertIn("crypto_only:timeframe_15m_not_allowed", reason_15m)
        self.assertTrue(ok_4h)
        self.assertEqual(reason_4h, "ok")

    def test_global_crypto_only_can_allow_daily_and_weekly_categories(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["1d", "1w"],
            settlement_days_max=0,
        )
        risk = self._make_risk(cfg, "global_crypto_daily_weekly")

        ok_1d, reason_1d = risk.check_crypto_only(
            self._trade(fill_key="allowed-1d", market_slug="bitcoin-up-or-down-on-april-9-2026")
        )
        ok_1w, reason_1w = risk.check_crypto_only(
            self._trade(fill_key="allowed-1w", market_slug="bitcoin-above-on-april-9")
        )
        ok_1h, reason_1h = risk.check_crypto_only(
            self._trade(fill_key="blocked-1h", market_slug="btc-updown-1h-1775476800")
        )
        ok_spx, reason_spx = risk.check_crypto_only(
            self._trade(fill_key="blocked-spx", market_slug="spx-up-or-down-on-march-10-2026")
        )

        self.assertTrue(ok_1d)
        self.assertEqual(reason_1d, "ok")
        self.assertTrue(ok_1w)
        self.assertEqual(reason_1w, "ok")
        self.assertFalse(ok_1h)
        self.assertIn("crypto_only:timeframe_1h_not_allowed", reason_1h)
        self.assertFalse(ok_spx)
        self.assertIn("crypto_only:not_crypto_market", reason_spx)

    def test_global_crypto_only_accepts_hourly_event_slug_when_1h_allowed(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["1h"],
            settlement_days_max=0,
        )
        risk = self._make_risk(cfg, "global_crypto_hourly")

        ok_1h, reason_1h = risk.check_crypto_only(
            self._trade(
                fill_key="allowed-hourly",
                market_slug="bitcoin-up-or-down-april-9-2026-10am-et",
            )
        )
        ok_1d, reason_1d = risk.check_crypto_only(
            self._trade(
                fill_key="blocked-daily",
                market_slug="bitcoin-up-or-down-on-april-9-2026",
            )
        )

        self.assertTrue(ok_1h)
        self.assertEqual(reason_1h, "ok")
        self.assertFalse(ok_1d)
        self.assertIn("crypto_only:timeframe_1d_not_allowed", reason_1d)

    def test_sell_signal_bypasses_crypto_filter_and_reaches_exit_manager(self):
        worker = self._make_worker(
            CopyTradeConfig(
                leader_addresses=[self.leader],
                crypto_only_enabled=True,
                crypto_allowed_timeframes=["15m"],
            ),
            "sell_exit",
        )
        exit_recorder = _ExitRecorder()
        sell_trade = self._trade(
            fill_key="sell-only",
            market_slug="btc-updown-15m-1775476800",
            side="SELL",
        )

        worker._poll_cycle(
            _StaticMonitor([sell_trade]),
            _RiskShouldNotRun(),
            executor=SimpleNamespace(_client=None),
            exit_mgr=exit_recorder,
            cfg=worker.account.config,
        )

        self.assertEqual(exit_recorder.seen, [sell_trade])

    def test_disallowed_crypto_signal_is_skipped_before_order_sizing(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            maker_like_enabled=False,
        )
        worker = self._make_worker(cfg, "blocked_crypto")
        executor = _CaptureExecutor(cfg)

        worker._poll_cycle(
            _StaticMonitor([
                self._trade(fill_key="blocked", market_slug="btc-updown-5m-1775476800")
            ]),
            _RejectCryptoOnlyRisk(),
            executor,
            _NoExitMgr(),
            worker.account.config,
        )

        row = worker.db.conn.execute(
            "SELECT status, skip_reason FROM ct_trades WHERE leader_fill_key='blocked'"
        ).fetchone()
        audit = worker.db.conn.execute(
            "SELECT stage, reason FROM ct_signal_audit WHERE leader_fill_key='blocked'"
        ).fetchone()

        self.assertEqual(executor.calls, [])
        self.assertIsNone(row)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["stage"], "risk_rejected")
        self.assertIn("crypto_only:", audit["reason"])
        self.assertEqual(worker._reject_counts.get("crypto_only"), 1)

    def test_allowed_crypto_signal_reaches_regular_order_path(self):
        cfg = CopyTradeConfig(
            leader_addresses=[self.leader],
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["15m"],
            fixed_usd_amount=50,
            min_trade_size_usd=10,
            maker_like_enabled=False,
            settlement_days_max=0,
        )
        worker = self._make_worker(cfg, "allowed_crypto")
        risk = self._make_risk(cfg, "allowed_crypto")
        executor = _CaptureExecutor(cfg)

        worker._poll_cycle(
            _StaticMonitor([
                self._trade(fill_key="allowed", market_slug="btc-updown-15m-1775476800")
            ]),
            risk,
            executor,
            _NoExitMgr(),
            worker.account.config,
        )

        row = worker.db.conn.execute(
            "SELECT status, our_usd FROM ct_trades WHERE leader_fill_key='allowed'"
        ).fetchone()

        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(row["status"], "filled")
        self.assertAlmostEqual(row["our_usd"], 50.0)


if __name__ == "__main__":
    unittest.main()
