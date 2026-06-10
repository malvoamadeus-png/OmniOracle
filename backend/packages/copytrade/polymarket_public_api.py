"""Polymarket public API helpers used by the copytrade runtime."""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import requests


DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
USER_PNL_API = "https://user-pnl-api.polymarket.com"


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


def try_midpoints_batch(session: requests.Session, token_ids: List[str]) -> Optional[Dict[str, float]]:
    if not token_ids:
        return {}
    try:
        data = http_get_json(
            session,
            f"{CLOB_API}/midpoints",
            params={"token_ids": ",".join(token_ids)},
            timeout_s=20.0,
            max_retries=2,
        )
    except BaseException:
        return None
    if not isinstance(data, dict):
        return None
    out: Dict[str, float] = {}
    for key, value in data.items():
        try:
            out[str(key)] = float(value)
        except Exception:
            continue
    return out


def fetch_midpoint(session: requests.Session, token_id: str) -> Optional[float]:
    try:
        data = http_get_json(
            session,
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout_s=20.0,
            max_retries=2,
        )
    except BaseException:
        return None
    if isinstance(data, dict):
        for key in ("mid", "midpoint", "price"):
            if key in data:
                try:
                    return float(data[key])
                except Exception:
                    return None
    return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except Exception:
            return None
    return None


def extract_trade_fields(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    side = row.get("side")
    if not isinstance(side, str):
        return None
    side = side.upper()
    if side not in {"BUY", "SELL"}:
        return None

    tx = row.get("transaction_hash") or row.get("transactionHash") or row.get("txHash") or row.get("hash")
    ts = row.get("timestamp") or row.get("time") or row.get("createdAt") or row.get("ts")
    market = row.get("market") or row.get("conditionId") or row.get("condition_id")
    slug = row.get("slug") or row.get("market_slug") or row.get("eventSlug")
    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")

    usd = None
    for key in ("usdcSize", "amountUSD", "amountUsd", "usdc", "usd", "value", "amount"):
        usd = _as_float(row.get(key))
        if usd is not None:
            break

    price = None
    for key in ("price", "avgPrice", "avg_price"):
        price = _as_float(row.get(key))
        if price is not None:
            break

    size = None
    for key in ("size", "shares", "amount", "qty", "quantity"):
        size = _as_float(row.get(key))
        if size is not None:
            break

    outcome_index = row.get("outcome_index") if isinstance(row.get("outcome_index"), int) else row.get("outcomeIndex")
    if not isinstance(outcome_index, int):
        outcome_index = None

    return {
        "tx": str(tx) if tx else None,
        "ts": str(ts) if ts else None,
        "side": side,
        "usd": usd,
        "price": price,
        "size": size,
        "market": str(market) if market else None,
        "slug": str(slug) if slug else None,
        "token_id": str(token_id) if token_id else None,
        "outcome_index": outcome_index,
        "raw": row,
    }


def extract_position_fields(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")
    market = row.get("market") or row.get("conditionId") or row.get("condition_id")
    slug = row.get("slug") or row.get("market_slug") or row.get("eventSlug")
    outcome = row.get("outcome") or row.get("title") or row.get("name")

    size = None
    for key in ("size", "shares", "balance", "quantity", "qty"):
        size = _as_float(row.get(key))
        if size is not None:
            break

    total_bought = _as_float(row.get("totalBought") or row.get("total_bought"))
    initial_value = _as_float(row.get("initialValue") or row.get("initial_value"))
    current_value = _as_float(row.get("currentValue") or row.get("current_value"))
    avg_price = _as_float(row.get("avgPrice") or row.get("avg_price"))

    cost_basis = None
    for key in ("initialValue", "initial_value", "costBasis", "cost_basis", "avgCost", "avg_cost", "cost"):
        cost_basis = _as_float(row.get(key))
        if cost_basis is not None:
            break
    if cost_basis is None and avg_price is not None and size is not None:
        cost_basis = abs(size) * avg_price

    cash_pnl = None
    for key in ("cashPnl", "cash_pnl", "pnl", "profit"):
        cash_pnl = _as_float(row.get(key))
        if cash_pnl is not None:
            break

    realized_pnl = _as_float(row.get("realizedPnl") or row.get("realized_pnl"))
    cur_price = _as_float(row.get("curPrice") or row.get("cur_price"))
    closed = bool(row.get("redeemed") or row.get("closed") or (row.get("redeemable") is True and row.get("size") in (0, "0", 0.0)))

    return {
        "market": str(market) if market else None,
        "slug": str(slug) if slug else None,
        "token_id": str(token_id) if token_id else None,
        "outcome": str(outcome) if isinstance(outcome, str) else None,
        "size": size,
        "total_bought": total_bought,
        "initial_value": initial_value,
        "current_value": current_value,
        "avg_price": avg_price,
        "cost_basis": cost_basis,
        "cash_pnl": cash_pnl,
        "realized_pnl": realized_pnl,
        "cur_price": cur_price,
        "closed": closed,
        "raw": row,
    }
