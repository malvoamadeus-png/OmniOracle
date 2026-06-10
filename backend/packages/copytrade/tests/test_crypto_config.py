import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.config import CopyTradeConfig, validate_copytrade_config


class CryptoConfigValidationTests(unittest.TestCase):
    def test_crypto_only_requires_non_empty_timeframes(self):
        cfg = CopyTradeConfig(
            crypto_only_enabled=True,
            crypto_allowed_timeframes=[],
        )

        with self.assertRaisesRegex(
            ValueError,
            "crypto_allowed_timeframes must not be empty when crypto_only_enabled=true",
        ):
            validate_copytrade_config(cfg, context="config")

    def test_crypto_timeframes_reject_invalid_enum(self):
        cfg = CopyTradeConfig(
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["15m", "30m"],
        )

        with self.assertRaisesRegex(
            ValueError,
            "crypto_allowed_timeframes has invalid value '30m'",
        ):
            validate_copytrade_config(cfg, context="config")

    def test_crypto_timeframes_accept_daily_and_weekly(self):
        cfg = CopyTradeConfig(
            crypto_only_enabled=True,
            crypto_allowed_timeframes=["1d", "1w"],
        )

        validated = validate_copytrade_config(cfg, context="config")

        self.assertEqual(validated.crypto_allowed_timeframes, ["1d", "1w"])

    def test_leader_override_enabled_cannot_follow_empty_global_timeframes(self):
        cfg = CopyTradeConfig(
            crypto_only_enabled=False,
            crypto_allowed_timeframes=[],
            leader_overrides={
                "0xabc": {
                    "crypto_only_enabled": True,
                }
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            r"leader_overrides\[0xabc\] enables crypto_only but has no effective allowed timeframes",
        ):
            validate_copytrade_config(cfg, context="config")

    def test_leader_config_ignores_account_level_auto_tp_override(self):
        cfg = CopyTradeConfig(
            auto_tp_enabled=False,
            leader_overrides={
                "0xabc": {
                    "auto_tp_enabled": True,
                    "fixed_usd_amount": 42.0,
                }
            },
        )

        leader_cfg = cfg.get_leader_config("0xabc")

        self.assertFalse(leader_cfg.auto_tp_enabled)
        self.assertEqual(leader_cfg.fixed_usd_amount, 42.0)


if __name__ == "__main__":
    unittest.main()
