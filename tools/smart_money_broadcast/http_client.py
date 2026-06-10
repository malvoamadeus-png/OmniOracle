from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"

_WINDOWS_PROXY_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings"


class RateLimiter:
    def __init__(self, calls: int, window_s: float = 10.0) -> None:
        self.calls = max(1, int(calls))
        self.window_s = float(window_s)
        self.events: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - self.window_s
                while self.events and self.events[0] < cutoff:
                    self.events.popleft()
                if len(self.events) < self.calls:
                    self.events.append(now)
                    return
                wait_s = max(0.02, self.window_s - (now - self.events[0]) + 0.01)
            time.sleep(min(wait_s, 0.25))


def _running_in_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _running_on_windows() -> bool:
    return os.name == "nt"


def _read_wsl_host_ip(resolv_path: Path = Path("/etc/resolv.conf")) -> Optional[str]:
    try:
        for raw in resolv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line.startswith("nameserver "):
                continue
            host = line.split(None, 1)[1].strip()
            return host or None
    except OSError:
        return None
    return None


def _parse_windows_proxy_server(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None
    if "=" in text:
        for part in text.split(";"):
            key, _, value = part.partition("=")
            if key.strip().lower() in {"https", "http"} and value.strip():
                return value.strip()
        return None
    return text


def _read_windows_proxy_server() -> Optional[str]:
    if _running_on_windows():
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
                if not int(enabled):
                    return None
                value, _ = winreg.QueryValueEx(key, "ProxyServer")
                return _parse_windows_proxy_server(str(value))
        except Exception:
            return None

    if not _running_in_wsl():
        return None
    try:
        result = subprocess.run(
            ["cmd.exe", "/c", "reg", "query", _WINDOWS_PROXY_REG_KEY, "/v", "ProxyServer"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for raw in result.stdout.splitlines():
        if "ProxyServer" not in raw:
            continue
        parts = raw.split()
        if not parts:
            continue
        candidate = parts[-1].strip()
        return _parse_windows_proxy_server(candidate)
    return None


def _normalize_proxy_url(raw_proxy: str, *, wsl_host_ip: Optional[str] = None) -> Optional[str]:
    text = str(raw_proxy or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    parsed = urlparse(text)
    if not parsed.hostname or parsed.port is None:
        return None
    host = parsed.hostname
    if wsl_host_ip and host in {"127.0.0.1", "localhost", "::1"}:
        host = wsl_host_ip
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"
    return f"{parsed.scheme}://{auth}{host}:{parsed.port}"


def _can_connect(proxy_url: str, timeout_s: float = 0.35) -> bool:
    parsed = urlparse(proxy_url)
    if not parsed.hostname or parsed.port is None:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=timeout_s):
            return True
    except OSError:
        return False


def detect_proxy_map() -> Optional[Dict[str, str]]:
    env_proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    if env_proxy:
        normalized = _normalize_proxy_url(env_proxy)
        if normalized:
            return {"http": normalized, "https": normalized}

    if not _running_in_wsl() and not _running_on_windows():
        return None

    wsl_host_ip = _read_wsl_host_ip() if _running_in_wsl() else None
    raw_windows_proxy = _read_windows_proxy_server()
    if not raw_windows_proxy:
        return None
    normalized = _normalize_proxy_url(raw_windows_proxy, wsl_host_ip=wsl_host_ip)
    if not normalized or not _can_connect(normalized):
        return None
    return {"http": normalized, "https": normalized}


class ApiClient:
    def __init__(self, timeout_s: float = 20.0) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.timeout_s = float(timeout_s)
        self.proxies = detect_proxy_map()
        if self.proxies:
            self.session.proxies.update(self.proxies)
        self.limiters = {
            "gamma": RateLimiter(10, window_s=1.0),
            "data": RateLimiter(10, window_s=1.0),
            "pnl": RateLimiter(10, window_s=1.0),
        }

    def close(self) -> None:
        self.session.close()

    def _key_for_url(self, url: str) -> str:
        if "gamma-api.polymarket.com" in url:
            return "gamma"
        if "user-pnl-api.polymarket.com" in url:
            return "pnl"
        return "data"

    def get_json(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout_s: Optional[float] = None,
        max_retries: int = 4,
        backoff_s: float = 0.6,
    ) -> Any:
        last_err: Optional[BaseException] = None
        key = self._key_for_url(url)
        for attempt in range(max_retries):
            try:
                self.limiters[key].acquire()
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=float(timeout_s or self.timeout_s),
                    headers={"accept": "application/json"},
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(backoff_s * (2**attempt))
                    continue
                if 400 <= resp.status_code < 500:
                    raise RuntimeError(f"GET {resp.url} failed: {resp.status_code} {resp.text[:400]}")
                resp.raise_for_status()
                return resp.json()
            except BaseException as exc:  # noqa: BLE001
                last_err = exc
                if attempt < max_retries - 1:
                    time.sleep(backoff_s * (2**attempt))
        raise RuntimeError(f"GET {url} failed after retries: {last_err}")


def normalize_address(value: Any) -> str:
    return str(value or "").strip().lower()


def to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            out = float(text)
        except ValueError:
            return None
        return out if out == out and out not in (float("inf"), float("-inf")) else None
    return None
