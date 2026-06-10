from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from metrics import (
    DEFAULT_CLOSED_POSITIONS_LIMIT,
    METRICS_COMPAT_VERSION,
    compute_drawdown_sharpe,
    compute_pnl_30d,
    compute_position_metrics,
    fetch_closed_positions,
    is_metrics_compatible,
    normalize_position,
)


class MetricsTest(unittest.TestCase):
    def test_compute_pnl_30d_interpolates_start_value(self) -> None:
        now = datetime(2026, 5, 14, tzinfo=timezone.utc)
        points = [
            (now - timedelta(days=40), 0.0),
            (now - timedelta(days=20), 200.0),
            (now, 300.0),
        ]

        self.assertEqual(compute_pnl_30d(points, now=now), 200.0)

    def test_position_metrics_basic_values(self) -> None:
        positions = [
            {"market": "m1", "cash_pnl": 30.0, "realized_pnl": 40.0, "cost_basis_usd": 100.0, "total_bought": 100.0, "avg_price": 0.6, "size": 100.0, "cur_price": 1.0, "closed": True},
            {"market": "m2", "cash_pnl": -10.0, "realized_pnl": -20.0, "cost_basis_usd": 50.0, "total_bought": 50.0, "avg_price": 0.4, "size": 50.0, "cur_price": 0.0, "closed": True},
            {"market": "m3", "cash_pnl": 20.0, "realized_pnl": 10.0, "cost_basis_usd": 100.0, "total_bought": 100.0, "avg_price": 0.7, "size": 100.0, "closed": False, "cur_price": 0.8},
        ]

        metrics = compute_position_metrics(positions)

        self.assertEqual(metrics["total_pnl"], 40.0)
        self.assertEqual(metrics["profit_factor"], 5.0)
        self.assertAlmostEqual(metrics["roi"], 40.0 / 250.0)
        self.assertEqual(metrics["total_trades"], 3)
        self.assertAlmostEqual(metrics["win_rate"], 2 / 3)
        self.assertAlmostEqual(metrics["avg_trade_price"], 0.6)
        self.assertAlmostEqual(metrics["realized_edge_score"], (0.4 * 100.0 - 0.4 * 50.0 + 0.1 * 100.0) / 250.0)
        self.assertEqual(metrics["position_edge"]["edge_samples"], 3)

    def test_realized_edge_uses_token_side_price_for_no_token(self) -> None:
        positions = [
            {"market": "yes-win-low", "cash_pnl": 9.9, "realized_pnl": 9.9, "cost_basis_usd": 10.0, "avg_price": 0.01, "size": 10.0, "cur_price": 1.0, "closed": True},
            {"market": "no-win", "cash_pnl": 3.0, "realized_pnl": 3.0, "cost_basis_usd": 10.0, "avg_price": 0.70, "size": 10.0, "cur_price": 1.0, "closed": True, "outcome": "No"},
        ]

        metrics = compute_position_metrics(positions)

        self.assertAlmostEqual(metrics["realized_edge_score"], (0.99 * 10.0 + 0.30 * 10.0) / 20.0)

    def test_ambiguous_closed_position_is_excluded(self) -> None:
        positions = [
            {"market": "resolved", "cash_pnl": 8.0, "realized_pnl": 8.0, "cost_basis_usd": 10.0, "avg_price": 0.2, "size": 10.0, "cur_price": 1.0, "closed": True},
            {"market": "early-exit", "cash_pnl": 1.0, "realized_pnl": 1.0, "cost_basis_usd": 10.0, "avg_price": 0.2, "size": 10.0, "cur_price": 0.3, "closed": True},
        ]

        metrics = compute_position_metrics(positions)

        self.assertAlmostEqual(metrics["realized_edge_score"], 0.8)
        self.assertEqual(metrics["position_edge"]["edge_samples"], 1)
        self.assertEqual(metrics["position_edge"]["skipped_ambiguous_resolution"], 1)

    def test_open_position_participates_in_realized_edge_score(self) -> None:
        positions = [
            {"market": "open-win", "cash_pnl": 12.0, "realized_pnl": None, "cost_basis_usd": 40.0, "avg_price": 0.50, "size": 100.0, "cur_price": 0.80, "closed": False},
        ]

        metrics = compute_position_metrics(positions)

        self.assertAlmostEqual(metrics["realized_edge_score"] or 0.0, 0.30)
        self.assertEqual(metrics["position_edge"]["edge_samples"], 1)
        self.assertEqual(metrics["position_edge"]["resolution_sources"]["cur_price"], 1)

    def test_normalize_position_accepts_closed_position_alias_fields(self) -> None:
        row = {
            "asset_id": "token-1",
            "eventSlug": "nba-test",
            "name": "YES",
            "balance": "25",
            "avgCost": "0.42",
            "price": "0.42",
            "pnl": "14.5",
            "curPrice": "1",
            "closed": True,
        }

        pos = normalize_position(row, closed=True)

        self.assertEqual(pos["token_id"], "token-1")
        self.assertEqual(pos["slug"], "nba-test")
        self.assertEqual(pos["outcome"], "YES")
        self.assertAlmostEqual(pos["size"] or 0.0, 25.0)
        self.assertAlmostEqual(pos["cost_basis_usd"] or 0.0, 0.42)
        self.assertAlmostEqual(pos["avg_price"] or 0.0, 0.42)
        self.assertAlmostEqual(pos["cash_pnl"] or 0.0, 14.5)
        self.assertAlmostEqual(pos["realized_pnl"] or 0.0, 14.5)
        self.assertTrue(pos["closed"])

    def test_normalize_position_uses_realized_pnl_as_cash_pnl_for_closed_rows(self) -> None:
        row = {
            "asset": "token-closed",
            "conditionId": "market-closed",
            "slug": "nhl-test",
            "outcome": "Yes",
            "avgPrice": 0.43,
            "totalBought": 15000,
            "realizedPnl": 8550,
            "curPrice": 1,
            "closed": True,
        }

        pos = normalize_position(row, closed=True)

        self.assertAlmostEqual(pos["cash_pnl"] or 0.0, 8550.0)
        self.assertAlmostEqual(pos["realized_pnl"] or 0.0, 8550.0)

    def test_normalize_position_inferrs_price_and_resolution_from_values(self) -> None:
        row = {
            "asset_id": "token-2",
            "eventSlug": "nba-test-2",
            "title": "YES",
            "totalBought": "100",
            "initialValue": "42",
            "currentValue": "100",
            "pnl": "58",
            "closed": True,
        }

        pos = normalize_position(row, closed=True)
        metrics = compute_position_metrics([pos])

        self.assertAlmostEqual(pos["avg_price"] or 0.0, 0.42)
        self.assertAlmostEqual(pos["size"] or 0.0, 100.0)
        self.assertAlmostEqual(pos["cur_price"] or 0.0, 1.0)
        self.assertAlmostEqual(metrics["realized_edge_score"] or 0.0, 0.58)
        self.assertEqual(metrics["position_edge"]["edge_samples"], 1)

    def test_normalize_position_uses_total_bought_as_share_count_when_size_missing(self) -> None:
        row = {
            "asset": "token-3",
            "conditionId": "market-3",
            "slug": "nba-sample",
            "outcome": "Yes",
            "avgPrice": 0.25,
            "totalBought": 20,
            "realizedPnl": -5,
            "curPrice": 0,
            "closed": True,
        }

        pos = normalize_position(row, closed=True)
        metrics = compute_position_metrics([pos])

        self.assertAlmostEqual(pos["size"] or 0.0, 20.0)
        self.assertAlmostEqual(pos["cost_basis_usd"] or 0.0, 5.0)
        self.assertAlmostEqual(metrics["realized_edge_score"] or 0.0, -0.25)
        self.assertEqual(metrics["position_edge"]["edge_samples"], 1)

    def test_drawdown_and_sharpe_are_computed(self) -> None:
        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        points = [
            (base + timedelta(days=0), 100.0),
            (base + timedelta(days=1), 150.0),
            (base + timedelta(days=2), 120.0),
            (base + timedelta(days=3), 180.0),
        ]

        drawdown, sharpe = compute_drawdown_sharpe(points)

        self.assertAlmostEqual(drawdown or 0.0, 30.0 / 150.0)
        self.assertIsNotNone(sharpe)
        self.assertTrue(math.isfinite(sharpe or 0.0))

    def test_drawdown_returns_zero_for_monotonic_curve(self) -> None:
        base = datetime(2026, 5, 1, tzinfo=timezone.utc)
        points = [
            (base + timedelta(days=0), 100.0),
            (base + timedelta(days=1), 120.0),
            (base + timedelta(days=2), 140.0),
        ]

        drawdown, _ = compute_drawdown_sharpe(points)

        self.assertEqual(drawdown, 0.0)

    def test_fetch_closed_positions_respects_limit(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get_json(self, *_args: object, **_kwargs: object) -> List[Dict[str, Any]]:
                self.calls += 1
                return [{"id": idx} for idx in range(50)]

        client = FakeClient()

        rows = fetch_closed_positions(client, "0xabc", closed_positions_limit=120)  # type: ignore[arg-type]

        self.assertEqual(len(rows), 120)
        self.assertEqual(client.calls, 3)

    def test_metrics_compatibility_checks_version_and_limit(self) -> None:
        self.assertTrue(
            is_metrics_compatible(
                {"metrics_compat_version": METRICS_COMPAT_VERSION, "closed_positions_limit": DEFAULT_CLOSED_POSITIONS_LIMIT}
            )
        )
        self.assertFalse(
            is_metrics_compatible({"metrics_compat_version": "old", "closed_positions_limit": DEFAULT_CLOSED_POSITIONS_LIMIT})
        )
        self.assertFalse(is_metrics_compatible({"metrics_compat_version": METRICS_COMPAT_VERSION, "closed_positions_limit": 1000}))
        self.assertFalse(is_metrics_compatible(None))


if __name__ == "__main__":
    unittest.main()
