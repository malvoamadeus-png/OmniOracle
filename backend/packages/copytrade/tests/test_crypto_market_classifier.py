import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.market_classifier import CryptoMarketClassifier


class _DBStub:
    def __init__(self, *, by_condition=None, by_token=None):
        self.by_condition = by_condition or {}
        self.by_token = by_token or {}

    def find_latest_market_slug(self, *, condition_id=None, token_id=None):
        if condition_id:
            return self.by_condition.get(str(condition_id).lower())
        if token_id:
            return self.by_token.get(str(token_id).lower())
        return None


class CryptoMarketClassifierTests(unittest.TestCase):
    def _classifier(self, db=None):
        return CryptoMarketClassifier(requests.Session(), db or _DBStub())

    def _trade(self, *, market_slug=None, condition_id=None, token_id=None):
        return SimpleNamespace(
            market_slug=market_slug,
            condition_id=condition_id,
            token_id=token_id,
        )

    def test_direct_slug_classifies_btc_15m(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="btc-updown-15m-1775476800")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "15m")
        self.assertEqual(result.source, "trade.market_slug")

    def test_direct_slug_classifies_eth_4h(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="eth-updown-4h-1775476800")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "eth")
        self.assertEqual(result.timeframe, "4h")
        self.assertEqual(result.source, "trade.market_slug")

    def test_direct_slug_classifies_bitcoin_daily_event(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="bitcoin-up-or-down-on-april-9-2026")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1d")
        self.assertEqual(result.kind, "crypto_updown_daily")

    def test_direct_slug_classifies_bitcoin_hourly_event(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="bitcoin-up-or-down-april-9-2026-10am-et")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1h")
        self.assertEqual(result.kind, "crypto_updown")
        self.assertEqual(result.source, "trade.market_slug")

    def test_direct_slug_classifies_bitcoin_weekly_event(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="bitcoin-above-on-april-9")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1w")
        self.assertEqual(result.kind, "crypto_threshold_weekly")

    def test_direct_slug_classifies_bitcoin_weekly_market(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="bitcoin-above-60k-on-april-9")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1w")
        self.assertEqual(result.kind, "crypto_threshold_weekly")

    def test_hour_specific_threshold_slug_does_not_fall_into_weekly_bucket(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="bitcoin-above-70200-on-april-8-2026-11am-et")
        )

        self.assertFalse(result.is_crypto_updown)
        self.assertEqual(result.kind, "not_crypto")
        self.assertEqual(result.source, "trade.market_slug")

    def test_non_crypto_daily_slug_stays_rejected(self):
        classifier = self._classifier()

        result = classifier.classify_leader_trade(
            self._trade(market_slug="spx-up-or-down-on-march-10-2026")
        )

        self.assertFalse(result.is_crypto_updown)
        self.assertEqual(result.kind, "not_crypto")

    def test_local_condition_history_can_supply_slug(self):
        classifier = self._classifier(
            _DBStub(by_condition={"cond-1": "sol-updown-5m-1775476800"})
        )

        result = classifier.classify_leader_trade(
            self._trade(condition_id="cond-1")
        )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "sol")
        self.assertEqual(result.timeframe, "5m")
        self.assertEqual(result.source, "db.condition_id")

    def test_token_id_can_fetch_gamma_metadata(self):
        classifier = self._classifier()

        def fake_http_get_json(session, url, params=None, **kwargs):
            self.assertTrue(url.endswith("/markets"))
            self.assertEqual(params, {"clob_token_ids": "tok-1", "limit": 1})
            return [{
                "slug": "eth-updown-4h-1775476800",
                "conditionId": "cond-1",
                "events": [{
                    "seriesSlug": "eth-updown",
                    "series": [{"recurrence": "4h"}],
                }],
            }]

        with patch("copytrade.market_classifier.http_get_json", side_effect=fake_http_get_json):
            result = classifier.classify_leader_trade(
                self._trade(token_id="tok-1")
            )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "eth")
        self.assertEqual(result.timeframe, "4h")
        self.assertEqual(result.source, "gamma.token_id")
        self.assertEqual(result.series_slug, "eth-updown")
        self.assertEqual(result.recurrence, "4h")

    def test_market_slug_can_fallback_to_event_metadata_series(self):
        classifier = self._classifier()

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                self.assertEqual(params, {"slug": "mystery-weekly-slug", "limit": 1})
                return []
            if url.endswith("/events"):
                self.assertEqual(params, {"slug": "mystery-weekly-slug", "limit": 1})
                return [{
                    "slug": "mystery-weekly-slug",
                    "seriesSlug": "btc-multi-strikes-weekly",
                }]
            raise AssertionError(f"unexpected url={url} params={params}")

        with patch("copytrade.market_classifier.http_get_json", side_effect=fake_http_get_json):
            result = classifier.classify_leader_trade(
                self._trade(market_slug="mystery-weekly-slug")
            )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1w")
        self.assertEqual(result.kind, "crypto_threshold_weekly")
        self.assertEqual(result.source, "gamma.event_slug")

    def test_token_id_can_classify_hourly_from_series_metadata(self):
        classifier = self._classifier()

        def fake_http_get_json(session, url, params=None, **kwargs):
            if url.endswith("/markets"):
                self.assertEqual(
                    params,
                    {"clob_token_ids": "tok-1", "limit": 1},
                )
                return [{
                    "slug": "bitcoin-up-or-down-april-9-2026-10am-et",
                    "conditionId": "cond-1",
                    "events": [{
                        "seriesSlug": "btc-up-or-down-hourly",
                        "series": [{"slug": "btc-up-or-down-hourly", "recurrence": "hourly"}],
                    }],
                }]
            raise AssertionError(f"unexpected url={url} params={params}")

        with patch("copytrade.market_classifier.http_get_json", side_effect=fake_http_get_json):
            result = classifier.classify_leader_trade(
                self._trade(token_id="tok-1")
            )

        self.assertTrue(result.is_crypto_updown)
        self.assertEqual(result.asset, "btc")
        self.assertEqual(result.timeframe, "1h")
        self.assertEqual(result.kind, "crypto_updown")
        self.assertEqual(result.source, "gamma.token_id")
        self.assertEqual(result.series_slug, "btc-up-or-down-hourly")
        self.assertEqual(result.recurrence, "hourly")

    def test_condition_lookup_requires_exact_condition_id_match(self):
        classifier = self._classifier()

        def fake_http_get_json(session, url, params=None, **kwargs):
            self.assertTrue(url.endswith("/markets"))
            self.assertEqual(params, {"conditionId": "cond-expected", "limit": 1})
            return [{
                "slug": "btc-updown-15m-1775476800",
                "conditionId": "cond-other",
            }]

        with patch("copytrade.market_classifier.http_get_json", side_effect=fake_http_get_json):
            result = classifier.classify_leader_trade(
                self._trade(condition_id="cond-expected")
            )

        self.assertFalse(result.is_crypto_updown)
        self.assertEqual(result.kind, "unclassified")
        self.assertEqual(result.source, "unclassified")


if __name__ == "__main__":
    unittest.main()
