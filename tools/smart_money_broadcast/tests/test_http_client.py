from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from http_client import (
    _normalize_proxy_url,
    _parse_windows_proxy_server,
    _read_wsl_host_ip,
    detect_proxy_map,
)


class HttpClientTest(unittest.TestCase):
    def test_parse_windows_proxy_server_prefers_https_entry(self) -> None:
        raw = "http=127.0.0.1:7890;https=127.0.0.1:7890;socks=127.0.0.1:7891"
        self.assertEqual(_parse_windows_proxy_server(raw), "127.0.0.1:7890")

    def test_normalize_proxy_url_rewrites_loopback_for_wsl(self) -> None:
        proxy = _normalize_proxy_url("127.0.0.1:7890", wsl_host_ip="172.27.176.1")
        self.assertEqual(proxy, "http://172.27.176.1:7890")

    def test_read_wsl_host_ip(self) -> None:
        path = Path("/tmp/test_resolv.conf")
        path.write_text("nameserver 172.27.176.1\n", encoding="utf-8")
        try:
            self.assertEqual(_read_wsl_host_ip(path), "172.27.176.1")
        finally:
            path.unlink(missing_ok=True)

    def test_detect_proxy_map_from_env(self) -> None:
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.local:8080"}, clear=False):
            proxy_map = detect_proxy_map()
        self.assertEqual(proxy_map, {"http": "http://proxy.local:8080", "https": "http://proxy.local:8080"})

    def test_detect_proxy_map_from_wsl_windows_proxy(self) -> None:
        with patch("http_client._running_in_wsl", return_value=True), patch(
            "http_client._running_on_windows", return_value=False
        ), patch(
            "http_client._read_wsl_host_ip", return_value="172.27.176.1"
        ), patch("http_client._read_windows_proxy_server", return_value="127.0.0.1:7890"), patch(
            "http_client._can_connect", return_value=True
        ), patch.dict("os.environ", {}, clear=True):
            proxy_map = detect_proxy_map()
        self.assertEqual(proxy_map, {"http": "http://172.27.176.1:7890", "https": "http://172.27.176.1:7890"})

    def test_detect_proxy_map_from_native_windows_proxy(self) -> None:
        with patch("http_client._running_in_wsl", return_value=False), patch(
            "http_client._running_on_windows", return_value=True
        ), patch("http_client._read_windows_proxy_server", return_value="127.0.0.1:7890"), patch(
            "http_client._can_connect", return_value=True
        ), patch.dict("os.environ", {}, clear=True):
            proxy_map = detect_proxy_map()
        self.assertEqual(proxy_map, {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"})


if __name__ == "__main__":
    unittest.main()
