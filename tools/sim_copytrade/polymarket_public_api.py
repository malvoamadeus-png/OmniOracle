"""Small, standalone Polymarket public API helpers for sim_copytrade."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"
USER_PNL_METRICS_INTERVAL = "all"
USER_PNL_METRICS_FIDELITY = "12h"

MAX_OPEN_POSITIONS_ROWS = 100_000
MAX_CLOSED_POSITIONS_ROWS = 7500
DEFAULT_CLOSED_PAGE_LIMIT = 50
CLOSED_FETCH_MAX_WORKERS = 8
CLOSED_FETCH_BATCH_PAGES = 8


class _RateLimiter:
    def __init__(self, qps: float):
        self.qps = float(qps)
        self._next_ts = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.qps <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait_s = self._next_ts - now
            if wait_s > 0:
                time.sleep(wait_s)
                now = time.monotonic()
            self._next_ts = max(self._next_ts, now) + 1.0 / self.qps


_LIMITERS: Dict[str, _RateLimiter] = {
    "gamma": _RateLimiter(10.0),
    "data": _RateLimiter(10.0),
    "clob": _RateLimiter(10.0),
    "user_pnl": _RateLimiter(10.0),
}


def _limit_for_url(url: str) -> Optional[_RateLimiter]:
    if url.startswith(GAMMA_API):
        return _LIMITERS["gamma"]
    if url.startswith(DATA_API):
        return _LIMITERS["data"]
    if url.startswith(CLOB_API):
        return _LIMITERS["clob"]
    if url.startswith(USER_PNL_API):
        return _LIMITERS["user_pnl"]
    return None


def _sleep_backoff(attempt: int, base_s: float) -> None:
    time.sleep(base_s * (2**attempt))


def _progress(msg: str) -> None:
    try:
        sys.stderr.write(str(msg).rstrip() + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def http_get_json(
    session: requests.Session,
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout_s: float = 25.0,
    max_retries: int = 3,
) -> Any:
    last_err: Optional[BaseException] = None
    for attempt in range(max_retries):
        try:
            limiter = _limit_for_url(url)
            if limiter is not None:
                limiter.acquire()
            response = session.get(
                url,
                params=params,
                timeout=timeout_s,
                headers={"accept": "application/json"},
            )
            if response.status_code in (429, 500, 502, 503, 504):
                _sleep_backoff(attempt, 1.0)
                continue
            if 400 <= response.status_code < 500:
                raise requests.HTTPError(f"{response.status_code} {response.text[:500]}", response=response)
            response.raise_for_status()
            return response.json()
        except BaseException as exc:
            last_err = exc
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                if exc.response.status_code in (400, 401, 403, 404):
                    raise
            _sleep_backoff(attempt, 1.0)
    raise RuntimeError(f"GET {url} failed after retries: {last_err}")


def fetch_user_pnl_series(
    session: requests.Session,
    address: str,
    interval: str = USER_PNL_METRICS_INTERVAL,
    fidelity: str = USER_PNL_METRICS_FIDELITY,
) -> List[Tuple[str, float]]:
    data = http_get_json(
        session,
        f"{USER_PNL_API}/user-pnl",
        params={"user_address": address, "interval": interval, "fidelity": fidelity},
        timeout_s=20.0,
        max_retries=3,
    )
    if not isinstance(data, list):
        return []

    out: List[Tuple[str, float]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        t = row.get("t")
        p = row.get("p")
        if not isinstance(t, (int, float)) or not isinstance(p, (int, float)):
            continue
        dt = datetime.fromtimestamp(float(t), tz=timezone.utc)
        out.append((dt.isoformat(), float(p)))
    out.sort(key=lambda item: item[0])
    return out


def fetch_positions(session: requests.Session, address: str) -> List[Dict[str, Any]]:
    url = f"{DATA_API}/positions"
    limit = 500
    offset = 0
    out: List[Dict[str, Any]] = []
    while True:
        if len(out) >= MAX_OPEN_POSITIONS_ROWS:
            _progress(f"    positions reached cap={MAX_OPEN_POSITIONS_ROWS}, stop paging")
            break
        data = http_get_json(
            session,
            url,
            params={"user": address, "sizeThreshold": 0, "limit": limit, "offset": offset},
        )
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if isinstance(row, dict):
                out.append(row)
                if len(out) >= MAX_OPEN_POSITIONS_ROWS:
                    break
        if len(data) < limit or len(out) >= MAX_OPEN_POSITIONS_ROWS:
            break
        offset += limit
    return out


def fetch_closed_positions(
    session: requests.Session,
    address: str,
    *,
    limit: int = DEFAULT_CLOSED_PAGE_LIMIT,
    start_offset: int = 0,
    sort_by: str = "TIMESTAMP",
    sort_direction: str = "ASC",
) -> List[Dict[str, Any]]:
    url = f"{DATA_API}/closed-positions"
    page_limit = max(1, min(50, int(limit)))
    offset = max(0, int(start_offset))
    batch_pages = max(1, int(CLOSED_FETCH_BATCH_PAGES))
    max_workers = max(1, int(CLOSED_FETCH_MAX_WORKERS))
    out: List[Dict[str, Any]] = []

    def _fetch_one_page(page_offset: int) -> List[Dict[str, Any]]:
        data = http_get_json(
            session,
            url,
            params={
                "user": address,
                "limit": page_limit,
                "offset": int(page_offset),
                "sortBy": str(sort_by),
                "sortDirection": str(sort_direction),
            },
        )
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while True:
            batch_offsets = [offset + i * page_limit for i in range(batch_pages)]
            futures = {off: pool.submit(_fetch_one_page, off) for off in batch_offsets}
            should_stop = False
            for off in batch_offsets:
                rows = futures[off].result()
                if not rows:
                    should_stop = True
                    break
                out.extend(rows)
                if len(out) >= MAX_CLOSED_POSITIONS_ROWS:
                    out = out[:MAX_CLOSED_POSITIONS_ROWS]
                    should_stop = True
                    break
                if len(rows) < page_limit:
                    should_stop = True
                    break
            if should_stop:
                break
            offset = batch_offsets[-1] + page_limit
    return out
