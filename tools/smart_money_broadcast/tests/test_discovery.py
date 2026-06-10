from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from discovery import DiscoveryResult, MarketInfo, discover_addresses, passes_gate
from metrics import DEFAULT_CLOSED_POSITIONS_LIMIT, METRICS_COMPAT_VERSION


class FakeStore:
    def __init__(self, old_addresses: Optional[set[str]] = None, latest: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.old_addresses = old_addresses or set()
        self.latest = latest or {}
        self.run_addresses: List[tuple[str, str]] = []
        self.saved_metrics: List[str] = []
        self.upserted: List[str] = []
        self.finished: Optional[tuple[int, int, Dict[str, int]]] = None

    def create_run(self, boards: List[str], target_count: int, min_age_days: float, min_trades: int, policy: str) -> int:
        self.boards = boards
        return 7

    def finish_run(self, run_id: int, found_count: int, failure_reasons: Dict[str, int]) -> None:
        self.finished = (run_id, found_count, failure_reasons)

    def has_address(self, address: str, board: str = "NBA") -> bool:
        return address in self.old_addresses

    def upsert_address(self, payload: Dict[str, Any], board: str = "NBA") -> None:
        self.upserted.append(payload["address"])

    def record_run_address(
        self,
        run_id: int,
        address: str,
        decision: str,
        reason: str = "",
        condition_id: str = "",
        slug: str = "",
        board: str = "NBA",
    ) -> None:
        self.run_addresses.append((address, decision))

    def latest_metrics(self, address: str, board: str = "NBA") -> Optional[Dict[str, Any]]:
        return self.latest.get(address)

    def latest_metrics_with_details(self, address: str, board: str = "NBA") -> Optional[Dict[str, Any]]:
        metrics = self.latest.get(address)
        if metrics is None:
            return None
        return {
            "metrics": metrics,
            "details": {
                "metrics_compat_version": METRICS_COMPAT_VERSION,
                "closed_positions_limit": DEFAULT_CLOSED_POSITIONS_LIMIT,
            },
        }

    def latest_metrics_any_board(self, address: str) -> Optional[Dict[str, Any]]:
        return self.latest.get(address)

    def latest_metrics_any_board_with_details(self, address: str) -> Optional[Dict[str, Any]]:
        metrics = self.latest.get(address)
        if metrics is None:
            return None
        return {
            "metrics": metrics,
            "details": {
                "metrics_compat_version": METRICS_COMPAT_VERSION,
                "closed_positions_limit": DEFAULT_CLOSED_POSITIONS_LIMIT,
            },
        }

    def save_metrics(self, address: str, metrics: Dict[str, Any], details: Dict[str, Any], board: str = "NBA") -> int:
        self.saved_metrics.append(address)
        self.latest[address] = metrics
        return len(self.saved_metrics)


@dataclass
class FakeMetricResult:
    metrics: Dict[str, Any]
    details: Dict[str, Any]


class DiscoveryTest(unittest.TestCase):
    def test_passes_gate(self) -> None:
        ok_profile = {"pnl_points": 3, "address_age_days": 100.0, "user_stats_trades": 101}
        self.assertEqual(passes_gate(ok_profile, 90, 100), (True, ""))
        self.assertEqual(passes_gate({**ok_profile, "pnl_points": 0}, 90, 100), (False, "no_pnl_points"))
        self.assertEqual(passes_gate({**ok_profile, "address_age_days": 10.0}, 90, 100), (False, "address_age_lt_threshold"))
        self.assertEqual(passes_gate({**ok_profile, "user_stats_trades": 100}, 90, 100), (False, "user_trades_le_threshold"))
        self.assertEqual(passes_gate({**ok_profile, "user_stats_trades": 30001}, 90, 100), (False, "user_trades_gt_threshold"))

    def run_policy_case(self, policy: str, target_count: int = 1) -> DiscoveryResult:
        market = MarketInfo(condition_id="cond1", market_id="1", slug="nba-game", title="NBA game", board="NBA")
        profiles = {
            "0xold": {"address": "0xold", "pnl_points": 5, "address_age_days": 120.0, "user_stats_trades": 200},
            "0xnew": {"address": "0xnew", "pnl_points": 5, "address_age_days": 120.0, "user_stats_trades": 200},
        }
        latest = {"0xold": {"total_pnl": 10.0}}
        store = FakeStore(old_addresses={"0xold"}, latest=latest)

        with patch("discovery.iter_board_markets", return_value=[market]), patch(
            "discovery.fetch_market_trade_addresses", return_value=["0xold", "0xnew"]
        ), patch("discovery.address_profile_for_gate", side_effect=lambda _client, address: profiles[address]), patch(
            "discovery.compute_address_metrics",
            side_effect=lambda _client, address, closed_positions_limit=DEFAULT_CLOSED_POSITIONS_LIMIT: FakeMetricResult(
                {"total_pnl": 20.0},
                {
                    "mock": True,
                    "closed_positions_limit": closed_positions_limit,
                    "metrics_compat_version": METRICS_COMPAT_VERSION,
                },
            ),
        ):
            result = discover_addresses(
                client=object(),
                store=store,  # type: ignore[arg-type]
                boards=["NBA"],
                target_count=target_count,
                min_age_days=90.0,
                min_trades=100,
                old_address_policy=policy,
            )
        result.store = store  # type: ignore[attr-defined]
        return result

    def test_reuse_old_metrics_policy_uses_cached_old_address(self) -> None:
        result = self.run_policy_case("reuse_old_metrics")
        store = result.store  # type: ignore[attr-defined]

        self.assertEqual([row["address"] for row in result.selected], ["0xold"])
        self.assertEqual(store.saved_metrics, [])
        self.assertEqual(result.selected[0]["metrics"], {"total_pnl": 10.0})

    def test_skip_old_policy_continues_to_new_address(self) -> None:
        result = self.run_policy_case("skip_old")
        store = result.store  # type: ignore[attr-defined]

        self.assertEqual([row["address"] for row in result.selected], ["0xnew"])
        self.assertEqual(store.saved_metrics, ["0xnew"])
        self.assertIn(("0xold", "skipped_old"), store.run_addresses)

    def test_refresh_old_metrics_policy_recomputes_old_address(self) -> None:
        result = self.run_policy_case("refresh_old_metrics")
        store = result.store  # type: ignore[attr-defined]

        self.assertEqual([row["address"] for row in result.selected], ["0xold"])
        self.assertEqual(store.saved_metrics, ["0xold"])
        self.assertEqual(result.selected[0]["metrics"], {"total_pnl": 20.0})

    def test_reuse_old_metrics_recomputes_incompatible_cached_snapshot(self) -> None:
        market = MarketInfo(condition_id="cond1", market_id="1", slug="nba-game", title="NBA game", board="NBA")
        profiles = {
            "0xold": {"address": "0xold", "pnl_points": 5, "address_age_days": 120.0, "user_stats_trades": 200},
        }
        store = FakeStore(old_addresses={"0xold"}, latest={"0xold": {"total_pnl": 10.0}})

        def bad_latest(_address: str, board: str = "NBA") -> Optional[Dict[str, Any]]:
            return {"metrics": {"total_pnl": 10.0}, "details": {"metrics_compat_version": "legacy", "closed_positions_limit": 80}}

        store.latest_metrics_with_details = bad_latest  # type: ignore[method-assign]
        store.latest_metrics_any_board_with_details = bad_latest  # type: ignore[method-assign]

        with patch("discovery.iter_board_markets", return_value=[market]), patch(
            "discovery.fetch_market_trade_addresses", return_value=["0xold"]
        ), patch("discovery.address_profile_for_gate", side_effect=lambda _client, address: profiles[address]), patch(
            "discovery.compute_address_metrics",
            side_effect=lambda _client, address, closed_positions_limit=DEFAULT_CLOSED_POSITIONS_LIMIT: FakeMetricResult(
                {"total_pnl": 20.0},
                {
                    "mock": True,
                    "closed_positions_limit": closed_positions_limit,
                    "metrics_compat_version": METRICS_COMPAT_VERSION,
                },
            ),
        ):
            result = discover_addresses(
                client=object(),
                store=store,  # type: ignore[arg-type]
                boards=["NBA"],
                target_count=1,
                min_age_days=90.0,
                min_trades=100,
                old_address_policy="reuse_old_metrics",
            )

        self.assertEqual([row["address"] for row in result.selected], ["0xold"])
        self.assertEqual(store.saved_metrics, ["0xold"])
        self.assertEqual(result.selected[0]["metrics"], {"total_pnl": 20.0})

    def test_multi_board_discovery_dedupes_selected_address_but_records_board_membership(self) -> None:
        markets = {
            "NBA": [MarketInfo(condition_id="cond-nba", market_id="1", slug="nba-game", title="NBA game", board="NBA")],
            "LOL": [MarketInfo(condition_id="cond-lol", market_id="2", slug="lol-game", title="LOL game", board="LOL")],
        }
        profiles = {
            "0xshared": {"address": "0xshared", "pnl_points": 5, "address_age_days": 120.0, "user_stats_trades": 200},
            "0xnew": {"address": "0xnew", "pnl_points": 5, "address_age_days": 120.0, "user_stats_trades": 200},
        }
        store = FakeStore()

        def fake_addresses(_client: object, condition_id: str) -> List[str]:
            if condition_id == "cond-nba":
                return ["0xshared"]
            return ["0xshared", "0xnew"]

        with patch("discovery.iter_board_markets", side_effect=lambda _client, board, max_markets=None: markets[board]), patch(
            "discovery.fetch_market_trade_addresses", side_effect=fake_addresses
        ), patch("discovery.address_profile_for_gate", side_effect=lambda _client, address: profiles[address]), patch(
            "discovery.compute_address_metrics",
            side_effect=lambda _client, address, closed_positions_limit=DEFAULT_CLOSED_POSITIONS_LIMIT: FakeMetricResult(
                {"total_pnl": 20.0},
                {
                    "mock": True,
                    "closed_positions_limit": closed_positions_limit,
                    "metrics_compat_version": METRICS_COMPAT_VERSION,
                },
            ),
        ):
            result = discover_addresses(
                client=object(),
                store=store,  # type: ignore[arg-type]
                boards=["NBA", "LOL"],
                target_count=2,
                min_age_days=90.0,
                min_trades=100,
                old_address_policy="reuse_old_metrics",
            )

        self.assertEqual([row["address"] for row in result.selected], ["0xshared", "0xnew"])
        self.assertEqual(store.upserted, ["0xshared", "0xshared", "0xnew"])
        self.assertIn(("0xshared", "selected_duplicate_board"), store.run_addresses)


if __name__ == "__main__":
    unittest.main()
