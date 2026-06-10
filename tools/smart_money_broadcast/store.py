from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "runtime" / "smart_money.sqlite"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SmartMoneyStore:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def _columns(self, table: str) -> set[str]:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row["name"]) for row in rows}

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS discovery_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                board TEXT,
                target_count INTEGER NOT NULL,
                min_age_days REAL NOT NULL,
                min_trades INTEGER NOT NULL,
                old_address_policy TEXT NOT NULL,
                found_count INTEGER NOT NULL DEFAULT 0,
                failure_reasons_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_run_boards (
                run_id INTEGER NOT NULL,
                board TEXT NOT NULL,
                scan_order INTEGER NOT NULL,
                PRIMARY KEY (run_id, board)
            );

            CREATE TABLE IF NOT EXISTS addresses (
                address TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                address_age_days REAL,
                user_stats_trades INTEGER,
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS address_boards (
                address TEXT NOT NULL,
                board TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_condition_id TEXT,
                first_market_slug TEXT,
                first_market_title TEXT,
                address_age_days REAL,
                user_stats_trades INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                PRIMARY KEY (address, board)
            );

            CREATE TABLE IF NOT EXISTS discovery_run_addresses (
                run_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                board TEXT NOT NULL DEFAULT 'NBA',
                decision TEXT NOT NULL,
                reason TEXT,
                source_condition_id TEXT,
                source_market_slug TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, address, board)
            );

            CREATE TABLE IF NOT EXISTS metrics_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                board TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_metrics_address_board_created
            ON metrics_snapshots(address, board, created_at DESC);

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL,
                board TEXT NOT NULL,
                report_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._migrate_legacy_schema()
        self._migrate_run_addresses_schema()
        self.conn.commit()

    def _migrate_legacy_schema(self) -> None:
        address_cols = self._columns("addresses")
        if "board" not in address_cols:
            return

        rows = self.conn.execute(
            """
            SELECT address, board, first_seen_at, last_seen_at, first_condition_id,
                   first_market_slug, first_market_title, address_age_days, user_stats_trades, status
            FROM addresses
            """
        ).fetchall()
        legacy_rows = [dict(row) for row in rows]
        self.conn.execute("ALTER TABLE addresses RENAME TO addresses_legacy")
        self.conn.executescript(
            """
            CREATE TABLE addresses (
                address TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                address_age_days REAL,
                user_stats_trades INTEGER,
                status TEXT NOT NULL DEFAULT 'active'
            );
            """
        )
        for row in legacy_rows:
            address = str(row.get("address") or "").lower()
            if not address:
                continue
            board = str(row.get("board") or "NBA").upper()
            first_seen = row.get("first_seen_at") or utc_now_iso()
            last_seen = row.get("last_seen_at") or first_seen
            self.conn.execute(
                """
                INSERT OR IGNORE INTO addresses(address, first_seen_at, last_seen_at, address_age_days, user_stats_trades, status)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    address,
                    first_seen,
                    last_seen,
                    row.get("address_age_days"),
                    row.get("user_stats_trades"),
                    row.get("status") or "active",
                ),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO address_boards(
                    address, board, first_seen_at, last_seen_at, first_condition_id,
                    first_market_slug, first_market_title, address_age_days, user_stats_trades, status
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    address,
                    board,
                    first_seen,
                    last_seen,
                    row.get("first_condition_id"),
                    row.get("first_market_slug"),
                    row.get("first_market_title"),
                    row.get("address_age_days"),
                    row.get("user_stats_trades"),
                    row.get("status") or "active",
                ),
            )
        self.conn.execute("DROP TABLE addresses_legacy")

    def _migrate_run_addresses_schema(self) -> None:
        cols = self._columns("discovery_run_addresses")
        if "board" in cols:
            return
        rows = self.conn.execute(
            """
            SELECT run_id, address, decision, reason, source_condition_id, source_market_slug, created_at
            FROM discovery_run_addresses
            """
        ).fetchall()
        legacy_rows = [dict(row) for row in rows]
        self.conn.execute("ALTER TABLE discovery_run_addresses RENAME TO discovery_run_addresses_legacy")
        self.conn.executescript(
            """
            CREATE TABLE discovery_run_addresses (
                run_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                board TEXT NOT NULL DEFAULT 'NBA',
                decision TEXT NOT NULL,
                reason TEXT,
                source_condition_id TEXT,
                source_market_slug TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (run_id, address, board)
            );
            """
        )
        for row in legacy_rows:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO discovery_run_addresses(
                    run_id, address, board, decision, reason, source_condition_id, source_market_slug, created_at
                )
                VALUES(?, ?, 'NBA', ?, ?, ?, ?, ?)
                """,
                (
                    row.get("run_id"),
                    str(row.get("address") or "").lower(),
                    row.get("decision"),
                    row.get("reason"),
                    row.get("source_condition_id"),
                    row.get("source_market_slug"),
                    row.get("created_at") or utc_now_iso(),
                ),
            )
        self.conn.execute("DROP TABLE discovery_run_addresses_legacy")

    def create_run(self, boards: Sequence[str], target_count: int, min_age_days: float, min_trades: int, policy: str) -> int:
        board_label = ",".join(str(board).upper() for board in boards)
        cur = self.conn.execute(
            """
            INSERT INTO discovery_runs(board, target_count, min_age_days, min_trades, old_address_policy, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (board_label, int(target_count), float(min_age_days), int(min_trades), policy, utc_now_iso()),
        )
        run_id = int(cur.lastrowid)
        for idx, board in enumerate(boards, start=1):
            self.conn.execute(
                "INSERT OR REPLACE INTO discovery_run_boards(run_id, board, scan_order) VALUES(?, ?, ?)",
                (run_id, str(board).upper(), idx),
            )
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: int, found_count: int, failure_reasons: Dict[str, int]) -> None:
        self.conn.execute(
            "UPDATE discovery_runs SET found_count=?, failure_reasons_json=? WHERE id=?",
            (int(found_count), json.dumps(failure_reasons, ensure_ascii=False), int(run_id)),
        )
        self.conn.commit()

    def has_address(self, address: str, board: Optional[str] = None) -> bool:
        addr = str(address or "").lower()
        if board is None:
            row = self.conn.execute("SELECT 1 FROM addresses WHERE address=? LIMIT 1", (addr,)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM address_boards WHERE address=? AND board=? LIMIT 1",
                (addr, str(board).upper()),
            ).fetchone()
        return row is not None

    def upsert_address(self, payload: Dict[str, Any], board: str) -> None:
        now = utc_now_iso()
        address = str(payload["address"]).lower()
        board_key = str(board).upper()
        self.conn.execute(
            """
            INSERT INTO addresses(address, first_seen_at, last_seen_at, address_age_days, user_stats_trades, status)
            VALUES(?, ?, ?, ?, ?, 'active')
            ON CONFLICT(address) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                address_age_days=COALESCE(excluded.address_age_days, addresses.address_age_days),
                user_stats_trades=COALESCE(excluded.user_stats_trades, addresses.user_stats_trades),
                status='active'
            """,
            (address, now, now, payload.get("address_age_days"), payload.get("user_stats_trades")),
        )
        self.conn.execute(
            """
            INSERT INTO address_boards(
                address, board, first_seen_at, last_seen_at, first_condition_id,
                first_market_slug, first_market_title, address_age_days, user_stats_trades, status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            ON CONFLICT(address, board) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                address_age_days=COALESCE(excluded.address_age_days, address_boards.address_age_days),
                user_stats_trades=COALESCE(excluded.user_stats_trades, address_boards.user_stats_trades),
                status='active'
            """,
            (
                address,
                board_key,
                now,
                now,
                payload.get("condition_id"),
                payload.get("slug"),
                payload.get("title"),
                payload.get("address_age_days"),
                payload.get("user_stats_trades"),
            ),
        )
        self.conn.commit()

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
        self.conn.execute(
            """
            INSERT OR REPLACE INTO discovery_run_addresses(
                run_id, address, board, decision, reason, source_condition_id, source_market_slug, created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, address.lower(), str(board).upper(), decision, reason, condition_id, slug, utc_now_iso()),
        )
        self.conn.commit()

    def save_metrics(self, address: str, metrics: Dict[str, Any], details: Optional[Dict[str, Any]] = None, board: str = "NBA") -> int:
        cur = self.conn.execute(
            """
            INSERT INTO metrics_snapshots(address, board, metrics_json, details_json, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                address.lower(),
                str(board).upper(),
                json.dumps(metrics, ensure_ascii=False),
                json.dumps(details or {}, ensure_ascii=False),
                utc_now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def latest_metrics_with_details(self, address: str, board: str = "NBA") -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT metrics_json, details_json FROM metrics_snapshots
            WHERE address=? AND board=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (address.lower(), str(board).upper()),
        ).fetchone()
        if row is None:
            return None
        return {
            "metrics": json.loads(row["metrics_json"]),
            "details": json.loads(row["details_json"] or "{}"),
        }

    def latest_metrics(self, address: str, board: str = "NBA") -> Optional[Dict[str, Any]]:
        payload = self.latest_metrics_with_details(address, board)
        return payload["metrics"] if payload is not None else None

    def latest_metrics_any_board_with_details(self, address: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT metrics_json, details_json FROM metrics_snapshots
            WHERE address=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (address.lower(),),
        ).fetchone()
        if row is None:
            return None
        return {
            "metrics": json.loads(row["metrics_json"]),
            "details": json.loads(row["details_json"] or "{}"),
        }

    def latest_metrics_any_board(self, address: str) -> Optional[Dict[str, Any]]:
        payload = self.latest_metrics_any_board_with_details(address)
        return payload["metrics"] if payload is not None else None

    def cohort_metrics(self, board: str = "NBA") -> List[Dict[str, Any]]:
        board_key = str(board).upper()
        rows = self.conn.execute(
            """
            SELECT m.address, m.metrics_json, m.created_at
            FROM address_boards ab
            JOIN metrics_snapshots m ON m.address=ab.address
            JOIN (
                SELECT address, MAX(created_at) AS created_at
                FROM metrics_snapshots
                GROUP BY address
            ) latest
            ON m.address=latest.address AND m.created_at=latest.created_at
            WHERE ab.board=?
            """,
            (board_key,),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            metrics["address"] = row["address"]
            metrics["snapshot_saved_at"] = row["created_at"]
            out.append(metrics)
        return out

    def save_report(self, address: str, report_path: Path, board: str = "NBA") -> None:
        self.conn.execute(
            "INSERT INTO reports(address, board, report_path, created_at) VALUES(?, ?, ?, ?)",
            (address.lower(), str(board).upper(), str(report_path), utc_now_iso()),
        )
        self.conn.commit()

    def cache_summary(self) -> Dict[str, Any]:
        address_count = self.conn.execute("SELECT COUNT(*) AS n FROM addresses").fetchone()["n"]
        metric_count = self.conn.execute("SELECT COUNT(*) AS n FROM metrics_snapshots").fetchone()["n"]
        report_count = self.conn.execute("SELECT COUNT(*) AS n FROM reports").fetchone()["n"]
        board_rows = self.conn.execute(
            """
            SELECT board, COUNT(*) AS n
            FROM address_boards
            GROUP BY board
            ORDER BY board
            """
        ).fetchall()
        return {
            "addresses": int(address_count),
            "metrics_snapshots": int(metric_count),
            "reports": int(report_count),
            "boards": {str(row["board"]): int(row["n"]) for row in board_rows},
            "db_path": str(self.path),
        }
