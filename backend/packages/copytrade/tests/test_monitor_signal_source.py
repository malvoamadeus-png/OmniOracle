import tempfile
import unittest
from pathlib import Path

import requests

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from copytrade.db import CopyTradeDB
from copytrade.monitor import LeaderTrade, TradeMonitor, build_leader_fill_key


class _StubSubgraphMonitor(TradeMonitor):
    def __init__(
        self,
        db: CopyTradeDB,
        leader: str,
        *,
        maker_rows=None,
        taker_rows=None,
        token_meta=None,
        signal_source: str = "subgraph",
    ):
        super().__init__(
            requests.Session(),
            db,
            [leader],
            account_name="acct",
            signal_source=signal_source,
            fetch_workers=1,
        )
        self._maker_rows = [dict(r) for r in (maker_rows or [])]
        self._taker_rows = [dict(r) for r in (taker_rows or [])]
        self._token_meta = {str(k): dict(v) for k, v in (token_meta or {}).items()}

    def _query_subgraph_fills(self, session, address, role, start_ts, *, skip=0, limit=200):
        rows = self._maker_rows if role == "maker" else self._taker_rows
        return [dict(r) for r in rows[skip : skip + limit]]

    def _get_token_market_meta(self, token_id, *, session=None):
        meta = self._token_meta.get(str(token_id))
        return dict(meta) if meta is not None else None


class _HybridMonitor(TradeMonitor):
    def __init__(self, db: CopyTradeDB, leader: str, activity_rows, subgraph_rows):
        super().__init__(
            requests.Session(),
            db,
            [leader],
            account_name="acct",
            signal_source="hybrid",
            fetch_workers=1,
        )
        self._activity_rows = [dict(r) for r in activity_rows]
        self._subgraph_rows = [dict(r) for r in subgraph_rows]

    def _fetch_activity(self, address: str, start_ts: int, cutoff_ts: int):
        return [dict(r) for r in self._activity_rows]

    def _fetch_subgraph_trades(self, address: str, start_ts: int, cutoff_ts: int):
        return [dict(r) for r in self._subgraph_rows]

    def _active_sources(self):
        return ("subgraph", "activity")


class _StreamHybridMonitor(TradeMonitor):
    def __init__(self, db: CopyTradeDB, leader: str, activity_rows):
        super().__init__(
            requests.Session(),
            db,
            [leader],
            account_name="acct",
            signal_source="stream_hybrid",
            fetch_workers=1,
            signal_reconcile_interval_s=1,
        )
        self._activity_rows = [dict(r) for r in activity_rows]
        self.activity_calls = 0
        self.subgraph_calls = 0

    def _fetch_activity(self, address: str, start_ts: int, cutoff_ts: int):
        self.activity_calls += 1
        return [dict(r) for r in self._activity_rows]

    def _fetch_subgraph_trades(self, address: str, start_ts: int, cutoff_ts: int):
        self.subgraph_calls += 1
        raise AssertionError("stream_hybrid must not call legacy subgraph")


class MonitorSignalSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _db_path(self, name: str) -> Path:
        return Path(self.tmpdir.name) / f"{name}.sqlite"

    def test_subgraph_rows_are_parsed_into_buy_and_sell_signals(self):
        leader = "0xabc"
        token_id = "tok-1"
        db = CopyTradeDB(str(self._db_path("subgraph_parse")))
        try:
            monitor = _StubSubgraphMonitor(
                db,
                leader,
                maker_rows=[
                    {
                        "id": "evt-buy",
                        "timestamp": "1700000000",
                        "maker": leader,
                        "taker": "0xother",
                        "makerAssetId": "0",
                        "takerAssetId": token_id,
                        "makerAmountFilled": "250000000",
                        "takerAmountFilled": "1000000000",
                        "transactionHash": "0xtx-buy",
                    }
                ],
                taker_rows=[
                    {
                        "id": "evt-sell",
                        "timestamp": "1700000010",
                        "maker": "0xother",
                        "taker": leader,
                        "makerAssetId": "0",
                        "takerAssetId": token_id,
                        "makerAmountFilled": "150000000",
                        "takerAmountFilled": "500000000",
                        "transactionHash": "0xtx-sell",
                    }
                ],
                token_meta={
                    token_id: {
                        "condition_id": "cond-1",
                        "market_slug": "market-1",
                        "outcome_index": 0,
                        "outcome": "YES",
                    }
                },
            )

            rows = monitor._fetch_subgraph_trades(leader, 0, 0)
        finally:
            db.close()

        self.assertEqual(len(rows), 2)

        buy = next(r for r in rows if r["tx"] == "0xtx-buy")
        sell = next(r for r in rows if r["tx"] == "0xtx-sell")

        self.assertEqual(buy["side"], "BUY")
        self.assertEqual(buy["source"], "subgraph")
        self.assertEqual(buy["token_id"], token_id)
        self.assertEqual(buy["market"], "cond-1")
        self.assertAlmostEqual(float(buy["size"]), 1000.0)
        self.assertAlmostEqual(float(buy["usd"]), 250.0)
        self.assertAlmostEqual(float(buy["price"]), 0.25)

        self.assertEqual(sell["side"], "SELL")
        self.assertAlmostEqual(float(sell["size"]), 500.0)
        self.assertAlmostEqual(float(sell["usd"]), 150.0)
        self.assertAlmostEqual(float(sell["price"]), 0.30)

    def test_hybrid_source_dedupes_same_fill_and_prefers_activity_fields(self):
        leader = "0xabc"
        ts = 1_700_000_000
        activity = {
            "tx": "0xtx-1",
            "ts": str(ts),
            "side": "BUY",
            "usd": 42.0,
            "price": 0.42,
            "size": 100.0,
            "market": "cond-1",
            "slug": "activity-slug",
            "token_id": "tok-1",
            "outcome_index": 0,
            "outcome": "YES",
        }
        fill_key = build_leader_fill_key(leader, activity)
        activity["fill_key"] = fill_key

        subgraph = {
            "tx": "0xtx-1",
            "ts": str(ts),
            "side": "BUY",
            "usd": 42.0,
            "price": 0.42,
            "size": 100.0,
            "market": "cond-1",
            "slug": None,
            "token_id": "tok-1",
            "outcome_index": 0,
            "outcome": None,
            "fill_key": fill_key,
        }

        db = CopyTradeDB(str(self._db_path("hybrid_dedupe")))
        try:
            monitor = _HybridMonitor(db, leader, [activity], [subgraph])

            first = monitor.poll_once()
            second = monitor.poll_once()

            row = db.conn.execute(
                "SELECT market_slug, leader_fill_key, source FROM ct_signal_attempts WHERE account_name='acct'"
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)
        self.assertEqual(first[0].source, "subgraph")
        self.assertIsNotNone(row)
        self.assertEqual(row["leader_fill_key"], fill_key)
        self.assertEqual(row["market_slug"], "activity-slug")
        self.assertEqual(row["source"], "subgraph")

    def test_stream_hybrid_reconciles_from_activity_without_legacy_subgraph(self):
        leader = "0xabc"
        activity = {
            "tx": "0xtx-stream",
            "ts": "1700000000",
            "side": "BUY",
            "usd": 21.0,
            "price": 0.42,
            "size": 50.0,
            "market": "cond-stream",
            "slug": "activity-stream",
            "token_id": "tok-stream",
            "outcome_index": 0,
            "outcome": "YES",
        }
        activity["fill_key"] = build_leader_fill_key(leader, activity)

        db = CopyTradeDB(str(self._db_path("stream_hybrid_activity")))
        try:
            monitor = _StreamHybridMonitor(db, leader, [activity])

            rows = monitor.poll_once()

            saved = db.conn.execute(
                "SELECT leader_fill_key, market_slug, source FROM ct_signal_attempts WHERE account_name='acct'"
            ).fetchone()
        finally:
            db.close()

        self.assertEqual(monitor.activity_calls, 1)
        self.assertEqual(monitor.subgraph_calls, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].source, "activity")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["leader_fill_key"], activity["fill_key"])
        self.assertEqual(saved["market_slug"], "activity-stream")
        self.assertEqual(saved["source"], "activity")


if __name__ == "__main__":
    unittest.main()
