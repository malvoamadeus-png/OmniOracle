import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import copytrade.build_leader_pnl_snapshot as snapshot
import copytrade.main as main_mod
from copytrade.db import CopyTradeDB


class CompareRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "copytrade.sqlite"
        db = CopyTradeDB(str(self.db_path))
        db.close()
        self._reset_scheduler(main_mod._maybe_run_compare_snapshot)
        self._reset_scheduler(main_mod._maybe_run_leader_attr_snapshot)

    def tearDown(self):
        self._reset_scheduler(main_mod._maybe_run_compare_snapshot)
        self._reset_scheduler(main_mod._maybe_run_leader_attr_snapshot)
        self.tmpdir.cleanup()

    def _reset_scheduler(self, fn) -> None:
        for attr in ("_last_run_ts", "_proc", "_started_at_ts"):
            if hasattr(fn, attr):
                delattr(fn, attr)

    def test_snapshot_main_compare_only_skips_full_snapshot_pipeline(self):
        args = SimpleNamespace(
            db=str(self.db_path),
            no_sync_supabase=False,
            force_rebuild_daily=False,
            accounts="main,pm-2",
            compare_date="",
            compare_only=True,
        )

        with patch.object(snapshot, "parse_args", return_value=args), \
             patch.object(snapshot, "repair_phantom_positions") as repair_phantom, \
             patch.object(snapshot, "reconcile_redeemed_positions") as reconcile, \
             patch.object(snapshot, "backfill_resolution_exit_settlement_times") as backfill, \
             patch.object(snapshot, "_migrate_daily_leader_pnl_to_pure_attribution_once") as migrate_daily, \
             patch.object(snapshot, "build_snapshots") as build_snapshots, \
             patch.object(snapshot, "_build_daily_leader_deltas") as build_daily_deltas, \
             patch.object(snapshot, "_rebuild_daily_leader_market_leg_pnl") as rebuild_leg_detail, \
             patch.object(
                 snapshot,
                 "build_daily_compare",
                 return_value={"summary_rows": 1, "market_leg_rows": 2, "open_leg_rows": 3},
             ) as build_compare, \
             patch.object(snapshot, "sync_supabase") as sync_supabase, \
             patch("sys.stdout", new=io.StringIO()):
            rc = snapshot.main()

        self.assertEqual(rc, 0)
        repair_phantom.assert_not_called()
        reconcile.assert_not_called()
        backfill.assert_not_called()
        migrate_daily.assert_not_called()
        build_snapshots.assert_not_called()
        build_daily_deltas.assert_not_called()
        rebuild_leg_detail.assert_not_called()
        build_compare.assert_called_once()
        sync_supabase.assert_called_once_with(str(self.db_path), compare_only=True)

    def test_snapshot_main_compare_only_can_rebuild_specific_compare_date(self):
        args = SimpleNamespace(
            db=str(self.db_path),
            no_sync_supabase=True,
            force_rebuild_daily=False,
            accounts="pm-1",
            compare_date="2026-05-05",
            compare_only=True,
        )

        with patch.object(snapshot, "parse_args", return_value=args), \
             patch.object(
                 snapshot,
                 "build_daily_compare",
                 return_value={"summary_rows": 1, "market_leg_rows": 2, "open_leg_rows": 3},
             ) as build_compare, \
             patch("sys.stdout", new=io.StringIO()):
            rc = snapshot.main()

        self.assertEqual(rc, 0)
        kwargs = build_compare.call_args.kwargs
        self.assertEqual(kwargs["account_names"], ["pm-1"])
        self.assertEqual(snapshot._compare_date_key(kwargs["now"]), "2026-05-05")

    def test_sync_supabase_compare_only_adds_lightweight_flag(self):
        sync_script = Path(self.tmpdir.name) / "sync_to_supabase.py"
        sync_script.write_text("print('ok')\n", encoding="utf-8")

        completed = SimpleNamespace(returncode=0, stdout="")
        with patch.object(snapshot, "SYNC_SCRIPT", sync_script), \
             patch.object(snapshot.subprocess, "run", return_value=completed) as run_mock:
            snapshot.sync_supabase(str(self.db_path), compare_only=True)

        cmd = run_mock.call_args.args[0]
        self.assertIn("--copytrade-compare-only", cmd)
        self.assertEqual(cmd[-1], "--copytrade-compare-only")

    def test_snapshot_db_lock_prevents_second_snapshot_for_same_db(self):
        with snapshot._snapshot_db_lock(str(self.db_path)) as first:
            self.assertTrue(first)
            with snapshot._snapshot_db_lock(str(self.db_path)) as second:
                self.assertFalse(second)

    def test_main_compare_scheduler_launches_compare_only_process(self):
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None

        with patch.object(main_mod.subprocess, "Popen", return_value=fake_proc) as popen_mock, \
             patch.object(main_mod.time, "time", return_value=12000.0):
            main_mod._maybe_run_compare_snapshot(
                str(self.db_path),
                False,
                ["main", "pm-2", "main"],
            )

        cmd = popen_mock.call_args.args[0]
        self.assertIn("--compare-only", cmd)
        self.assertIn("--accounts", cmd)
        self.assertEqual(cmd[-1], "main,pm-2")


if __name__ == "__main__":
    unittest.main()
