from __future__ import annotations

import unittest

from report import fmt_copytrade_value, percentile_rank, render_report, resilience_rank_pct


class ReportTest(unittest.TestCase):
    def test_percentile_higher_is_better(self) -> None:
        cohort = [{"total_pnl": 300.0}, {"total_pnl": 200.0}, {"total_pnl": 100.0}]

        self.assertAlmostEqual(percentile_rank({"total_pnl": 300.0}, cohort, "total_pnl", lower_is_better=False) or 0.0, 100 / 3)
        self.assertAlmostEqual(percentile_rank({"total_pnl": 100.0}, cohort, "total_pnl", lower_is_better=False) or 0.0, 100.0)

    def test_percentile_realized_edge_higher_is_better(self) -> None:
        cohort = [{"realized_edge_score": 0.01}, {"realized_edge_score": 0.50}, {"realized_edge_score": 0.99}]

        self.assertAlmostEqual(percentile_rank({"realized_edge_score": 0.99}, cohort, "realized_edge_score", lower_is_better=False) or 0.0, 100 / 3)
        self.assertAlmostEqual(percentile_rank({"realized_edge_score": 0.01}, cohort, "realized_edge_score", lower_is_better=False) or 0.0, 100.0)

    def test_percentile_missing_or_small_pool(self) -> None:
        self.assertIsNone(percentile_rank({"total_pnl": None}, [{"total_pnl": 1.0}], "total_pnl", lower_is_better=False))
        self.assertIsNone(percentile_rank({"total_pnl": 1.0}, [{"total_pnl": 1.0}], "total_pnl", lower_is_better=False))

    def test_render_report_degrades_missing_fields(self) -> None:
        text = render_report("0xabc", {}, [], board="LOL")

        self.assertIn("过去30日盈利暂无数据万美元，总盈利暂无数据万美元", text)
        self.assertIn("精准预测能力在LOL板块排行前暂无排行%", text)
        self.assertIn("Realized Edge分数暂无数据，投注回报率暂无数据，夏普比暂无数据，最大回撤暂无数据", text)
        self.assertIn("跟单价值：暂无数据", text)
        self.assertIn("地址总盈亏暂无数据万美元，胜率暂无数据", text)

    def test_render_report_includes_total_pnl_and_win_rate_line(self) -> None:
        text = render_report(
            "0xabc",
            {
                "pnl_30d": 12000.0,
                "total_pnl": 34567.0,
                "realized_edge_score": 0.1234,
                "roi": 0.4567,
                "sharpe": 1.2345,
                "max_drawdown": 0.1111,
                "ulcer_index": 0.2222,
                "copytrade_value_level": "high",
                "copytrade_value_score": 88.8,
                "win_rate": 0.625,
            },
            [
                {"address": "0xabc", "total_pnl": 34567.0, "max_drawdown": 0.1111, "ulcer_index": 0.2222},
                {"address": "0xdef", "total_pnl": 100.0, "max_drawdown": 0.5, "ulcer_index": 0.8},
            ],
            board="NBA",
        )

        self.assertIn("Realized Edge分数0.1234，投注回报率0.4567，夏普比1.2345，最大回撤0.1111，溃疡指标0.2222", text)
        self.assertIn("跟单价值：high（88.8分）", text)
        self.assertIn("地址总盈亏3.46万美元，胜率62.5%", text)

    def test_resilience_rank_uses_harmonic_score(self) -> None:
        cohort = [
            {"address": "0xstrong", "max_drawdown": 0.05, "ulcer_index": 0.05},
            {"address": "0xmixed", "max_drawdown": 0.05, "ulcer_index": 0.50},
            {"address": "0xweak", "max_drawdown": 0.50, "ulcer_index": 0.50},
        ]

        strong_rank = resilience_rank_pct("0xstrong", cohort[0], cohort)
        mixed_rank = resilience_rank_pct("0xmixed", cohort[1], cohort)
        weak_rank = resilience_rank_pct("0xweak", cohort[2], cohort)

        self.assertAlmostEqual(strong_rank or 0.0, 100.0 / 3.0)
        self.assertAlmostEqual(mixed_rank or 0.0, 200.0 / 3.0)
        self.assertAlmostEqual(weak_rank or 0.0, 100.0)

    def test_fmt_copytrade_value_for_exclusion(self) -> None:
        self.assertEqual(
            fmt_copytrade_value(
                {
                    "copytrade_value_level": "not_worth_copying",
                    "copytrade_value_exclusion_reason": "threshold_config_missing",
                }
            ),
            "不值得跟单（threshold_config_missing）",
        )


if __name__ == "__main__":
    unittest.main()
