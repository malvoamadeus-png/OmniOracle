import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import copytrade.main as main


class SignalHubStatusLoggingTests(unittest.TestCase):
    def test_signal_hub_status_logging_disabled_by_default(self):
        hub = SimpleNamespace(format_status_line=lambda: "connected=1")
        stderr = io.StringIO()

        with patch.dict("os.environ", {}, clear=False), patch("sys.stderr", stderr):
            result = main._maybe_log_signal_hub_status(hub, 0.0)

        self.assertEqual(result, 0.0)
        self.assertEqual(stderr.getvalue(), "")

    def test_signal_hub_status_logging_can_be_enabled_by_env(self):
        hub = SimpleNamespace(format_status_line=lambda: "connected=1")
        stderr = io.StringIO()

        with (
            patch.dict("os.environ", {"COPYTRADE_SIGNAL_HUB_STATUS_LOG": "1"}, clear=False),
            patch("sys.stderr", stderr),
            patch("copytrade.main.time.time", return_value=123.0),
        ):
            result = main._maybe_log_signal_hub_status(hub, 0.0)

        self.assertEqual(result, 123.0)
        self.assertIn("[signal_hub] status connected=1", stderr.getvalue())

    def test_background_task_logging_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(main._background_task_logging_enabled())

    def test_background_task_logging_can_be_enabled_by_env(self):
        with patch.dict("os.environ", {"COPYTRADE_BACKGROUND_TASK_LOG": "1"}, clear=True):
            self.assertTrue(main._background_task_logging_enabled())


if __name__ == "__main__":
    unittest.main()
