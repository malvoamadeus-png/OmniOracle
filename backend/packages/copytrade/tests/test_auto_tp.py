import tempfile
import unittest
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.executor import DryRunExecutor, OrderExecutor, OrderParams, OrderResult
from copytrade.exit_manager import ExitManager
from copytrade.monitor import LeaderTrade
from copytrade.paths import DOTENV_PATH
from copytrade.user_order_hub import UserOrderEvent
from copytrade.worker import AccountWorker


class _AllowRisk:
    def check_all(self, leader_trade, our_usd):
        return True, "ok"


class _StaticMonitor:
    def __init__(self, trades):
        self._trades = list(trades)

    def poll_once(self):
        return list(self._trades)


class _AutoTPExecutor:
    def __init__(self, *, min_order_size=0.0, tick_size=0.01, fail_error=None, fail_error_code=None):
        self._next_id = 1
        self.placed = []
        self.cancelled = []
        self.order_status = {}
        self.min_order_size = min_order_size
        self.tick_size = tick_size
        self.fail_error = fail_error
        self.fail_error_code = fail_error_code
        self.attempts = 0
        self._client = SimpleNamespace(get_order=self._get_order)

    def get_market_constraints(self, token_id):
        return self.tick_size, self.min_order_size

    def execute_order(self, params):
        self.attempts += 1
        if self.fail_error:
            return OrderResult(success=False, error=self.fail_error, error_code=self.fail_error_code)
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        self.placed.append((order_id, params))
        if getattr(params, "order_purpose", "") == "mirror_sell":
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=params.price,
                filled_size=params.size,
                filled_usd=params.price * params.size,
                exchange_status="matched",
            )
        return OrderResult(
            success=True,
            order_id=order_id,
            limit_price=params.price,
            exchange_status="submitted",
        )

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def _get_order(self, order_id):
        return dict(self.order_status.get(order_id, {"status": "live"}))


class _StepwiseSubmittedExecutor:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._next_id = 1
        self._client = SimpleNamespace(get_order=self._get_order)

    def get_market_constraints(self, token_id):
        return 0.01, 0.0

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
        order_id = f"order-{self._next_id}"
        self._next_id += 1
        return OrderResult(
            success=True,
            order_id=order_id,
            limit_price=params.price,
            exchange_status="submitted",
        )

    def _get_order(self, order_id):
        if order_id != "order-1":
            return {"status": "live"}
        if self._statuses:
            return dict(self._statuses.pop(0))
        return {"status": "live"}


class _CaptureOrderClient:
    def __init__(
        self,
        *,
        tick_size="0.01",
        min_order_size="5",
        best_bid="0.74",
        best_ask="0.76",
        neg_risk=False,
        market_tokens=None,
        include_market_tokens=True,
        book_error=None,
        ask_prices=None,
        bid_prices=None,
    ):
        self.order_args = None
        self.order_options = None
        self.order_type = None
        self.post_calls = 0
        self.order_book_calls = 0
        self.market_info_calls = []
        self.tick_size = tick_size
        self.min_order_size = min_order_size
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.ask_prices = list(ask_prices) if ask_prices is not None else None
        self.bid_prices = list(bid_prices) if bid_prices is not None else None
        self.neg_risk = neg_risk
        self.market_tokens = ["tok"] if market_tokens is None else list(market_tokens)
        self.include_market_tokens = include_market_tokens
        self.book_error = book_error

    def get_clob_market_info(self, condition_id):
        self.market_info_calls.append(condition_id)
        info = {
            "mts": self.tick_size,
            "mos": self.min_order_size,
            "nr": self.neg_risk,
        }
        if self.include_market_tokens:
            info["tokens"] = [{"token_id": token_id} for token_id in self.market_tokens]
        return info

    def get_order_book(self, token_id):
        self.order_book_calls += 1
        if self.book_error:
            raise self.book_error
        bid_prices = self.bid_prices if self.bid_prices is not None else [self.best_bid]
        ask_prices = self.ask_prices if self.ask_prices is not None else [self.best_ask]
        return SimpleNamespace(
            tick_size=self.tick_size,
            min_order_size=self.min_order_size,
            bids=[SimpleNamespace(price=price, size="10") for price in bid_prices],
            asks=[SimpleNamespace(price=price, size="10") for price in ask_prices],
        )

    def create_and_post_order(self, *, order_args, options, order_type):
        self.post_calls += 1
        self.order_args = order_args
        self.order_options = options
        self.order_type = order_type
        return {"orderID": "captured-order"}


class _BalanceCaptureClient(_CaptureOrderClient):
    def __init__(self, *, balance_row=None, balance_error=None, **kwargs):
        super().__init__(**kwargs)
        self.balance_row = balance_row
        self.balance_error = balance_error
        self.balance_params = []
        self.update_calls = 0

    def get_balance_allowance(self, params=None):
        self.balance_params.append(params)
        if self.balance_error:
            raise self.balance_error
        return self.balance_row

    def update_balance_allowance(self, params=None):
        self.update_calls += 1
        return self.balance_row


class AutoTakeProfitTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.resources = []

    def tearDown(self):
        for resource in self.resources:
            resource.close()
        self.tmpdir.cleanup()

    def _db(self, name: str) -> CopyTradeDB:
        db = CopyTradeDB(str(Path(self.tmpdir.name) / f"{name}.sqlite"))
        self.resources.append(db)
        return db

    def _cfg(self) -> CopyTradeConfig:
        return CopyTradeConfig(
            leader_addresses=["0xabc"],
            exit_strategy="mirror_sell",
            auto_tp_enabled=True,
        )

    def _insert_trade(self, db: CopyTradeDB, *, price: float = 0.55, size: float = 100.0, usd: float = 55.0) -> int:
        return db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-buy",
                "leader_fill_key": f"fill-buy-{price}-{size}",
                "leader_side": "BUY",
                "our_side": "BUY",
                "our_price": price,
                "our_size": size,
                "our_usd": usd,
                "status": "filled",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )

    def _trade(self) -> LeaderTrade:
        return LeaderTrade(
            leader_address="0xabc",
            tx_hash="tx-1",
            fill_key="fill-1",
            timestamp="1700000000",
            side="BUY",
            token_id="tok",
            condition_id="cond",
            price=0.5,
            size=2000.0,
            usd_amount=1000.0,
            outcome="YES",
            market_slug="mkt",
            ts_int=1_700_000_000,
        )

    def test_curve_boundaries(self):
        mgr = ExitManager(requests.Session(), self._cfg(), self._db("curve"), _AutoTPExecutor(), account_name="acct")

        cases = {
            0.39: (0.78, 0.40),
            0.40: (0.80, 0.40),
            0.55: (0.85, 0.30),
            0.70: (0.90, 0.20),
        }
        for price, (target, ratio) in cases.items():
            plan = mgr._build_tp_plan(price)
            self.assertIsNotNone(plan)
            self.assertAlmostEqual(plan.target_price, target, places=6)
            self.assertAlmostEqual(plan.sell_ratio, ratio, places=6)
        self.assertIsNone(mgr._build_tp_plan(0.7001))

    def test_entry_fills_aggregate_into_single_bucket_above_min_order_size(self):
        db = self._db("bucket_aggregate")
        executor = _AutoTPExecutor(min_order_size=5.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=18.0, usd=9.9)

        mgr.register_entry_fill(trade_id, filled_size=9.0, filled_usd=4.95, fill_price=0.55)
        first_count = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM ct_auto_tp_bucket_orders"
        ).fetchone()
        self.assertEqual(int(first_count["cnt"]), 0)

        mgr.register_entry_fill(trade_id, filled_size=9.0, filled_usd=4.95, fill_price=0.55)

        orders = db.conn.execute(
            "SELECT kind, side, requested_size, bucket_price, status FROM ct_auto_tp_bucket_orders"
        ).fetchall()
        self.assertEqual(len(orders), 1)
        self.assertEqual(tuple(orders[0][:2]), ("tp_sell", "SELL"))
        self.assertAlmostEqual(float(orders[0]["requested_size"]), 5.4, places=6)
        self.assertAlmostEqual(float(orders[0]["bucket_price"]), 0.85, places=6)
        self.assertEqual(orders[0]["status"], "submitted")

        mapping_count = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM ct_auto_tp_bucket_order_lots"
        ).fetchone()
        self.assertEqual(int(mapping_count["cnt"]), 2)

        legacy_count = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM ct_auto_tp_orders"
        ).fetchone()
        self.assertEqual(int(legacy_count["cnt"]), 0)

    def test_live_bucket_order_blocks_second_order_for_same_price_lot(self):
        db = self._db("bucket_live_block")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=60.0, usd=33.0)

        mgr.register_entry_fill(trade_id, filled_size=30.0, filled_usd=16.5, fill_price=0.55)
        mgr.register_entry_fill(trade_id, filled_size=30.0, filled_usd=16.5, fill_price=0.55)

        orders = db.conn.execute(
            "SELECT id, requested_size, bucket_price FROM ct_auto_tp_bucket_orders ORDER BY id"
        ).fetchall()
        self.assertEqual(len(orders), 1)
        self.assertAlmostEqual(float(orders[0]["requested_size"]), 9.0, places=6)

        mapping_rows = db.conn.execute(
            "SELECT lot_id, requested_size FROM ct_auto_tp_bucket_order_lots ORDER BY id"
        ).fetchall()
        self.assertEqual(len(mapping_rows), 1)
        self.assertAlmostEqual(float(mapping_rows[0]["requested_size"]), 9.0, places=6)

    def test_different_bucket_prices_are_not_merged(self):
        db = self._db("bucket_split_by_price")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=10.5)

        mgr.register_entry_fill(trade_id, filled_size=10.0, filled_usd=5.0, fill_price=0.5)
        mgr.register_entry_fill(trade_id, filled_size=10.0, filled_usd=5.5, fill_price=0.55)

        rows = db.conn.execute(
            "SELECT kind, bucket_price, requested_size FROM ct_auto_tp_bucket_orders ORDER BY bucket_price"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([round(float(row["bucket_price"]), 2) for row in rows], [0.84, 0.85])
        self.assertEqual([round(float(row["requested_size"]), 2) for row in rows], [3.33, 3.0])

    def test_partial_buy_verification_creates_multiple_lots(self):
        db = self._db("partial_buy_lots")
        cfg = self._cfg()
        account = SimpleNamespace(name="acct", env_suffix="ACCT", config=cfg)
        worker = AccountWorker(account, db, dry_run=True, once=True)
        executor = _StepwiseSubmittedExecutor(
            [
                {"status": "live"},
                {"status": "live", "size_matched": "10", "price": "0.5", "original_size": "100"},
                {"status": "live", "size_matched": "30", "price": "0.5", "original_size": "100"},
            ]
        )
        mgr = ExitManager(requests.Session(), cfg, db, executor, account_name="acct")

        worker._poll_cycle(_StaticMonitor([self._trade()]), _AllowRisk(), executor, mgr, cfg)
        worker._verify_recent_orders(executor, mgr)
        worker._verify_recent_orders(executor, mgr)

        rows = db.conn.execute(
            "SELECT original_size, remaining_size FROM ct_auto_tp_lots ORDER BY id"
        ).fetchall()
        self.assertEqual([round(float(r["original_size"]), 6) for r in rows], [10.0, 20.0])
        self.assertEqual([round(float(r["remaining_size"]), 6) for r in rows], [10.0, 20.0])
        trade_status = db.conn.execute(
            "SELECT status, exchange_order_status FROM ct_trades WHERE our_order_id='order-1'"
        ).fetchone()
        self.assertEqual(tuple(trade_status), ("partially_filled", "live"))

    def test_bucket_partial_tp_fill_places_rebuy_and_rebuy_fill_creates_child_lots(self):
        db = self._db("bucket_tp_rebuy")
        executor = _AutoTPExecutor(min_order_size=5.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=11.0)

        mgr.register_entry_fill(trade_id, filled_size=10.0, filled_usd=5.5, fill_price=0.55)
        mgr.register_entry_fill(trade_id, filled_size=10.0, filled_usd=5.5, fill_price=0.55)

        tp_order = db.conn.execute(
            "SELECT order_id, requested_size, bucket_price FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        executor.order_status[tp_order["order_id"]] = {
            "status": "live",
            "size_matched": "3.0",
            "price": str(tp_order["bucket_price"]),
        }
        executor.min_order_size = 0.0

        mgr.process_exits([])

        parent_rows = db.conn.execute(
            "SELECT remaining_size, tp_filled_size, pending_rebuy_size FROM ct_auto_tp_lots ORDER BY id"
        ).fetchall()
        self.assertEqual(len(parent_rows), 2)
        self.assertEqual(
            [(round(float(r["remaining_size"]), 2), round(float(r["tp_filled_size"]), 2), round(float(r["pending_rebuy_size"]), 2)) for r in parent_rows],
            [(8.5, 1.5, 0.75), (8.5, 1.5, 0.75)],
        )
        tp_status = db.conn.execute(
            "SELECT status, exchange_order_status FROM ct_auto_tp_bucket_orders WHERE order_id=?",
            (tp_order["order_id"],),
        ).fetchone()
        self.assertEqual(tuple(tp_status), ("partially_filled", "live"))
        tp_count_before = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell'"
        ).fetchone()
        mgr._refresh_auto_tp_group("0xabc", "tok")
        tp_count_after = db.conn.execute(
            "SELECT COUNT(*) AS cnt FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell'"
        ).fetchone()
        self.assertEqual(int(tp_count_after["cnt"]), int(tp_count_before["cnt"]))

        rebuy_order = db.conn.execute(
            "SELECT order_id, requested_size, bucket_price FROM ct_auto_tp_bucket_orders WHERE kind='rebuy_buy' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(rebuy_order)
        self.assertAlmostEqual(float(rebuy_order["requested_size"]), 1.5, places=6)

        executor.order_status[rebuy_order["order_id"]] = {
            "status": "matched",
            "size_matched": str(rebuy_order["requested_size"]),
            "price": str(rebuy_order["bucket_price"]),
        }

        mgr.process_exits([])

        child_rows = db.conn.execute(
            "SELECT parent_lot_id, original_size, entry_price FROM ct_auto_tp_lots WHERE parent_lot_id IS NOT NULL ORDER BY id"
        ).fetchall()
        self.assertEqual(len(child_rows), 2)
        self.assertEqual([round(float(row["original_size"]), 2) for row in child_rows], [0.75, 0.75])
        self.assertEqual([round(float(row["entry_price"]), 2) for row in child_rows], [0.55, 0.55])

    def test_leader_full_close_cancels_bucket_orders_and_closes_lots(self):
        db = self._db("leader_full_close")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=11.0)
        mgr.register_entry_fill(trade_id, filled_size=20.0, filled_usd=11.0, fill_price=0.55)

        bucket_order = db.conn.execute(
            "SELECT order_id FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell' ORDER BY id DESC LIMIT 1"
        ).fetchone()

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
                    "size": 0.0,
                    "avgPrice": 0.55,
                    "currentValue": 0.0,
                    "cashPnl": 0.0,
                }
            ],
        ):
            mgr.process_exits([sell_trade])

        self.assertIn(bucket_order["order_id"], executor.cancelled)
        lot = db.conn.execute(
            "SELECT remaining_size, status FROM ct_auto_tp_lots LIMIT 1"
        ).fetchone()
        self.assertAlmostEqual(float(lot["remaining_size"]), 0.0, places=6)
        self.assertEqual(lot["status"], "leader_closed")

    def test_dry_run_auto_tp_uses_bucket_orders_without_recursing(self):
        db = self._db("dry_run")
        executor = DryRunExecutor(self._cfg())
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=11.0)

        mgr.register_entry_fill(trade_id, filled_size=20.0, filled_usd=11.0, fill_price=0.55)

        lots = db.conn.execute("SELECT COUNT(*) AS cnt FROM ct_auto_tp_lots").fetchone()
        orders = db.conn.execute("SELECT kind, status FROM ct_auto_tp_bucket_orders").fetchall()
        self.assertEqual(int(lots["cnt"]), 1)
        self.assertEqual(len(orders), 1)
        self.assertEqual(tuple(orders[0]), ("tp_sell", "submitted"))

    def test_auto_tp_orders_use_gtc_without_expiration_and_keep_static_price(self):
        client = _CaptureOrderClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="SELL",
                    price=0.8,
                    size=10.0,
                    usd=8.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    order_purpose="auto_tp",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(client.order_args.expiration, 0)
        self.assertEqual(client.order_type, "GTC")
        self.assertAlmostEqual(float(client.order_args.price), 0.8, places=6)
        self.assertEqual(executor.get_market_constraints("tok"), (0.01, 5.0))
        self.assertEqual(client.order_options.tick_size, "0.01")
        self.assertFalse(client.order_options.neg_risk)

    def test_sell_preflight_blocks_missing_conditional_balance(self):
        client = _BalanceCaptureClient(
            balance_row={
                "balance": "0",
                "allowances": {"0xexchange": str(10**30)},
            }
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="SELL",
                price=0.8,
                size=10.0,
                usd=8.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "balance_allowance")
        self.assertIn("insufficient_clob_balance", result.error)
        self.assertIn("conditional token_id=tok", result.error)
        self.assertEqual(client.post_calls, 0)
        self.assertEqual(client.update_calls, 1)

    def test_sell_preflight_blocks_when_balance_query_fails(self):
        client = _BalanceCaptureClient(balance_error=RuntimeError("server disconnected"))
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="SELL",
                price=0.8,
                size=10.0,
                usd=8.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "balance_allowance")
        self.assertIn("clob_balance_preflight_unavailable", result.error)
        self.assertEqual(client.post_calls, 0)

    def test_auto_tp_lot_pauses_after_missing_conditional_balance(self):
        db = self._db("auto_tp_balance_unavailable")
        executor = _AutoTPExecutor(
            fail_error=(
                "insufficient_clob_balance asset=conditional token_id=tok "
                "side=SELL purpose=auto_tp balance=0.000000 required=40.000000"
            )
        )
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.1, size=100.0, usd=10.0)
        lot_id = db.insert_auto_tp_lot(
            {
                "account_name": "acct",
                "root_trade_id": trade_id,
                "parent_lot_id": None,
                "leader_address": "0xabc",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
                "entry_price": 0.1,
                "original_size": 100.0,
                "remaining_size": 100.0,
                "tp_target_size": 40.0,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )

        mgr._refresh_auto_tp_group("0xabc", "tok")

        lot = db.get_auto_tp_lot(lot_id, account_name="acct")
        self.assertEqual(lot["status"], "balance_unavailable")
        self.assertEqual(
            db.get_auto_tp_lots_for_group(
                account_name="acct",
                leader_address="0xabc",
                token_id="tok",
                include_closed=False,
            ),
            [],
        )

    def test_auto_tp_min_size_failure_marks_lot_pending_without_retries(self):
        db = self._db("auto_tp_min_size_pending")
        executor = _AutoTPExecutor(
            min_order_size=0.0,
            tick_size=0.01,
            fail_error="structured min size response",
            fail_error_code="min_order_size",
        )
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=7.0, usd=3.85)
        lot_id = db.insert_auto_tp_lot(
            {
                "account_name": "acct",
                "root_trade_id": trade_id,
                "parent_lot_id": None,
                "leader_address": "0xabc",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
                "entry_price": 0.55,
                "original_size": 7.0,
                "remaining_size": 7.0,
                "tp_target_size": 2.1,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )

        mgr._refresh_auto_tp_group("0xabc", "tok")
        mgr.process_exits([], skip_verification=True)

        lot = db.get_auto_tp_lot(lot_id, account_name="acct")
        self.assertEqual(lot["status"], "min_size_pending")
        self.assertEqual(executor.attempts, 1)
        self.assertEqual(
            db.get_auto_tp_lots_for_group(
                account_name="acct",
                leader_address="0xabc",
                token_id="tok",
                include_closed=False,
            )[0]["status"],
            "min_size_pending",
        )

    def test_executor_blocks_sub_min_size_order_before_posting(self):
        client = _CaptureOrderClient(min_order_size="0")
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="SELL",
                price=0.52,
                size=2.1,
                usd=1.092,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "min_order_size")
        self.assertAlmostEqual(result.submitted_size, 2.1, places=6)
        self.assertAlmostEqual(result.min_order_size, 5.0, places=6)
        self.assertIn("clob_min_order_size", str(result.error))
        self.assertIn("min=5.000000", str(result.error))
        self.assertEqual(client.post_calls, 0)

    def test_executor_uses_market_min_size_before_fallback(self):
        client = _CaptureOrderClient(min_order_size="7")
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="BUY",
                price=0.5,
                size=6.0,
                usd=3.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                passive_price_mode=True,
                order_purpose="copytrade",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "min_order_size")
        self.assertAlmostEqual(result.min_order_size, 7.0, places=6)
        self.assertEqual(client.post_calls, 0)

    def test_executor_checks_min_size_before_balance_preflight(self):
        client = _BalanceCaptureClient(
            min_order_size="5",
            balance_error=AssertionError("balance preflight should not run"),
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="BUY",
                price=0.5,
                size=2.0,
                usd=1.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                passive_price_mode=True,
                order_purpose="copytrade",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "min_order_size")
        self.assertEqual(client.balance_params, [])
        self.assertEqual(client.post_calls, 0)

    def test_executor_does_not_submit_when_orderbook_is_missing(self):
        client = _CaptureOrderClient(
            market_tokens=[],
            book_error=RuntimeError(
                'request error status=404 url=https://clob.polymarket.com/book '
                'body={"error":"No orderbook exists for the requested token id"}'
            ),
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="stale-token",
                side="SELL",
                price=0.8,
                size=10.0,
                usd=8.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertIn("clob_orderbook_unavailable", str(result.error))
        self.assertEqual(client.order_book_calls, 0)
        self.assertEqual(client.post_calls, 0)
        self.assertIsNone(client.order_args)

    def test_executor_falls_back_to_book_availability_when_market_info_has_no_tokens(self):
        client = _CaptureOrderClient(
            include_market_tokens=False,
            book_error=RuntimeError(
                'request error status=404 url=https://clob.polymarket.com/book '
                'body={"error":"No orderbook exists for the requested token id"}'
            ),
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="stale-token",
                side="SELL",
                price=0.8,
                size=10.0,
                usd=8.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertIn("clob_orderbook_unavailable", str(result.error))
        self.assertEqual(client.order_book_calls, 1)
        self.assertEqual(client.post_calls, 0)

    def test_missing_book_during_price_chase_does_not_submit_and_is_cached(self):
        client = _CaptureOrderClient(
            market_tokens=["stale-token"],
            book_error=RuntimeError(
                'request error status=404 url=https://clob.polymarket.com/book '
                'body={"error":"No orderbook exists for the requested token id"}'
            ),
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        params = OrderParams(
            token_id="stale-token",
            side="BUY",
            price=0.5,
            size=10.0,
            usd=5.0,
            condition_id="cond",
            market_slug="mkt",
            outcome="YES",
            order_purpose="copytrade",
        )

        first = executor.execute_order(params)
        second = executor.execute_order(params)

        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertIn("clob_orderbook_unavailable", str(first.error))
        self.assertIn("clob_orderbook_unavailable", str(second.error))
        self.assertEqual(client.order_book_calls, 1)
        self.assertEqual(client.post_calls, 0)

    def test_auto_tp_verifies_book_even_when_market_info_contains_token(self):
        client = _CaptureOrderClient(
            market_tokens=["stale-token"],
            book_error=RuntimeError(
                'request error status=404 url=https://clob.polymarket.com/book '
                'body={"error":"No orderbook exists for the requested token id"}'
            ),
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="stale-token",
                side="SELL",
                price=0.8,
                size=10.0,
                usd=8.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                order_purpose="auto_tp",
            )
        )

        self.assertFalse(result.success)
        self.assertIn("clob_orderbook_unavailable", str(result.error))
        self.assertEqual(client.order_book_calls, 1)
        self.assertEqual(client.post_calls, 0)

    def test_auto_tp_book_probe_uses_raw_http_when_client_has_host(self):
        class _MissingBookResponse:
            status_code = 404
            text = '{"error":"No orderbook exists for the requested token id"}'

            def json(self):
                return {"error": "No orderbook exists for the requested token id"}

        client = _CaptureOrderClient(
            market_tokens=["stale-token"],
            book_error=AssertionError("sdk get_order_book should not be called"),
        )
        client.host = "https://clob.polymarket.com"
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.requests.get", return_value=_MissingBookResponse()) as get_mock:
            result = executor.execute_order(
                OrderParams(
                    token_id="stale-token",
                    side="SELL",
                    price=0.8,
                    size=10.0,
                    usd=8.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    order_purpose="auto_tp",
                )
            )

        self.assertFalse(result.success)
        self.assertIn("clob_orderbook_unavailable", str(result.error))
        self.assertEqual(client.order_book_calls, 0)
        self.assertEqual(client.post_calls, 0)
        get_mock.assert_called_once()

    def test_missing_orderbook_marks_auto_tp_lots_inactive(self):
        db = self._db("orderbook_unavailable")
        executor = _AutoTPExecutor(
            min_order_size=0.0,
            tick_size=0.01,
            fail_error="clob_orderbook_unavailable token_id=tok condition_id=cond",
        )
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=11.0)

        mgr.register_entry_fill(trade_id, filled_size=20.0, filled_usd=11.0, fill_price=0.55)

        lots = db.conn.execute(
            "SELECT status, pending_rebuy_size FROM ct_auto_tp_lots ORDER BY id"
        ).fetchall()
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0]["status"], "orderbook_unavailable")
        self.assertAlmostEqual(float(lots[0]["pending_rebuy_size"]), 0.0, places=6)
        self.assertEqual(executor.placed, [])
        self.assertEqual(
            db.get_auto_tp_lots_for_group(
                account_name="acct",
                leader_address="0xabc",
                token_id="tok",
                include_closed=False,
            ),
            [],
        )

        mgr.process_exits([], skip_verification=True)
        self.assertEqual(executor.placed, [])

    def test_regular_orders_keep_two_hour_gtd_expiration(self):
        client = _CaptureOrderClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="BUY",
                    price=0.5,
                    size=10.0,
                    usd=5.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    passive_price_mode=True,
                    order_purpose="copytrade",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(client.order_args.expiration, 1_700_007_200)
        self.assertEqual(client.order_type, "GTD")
        self.assertFalse(hasattr(client.order_args, "feeRateBps"))
        self.assertFalse(hasattr(client.order_args, "fee_rate_bps"))
        self.assertFalse(hasattr(client.order_args, "nonce"))
        self.assertFalse(hasattr(client.order_args, "taker"))

    def test_custom_tif_can_keep_order_live_until_manual_cancel(self):
        client = _CaptureOrderClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="BUY",
                    price=0.7,
                    size=10.0,
                    usd=7.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    pricing_mode="original",
                    order_purpose="nba_q3q4_counterparty",
                    tif="GTC",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(client.order_args.expiration, 0)
        self.assertEqual(client.order_type, "GTC")
        self.assertAlmostEqual(float(client.order_args.price), 0.7, places=6)

    def test_unsupported_tif_returns_structured_error(self):
        client = _CaptureOrderClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="BUY",
                price=0.7,
                size=10.0,
                usd=7.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                pricing_mode="original",
                order_purpose="copytrade",
                tif="BAD_TIF",
            )
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "unsupported_tif")
        self.assertEqual(client.post_calls, 0)

    def test_v2_order_creation_propagates_builder_code_and_neg_risk_options(self):
        builder_code = "0x" + "33" * 32
        client = _CaptureOrderClient(neg_risk=True, tick_size="0.001")
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client
        executor._builder_code = builder_code

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="BUY",
                price=0.5,
                size=10.0,
                usd=5.0,
                condition_id="0xABCDEF",
                market_slug="mkt",
                outcome="YES",
                passive_price_mode=True,
                order_purpose="copytrade",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(client.market_info_calls, ["0xabcdef"])
        self.assertEqual(client.order_args.builder_code, builder_code)
        self.assertEqual(client.order_options.tick_size, "0.001")
        self.assertTrue(client.order_options.neg_risk)

    def test_v2_order_rederives_api_key_once_on_401(self):
        class _AuthRefreshClient(_CaptureOrderClient):
            def __init__(self):
                super().__init__()
                self.post_attempts = 0
                self.derived = 0
                self.creds = None

            def create_or_derive_api_key(self):
                self.derived += 1
                return SimpleNamespace(
                    api_key="new-key",
                    api_secret="new-secret",
                    api_passphrase="new-passphrase",
                )

            def set_api_creds(self, creds):
                self.creds = creds

            def create_and_post_order(self, *, order_args, options, order_type):
                self.post_attempts += 1
                if self.post_attempts == 1:
                    raise RuntimeError("request error status=401 body={\"error\":\"Unauthorized/Invalid api key\"}")
                return super().create_and_post_order(
                    order_args=order_args,
                    options=options,
                    order_type=order_type,
                )

        client = _AuthRefreshClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        result = executor.execute_order(
            OrderParams(
                token_id="tok",
                side="BUY",
                price=0.5,
                size=10.0,
                usd=5.0,
                condition_id="cond",
                market_slug="mkt",
                outcome="YES",
                passive_price_mode=True,
                order_purpose="copytrade",
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(client.post_attempts, 2)
        self.assertEqual(client.derived, 1)
        self.assertEqual(client.creds.api_key, "new-key")

    def test_v2_cancel_uses_order_payload(self):
        class _CancelClient:
            def __init__(self):
                self.payload = None

            def cancel_order(self, payload):
                self.payload = payload

        client = _CancelClient()
        executor = OrderExecutor.__new__(OrderExecutor)
        executor._client = client

        self.assertTrue(executor.cancel_order("order-123"))
        self.assertEqual(client.payload.orderID, "order-123")

    def test_v2_client_init_derives_api_creds_and_maps_proxy_signature(self):
        from py_clob_client_v2 import SignatureTypeV2

        instances = []

        class _FakeClobClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.creds = kwargs.get("creds")
                self.derived = False
                instances.append(self)

            def create_or_derive_api_key(self):
                self.derived = True
                return SimpleNamespace(
                    api_key="derived-key",
                    api_secret="derived-secret",
                    api_passphrase="derived-pass",
                )

            def set_api_creds(self, creds):
                self.creds = creds

        env = {
            "PRIVATE_KEY": "0x" + "11" * 32,
            "FUNDER_ADDRESS": "0x" + "22" * 20,
            "POLY_BUILDER_CODE": "0x" + "33" * 32,
        }
        with patch.dict(os.environ, env, clear=True), patch.object(OrderExecutor, "_find_repo_dotenv", return_value=None), patch(
            "py_clob_client_v2.ClobClient",
            _FakeClobClient,
        ):
            executor = OrderExecutor(self._cfg(), wallet_type="proxy")

        self.assertIs(executor._client, instances[0])
        self.assertTrue(instances[0].derived)
        self.assertEqual(instances[0].creds.api_key, "derived-key")
        self.assertEqual(instances[0].kwargs["signature_type"], SignatureTypeV2.POLY_PROXY)
        self.assertEqual(instances[0].kwargs["funder"], env["FUNDER_ADDRESS"])
        self.assertEqual(instances[0].kwargs["builder_config"].builder_code, env["POLY_BUILDER_CODE"])

    def test_v2_client_init_silently_derives_existing_api_creds_before_create(self):
        instances = []

        class _FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "apiKey": "derived-key",
                    "secret": "derived-secret",
                    "passphrase": "derived-pass",
                }

        class _FakeClobClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.host = kwargs["host"]
                self.creds = kwargs.get("creds")
                self.fallback_called = False
                instances.append(self)

            def _l1_headers(self):
                return {"POLY_ADDRESS": "0xabc"}

            def create_or_derive_api_key(self):
                self.fallback_called = True
                raise AssertionError("SDK create_or_derive_api_key should not be called")

            def set_api_creds(self, creds):
                self.creds = creds

        env = {
            "PRIVATE_KEY": "0x" + "11" * 32,
            "FUNDER_ADDRESS": "0x" + "22" * 20,
        }
        with patch.dict(os.environ, env, clear=True), patch.object(OrderExecutor, "_find_repo_dotenv", return_value=None), patch(
            "py_clob_client_v2.ClobClient",
            _FakeClobClient,
        ), patch("copytrade.executor.requests.get", return_value=_FakeResponse()) as get_mock, patch(
            "copytrade.executor.requests.post"
        ) as post_mock:
            executor = OrderExecutor(self._cfg(), wallet_type="proxy")

        self.assertIs(executor._client, instances[0])
        self.assertFalse(instances[0].fallback_called)
        self.assertEqual(instances[0].creds.api_key, "derived-key")
        get_mock.assert_called_once()
        post_mock.assert_not_called()

    def test_v2_proxy_init_fails_fast_without_funder(self):
        env = {"PRIVATE_KEY": "0x" + "11" * 32}
        with patch.dict(os.environ, env, clear=True), patch.object(OrderExecutor, "_find_repo_dotenv", return_value=None):
            executor = OrderExecutor(self._cfg(), wallet_type="proxy")

        self.assertIsNone(executor._client)

    def test_executor_finds_repo_env_in_workspace_root(self):
        dotenv_path = OrderExecutor._find_repo_dotenv()
        self.assertIsNotNone(dotenv_path)
        self.assertEqual(dotenv_path.resolve(), DOTENV_PATH)

    def test_aggressive_buy_orders_are_capped_by_signal_relative_chase_limit(self):
        client = _CaptureOrderClient(best_ask="0.99", tick_size="0.01")
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="BUY",
                    price=0.27,
                    size=18.5185185185,
                    usd=5.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    order_purpose="copytrade",
                    aggressive_price_chase_cap_abs=0.01,
                    aggressive_price_chase_cap_bps=300.0,
                )
            )

        self.assertTrue(result.success)
        self.assertAlmostEqual(float(client.order_args.price), 0.27, places=6)
        self.assertAlmostEqual(float(client.order_args.size), 18.51, places=2)

    def test_regular_buy_orders_respect_three_decimal_tick_ceiling(self):
        client = _CaptureOrderClient(best_ask="0.999", tick_size="0.001", min_order_size="5")
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="BUY",
                    price=0.95,
                    size=10.0,
                    usd=9.5,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    order_purpose="copytrade",
                    aggressive_price_chase_cap_abs=0.0,
                    aggressive_price_chase_cap_bps=0.0,
                )
            )

        self.assertTrue(result.success)
        self.assertAlmostEqual(float(client.order_args.price), 0.999, places=6)

    def test_aggressive_buy_uses_lowest_ask_when_orderbook_is_unsorted(self):
        client = _CaptureOrderClient(
            ask_prices=["0.99", "0.28", "0.35"],
            tick_size="0.01",
        )
        executor = OrderExecutor.__new__(OrderExecutor)
        executor.config = self._cfg()
        executor._client = client

        with patch("copytrade.executor.time.time", return_value=1_700_000_000):
            result = executor.execute_order(
                OrderParams(
                    token_id="tok",
                    side="BUY",
                    price=0.27,
                    size=18.5185185185,
                    usd=5.0,
                    condition_id="cond",
                    market_slug="mkt",
                    outcome="YES",
                    order_purpose="copytrade",
                    aggressive_price_chase_cap_abs=0.05,
                    aggressive_price_chase_cap_bps=5000.0,
                )
            )

        self.assertTrue(result.success)
        self.assertAlmostEqual(float(client.order_args.price), 0.29, places=6)
        self.assertAlmostEqual(float(client.order_args.size), 17.24, places=2)

    def test_live_legacy_gtd_auto_tp_order_is_cancelled_for_migration(self):
        db = self._db("legacy_gtd")
        executor = _AutoTPExecutor()
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=10.0, usd=5.5)

        lot_id = db.insert_auto_tp_lot(
            {
                "account_name": "acct",
                "root_trade_id": trade_id,
                "parent_lot_id": None,
                "leader_address": "0xabc",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
                "entry_price": 0.55,
                "original_size": 10.0,
                "remaining_size": 10.0,
                "tp_target_size": 3.0,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )
        db.insert_auto_tp_order(
            {
                "lot_id": lot_id,
                "root_trade_id": trade_id,
                "account_name": "acct",
                "kind": "tp_sell",
                "order_id": "legacy-order-1",
                "side": "SELL",
                "requested_price": 0.85,
                "requested_size": 3.0,
                "requested_usd": 2.55,
                "status": "submitted",
                "exchange_order_status": "live",
            }
        )
        executor.order_status["legacy-order-1"] = {
            "status": "live",
            "size_matched": "0",
            "price": "0.85",
            "order_type": "GTD",
        }

        mgr.process_exits([])

        self.assertIn("legacy-order-1", executor.cancelled)
        row = db.conn.execute(
            "SELECT status, exchange_order_status FROM ct_auto_tp_orders WHERE order_id=?",
            ("legacy-order-1",),
        ).fetchone()
        self.assertEqual(tuple(row), ("submitted", "cancel_requested"))

    def test_user_ws_bucket_trade_and_order_snapshot_do_not_double_count_and_allow_rehang(self):
        db = self._db("ws_bucket_idempotent")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=20.0, usd=11.0)

        mgr.register_entry_fill(trade_id, filled_size=20.0, filled_usd=11.0, fill_price=0.55)
        bucket = db.conn.execute(
            "SELECT order_id, bucket_price FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell' ORDER BY id DESC LIMIT 1"
        ).fetchone()

        summary = mgr.process_user_order_events(
            [
                UserOrderEvent(
                    channel_event="trade",
                    order_id=str(bucket["order_id"]),
                    condition_id="cond",
                    exchange_order_status="matched",
                    matched_size=2.0,
                    price=float(bucket["bucket_price"]),
                    is_delta=True,
                    raw_id="trade-1",
                    raw_payload={},
                ),
                UserOrderEvent(
                    channel_event="order",
                    order_id=str(bucket["order_id"]),
                    condition_id="cond",
                    exchange_order_status="cancelled",
                    matched_size=2.0,
                    price=float(bucket["bucket_price"]),
                    is_delta=False,
                    raw_id="order-1",
                    raw_payload={},
                ),
            ]
        )

        self.assertEqual(int(summary["buy_fill_count"]), 0)
        lot = db.conn.execute(
            "SELECT remaining_size, tp_filled_size, pending_rebuy_size FROM ct_auto_tp_lots ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertAlmostEqual(float(lot["remaining_size"]), 18.0, places=6)
        self.assertAlmostEqual(float(lot["tp_filled_size"]), 2.0, places=6)
        self.assertAlmostEqual(float(lot["pending_rebuy_size"]), 1.0, places=6)

        bucket_row = db.conn.execute(
            "SELECT status, exchange_order_status, filled_size_actual FROM ct_auto_tp_bucket_orders WHERE order_id=?",
            (str(bucket["order_id"]),),
        ).fetchone()
        self.assertEqual(bucket_row["status"], "partially_filled")
        self.assertEqual(bucket_row["exchange_order_status"], "cancelled")
        self.assertAlmostEqual(float(bucket_row["filled_size_actual"]), 2.0, places=6)

        mgr.process_exits([], skip_verification=True)

        tp_rows = db.conn.execute(
            "SELECT order_id, requested_size, status FROM ct_auto_tp_bucket_orders WHERE kind='tp_sell' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(tp_rows), 2)
        self.assertEqual(tp_rows[-1]["status"], "submitted")
        self.assertAlmostEqual(float(tp_rows[-1]["requested_size"]), 4.0, places=6)

    def test_user_ws_entry_trade_and_order_snapshot_create_single_tp_lot(self):
        db = self._db("ws_entry_fill_event_idempotent")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        db.insert_trade(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "leader_tx_hash": "tx-entry-ws",
                "leader_fill_key": "fill-entry-ws",
                "leader_side": "BUY",
                "our_order_id": "entry-order-1",
                "our_side": "BUY",
                "status": "submitted",
                "exchange_order_status": "live",
                "requested_price": 0.55,
                "requested_size": 20.0,
                "requested_usd": 11.0,
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
            }
        )

        summary = mgr.process_user_order_events(
            [
                UserOrderEvent(
                    channel_event="trade",
                    order_id="entry-order-1",
                    condition_id="cond",
                    exchange_order_status="matched",
                    matched_size=10.0,
                    price=0.55,
                    is_delta=True,
                    raw_id="trade-entry-1",
                    raw_payload={},
                ),
                UserOrderEvent(
                    channel_event="order",
                    order_id="entry-order-1",
                    condition_id="cond",
                    exchange_order_status="live",
                    matched_size=10.0,
                    price=0.55,
                    is_delta=False,
                    raw_id="order-entry-1",
                    raw_payload={},
                ),
            ]
        )

        self.assertEqual(int(summary["buy_fill_count"]), 1)
        trade = db.conn.execute(
            "SELECT status, exchange_order_status, our_size, our_usd, filled_size_actual "
            "FROM ct_trades WHERE our_order_id='entry-order-1'"
        ).fetchone()
        self.assertEqual(tuple(trade[:2]), ("partially_filled", "live"))
        self.assertAlmostEqual(float(trade["our_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(trade["our_usd"]), 5.5, places=6)
        self.assertAlmostEqual(float(trade["filled_size_actual"]), 10.0, places=6)

        lots = db.conn.execute(
            "SELECT original_size, remaining_size FROM ct_auto_tp_lots ORDER BY id"
        ).fetchall()
        self.assertEqual(len(lots), 1)
        self.assertAlmostEqual(float(lots[0]["original_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(lots[0]["remaining_size"]), 10.0, places=6)

    def test_post_cutover_bucket_sync_error_unblocks_group(self):
        db = self._db("bucket_sync_error")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        get_order_calls = {"count": 0}

        def fail_get_order(order_id):
            get_order_calls["count"] += 1
            raise RuntimeError("boom")

        executor._client = SimpleNamespace(get_order=fail_get_order)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        trade_id = self._insert_trade(db, price=0.55, size=10.0, usd=5.5)

        lot_id = db.insert_auto_tp_lot(
            {
                "account_name": "acct",
                "root_trade_id": trade_id,
                "parent_lot_id": None,
                "leader_address": "0xabc",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
                "entry_price": 0.55,
                "original_size": 10.0,
                "remaining_size": 10.0,
                "tp_target_size": 3.0,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )
        bucket_order_id = db.insert_auto_tp_bucket_order(
            {
                "account_name": "acct",
                "leader_address": "0xabc",
                "token_id": "tok",
                "condition_id": "cond",
                "market_slug": "mkt",
                "outcome": "YES",
                "kind": "tp_sell",
                "side": "SELL",
                "bucket_price": 0.85,
                "requested_size": 3.0,
                "requested_usd": 2.55,
                "order_id": "bucket-live-1",
                "status": "submitted",
                "exchange_order_status": "live",
            }
        )
        db.insert_auto_tp_bucket_order_lots(
            bucket_order_id,
            [
                {
                    "lot_id": lot_id,
                    "root_trade_id": trade_id,
                    "account_name": "acct",
                    "requested_size": 3.0,
                }
            ],
        )
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        db.conn.execute(
            "UPDATE ct_auto_tp_bucket_orders SET created_at=?, updated_at=? WHERE id=?",
            (old_ts, old_ts, bucket_order_id),
        )
        db.conn.commit()

        for _ in range(3):
            mgr.verify_recent_order_state(source="rest")
        self.assertEqual(get_order_calls["count"], 3)

        row = db.conn.execute(
            "SELECT exchange_order_status, sync_error_count FROM ct_auto_tp_bucket_orders WHERE id=?",
            (bucket_order_id,),
        ).fetchone()
        self.assertEqual(row["exchange_order_status"], "sync_error")
        self.assertEqual(int(row["sync_error_count"]), 3)
        blockers = db.get_open_auto_tp_bucket_orders_for_group(
            account_name="acct",
            leader_address="0xabc",
            token_id="tok",
        )
        self.assertEqual(blockers, [])

        mgr.verify_recent_order_state(source="rest")
        self.assertEqual(get_order_calls["count"], 3)

        retry_ts = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        db.conn.execute(
            "UPDATE ct_auto_tp_bucket_orders SET updated_at=? WHERE id=?",
            (retry_ts, bucket_order_id),
        )
        db.conn.commit()
        mgr.verify_recent_order_state(source="rest")
        self.assertEqual(get_order_calls["count"], 4)

    def test_bucket_sync_transport_error_opens_short_circuit(self):
        db = self._db("bucket_sync_transport")
        executor = _AutoTPExecutor(min_order_size=0.0, tick_size=0.01)
        calls = {"count": 0}
        PolyApiException = type("PolyApiException", (Exception,), {"status_code": None})

        def fail_get_order(order_id):
            calls["count"] += 1
            raise PolyApiException("Request exception!")

        executor._client = SimpleNamespace(get_order=fail_get_order)
        mgr = ExitManager(requests.Session(), self._cfg(), db, executor, account_name="acct")
        for idx in range(2):
            db.insert_auto_tp_bucket_order(
                {
                    "account_name": "acct",
                    "leader_address": "0xabc",
                    "token_id": f"tok-{idx}",
                    "condition_id": f"cond-{idx}",
                    "market_slug": f"mkt-{idx}",
                    "outcome": "YES",
                    "kind": "tp_sell",
                    "side": "SELL",
                    "bucket_price": 0.85,
                    "requested_size": 3.0,
                    "requested_usd": 2.55,
                    "order_id": f"bucket-live-{idx}",
                    "status": "submitted",
                    "exchange_order_status": "live",
                }
            )

        mgr._verify_recent_auto_tp_bucket_orders(source="rest")
        self.assertEqual(calls["count"], 1)

        mgr._verify_recent_auto_tp_bucket_orders(source="rest")
        self.assertEqual(calls["count"], 1)


if __name__ == "__main__":
    unittest.main()
