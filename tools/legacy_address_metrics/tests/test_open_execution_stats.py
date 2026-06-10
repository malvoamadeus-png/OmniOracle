import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


TOOL_DIR = Path(__file__).resolve().parents[1]
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import polymarket_metrics as metrics  # noqa: E402


class OpenExecutionStatsTest(unittest.TestCase):
    def test_compute_orderbook_top5_depth_usd(self) -> None:
        book = {
            "bids": [
                {"price": "0.40", "size": "100"},
                {"price": "0.39", "size": "100"},
                {"price": "0.38", "size": "100"},
                {"price": "0.37", "size": "100"},
                {"price": "0.36", "size": "100"},
                {"price": "0.35", "size": "99999"},
            ],
            "asks": [
                {"price": "0.41", "size": "100"},
                {"price": "0.42", "size": "100"},
                {"price": "0.43", "size": "100"},
                {"price": "0.44", "size": "100"},
                {"price": "0.45", "size": "100"},
                {"price": "0.46", "size": "99999"},
            ],
        }

        depth = metrics.compute_orderbook_top5_depth_usd(book)

        self.assertIsNotNone(depth)
        assert depth is not None
        self.assertAlmostEqual(depth["bid_top5_depth_usd"], 190.0)
        self.assertAlmostEqual(depth["ask_top5_depth_usd"], 215.0)
        self.assertAlmostEqual(depth["top5_depth_usd"], 405.0)

    def test_open_execution_stats_skip_resolved_and_count_missing(self) -> None:
        positions = [
            {"closed": False, "token_id": "tok-ok", "slug": "open-ok"},
            {"closed": False, "token_id": "tok-nobook", "slug": "open-nobook"},
            {"closed": False, "token_id": "tok-missing-settle", "slug": "open-missing-settle"},
            {"closed": False, "token_id": "tok-resolved", "slug": "resolved"},
            {"closed": True, "token_id": "tok-closed", "slug": "open-ok"},
            {"closed": False, "slug": "missing-token"},
        ]
        markets = {
            "open-ok": {"endDate": "2026-01-11T00:00:00Z", "active": True},
            "open-nobook": {"endDate": "2026-01-21T00:00:00Z", "active": True},
            "open-missing-settle": {"active": True},
            "resolved": {"closed": True, "endDate": "2026-01-21T00:00:00Z"},
        }
        books = {
            "tok-ok": {"bids": [{"price": "0.5", "size": "10"}], "asks": [{"price": "0.6", "size": "10"}]},
            "tok-nobook": None,
            "tok-missing-settle": {"bids": [{"price": "0.4", "size": "20"}], "asks": []},
        }

        with patch.object(metrics, "utc_now", return_value=datetime(2026, 1, 1, tzinfo=timezone.utc)), patch.object(
            metrics, "fetch_market_by_slug", side_effect=lambda _session, slug, _cache: markets.get(slug)
        ), patch.object(metrics, "fetch_orderbook", side_effect=lambda _session, token: books.get(token)):
            stats = metrics.compute_open_position_execution_stats(object(), positions)  # type: ignore[arg-type]

        self.assertEqual(stats["open_positions_analyzed"], 3)
        self.assertEqual(stats["open_positions_resolved_skipped"], 1)
        self.assertEqual(stats["open_positions_missing_book"], 1)
        self.assertEqual(stats["open_positions_missing_settlement"], 1)
        self.assertAlmostEqual(stats["avg_open_top5_depth_usd"], 9.5)
        self.assertAlmostEqual(stats["avg_open_settlement_days"], 15.0)


if __name__ == "__main__":
    unittest.main()
