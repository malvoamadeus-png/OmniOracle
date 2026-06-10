import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import copytrade.build_leader_pnl_snapshot as snapshot
from copytrade.db import CopyTradeDB


class DailyCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db = CopyTradeDB(str(Path(self.tmpdir.name) / "copytrade.sqlite"))
        self.accounts_dir = Path(self.tmpdir.name) / "accounts"
        self.accounts_dir.mkdir()

    def tearDown(self):
        self.db.close()
        self.tmpdir.cleanup()

    def _write_account(self, name: str, leaders: list[str]) -> None:
        body = "leader_addresses = [\n" + "".join(f'  "{leader}",\n' for leader in leaders) + "]\n"
        (self.accounts_dir / f"{name}.toml").write_text(body, encoding="utf-8")

    def test_resolve_compare_accounts_defaults_to_all_account_files(self):
        self._write_account("main", ["0xleader"])
        self._write_account("pm-2", ["0xleader"])
        (self.accounts_dir / "_defaults.toml").write_text("leader_addresses = []\n", encoding="utf-8")

        accounts = snapshot._resolve_compare_accounts("", accounts_dir=self.accounts_dir)

        self.assertEqual(accounts, ["main", "pm-2"])

    def _insert_trade(self, *, account_name: str = "main", leader_address: str = "0xleader", token_id: str = "tok-1",
                      condition_id: str = "cond-1", market_slug: str = "market-1", outcome: str = "YES",
                      created_at: str = "2026-04-10T02:00:00+00:00", price: float = 0.4, size: float = 10.0,
                      usd: float = 4.0, status: str = "filled", exit_status: str = "open", exit_at: str | None = None,
                      official_settlement_at: str | None = None, exit_price: float | None = None,
                      exit_usd: float | None = None) -> int:
        trade_id = self.db.insert_trade({
            "account_name": account_name,
            "leader_address": leader_address,
            "leader_tx_hash": f"tx-{leader_address}-{token_id}-{created_at}",
            "leader_fill_key": f"fill-{leader_address}-{token_id}-{created_at}",
            "leader_side": "BUY",
            "leader_price": price,
            "leader_size": size,
            "leader_usd": usd,
            "our_order_id": f"order-{leader_address}-{token_id}-{created_at}",
            "our_side": "BUY",
            "our_price": price,
            "our_size": size,
            "our_usd": usd,
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "outcome": outcome,
            "status": status,
            "exit_status": exit_status,
            "requested_price": price,
            "requested_size": size,
            "requested_usd": usd,
            "filled_size_actual": size,
            "filled_usd_actual": usd,
            "official_settlement_at": official_settlement_at,
        })
        self.db.conn.execute(
            "UPDATE ct_trades SET created_at=?, updated_at=?, exit_at=?, exit_price=?, exit_usd=?, exit_status=?, official_settlement_at=? WHERE id=?",
            (
                created_at,
                created_at,
                exit_at,
                exit_price,
                exit_usd,
                exit_status,
                official_settlement_at,
                trade_id,
            ),
        )
        self.db.conn.commit()
        return trade_id

    def _insert_leader_activity(self, *, leader_address: str = "0xleader", side: str = "BUY", token_id: str = "tok-1",
                                condition_id: str = "cond-1", market_slug: str = "market-1", outcome: str = "YES",
                                price: float = 0.4, size: float = 10.0, usd: float = 4.0,
                                timestamp_utc: str = "2026-04-10T02:00:00+00:00") -> None:
        dt = datetime.fromisoformat(timestamp_utc)
        self.db.insert_leader_activities([{
            "leader_address": leader_address,
            "tx_hash": f"{leader_address}-{side}-{token_id}-{timestamp_utc}",
            "timestamp_utc": timestamp_utc,
            "ts_epoch": int(dt.timestamp()),
            "side": side,
            "token_id": token_id,
            "condition_id": condition_id,
            "market_slug": market_slug,
            "outcome": outcome,
            "price": price,
            "size": size,
            "usd": usd,
        }])

    def _replace_open_baseline(self, *, scope_kind: str, rows: list[dict], date_key: str = "2026-04-10") -> None:
        self.db.replace_compare_open_leg_state(
            date_key=date_key,
            account_names=["main"],
            rows=[
                {
                    "date_key": date_key,
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": scope_kind,
                    **row,
                }
                for row in rows
            ],
        )

    def _summary_rows(self):
        return self.db.conn.execute(
            "SELECT * FROM ct_compare_daily_summary ORDER BY account_name, leader_address"
        ).fetchall()

    def _market_rows(self):
        return self.db.conn.execute(
            "SELECT * FROM ct_compare_daily_market_leg ORDER BY account_name, leader_address, condition_id, token_id"
        ).fetchall()

    def _open_rows(self):
        return self.db.conn.execute(
            "SELECT * FROM ct_compare_open_leg_state ORDER BY account_name, leader_address, scope_kind, condition_id, token_id"
        ).fetchall()

    def _set_compare_mode(self, value: str = snapshot._DAILY_COMPARE_MODE) -> None:
        snapshot._ensure_ct_meta_table(self.db)
        snapshot._upsert_ct_meta(self.db, "daily_compare_mode", value)

    def test_leader_moving_average_sell_uses_bod_baseline(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.5,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 1.0,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": None,
                "mark_price_source": "bod",
            }],
        )
        self._insert_leader_activity(
            side="SELL",
            size=4.0,
            usd=3.0,
            price=0.75,
            timestamp_utc="2026-04-10T03:00:00+00:00",
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            stats = snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(stats["summary_rows"], 1)
        row = self._market_rows()[0]
        self.assertAlmostEqual(float(row["leader_realized_pnl"]), 1.4, places=6)
        self.assertAlmostEqual(float(row["leader_unrealized_change"]), -0.4, places=6)
        self.assertAlmostEqual(float(row["leader_total_pnl"]), 1.0, places=6)
        open_row = [r for r in self._open_rows() if r["scope_kind"] == "leader"][0]
        self.assertAlmostEqual(float(open_row["open_size"]), 6.0, places=6)
        self.assertAlmostEqual(float(open_row["open_cost"]), 2.4, places=6)

    def test_existing_same_day_baseline_repairs_missing_unrealized_bod(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            date_key="2026-04-09",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.45,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 0.5,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": "2026-04-09T03:00:00+00:00",
                "mark_price_source": "carry",
            }],
        )
        self._replace_open_baseline(
            scope_kind="leader",
            date_key="2026-04-10",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.4,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 0.0,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": "2026-04-09T03:00:00+00:00",
                "mark_price_source": "carry",
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            stats = snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(stats["summary_rows"], 0)
        self.assertEqual(self._summary_rows(), [])
        self.assertEqual(self._market_rows(), [])
        open_row = [
            r for r in self._open_rows()
            if r["scope_kind"] == "leader" and r["date_key"] == "2026-04-10"
        ][0]
        self.assertAlmostEqual(float(open_row["bod_mark_price"]), 0.5, places=6)
        self.assertAlmostEqual(float(open_row["unrealized_bod"]), 1.0, places=6)
        self.assertAlmostEqual(float(open_row["unrealized_now"]), 1.0, places=6)

    def test_previous_day_carryover_uses_prior_eod_unrealized_as_bod(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            date_key="2026-04-09",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.45,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 0.5,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": "2026-04-09T03:00:00+00:00",
                "mark_price_source": "carry",
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            stats = snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(stats["summary_rows"], 0)
        self.assertEqual(self._summary_rows(), [])
        self.assertEqual(self._market_rows(), [])
        open_row = [
            r for r in self._open_rows()
            if r["scope_kind"] == "leader" and r["date_key"] == "2026-04-10"
        ][0]
        self.assertAlmostEqual(float(open_row["bod_mark_price"]), 0.5, places=6)
        self.assertAlmostEqual(float(open_row["unrealized_bod"]), 1.0, places=6)
        self.assertEqual(open_row["last_event_ts"], "2026-04-09T03:00:00+00:00")

    def test_our_baseline_rebuild_prefers_ct_trades_over_stale_carryover_cost(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._insert_trade(
            created_at="2026-04-09T12:00:00+00:00",
            price=0.4,
            size=10.0,
            usd=4.0,
        )
        stale_row = {
            "condition_id": "cond-1",
            "token_id": "tok-1",
            "market_slug": "market-1",
            "outcome": "YES",
            "bod_open_size": 10.0,
            "bod_open_cost": 9.5,
            "bod_avg_open_price": 0.95,
            "bod_mark_price": 0.5,
            "open_size": 10.0,
            "open_cost": 9.5,
            "avg_open_price": 0.95,
            "unrealized_bod": -4.5,
            "mark_price_now": 0.5,
            "unrealized_now": -4.5,
            "realized_pnl": 0.0,
            "status": "open",
            "settlement_time": "2026-05-01T00:00:00+00:00",
            "last_event_ts": "2026-04-09T23:59:00+00:00",
            "mark_price_source": "carry",
            "bod_cumulative_buy_fill_count": 1,
            "bod_cumulative_buy_size": 10.0,
            "bod_cumulative_buy_usd": 9.5,
            "cumulative_buy_fill_count": 1,
            "cumulative_buy_size": 10.0,
            "cumulative_buy_usd": 9.5,
        }
        self._replace_open_baseline(
            scope_kind="our",
            date_key="2026-04-09",
            rows=[stale_row],
        )
        self._replace_open_baseline(
            scope_kind="our",
            date_key="2026-04-10",
            rows=[stale_row],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        open_row = [
            r for r in self._open_rows()
            if r["scope_kind"] == "our" and r["date_key"] == "2026-04-10"
        ][0]
        self.assertAlmostEqual(float(open_row["bod_open_cost"]), 4.0, places=6)
        self.assertAlmostEqual(float(open_row["open_cost"]), 4.0, places=6)
        self.assertAlmostEqual(float(open_row["bod_avg_open_price"]), 0.4, places=6)
        self.assertAlmostEqual(float(open_row["avg_open_price"]), 0.4, places=6)
        self.assertAlmostEqual(float(open_row["bod_cumulative_buy_usd"]), 4.0, places=6)
        self.assertAlmostEqual(float(open_row["cumulative_buy_usd"]), 4.0, places=6)

    def test_our_prebaseline_trade_rebuild_uses_previous_eod_mark_as_bod(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._insert_trade(
            created_at="2026-04-09T14:00:00+00:00",
            price=0.4,
            size=10.0,
            usd=4.0,
        )
        self._insert_trade(
            created_at="2026-04-09T15:30:00+00:00",
            price=0.4,
            size=5.0,
            usd=2.0,
        )
        self._replace_open_baseline(
            scope_kind="our",
            date_key="2026-04-09",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.45,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 0.5,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": "2026-04-09T14:00:00+00:00",
                "mark_price_source": "midpoint",
                "bod_cumulative_buy_fill_count": 1,
                "bod_cumulative_buy_size": 10.0,
                "bod_cumulative_buy_usd": 4.0,
                "cumulative_buy_fill_count": 1,
                "cumulative_buy_size": 10.0,
                "cumulative_buy_usd": 4.0,
            }],
        )
        self._replace_open_baseline(
            scope_kind="our",
            date_key="2026-04-10",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 15.0,
                "bod_open_cost": 6.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.2,
                "open_size": 15.0,
                "open_cost": 6.0,
                "avg_open_price": 0.4,
                "unrealized_bod": -3.0,
                "mark_price_now": 0.2,
                "unrealized_now": -3.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": "2026-04-09T15:30:00+00:00",
                "mark_price_source": "midpoint",
                "bod_cumulative_buy_fill_count": 2,
                "bod_cumulative_buy_size": 15.0,
                "bod_cumulative_buy_usd": 6.0,
                "cumulative_buy_fill_count": 2,
                "cumulative_buy_size": 15.0,
                "cumulative_buy_usd": 6.0,
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-1": 0.2}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(int(row["our_buy_fill_count"]), 2)
        self.assertAlmostEqual(float(row["our_buy_usd"]), 6.0, places=6)
        self.assertAlmostEqual(float(row["our_unrealized_change"]), -4.5, places=6)
        self.assertAlmostEqual(float(row["our_total_pnl"]), -4.5, places=6)
        open_row = [r for r in self._open_rows() if r["scope_kind"] == "our" and r["date_key"] == "2026-04-10"][0]
        self.assertAlmostEqual(float(open_row["bod_mark_price"]), 0.5, places=6)
        self.assertAlmostEqual(float(open_row["unrealized_bod"]), 1.5, places=6)
        self.assertAlmostEqual(float(open_row["unrealized_now"]), -3.0, places=6)

    def test_ct_trades_side_builds_market_leg_and_count_gap(self):
        self._write_account("main", ["0xleader"])
        self._insert_leader_activity(side="BUY", size=10.0, usd=4.0, timestamp_utc="2026-04-10T02:00:00+00:00")
        self._insert_leader_activity(side="BUY", size=5.0, usd=2.1, price=0.42, timestamp_utc="2026-04-10T02:30:00+00:00")
        self._insert_trade(created_at="2026-04-10T01:50:00+00:00")

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(int(row["leader_buy_fill_count"]), 2)
        self.assertEqual(int(row["our_buy_fill_count"]), 1)
        self.assertEqual(row["primary_gap_reason"], "count_gap")

    def test_epoch_string_leader_activity_is_visible_same_day(self):
        self._write_account("main", ["0xleader"])
        event_dt = datetime(2026, 4, 10, 2, 0, tzinfo=timezone.utc)
        self.db.insert_leader_activities([{
            "leader_address": "0xleader",
            "tx_hash": "tx-epoch-buy",
            "timestamp_utc": str(int(event_dt.timestamp())),
            "ts_epoch": int(event_dt.timestamp()),
            "side": "BUY",
            "token_id": "tok-epoch",
            "condition_id": "cond-epoch",
            "market_slug": "market-epoch",
            "outcome": "YES",
            "price": 0.45,
            "size": 20.0,
            "usd": 9.0,
        }])

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-epoch": {"market_slug": "market-epoch", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-epoch": 0.47}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(row["token_id"], "tok-epoch")
        self.assertEqual(int(row["leader_buy_fill_count"]), 1)
        self.assertEqual(int(row["our_buy_fill_count"]), 0)

    def test_leader_follow_start_uses_first_observed_trade_not_first_filled_trade(self):
        self._write_account("main", ["0xleader"])
        self._insert_trade(
            created_at="2026-04-09T15:00:00+00:00",
            token_id="tok-anchor",
            condition_id="cond-anchor",
            market_slug="market-anchor",
            status="detected",
        )
        self._insert_leader_activity(
            token_id="tok-1",
            condition_id="cond-1",
            market_slug="market-1",
            timestamp_utc="2026-04-09T19:00:00+00:00",
            usd=4.0,
            size=10.0,
            price=0.4,
        )
        self._insert_leader_activity(
            token_id="tok-1",
            condition_id="cond-1",
            market_slug="market-1",
            timestamp_utc="2026-04-09T19:30:00+00:00",
            usd=2.0,
            size=5.0,
            price=0.4,
        )
        self._insert_trade(
            created_at="2026-04-09T22:00:00+00:00",
            token_id="tok-1",
            condition_id="cond-1",
            market_slug="market-1",
            usd=4.0,
            size=10.0,
            price=0.4,
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = next(r for r in self._market_rows() if r["token_id"] == "tok-1")
        self.assertEqual(int(row["leader_buy_fill_count"]), 3)
        self.assertEqual(int(row["our_buy_fill_count"]), 1)
        self.assertEqual(row["primary_gap_reason"], "count_gap")

    def test_leader_buys_fallback_to_ct_trades_when_activity_missing(self):
        self._write_account("main", ["0xleader"])
        self._insert_trade(
            created_at="2026-04-10T16:25:00+00:00",
            price=0.46,
            size=43.48,
            usd=20.0008,
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-1": 0.5}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 18, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(int(row["leader_buy_fill_count"]), 1)
        self.assertEqual(int(row["our_buy_fill_count"]), 1)
        self.assertAlmostEqual(float(row["leader_buy_usd"]), 20.0008, places=6)
        self.assertAlmostEqual(float(row["our_buy_usd"]), 20.0008, places=6)

    def test_resolution_settlement_locks_realized_and_marks_state_settled(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.5,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 1.0,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-04-10T08:00:00+00:00",
                "last_event_ts": None,
                "mark_price_source": "bod",
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-04-10T08:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({"tok-1": 1.0}, {"tok-1": "2026-04-10T08:00:00+00:00"}, set(), 1, 1)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertAlmostEqual(float(row["leader_realized_pnl"]), 6.0, places=6)
        self.assertAlmostEqual(float(row["leader_unrealized_change"]), -1.0, places=6)
        leader_open_rows = [r for r in self._open_rows() if r["scope_kind"] == "leader"]
        self.assertEqual(len(leader_open_rows), 1)
        self.assertEqual(leader_open_rows[0]["status"], "settled")
        self.assertAlmostEqual(float(leader_open_rows[0]["open_size"]), 0.0, places=6)

    def test_market_closed_resolution_does_not_settle_before_effective_time(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.5,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 1.0,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-04-11T08:00:00+00:00",
                "last_event_ts": None,
                "mark_price_source": "bod",
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-1": {
                "market_slug": "market-1",
                "outcome": "YES",
                "settlement_time": "2026-04-11T08:00:00+00:00",
                "market_closed": True,
                "resolution_price": 1.0,
            }
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(self._market_rows(), [])
        leader_open_rows = [r for r in self._open_rows() if r["scope_kind"] == "leader"]
        self.assertEqual(len(leader_open_rows), 1)
        self.assertEqual(leader_open_rows[0]["status"], "open")
        self.assertAlmostEqual(float(leader_open_rows[0]["open_size"]), 10.0, places=6)

    def test_long_dated_leg_is_excluded_and_old_rows_are_purged(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="leader",
            rows=[{
                "condition_id": "cond-1",
                "token_id": "tok-1",
                "market_slug": "market-1",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.6,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 2.0,
                "mark_price_now": 0.6,
                "unrealized_now": 2.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2028-06-01T00:00:00+00:00",
                "last_event_ts": None,
                "mark_price_source": "bod",
            }],
        )
        self.db.replace_compare_daily_summary(
            date_key="2026-03-20",
            account_names=["main"],
            rows=[{
                "date_key": "2026-03-20",
                "account_name": "main",
                "leader_address": "0xleader",
                "leader_total_pnl": 1.0,
                "our_total_pnl": 0.5,
                "delta_pnl": -0.5,
                "leader_excluded_pnl": 0.0,
                "our_excluded_pnl": 0.0,
                "visible_leader_pnl": 1.0,
                "visible_our_pnl": 0.5,
            }],
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2028-06-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(self._market_rows(), [])
        open_row = [r for r in self._open_rows() if r["scope_kind"] == "leader"][0]
        self.assertEqual(open_row["exclusion_reason"], "excluded_long_dated")
        old_row = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM ct_compare_daily_summary WHERE date_key='2026-03-20'"
        ).fetchone()
        self.assertEqual(int(old_row["n"]), 0)

    def test_only_selected_account_and_current_leaders_are_processed(self):
        self._write_account("main", ["0xleader-current"])
        self._write_account("pm-1", ["0xpm1leader"])
        self._insert_trade(leader_address="0xleader-old", created_at="2026-04-10T02:00:00+00:00")

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        rows = self._summary_rows()
        self.assertEqual(rows, [])
        missing = self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM ct_compare_daily_summary WHERE leader_address='0xleader-old' OR account_name='pm-1'"
        ).fetchone()
        self.assertEqual(int(missing["n"]), 0)

    def test_previous_day_open_state_carries_forward_without_full_history(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self.db.replace_compare_open_leg_state(
            date_key="2026-04-09",
            account_names=["main"],
            rows=[
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "our",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "mark_price_now": 0.55,
                    "unrealized_now": 1.5,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
            ],
        )

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-1": 0.6}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(self._market_rows(), [])
        open_row = [r for r in self._open_rows() if r["scope_kind"] == "our"][0]
        self.assertAlmostEqual(float(open_row["open_size"]), 10.0, places=6)

    def test_previous_day_carryover_shares_bod_midpoint_fetch_for_same_token(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self.db.replace_compare_open_leg_state(
            date_key="2026-04-09",
            account_names=["main"],
            rows=[
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "leader",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "mark_price_now": 0.55,
                    "unrealized_now": 1.5,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "our",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "mark_price_now": 0.55,
                    "unrealized_now": 1.5,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
            ],
        )

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-1": 0.6}) as fetch_midpoints:
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(fetch_midpoints.call_count, 2)
        self.assertEqual(fetch_midpoints.call_args_list[0].args[1], ["tok-1"])
        self.assertEqual(fetch_midpoints.call_args_list[1].args[1], ["tok-1"])

    def test_token_meta_watchlist_skips_irrelevant_zero_position_rows(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self._replace_open_baseline(
            scope_kind="our",
            rows=[
                {
                    "condition_id": "cond-open",
                    "token_id": "tok-open",
                    "market_slug": "market-open",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "mark_price_now": 0.5,
                    "unrealized_now": 1.0,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "bod",
                },
                {
                    "condition_id": "cond-stale",
                    "token_id": "tok-stale",
                    "market_slug": "market-stale",
                    "outcome": "YES",
                    "bod_open_size": 0.0,
                    "bod_open_cost": 0.0,
                    "bod_avg_open_price": 0.0,
                    "bod_mark_price": None,
                    "open_size": 0.0,
                    "open_cost": 0.0,
                    "avg_open_price": 0.0,
                    "unrealized_bod": 0.0,
                    "mark_price_now": None,
                    "unrealized_now": 0.0,
                    "realized_pnl": 0.0,
                    "status": "sold",
                    "settlement_time": None,
                    "last_event_ts": None,
                    "mark_price_source": None,
                },
            ],
        )

        meta_calls: list[list[str]] = []

        def capture_meta(_session, token_ids):
            meta_calls.append(list(token_ids))
            return {
                "tok-open": {
                    "market_slug": "market-open",
                    "outcome": "YES",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                }
            }

        with patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-open": 0.5}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", side_effect=capture_meta):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(meta_calls, [["tok-open"]])

    def test_cumulative_buy_metrics_carry_forward_while_today_pnl_stays_daily(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self.db.replace_compare_open_leg_state(
            date_key="2026-04-09",
            account_names=["main"],
            rows=[
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "our",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "bod_cumulative_buy_fill_count": 1,
                    "bod_cumulative_buy_size": 10.0,
                    "bod_cumulative_buy_usd": 4.0,
                    "bod_cumulative_sell_fill_count": 0,
                    "bod_cumulative_sell_size": 0.0,
                    "bod_cumulative_sell_usd": 0.0,
                    "cumulative_buy_fill_count": 1,
                    "cumulative_buy_size": 10.0,
                    "cumulative_buy_usd": 4.0,
                    "cumulative_sell_fill_count": 0,
                    "cumulative_sell_size": 0.0,
                    "cumulative_sell_usd": 0.0,
                    "mark_price_now": 0.55,
                    "unrealized_now": 1.5,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
            ],
        )

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", side_effect=[{"tok-1": 0.5}, {"tok-1": 0.6}]):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(int(row["our_buy_fill_count"]), 1)
        self.assertAlmostEqual(float(row["our_buy_usd"]), 4.0, places=6)
        self.assertAlmostEqual(float(row["our_buy_avg_price"]), 0.4, places=6)
        self.assertAlmostEqual(float(row["our_realized_pnl"]), 0.0, places=6)
        self.assertAlmostEqual(float(row["our_unrealized_change"]), 1.0, places=6)
        self.assertAlmostEqual(float(row["our_total_pnl"]), 1.0, places=6)

    def test_static_open_leg_with_zero_today_change_is_hidden(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self.db.replace_compare_open_leg_state(
            date_key="2026-04-09",
            account_names=["main"],
            rows=[
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "our",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "bod_cumulative_buy_fill_count": 1,
                    "bod_cumulative_buy_size": 10.0,
                    "bod_cumulative_buy_usd": 4.0,
                    "cumulative_buy_fill_count": 1,
                    "cumulative_buy_size": 10.0,
                    "cumulative_buy_usd": 4.0,
                    "mark_price_now": 0.5,
                    "unrealized_now": 1.0,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
            ],
        )

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={}), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", side_effect=[{"tok-1": 0.5}, {"tok-1": 0.5}]):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(self._market_rows(), [])

    def test_sell_side_cumulative_gap_sets_primary_gap_reason(self):
        self._write_account("main", ["0xleader"])
        self._set_compare_mode()
        self.db.replace_compare_open_leg_state(
            date_key="2026-04-09",
            account_names=["main"],
            rows=[
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "leader",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "bod_cumulative_buy_fill_count": 1,
                    "bod_cumulative_buy_size": 10.0,
                    "bod_cumulative_buy_usd": 4.0,
                    "cumulative_buy_fill_count": 1,
                    "cumulative_buy_size": 10.0,
                    "cumulative_buy_usd": 4.0,
                    "mark_price_now": 0.5,
                    "unrealized_now": 1.0,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
                {
                    "date_key": "2026-04-09",
                    "account_name": "main",
                    "leader_address": "0xleader",
                    "scope_kind": "our",
                    "condition_id": "cond-1",
                    "token_id": "tok-1",
                    "market_slug": "market-1",
                    "outcome": "YES",
                    "bod_open_size": 10.0,
                    "bod_open_cost": 4.0,
                    "bod_avg_open_price": 0.4,
                    "bod_mark_price": 0.5,
                    "open_size": 10.0,
                    "open_cost": 4.0,
                    "avg_open_price": 0.4,
                    "unrealized_bod": 1.0,
                    "bod_cumulative_buy_fill_count": 1,
                    "bod_cumulative_buy_size": 10.0,
                    "bod_cumulative_buy_usd": 4.0,
                    "cumulative_buy_fill_count": 1,
                    "cumulative_buy_size": 10.0,
                    "cumulative_buy_usd": 4.0,
                    "mark_price_now": 0.5,
                    "unrealized_now": 1.0,
                    "realized_pnl": 0.0,
                    "status": "open",
                    "settlement_time": "2026-05-01T00:00:00+00:00",
                    "last_event_ts": None,
                    "mark_price_source": "midpoint",
                },
            ],
        )
        self._insert_leader_activity(
            side="SELL",
            size=4.0,
            usd=2.8,
            price=0.7,
            timestamp_utc="2026-04-10T03:00:00+00:00",
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        row = self._market_rows()[0]
        self.assertEqual(int(row["leader_sell_fill_count"]), 1)
        self.assertEqual(int(row["our_sell_fill_count"]), 0)
        self.assertEqual(row["primary_gap_reason"], "count_gap")

    def test_prefollow_leader_buys_are_excluded_from_compare(self):
        self._write_account("main", ["0xleader"])
        self._insert_trade(
            token_id="tok-new",
            condition_id="cond-new",
            market_slug="market-new",
            created_at="2026-04-09T18:00:00+00:00",
            usd=5.0,
            size=10.0,
            price=0.5,
        )
        self._insert_leader_activity(
            token_id="tok-old",
            condition_id="cond-old",
            market_slug="market-old",
            timestamp_utc="2026-04-09T17:00:00+00:00",
            usd=4.0,
            size=10.0,
            price=0.4,
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-new": {"market_slug": "market-new", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
            "tok-old": {"market_slug": "market-old", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        token_ids = {row["token_id"] for row in self._market_rows()}
        self.assertIn("tok-new", token_ids)
        self.assertNotIn("tok-old", token_ids)
        open_token_ids = {row["token_id"] for row in self._open_rows()}
        self.assertNotIn("tok-old", open_token_ids)

    def test_prefollow_leader_sell_does_not_create_compare_row(self):
        self._write_account("main", ["0xleader"])
        self._insert_trade(
            token_id="tok-new",
            condition_id="cond-new",
            market_slug="market-new",
            created_at="2026-04-09T18:00:00+00:00",
            usd=5.0,
            size=10.0,
            price=0.5,
        )
        self._insert_leader_activity(
            token_id="tok-old",
            condition_id="cond-old",
            market_slug="market-old",
            timestamp_utc="2026-04-09T17:00:00+00:00",
            usd=4.0,
            size=10.0,
            price=0.4,
        )
        self._insert_leader_activity(
            side="SELL",
            token_id="tok-old",
            condition_id="cond-old",
            market_slug="market-old",
            timestamp_utc="2026-04-09T19:00:00+00:00",
            usd=6.0,
            size=10.0,
            price=0.6,
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-new": {"market_slug": "market-new", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
            "tok-old": {"market_slug": "market-old", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(
            [row for row in self._market_rows() if row["token_id"] == "tok-old"],
            [],
        )

    def test_stale_compare_mode_ignores_dirty_carryover_rows(self):
        self._write_account("main", ["0xleader"])
        self._replace_open_baseline(
            scope_kind="leader",
            rows=[{
                "condition_id": "cond-ghost",
                "token_id": "tok-ghost",
                "market_slug": "market-ghost",
                "outcome": "YES",
                "bod_open_size": 10.0,
                "bod_open_cost": 4.0,
                "bod_avg_open_price": 0.4,
                "bod_mark_price": 0.5,
                "open_size": 10.0,
                "open_cost": 4.0,
                "avg_open_price": 0.4,
                "unrealized_bod": 1.0,
                "mark_price_now": 0.5,
                "unrealized_now": 1.0,
                "realized_pnl": 0.0,
                "status": "open",
                "settlement_time": "2026-05-01T00:00:00+00:00",
                "last_event_ts": None,
                "mark_price_source": "bod",
            }],
        )
        snapshot._ensure_ct_meta_table(self.db)
        snapshot._upsert_ct_meta(self.db, "daily_compare_mode", "legacy_compare_v0")

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={"tok-ghost": {"market_slug": "market-ghost", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}}), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            stats = snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(stats["summary_rows"], 0)
        self.assertEqual(self._summary_rows(), [])
        self.assertEqual(self._market_rows(), [])
        self.assertEqual(self._open_rows(), [])

    def test_mode_mismatch_bootstraps_from_current_open_positions(self):
        self._write_account("main", ["0xleader"])
        self._insert_trade(
            created_at="2026-04-09T18:00:00+00:00",
            token_id="tok-1",
            condition_id="cond-1",
            market_slug="market-1",
            usd=4.0,
            size=10.0,
            price=0.4,
        )
        snapshot._ensure_ct_meta_table(self.db)
        snapshot._upsert_ct_meta(self.db, "daily_compare_mode", "legacy_compare_v0")

        with patch.object(snapshot, "_fetch_onchain_positions", return_value={
            "tok-1": {
                "size": 10.0,
                "initial_value": 4.0,
                "current_value": 5.0,
                "condition_id": "cond-1",
                "slug": "market-1",
                "outcome": "YES",
                "redeemable": False,
            }
        }), \
             patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
                 "tok-1": {"market_slug": "market-1", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"}
             }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={"tok-1": 0.5}):
            stats = snapshot.build_daily_compare(
                self.db,
                account_names=["main"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        self.assertEqual(stats["summary_rows"], 0)
        self.assertEqual(self._summary_rows(), [])
        self.assertEqual(self._market_rows(), [])
        open_rows = self._open_rows()
        self.assertEqual(len(open_rows), 2)
        leader_row = next(row for row in open_rows if row["scope_kind"] == "leader")
        our_row = next(row for row in open_rows if row["scope_kind"] == "our")
        self.assertEqual(leader_row["token_id"], "tok-1")
        self.assertEqual(our_row["token_id"], "tok-1")
        self.assertAlmostEqual(float(leader_row["open_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(leader_row["open_cost"]), 4.0, places=6)
        self.assertEqual(int(leader_row["cumulative_buy_fill_count"]), 0)
        self.assertAlmostEqual(float(our_row["open_size"]), 10.0, places=6)
        self.assertAlmostEqual(float(our_row["open_cost"]), 4.0, places=6)
        self.assertEqual(int(our_row["cumulative_buy_fill_count"]), 0)

    def test_same_leader_can_have_different_follow_windows_by_account(self):
        self._write_account("main", ["0xleader"])
        self._write_account("pm-2", ["0xleader"])
        self._insert_trade(
            account_name="main",
            token_id="tok-main",
            condition_id="cond-main",
            market_slug="market-main",
            created_at="2026-04-09T17:00:00+00:00",
            usd=5.0,
            size=10.0,
            price=0.5,
        )
        self._insert_trade(
            account_name="pm-2",
            token_id="tok-pm",
            condition_id="cond-pm",
            market_slug="market-pm",
            created_at="2026-04-09T21:00:00+00:00",
            usd=6.0,
            size=10.0,
            price=0.6,
        )
        self._insert_leader_activity(
            token_id="tok-shared",
            condition_id="cond-shared",
            market_slug="market-shared",
            timestamp_utc="2026-04-09T18:00:00+00:00",
            usd=4.0,
            size=10.0,
            price=0.4,
        )

        with patch.object(snapshot, "_fetch_token_market_meta_batch", return_value={
            "tok-main": {"market_slug": "market-main", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
            "tok-pm": {"market_slug": "market-pm", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
            "tok-shared": {"market_slug": "market-shared", "outcome": "YES", "settlement_time": "2026-05-01T00:00:00+00:00"},
        }), \
             patch.object(snapshot, "_resolve_tokens_with_cache_and_live", return_value=({}, {}, set(), 0, 0)), \
             patch.object(snapshot, "_fetch_midpoints_with_fallback", return_value={}):
            snapshot.build_daily_compare(
                self.db,
                account_names=["main", "pm-2"],
                now=datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc),
                accounts_dir=self.accounts_dir,
                sync_leader_activity=False,
            )

        shared_rows = [
            (row["account_name"], row["token_id"])
            for row in self._market_rows()
            if row["token_id"] == "tok-shared"
        ]
        self.assertEqual(shared_rows, [("main", "tok-shared")])


if __name__ == "__main__":
    unittest.main()
