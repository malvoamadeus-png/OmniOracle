import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import copytrade.build_leader_pnl_snapshot as snapshot
from copytrade.db import CopyTradeDB


class SettlementDateAttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = CopyTradeDB(str(Path(self.tmpdir.name) / "copytrade.sqlite"))

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _insert_trade(self, **overrides):
        token_id = overrides.get("token_id", "tok-1")
        trade = {
            "account_name": "acct",
            "leader_address": "0xleader",
            "leader_tx_hash": f"tx-{token_id}",
            "leader_fill_key": f"fill-{token_id}",
            "leader_side": "BUY",
            "leader_price": 0.4,
            "leader_size": 10.0,
            "leader_usd": 4.0,
            "our_order_id": f"order-{token_id}",
            "our_side": "BUY",
            "our_price": 0.4,
            "our_size": 10.0,
            "our_usd": 4.0,
            "token_id": token_id,
            "condition_id": overrides.get("condition_id", f"cond-{token_id}"),
            "market_slug": overrides.get("market_slug", f"market-{token_id}"),
            "outcome": "YES",
            "status": "filled",
            "exit_status": "open",
            "requested_price": 0.4,
            "requested_size": 10.0,
            "requested_usd": 4.0,
            "filled_size_actual": 10.0,
            "filled_usd_actual": 4.0,
        }
        trade.update(overrides)
        trade_id = self.db.insert_trade(trade)
        update_fields = {
            "created_at": overrides.get("created_at", "2026-04-01T00:00:00+00:00"),
            "updated_at": overrides.get("updated_at", "2026-04-01T00:00:00+00:00"),
        }
        for key in (
            "status",
            "exit_status",
            "exit_price",
            "exit_usd",
            "exit_at",
            "profit",
            "our_size",
            "our_usd",
            "filled_size_actual",
            "filled_usd_actual",
            "official_settlement_at",
            "skip_reason",
            "our_price",
            "token_id",
            "condition_id",
            "market_slug",
        ):
            if key in overrides:
                update_fields[key] = overrides[key]

        sets = ", ".join(f"{key}=?" for key in update_fields.keys())
        values = list(update_fields.values()) + [trade_id]
        self.db.conn.execute(
            f"UPDATE ct_trades SET {sets} WHERE id=?",
            values,
        )
        self.db.conn.commit()
        return trade_id

    def _trade_row(self, trade_id):
        return self.db.conn.execute(
            "SELECT * FROM ct_trades WHERE id=?",
            (trade_id,),
        ).fetchone()

    def _daily_rows(self):
        return self.db.conn.execute(
            "SELECT date_key, leader_address, account_name, realized_pnl, unrealized_pnl, total_pnl, market_count "
            "FROM ct_daily_leader_pnl ORDER BY date_key, leader_address"
        ).fetchall()

    def _ct_meta_value(self, key):
        row = self.db.conn.execute(
            "SELECT value FROM ct_meta WHERE key=?",
            (key,),
        ).fetchone()
        return None if row is None else row["value"]

    def test_repair_overstated_entry_fill_costs_uses_requested_cost_basis(self):
        trade_id = self._insert_trade(
            token_id="tok-overstated-entry",
            requested_price=0.31,
            requested_size=161.29032258064515,
            requested_usd=50.0,
            our_price=0.95,
            our_limit_price=0.95,
            our_filled_price=0.95,
            our_size=0.0,
            our_usd=0.0,
            filled_size_actual=161.29,
            filled_usd_actual=153.2255,
            exit_status="exited",
            exit_price=0.0,
            exit_usd=None,
            profit=-153.2255,
            official_settlement_at="2026-04-19T02:06:44+00:00",
        )

        stats = snapshot.repair_overstated_entry_fill_costs(self.db)

        self.assertEqual(int(stats["repaired"]), 1)
        row = self._trade_row(trade_id)
        self.assertAlmostEqual(float(row["our_price"]), 0.31, places=6)
        self.assertAlmostEqual(float(row["our_filled_price"]), 0.31, places=6)
        self.assertAlmostEqual(float(row["filled_usd_actual"]), 50.0, places=4)
        self.assertAlmostEqual(float(row["profit"]), -50.0, places=4)

    def test_reconcile_requires_official_settlement_time(self):
        trade_id = self._insert_trade(
            token_id="tok-missing-time",
            market_slug="nba-phx-cha-2026-04-02-spread-home-5pt5",
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({"tok-missing-time": 1.0}, {}, set(), 1, 1),
             ):
            marked = snapshot.reconcile_redeemed_positions(self.db)

        self.assertEqual(marked, 0)
        row = self._trade_row(trade_id)
        self.assertEqual(row["exit_status"], "open")
        self.assertIsNone(row["official_settlement_at"])
        self.assertIsNone(row["exit_at"])
        self.assertEqual(row["skip_reason"], "pending_settlement: official time missing")

    def test_reconcile_keeps_future_settlement_time_open(self):
        trade_id = self._insert_trade(
            token_id="tok-future-time",
            market_slug="nba-rookie-of-the-year-873",
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({"tok-future-time": 0.0}, {"tok-future-time": "2999-05-18T00:00:00+00:00"}, set(), 1, 1),
             ):
            marked = snapshot.reconcile_redeemed_positions(self.db)

        self.assertEqual(marked, 0)
        row = self._trade_row(trade_id)
        self.assertEqual(row["exit_status"], "open")
        self.assertIsNone(row["official_settlement_at"])
        self.assertIsNone(row["exit_at"])
        self.assertEqual(row["skip_reason"], "pending_settlement: official time in future")

    def test_reconcile_uses_official_settlement_time_for_resolution_exit(self):
        trade_id = self._insert_trade(
            token_id="tok-resolution",
            market_slug="nba-phi-was-2026-04-01-total-240pt5",
        )
        settlement_time = "2026-04-02T23:30:00+00:00"

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({"tok-resolution": 1.0}, {"tok-resolution": settlement_time}, set(), 1, 1),
             ):
            marked = snapshot.reconcile_redeemed_positions(self.db)

        self.assertEqual(marked, 1)
        row = self._trade_row(trade_id)
        self.assertEqual(row["exit_status"], "exited")
        self.assertEqual(row["exit_at"], settlement_time)
        self.assertEqual(row["official_settlement_at"], settlement_time)
        self.assertAlmostEqual(float(row["profit"]), 6.0)

    def test_reopen_future_resolution_exits_restores_open_trade(self):
        trade_id = self._insert_trade(
            token_id="tok-reopen-future",
            condition_id="cond-reopen-future",
            market_slug="nba-rookie-of-the-year-873",
            exit_status="exited",
            exit_usd=0.0,
            our_size=0.0,
            our_usd=0.0,
            filled_size_actual=10.0,
            filled_usd_actual=4.0,
            profit=-4.0,
            official_settlement_at="2999-05-18T00:00:00+00:00",
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2999-05-18T00:00:00+00:00", trade_id),
        )
        self.db.conn.commit()

        reopened = snapshot.reopen_future_resolution_exits(self.db)

        self.assertEqual(reopened, 1)
        row = self._trade_row(trade_id)
        self.assertEqual(row["exit_status"], "open")
        self.assertIsNone(row["official_settlement_at"])
        self.assertIsNone(row["exit_at"])
        self.assertIsNone(row["profit"])
        self.assertAlmostEqual(float(row["our_size"]), 10.0)
        self.assertAlmostEqual(float(row["our_usd"]), 4.0)
        self.assertEqual(row["skip_reason"], "pending_settlement: official time in future")

    def test_sell_exit_stays_on_sell_day_even_with_official_settlement_at(self):
        trade_id = self._insert_trade(
            token_id="tok-sell",
            market_slug="nba-bos-lal-2026-04-01-moneyline-home",
            exit_status="exited",
            exit_price=0.7,
            exit_usd=7.0,
            profit=3.0,
            our_size=0.0,
            our_usd=0.0,
            official_settlement_at="2026-04-06T12:00:00+00:00",
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-04T02:00:00+00:00", trade_id),
        )
        self.db.conn.commit()

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-07",
            force_rebuild=True,
        )

        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-04")
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), 3.0)

    def test_daily_history_backfill_uses_official_settlement_day(self):
        trade_id = self._insert_trade(
            token_id="tok-backfill",
            market_slug="nba-phx-cha-2026-04-02-spread-home-5pt5",
            exit_status="exited",
            exit_price=1.0,
            exit_usd=0.0,
            profit=6.0,
            our_size=0.0,
            our_usd=0.0,
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-06T20:34:08+00:00", trade_id),
        )
        self.db.conn.commit()

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-07",
            force_rebuild=True,
        )
        self.assertEqual(self._daily_rows(), [])

        with patch.object(
            snapshot,
            "_resolve_tokens_with_cache_and_live",
            return_value=(
                {"tok-backfill": 1.0},
                {"tok-backfill": "2026-04-03T04:11:51+00:00"},
                set(),
                1,
                1,
            ),
        ):
            updated = snapshot.backfill_resolution_exit_settlement_times(self.db)

        self.assertEqual(updated, 1)
        row = self._trade_row(trade_id)
        self.assertEqual(row["official_settlement_at"], "2026-04-03T04:11:51+00:00")
        self.assertEqual(row["exit_at"], "2026-04-03T04:11:51+00:00")

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-07",
            force_rebuild=True,
        )

        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-03")
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), 6.0)
        self.assertEqual(int(rows[0]["market_count"]), 1)

    def test_backfill_skips_already_complete_resolution_exit(self):
        trade_id = self._insert_trade(
            token_id="tok-complete-resolution",
            market_slug="nba-phx-cha-2026-04-02-spread-home-5pt5",
            exit_status="exited",
            exit_price=1.0,
            exit_usd=0.0,
            profit=6.0,
            our_size=0.0,
            our_usd=0.0,
            official_settlement_at="2026-04-03T04:11:51+00:00",
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-03T04:11:51+00:00", trade_id),
        )
        self.db.conn.commit()

        with patch.object(snapshot, "_resolve_tokens_with_cache_and_live") as resolve:
            updated = snapshot.backfill_resolution_exit_settlement_times(self.db)

        self.assertEqual(updated, 0)
        resolve.assert_not_called()
        row = self._trade_row(trade_id)
        self.assertEqual(row["official_settlement_at"], "2026-04-03T04:11:51+00:00")
        self.assertEqual(row["exit_at"], "2026-04-03T04:11:51+00:00")

    def test_resolution_without_official_settlement_is_excluded_from_realized_outputs(self):
        trade_id = self._insert_trade(
            token_id="tok-pending-resolution",
            market_slug="nba-phx-cha-2026-04-02-spread-home-5pt5",
            exit_status="exited",
            exit_price=1.0,
            exit_usd=0.0,
            profit=6.0,
            our_size=0.0,
            our_usd=0.0,
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-06T20:34:08+00:00", trade_id),
        )
        self.db.conn.commit()

        market_map, realized_by_leader = snapshot._load_realized_market_pnl(self.db, "acct")
        self.assertEqual(market_map, {})
        self.assertEqual(realized_by_leader, {})

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-07",
            force_rebuild=True,
        )
        self.assertEqual(self._daily_rows(), [])

    def test_open_resolved_trade_backfills_history_to_settlement_day(self):
        self._insert_trade(
            token_id="tok-open-resolved",
            condition_id="cond-open-resolved",
            market_slug="us-forces-enter-iran-by",
            exit_status="open",
            profit=6.0,
            updated_at="2026-04-09T12:00:00+00:00",
        )

        with patch.object(
            snapshot,
            "_resolve_tokens_with_cache_and_live",
            return_value=(
                {"tok-open-resolved": 1.0},
                {"tok-open-resolved": "2026-04-03T04:11:51+00:00"},
                set(),
                1,
                1,
            ),
        ):
            snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
                self.db,
                "2026-04-10",
                force_rebuild=True,
            )

        rows = self._daily_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-03")
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), 6.0)
        self.assertAlmostEqual(float(rows[0]["unrealized_pnl"]), 0.0)
        self.assertAlmostEqual(float(rows[0]["total_pnl"]), 6.0)

    def test_market_leg_detail_uses_official_settlement_day(self):
        trade_id = self._insert_trade(
            token_id="tok-leg-detail",
            condition_id="cond-leg-detail",
            market_slug="nba-phi-was-2026-04-01-total-240pt5",
            exit_status="exited",
            exit_price=1.0,
            exit_usd=0.0,
            profit=6.0,
            our_size=0.0,
            our_usd=0.0,
            official_settlement_at="2026-04-03T04:11:51+00:00",
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-06T20:34:08+00:00", trade_id),
        )
        self.db.conn.commit()

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-07",
            force_rebuild=True,
        )

        leg_rows = snapshot._rebuild_daily_leader_market_leg_pnl(
            self.db,
            current_date_key="2026-04-07",
        )
        matched = [
            row for row in leg_rows
            if row["token_id"] == "tok-leg-detail"
        ]
        settlement_rows = [
            row for row in matched
            if row["date_key"] == "2026-04-03"
        ]

        self.assertEqual(len(settlement_rows), 1)
        self.assertAlmostEqual(float(settlement_rows[0]["realized_pnl_delta"]), 6.0)
        self.assertAlmostEqual(float(settlement_rows[0]["settled_size"]), 10.0)
        self.assertFalse(any(row["date_key"] == "2026-04-07" for row in matched))

    def test_open_position_cutover_day_uses_eod_unrealized_as_delta(self):
        rows = snapshot._build_daily_leader_deltas(
            self.db,
            "2026-04-08",
            [
                {
                    "account_name": "acct",
                    "leader_address": "0xleader",
                    "total_realized_pnl": 0.0,
                    "total_unrealized_pnl": 50.0,
                    "total_markets": 1,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-08")
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), 0.0)
        self.assertAlmostEqual(float(rows[0]["unrealized_pnl"]), 50.0)
        self.assertAlmostEqual(float(rows[0]["total_pnl"]), 50.0)

    def test_open_position_next_day_uses_eod_diff(self):
        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-08",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 50.0,
                    "total_pnl": 50.0,
                    "market_count": 1,
                }
            ]
        )

        rows = snapshot._build_daily_leader_deltas(
            self.db,
            "2026-04-09",
            [
                {
                    "account_name": "acct",
                    "leader_address": "0xleader",
                    "total_realized_pnl": 0.0,
                    "total_unrealized_pnl": 80.0,
                    "total_markets": 1,
                }
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-09")
        self.assertAlmostEqual(float(rows[0]["unrealized_pnl"]), 30.0)
        self.assertAlmostEqual(float(rows[0]["total_pnl"]), 30.0)

    def test_daily_leader_rows_aggregate_from_leg_rows(self):
        rows = snapshot._build_daily_leader_rows_from_leg_rows(
            [
                {
                    "date_key": "2026-04-09",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-a",
                    "token_id": "tok-a-yes",
                    "realized_pnl_delta": 7.0,
                    "unrealized_pnl_delta": 30.0,
                },
                {
                    "date_key": "2026-04-09",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-a",
                    "token_id": "tok-a-no",
                    "realized_pnl_delta": 3.0,
                    "unrealized_pnl_delta": -5.0,
                },
                {
                    "date_key": "2026-04-09",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-b",
                    "token_id": "tok-b-yes",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 12.0,
                },
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date_key"], "2026-04-09")
        self.assertEqual(rows[0]["leader_address"], "0xleader")
        self.assertEqual(rows[0]["account_name"], "acct")
        self.assertAlmostEqual(float(rows[0]["realized_pnl"]), 10.0)
        self.assertAlmostEqual(float(rows[0]["unrealized_pnl"]), 37.0)
        self.assertAlmostEqual(float(rows[0]["total_pnl"]), 47.0)
        self.assertEqual(int(rows[0]["market_count"]), 2)

    def test_force_rebuild_preserves_post_cutover_open_history(self):
        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-07",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 99.0,
                    "total_pnl": 99.0,
                    "market_count": 1,
                },
                {
                    "date_key": "2026-04-08",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 50.0,
                    "total_pnl": 50.0,
                    "market_count": 1,
                },
                {
                    "date_key": "2026-04-09",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 30.0,
                    "total_pnl": 30.0,
                    "market_count": 1,
                },
            ]
        )
        trade_id = self._insert_trade(
            token_id="tok-dual-rebuild",
            condition_id="cond-dual-rebuild",
            market_slug="nba-bos-lal-2026-04-09-moneyline-home",
            exit_status="exited",
            exit_price=0.7,
            exit_usd=7.0,
            profit=7.0,
            our_size=0.0,
            our_usd=0.0,
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-09T02:00:00+00:00", trade_id),
        )
        self.db.conn.commit()

        snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
            self.db,
            "2026-04-10",
            force_rebuild=True,
        )

        rows = self._daily_rows()
        by_date = {row["date_key"]: row for row in rows}
        self.assertNotIn("2026-04-07", by_date)
        self.assertAlmostEqual(float(by_date["2026-04-08"]["realized_pnl"]), 0.0)
        self.assertAlmostEqual(float(by_date["2026-04-08"]["unrealized_pnl"]), 50.0)
        self.assertAlmostEqual(float(by_date["2026-04-08"]["total_pnl"]), 50.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["realized_pnl"]), 7.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["unrealized_pnl"]), 30.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["total_pnl"]), 37.0)
        self.assertEqual(self._ct_meta_value("daily_leader_dual_basis_mode"), "dual_basis_v2")
        self.assertEqual(self._ct_meta_value("daily_leader_open_cutover_date"), "2026-04-08")

    def test_force_rebuild_filters_stale_preserved_unrealized_by_leg(self):
        self._insert_trade(
            token_id="tok-stale-preserved",
            condition_id="cond-stale-preserved",
            market_slug="market-stale-preserved",
            updated_at="2026-04-10T00:00:00+00:00",
        )
        self._insert_trade(
            token_id="tok-live-preserved",
            condition_id="cond-live-preserved",
            market_slug="market-live-preserved",
            updated_at="2026-04-12T00:00:00+00:00",
        )

        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 30.0,
                    "total_pnl": 30.0,
                    "market_count": 2,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 35.0,
                    "total_pnl": 35.0,
                    "market_count": 2,
                },
            ]
        )
        self.db.replace_daily_leader_market_leg_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-stale-preserved",
                    "token_id": "tok-stale-preserved",
                    "market_slug": "market-stale-preserved",
                    "outcome": "YES",
                    "buy_fill_count": 1,
                    "buy_size": 10.0,
                    "buy_cost_usd": 4.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 30.0,
                    "total_pnl_delta": 30.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 30.0,
                    "total_pnl_eod": 30.0,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-stale-preserved",
                    "token_id": "tok-stale-preserved",
                    "market_slug": "market-stale-preserved",
                    "outcome": "YES",
                    "buy_fill_count": 0,
                    "buy_size": 0.0,
                    "buy_cost_usd": 0.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 30.0,
                    "total_pnl_delta": 30.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 60.0,
                    "total_pnl_eod": 60.0,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-live-preserved",
                    "token_id": "tok-live-preserved",
                    "market_slug": "market-live-preserved",
                    "outcome": "YES",
                    "buy_fill_count": 0,
                    "buy_size": 0.0,
                    "buy_cost_usd": 0.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 5.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 5.0,
                    "total_pnl_delta": 5.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 5.0,
                    "total_pnl_eod": 5.0,
                },
            ]
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(
                 snapshot,
                 "_fetch_onchain_positions",
                 return_value={"tok-live-preserved": {"unrealized_pnl": 12.0}},
             ):
            snapshot._migrate_daily_leader_pnl_to_pure_attribution_once(
                self.db,
                "2026-04-12",
                force_rebuild=True,
            )

        by_date = {row["date_key"]: row for row in self._daily_rows()}
        self.assertAlmostEqual(float(by_date["2026-04-10"]["unrealized_pnl"]), 30.0)
        self.assertAlmostEqual(float(by_date["2026-04-11"]["unrealized_pnl"]), 5.0)
        self.assertEqual(int(by_date["2026-04-11"]["market_count"]), 2)

    def test_market_leg_rebuild_stops_stale_open_leg_after_updated_day(self):
        self._insert_trade(
            token_id="tok-stale-open",
            condition_id="cond-stale-open",
            market_slug="market-stale-open",
            updated_at="2026-04-10T00:00:00+00:00",
        )
        self._insert_trade(
            token_id="tok-live-open",
            condition_id="cond-live-open",
            market_slug="market-live-open",
            updated_at="2026-04-12T00:00:00+00:00",
        )

        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 30.0,
                    "total_pnl": 30.0,
                    "market_count": 2,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 5.0,
                    "total_pnl": 5.0,
                    "market_count": 2,
                },
                {
                    "date_key": "2026-04-12",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 2.0,
                    "total_pnl": 2.0,
                    "market_count": 2,
                },
            ]
        )
        self.db.replace_daily_leader_market_leg_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-stale-open",
                    "token_id": "tok-stale-open",
                    "market_slug": "market-stale-open",
                    "outcome": "YES",
                    "buy_fill_count": 1,
                    "buy_size": 10.0,
                    "buy_cost_usd": 4.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 30.0,
                    "total_pnl_delta": 30.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 30.0,
                    "total_pnl_eod": 30.0,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-stale-open",
                    "token_id": "tok-stale-open",
                    "market_slug": "market-stale-open",
                    "outcome": "YES",
                    "buy_fill_count": 0,
                    "buy_size": 0.0,
                    "buy_cost_usd": 0.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 30.0,
                    "total_pnl_delta": 30.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 60.0,
                    "total_pnl_eod": 60.0,
                },
            ]
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(
                 snapshot,
                 "_fetch_onchain_positions",
                 return_value={"tok-live-open": {"unrealized_pnl": 7.0}},
             ), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({}, {}, set(), 0, 2),
             ):
            leg_rows = snapshot._rebuild_daily_leader_market_leg_pnl(
                self.db,
                current_date_key="2026-04-12",
            )

        stale_rows = [
            row for row in leg_rows
            if row["token_id"] == "tok-stale-open"
        ]
        self.assertEqual(
            [row["date_key"] for row in stale_rows],
            ["2026-04-01", "2026-04-10"],
        )
        self.assertFalse(
            any(
                row["token_id"] == "tok-stale-open" and row["date_key"] > "2026-04-10"
                for row in leg_rows
            )
        )

    def test_market_leg_rebuild_preserves_open_history_and_backfills_closed(self):
        self._insert_trade(
            token_id="tok-open-leg",
            condition_id="cond-open-leg",
            market_slug="nba-open-leg",
            created_at="2026-04-08T00:00:00+00:00",
            updated_at="2026-04-09T00:00:00+00:00",
        )
        closed_trade_id = self._insert_trade(
            token_id="tok-closed-leg",
            condition_id="cond-closed-leg",
            market_slug="nba-closed-leg",
            created_at="2026-04-07T00:00:00+00:00",
            updated_at="2026-04-09T02:00:00+00:00",
            exit_status="exited",
            exit_price=0.7,
            exit_usd=7.0,
            profit=7.0,
            our_size=0.0,
            our_usd=0.0,
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-09T02:00:00+00:00", closed_trade_id),
        )
        self.db.conn.commit()

        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-08",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 50.0,
                    "total_pnl": 50.0,
                    "market_count": 1,
                },
                {
                    "date_key": "2026-04-09",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 7.0,
                    "unrealized_pnl": 30.0,
                    "total_pnl": 37.0,
                    "market_count": 2,
                },
            ]
        )
        self.db.replace_daily_leader_market_leg_pnl(
            [
                {
                    "date_key": "2026-04-08",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-open-leg",
                    "token_id": "tok-open-leg",
                    "market_slug": "nba-open-leg",
                    "outcome": "YES",
                    "buy_fill_count": 1,
                    "buy_size": 10.0,
                    "buy_cost_usd": 4.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "open",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 50.0,
                    "total_pnl_delta": 50.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": 50.0,
                    "total_pnl_eod": 50.0,
                }
            ]
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(
                 snapshot,
                 "_fetch_onchain_positions",
                 return_value={"tok-open-leg": {"unrealized_pnl": 80.0}},
             ), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({}, {}, set(), 0, 0),
             ):
            leg_rows = snapshot._rebuild_daily_leader_market_leg_pnl(
                self.db,
                current_date_key="2026-04-09",
            )

        rows_by_key = {
            (row["date_key"], row["token_id"]): row
            for row in leg_rows
        }
        open_day_one = rows_by_key[("2026-04-08", "tok-open-leg")]
        open_day_two = rows_by_key[("2026-04-09", "tok-open-leg")]
        closed_day_two = rows_by_key[("2026-04-09", "tok-closed-leg")]

        self.assertAlmostEqual(float(open_day_one["unrealized_pnl_delta"]), 50.0)
        self.assertAlmostEqual(float(open_day_one["unrealized_pnl_eod"]), 50.0)
        self.assertAlmostEqual(float(open_day_two["unrealized_pnl_delta"]), 30.0)
        self.assertAlmostEqual(float(open_day_two["unrealized_pnl_eod"]), 80.0)
        self.assertAlmostEqual(float(open_day_two["realized_pnl_delta"]), 0.0)
        self.assertAlmostEqual(float(closed_day_two["realized_pnl_delta"]), 7.0)
        self.assertAlmostEqual(float(closed_day_two["unrealized_pnl_delta"]), 0.0)
        day_two_rows = [row for row in leg_rows if row["date_key"] == "2026-04-09"]
        self.assertAlmostEqual(sum(float(row["realized_pnl_delta"]) for row in day_two_rows), 7.0)
        self.assertAlmostEqual(sum(float(row["unrealized_pnl_delta"]) for row in day_two_rows), 30.0)
        self.assertAlmostEqual(sum(float(row["total_pnl_delta"]) for row in day_two_rows), 37.0)

        daily_rows = snapshot._build_daily_leader_rows_from_leg_rows(leg_rows)
        by_date = {row["date_key"]: row for row in daily_rows}
        self.assertAlmostEqual(float(by_date["2026-04-08"]["realized_pnl"]), 0.0)
        self.assertAlmostEqual(float(by_date["2026-04-08"]["unrealized_pnl"]), 50.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["realized_pnl"]), 7.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["unrealized_pnl"]), 30.0)
        self.assertAlmostEqual(float(by_date["2026-04-09"]["total_pnl"]), 37.0)

    def test_current_day_exact_eod_skips_settled_history_residual(self):
        self._insert_trade(
            token_id="tok-redeemable",
            condition_id="cond-redeemable",
            market_slug="market-redeemable",
            created_at="2026-04-10T00:00:00+00:00",
            updated_at="2026-04-10T00:00:00+00:00",
        )

        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": -50.0,
                    "total_pnl": -50.0,
                    "market_count": 1,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "total_pnl": 0.0,
                    "market_count": 1,
                },
                {
                    "date_key": "2026-04-12",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 100.0,
                    "total_pnl": 100.0,
                    "market_count": 1,
                },
            ]
        )
        self.db.replace_daily_leader_market_leg_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-redeemable",
                    "token_id": "tok-redeemable",
                    "market_slug": "market-redeemable",
                    "outcome": "YES",
                    "buy_fill_count": 1,
                    "buy_size": 10.0,
                    "buy_cost_usd": 4.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "redeemable",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": -50.0,
                    "total_pnl_delta": -50.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": -50.0,
                    "total_pnl_eod": -50.0,
                },
                {
                    "date_key": "2026-04-11",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-redeemable",
                    "token_id": "tok-redeemable",
                    "market_slug": "market-redeemable",
                    "outcome": "YES",
                    "buy_fill_count": 0,
                    "buy_size": 0.0,
                    "buy_cost_usd": 0.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "redeemable",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": 0.0,
                    "total_pnl_delta": 0.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": -50.0,
                    "total_pnl_eod": -50.0,
                },
            ]
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(
                 snapshot,
                 "_fetch_onchain_positions",
                 return_value={"tok-redeemable": {"unrealized_pnl": -3.0}},
             ), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({}, {"tok-redeemable": "2026-04-10T01:00:00+00:00"}, set(), 0, 1),
             ):
            leg_rows = snapshot._rebuild_daily_leader_market_leg_pnl(
                self.db,
                current_date_key="2026-04-12",
            )

        current_row = next(
            row
            for row in leg_rows
            if row["date_key"] == "2026-04-12" and row["token_id"] == "tok-redeemable"
        )
        self.assertAlmostEqual(float(current_row["unrealized_pnl_delta"]), 0.0)
        self.assertAlmostEqual(float(current_row["unrealized_pnl_eod"]), -3.0)
        self.assertAlmostEqual(float(current_row["total_pnl_eod"]), -3.0)
        self.assertEqual(current_row["close_state_eod"], "redeemable")

    def test_realized_day_rebuild_drops_preserved_open_row(self):
        trade_id = self._insert_trade(
            token_id="tok-settled",
            condition_id="cond-settled",
            market_slug="market-settled",
            created_at="2026-04-10T00:00:00+00:00",
            updated_at="2026-04-10T00:00:00+00:00",
            exit_status="exited",
            exit_price=0.0,
            exit_usd=0.0,
            profit=-3.0,
            our_size=0.0,
            our_usd=0.0,
            official_settlement_at="2026-04-10T01:00:00+00:00",
        )
        self.db.conn.execute(
            "UPDATE ct_trades SET exit_at=? WHERE id=?",
            ("2026-04-10T01:00:00+00:00", trade_id),
        )
        self.db.conn.commit()

        self.db.upsert_daily_leader_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "realized_pnl": -3.0,
                    "unrealized_pnl": 0.0,
                    "total_pnl": -3.0,
                    "market_count": 1,
                },
            ]
        )
        self.db.replace_daily_leader_market_leg_pnl(
            [
                {
                    "date_key": "2026-04-10",
                    "leader_address": "0xleader",
                    "account_name": "acct",
                    "condition_id": "cond-settled",
                    "token_id": "tok-settled",
                    "market_slug": "market-settled",
                    "outcome": "YES",
                    "buy_fill_count": 1,
                    "buy_size": 10.0,
                    "buy_cost_usd": 4.0,
                    "sell_fill_count": 0,
                    "sell_size": 0.0,
                    "sell_proceeds_usd": 0.0,
                    "settled_size": 0.0,
                    "open_size_eod": 10.0,
                    "close_state_eod": "redeemable",
                    "realized_pnl_delta": 0.0,
                    "unrealized_pnl_delta": -3.0,
                    "total_pnl_delta": -3.0,
                    "realized_pnl_eod": 0.0,
                    "unrealized_pnl_eod": -3.0,
                    "total_pnl_eod": -3.0,
                }
            ]
        )

        with patch.object(snapshot, "_load_account_addresses", return_value={"acct": "0xfunder"}), \
             patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(
                 snapshot,
                 "_resolve_tokens_with_cache_and_live",
                 return_value=({}, {}, set(), 0, 0),
             ):
            leg_rows = snapshot._rebuild_daily_leader_market_leg_pnl(
                self.db,
                current_date_key="2026-04-12",
            )

        row = next(
            item
            for item in leg_rows
            if item["date_key"] == "2026-04-10" and item["token_id"] == "tok-settled"
        )
        self.assertAlmostEqual(float(row["settled_size"]), 10.0)
        self.assertAlmostEqual(float(row["open_size_eod"]), 0.0)
        self.assertAlmostEqual(float(row["realized_pnl_delta"]), -3.0)
        self.assertAlmostEqual(float(row["unrealized_pnl_delta"]), 0.0)
        self.assertAlmostEqual(float(row["total_pnl_eod"]), -3.0)
        self.assertEqual(row["close_state_eod"], "settled")


class ResolutionFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = CopyTradeDB(str(Path(self.tmpdir.name) / "copytrade.sqlite"))

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def test_resolve_tokens_falls_back_to_event_slug_and_caches_result(self):
        token_id = "tok-event-fallback"
        settlement_time = "2026-04-09T00:00:51+00:00"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                self.assertEqual(params, {"clob_token_ids": token_id, "limit": 1})
                return []
            if url.endswith("/events"):
                self.assertEqual(params, {"slug": "btc-updown-15m-1775691900", "limit": 1})
                return [
                    {
                        "slug": "btc-updown-15m-1775691900",
                        "markets": [
                            {
                                "slug": "btc-updown-15m-1775691900",
                                "conditionId": "cond-1",
                                "closed": True,
                                "closedTime": settlement_time,
                                "clobTokenIds": json.dumps([token_id, "tok-other"]),
                                "outcomePrices": json.dumps(["0", "1"]),
                            }
                        ],
                    }
                ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, live_resolved, live_attempted = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "btc-updown-15m-1775691900",
                        "condition_id": "cond-1",
                    }
                },
            )

        self.assertEqual(prices[token_id], 0.0)
        self.assertEqual(settlement_times[token_id], settlement_time)
        self.assertEqual(unresolved, set())
        self.assertEqual(live_resolved, 1)
        self.assertEqual(live_attempted, 1)

        cache_row = self.db.conn.execute(
            "SELECT resolution_price, settlement_time FROM ct_resolved_prices WHERE token_id=?",
            (token_id,),
        ).fetchone()
        self.assertIsNotNone(cache_row)
        self.assertEqual(float(cache_row["resolution_price"]), 0.0)
        self.assertEqual(cache_row["settlement_time"], settlement_time)

    def test_open_market_end_date_does_not_count_as_settlement_time(self):
        token_id = "tok-open-market"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return [
                    {
                        "slug": "spl-njm-neo-2026-04-10-neo",
                        "conditionId": "cond-open-market",
                        "closed": False,
                        "endDate": "2026-04-10T16:00:00Z",
                        "clobTokenIds": json.dumps([token_id]),
                        "outcomePrices": json.dumps(["0.0005"]),
                    }
                ]
            if url.endswith("/events"):
                return []
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, live_resolved, live_attempted = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "spl-njm-neo-2026-04-10-neo",
                        "condition_id": "cond-open-market",
                    }
                },
                fetch_live_for_cached=True,
            )

        self.assertEqual(prices, {})
        self.assertEqual(settlement_times, {})
        self.assertEqual(unresolved, {token_id})
        self.assertEqual(live_resolved, 0)
        self.assertEqual(live_attempted, 1)

    def test_event_fallback_can_match_condition_inside_parent_event(self):
        token_id = "tok-parent-condition"
        settlement_time = "2026-04-09T00:28:21+00:00"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return []
            if url.endswith("/events"):
                self.assertEqual(params, {"slug": "us-forces-enter-iran-by", "limit": 1})
                return [
                    {
                        "slug": "us-forces-enter-iran-by",
                        "markets": [
                            {
                                "slug": "us-forces-enter-iran-by-april-30-899",
                                "conditionId": "cond-parent-match",
                                "closed": True,
                                "closedTime": settlement_time,
                                "clobTokenIds": json.dumps(["tok-other", token_id]),
                                "outcomePrices": json.dumps(["1", "0"]),
                            }
                        ],
                    }
                ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, _, _ = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "us-forces-enter-iran-by",
                        "condition_id": "cond-parent-match",
                    }
                },
            )

        self.assertEqual(prices[token_id], 0.0)
        self.assertEqual(settlement_times[token_id], settlement_time)
        self.assertEqual(unresolved, set())

    def test_event_fallback_tries_parent_event_slug_for_market_slug(self):
        token_id = "tok-parent-slug"
        settlement_time = "2026-04-09T00:28:21+00:00"
        event_calls = []

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return []
            if url.endswith("/events"):
                event_calls.append(dict(params or {}))
                if params == {"slug": "us-forces-enter-iran-by-april-30-899", "limit": 1}:
                    return []
                if params == {"slug": "us-forces-enter-iran-by-april-30", "limit": 1}:
                    return []
                if params == {"slug": "us-forces-enter-iran-by", "limit": 1}:
                    return [
                        {
                            "slug": "us-forces-enter-iran-by",
                            "markets": [
                                {
                                    "slug": "us-forces-enter-iran-by-april-30-899",
                                    "conditionId": "cond-parent-slug",
                                    "closed": True,
                                    "closedTime": settlement_time,
                                    "clobTokenIds": json.dumps([token_id]),
                                    "outcomePrices": json.dumps(["0"]),
                                }
                            ],
                        }
                    ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, _, _ = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "us-forces-enter-iran-by-april-30-899",
                        "condition_id": "cond-parent-slug",
                    }
                },
            )

        self.assertEqual(prices[token_id], 0.0)
        self.assertEqual(settlement_times[token_id], settlement_time)
        self.assertEqual(unresolved, set())
        self.assertEqual(
            event_calls,
            [
                {"slug": "us-forces-enter-iran-by-april-30-899", "limit": 1},
                {"slug": "us-forces-enter-iran-by-april-30", "limit": 1},
                {"slug": "us-forces-enter-iran-by-april", "limit": 1},
                {"slug": "us-forces-enter-iran-by", "limit": 1},
            ],
        )

    def test_event_fallback_uses_exact_market_slug_only(self):
        token_id = "tok-correct"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return []
            if url.endswith("/events"):
                return [
                    {
                        "slug": "btc-updown-4h-1775664000",
                        "markets": [
                            {
                                "slug": "btc-updown-4h-1775664000-shadow",
                                "conditionId": "cond-1",
                                "closed": True,
                                "closedTime": "2026-04-08T20:00:00+00:00",
                                "clobTokenIds": json.dumps([token_id]),
                                "outcomePrices": json.dumps(["1"]),
                            },
                            {
                                "slug": "btc-updown-4h-1775664000",
                                "conditionId": "cond-1",
                                "closed": True,
                                "closedTime": "2026-04-08T20:01:19+00:00",
                                "clobTokenIds": json.dumps([token_id]),
                                "outcomePrices": json.dumps(["0"]),
                            },
                        ],
                    }
                ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, _, _ = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "btc-updown-4h-1775664000",
                        "condition_id": "cond-1",
                    }
                },
            )

        self.assertEqual(prices[token_id], 0.0)
        self.assertEqual(settlement_times[token_id], "2026-04-08T20:01:19+00:00")
        self.assertEqual(unresolved, set())

    def test_event_fallback_rejects_condition_mismatch(self):
        token_id = "tok-mismatch"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return []
            if url.endswith("/events"):
                return [
                    {
                        "slug": "btc-updown-4h-1775664000",
                        "markets": [
                            {
                                "slug": "btc-updown-4h-1775664000",
                                "conditionId": "cond-other",
                                "closed": True,
                                "closedTime": "2026-04-08T20:01:19+00:00",
                                "clobTokenIds": json.dumps([token_id]),
                                "outcomePrices": json.dumps(["1"]),
                            }
                        ],
                    }
                ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, _, _ = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "btc-updown-4h-1775664000",
                        "condition_id": "cond-expected",
                    }
                },
            )

        self.assertEqual(prices, {})
        self.assertEqual(settlement_times, {})
        self.assertEqual(unresolved, {token_id})

    def test_event_fallback_requires_settlement_time(self):
        token_id = "tok-missing-settlement"

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                return []
            if url.endswith("/events"):
                return [
                    {
                        "slug": "btc-updown-4h-1775664000",
                        "markets": [
                            {
                                "slug": "btc-updown-4h-1775664000",
                                "conditionId": "cond-1",
                                "closed": True,
                                "clobTokenIds": json.dumps([token_id]),
                                "outcomePrices": json.dumps(["1"]),
                            }
                        ],
                    }
                ]
            raise AssertionError(f"unexpected call url={url} params={params}")

        with patch.object(snapshot, "http_get_json", side_effect=fake_http_get_json):
            prices, settlement_times, unresolved, _, _ = snapshot._resolve_tokens_with_cache_and_live(
                self.db,
                requests.Session(),
                [token_id],
                token_context_map={
                    token_id: {
                        "market_slug": "btc-updown-4h-1775664000",
                        "condition_id": "cond-1",
                    }
                },
            )

        self.assertEqual(prices, {})
        self.assertEqual(settlement_times, {})
        self.assertEqual(unresolved, {token_id})


if __name__ == "__main__":
    unittest.main()
