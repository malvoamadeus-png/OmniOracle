from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store import SmartMoneyStore


class StoreTest(unittest.TestCase):
    def test_metrics_round_trip_and_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SmartMoneyStore(Path(tmp) / "smart_money.sqlite")
            try:
                store.upsert_address({"address": "0xabc"}, "NBA")
                store.upsert_address({"address": "0xdef"}, "NBA")
                store.upsert_address({"address": "0xabc"}, "LOL")
                store.save_metrics("0xabc", {"total_pnl": 1.0}, {"source": "test"}, "NBA")
                store.save_metrics("0xdef", {"total_pnl": 2.0}, {"source": "test"}, "NBA")
                store.save_metrics("0xabc", {"total_pnl": 9.0}, {"source": "test"}, "LOL")

                self.assertEqual(store.latest_metrics("0xabc"), {"total_pnl": 1.0})
                latest_payload = store.latest_metrics_with_details("0xabc")
                self.assertEqual(latest_payload, {"metrics": {"total_pnl": 1.0}, "details": {"source": "test"}})
                cohort = sorted(store.cohort_metrics(), key=lambda row: row["address"])
                self.assertEqual([row["address"] for row in cohort], ["0xabc", "0xdef"])
                self.assertEqual([row["total_pnl"] for row in cohort], [9.0, 2.0])
                lol_cohort = store.cohort_metrics("LOL")
                self.assertEqual([row["address"] for row in lol_cohort], ["0xabc"])
            finally:
                store.close()

    def test_address_and_run_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SmartMoneyStore(Path(tmp) / "smart_money.sqlite")
            try:
                self.assertFalse(store.has_address("0xabc"))
                store.upsert_address(
                    {
                        "address": "0xabc",
                        "condition_id": "cond",
                        "slug": "slug",
                        "title": "title",
                        "address_age_days": 33.0,
                        "user_stats_trades": 44,
                    },
                    "NBA",
                )
                store.upsert_address({"address": "0xabc"}, "LOL")
                self.assertTrue(store.has_address("0xabc"))
                self.assertTrue(store.has_address("0xabc", "NBA"))
                self.assertTrue(store.has_address("0xabc", "LOL"))
                run_id = store.create_run(["NBA", "LOL"], 1, 30.0, 10, "reuse_old_metrics")
                store.record_run_address(run_id, "0xabc", "selected", "new", "cond", "slug", "NBA")
                store.finish_run(run_id, 1, {})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
