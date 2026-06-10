import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.aggregation import aggregate_trade_dicts_offline as aggregate_leader_trades
from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.executor import DryRunExecutor
from copytrade.monitor import LeaderTrade
from copytrade.risk import RiskManager
from copytrade.worker import AccountWorker


class ExecutionEpisodeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workers = []
        self.dbs = []
        self.sessions = []
        self.leader = "0xe1d6b51521bd4365769199f392f9818661bd907c"

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for db in self.dbs:
            db.close()
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def _cfg(self, **overrides) -> CopyTradeConfig:
        data = {
            "leader_addresses": [self.leader],
            "maker_like_enabled": True,
            "aggregation_mode": "execution_episode",
            "execution_episode_window_minutes": 20,
            "execution_episode_max_gap_minutes": 5,
            "execution_episode_price_band_abs": 0.03,
            "execution_episode_price_band_bps": 500.0,
            "execution_episode_min_fill_count": 2,
            "min_trade_size_usd": 100.0,
            "settlement_days_max": 0,
            "copy_mode": "fixed_usd",
            "fixed_usd_amount": 100.0,
            "pricing_mode": "original",
        }
        data.update(overrides)
        return CopyTradeConfig(**data)

    def _make_worker(self, cfg: CopyTradeConfig, name: str) -> AccountWorker:
        db = CopyTradeDB(str(self._db_path(name)))
        self.dbs.append(db)
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
        self.dbs.append(db)
        self.sessions.append(session)
        return RiskManager(session, cfg, db, account_name=name)

    def _trade(
        self,
        *,
        fill_key: str,
        price: float,
        usd: float,
        ts_int: int,
        tx_hash: Optional[str] = None,
        condition_id: str = "cond",
        token_id: str = "tok",
        outcome: str = "YES",
    ) -> LeaderTrade:
        return LeaderTrade(
            leader_address=self.leader,
            tx_hash=tx_hash or f"tx-{fill_key}",
            fill_key=fill_key,
            timestamp=str(ts_int),
            side="BUY",
            token_id=token_id,
            condition_id=condition_id,
            price=price,
            size=usd / price,
            usd_amount=usd,
            outcome=outcome,
            market_slug="btc-updown-4h-1775318400",
            ts_int=ts_int,
        )

    def test_execution_episode_emits_once_at_threshold_with_vwap_and_last_price_hint(self):
        cfg = self._cfg()
        worker = self._make_worker(cfg, "emit_once")

        signals = worker._prepare_copy_signals(
            [
                self._trade(fill_key="a", price=0.40, usd=60.0, ts_int=1_700_000_000),
                self._trade(fill_key="b", price=0.43, usd=50.0, ts_int=1_700_000_060),
                self._trade(fill_key="c", price=0.44, usd=30.0, ts_int=1_700_000_120),
            ],
            cfg,
        )

        self.assertEqual(len(signals), 1)
        agg = signals[0]
        expected_vwap = (60.0 + 50.0) / ((60.0 / 0.40) + (50.0 / 0.43))
        self.assertTrue(agg.is_maker_like_aggregated)
        self.assertEqual(agg.aggregation_kind, "execution_episode")
        self.assertEqual(agg.aggregation_source_count, 2)
        self.assertAlmostEqual(agg.price or 0.0, expected_vwap, places=9)
        self.assertAlmostEqual(agg.execution_price_hint or 0.0, 0.43, places=9)

    def test_execution_episode_same_tx_bypasses_price_band(self):
        cfg = self._cfg()
        worker = self._make_worker(cfg, "same_tx")

        signals = worker._prepare_copy_signals(
            [
                self._trade(fill_key="a", price=0.20, usd=60.0, ts_int=1_700_000_000, tx_hash="tx-same"),
                self._trade(fill_key="b", price=0.50, usd=50.0, ts_int=1_700_000_001, tx_hash="tx-same"),
            ],
            cfg,
        )

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].aggregation_source_count, 2)
        self.assertAlmostEqual(signals[0].execution_price_hint or 0.0, 0.50, places=9)

    def test_execution_episode_respects_gap_cut(self):
        cfg = self._cfg(execution_episode_max_gap_minutes=1)
        worker = self._make_worker(cfg, "gap_cut")

        signals = worker._prepare_copy_signals(
            [
                self._trade(fill_key="a", price=0.40, usd=60.0, ts_int=1_700_000_000),
                self._trade(fill_key="b", price=0.42, usd=60.0, ts_int=1_700_000_061),
            ],
            cfg,
        )

        self.assertEqual(signals, [])

    def test_executor_uses_execution_price_hint_and_disables_passive_mode(self):
        cfg = self._cfg(fixed_usd_amount=90.0)
        executor = DryRunExecutor(cfg)

        strict_trade = self._trade(fill_key="strict", price=0.40, usd=150.0, ts_int=1_700_000_000)
        strict_trade.is_maker_like_aggregated = True
        strict_trade.aggregation_kind = "strict_price"
        strict_params = executor.compute_order_params(strict_trade)

        exec_trade = self._trade(fill_key="episode", price=0.41, usd=150.0, ts_int=1_700_000_000)
        exec_trade.is_maker_like_aggregated = True
        exec_trade.aggregation_kind = "execution_episode"
        exec_trade.execution_price_hint = 0.45
        exec_params = executor.compute_order_params(exec_trade)

        self.assertIsNotNone(strict_params)
        self.assertTrue(strict_params.passive_price_mode)
        self.assertAlmostEqual(strict_params.price, 0.40, places=9)

        self.assertIsNotNone(exec_params)
        self.assertFalse(exec_params.passive_price_mode)
        self.assertAlmostEqual(exec_params.price, 0.45, places=9)
        self.assertAlmostEqual(exec_params.size, 90.0 / 0.45, places=9)

    def test_risk_checks_use_execution_price_hint(self):
        cfg = self._cfg(max_price=0.80)
        risk = self._make_risk(cfg, "risk_hint")
        trade = self._trade(fill_key="risk", price=0.75, usd=500.0, ts_int=1_700_000_000)
        trade.execution_price_hint = 0.85
        trade.is_maker_like_aggregated = True
        trade.aggregation_kind = "execution_episode"

        ok, reason = risk.check_all(trade, 100.0)

        self.assertFalse(ok)
        self.assertIn("price 0.8500 > max 0.8", reason)

    def test_analytics_strict_price_keeps_variable_price_fragments_separate(self):
        trades = [
            {
                "leader_address": self.leader,
                "tx_hash": "tx-a",
                "timestamp_utc": "1700000000",
                "ts_epoch": 1_700_000_000,
                "side": "BUY",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "btc-updown-4h-1775318400",
                "outcome": "YES",
                "price": 0.40,
                "size": 150.0,
                "usd": 60.0,
            },
            {
                "leader_address": self.leader,
                "tx_hash": "tx-b",
                "timestamp_utc": "1700000060",
                "ts_epoch": 1_700_000_060,
                "side": "BUY",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "btc-updown-4h-1775318400",
                "outcome": "YES",
                "price": 0.43,
                "size": 50.0 / 0.43,
                "usd": 50.0,
            },
        ]

        strict_rows = aggregate_leader_trades(
            trades,
            min_trade_size_usd=100.0,
            enabled=True,
            score_threshold=0.0,
            aggregation_mode="strict_price",
        )
        episode_rows = aggregate_leader_trades(
            trades,
            min_trade_size_usd=100.0,
            enabled=True,
            aggregation_mode="execution_episode",
            execution_episode_window_minutes=20.0,
            execution_episode_max_gap_minutes=5.0,
            execution_episode_price_band_abs=0.03,
            execution_episode_price_band_bps=500.0,
            execution_episode_min_fill_count=2,
        )

        self.assertEqual(len(strict_rows), 2)
        self.assertEqual(len(episode_rows), 1)
        self.assertEqual(episode_rows[0]["_aggregation_kind"], "execution_episode")
        self.assertEqual(episode_rows[0]["_source_count"], 2)
        self.assertAlmostEqual(episode_rows[0]["_execution_price_hint"], 0.43, places=9)


if __name__ == "__main__":
    unittest.main()
