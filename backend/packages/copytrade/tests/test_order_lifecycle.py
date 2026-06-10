import sqlite3
import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.executor import OrderExecutor, OrderParams, OrderResult
from copytrade.exit_manager import ExitManager
from copytrade.monitor import LeaderTrade
from copytrade.risk import RiskManager
from copytrade.worker import AccountWorker


class _StaticMonitor:
    def __init__(self, trades):
        self._trades = list(trades)

    def poll_once(self):
        return list(self._trades)


class _AllowRisk:
    def check_all(self, leader_trade, our_usd):
        return True, "ok"


class _NoExitMgr:
    def process_exits(self, new_trades, skip_verification=False):
        return []

    def verify_recent_order_state(self, *, source="rest"):
        return {"buy_fill_count": 0, "updated": 0}

    def register_entry_fill(self, *args, **kwargs):
        return None


class _SubmittedExecutor:
    def __init__(self, *, order_status: str, matched_size: float = 0.0, matched_price: float = 0.5):
        self._order_status = order_status
        self._matched_size = matched_size
        self._matched_price = matched_price
        self._client = SimpleNamespace(get_order=self._get_order)

    def compute_order_params(self, leader_trade, db=None, account_name=None):
        return OrderParams(
            token_id=leader_trade.token_id,
            side="BUY",
            price=leader_trade.price,
            size=100.0,
            usd=50.0,
            condition_id=leader_trade.condition_id,
            market_slug=leader_trade.market_slug,
            outcome=leader_trade.outcome,
        )

    def execute_order(self, params):
        return OrderResult(
            success=True,
            order_id="order-1",
            limit_price=params.price,
            exchange_status="submitted",
        )

    def _get_order(self, order_id):
        payload = {"status": self._order_status}
        if self._matched_size:
            payload["size_matched"] = str(self._matched_size)
            payload["price"] = str(self._matched_price)
            payload["original_size"] = "100"
        return payload


class _SubmittedExitExecutor:
    def __init__(self, *, order_status: str, matched_size: float = 0.0, matched_price: float = 0.6):
        self._order_status = order_status
        self._matched_size = matched_size
        self._matched_price = matched_price
        self._client = SimpleNamespace(get_order=self._get_order)

    def execute_order(self, params):
        return OrderResult(
            success=True,
            order_id="exit-order-1",
            limit_price=params.price,
            exchange_status="submitted",
        )

    def _get_order(self, order_id):
        payload = {"status": self._order_status}
        if self._matched_size:
            payload["size_matched"] = str(self._matched_size)
            payload["price"] = str(self._matched_price)
            payload["original_size"] = str(self._matched_size)
        return payload


class _ScriptedBuyExecutor:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.compute_calls = 0
        self.execute_calls = 0

    def compute_order_params(self, leader_trade, db=None, account_name=None):
        self.compute_calls += 1
        return OrderParams(
            token_id=leader_trade.token_id,
            side="BUY",
            price=leader_trade.price,
            size=100.0,
            usd=50.0,
            condition_id=leader_trade.condition_id,
            market_slug=leader_trade.market_slug,
            outcome=leader_trade.outcome,
        )

    def execute_order(self, params):
        self.execute_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "balance":
            return OrderResult(
                success=False,
                error="insufficient_clob_balance asset=pUSD collateral side=BUY purpose=copytrade",
                error_code="balance_allowance",
                limit_price=params.price,
                submitted_size=params.size,
                retryable=True,
            )
        return OrderResult(
            success=True,
            order_id=f"order-{self.execute_calls}",
            limit_price=params.price,
            exchange_status="submitted",
        )


class OrderLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.db.close()
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def _trade(self, *, fill_key: str = "fill-1", ts: int = 1_700_000_000, leader: str = "0xabc") -> LeaderTrade:
        return LeaderTrade(
            leader_address=leader,
            tx_hash=f"tx-{fill_key}",
            fill_key=fill_key,
            timestamp=str(ts),
            side="BUY",
            token_id="tok",
            condition_id="cond",
            price=0.5,
            size=2000.0,
            usd_amount=1000.0,
            outcome="YES",
            market_slug="mkt",
            ts_int=ts,
        )

    def _make_worker(self, name: str = "acct") -> AccountWorker:
        account = SimpleNamespace(name=name, env_suffix=name.upper(), config=CopyTradeConfig(leader_addresses=["0xabc"]))
        worker = AccountWorker(account, CopyTradeDB(str(self._db_path(name))), dry_run=True, once=True)
        self.workers.append(worker)
        return worker

    def _claim_attempt(self, worker: AccountWorker, trade: LeaderTrade, account_name: str) -> int:
        attempt_id = worker.db.claim_leader_fill(
            {
                "leader_address": trade.leader_address,
                "leader_tx_hash": trade.tx_hash,
                "leader_fill_key": trade.fill_key,
                "leader_side": trade.side,
                "leader_price": trade.price,
                "leader_size": trade.size,
                "leader_usd": trade.usd_amount,
                "token_id": trade.token_id,
                "condition_id": trade.condition_id,
                "market_slug": trade.market_slug,
                "outcome": trade.outcome,
            },
            account_name=account_name,
        )
        self.assertIsNotNone(attempt_id)
        trade.signal_attempt_id = int(attempt_id)
        return int(attempt_id)

    def test_live_submission_stays_submitted_until_exchange_fill(self):
        worker = self._make_worker("live_submit")
        monitor = _StaticMonitor([self._trade()])
        executor = _SubmittedExecutor(order_status="live")
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            worker.db,
            executor,
            account_name="live_submit",
        )

        worker._poll_cycle(monitor, _AllowRisk(), executor, mgr, worker.account.config)

        row = worker.db.conn.execute(
            "SELECT status, exchange_order_status, our_size, our_usd FROM ct_trades WHERE our_order_id='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("submitted", "live", 0.0, 0.0))
        self.assertEqual(worker.db.get_daily_spend(account_name="live_submit"), 0.0)

    def test_partial_fill_reconciles_core_position_fields(self):
        worker = self._make_worker("partial_fill")
        monitor = _StaticMonitor([self._trade()])
        executor = _SubmittedExecutor(order_status="expired", matched_size=10.0, matched_price=0.5)
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            worker.db,
            executor,
            account_name="partial_fill",
        )

        worker._poll_cycle(monitor, _AllowRisk(), executor, mgr, worker.account.config)

        row = worker.db.conn.execute(
            "SELECT status, exchange_order_status, our_size, our_usd, partial_fill_status "
            "FROM ct_trades WHERE our_order_id='order-1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("partially_filled", "expired", 10.0, 5.0, "partial"))
        self.assertEqual(worker.db.get_daily_spend(account_name="partial_fill"), 5.0)
        self.assertEqual(len(worker.db.get_open_trades(token_id="tok", account_name="partial_fill")), 1)
        self.assertAlmostEqual(worker.db.get_position_usd("cond", account_name="partial_fill"), 5.0)
        self.assertTrue(worker.db.has_filled_buy_for_market("cond", account_name="partial_fill"))
        self.assertEqual(
            worker.db.count_buy_entries_for_token_by_leader("tok", "0xabc", account_name="partial_fill"),
            1,
        )
        self.assertEqual(worker.db.get_status_summary(account_name="partial_fill")["total_filled"], 1)

    def test_reconcile_entry_fill_uses_requested_avg_when_fill_price_missing(self):
        db = CopyTradeDB(str(self._db_path("entry_reconcile")))
        self.workers.append(SimpleNamespace(db=db))
        trade_id = db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-entry",
                "leader_fill_key": "fill-entry",
                "leader_side": "BUY",
                "our_order_id": "order-1",
                "our_side": "BUY",
                "status": "submitted",
                "requested_price": 0.31,
                "requested_size": 161.29032258064515,
                "requested_usd": 50.0,
                "our_limit_price": 0.95,
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )

        recon = db.reconcile_order_state(
            "order-1",
            account_name="acct",
            exchange_order_status="matched",
            matched_size=161.29,
            fill_price=None,
        )

        row = db.conn.execute(
            "SELECT our_price, our_usd, filled_size_actual, filled_usd_actual, our_filled_price "
            "FROM ct_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        self.assertTrue(recon["updated"])
        self.assertAlmostEqual(float(row["filled_size_actual"]), 161.29, places=6)
        self.assertAlmostEqual(float(row["our_price"]), 0.31, places=6)
        self.assertAlmostEqual(float(row["our_filled_price"]), 0.31, places=6)
        self.assertAlmostEqual(float(row["our_usd"]), 49.9999, places=4)
        self.assertAlmostEqual(float(row["filled_usd_actual"]), 49.9999, places=4)

    def test_buy_size_is_capped_to_budget_after_chase_guard(self):
        params = OrderParams(
            token_id="tok",
            side="BUY",
            price=0.27,
            size=18.5185185185,
            usd=5.0,
            condition_id="cond",
            market_slug="mkt",
            outcome="YES",
            aggressive_price_chase_cap_abs=0.01,
            aggressive_price_chase_cap_bps=300.0,
        )

        guarded_price = OrderExecutor._apply_aggressive_chase_guard(params, 0.99)
        submitted_size = OrderExecutor._submitted_order_size(params, guarded_price)

        self.assertAlmostEqual(guarded_price, 0.2781, places=6)
        self.assertAlmostEqual(submitted_size, 17.97, places=2)
        self.assertLessEqual(submitted_size * guarded_price, 5.0 + 1e-9)

    def test_rest_entry_verification_ignores_order_limit_price_without_avg_fill(self):
        db = CopyTradeDB(str(self._db_path("entry_verify")))
        self.workers.append(SimpleNamespace(db=db))
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-entry-verify",
                "leader_fill_key": "fill-entry-verify",
                "leader_side": "BUY",
                "our_order_id": "order-1",
                "our_side": "BUY",
                "status": "submitted",
                "exchange_order_status": "submitted",
                "requested_price": 0.31,
                "requested_size": 161.29032258064515,
                "requested_usd": 50.0,
                "our_limit_price": 0.95,
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        executor = SimpleNamespace(
            _client=SimpleNamespace(
                get_order=lambda order_id: {
                    "status": "matched",
                    "size_matched": "161.29",
                    "price": "0.95",
                    "original_size": "161.29",
                }
            )
        )
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            db,
            executor,
            account_name="acct",
        )

        summary = mgr.verify_recent_order_state(source="rest")

        row = db.conn.execute(
            "SELECT status, exchange_order_status, our_price, our_usd, filled_usd_actual "
            "FROM ct_trades WHERE our_order_id='order-1'"
        ).fetchone()
        self.assertEqual(summary["buy_fill_count"], 1)
        self.assertEqual(tuple(row[:2]), ("filled", "matched"))
        self.assertAlmostEqual(float(row["our_price"]), 0.31, places=6)
        self.assertAlmostEqual(float(row["our_usd"]), 49.9999, places=4)
        self.assertAlmostEqual(float(row["filled_usd_actual"]), 49.9999, places=4)

    def test_rest_entry_snapshot_below_previous_cumulative_does_not_reduce_fill(self):
        db = CopyTradeDB(str(self._db_path("entry_snapshot_floor")))
        self.workers.append(SimpleNamespace(db=db))
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-entry-floor",
                "leader_fill_key": "fill-entry-floor",
                "leader_side": "BUY",
                "our_order_id": "order-1",
                "our_side": "BUY",
                "status": "partially_filled",
                "exchange_order_status": "live",
                "requested_price": 0.5,
                "requested_size": 20.0,
                "requested_usd": 10.0,
                "our_price": 0.5,
                "our_size": 10.0,
                "our_usd": 5.0,
                "filled_size_actual": 10.0,
                "filled_usd_actual": 5.0,
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        executor = SimpleNamespace(
            _client=SimpleNamespace(
                get_order=lambda order_id: {
                    "status": "live",
                    "size_matched": "5.0",
                    "avg_price": "0.5",
                    "original_size": "20.0",
                }
            )
        )
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            db,
            executor,
            account_name="acct",
        )

        summary = mgr.verify_recent_order_state(source="rest")

        row = db.conn.execute(
            "SELECT status, exchange_order_status, our_size, our_usd, filled_size_actual "
            "FROM ct_trades WHERE our_order_id='order-1'"
        ).fetchone()
        self.assertEqual(summary["buy_fill_count"], 0)
        self.assertEqual(tuple(row[:2]), ("partially_filled", "live"))
        self.assertAlmostEqual(float(row["our_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(row["our_usd"]), 5.0, places=6)
        self.assertAlmostEqual(float(row["filled_size_actual"]), 10.0, places=6)

    def test_verification_only_updates_current_account(self):
        db = CopyTradeDB(str(self._db_path("scoped_verify")))
        worker_a = AccountWorker(
            SimpleNamespace(name="acct_a", env_suffix="ACCT_A", config=CopyTradeConfig(leader_addresses=["0xabc"])),
            db,
            dry_run=True,
            once=True,
        )
        worker_b = AccountWorker(
            SimpleNamespace(name="acct_b", env_suffix="ACCT_B", config=CopyTradeConfig(leader_addresses=["0xabc"])),
            db,
            dry_run=True,
            once=True,
        )
        self.workers.extend([worker_a, worker_b])

        db.insert_trade(
            {
                "account_name": "acct_a",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-a",
                "leader_fill_key": "fill-a",
                "leader_side": "BUY",
                "our_order_id": "order-a",
                "our_side": "BUY",
                "status": "submitted",
                "requested_price": 0.5,
                "requested_size": 100.0,
                "requested_usd": 50.0,
                "exchange_order_status": "submitted",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
            }
        )
        db.insert_trade(
            {
                "account_name": "acct_b",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-b",
                "leader_fill_key": "fill-b",
                "leader_side": "BUY",
                "our_order_id": "order-b",
                "our_side": "BUY",
                "status": "submitted",
                "requested_price": 0.5,
                "requested_size": 100.0,
                "requested_usd": 50.0,
                "exchange_order_status": "submitted",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
            }
        )

        client = SimpleNamespace(get_order=lambda order_id: {"status": "expired"})
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            db,
            SimpleNamespace(_client=client),
            account_name="acct_a",
        )
        worker_a._verify_recent_orders(SimpleNamespace(_client=client), mgr)

        rows = db.conn.execute(
            "SELECT account_name, status, exchange_order_status FROM ct_trades ORDER BY account_name"
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("acct_a", "expired", "expired"), ("acct_b", "submitted", "submitted")])

    def test_compute_error_keeps_a_signal_attempt_record(self):
        worker = self._make_worker("compute_fail")
        trade = self._trade(fill_key="fill-compute")
        attempt_id = worker.db.claim_leader_fill(
            {
                "leader_address": trade.leader_address,
                "leader_tx_hash": trade.tx_hash,
                "leader_fill_key": trade.fill_key,
                "leader_side": trade.side,
                "leader_price": trade.price,
                "leader_size": trade.size,
                "leader_usd": trade.usd_amount,
                "token_id": trade.token_id,
                "condition_id": trade.condition_id,
                "market_slug": trade.market_slug,
                "outcome": trade.outcome,
            },
            account_name="compute_fail",
        )
        trade.signal_attempt_id = attempt_id

        class _BoomExecutor:
            def compute_order_params(self, *args, **kwargs):
                raise RuntimeError("boom")

        worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), _BoomExecutor(), _NoExitMgr(), worker.account.config)

        row = worker.db.conn.execute(
            "SELECT status, reason FROM ct_signal_attempts WHERE leader_fill_key='fill-compute'"
        ).fetchone()
        self.assertEqual(row["status"], "failed_internal")
        self.assertIn("compute_params_error", row["reason"])

    def test_buy_below_exchange_min_size_is_skipped_without_trade_entry(self):
        worker = self._make_worker("min_size_skip")
        trade = self._trade(fill_key="fill-min-size")
        attempt_id = worker.db.claim_leader_fill(
            {
                "leader_address": trade.leader_address,
                "leader_tx_hash": trade.tx_hash,
                "leader_fill_key": trade.fill_key,
                "leader_side": trade.side,
                "leader_price": trade.price,
                "leader_size": trade.size,
                "leader_usd": trade.usd_amount,
                "token_id": trade.token_id,
                "condition_id": trade.condition_id,
                "market_slug": trade.market_slug,
                "outcome": trade.outcome,
            },
            account_name="min_size_skip",
        )
        trade.signal_attempt_id = attempt_id

        class _SubMinExecutor:
            def compute_order_params(self, leader_trade, db=None, account_name=None):
                return OrderParams(
                    token_id=leader_trade.token_id,
                    side="BUY",
                    price=leader_trade.price,
                    size=2.0,
                    usd=1.0,
                    condition_id=leader_trade.condition_id,
                    market_slug=leader_trade.market_slug,
                    outcome=leader_trade.outcome,
                )

            def execute_order(self, params):
                return OrderResult(
                    success=False,
                    error="clob_min_order_size side=BUY purpose=copytrade size=2.000000 min=5.000000 token_id=tok",
                    error_code="min_order_size",
                    limit_price=0.5,
                    submitted_size=2.0,
                    min_order_size=5.0,
                )

        worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), _SubMinExecutor(), _NoExitMgr(), worker.account.config)

        trade_row = worker.db.conn.execute(
            "SELECT id FROM ct_trades WHERE leader_fill_key='fill-min-size'"
        ).fetchone()
        attempt_row = worker.db.conn.execute(
            "SELECT status, reason FROM ct_signal_attempts WHERE leader_fill_key='fill-min-size'"
        ).fetchone()
        audit_row = worker.db.conn.execute(
            "SELECT stage, reason, details_json FROM ct_signal_audit WHERE leader_fill_key='fill-min-size'"
        ).fetchone()

        self.assertIsNone(trade_row)
        self.assertEqual(attempt_row["status"], "skipped")
        self.assertIn("clob_min_order_size", attempt_row["reason"])
        self.assertEqual(audit_row["stage"], "min_size_skipped")
        self.assertIn('"min_order_size": 5.0', audit_row["details_json"])
        self.assertEqual(worker._hourly_stats["buy_fail"], 0)
        self.assertFalse(worker.db.has_buy_attempt_for_token("tok", account_name="min_size_skip"))

    def test_signal_attempt_retry_columns_are_migrated_for_old_db(self):
        path = self._db_path("retry_migration")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE ct_signal_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT 'default',
                leader_address TEXT NOT NULL,
                leader_tx_hash TEXT,
                leader_fill_key TEXT NOT NULL,
                leader_side TEXT,
                leader_price REAL,
                leader_size REAL,
                leader_usd REAL,
                token_id TEXT,
                condition_id TEXT,
                market_slug TEXT,
                outcome TEXT,
                status TEXT NOT NULL DEFAULT 'detected',
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(leader_fill_key, leader_address, account_name)
            )
            """
        )
        conn.commit()
        conn.close()

        db = CopyTradeDB(str(path))
        self.workers.append(SimpleNamespace(db=db))
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(ct_signal_attempts)").fetchall()}
        self.assertTrue({"retry_count", "retry_after", "expires_at", "last_error_code", "source"}.issubset(cols))

    def test_maintenance_pending_attempts_are_account_scoped(self):
        db = CopyTradeDB(str(self._db_path("retry_scope")))
        self.workers.append(SimpleNamespace(db=db))
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=10)).isoformat()
        for account_name in ("acct_a", "acct_b"):
            attempt_id = db.claim_leader_fill(
                {
                    "leader_address": "0xabc",
                    "leader_tx_hash": f"tx-{account_name}",
                    "leader_fill_key": f"fill-{account_name}",
                    "leader_side": "BUY",
                    "leader_price": 0.5,
                    "leader_size": 100.0,
                    "leader_usd": 50.0,
                    "token_id": "tok",
                    "condition_id": "cond",
                    "market_slug": "mkt",
                    "outcome": "YES",
                },
                account_name=account_name,
            )
            db.mark_signal_attempt_maintenance_pending(
                int(attempt_id),
                reason="balance",
                error_code="balance_allowance",
                expires_at=expires_at,
            )

        released = db.release_maintenance_signal_attempts(account_name="acct_a")

        self.assertEqual(released, 1)
        self.assertEqual(len(db.get_due_maintenance_signal_attempts(account_name="acct_a")), 1)
        self.assertEqual(len(db.get_due_maintenance_signal_attempts(account_name="acct_b")), 0)

    def test_balance_allowance_failure_enters_maintenance_pending_without_trade_entry(self):
        worker = self._make_worker("balance_pending")
        trade = self._trade(
            fill_key="fill-balance-pending",
            ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self._claim_attempt(worker, trade, "balance_pending")
        executor = _ScriptedBuyExecutor(["balance"])

        with patch.object(worker, "_trigger_emergency_redeem") as trigger:
            worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)

        trade_row = worker.db.conn.execute(
            "SELECT id FROM ct_trades WHERE leader_fill_key='fill-balance-pending'"
        ).fetchone()
        attempt_row = worker.db.conn.execute(
            "SELECT status, retry_count, retry_after, expires_at, last_error_code "
            "FROM ct_signal_attempts WHERE leader_fill_key='fill-balance-pending'"
        ).fetchone()
        audit_row = worker.db.conn.execute(
            "SELECT stage, details_json FROM ct_signal_audit WHERE leader_fill_key='fill-balance-pending'"
        ).fetchone()

        self.assertIsNone(trade_row)
        self.assertEqual(attempt_row["status"], "maintenance_pending")
        self.assertEqual(attempt_row["retry_count"], 0)
        self.assertIsNone(attempt_row["retry_after"])
        self.assertIsNotNone(attempt_row["expires_at"])
        self.assertEqual(attempt_row["last_error_code"], "balance_allowance")
        self.assertEqual(audit_row["stage"], "maintenance_pending")
        self.assertIn('"error_code": "balance_allowance"', audit_row["details_json"])
        self.assertEqual(worker._hourly_stats["buy_fail"], 0)
        self.assertFalse(worker.db.has_buy_attempt_for_token("tok", account_name="balance_pending"))
        trigger.assert_called_once_with("BALANCE_PENDING")

    def test_balance_allowance_failure_does_not_print_per_order_console_line(self):
        worker = self._make_worker("balance_quiet")
        trade = self._trade(
            fill_key="fill-balance-quiet",
            ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self._claim_attempt(worker, trade, "balance_quiet")
        executor = _ScriptedBuyExecutor(["balance"])
        stderr = io.StringIO()

        with patch.object(worker, "_trigger_emergency_redeem"), patch("sys.stderr", stderr):
            worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)

        self.assertNotIn("[order_submit] failed", stderr.getvalue())
        self.assertEqual(worker._hourly_stats.get("balance_pending"), 1)

    def test_emergency_redeem_running_records_event_without_console_spam(self):
        worker = self._make_worker("redeem_running")
        stderr = io.StringIO()
        proc = SimpleNamespace(poll=lambda: None)
        worker._redeem_proc = proc

        with patch("sys.stderr", stderr):
            worker._trigger_emergency_redeem("REDEEM_RUNNING")

        self.assertEqual(stderr.getvalue(), "")
        row = worker.db.conn.execute(
            "SELECT event_type, message FROM ct_runtime_events WHERE account_name='redeem_running'"
        ).fetchone()
        self.assertEqual(tuple(row), ("maintenance_redeem_trigger_skipped", "redeem already running"))

    def test_redeem_timeout_logs_error_and_records_runtime_event(self):
        worker = self._make_worker("redeem_timeout")

        class Proc:
            def poll(self):
                return None

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                return None

        proc = Proc()
        worker._redeem_proc = proc
        worker._redeem_start_ts = 100.0
        stderr = io.StringIO()

        with (
            patch("copytrade.worker.time.time", return_value=500.0),
            patch("copytrade.worker._redeem_timeout_s", return_value=300),
            patch("sys.stderr", stderr),
        ):
            worker._maybe_run_redeem(worker.account.config, worker.account.env_suffix)

        self.assertIn("[redeem] 子进程超时", stderr.getvalue())
        row = worker.db.conn.execute(
            "SELECT event_type, severity, message FROM ct_runtime_events WHERE account_name='redeem_timeout'"
        ).fetchone()
        self.assertEqual(row["event_type"], "maintenance_redeem_timeout")
        self.assertEqual(row["severity"], "error")
        self.assertIn("redeem timeout after 400s", row["message"])

    def test_released_maintenance_retry_resubmits_original_signal_once(self):
        worker = self._make_worker("retry_success")
        trade = self._trade(
            fill_key="fill-retry-success",
            ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self._claim_attempt(worker, trade, "retry_success")
        executor = _ScriptedBuyExecutor(["balance", "success"])

        with patch.object(worker, "_trigger_emergency_redeem"):
            worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)
        worker.db.release_maintenance_signal_attempts(account_name="retry_success")
        worker._poll_cycle(_StaticMonitor([]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)

        trades = worker.db.conn.execute(
            "SELECT status, our_order_id FROM ct_trades WHERE leader_fill_key='fill-retry-success'"
        ).fetchall()
        attempt_row = worker.db.conn.execute(
            "SELECT status, retry_count, reason FROM ct_signal_attempts WHERE leader_fill_key='fill-retry-success'"
        ).fetchone()
        stages = [
            row["stage"]
            for row in worker.db.conn.execute(
                "SELECT stage FROM ct_signal_audit WHERE leader_fill_key='fill-retry-success' ORDER BY id"
            ).fetchall()
        ]

        self.assertEqual(len(trades), 1)
        self.assertEqual(tuple(trades[0]), ("submitted", "order-2"))
        self.assertEqual(attempt_row["status"], "submitted")
        self.assertEqual(attempt_row["retry_count"], 1)
        self.assertEqual(attempt_row["reason"], "maintenance_retry_submitted")
        self.assertEqual(stages, ["maintenance_pending", "maintenance_retry_submitted"])
        self.assertEqual(executor.compute_calls, 2)
        self.assertEqual(executor.execute_calls, 2)

    def test_maintenance_retry_failure_is_final_and_counts_one_buy_failure(self):
        worker = self._make_worker("retry_failed")
        trade = self._trade(
            fill_key="fill-retry-failed",
            ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self._claim_attempt(worker, trade, "retry_failed")
        executor = _ScriptedBuyExecutor(["balance", "balance"])

        with patch.object(worker, "_trigger_emergency_redeem") as trigger:
            worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)
        worker.db.release_maintenance_signal_attempts(account_name="retry_failed")
        worker._poll_cycle(_StaticMonitor([]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)

        attempt_row = worker.db.conn.execute(
            "SELECT status, retry_count, last_error_code FROM ct_signal_attempts WHERE leader_fill_key='fill-retry-failed'"
        ).fetchone()
        stages = [
            row["stage"]
            for row in worker.db.conn.execute(
                "SELECT stage FROM ct_signal_audit WHERE leader_fill_key='fill-retry-failed' ORDER BY id"
            ).fetchall()
        ]

        self.assertEqual(attempt_row["status"], "order_failed")
        self.assertEqual(attempt_row["retry_count"], 1)
        self.assertEqual(attempt_row["last_error_code"], "balance_allowance")
        self.assertEqual(stages, ["maintenance_pending", "maintenance_retry_failed"])
        self.assertEqual(worker._hourly_stats["buy_fail"], 1)
        self.assertEqual(executor.execute_calls, 2)
        trigger.assert_called_once()

    def test_expired_maintenance_retry_is_skipped_without_order_submission(self):
        worker = self._make_worker("retry_expired")
        trade = self._trade(
            fill_key="fill-retry-expired",
            ts=int(datetime.now(timezone.utc).timestamp()),
        )
        self._claim_attempt(worker, trade, "retry_expired")
        executor = _ScriptedBuyExecutor(["balance", "success"])

        with patch.object(worker, "_trigger_emergency_redeem"):
            worker._poll_cycle(_StaticMonitor([trade]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        worker.db.conn.execute(
            "UPDATE ct_signal_attempts SET expires_at=? WHERE leader_fill_key='fill-retry-expired'",
            (past,),
        )
        worker.db.conn.commit()
        worker._poll_cycle(_StaticMonitor([]), _AllowRisk(), executor, _NoExitMgr(), worker.account.config)

        trade_row = worker.db.conn.execute(
            "SELECT id FROM ct_trades WHERE leader_fill_key='fill-retry-expired'"
        ).fetchone()
        attempt_row = worker.db.conn.execute(
            "SELECT status, retry_count, reason FROM ct_signal_attempts WHERE leader_fill_key='fill-retry-expired'"
        ).fetchone()
        audit_row = worker.db.conn.execute(
            "SELECT stage FROM ct_signal_audit WHERE leader_fill_key='fill-retry-expired' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        self.assertIsNone(trade_row)
        self.assertEqual(attempt_row["status"], "skipped")
        self.assertEqual(attempt_row["retry_count"], 0)
        self.assertEqual(attempt_row["reason"], "maintenance_retry_expired")
        self.assertEqual(audit_row["stage"], "maintenance_retry_expired")
        self.assertEqual(executor.execute_calls, 1)
        self.assertEqual(worker._hourly_stats["buy_fail"], 0)

    def test_mirror_sell_refreshes_leader_remaining_size_each_cycle(self):
        db = CopyTradeDB(str(self._db_path("mirror_sell")))
        executor = SimpleNamespace(
            execute_order=lambda params: OrderResult(
                success=True,
                filled_price=params.price,
                filled_size=params.size,
                filled_usd=params.price * params.size,
                exchange_status="matched",
            )
        )
        mgr = ExitManager(requests.Session(), CopyTradeConfig(exit_strategy="mirror_sell"), db, executor, account_name="acct")
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-buy",
                "leader_fill_key": "fill-buy",
                "leader_side": "BUY",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 100.0,
                "our_usd": 50.0,
                "status": "filled",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        sell_trade = LeaderTrade(
            leader_address="0xabc",
            tx_hash="tx-sell",
            fill_key="fill-sell",
            timestamp="1700000000",
            side="SELL",
            token_id="tok",
            condition_id="cond",
            price=0.6,
            size=10.0,
            usd_amount=6.0,
            outcome="YES",
            market_slug="mkt",
        )
        mgr._leader_position_cache[("0xabc", "tok")] = 100.0

        with patch(
            "copytrade.exit_manager.http_get_json",
            return_value=[
                {
                    "asset": "tok",
                    "conditionId": "cond",
                    "slug": "mkt",
                    "outcome": "YES",
                    "size": 50.0,
                    "avgPrice": 0.5,
                    "currentValue": 25.0,
                    "cashPnl": 0.0,
                }
            ],
        ):
            actions = mgr.process_exits([sell_trade])

        self.assertEqual(len(actions), 1)
        self.assertAlmostEqual(actions[0].size, 100.0 * (10.0 / 60.0), places=6)
        row = db.conn.execute(
            "SELECT our_size, exit_usd, profit FROM ct_trades WHERE account_name='acct'"
        ).fetchone()
        self.assertAlmostEqual(row["our_size"], 100.0 - actions[0].size, places=6)
        self.assertAlmostEqual(row["exit_usd"], actions[0].price * actions[0].size, places=6)
        self.assertIsNotNone(row["profit"])
        db.close()

    def test_mirror_sell_min_size_failure_marks_trade_pending(self):
        db = CopyTradeDB(str(self._db_path("mirror_sell_min_size")))

        class _MinSizeExitExecutor:
            def __init__(self):
                self.calls = 0

            def execute_order(self, params):
                self.calls += 1
                return OrderResult(
                    success=False,
                    error="structured min size",
                    error_code="min_order_size",
                    submitted_size=params.size,
                    min_order_size=5.0,
                )

        executor = _MinSizeExitExecutor()
        mgr = ExitManager(requests.Session(), CopyTradeConfig(exit_strategy="mirror_sell"), db, executor, account_name="acct")
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-buy",
                "leader_fill_key": "fill-buy-min",
                "leader_side": "BUY",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 10.0,
                "our_usd": 5.0,
                "status": "filled",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        sell_trade = LeaderTrade(
            leader_address="0xabc",
            tx_hash="tx-sell",
            fill_key="fill-sell-min",
            timestamp="1700000000",
            side="SELL",
            token_id="tok",
            condition_id="cond",
            price=0.6,
            size=1.0,
            usd_amount=0.6,
            outcome="YES",
            market_slug="mkt",
        )

        with patch("copytrade.exit_manager.http_get_json", return_value=[]):
            actions = mgr.process_exits([sell_trade])
        mgr.process_exits([sell_trade])

        row = db.conn.execute(
            "SELECT exit_status, skip_reason FROM ct_trades WHERE account_name='acct'"
        ).fetchone()
        self.assertEqual(len(actions), 1)
        self.assertEqual(executor.calls, 1)
        self.assertEqual(row["exit_status"], "open")
        self.assertTrue(row["skip_reason"].startswith("min_size_pending:"))
        db.close()

    def test_submitted_exit_order_waits_for_verification_before_reducing_position(self):
        db = CopyTradeDB(str(self._db_path("exit_submit")))
        executor = _SubmittedExitExecutor(order_status="live")
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            db,
            executor,
            account_name="acct",
        )
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-buy",
                "leader_fill_key": "fill-buy",
                "leader_side": "BUY",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 100.0,
                "our_usd": 50.0,
                "status": "filled",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        sell_trade = LeaderTrade(
            leader_address="0xabc",
            tx_hash="tx-sell",
            fill_key="fill-sell",
            timestamp="1700000000",
            side="SELL",
            token_id="tok",
            condition_id="cond",
            price=0.6,
            size=10.0,
            usd_amount=6.0,
            outcome="YES",
            market_slug="mkt",
        )

        with patch(
            "copytrade.exit_manager.http_get_json",
            return_value=[
                {
                    "asset": "tok",
                    "conditionId": "cond",
                    "slug": "mkt",
                    "outcome": "YES",
                    "size": 50.0,
                    "avgPrice": 0.5,
                    "currentValue": 25.0,
                    "cashPnl": 0.0,
                }
            ],
        ):
            actions = mgr.process_exits([sell_trade])

        self.assertEqual(len(actions), 1)
        trade_row = db.conn.execute(
            "SELECT our_size, our_usd, exit_usd, profit, exit_status FROM ct_trades WHERE account_name='acct'"
        ).fetchone()
        self.assertEqual(
            tuple(trade_row),
            (100.0, 50.0, None, None, "open"),
        )
        exit_row = db.conn.execute(
            "SELECT status, exchange_order_status, requested_size FROM ct_exit_orders WHERE order_id='exit-order-1'"
        ).fetchone()
        self.assertEqual(tuple(exit_row[:2]), ("submitted", "submitted"))

        executor._order_status = "matched"
        executor._matched_size = float(exit_row["requested_size"])
        executor._matched_price = 0.6
        mgr.process_exits([])

        trade_row = db.conn.execute(
            "SELECT our_size, our_usd, exit_usd, profit FROM ct_trades WHERE account_name='acct'"
        ).fetchone()
        self.assertAlmostEqual(trade_row["our_size"], 100.0 - float(exit_row["requested_size"]), places=6)
        self.assertAlmostEqual(trade_row["exit_usd"], float(exit_row["requested_size"]) * 0.6, places=6)
        self.assertIsNotNone(trade_row["profit"])
        exit_row = db.conn.execute(
            "SELECT status, exchange_order_status, filled_size_actual FROM ct_exit_orders WHERE order_id='exit-order-1'"
        ).fetchone()
        self.assertEqual(tuple(exit_row[:2]), ("filled", "matched"))
        self.assertAlmostEqual(exit_row["filled_size_actual"], float(trade_row["exit_usd"]) / 0.6, places=6)
        db.close()

    def test_mirror_sell_zero_conditional_balance_pauses_trade_without_realizing_pnl(self):
        db = CopyTradeDB(str(self._db_path("mirror_zero_balance")))

        class _ZeroBalanceExecutor:
            def __init__(self):
                self.calls = 0

            def execute_order(self, params):
                self.calls += 1
                return OrderResult(
                    success=False,
                    error=(
                        "insufficient_clob_balance asset=conditional token_id=tok "
                        "side=SELL purpose=mirror_sell balance=0.000000 required=1.000000"
                    ),
                )

        executor = _ZeroBalanceExecutor()
        mgr = ExitManager(
            requests.Session(),
            CopyTradeConfig(exit_strategy="mirror_sell"),
            db,
            executor,
            account_name="acct",
        )
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-buy",
                "leader_fill_key": "fill-buy",
                "leader_side": "BUY",
                "our_side": "BUY",
                "our_price": 0.5,
                "our_size": 10.0,
                "our_usd": 5.0,
                "status": "filled",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )
        sell_trade = LeaderTrade(
            leader_address="0xabc",
            tx_hash="tx-sell",
            fill_key="fill-sell",
            timestamp="1700000000",
            side="SELL",
            token_id="tok",
            condition_id="cond",
            price=0.6,
            size=10.0,
            usd_amount=6.0,
            outcome="YES",
            market_slug="mkt",
        )

        position_payload = [
            {
                "asset": "tok",
                "conditionId": "cond",
                "slug": "mkt",
                "outcome": "YES",
                "size": 50.0,
                "avgPrice": 0.5,
                "currentValue": 25.0,
                "cashPnl": 0.0,
            }
        ]
        with patch("copytrade.exit_manager.http_get_json", return_value=position_payload):
            first_actions = mgr.process_exits([sell_trade], skip_verification=True)
            second_actions = mgr.process_exits([sell_trade], skip_verification=True)

        row = db.conn.execute(
            "SELECT exit_status, skip_reason, profit, our_size, our_usd FROM ct_trades WHERE account_name='acct'"
        ).fetchone()
        self.assertEqual(len(first_actions), 1)
        self.assertEqual(second_actions, [])
        self.assertEqual(executor.calls, 1)
        self.assertEqual(row["exit_status"], "open")
        self.assertTrue(str(row["skip_reason"]).startswith("pending_clob_balance:"))
        self.assertIsNone(row["profit"])
        self.assertAlmostEqual(float(row["our_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(row["our_usd"]), 5.0, places=6)
        db.close()

    def test_settlement_check_uses_market_slug_fallback(self):
        db = CopyTradeDB(str(self._db_path("risk_slug")))
        trade = self._trade()
        trade.market_slug = "specific-market"
        risk = RiskManager(requests.Session(), CopyTradeConfig(settlement_days_max=1), db, account_name="acct")

        def fake_http_get_json(session, url, params=None, **kwargs):
            params = params or {}
            if url.endswith("/markets") and params.get("conditionId") == "cond":
                return [{"conditionId": "other", "slug": "wrong"}]
            if url.endswith("/markets") and params.get("slug") == "specific-market":
                return [{"conditionId": "cond", "slug": "specific-market", "endDate": "2099-01-01T00:00:00Z"}]
            if url.endswith("/events") and params.get("slug") == "specific-market":
                return []
            raise AssertionError((url, params))

        with patch("copytrade.risk.http_get_json", side_effect=fake_http_get_json):
            ok, reason = risk.check_all(trade, 100.0)

        self.assertFalse(ok)
        self.assertIn("settlement in", reason)
        db.close()


if __name__ == "__main__":
    unittest.main()
