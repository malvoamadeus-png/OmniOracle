import os
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.ws_proxy import _pick_windows_proxy_value, resolve_websocket_proxy_options


class WebsocketProxyTests(unittest.TestCase):
    def test_loopback_url_bypasses_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            options = resolve_websocket_proxy_options("ws://127.0.0.1:12345")
        self.assertEqual(options, {})

    def test_secure_url_uses_env_proxy(self):
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7890"}, clear=False):
            options = resolve_websocket_proxy_options("wss://example.com/ws")
        self.assertEqual(options["http_proxy_host"], "127.0.0.1")
        self.assertEqual(int(options["http_proxy_port"]), 7890)
        self.assertEqual(options["proxy_type"], "http")

    def test_windows_proxy_server_mapping_prefers_https_for_wss(self):
        options = _pick_windows_proxy_value(
            "http=127.0.0.1:7890;https=127.0.0.1:7891;socks=127.0.0.1:7892",
            is_secure=True,
        )
        self.assertIsNotNone(options)
        self.assertEqual(options["http_proxy_host"], "127.0.0.1")
        self.assertEqual(int(options["http_proxy_port"]), 7891)
        self.assertEqual(options["proxy_type"], "http")


if __name__ == "__main__":
    unittest.main()
