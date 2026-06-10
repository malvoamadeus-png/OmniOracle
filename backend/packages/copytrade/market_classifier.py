"""Shared market classification helpers for copytrade filtering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

from copytrade.polymarket_public_api import GAMMA_API, http_get_json

from copytrade.db import CopyTradeDB


CRYPTO_UPDOWN_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-updown-(?P<tf>5m|15m|1h|4h)-\d+$"
)
CRYPTO_HOURLY_EVENT_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-up-or-down-(?!on-)(?:[a-z0-9]+-)*\d{1,2}(?:am|pm)-et$"
)
CRYPTO_DAILY_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-up-or-down-on-[a-z0-9-]+$"
)
CRYPTO_WEEKLY_EVENT_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-above-on-(?P<date>[a-z]+-\d{1,2})$"
)
CRYPTO_WEEKLY_MARKET_SLUG_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-above-[a-z0-9-]+-on-(?P<date>[a-z]+-\d{1,2})$"
)
CRYPTO_UPDOWN_SERIES_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-(?:updown|up-or-down)-(?P<tf>5m|15m|1h|4h|hourly)$"
)
CRYPTO_UPDOWN_BASE_SERIES_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-(?:updown|up-or-down)$"
)
CRYPTO_DAILY_SERIES_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-up-or-down-daily$"
)
CRYPTO_WEEKLY_SERIES_RE = re.compile(
    r"^(?P<asset>[a-z0-9]+)-multi-strikes-weekly$"
)
CRYPTO_INTRADAY_TIMEFRAME_SET = {"5m", "15m", "1h", "4h"}
CRYPTO_INTRADAY_TIMEFRAME_ALIASES = {
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "hourly": "1h",
}
CRYPTO_ASSET_ALIASES = {
    "btc": "btc",
    "bitcoin": "btc",
    "eth": "eth",
    "ethereum": "eth",
    "sol": "sol",
    "solana": "sol",
    "xrp": "xrp",
}


@dataclass(frozen=True)
class CryptoMarketClassification:
    is_crypto_updown: bool
    timeframe: Optional[str]
    asset: Optional[str]
    slug: Optional[str]
    source: str
    kind: str
    series_slug: Optional[str] = None
    recurrence: Optional[str] = None


class CryptoMarketClassifier:
    def __init__(self, session: requests.Session, db: CopyTradeDB):
        self.session = session
        self.db = db
        self._slug_classification_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        self._token_meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._slug_meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._event_meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._condition_meta_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        self._local_slug_by_condition_cache: Dict[str, Optional[str]] = {}
        self._local_slug_by_token_cache: Dict[str, Optional[str]] = {}

    def classify_leader_trade(self, leader_trade: Any) -> CryptoMarketClassification:
        market_slug = self._clean_text(getattr(leader_trade, "market_slug", None))
        condition_id = self._clean_text(getattr(leader_trade, "condition_id", None))
        token_id = self._clean_text(getattr(leader_trade, "token_id", None))

        best_non_crypto: Optional[CryptoMarketClassification] = None

        if market_slug:
            direct = self._classify_slug(market_slug, source="trade.market_slug")
            if direct.is_crypto_updown:
                return direct
            if direct.kind == "not_crypto":
                best_non_crypto = direct

        if not market_slug and condition_id:
            local_slug = self._get_local_slug_by_condition(condition_id)
            if local_slug:
                local = self._classify_slug(local_slug, source="db.condition_id")
                if local.is_crypto_updown:
                    return local
                if local.kind == "not_crypto":
                    best_non_crypto = local

        if not market_slug and token_id:
            local_slug = self._get_local_slug_by_token(token_id)
            if local_slug:
                local = self._classify_slug(local_slug, source="db.token_id")
                if local.is_crypto_updown:
                    return local
                if local.kind == "not_crypto":
                    best_non_crypto = local

        if token_id:
            by_token = self._classify_from_meta(
                self._fetch_market_by_token_id(token_id),
                source="gamma.token_id",
            )
            if by_token is not None:
                if by_token.is_crypto_updown:
                    return by_token
                if by_token.kind == "not_crypto":
                    best_non_crypto = by_token

        if market_slug:
            by_slug = self._classify_from_meta(
                self._fetch_market_by_slug(market_slug),
                source="gamma.market_slug",
            )
            if by_slug is not None:
                if by_slug.is_crypto_updown:
                    return by_slug
                if by_slug.kind == "not_crypto":
                    best_non_crypto = by_slug

        if market_slug:
            by_event = self._classify_from_meta(
                self._fetch_event_by_slug(market_slug),
                source="gamma.event_slug",
            )
            if by_event is not None:
                if by_event.is_crypto_updown:
                    return by_event
                if by_event.kind == "not_crypto":
                    best_non_crypto = by_event

        if condition_id:
            by_condition = self._classify_from_meta(
                self._fetch_market_by_condition_id(condition_id),
                source="gamma.condition_id",
            )
            if by_condition is not None:
                if by_condition.is_crypto_updown:
                    return by_condition
                if by_condition.kind == "not_crypto":
                    best_non_crypto = by_condition

        if best_non_crypto is not None:
            return best_non_crypto

        return CryptoMarketClassification(
            is_crypto_updown=False,
            timeframe=None,
            asset=None,
            slug=market_slug,
            source="unclassified",
            kind="unclassified",
        )

    def _classify_from_meta(
        self,
        meta: Optional[Dict[str, Any]],
        *,
        source: str,
    ) -> Optional[CryptoMarketClassification]:
        if not isinstance(meta, dict):
            return None

        slug = self._clean_text(meta.get("slug"))
        series_slug = self._clean_text(meta.get("seriesSlug"))
        recurrence = self._clean_text(meta.get("recurrence"))

        first_event = self._first_event(meta)
        if series_slug is None and isinstance(first_event, dict):
            series_slug = self._clean_text(first_event.get("seriesSlug"))
        if recurrence is None and isinstance(first_event, dict):
            recurrence = self._clean_text(first_event.get("recurrence"))
            series = first_event.get("series")
            if recurrence is None and isinstance(series, list) and series and isinstance(series[0], dict):
                recurrence = self._clean_text(series[0].get("recurrence"))
            if series_slug is None and isinstance(series, list) and series and isinstance(series[0], dict):
                series_slug = self._clean_text(series[0].get("slug"))

        if slug or series_slug:
            return self._classify_slug(
                slug or series_slug,
                source=source,
                series_slug=series_slug,
                recurrence=recurrence,
            )

        return CryptoMarketClassification(
            is_crypto_updown=False,
            timeframe=None,
            asset=None,
            slug=None,
            source=source,
            kind="unclassified",
            series_slug=series_slug,
            recurrence=recurrence,
        )

    def _classify_slug(
        self,
        slug: str,
        *,
        source: str,
        series_slug: Optional[str] = None,
        recurrence: Optional[str] = None,
    ) -> CryptoMarketClassification:
        normalized_slug = self._clean_text(slug)
        normalized_series_slug = self._clean_text(series_slug)
        normalized_recurrence = self._clean_text(recurrence)
        if not normalized_slug:
            return CryptoMarketClassification(
                is_crypto_updown=False,
                timeframe=None,
                asset=None,
                slug=None,
                source=source,
                kind="unclassified",
                series_slug=series_slug,
                recurrence=recurrence,
            )

        cache_key = (
            normalized_slug,
            normalized_series_slug or "",
            normalized_recurrence or "",
        )
        cached = self._slug_classification_cache.get(cache_key)
        if cached is None:
            cached = self._classify_slug_with_meta(
                normalized_slug,
                series_slug=normalized_series_slug,
                recurrence=normalized_recurrence,
            )
            if cached is None:
                cached = {
                    "is_crypto_updown": False,
                    "timeframe": None,
                    "asset": None,
                    "slug": normalized_slug,
                    "kind": "not_crypto",
                }
            self._slug_classification_cache[cache_key] = cached

        return CryptoMarketClassification(
            is_crypto_updown=bool(cached["is_crypto_updown"]),
            timeframe=cached["timeframe"],
            asset=cached["asset"],
            slug=cached["slug"],
            source=source,
            kind=str(cached["kind"]),
            series_slug=series_slug,
            recurrence=recurrence,
        )

    def _get_local_slug_by_condition(self, condition_id: str) -> Optional[str]:
        cid = self._clean_text(condition_id)
        if not cid:
            return None
        if cid not in self._local_slug_by_condition_cache:
            self._local_slug_by_condition_cache[cid] = self.db.find_latest_market_slug(
                condition_id=cid
            )
        return self._local_slug_by_condition_cache[cid]

    def _get_local_slug_by_token(self, token_id: str) -> Optional[str]:
        tid = self._clean_text(token_id)
        if not tid:
            return None
        if tid not in self._local_slug_by_token_cache:
            self._local_slug_by_token_cache[tid] = self.db.find_latest_market_slug(
                token_id=tid
            )
        return self._local_slug_by_token_cache[tid]

    def _fetch_market_by_token_id(self, token_id: str) -> Optional[Dict[str, Any]]:
        tid = self._clean_text(token_id)
        if not tid:
            return None
        if tid in self._token_meta_cache:
            return self._clone_meta(self._token_meta_cache[tid])

        meta = self._fetch_market_meta(
            params={"clob_token_ids": tid, "limit": 1},
            expected_condition_id=None,
        )
        self._token_meta_cache[tid] = self._clone_meta(meta)
        return self._clone_meta(meta)

    def _fetch_market_by_slug(self, market_slug: str) -> Optional[Dict[str, Any]]:
        slug = self._clean_text(market_slug)
        if not slug:
            return None
        if slug in self._slug_meta_cache:
            return self._clone_meta(self._slug_meta_cache[slug])

        meta = self._fetch_market_meta(
            params={"slug": slug, "limit": 1},
            expected_condition_id=None,
        )
        self._slug_meta_cache[slug] = self._clone_meta(meta)
        return self._clone_meta(meta)

    def _fetch_event_by_slug(self, market_slug: str) -> Optional[Dict[str, Any]]:
        slug = self._clean_text(market_slug)
        if not slug:
            return None
        if slug in self._event_meta_cache:
            return self._clone_meta(self._event_meta_cache[slug])

        meta = self._fetch_event_meta(slug)
        self._event_meta_cache[slug] = self._clone_meta(meta)
        return self._clone_meta(meta)

    def _fetch_market_by_condition_id(self, condition_id: str) -> Optional[Dict[str, Any]]:
        cid = self._clean_text(condition_id)
        if not cid:
            return None
        if cid in self._condition_meta_cache:
            return self._clone_meta(self._condition_meta_cache[cid])

        meta = self._fetch_market_meta(
            params={"conditionId": cid, "limit": 1},
            expected_condition_id=cid,
        )
        self._condition_meta_cache[cid] = self._clone_meta(meta)
        return self._clone_meta(meta)

    def _fetch_market_meta(
        self,
        *,
        params: Dict[str, Any],
        expected_condition_id: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        try:
            data = http_get_json(
                self.session,
                f"{GAMMA_API}/markets",
                params=params,
                timeout_s=15.0,
                max_retries=2,
            )
        except Exception:
            return None

        row = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else data
            if isinstance(data, dict)
            else None
        )
        if not isinstance(row, dict):
            return None

        normalized_condition_id = self._extract_condition_id(row)
        if expected_condition_id and normalized_condition_id != expected_condition_id:
            return None
        return dict(row)

    def _fetch_event_meta(self, slug: str) -> Optional[Dict[str, Any]]:
        try:
            data = http_get_json(
                self.session,
                f"{GAMMA_API}/events",
                params={"slug": slug, "limit": 1},
                timeout_s=15.0,
                max_retries=2,
            )
        except Exception:
            return None

        row = (
            data[0]
            if isinstance(data, list) and data and isinstance(data[0], dict)
            else data
            if isinstance(data, dict)
            else None
        )
        return dict(row) if isinstance(row, dict) else None

    @staticmethod
    def _first_event(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        events = meta.get("events")
        if isinstance(events, list) and events and isinstance(events[0], dict):
            return events[0]
        return None

    def _classify_slug_with_meta(
        self,
        normalized_slug: str,
        *,
        series_slug: Optional[str],
        recurrence: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        direct = self._classify_direct_slug(normalized_slug)
        if direct is not None:
            return direct
        return self._classify_from_series_meta(
            normalized_slug,
            series_slug=series_slug,
            recurrence=recurrence,
        )

    def _classify_direct_slug(self, normalized_slug: str) -> Optional[Dict[str, Any]]:
        match = CRYPTO_UPDOWN_SLUG_RE.match(normalized_slug)
        if match:
            asset = self._normalize_crypto_asset(match.group("asset"))
            timeframe = self._normalize_intraday_timeframe(match.group("tf"))
            if asset and timeframe:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe=timeframe,
                    kind="crypto_updown",
                )

        hourly = CRYPTO_HOURLY_EVENT_SLUG_RE.match(normalized_slug)
        if hourly:
            asset = self._normalize_crypto_asset(hourly.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1h",
                    kind="crypto_updown",
                )

        daily = CRYPTO_DAILY_SLUG_RE.match(normalized_slug)
        if daily:
            asset = self._normalize_crypto_asset(daily.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1d",
                    kind="crypto_updown_daily",
                )

        weekly_event = CRYPTO_WEEKLY_EVENT_SLUG_RE.match(normalized_slug)
        if weekly_event:
            asset = self._normalize_crypto_asset(weekly_event.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1w",
                    kind="crypto_threshold_weekly",
                )

        weekly_market = CRYPTO_WEEKLY_MARKET_SLUG_RE.match(normalized_slug)
        if weekly_market:
            asset = self._normalize_crypto_asset(weekly_market.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1w",
                    kind="crypto_threshold_weekly",
                )

        return None

    def _classify_from_series_meta(
        self,
        normalized_slug: str,
        *,
        series_slug: Optional[str],
        recurrence: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        normalized_series = self._clean_text(series_slug)
        normalized_recurrence = self._clean_text(recurrence)
        if not normalized_series:
            return None

        intraday = CRYPTO_UPDOWN_SERIES_RE.match(normalized_series)
        if intraday:
            asset = self._normalize_crypto_asset(intraday.group("asset"))
            timeframe = self._normalize_intraday_timeframe(intraday.group("tf"))
            if asset and timeframe:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe=timeframe,
                    kind="crypto_updown",
                )

        base_series = CRYPTO_UPDOWN_BASE_SERIES_RE.match(normalized_series)
        timeframe = self._normalize_intraday_timeframe(normalized_recurrence)
        if base_series and timeframe:
            asset = self._normalize_crypto_asset(base_series.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe=timeframe,
                    kind="crypto_updown",
                )

        daily = CRYPTO_DAILY_SERIES_RE.match(normalized_series)
        if daily:
            asset = self._normalize_crypto_asset(daily.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1d",
                    kind="crypto_updown_daily",
                )

        weekly = CRYPTO_WEEKLY_SERIES_RE.match(normalized_series)
        if weekly:
            asset = self._normalize_crypto_asset(weekly.group("asset"))
            if asset:
                return self._crypto_match_dict(
                    slug=normalized_slug,
                    asset=asset,
                    timeframe="1w",
                    kind="crypto_threshold_weekly",
                )

        return None

    @staticmethod
    def _crypto_match_dict(
        *,
        slug: str,
        asset: str,
        timeframe: str,
        kind: str,
    ) -> Dict[str, Any]:
        return {
            "is_crypto_updown": True,
            "timeframe": timeframe,
            "asset": asset,
            "slug": slug,
            "kind": kind,
        }

    @staticmethod
    def _extract_condition_id(meta: Dict[str, Any]) -> Optional[str]:
        value = meta.get("conditionId") or meta.get("condition_id")
        text = str(value or "").strip().lower()
        return text or None

    @staticmethod
    def _normalize_crypto_asset(value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        return CRYPTO_ASSET_ALIASES.get(text)

    @staticmethod
    def _normalize_intraday_timeframe(value: Any) -> Optional[str]:
        text = str(value or "").strip().lower()
        if not text:
            return None
        timeframe = CRYPTO_INTRADAY_TIMEFRAME_ALIASES.get(text)
        if timeframe in CRYPTO_INTRADAY_TIMEFRAME_SET:
            return timeframe
        return None

    @staticmethod
    def _clean_text(value: Any) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        return text.lower()

    @staticmethod
    def _clone_meta(meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return dict(meta) if isinstance(meta, dict) else None
