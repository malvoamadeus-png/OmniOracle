"""风控过滤链."""

import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import requests

from copytrade.polymarket_public_api import GAMMA_API, http_get_json

from copytrade.aggregation import get_effective_signal_price
from copytrade.config import (
    CopyTradeConfig,
    MARKET_LIMIT_GLOBAL_ONCE,
    MARKET_LIMIT_PER_ADDRESS,
    MARKET_LIMIT_UNLIMITED,
)
from copytrade.db import CopyTradeDB
from copytrade.market_classifier import CryptoMarketClassifier
from copytrade.monitor import LeaderTrade


class RiskManager:
    def __init__(self, session: requests.Session, config: CopyTradeConfig, db: CopyTradeDB, account_name: str = "default"):
        self.session = session
        self.config = config
        self.db = db
        self.account_name = account_name
        self._market_meta_cache: Dict[str, Dict[str, Any]] = {}
        self._event_market_cache: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._crypto_classifier = CryptoMarketClassifier(session, db)

    def check_crypto_only(self, leader_trade: LeaderTrade) -> Tuple[bool, str]:
        cfg = self.config.get_leader_config(leader_trade.leader_address)
        return self._check_crypto_only_filter(leader_trade, cfg)

    def check_all(self, leader_trade: LeaderTrade, our_usd: float) -> Tuple[bool, str]:
        """????????????????????????????(False, reason)."""
        # ??? per-leader config?????? override ???????????
        cfg = self.config.get_leader_config(leader_trade.leader_address)
        effective_price = get_effective_signal_price(leader_trade)

        # 1. ????????
        if effective_price is not None and effective_price < cfg.min_price:
            return False, f"price {effective_price:.4f} < min {cfg.min_price}"

        # 2. ????????
        if effective_price is not None and effective_price > cfg.max_price:
            return False, f"price {effective_price:.4f} > max {cfg.max_price}"

        # 3. 最小交易规模
        if leader_trade.usd_amount is not None and leader_trade.usd_amount < cfg.min_trade_size_usd:
            return False, f"leader usd {leader_trade.usd_amount:.2f} < min {cfg.min_trade_size_usd}"

        # 4. Crypto-only 过滤
        ok, reason = self._check_crypto_only_filter(leader_trade, cfg)
        if not ok:
            return False, reason

        # 5. 结算日期检查
        if cfg.settlement_days_max > 0 and leader_trade.condition_id:
            ok, reason = self._check_settlement_date(
                leader_trade.condition_id,
                cfg,
                market_slug=leader_trade.market_slug,
            )
            if not ok:
                return False, reason

        # 6. 市场跟单限制检查
        if leader_trade.token_id:
            mode = cfg.market_limit_mode
            max_entries = cfg.max_entries_per_market

            if mode == MARKET_LIMIT_GLOBAL_ONCE:
                if self.db.has_buy_attempt_for_token(
                    leader_trade.token_id,
                    account_name=self.account_name,
                ):
                    return False, f"global_once: already traded token {str(leader_trade.token_id)[:16]}"

            elif mode == MARKET_LIMIT_PER_ADDRESS:
                if self.db.has_buy_attempt_for_token(
                    leader_trade.token_id,
                    account_name=self.account_name,
                    leader_address=leader_trade.leader_address,
                ):
                    return False, f"per_address_once: already traded token {str(leader_trade.token_id)[:16]} from {leader_trade.leader_address[:8]}"

            elif mode == MARKET_LIMIT_UNLIMITED:
                # unlimited 模式下，如果设了 max_entries_per_market 则检查次数上限
                if max_entries > 0:
                    current_count = self.db.count_buy_entries_for_token_by_leader(
                        leader_trade.token_id,
                        leader_trade.leader_address,
                        account_name=self.account_name,
                    )
                    if current_count >= max_entries:
                        return False, f"max_entries: {current_count}/{max_entries} for token {str(leader_trade.token_id)[:16]}"

        # 7. 单笔最大头寸
        # 8. 每日限额
        return True, "ok"

    def _check_crypto_only_filter(
        self,
        leader_trade: LeaderTrade,
        cfg: CopyTradeConfig,
    ) -> Tuple[bool, str]:
        if not getattr(cfg, "crypto_only_enabled", False):
            return True, "ok"

        allowed = [
            str(item).strip()
            for item in getattr(cfg, "crypto_allowed_timeframes", []) or []
            if str(item).strip()
        ]
        if not allowed:
            return False, "crypto_only:invalid_config_no_timeframes"

        classification = self._crypto_classifier.classify_leader_trade(leader_trade)
        if classification.is_crypto_updown:
            timeframe = str(classification.timeframe or "").strip()
            if timeframe in allowed:
                return True, "ok"
            allowed_desc = "/".join(allowed)
            slug_desc = classification.slug or "unknown"
            return (
                False,
                f"crypto_only:timeframe_{timeframe}_not_allowed allowed={allowed_desc} slug={slug_desc}",
            )

        if classification.kind == "not_crypto":
            slug_desc = classification.slug or "unknown"
            return False, f"crypto_only:not_crypto_market slug={slug_desc} source={classification.source}"

        return False, f"crypto_only:unclassified_market source={classification.source}"

    def _check_settlement_date(
        self,
        condition_id: str,
        cfg: CopyTradeConfig = None,
        *,
        market_slug: Optional[str] = None,
    ) -> Tuple[bool, str]:
        if cfg is None:
            cfg = self.config
        meta = self._get_market_meta(condition_id, market_slug)
        if meta is None:
            return True, "ok"

        end_date_str = meta.get("endDate") or meta.get("endDateIso")
        if not end_date_str:
            return True, "ok"

        try:
            from dateutil import parser as date_parser
            end_dt = date_parser.isoparse(str(end_date_str))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_left = (end_dt - now).days
            if days_left > cfg.settlement_days_max:
                return False, f"settlement in {days_left}d > max {cfg.settlement_days_max}d"
        except Exception:
            pass

        return True, "ok"

    @staticmethod
    def _extract_condition_id(meta: Dict[str, Any]) -> str:
        return str(meta.get("conditionId") or meta.get("condition_id") or "").strip().lower()

    def _get_market_meta_from_event_slug(self, market_slug: str, condition_id: str) -> Optional[Dict[str, Any]]:
        slug = str(market_slug or "").strip()
        if not slug:
            return None

        cached = self._event_market_cache.get(slug)
        if cached is not None:
            return cached.get(condition_id)

        event_map: Dict[str, Dict[str, Any]] = {}
        try:
            data = http_get_json(
                self.session,
                f"{GAMMA_API}/events",
                params={"slug": slug, "limit": 1},
            )
            event = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data if isinstance(data, dict) else None
            markets = event.get("markets") if isinstance(event, dict) else None
            if isinstance(markets, list):
                for row in markets:
                    if not isinstance(row, dict):
                        continue
                    row_cid = self._extract_condition_id(row)
                    if row_cid:
                        event_map[row_cid] = row
        except Exception as e:
            sys.stderr.write(f"[risk] failed to fetch event market meta {slug}: {e}\n")

        self._event_market_cache[slug] = event_map
        return event_map.get(condition_id)

    def _get_market_meta_from_market_slug(self, market_slug: str, condition_id: str) -> Optional[Dict[str, Any]]:
        slug = str(market_slug or "").strip()
        if not slug:
            return None

        try:
            data = http_get_json(
                self.session,
                f"{GAMMA_API}/markets",
                params={"slug": slug, "limit": 1},
            )
            meta = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else data if isinstance(data, dict) else None
            if isinstance(meta, dict) and self._extract_condition_id(meta) == condition_id:
                return meta
        except Exception as e:
            sys.stderr.write(f"[risk] failed to fetch market meta by slug {slug}: {e}\n")

        return None

    def _get_market_meta(self, condition_id: str, market_slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
        cid = str(condition_id or "").strip().lower()
        if not cid:
            return None
        if cid in self._market_meta_cache:
            return self._market_meta_cache[cid]

        try:
            data = http_get_json(
                self.session,
                f"{GAMMA_API}/markets",
                params={"conditionId": cid, "limit": 1},
            )
            if isinstance(data, list) and data and isinstance(data[0], dict):
                meta = data[0]
                if self._extract_condition_id(meta) == cid:
                    self._market_meta_cache[cid] = meta
                    return meta
        except Exception as e:
            sys.stderr.write(f"[risk] failed to fetch market meta {cid}: {e}\n")

        meta = self._get_market_meta_from_market_slug(str(market_slug or ""), cid)
        if meta is not None:
            self._market_meta_cache[cid] = meta
            return meta

        meta = self._get_market_meta_from_event_slug(str(market_slug or ""), cid)
        if meta is not None:
            self._market_meta_cache[cid] = meta
            return meta

        return None
