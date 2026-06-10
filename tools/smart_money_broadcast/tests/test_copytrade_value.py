from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from copytrade_value import (
    COPYTRADE_VALUE_SCORE_VERSION,
    apply_copytrade_value,
    compute_resilience_scores,
    load_threshold_config,
    threshold_status,
)


def valid_config() -> dict:
    return {
        "score_version": COPYTRADE_VALUE_SCORE_VERSION,
        "high_threshold": 80.0,
        "medium_threshold": 50.0,
        "metric_samples": {
            "sharpe": [0.1, 0.5, 1.0],
            "realized_edge_score": [0.01, 0.05, 0.10],
            "roi": [0.05, 0.10, 0.20],
            "profit_factor": [1.0, 2.0, 3.0],
            "max_drawdown": [0.10, 0.20, 0.40],
            "ulcer_index": [0.05, 0.10, 0.30],
        },
    }


def base_metrics() -> dict:
    return {
        "total_trades": 80,
        "max_drawdown": 0.10,
        "avg_trade_price": 0.50,
        "current_position_value_usd": 1200,
        "sharpe": 1.0,
        "realized_edge_score": 0.10,
        "roi": 0.20,
        "profit_factor": 3.0,
        "ulcer_index": 0.05,
    }


class CopytradeValueTest(unittest.TestCase):
    def test_load_threshold_config_requires_real_calibration_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "thresholds.json"
            path.write_text(
                json.dumps(
                    {
                        "score_version": COPYTRADE_VALUE_SCORE_VERSION,
                        "high_threshold": None,
                        "medium_threshold": None,
                        "metric_samples": {"sharpe": []},
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(load_threshold_config(path))
            self.assertIn("不可用", threshold_status(path))

            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            self.assertIsNotNone(load_threshold_config(path))
            self.assertEqual(threshold_status(path), "")

    def test_apply_copytrade_value_scores_with_fixed_thresholds(self) -> None:
        result = apply_copytrade_value(base_metrics(), valid_config())

        self.assertEqual(result["copytrade_value_level"], "high")
        self.assertAlmostEqual(result["copytrade_value_score"], 100.0)
        self.assertIsNone(result["copytrade_value_exclusion_reason"])

    def test_apply_copytrade_value_hard_exclusion_and_missing_config(self) -> None:
        excluded = apply_copytrade_value({**base_metrics(), "total_trades": 49}, valid_config())
        missing = apply_copytrade_value(base_metrics(), None)

        self.assertEqual(excluded["copytrade_value_level"], "not_worth_copying")
        self.assertEqual(excluded["copytrade_value_exclusion_reason"], "total_trades_lt_50")
        self.assertIsNone(missing["copytrade_value_level"])
        self.assertEqual(missing["copytrade_value_exclusion_reason"], "threshold_config_missing")

    def test_apply_copytrade_value_requires_three_score_metrics(self) -> None:
        metrics = {
            "total_trades": 80,
            "max_drawdown": 0.10,
            "avg_trade_price": 0.50,
            "current_position_value_usd": 1200,
            "sharpe": 1.0,
        }

        result = apply_copytrade_value(metrics, valid_config())

        self.assertEqual(result["copytrade_value_level"], "not_worth_copying")
        self.assertEqual(result["copytrade_value_exclusion_reason"], "insufficient_score_metrics")

    def test_resilience_score_uses_harmonic_mean(self) -> None:
        scores = compute_resilience_scores(
            [
                {"address": "0xstrong", "max_drawdown": 0.10, "ulcer_index": 0.05},
                {"address": "0xmixed", "max_drawdown": 0.10, "ulcer_index": 0.50},
                {"address": "0xweak", "max_drawdown": 0.50, "ulcer_index": 0.50},
            ]
        )

        self.assertGreater(scores["0xstrong"], scores["0xmixed"])
        self.assertGreater(scores["0xmixed"], scores["0xweak"])
        self.assertLess(scores["0xmixed"], 100.0)


if __name__ == "__main__":
    unittest.main()
