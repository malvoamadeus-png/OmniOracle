import sys
import tempfile
import unittest
from pathlib import Path
import sqlite3
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.web import server


class WebConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.accounts_dir = Path(self.tmpdir.name) / "accounts"
        self.accounts_dir.mkdir(parents=True, exist_ok=True)
        self.dotenv_path = Path(self.tmpdir.name) / ".env"
        self.db_path = Path(self.tmpdir.name) / "copytrade.sqlite"

        self.accounts_patcher = patch.object(server, "ACCOUNTS_DIR", self.accounts_dir)
        self.dotenv_patcher = patch.object(server, "DOTENV_PATH", self.dotenv_path)
        self.db_patcher = patch.object(server, "DB_PATH", self.db_path)
        self.accounts_patcher.start()
        self.dotenv_patcher.start()
        self.db_patcher.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self.client.close()
        self.db_patcher.stop()
        self.accounts_patcher.stop()
        self.dotenv_patcher.stop()
        self.tmpdir.cleanup()

    def test_put_defaults_rejects_enabled_crypto_without_timeframes(self):
        response = self.client.put(
            "/api/defaults",
            json={
                "crypto_only_enabled": True,
                "crypto_allowed_timeframes": [],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "crypto_allowed_timeframes must not be empty",
            response.json()["detail"],
        )

    def test_index_exposes_execution_episode_controls(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("aggregation_mode", response.text)
        self.assertIn("execution_episode_window_minutes", response.text)
        self.assertIn("execution_episode_price_band_abs", response.text)
        self.assertIn("Execution Episode", response.text)
        self.assertIn("auto_tp_enabled", response.text)

    def test_put_defaults_rejects_auto_tp_without_mirror_sell(self):
        response = self.client.put(
            "/api/defaults",
            json={
                "exit_strategy": "hold_to_resolution",
                "auto_tp_enabled": True,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "auto_tp_enabled requires exit_strategy=mirror_sell",
            response.json()["detail"],
        )

    def test_put_defaults_revalidates_existing_accounts(self):
        server._write_toml(
            self.accounts_dir / "main.toml",
            {
                "leader_addresses": ["0xabc"],
                "leader_overrides": {
                    "0xabc": {
                        "crypto_only_enabled": True,
                    }
                },
            },
        )

        response = self.client.put(
            "/api/defaults",
            json={
                "crypto_only_enabled": False,
                "crypto_allowed_timeframes": [],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("leader_overrides[0xabc]", response.json()["detail"])

    def test_put_account_rejects_invalid_crypto_override(self):
        server._write_toml(
            self.accounts_dir / "_defaults.toml",
            {
                "crypto_only_enabled": True,
                "crypto_allowed_timeframes": ["15m"],
            },
        )

        response = self.client.put(
            "/api/accounts/main",
            json={
                "leader_addresses": ["0xabc"],
                "leader_overrides": {
                    "0xabc": {
                        "crypto_only_enabled": True,
                        "crypto_allowed_timeframes": [],
                    }
                },
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("leader_overrides[0xabc]", response.json()["detail"])

    def test_put_account_strips_auto_tp_leader_override(self):
        server._write_toml(
            self.accounts_dir / "_defaults.toml",
            {
                "exit_strategy": "hold_to_resolution",
            },
        )

        response = self.client.put(
            "/api/accounts/main",
            json={
                "leader_addresses": ["0xabc"],
                "leader_overrides": {
                    "0xabc": {
                        "auto_tp_enabled": True,
                    }
                },
            },
        )
        read_account = self.client.get("/api/accounts/main")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(read_account.status_code, 200)
        self.assertEqual(read_account.json().get("leader_overrides"), {})

    def test_valid_defaults_and_account_can_be_saved_and_loaded(self):
        defaults_body = {
            "crypto_only_enabled": True,
            "crypto_allowed_timeframes": ["15m", "4h"],
        }
        account_body = {
            "leader_addresses": ["0xabc"],
            "leader_overrides": {
                "0xabc": {
                    "crypto_only_enabled": True,
                    "crypto_allowed_timeframes": ["4h"],
                }
            },
        }

        defaults_resp = self.client.put("/api/defaults", json=defaults_body)
        account_resp = self.client.put("/api/accounts/main", json=account_body)
        read_defaults = self.client.get("/api/defaults")
        read_account = self.client.get("/api/accounts/main")

        self.assertEqual(defaults_resp.status_code, 200)
        self.assertEqual(account_resp.status_code, 200)
        self.assertEqual(read_defaults.status_code, 200)
        self.assertEqual(read_account.status_code, 200)
        self.assertEqual(read_defaults.json()["crypto_allowed_timeframes"], ["15m", "4h"])
        self.assertEqual(
            read_account.json()["leader_overrides"]["0xabc"]["crypto_allowed_timeframes"],
            ["4h"],
        )

    def test_daily_and_weekly_timeframes_round_trip(self):
        defaults_body = {
            "crypto_only_enabled": True,
            "crypto_allowed_timeframes": ["1d", "1w"],
        }
        account_body = {
            "leader_addresses": ["0xabc"],
            "leader_overrides": {
                "0xabc": {
                    "crypto_only_enabled": True,
                    "crypto_allowed_timeframes": ["1w"],
                }
            },
        }

        defaults_resp = self.client.put("/api/defaults", json=defaults_body)
        account_resp = self.client.put("/api/accounts/main", json=account_body)
        read_defaults = self.client.get("/api/defaults")
        read_account = self.client.get("/api/accounts/main")

        self.assertEqual(defaults_resp.status_code, 200)
        self.assertEqual(account_resp.status_code, 200)
        self.assertEqual(read_defaults.status_code, 200)
        self.assertEqual(read_account.status_code, 200)
        self.assertEqual(read_defaults.json()["crypto_allowed_timeframes"], ["1d", "1w"])
        self.assertEqual(
            read_account.json()["leader_overrides"]["0xabc"]["crypto_allowed_timeframes"],
            ["1w"],
        )

    def test_delete_account_removes_matching_env_keys_only(self):
        server._write_toml(
            self.accounts_dir / "main.toml",
            {
                "env_suffix": "MAIN",
                "leader_addresses": ["0xabc"],
            },
        )
        server._write_dotenv_key("PRIVATE_KEY_MAIN", "pk-main")
        server._write_dotenv_key("FUNDER_ADDRESS_MAIN", "funder-main")
        server._write_dotenv_key("PRIVATE_KEY_ALT", "pk-alt")

        response = self.client.delete("/api/accounts/main")

        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.accounts_dir / "main.toml").exists())
        env = server._read_dotenv()
        self.assertNotIn("PRIVATE_KEY_MAIN", env)
        self.assertNotIn("FUNDER_ADDRESS_MAIN", env)
        self.assertEqual(env.get("PRIVATE_KEY_ALT"), "pk-alt")

    def test_put_account_strips_deprecated_strategy_fields(self):
        response = self.client.put(
            "/api/accounts/main",
            json={
                "leader_addresses": ["0xabc"],
                "delayed_follow_enabled": True,
                "super_follow_enabled": True,
                "fixed_shares_count": 10,
                "take_profit_pct": 25,
                "leader_overrides": {
                    "0xabc": {
                        "delayed_follow_enabled": True,
                        "additional_usd_amount": 50,
                        "auto_tp_enabled": True,
                    }
                },
            },
        )
        read_account = self.client.get("/api/accounts/main")

        self.assertEqual(response.status_code, 200)
        payload = read_account.json()
        self.assertNotIn("delayed_follow_enabled", payload)
        self.assertNotIn("super_follow_enabled", payload)
        self.assertNotIn("fixed_shares_count", payload)
        self.assertNotIn("take_profit_pct", payload)
        self.assertEqual(payload.get("leader_overrides"), {})

    def test_env_audit_records_masked_metadata_only(self):
        response = self.client.put(
            "/api/env/MAIN",
            json={
                "PRIVATE_KEY": "secret-private-key",
                "CLOB_SECRET": "secret-clob",
            },
        )

        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT details_json FROM ct_config_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        details = row[0]
        self.assertIn("PRIVATE_KEY", details)
        self.assertIn("CLOB_SECRET", details)
        self.assertIn("masked", details)
        self.assertNotIn("secret-private-key", details)
        self.assertNotIn("secret-clob", details)


if __name__ == "__main__":
    unittest.main()
