"""Helpers for applying proxy settings to websocket-client connections."""

import os
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    import winreg  # type: ignore
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore


def _split_no_proxy(raw: str) -> List[str]:
    return [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]


def _is_loopback_host(hostname: str) -> bool:
    host = str(hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        return socket.gethostbyname(host).startswith("127.")
    except Exception:
        return False


def _host_matches_no_proxy(hostname: str, entries: List[str]) -> bool:
    host = str(hostname or "").strip().lower()
    if not host:
        return True
    if _is_loopback_host(host):
        return True
    for entry in entries:
        if not entry:
            continue
        normalized = entry.lstrip(".").lower()
        if host == normalized or host.endswith("." + normalized):
            return True
    return False


def _parse_proxy_url(proxy_value: str, *, default_scheme: str) -> Optional[Dict[str, Any]]:
    text = str(proxy_value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"{default_scheme}://{text}"
    parsed = urlparse(text)
    host = str(parsed.hostname or "").strip()
    if not host:
        return None
    scheme = str(parsed.scheme or default_scheme).strip().lower() or default_scheme
    auth = None
    if parsed.username:
        auth = (parsed.username, parsed.password or "")
    proxy_type = "http"
    if scheme.startswith("socks5"):
        proxy_type = "socks5h" if scheme.endswith("h") else "socks5"
    elif scheme.startswith("socks4"):
        proxy_type = "socks4a" if scheme.endswith("a") else "socks4"
    return {
        "http_proxy_host": host,
        "http_proxy_port": int(parsed.port or (80 if proxy_type == "http" else 1080)),
        "http_proxy_auth": auth,
        "proxy_type": proxy_type,
    }


def _pick_windows_proxy_value(proxy_server: str, *, is_secure: bool) -> Optional[Dict[str, Any]]:
    raw = str(proxy_server or "").strip()
    if not raw:
        return None

    # Windows may return either "host:port" or "http=...;https=...;socks=..."
    if "=" not in raw:
        return _parse_proxy_url(raw, default_scheme="http")

    mapping: Dict[str, str] = {}
    for piece in raw.split(";"):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        mapping[key.strip().lower()] = value.strip()

    keys = ["https", "http", "socks"] if is_secure else ["http", "https", "socks"]
    for key in keys:
        value = mapping.get(key)
        if not value:
            continue
        default_scheme = "http" if key in {"http", "https"} else "socks5"
        return _parse_proxy_url(value, default_scheme=default_scheme)
    return None


def _get_windows_internet_proxy(*, is_secure: bool) -> Optional[Dict[str, Any]]:
    if os.name != "nt" or winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            proxy_enable = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            proxy_server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except Exception:
        return None
    if proxy_enable != 1 or not proxy_server:
        return None
    return _pick_windows_proxy_value(proxy_server, is_secure=is_secure)


def resolve_websocket_proxy_options(url: str) -> Dict[str, Any]:
    parsed = urlparse(str(url or "").strip())
    hostname = str(parsed.hostname or "").strip().lower()
    is_secure = parsed.scheme.lower() == "wss"
    no_proxy_entries = _split_no_proxy(os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or "")
    if _host_matches_no_proxy(hostname, no_proxy_entries):
        return {}

    env_key_candidates = ["https_proxy", "all_proxy"] if is_secure else ["http_proxy", "all_proxy"]
    env_value = ""
    for key in env_key_candidates:
        env_value = str(os.environ.get(key) or os.environ.get(key.upper()) or "").strip()
        if env_value:
            break

    proxy = None
    if env_value:
        default_scheme = "http"
        proxy = _parse_proxy_url(env_value, default_scheme=default_scheme)
    else:
        proxy = _get_windows_internet_proxy(is_secure=is_secure)

    if not proxy:
        return {}

    options = dict(proxy)
    if no_proxy_entries:
        options["http_no_proxy"] = no_proxy_entries
    return options
