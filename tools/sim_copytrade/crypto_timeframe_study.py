from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for _path in (SIM_ROOT, SIM_PARENT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from main import as_float, format_utc_from_epoch, now_utc_iso, parse_epoch, short_address  # type: ignore
from polymarket_public_api import fetch_closed_positions, fetch_positions, http_get_json  # type: ignore


DATA_API = "https://data-api.polymarket.com/activity"
GAMMA_MARKETS_API = "https://gamma-api.polymarket.com/markets"
TARGET_BUCKETS = ("5m", "15m", "1h", "4h", "1d", "1w")
BUCKET_DURATION_MINUTES = {
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
    "1w": 10080,
}
DURATION_TO_BUCKET = {minutes: bucket for bucket, minutes in BUCKET_DURATION_MINUTES.items()}
EPS = 1e-9

CRYPTO_NAME_ALIASES = {
    "Bitcoin": ("Bitcoin", "BTC"),
    "Ethereum": ("Ethereum", "ETH"),
    "Solana": ("Solana", "SOL"),
    "XRP": ("XRP",),
    "Dogecoin": ("Dogecoin", "DOGE"),
    "BNB": ("BNB",),
    "Cardano": ("Cardano", "ADA"),
    "Avalanche": ("Avalanche", "AVAX"),
    "Sui": ("Sui",),
    "Litecoin": ("Litecoin", "LTC"),
    "Chainlink": ("Chainlink", "LINK"),
    "Official Trump": ("Official Trump", "TRUMP"),
    "MELANIA": ("MELANIA",),
    "Hyperliquid": ("Hyperliquid", "HYPE"),
    "Berachain": ("Berachain",),
    "Kaspa": ("Kaspa",),
    "Monero": ("Monero",),
    "Toncoin": ("Toncoin", "TON"),
    "Aptos": ("Aptos",),
    "Arbitrum": ("Arbitrum", "ARB"),
    "Sei": ("Sei",),
    "Pepe": ("Pepe",),
    "Fartcoin": ("Fartcoin",),
    "Bonk": ("Bonk",),
    "Jupiter": ("Jupiter",),
    "Jito": ("Jito",),
    "Ondo": ("Ondo",),
    "ENA": ("ENA",),
    "HBAR": ("HBAR",),
    "Polkadot": ("Polkadot", "DOT"),
    "NEAR": ("NEAR",),
    "TAO": ("TAO",),
    "PENGU": ("PENGU",),
    "SHIB": ("SHIB",),
    "Pi Network": ("Pi Network",),
    "IP": ("IP",),
}

CRYPTO_ALIASES = tuple(alias for aliases in CRYPTO_NAME_ALIASES.values() for alias in aliases)
CRYPTO_ALIAS_TO_NAME = {
    alias.lower(): canonical_name
    for canonical_name, aliases in CRYPTO_NAME_ALIASES.items()
    for alias in aliases
}
CRYPTO_ALIAS_PATTERN = "|".join(re.escape(alias) for alias in sorted(CRYPTO_ALIASES, key=len, reverse=True))
CRYPTO_TITLE_RE = re.compile(
    r"^(?P<alias>" + CRYPTO_ALIAS_PATTERN + r")\b",
    re.IGNORECASE,
)
CRYPTO_ANYWHERE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<alias>" + CRYPTO_ALIAS_PATTERN + r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
TIME_WINDOW_RE = re.compile(
    r" - [A-Za-z]+ \d{1,2}, (?P<start>\d{1,2}(?::\d{2})?[AP]M)-(?P<end>\d{1,2}(?::\d{2})?[AP]M) ET$",
)
SINGLE_TIME_RE = re.compile(
    r" - [A-Za-z]+ \d{1,2}, (?P<anchor>\d{1,2}(?::\d{2})?[AP]M) ET$",
)
EXPLICIT_DURATION_SLUG_RE = re.compile(
    r"-(?P<duration>(?:5m|15m|30m|1h|2h|4h|6h|12h|24h|1d|7d|1w))-(?:\d+)$",
    re.IGNORECASE,
)
HOURLY_UPDOWN_SLUG_RE = re.compile(
    r"(?:^|/)(?:bitcoin|ethereum|solana|xrp|dogecoin|bnb|cardano|avalanche|sui|litecoin|chainlink|btc|eth|sol|xrp)"
    r"-up-or-down-.*-\d{1,2}(?::\d{2})?(?:am|pm)-et$",
    re.IGNORECASE,
)
DATE_WITH_OPTIONAL_TIME_PATTERN = (
    r"[A-Za-z]+ \d{1,2}(?:, \d{4})?(?:, \d{1,2}(?::\d{2})?[AP]M ET)?"
)
DATE_RANGE_PATTERN = (
    r"(?:"
    r"[A-Za-z]+ \d{1,2}\s*(?:-|to|through|–)\s*\d{1,2}"
    r"|"
    r"[A-Za-z]+ \d{1,2}\s*(?:-|to|through|–)\s*[A-Za-z]+ \d{1,2}"
    r")(?:, \d{4})?"
)
DAILY_UPDOWN_ON_RE = re.compile(
    rf"^(?P<alias>{CRYPTO_ALIAS_PATTERN}) Up or Down on {DATE_WITH_OPTIONAL_TIME_PATTERN}\??$",
    re.IGNORECASE,
)
DAILY_PRICE_HIT_RE = re.compile(
    rf"^What price will (?P<alias>{CRYPTO_ALIAS_PATTERN}) hit on {DATE_WITH_OPTIONAL_TIME_PATTERN}\??$",
    re.IGNORECASE,
)
WEEKLY_ABOVE_RE = re.compile(
    rf"^(?P<alias>{CRYPTO_ALIAS_PATTERN}) above .+ on {DATE_WITH_OPTIONAL_TIME_PATTERN}\??$",
    re.IGNORECASE,
)
WEEKLY_PRICE_HIT_RANGE_RE = re.compile(
    rf"^What price will (?P<alias>{CRYPTO_ALIAS_PATTERN}) hit (?:on )?{DATE_RANGE_PATTERN}\??$",
    re.IGNORECASE,
)


@dataclass
class ActivityTradeRow:
    tx_hash: str
    ts: int
    side: str
    condition_id: str
    asset_id: str
    outcome: str
    title: str
    market_slug: str
    price: Optional[float]
    size: Optional[float]
    usd: Optional[float]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Study a wallet's crypto timed-market performance and hedge-side behavior"
    )
    ap.add_argument("--address", required=True, help="Wallet address to inspect")
    ap.add_argument(
        "--lookback-trades",
        "--max-activities",
        dest="lookback_trades",
        type=int,
        default=300000,
        help="How many recent trade activity rows to fetch (default: 300000)",
    )
    ap.add_argument("--page-limit", type=int, default=1000, help=argparse.SUPPRESS)
    ap.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Optional output directory (default: <script_dir>/output/crypto_timeframe_study)",
    )
    return ap.parse_args()


def _parse_clock(value: str) -> datetime:
    s = str(value or "").strip().upper()
    if ":" in s:
        return datetime.strptime(s, "%I:%M%p")
    return datetime.strptime(s, "%I%p")


def _bucket_from_duration_minutes(duration_minutes: int) -> Optional[str]:
    return DURATION_TO_BUCKET.get(int(duration_minutes))


def _duration_from_slug(slug: Any) -> Optional[int]:
    raw_slug = str(slug or "").strip().lower()
    if not raw_slug:
        return None

    explicit = EXPLICIT_DURATION_SLUG_RE.search(raw_slug)
    if explicit:
        token = explicit.group("duration").lower()
        mapping = {
            "5m": 5,
            "15m": 15,
            "30m": 30,
            "1h": 60,
            "2h": 120,
            "4h": 240,
            "6h": 360,
            "12h": 720,
            "24h": 1440,
            "1d": 1440,
            "7d": 10080,
            "1w": 10080,
        }
        return mapping.get(token)

    if HOURLY_UPDOWN_SLUG_RE.search(raw_slug):
        return 60
    return None


def _duration_reason(duration_minutes: int) -> str:
    minutes = int(duration_minutes)
    if minutes < 60:
        return "unsupported_subhour_duration"
    if minutes < 240:
        return "unsupported_intraday_duration"
    if minutes < 1440:
        return "unsupported_multihour_duration"
    if minutes < 10080:
        return "unsupported_multiday_duration"
    return "unsupported_long_duration"


def _classify_non_window_title(raw_title: str) -> Optional[Dict[str, Any]]:
    title = str(raw_title or "").strip()
    if not title:
        return None

    daily_matchers = (DAILY_UPDOWN_ON_RE, DAILY_PRICE_HIT_RE)
    for matcher in daily_matchers:
        if matcher.search(title):
            return {
                "is_crypto_timed_market": True,
                "duration_minutes": BUCKET_DURATION_MINUTES["1d"],
                "bucket": "1d",
                "reason": "title_daily_contract",
            }

    weekly_matchers = (WEEKLY_ABOVE_RE, WEEKLY_PRICE_HIT_RANGE_RE)
    for matcher in weekly_matchers:
        if matcher.search(title):
            return {
                "is_crypto_timed_market": True,
                "duration_minutes": BUCKET_DURATION_MINUTES["1w"],
                "bucket": "1w",
                "reason": "title_weekly_contract",
            }

    return None


def extract_crypto_label(title: Any, market_slug: Any = "") -> Optional[str]:
    raw_title = str(title or "").strip()
    if raw_title:
        prefix = raw_title.split(" Up or Down - ", 1)[0].strip()
        canonical = CRYPTO_ALIAS_TO_NAME.get(prefix.lower())
        if canonical:
            return canonical
        title_match = CRYPTO_TITLE_RE.search(raw_title)
        if title_match:
            alias = str(title_match.group("alias") or "").strip().lower()
            canonical = CRYPTO_ALIAS_TO_NAME.get(alias)
            if canonical:
                return canonical
        anywhere_match = CRYPTO_ANYWHERE_RE.search(raw_title)
        if anywhere_match:
            alias = str(anywhere_match.group("alias") or "").strip().lower()
            canonical = CRYPTO_ALIAS_TO_NAME.get(alias)
            if canonical:
                return canonical

    raw_slug = str(market_slug or "").strip().lower()
    if raw_slug:
        for alias, canonical in sorted(CRYPTO_ALIAS_TO_NAME.items(), key=lambda item: len(item[0]), reverse=True):
            marker = f"{alias}-up-or-down-"
            if raw_slug.startswith(marker) or f"/{marker}" in raw_slug:
                return canonical
    return None


def classify_market_title(title: Any, market_slug: Any = "") -> Dict[str, Any]:
    raw_title = str(title or "").strip()
    raw_slug = str(market_slug or "").strip()
    result: Dict[str, Any] = {
        "title": raw_title,
        "market_slug": raw_slug,
        "is_crypto_timed_market": False,
        "duration_minutes": None,
        "bucket": None,
        "reason": None,
    }
    has_title = bool(raw_title)
    has_updown_title = has_title and " Up or Down - " in raw_title
    is_crypto_title = bool(extract_crypto_label(raw_title, raw_slug))

    if has_updown_title and is_crypto_title:
        range_match = TIME_WINDOW_RE.search(raw_title)
        if range_match:
            start_clock = _parse_clock(range_match.group("start"))
            end_clock = _parse_clock(range_match.group("end"))
            duration_minutes = int((end_clock - start_clock).total_seconds() / 60)
            if duration_minutes <= 0:
                duration_minutes += 24 * 60
            bucket = _bucket_from_duration_minutes(duration_minutes)
            result["is_crypto_timed_market"] = True
            result["duration_minutes"] = duration_minutes
            result["bucket"] = bucket
            result["reason"] = "title_range" if bucket else _duration_reason(duration_minutes)
            return result

        single_match = SINGLE_TIME_RE.search(raw_title)
        if single_match:
            # Legacy hourly titles omit the end timestamp and refer to the 1H candle
            # beginning at the stated ET time.
            result["is_crypto_timed_market"] = True
            result["duration_minutes"] = 60
            result["bucket"] = "1h"
            result["reason"] = "single_time_hourly"
            return result

    discrete_title = _classify_non_window_title(raw_title)
    if discrete_title is not None:
        result.update(discrete_title)
        return result

    slug_duration = _duration_from_slug(raw_slug)
    if slug_duration is not None:
        bucket = _bucket_from_duration_minutes(slug_duration)
        result["is_crypto_timed_market"] = True
        result["duration_minutes"] = slug_duration
        result["bucket"] = bucket
        result["reason"] = "slug_duration" if bucket else _duration_reason(slug_duration)
        return result

    if not raw_title:
        result["reason"] = "missing_title"
        return result
    if not is_crypto_title:
        result["reason"] = "non_crypto"
        return result
    result["reason"] = "unparseable_duration"
    return result


def parse_activity_trade_row(row: Dict[str, Any]) -> Optional[ActivityTradeRow]:
    if not isinstance(row, dict):
        return None
    side = str(row.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return None

    ts = parse_epoch(row.get("timestamp") or row.get("time") or row.get("createdAt") or row.get("ts"))
    if ts is None:
        return None

    asset_id = str(row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("tokenID") or "").strip()
    if not asset_id:
        return None

    condition_id = str(row.get("market") or row.get("conditionId") or row.get("condition_id") or "").strip()
    if not condition_id:
        return None

    price = None
    for key in ("price", "avgPrice", "avg_price"):
        price = as_float(row.get(key))
        if price is not None:
            break

    size = None
    for key in ("size", "shares", "amount", "qty", "quantity"):
        size = as_float(row.get(key))
        if size is not None:
            break

    usd = None
    for key in ("usdcSize", "amountUSD", "amountUsd", "usdc", "usd", "value", "amount"):
        usd = as_float(row.get(key))
        if usd is not None:
            break

    if size is None and usd is not None and price is not None and price > 0:
        size = usd / price
    if usd is None and size is not None and price is not None:
        usd = size * price

    return ActivityTradeRow(
        tx_hash=str(
            row.get("transaction_hash")
            or row.get("transactionHash")
            or row.get("txHash")
            or row.get("hash")
            or ""
        ),
        ts=int(ts),
        side=side,
        condition_id=condition_id,
        asset_id=asset_id,
        outcome=str(row.get("outcome") or "").strip(),
        title=str(row.get("title") or row.get("question") or "").strip(),
        market_slug=str(row.get("eventSlug") or row.get("slug") or row.get("marketSlug") or "").strip(),
        price=price,
        size=size,
        usd=usd,
    )


def fetch_activity_trade_rows(
    session: requests.Session,
    address: str,
    *,
    max_activities: int,
    page_limit: int,
) -> Tuple[List[ActivityTradeRow], int]:
    rows_desc: List[ActivityTradeRow] = []
    raw_count = 0
    end_cursor: Optional[int] = None
    page = 0

    while len(rows_desc) < max_activities:
        remaining = max_activities - len(rows_desc)
        current_limit = min(max(1, int(page_limit)), remaining)
        if current_limit <= 0:
            break

        params: Dict[str, Any] = {
            "user": address,
            "type": "TRADE",
            "limit": current_limit,
            "offset": 0,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        if end_cursor is not None:
            params["end"] = end_cursor

        data = http_get_json(session, DATA_API, params=params, timeout_s=30.0, max_retries=4)
        if not isinstance(data, list) or not data:
            break

        raw_count += len(data)
        page += 1
        parsed_page: List[ActivityTradeRow] = []
        oldest_ts: Optional[int] = None
        for item in data:
            parsed = parse_activity_trade_row(item)
            if parsed is None:
                continue
            parsed_page.append(parsed)
            oldest_ts = parsed.ts if oldest_ts is None else min(oldest_ts, parsed.ts)

        rows_desc.extend(parsed_page)
        print(f"[activity] page={page} parsed={len(parsed_page)} total={len(rows_desc)}")

        if oldest_ts is None:
            break
        end_cursor = oldest_ts - 1
        if len(data) < current_limit:
            break

    if len(rows_desc) > max_activities:
        rows_desc = rows_desc[:max_activities]

    seen: set = set()
    deduped: List[ActivityTradeRow] = []
    for row in rows_desc:
        key = (
            row.tx_hash,
            row.condition_id,
            row.asset_id,
            row.side,
            row.ts,
            round(row.price, 8) if row.price is not None else None,
            round(row.size, 8) if row.size is not None else None,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    deduped.sort(key=lambda item: (item.ts, item.tx_hash, item.condition_id, item.asset_id, item.side))
    return deduped, raw_count


def update_market_meta(
    meta_by_condition: Dict[str, Dict[str, Any]],
    *,
    condition_id: Any,
    title: Any,
    market_slug: Any = "",
    asset_id: Any = "",
    outcome: Any = "",
) -> None:
    cid = str(condition_id or "").strip()
    if not cid:
        return

    meta = meta_by_condition.setdefault(
        cid,
        {
            "condition_id": cid,
            "title": "",
            "market_slug": "",
            "crypto_label": "",
            "duration_minutes": None,
            "bucket": None,
            "is_crypto_timed_market": False,
            "classify_reason": "missing_title",
            "outcome_by_asset": {},
        },
    )

    if asset_id and outcome:
        meta["outcome_by_asset"][str(asset_id)] = str(outcome)

    title_text = str(title or "").strip()
    if title_text and not meta["title"]:
        meta["title"] = title_text
    if market_slug and not meta["market_slug"]:
        meta["market_slug"] = str(market_slug)
    if not meta.get("crypto_label"):
        crypto_label = extract_crypto_label(meta.get("title") or title_text, meta.get("market_slug") or market_slug)
        if crypto_label:
            meta["crypto_label"] = crypto_label

    classify_title = meta.get("title") or title_text
    classify_slug = meta.get("market_slug") or str(market_slug or "").strip()
    if classify_title or classify_slug:
        classified = classify_market_title(classify_title, classify_slug)
        should_update_classification = (
            classified.get("duration_minutes") is not None
            and (
                meta.get("duration_minutes") is None
                or (meta.get("bucket") is None and classified.get("bucket") is not None)
                or (
                    not meta.get("is_crypto_timed_market")
                    and bool(classified.get("is_crypto_timed_market"))
                )
            )
        )
        if should_update_classification:
            meta["duration_minutes"] = int(classified["duration_minutes"])
            meta["bucket"] = classified.get("bucket")
            meta["is_crypto_timed_market"] = bool(classified.get("is_crypto_timed_market"))
            meta["classify_reason"] = classified.get("reason")
        elif not meta.get("title"):
            meta["title"] = title_text


def fetch_gamma_market_meta_by_conditions(
    session: requests.Session,
    condition_ids: Iterable[str],
    *,
    batch_size: int = 50,
) -> Dict[str, Dict[str, Any]]:
    targets = [str(cid).strip() for cid in condition_ids if str(cid).strip()]
    out: Dict[str, Dict[str, Any]] = {}
    if not targets:
        return out

    for start in range(0, len(targets), max(1, int(batch_size))):
        batch = targets[start : start + max(1, int(batch_size))]
        params = [("condition_ids", cid) for cid in batch]
        data = http_get_json(session, GAMMA_MARKETS_API, params=params, timeout_s=30.0, max_retries=4)
        if not isinstance(data, list):
            continue
        for row in data:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("conditionId") or "").strip()
            if not cid:
                continue
            event0 = None
            events = row.get("events")
            if isinstance(events, list) and events and isinstance(events[0], dict):
                event0 = events[0]
            out[cid] = {
                "condition_id": cid,
                "title": str(
                    row.get("question")
                    or row.get("title")
                    or (event0.get("title") if isinstance(event0, dict) else "")
                    or ""
                ).strip(),
                "market_slug": str(
                    row.get("slug")
                    or row.get("marketSlug")
                    or (event0.get("slug") if isinstance(event0, dict) else "")
                    or ""
                ).strip(),
            }
    return out


def build_market_meta(
    session: requests.Session,
    activity_rows: Iterable[ActivityTradeRow],
    open_positions: Iterable[Dict[str, Any]],
    closed_positions: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    meta_by_condition: Dict[str, Dict[str, Any]] = {}
    for row in activity_rows:
        update_market_meta(
            meta_by_condition,
            condition_id=row.condition_id,
            title=row.title,
            market_slug=row.market_slug,
            asset_id=row.asset_id,
            outcome=row.outcome,
        )
    for source_rows in (open_positions, closed_positions):
        for row in source_rows:
            if not isinstance(row, dict):
                continue
            update_market_meta(
                meta_by_condition,
                condition_id=row.get("conditionId") or row.get("condition_id") or row.get("market"),
                title=row.get("title") or row.get("question"),
                market_slug=row.get("marketSlug") or row.get("slug"),
                asset_id=row.get("asset"),
                outcome=row.get("outcome"),
            )
    unresolved = sorted(
        cid
        for cid, meta in meta_by_condition.items()
        if (not meta.get("title") or meta.get("duration_minutes") is None)
    )
    if unresolved:
        gamma_meta = fetch_gamma_market_meta_by_conditions(session, unresolved)
        for cid, item in gamma_meta.items():
            update_market_meta(
                meta_by_condition,
                condition_id=cid,
                title=item.get("title"),
                market_slug=item.get("market_slug"),
            )
    return meta_by_condition


def build_market_pnl_maps(
    open_positions: Iterable[Dict[str, Any]],
    closed_positions: Iterable[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    markets: Dict[str, Dict[str, Any]] = {}

    def _market(cid: str) -> Dict[str, Any]:
        return markets.setdefault(
            cid,
            {
                "condition_id": cid,
                "open_cash_pnl": 0.0,
                "realized_pnl": 0.0,
                "open_rows": 0,
                "closed_rows": 0,
            },
        )

    for row in open_positions:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conditionId") or row.get("condition_id") or row.get("market") or "").strip()
        if not cid:
            continue
        market = _market(cid)
        market["open_rows"] += 1
        market["open_cash_pnl"] += as_float(row.get("cashPnl")) or 0.0
        market["realized_pnl"] += as_float(row.get("realizedPnl")) or 0.0

    for row in closed_positions:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("conditionId") or row.get("condition_id") or row.get("market") or "").strip()
        if not cid:
            continue
        market = _market(cid)
        market["closed_rows"] += 1
        market["realized_pnl"] += as_float(row.get("realizedPnl")) or 0.0

    for market in markets.values():
        market["open_cash_pnl"] = round(float(market["open_cash_pnl"]), 6)
        market["realized_pnl"] = round(float(market["realized_pnl"]), 6)
        market["total_pnl"] = round(float(market["realized_pnl"] + market["open_cash_pnl"]), 6)
    return markets


def _init_selected_market(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "condition_id": meta.get("condition_id"),
        "title": meta.get("title") or "",
        "market_slug": meta.get("market_slug") or "",
        "crypto_label": meta.get("crypto_label") or "",
        "bucket": meta.get("bucket"),
        "duration_minutes": meta.get("duration_minutes"),
        "trade_rows": 0,
        "buy_rows": 0,
        "sell_rows": 0,
        "buy_usd": 0.0,
        "sell_usd": 0.0,
        "activity_days": set(),
        "asset_trade_rows": Counter(),
        "asset_buy_usd": defaultdict(float),
        "asset_buy_rows": Counter(),
        "asset_sell_usd": defaultdict(float),
        "asset_net_shares": defaultdict(float),
        "outcome_by_asset": dict(meta.get("outcome_by_asset") or {}),
        "opposite_buy_event_count": 0,
        "opposite_buy_usd": 0.0,
        "opposite_buy_examples": [],
    }


def build_selected_market_activity(
    activity_rows: Iterable[ActivityTradeRow],
    meta_by_condition: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[ActivityTradeRow]]:
    selected: Dict[str, Dict[str, Any]] = {}
    selected_rows: List[ActivityTradeRow] = []
    excluded_event_rows = Counter()
    excluded_market_sets: Dict[str, set] = defaultdict(set)

    for row in activity_rows:
        meta = meta_by_condition.get(row.condition_id)
        if meta is None:
            meta = {
                "condition_id": row.condition_id,
                "title": row.title,
                "bucket": None,
                "duration_minutes": None,
                "is_crypto_timed_market": False,
                "classify_reason": "missing_meta",
                "crypto_label": extract_crypto_label(row.title, row.market_slug) or "",
                "outcome_by_asset": {row.asset_id: row.outcome} if row.outcome else {},
            }

        reason = None
        if not meta.get("is_crypto_timed_market"):
            reason = meta.get("classify_reason") or "non_crypto"
        elif meta.get("bucket") not in TARGET_BUCKETS:
            reason = meta.get("classify_reason") or "unsupported_duration"

        if reason:
            excluded_event_rows[reason] += 1
            excluded_market_sets[reason].add(row.condition_id)
            continue

        market = selected.setdefault(row.condition_id, _init_selected_market(meta))
        market["trade_rows"] += 1
        market["asset_trade_rows"][row.asset_id] += 1
        market["activity_days"].add(datetime.fromtimestamp(row.ts, tz=timezone.utc).date().isoformat())
        market["outcome_by_asset"].setdefault(row.asset_id, row.outcome)
        selected_rows.append(row)

        usd = float(row.usd or 0.0)
        if row.side == "BUY":
            if any(size > EPS for asset, size in market["asset_net_shares"].items() if asset != row.asset_id):
                market["opposite_buy_event_count"] += 1
                market["opposite_buy_usd"] += usd
                if len(market["opposite_buy_examples"]) < 5:
                    market["opposite_buy_examples"].append(
                        {
                            "ts": row.ts,
                            "utc": format_utc_from_epoch(row.ts),
                            "asset_id": row.asset_id,
                            "outcome": row.outcome,
                            "usd": round(usd, 6),
                        }
                    )
            market["buy_rows"] += 1
            market["buy_usd"] += usd
            market["asset_buy_rows"][row.asset_id] += 1
            market["asset_buy_usd"][row.asset_id] += usd
            if row.size is not None:
                market["asset_net_shares"][row.asset_id] += float(row.size)
        else:
            market["sell_rows"] += 1
            market["sell_usd"] += usd
            market["asset_sell_usd"][row.asset_id] += usd
            if row.size is not None:
                market["asset_net_shares"][row.asset_id] -= float(row.size)

    for market in selected.values():
        buy_values = [value for value in market["asset_buy_usd"].values() if value > 0]
        total_buy = float(market["buy_usd"])
        dominant_buy = max(buy_values) if buy_values else 0.0
        balanced_other = max(0.0, total_buy - dominant_buy)
        hedge_coverage = 0.0
        if total_buy > 0:
            hedge_coverage = 2.0 * min(dominant_buy, balanced_other) / total_buy

        buy_asset_count = sum(1 for value in market["asset_buy_usd"].values() if value > EPS)
        trade_asset_count = sum(1 for value in market["asset_trade_rows"].values() if value > 0)

        dominant_asset = None
        dominant_outcome = None
        if market["asset_buy_usd"]:
            dominant_asset = max(market["asset_buy_usd"].items(), key=lambda item: item[1])[0]
            dominant_outcome = market["outcome_by_asset"].get(dominant_asset) or ""

        market["activity_days"] = sorted(market["activity_days"])
        market["buy_usd"] = round(float(market["buy_usd"]), 6)
        market["sell_usd"] = round(float(market["sell_usd"]), 6)
        market["opposite_buy_usd"] = round(float(market["opposite_buy_usd"]), 6)
        market["buy_asset_count"] = buy_asset_count
        market["trade_asset_count"] = trade_asset_count
        market["both_side_buy_market"] = buy_asset_count >= 2
        market["both_side_trade_market"] = trade_asset_count >= 2
        market["hedge_coverage"] = round(float(hedge_coverage), 6)
        market["opposite_buy_usd_share"] = round(
            float(market["opposite_buy_usd"] / total_buy) if total_buy > 0 else 0.0,
            6,
        )
        total_flow_usd = float(market["buy_usd"] + market["sell_usd"])
        market["explicit_sell_row_share"] = round(
            float(market["sell_rows"] / market["trade_rows"]) if market["trade_rows"] > 0 else 0.0,
            6,
        )
        market["explicit_sell_usd_share"] = round(
            float(market["sell_usd"] / total_flow_usd) if total_flow_usd > 0 else 0.0,
            6,
        )
        market["dominant_outcome"] = dominant_outcome
        market["dominant_asset_id"] = dominant_asset
        hedge_side_asset = None
        hedge_side_buy_rows = 0
        hedge_side_buy_usd = 0.0
        if market["both_side_buy_market"]:
            positive_assets = [(asset, usd) for asset, usd in market["asset_buy_usd"].items() if usd > EPS]
            if positive_assets:
                hedge_side_asset, hedge_side_buy_usd = min(positive_assets, key=lambda item: item[1])
                hedge_side_buy_rows = int(market["asset_buy_rows"].get(hedge_side_asset, 0))
        market["hedge_side_asset_id"] = hedge_side_asset
        market["hedge_side_outcome"] = market["outcome_by_asset"].get(hedge_side_asset) if hedge_side_asset else ""
        market["hedge_side_buy_rows"] = hedge_side_buy_rows
        market["hedge_side_buy_usd"] = round(float(hedge_side_buy_usd), 6)
        market["hedge_side_buy_usd_share"] = round(
            float(hedge_side_buy_usd / total_buy) if total_buy > 0 else 0.0,
            6,
        )
        market["asset_buy_usd"] = {key: round(float(value), 6) for key, value in market["asset_buy_usd"].items()}
        market["asset_sell_usd"] = {key: round(float(value), 6) for key, value in market["asset_sell_usd"].items()}
        market["asset_trade_rows"] = dict(market["asset_trade_rows"])
        market["asset_buy_rows"] = dict(market["asset_buy_rows"])

    excluded_counts = {
        "event_rows": dict(excluded_event_rows),
        "unique_markets": {reason: len(values) for reason, values in excluded_market_sets.items()},
    }
    return selected, excluded_counts, selected_rows


def enrich_markets_with_pnl(
    market_activity: Dict[str, Dict[str, Any]],
    pnl_by_condition: Dict[str, Dict[str, Any]],
) -> None:
    for cid, market in market_activity.items():
        pnl = pnl_by_condition.get(cid, {})
        market["open_cash_pnl"] = round(float(pnl.get("open_cash_pnl") or 0.0), 6)
        market["realized_pnl"] = round(float(pnl.get("realized_pnl") or 0.0), 6)
        market["total_pnl"] = round(float(market["open_cash_pnl"] + market["realized_pnl"]), 6)
        market["pnl_rows_open"] = int(pnl.get("open_rows") or 0)
        market["pnl_rows_closed"] = int(pnl.get("closed_rows") or 0)


def safe_mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.mean(values)), 6)


def safe_median(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return round(float(statistics.median(values)), 6)


def safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(float(numerator / denominator), 6)


def build_bucket_stats(market_activity: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    bucket_stats: Dict[str, Dict[str, Any]] = {}
    for bucket in TARGET_BUCKETS:
        markets = [market for market in market_activity.values() if market.get("bucket") == bucket]
        trade_counts = [int(market["trade_rows"]) for market in markets]
        buy_counts = [int(market["buy_rows"]) for market in markets]
        hedge_coverages = [float(market["hedge_coverage"]) for market in markets]
        hedge_side_buy_rows = [float(market["hedge_side_buy_rows"]) for market in markets]
        hedge_side_buy_usd_shares = [float(market["hedge_side_buy_usd_share"]) for market in markets]
        pnls = [float(market["total_pnl"]) for market in markets]

        trade_rows = int(sum(market["trade_rows"] for market in markets))
        buy_rows = int(sum(market["buy_rows"] for market in markets))
        sell_rows = int(sum(market["sell_rows"] for market in markets))
        buy_usd = float(sum(market["buy_usd"] for market in markets))
        sell_usd = float(sum(market["sell_usd"] for market in markets))
        realized_pnl = float(sum(market["realized_pnl"] for market in markets))
        open_cash_pnl = float(sum(market["open_cash_pnl"] for market in markets))
        total_pnl = realized_pnl + open_cash_pnl
        active_days = len({day for market in markets for day in market["activity_days"]})
        winning_markets = sum(1 for market in markets if market["total_pnl"] > 0)

        bucket_stats[bucket] = {
            "bucket": bucket,
            "duration_minutes": BUCKET_DURATION_MINUTES[bucket],
            "has_sample": bool(markets),
            "sample_note": None if markets else "no sample",
            "trade_rows": trade_rows,
            "buy_rows": buy_rows,
            "sell_rows": sell_rows,
            "unique_markets": len(markets),
            "active_days": active_days,
            "avg_trades_per_market": safe_mean([float(value) for value in trade_counts]),
            "median_trades_per_market": safe_median([float(value) for value in trade_counts]),
            "avg_buys_per_market": safe_mean([float(value) for value in buy_counts]),
            "median_buys_per_market": safe_median([float(value) for value in buy_counts]),
            "realized_pnl": round(realized_pnl, 6),
            "open_cash_pnl": round(open_cash_pnl, 6),
            "total_pnl": round(total_pnl, 6),
            "buy_usd": round(buy_usd, 6),
            "roi": safe_ratio(total_pnl, buy_usd),
            "market_win_rate": safe_ratio(float(winning_markets), float(len(markets))) if markets else None,
            "avg_pnl_per_market": safe_mean(pnls),
            "median_pnl_per_market": safe_median(pnls),
            "both_side_buy_markets": sum(1 for market in markets if market["both_side_buy_market"]),
            "both_side_trade_markets": sum(1 for market in markets if market["both_side_trade_market"]),
            "opposite_buy_event_count": int(sum(market["opposite_buy_event_count"] for market in markets)),
            "opposite_buy_usd": round(float(sum(market["opposite_buy_usd"] for market in markets)), 6),
            "opposite_buy_usd_share": safe_ratio(
                float(sum(market["opposite_buy_usd"] for market in markets)),
                buy_usd,
            ),
            "avg_hedge_side_buy_rows": safe_mean(hedge_side_buy_rows),
            "median_hedge_side_buy_rows": safe_median(hedge_side_buy_rows),
            "avg_hedge_side_buy_usd_share": safe_mean(hedge_side_buy_usd_shares),
            "median_hedge_side_buy_usd_share": safe_median(hedge_side_buy_usd_shares),
            "explicit_sell_row_share": safe_ratio(float(sell_rows), float(trade_rows)) if trade_rows > 0 else None,
            "explicit_sell_usd_share": safe_ratio(sell_usd, buy_usd + sell_usd) if (buy_usd + sell_usd) > 0 else None,
            "avg_hedge_coverage": safe_mean(hedge_coverages),
            "median_hedge_coverage": safe_median(hedge_coverages),
        }
    return bucket_stats


def build_crypto_stats(market_activity: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_crypto: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for market in market_activity.values():
        crypto_label = str(market.get("crypto_label") or "").strip() or "Unknown"
        by_crypto[crypto_label].append(market)

    stats_rows: List[Dict[str, Any]] = []
    for crypto_label, markets in by_crypto.items():
        trade_counts = [float(market["trade_rows"]) for market in markets]
        buy_counts = [float(market["buy_rows"]) for market in markets]
        hedge_coverages = [float(market["hedge_coverage"]) for market in markets]
        hedge_side_buy_rows = [float(market["hedge_side_buy_rows"]) for market in markets]
        hedge_side_buy_usd_shares = [float(market["hedge_side_buy_usd_share"]) for market in markets]
        pnls = [float(market["total_pnl"]) for market in markets]

        trade_rows = int(sum(market["trade_rows"] for market in markets))
        buy_rows = int(sum(market["buy_rows"] for market in markets))
        sell_rows = int(sum(market["sell_rows"] for market in markets))
        buy_usd = float(sum(market["buy_usd"] for market in markets))
        sell_usd = float(sum(market["sell_usd"] for market in markets))
        realized_pnl = float(sum(market["realized_pnl"] for market in markets))
        open_cash_pnl = float(sum(market["open_cash_pnl"] for market in markets))
        total_pnl = realized_pnl + open_cash_pnl
        unique_markets = len(markets)
        winning_markets = sum(1 for market in markets if market["total_pnl"] > 0)

        stats_rows.append(
            {
                "crypto": crypto_label,
                "unique_markets": unique_markets,
                "markets_by_bucket": {
                    bucket: sum(1 for market in markets if market.get("bucket") == bucket) for bucket in TARGET_BUCKETS
                },
                "buy_usd_by_bucket": {
                    bucket: round(
                        float(sum(market["buy_usd"] for market in markets if market.get("bucket") == bucket)),
                        6,
                    )
                    for bucket in TARGET_BUCKETS
                },
                "total_pnl_by_bucket": {
                    bucket: round(
                        float(sum(market["total_pnl"] for market in markets if market.get("bucket") == bucket)),
                        6,
                    )
                    for bucket in TARGET_BUCKETS
                },
                "trade_rows": trade_rows,
                "buy_rows": buy_rows,
                "sell_rows": sell_rows,
                "active_days": len({day for market in markets for day in market["activity_days"]}),
                "avg_trades_per_market": safe_mean(trade_counts),
                "median_trades_per_market": safe_median(trade_counts),
                "avg_buys_per_market": safe_mean(buy_counts),
                "median_buys_per_market": safe_median(buy_counts),
                "realized_pnl": round(realized_pnl, 6),
                "open_cash_pnl": round(open_cash_pnl, 6),
                "total_pnl": round(total_pnl, 6),
                "buy_usd": round(buy_usd, 6),
                "roi": safe_ratio(total_pnl, buy_usd),
                "market_win_rate": safe_ratio(float(winning_markets), float(unique_markets)) if unique_markets > 0 else None,
                "avg_pnl_per_market": safe_mean(pnls),
                "median_pnl_per_market": safe_median(pnls),
                "both_side_buy_markets": sum(1 for market in markets if market["both_side_buy_market"]),
                "both_side_trade_markets": sum(1 for market in markets if market["both_side_trade_market"]),
                "opposite_buy_event_count": int(sum(market["opposite_buy_event_count"] for market in markets)),
                "opposite_buy_usd": round(float(sum(market["opposite_buy_usd"] for market in markets)), 6),
                "opposite_buy_usd_share": safe_ratio(
                    float(sum(market["opposite_buy_usd"] for market in markets)),
                    buy_usd,
                ),
                "avg_hedge_side_buy_rows": safe_mean(hedge_side_buy_rows),
                "median_hedge_side_buy_rows": safe_median(hedge_side_buy_rows),
                "avg_hedge_side_buy_usd_share": safe_mean(hedge_side_buy_usd_shares),
                "median_hedge_side_buy_usd_share": safe_median(hedge_side_buy_usd_shares),
                "explicit_sell_row_share": safe_ratio(float(sell_rows), float(trade_rows)) if trade_rows > 0 else None,
                "explicit_sell_usd_share": safe_ratio(sell_usd, buy_usd + sell_usd) if (buy_usd + sell_usd) > 0 else None,
                "avg_hedge_coverage": safe_mean(hedge_coverages),
                "median_hedge_coverage": safe_median(hedge_coverages),
            }
        )

    stats_rows.sort(key=lambda row: (-float(row["buy_usd"]), -float(row["total_pnl"]), str(row["crypto"]).lower()))
    return stats_rows


def make_market_output_row(market: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "condition_id": market["condition_id"],
        "crypto": market.get("crypto_label") or "",
        "title": market["title"],
        "bucket": market["bucket"],
        "duration_minutes": market["duration_minutes"],
        "trade_rows": market["trade_rows"],
        "buy_rows": market["buy_rows"],
        "sell_rows": market["sell_rows"],
        "buy_usd": round(float(market["buy_usd"]), 6),
        "sell_usd": round(float(market["sell_usd"]), 6),
        "realized_pnl": round(float(market["realized_pnl"]), 6),
        "open_cash_pnl": round(float(market["open_cash_pnl"]), 6),
        "total_pnl": round(float(market["total_pnl"]), 6),
        "roi": safe_ratio(float(market["total_pnl"]), float(market["buy_usd"])),
        "both_side_buy_market": bool(market["both_side_buy_market"]),
        "both_side_trade_market": bool(market["both_side_trade_market"]),
        "hedge_coverage": round(float(market["hedge_coverage"]), 6),
        "opposite_buy_event_count": int(market["opposite_buy_event_count"]),
        "opposite_buy_usd": round(float(market["opposite_buy_usd"]), 6),
        "opposite_buy_usd_share": round(float(market["opposite_buy_usd_share"]), 6),
        "hedge_side_buy_rows": int(market["hedge_side_buy_rows"]),
        "hedge_side_buy_usd": round(float(market["hedge_side_buy_usd"]), 6),
        "hedge_side_buy_usd_share": round(float(market["hedge_side_buy_usd_share"]), 6),
        "hedge_side_outcome": market.get("hedge_side_outcome") or "",
        "explicit_sell_row_share": round(float(market["explicit_sell_row_share"]), 6),
        "explicit_sell_usd_share": round(float(market["explicit_sell_usd_share"]), 6),
        "dominant_outcome": market.get("dominant_outcome") or "",
        "activity_days": list(market["activity_days"]),
    }


def build_validation(
    market_activity: Dict[str, Dict[str, Any]],
    bucket_stats: Dict[str, Dict[str, Any]],
    crypto_stats: List[Dict[str, Any]],
    selected_rows: List[ActivityTradeRow],
) -> Dict[str, Any]:
    all_markets = list(market_activity.values())
    selected_total = {
        "trade_rows": int(sum(market["trade_rows"] for market in all_markets)),
        "buy_rows": int(sum(market["buy_rows"] for market in all_markets)),
        "sell_rows": int(sum(market["sell_rows"] for market in all_markets)),
        "buy_usd": round(float(sum(market["buy_usd"] for market in all_markets)), 6),
        "realized_pnl": round(float(sum(market["realized_pnl"] for market in all_markets)), 6),
        "open_cash_pnl": round(float(sum(market["open_cash_pnl"] for market in all_markets)), 6),
        "total_pnl": round(float(sum(market["total_pnl"] for market in all_markets)), 6),
        "unique_markets": len(all_markets),
    }
    bucket_total = {
        "trade_rows": int(sum(stats["trade_rows"] for stats in bucket_stats.values())),
        "buy_rows": int(sum(stats["buy_rows"] for stats in bucket_stats.values())),
        "sell_rows": int(sum(stats["sell_rows"] for stats in bucket_stats.values())),
        "buy_usd": round(float(sum(stats["buy_usd"] for stats in bucket_stats.values())), 6),
        "realized_pnl": round(float(sum(stats["realized_pnl"] for stats in bucket_stats.values())), 6),
        "open_cash_pnl": round(float(sum(stats["open_cash_pnl"] for stats in bucket_stats.values())), 6),
        "total_pnl": round(float(sum(stats["total_pnl"] for stats in bucket_stats.values())), 6),
        "unique_markets": int(sum(stats["unique_markets"] for stats in bucket_stats.values())),
    }
    crypto_total = {
        "trade_rows": int(sum(stats["trade_rows"] for stats in crypto_stats)),
        "buy_rows": int(sum(stats["buy_rows"] for stats in crypto_stats)),
        "sell_rows": int(sum(stats["sell_rows"] for stats in crypto_stats)),
        "buy_usd": round(float(sum(stats["buy_usd"] for stats in crypto_stats)), 6),
        "realized_pnl": round(float(sum(stats["realized_pnl"] for stats in crypto_stats)), 6),
        "open_cash_pnl": round(float(sum(stats["open_cash_pnl"] for stats in crypto_stats)), 6),
        "total_pnl": round(float(sum(stats["total_pnl"] for stats in crypto_stats)), 6),
        "unique_markets": int(sum(stats["unique_markets"] for stats in crypto_stats)),
    }
    row_direct = {
        "trade_rows": len(selected_rows),
        "buy_rows": sum(1 for row in selected_rows if row.side == "BUY"),
        "sell_rows": sum(1 for row in selected_rows if row.side == "SELL"),
        "buy_usd": round(float(sum(float(row.usd or 0.0) for row in selected_rows if row.side == "BUY")), 6),
    }
    passed = (
        selected_total["trade_rows"] == bucket_total["trade_rows"] == row_direct["trade_rows"]
        and selected_total["buy_rows"] == bucket_total["buy_rows"] == row_direct["buy_rows"]
        and selected_total["sell_rows"] == bucket_total["sell_rows"] == row_direct["sell_rows"]
        and abs(selected_total["buy_usd"] - bucket_total["buy_usd"]) <= 1e-6
        and abs(selected_total["buy_usd"] - row_direct["buy_usd"]) <= 1e-6
        and abs(selected_total["total_pnl"] - bucket_total["total_pnl"]) <= 1e-6
        and selected_total["trade_rows"] == crypto_total["trade_rows"]
        and selected_total["buy_rows"] == crypto_total["buy_rows"]
        and selected_total["sell_rows"] == crypto_total["sell_rows"]
        and abs(selected_total["buy_usd"] - crypto_total["buy_usd"]) <= 1e-6
        and abs(selected_total["total_pnl"] - crypto_total["total_pnl"]) <= 1e-6
    )
    return {
        "passed": passed,
        "selected_long_cycle_totals": selected_total,
        "bucket_sum_totals": bucket_total,
        "crypto_sum_totals": crypto_total,
        "row_direct_totals": row_direct,
        "no_sample_buckets": [bucket for bucket, stats in bucket_stats.items() if not stats["has_sample"]],
    }


def build_summary_conclusions(
    bucket_stats: Dict[str, Dict[str, Any]],
    market_activity: Dict[str, Dict[str, Any]],
    excluded_counts: Dict[str, Any],
    crypto_stats: List[Dict[str, Any]],
) -> List[str]:
    markets = list(market_activity.values())
    selected_markets = len(markets)
    selected_buy_usd = float(sum(market["buy_usd"] for market in markets))
    selected_sell_rows = int(sum(market["sell_rows"] for market in markets))
    both_side_markets = sum(1 for market in markets if market["both_side_buy_market"])
    opposite_buy_usd = float(sum(market["opposite_buy_usd"] for market in markets))
    overall_hedge_share = opposite_buy_usd / selected_buy_usd if selected_buy_usd > 0 else 0.0
    both_side_share = both_side_markets / selected_markets if selected_markets > 0 else 0.0

    conclusions: List[str] = []
    conclusions.append(
        f"Selected long-cycle sample covers {selected_markets} markets across "
        f"{', '.join(bucket for bucket in TARGET_BUCKETS if bucket_stats[bucket]['has_sample']) or 'no target buckets'}."
    )

    no_sample_buckets = [bucket for bucket in TARGET_BUCKETS if bucket_stats[bucket]["unique_markets"] == 0]
    if no_sample_buckets:
        conclusions.append(
            f"No sample was found for requested buckets: {', '.join(no_sample_buckets)}."
        )

    if selected_markets > 0 and both_side_share >= 0.5 and selected_sell_rows == 0 and overall_hedge_share >= 0.15:
        conclusions.append(
            "Strong hedge signal: the address rarely uses explicit SELL in selected long-cycle markets and instead frequently buys the opposite side while prior exposure is still open."
        )
    elif selected_markets > 0 and both_side_share >= 0.25 and overall_hedge_share >= 0.08:
        conclusions.append(
            "Moderate hedge signal: opposite-side buying is common enough to act as a meaningful partial hedge or synthetic reduction mechanism."
        )
    else:
        conclusions.append(
            "Weak hedge signal: opposite-side buying exists but does not dominate the long-cycle sample."
        )

    if crypto_stats:
        largest_crypto = max(
            crypto_stats,
            key=lambda row: (
                float(row.get("buy_usd") or 0.0),
                int(row.get("unique_markets") or 0),
            ),
        )
        conclusions.append(
            f"Largest crypto exposure is {largest_crypto['crypto']} with ${largest_crypto['buy_usd']:,.2f} buy flow across {largest_crypto['unique_markets']} markets."
        )

        hedge_heavy_crypto = max(
            crypto_stats,
            key=lambda row: (
                float(row.get("avg_hedge_side_buy_usd_share") or 0.0),
                float(row.get("avg_hedge_side_buy_rows") or 0.0),
                float(row.get("buy_usd") or 0.0),
            ),
        )
        hedge_rows = hedge_heavy_crypto.get("avg_hedge_side_buy_rows")
        hedge_share = hedge_heavy_crypto.get("avg_hedge_side_buy_usd_share")
        if hedge_rows is not None and hedge_share is not None:
            conclusions.append(
                f"{hedge_heavy_crypto['crypto']} shows the heaviest hedge-side pattern: the smaller side averages {hedge_rows:.2f} buys and {hedge_share * 100:.2f}% of a market's total buy USD."
            )

    best_bucket = max(
        bucket_stats.values(),
        key=lambda stats: (
            stats["roi"] if stats["roi"] is not None else float("-inf"),
            stats["total_pnl"],
            stats["buy_usd"],
        ),
    )
    if best_bucket["has_sample"] and best_bucket["roi"] is not None:
        conclusions.append(
            f"Best observed target bucket by ROI is {best_bucket['bucket']} with ROI {best_bucket['roi'] * 100:.2f}% "
            f"on ${best_bucket['buy_usd']:,.2f} buy flow."
        )
        if best_bucket.get("avg_hedge_side_buy_rows") is not None and best_bucket.get("avg_hedge_side_buy_usd_share") is not None:
            conclusions.append(
                f"In {best_bucket['bucket']}, the hedge-side leg averages {best_bucket['avg_hedge_side_buy_rows']:.2f} buys "
                f"and {best_bucket['avg_hedge_side_buy_usd_share'] * 100:.2f}% of a market's total buy USD."
            )

    excluded_rows = excluded_counts.get("event_rows", {})
    unsupported_rows = int(
        sum(
            count
            for reason, count in excluded_rows.items()
            if str(reason).startswith("unsupported_")
        )
    )
    if unsupported_rows > 0:
        conclusions.append(
            f"Unsupported duration buckets outside 5m/15m/1h/4h/1d/1w were excluded from the main analysis: {unsupported_rows:,} trade rows."
        )
    return conclusions


def render_bucket_table(bucket_stats: Dict[str, Dict[str, Any]]) -> str:
    lines = [
        "| Bucket | Markets | TradeRows | BuyRows | SellRows | ActiveDays | BuyUSD | RealizedPnL | OpenCashPnL | TotalPnL | ROI | BothSideBuy | HedgeSideBuys(avg) | HedgeSideUSD(avg) | OppBuyUSDShare | SellUSDShare | AvgHedge |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for bucket in TARGET_BUCKETS:
        stats = bucket_stats[bucket]
        if not stats["has_sample"]:
            lines.append(
                f"| {bucket} | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | no sample | 0 | no sample | no sample | no sample | no sample | no sample |"
            )
            continue
        lines.append(
            "| {bucket} | {markets} | {trade_rows} | {buy_rows} | {sell_rows} | {active_days} | "
            "{buy_usd} | {realized_pnl} | {open_cash_pnl} | {total_pnl} | {roi} | {both_side_buy_markets} | "
            "{avg_hedge_side_buy_rows} | {avg_hedge_side_buy_usd_share} | {opposite_buy_usd_share} | {explicit_sell_usd_share} | {avg_hedge_coverage} |".format(
                bucket=bucket,
                markets=stats["unique_markets"],
                trade_rows=stats["trade_rows"],
                buy_rows=stats["buy_rows"],
                sell_rows=stats["sell_rows"],
                active_days=stats["active_days"],
                buy_usd=f"${stats['buy_usd']:,.2f}",
                realized_pnl=f"${stats['realized_pnl']:+,.2f}",
                open_cash_pnl=f"${stats['open_cash_pnl']:+,.2f}",
                total_pnl=f"${stats['total_pnl']:+,.2f}",
                roi=f"{stats['roi'] * 100:.2f}%" if stats["roi"] is not None else "N/A",
                both_side_buy_markets=stats["both_side_buy_markets"],
                avg_hedge_side_buy_rows=f"{stats['avg_hedge_side_buy_rows']:.2f}"
                if stats["avg_hedge_side_buy_rows"] is not None
                else "N/A",
                avg_hedge_side_buy_usd_share=f"{stats['avg_hedge_side_buy_usd_share'] * 100:.2f}%"
                if stats["avg_hedge_side_buy_usd_share"] is not None
                else "N/A",
                opposite_buy_usd_share=f"{stats['opposite_buy_usd_share'] * 100:.2f}%"
                if stats["opposite_buy_usd_share"] is not None
                else "N/A",
                explicit_sell_usd_share=f"{stats['explicit_sell_usd_share'] * 100:.2f}%"
                if stats["explicit_sell_usd_share"] is not None
                else "N/A",
                avg_hedge_coverage=f"{stats['avg_hedge_coverage'] * 100:.2f}%"
                if stats["avg_hedge_coverage"] is not None
                else "N/A",
            )
        )
    return "\n".join(lines)


def render_crypto_table(crypto_stats: List[Dict[str, Any]], *, limit: int = 15, title: str = "By Crypto") -> str:
    lines = [f"## {title}", ""]
    if not crypto_stats:
        lines.append("No rows.")
        return "\n".join(lines)

    bucket_headers = " | ".join(TARGET_BUCKETS)
    bucket_separators = " | ".join("---:" for _ in TARGET_BUCKETS)
    lines.extend(
        [
            f"| Crypto | Markets | {bucket_headers} | BuyUSD | TotalPnL | ROI | WinRate | BothSideBuy | OppBuyUSDShare | HedgeSideBuys(avg/med) | HedgeSideUSD(avg/med) |",
            f"| --- | ---: | {bucket_separators} | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in crypto_stats[:limit]:
        markets_by_bucket = row.get("markets_by_bucket") or {}
        bucket_cells = " | ".join(str(int(markets_by_bucket.get(bucket) or 0)) for bucket in TARGET_BUCKETS)
        lines.append(
            "| {crypto} | {markets} | {bucket_cells} | {buy_usd} | {total_pnl} | {roi} | {win_rate} | {both_side_buy} | {opp_buy_share} | {hedge_buys} | {hedge_share} |".format(
                crypto=str(row.get("crypto") or "").replace("|", "/"),
                markets=int(row.get("unique_markets") or 0),
                bucket_cells=bucket_cells,
                buy_usd=f"${float(row.get('buy_usd') or 0.0):,.2f}",
                total_pnl=f"${float(row.get('total_pnl') or 0.0):+,.2f}",
                roi=f"{float(row.get('roi') or 0.0) * 100:.2f}%" if row.get("roi") is not None else "N/A",
                win_rate=f"{float(row.get('market_win_rate') or 0.0) * 100:.2f}%"
                if row.get("market_win_rate") is not None
                else "N/A",
                both_side_buy=int(row.get("both_side_buy_markets") or 0),
                opp_buy_share=f"{float(row.get('opposite_buy_usd_share') or 0.0) * 100:.2f}%"
                if row.get("opposite_buy_usd_share") is not None
                else "N/A",
                hedge_buys="{avg} / {med}".format(
                    avg=f"{float(row.get('avg_hedge_side_buy_rows') or 0.0):.2f}"
                    if row.get("avg_hedge_side_buy_rows") is not None
                    else "N/A",
                    med=f"{float(row.get('median_hedge_side_buy_rows') or 0.0):.2f}"
                    if row.get("median_hedge_side_buy_rows") is not None
                    else "N/A",
                ),
                hedge_share="{avg} / {med}".format(
                    avg=f"{float(row.get('avg_hedge_side_buy_usd_share') or 0.0) * 100:.2f}%"
                    if row.get("avg_hedge_side_buy_usd_share") is not None
                    else "N/A",
                    med=f"{float(row.get('median_hedge_side_buy_usd_share') or 0.0) * 100:.2f}%"
                    if row.get("median_hedge_side_buy_usd_share") is not None
                    else "N/A",
                ),
            )
        )
    return "\n".join(lines)


def render_market_table(rows: List[Dict[str, Any]], *, limit: int, title: str) -> str:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("No rows.")
        return "\n".join(lines)

    lines.extend(
        [
            "| Bucket | Market | BuyUSD | RealizedPnL | OpenCashPnL | TotalPnL | HedgeCoverage | HedgeSideBuys | HedgeSideUSDShare | OppBuyUSDShare | SellUSDShare |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows[:limit]:
        lines.append(
            "| {bucket} | {title} | {buy_usd} | {realized_pnl} | {open_cash_pnl} | {total_pnl} | {hedge_coverage} | {hedge_buys} | {hedge_side_share} | {opp_share} | {sell_share} |".format(
                bucket=row.get("bucket") or "N/A",
                title=str(row.get("title") or "").replace("|", "/"),
                buy_usd=f"${float(row.get('buy_usd') or 0.0):,.2f}",
                realized_pnl=f"${float(row.get('realized_pnl') or 0.0):+,.2f}",
                open_cash_pnl=f"${float(row.get('open_cash_pnl') or 0.0):+,.2f}",
                total_pnl=f"${float(row.get('total_pnl') or 0.0):+,.2f}",
                hedge_coverage=f"{float(row.get('hedge_coverage') or 0.0) * 100:.2f}%",
                hedge_buys=int(row.get("hedge_side_buy_rows") or 0),
                hedge_side_share=f"{float(row.get('hedge_side_buy_usd_share') or 0.0) * 100:.2f}%",
                opp_share=f"{float(row.get('opposite_buy_usd_share') or 0.0) * 100:.2f}%",
                sell_share=f"{float(row.get('explicit_sell_usd_share') or 0.0) * 100:.2f}%",
            )
        )
    return "\n".join(lines)


def build_markdown_report(
    *,
    address: str,
    sample_window: Dict[str, Any],
    excluded_counts: Dict[str, Any],
    bucket_stats: Dict[str, Dict[str, Any]],
    crypto_stats: List[Dict[str, Any]],
    top_hedged_markets: List[Dict[str, Any]],
    top_profitable_markets: List[Dict[str, Any]],
    top_losing_markets: List[Dict[str, Any]],
    validation: Dict[str, Any],
    summary_conclusions: List[str],
) -> str:
    lines = [
        f"# {address} Crypto Timed-Market Study",
        "",
        f"- Generated at (UTC): {now_utc_iso()}",
        f"- Activity sample window: {sample_window.get('first_utc') or 'N/A'} -> {sample_window.get('last_utc') or 'N/A'}",
        f"- Requested lookback trades: {sample_window.get('requested_lookback_trades', 0):,}",
        f"- Raw activity rows fetched: {sample_window.get('raw_rows', 0):,}",
        f"- Deduped activity rows: {sample_window.get('deduped_rows', 0):,}",
        f"- Selected long-cycle trade rows: {sample_window.get('selected_trade_rows', 0):,}",
        "",
        "## Overview",
        "",
        render_bucket_table(bucket_stats),
        "",
        render_crypto_table(crypto_stats),
        "",
        "## Excluded Counts",
        "",
        f"- Event rows: `{json.dumps(excluded_counts.get('event_rows', {}), ensure_ascii=False)}`",
        f"- Unique markets: `{json.dumps(excluded_counts.get('unique_markets', {}), ensure_ascii=False)}`",
        "",
        "## Conclusions",
        "",
    ]
    lines.extend(f"- {item}" for item in summary_conclusions)
    lines.extend(
        [
            "",
            render_market_table(top_hedged_markets, limit=10, title="Top Hedged Markets"),
            "",
            render_market_table(top_profitable_markets, limit=10, title="Top Profitable Markets"),
            "",
            render_market_table(top_losing_markets, limit=10, title="Top Losing Markets"),
            "",
            "## Validation",
            "",
            f"- Passed: `{validation.get('passed')}`",
            f"- No-sample buckets: `{validation.get('no_sample_buckets')}`",
            f"- Selected totals: `{json.dumps(validation.get('selected_long_cycle_totals', {}), ensure_ascii=False)}`",
            f"- Bucket-sum totals: `{json.dumps(validation.get('bucket_sum_totals', {}), ensure_ascii=False)}`",
            f"- Crypto-sum totals: `{json.dumps(validation.get('crypto_sum_totals', {}), ensure_ascii=False)}`",
            f"- Row-direct totals: `{json.dumps(validation.get('row_direct_totals', {}), ensure_ascii=False)}`",
            "",
        ]
    )
    return "\n".join(lines)


def build_payload(
    *,
    address: str,
    requested_lookback_trades: int,
    raw_rows: int,
    activity_rows: List[ActivityTradeRow],
    selected_rows: List[ActivityTradeRow],
    excluded_counts: Dict[str, Any],
    bucket_stats: Dict[str, Dict[str, Any]],
    crypto_stats: List[Dict[str, Any]],
    market_activity: Dict[str, Dict[str, Any]],
    validation: Dict[str, Any],
    summary_conclusions: List[str],
) -> Dict[str, Any]:
    top_hedged_markets = sorted(
        (make_market_output_row(market) for market in market_activity.values()),
        key=lambda row: (
            float(row.get("hedge_coverage") or 0.0),
            float(row.get("opposite_buy_usd_share") or 0.0),
            float(row.get("buy_usd") or 0.0),
        ),
        reverse=True,
    )[:20]
    top_profitable_markets = sorted(
        (make_market_output_row(market) for market in market_activity.values()),
        key=lambda row: (
            float(row.get("total_pnl") or 0.0),
            float(row.get("buy_usd") or 0.0),
        ),
        reverse=True,
    )[:20]
    top_losing_markets = sorted(
        (make_market_output_row(market) for market in market_activity.values()),
        key=lambda row: (
            float(row.get("total_pnl") or 0.0),
            -float(row.get("buy_usd") or 0.0),
        ),
    )[:20]

    first_ts = activity_rows[0].ts if activity_rows else None
    last_ts = activity_rows[-1].ts if activity_rows else None
    sample_window = {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "first_utc": format_utc_from_epoch(first_ts),
        "last_utc": format_utc_from_epoch(last_ts),
        "requested_lookback_trades": int(requested_lookback_trades),
        "raw_rows": raw_rows,
        "deduped_rows": len(activity_rows),
        "selected_trade_rows": len(selected_rows),
        "selected_buy_rows": sum(1 for row in selected_rows if row.side == "BUY"),
        "selected_sell_rows": sum(1 for row in selected_rows if row.side == "SELL"),
    }

    top_hedged_cryptos = sorted(
        crypto_stats,
        key=lambda row: (
            float(row.get("avg_hedge_side_buy_usd_share") or 0.0),
            float(row.get("avg_hedge_side_buy_rows") or 0.0),
            float(row.get("buy_usd") or 0.0),
        ),
        reverse=True,
    )[:10]
    top_crypto_by_pnl = sorted(
        crypto_stats,
        key=lambda row: (
            float(row.get("total_pnl") or 0.0),
            float(row.get("buy_usd") or 0.0),
        ),
        reverse=True,
    )[:10]
    top_crypto_by_roi = sorted(
        (row for row in crypto_stats if row.get("roi") is not None),
        key=lambda row: (
            float(row.get("roi") or 0.0),
            float(row.get("buy_usd") or 0.0),
            float(row.get("total_pnl") or 0.0),
        ),
        reverse=True,
    )[:10]

    return {
        "address": address,
        "generated_at": now_utc_iso(),
        "sample_window": sample_window,
        "excluded_counts": excluded_counts,
        "bucket_stats": {bucket: bucket_stats[bucket] for bucket in TARGET_BUCKETS},
        "crypto_stats": crypto_stats,
        "top_hedged_cryptos": top_hedged_cryptos,
        "top_crypto_by_pnl": top_crypto_by_pnl,
        "top_crypto_by_roi": top_crypto_by_roi,
        "top_hedged_markets": top_hedged_markets,
        "top_profitable_markets": top_profitable_markets,
        "top_losing_markets": top_losing_markets,
        "validation": validation,
        "summary_conclusions": summary_conclusions,
    }


def run_self_checks() -> None:
    ts_utc = parse_epoch("2026-04-11T00:00:00Z")
    ts_naive = parse_epoch("2026-04-11T00:00:00")
    assert ts_utc is not None
    assert ts_naive == ts_utc
    assert format_utc_from_epoch(ts_naive) == "2026-04-11 00:00:00 UTC"

    assert classify_market_title("Bitcoin Up or Down - April 6, 4:00AM-8:00AM ET")["bucket"] == "4h"
    assert classify_market_title("Bitcoin Up or Down - April 6, 5:00AM-6:00AM ET")["bucket"] == "1h"
    assert classify_market_title("Bitcoin Up or Down - April 6, 9:30PM-9:45PM ET")["bucket"] == "15m"
    assert classify_market_title("Bitcoin Up or Down - April 6, 9:40PM-9:45PM ET")["bucket"] == "5m"
    assert classify_market_title("Bitcoin Up or Down - April 6, 8:00AM-8:00AM ET")["bucket"] == "1d"
    assert classify_market_title("Solana Up or Down - April 4, 11PM ET")["bucket"] == "1h"
    assert classify_market_title("Bitcoin Up or Down on April 11?")["bucket"] == "1d"
    assert classify_market_title("What price will Bitcoin hit on April 11?")["bucket"] == "1d"
    assert classify_market_title("Ethereum Up or Down on April 11?")["bucket"] == "1d"
    assert classify_market_title("Bitcoin above 90,000 on April 11?")["bucket"] == "1w"
    assert classify_market_title("Bitcoin above 72,600 on April 10, 10PM ET?")["bucket"] == "1w"
    assert classify_market_title("What price will Bitcoin hit April 6-12?")["bucket"] == "1w"
    assert classify_market_title("What price will Bitcoin hit on April 6-12?")["bucket"] == "1w"
    assert classify_market_title("Ethereum above 2,000 on April 11?")["bucket"] == "1w"
    assert classify_market_title("", "solana-up-or-down-april-4-2026-11pm-et")["bucket"] == "1h"
    assert classify_market_title("", "sol-updown-15m-1775871000")["bucket"] == "15m"
    assert classify_market_title("", "bitcoin-updown-1w-1775871000")["bucket"] == "1w"
    unsupported = classify_market_title("Bitcoin Up or Down - April 6, 9:30PM-10:00PM ET")
    assert unsupported["bucket"] is None
    assert unsupported["reason"] == "unsupported_subhour_duration"
    assert classify_market_title("Bitcoin Up or Down - April 6, 11:00PM-1:00AM ET")["duration_minutes"] == 120
    assert extract_crypto_label("BTC Up or Down - April 6, 5:00AM-6:00AM ET") == "Bitcoin"
    assert extract_crypto_label("What price will Bitcoin hit on April 11?") == "Bitcoin"
    assert extract_crypto_label("Ethereum above 2,000 on April 11?") == "Ethereum"
    assert extract_crypto_label("", "solana-up-or-down-april-4-2026-11pm-et") == "Solana"

    meta_slug_only: Dict[str, Dict[str, Any]] = {}
    update_market_meta(
        meta_slug_only,
        condition_id="0xslug",
        title="",
        market_slug="sol-updown-15m-1775871000",
        asset_id="yes",
        outcome="Up",
    )
    assert meta_slug_only["0xslug"]["bucket"] == "15m"
    assert meta_slug_only["0xslug"]["duration_minutes"] == 15

    meta = {
        "0xabc": {
            "condition_id": "0xabc",
            "title": "Bitcoin Up or Down - April 6, 4:00AM-8:00AM ET",
            "crypto_label": "Bitcoin",
            "bucket": "4h",
            "duration_minutes": 240,
            "is_crypto_timed_market": True,
            "classify_reason": None,
            "outcome_by_asset": {"yes": "Up", "no": "Down"},
        }
    }
    rows = [
        ActivityTradeRow("tx1", 1, "BUY", "0xabc", "yes", "Up", meta["0xabc"]["title"], "", 0.60, 10.0, 6.0),
        ActivityTradeRow("tx2", 2, "BUY", "0xabc", "no", "Down", meta["0xabc"]["title"], "", 0.40, 5.0, 2.0),
        ActivityTradeRow("tx3", 3, "SELL", "0xabc", "yes", "Up", meta["0xabc"]["title"], "", 0.70, 4.0, 2.8),
    ]
    selected, excluded, selected_rows = build_selected_market_activity(rows, meta)
    assert excluded["event_rows"] == {}
    assert len(selected_rows) == 3
    assert selected["0xabc"]["opposite_buy_event_count"] == 1
    assert selected["0xabc"]["both_side_buy_market"] is True
    assert selected["0xabc"]["sell_rows"] == 1
    assert selected["0xabc"]["hedge_side_buy_rows"] == 1
    assert abs(selected["0xabc"]["hedge_side_buy_usd_share"] - 0.25) <= 1e-9

    pnl_map = {"0xabc": {"open_cash_pnl": 3.0, "realized_pnl": 1.5, "open_rows": 1, "closed_rows": 1}}
    enrich_markets_with_pnl(selected, pnl_map)
    assert abs(selected["0xabc"]["total_pnl"] - 4.5) <= 1e-9

    stats = build_bucket_stats(selected)
    crypto_stats = build_crypto_stats(selected)
    assert stats["4h"]["unique_markets"] == 1
    assert stats["1h"]["unique_markets"] == 0
    assert abs((stats["4h"]["avg_hedge_side_buy_rows"] or 0.0) - 1.0) <= 1e-9
    assert len(crypto_stats) == 1
    assert crypto_stats[0]["crypto"] == "Bitcoin"
    assert abs((crypto_stats[0]["avg_hedge_side_buy_usd_share"] or 0.0) - 0.25) <= 1e-9


def main() -> int:
    args = parse_args()
    address = str(args.address or "").strip().lower()
    if not address:
        raise SystemExit("address is required")

    run_self_checks()

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else SIM_ROOT / "output" / "crypto_timeframe_study"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    t0 = time.time()
    print(f"=== Crypto Market Study: {address} ===")

    activity_rows, raw_rows = fetch_activity_trade_rows(
        session,
        address,
        max_activities=max(1, int(args.lookback_trades)),
        page_limit=max(1, int(args.page_limit)),
    )
    print(f"[activity] raw_rows={raw_rows:,} deduped_rows={len(activity_rows):,}")

    print("[positions] fetching open positions...")
    open_positions = fetch_positions(session, address)
    print(f"[positions] open_rows={len(open_positions):,}")

    print("[positions] fetching closed positions...")
    closed_positions = fetch_closed_positions(session, address)
    print(f"[positions] closed_rows={len(closed_positions):,}")

    meta_by_condition = build_market_meta(session, activity_rows, open_positions, closed_positions)
    pnl_by_condition = build_market_pnl_maps(open_positions, closed_positions)
    market_activity, excluded_counts, selected_rows = build_selected_market_activity(activity_rows, meta_by_condition)
    enrich_markets_with_pnl(market_activity, pnl_by_condition)
    bucket_stats = build_bucket_stats(market_activity)
    crypto_stats = build_crypto_stats(market_activity)
    validation = build_validation(market_activity, bucket_stats, crypto_stats, selected_rows)
    summary_conclusions = build_summary_conclusions(bucket_stats, market_activity, excluded_counts, crypto_stats)

    payload = build_payload(
        address=address,
        requested_lookback_trades=max(1, int(args.lookback_trades)),
        raw_rows=raw_rows,
        activity_rows=activity_rows,
        selected_rows=selected_rows,
        excluded_counts=excluded_counts,
        bucket_stats=bucket_stats,
        crypto_stats=crypto_stats,
        market_activity=market_activity,
        validation=validation,
        summary_conclusions=summary_conclusions,
    )
    markdown = build_markdown_report(
        address=address,
        sample_window=payload["sample_window"],
        excluded_counts=payload["excluded_counts"],
        bucket_stats=payload["bucket_stats"],
        crypto_stats=payload["crypto_stats"],
        top_hedged_markets=payload["top_hedged_markets"],
        top_profitable_markets=payload["top_profitable_markets"],
        top_losing_markets=payload["top_losing_markets"],
        validation=payload["validation"],
        summary_conclusions=payload["summary_conclusions"],
    )

    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = short_address(address)
    json_path = out_dir / f"address_crypto_study_{short}_{ts_str}.json"
    md_path = out_dir / f"address_crypto_study_{short}_{ts_str}.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")

    print(f"[output] {json_path}")
    print(f"[output] {md_path}")
    print(f"[done] elapsed={time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
