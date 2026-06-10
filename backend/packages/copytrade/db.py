"""SQLite schema + CRUD for copytrade system."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from copytrade.domain import (
    FILLED_ORDER_STATUSES,
    classify_order_fill_status,
)

LEGACY_ACCOUNT_SCOPE = "__legacy__"
CLOB_V2_CUTOVER_AT = datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc)
FILLED_TRADE_STATUS_SQL = "status IN ('filled','partially_filled')"
OPEN_ORDER_STATUS_SQL = "status IN ('submitted','partially_filled')"
AUTO_TP_SYNC_ERROR_MARK_COUNT = 3
AUTO_TP_SYNC_RETRY_BACKOFFS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso_utc(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _auto_tp_sync_retry_cutoffs(now: Optional[datetime] = None) -> Tuple[str, str, str]:
    current = now or datetime.now(timezone.utc)
    cutoffs = tuple((current - backoff).isoformat() for backoff in AUTO_TP_SYNC_RETRY_BACKOFFS)
    return cutoffs[0], cutoffs[1], cutoffs[2]


class _ThreadSafeConnection:
    """Wraps a sqlite3.Connection with a threading lock for all operations."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params: Any = ()) -> "_BufferedCursor":
        with self._lock:
            cur = self._conn.execute(sql, params)
            return _BufferedCursor(cur, buffered_rows=self._buffer_rows(cur))

    def executemany(self, sql: str, params: Any) -> "_BufferedCursor":
        with self._lock:
            cur = self._conn.executemany(sql, params)
            return _BufferedCursor(cur, buffered_rows=self._buffer_rows(cur))

    def executescript(self, sql: str) -> "_BufferedCursor":
        with self._lock:
            cur = self._conn.executescript(sql)
            return _BufferedCursor(cur, buffered_rows=self._buffer_rows(cur))

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _buffer_rows(cur: sqlite3.Cursor):
        if cur.description is None:
            return None
        return cur.fetchall()

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def __enter__(self):
        self._lock.acquire()
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            return self._conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self._lock.release()


class _BufferedCursor:
    """Cursor wrapper that buffers result rows inside the connection lock.

    This avoids sharing a live sqlite cursor across threads after `execute()`
    returns, which can otherwise corrupt reads on a single shared connection.
    """

    def __init__(self, cur: sqlite3.Cursor, *, buffered_rows):
        self._cur = cur
        self._rows = list(buffered_rows) if buffered_rows is not None else None
        self._index = 0
        self.description = cur.description
        self.rowcount = cur.rowcount
        self.lastrowid = cur.lastrowid

    def fetchone(self):
        if self._rows is None:
            return self._cur.fetchone()
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        if self._rows is None:
            return self._cur.fetchall()
        if self._index >= len(self._rows):
            return []
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass

    def __iter__(self):
        return iter(self.fetchall())


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ct_leader_state (
            address TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (address, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            leader_tx_hash TEXT NOT NULL,
            leader_fill_key TEXT,
            leader_side TEXT NOT NULL,
            leader_price REAL,
            leader_size REAL,
            leader_usd REAL,
            our_order_id TEXT,
            our_side TEXT,
            our_price REAL,
            our_size REAL,
            our_usd REAL,
            token_id TEXT,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            skip_reason TEXT,
            exit_status TEXT NOT NULL DEFAULT 'open',
            exit_price REAL,
            exit_usd REAL,
            exit_at TEXT,
            official_settlement_at TEXT,
            profit REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            requested_price REAL,
            requested_size REAL,
            requested_usd REAL,
            exchange_order_status TEXT,
            filled_size_actual REAL,
            filled_usd_actual REAL,
            partial_fill_status TEXT,
            our_limit_price REAL,
            our_filled_price REAL,
            is_aggregated_order INTEGER DEFAULT 0,
            aggregation_source_count INTEGER,
            UNIQUE(leader_fill_key, leader_address, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_exit_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            reason TEXT,
            order_id TEXT,
            token_id TEXT NOT NULL,
            side TEXT NOT NULL,
            requested_price REAL,
            requested_size REAL,
            requested_usd REAL,
            status TEXT NOT NULL DEFAULT 'submitted',
            exchange_order_status TEXT,
            filled_size_actual REAL,
            filled_usd_actual REAL,
            filled_price_actual REAL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(order_id, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_auto_tp_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            root_trade_id INTEGER NOT NULL,
            parent_lot_id INTEGER,
            leader_address TEXT NOT NULL,
            token_id TEXT NOT NULL,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            entry_price REAL NOT NULL,
            original_size REAL NOT NULL,
            remaining_size REAL NOT NULL,
            tp_target_size REAL NOT NULL DEFAULT 0,
            tp_filled_size REAL NOT NULL DEFAULT 0,
            pending_rebuy_size REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_auto_tp_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lot_id INTEGER NOT NULL,
            root_trade_id INTEGER NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            kind TEXT NOT NULL,
            order_id TEXT,
            side TEXT NOT NULL,
            requested_price REAL,
            requested_size REAL,
            requested_usd REAL,
            status TEXT NOT NULL DEFAULT 'submitted',
            exchange_order_status TEXT,
            filled_size_actual REAL,
            filled_usd_actual REAL,
            filled_price_actual REAL,
            last_error TEXT,
            last_sync_ok_at TEXT,
            last_sync_source TEXT,
            sync_error_count INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(order_id, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_auto_tp_bucket_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            token_id TEXT NOT NULL,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            kind TEXT NOT NULL,
            side TEXT NOT NULL,
            bucket_price REAL NOT NULL,
            requested_size REAL,
            requested_usd REAL,
            order_id TEXT,
            status TEXT NOT NULL DEFAULT 'submitted',
            exchange_order_status TEXT,
            filled_size_actual REAL,
            filled_usd_actual REAL,
            filled_price_actual REAL,
            last_error TEXT,
            last_sync_ok_at TEXT,
            last_sync_source TEXT,
            sync_error_count INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(order_id, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_auto_tp_bucket_order_lots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bucket_order_id INTEGER NOT NULL,
            lot_id INTEGER NOT NULL,
            root_trade_id INTEGER NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            requested_size REAL NOT NULL,
            filled_size_allocated REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_daily_spend (
            date_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            total_usd REAL NOT NULL DEFAULT 0,
            trade_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_attributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            leader_address TEXT NOT NULL,
            weight REAL,
            profit_share REAL,
            attributed_profit REAL,
            created_at TEXT NOT NULL,
            UNIQUE(condition_id, leader_address)
        );

        CREATE TABLE IF NOT EXISTS ct_seen_txs (
            tx_hash TEXT PRIMARY KEY,
            seen_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_seen_fills (
            fill_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            seen_at TEXT NOT NULL,
            PRIMARY KEY (fill_key, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_signal_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT,
            leader_fill_key TEXT,
            leader_side TEXT,
            token_id TEXT,
            condition_id TEXT,
            source TEXT,
            stage TEXT NOT NULL,
            reason TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_signal_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            leader_tx_hash TEXT,
            leader_fill_key TEXT NOT NULL,
            leader_side TEXT,
            leader_price REAL,
            leader_size REAL,
            leader_usd REAL,
            token_id TEXT,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            status TEXT NOT NULL DEFAULT 'detected',
            reason TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            retry_after TEXT,
            expires_at TEXT,
            last_error_code TEXT,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(leader_fill_key, leader_address, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_runtime_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT,
            component TEXT NOT NULL,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            message TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_worker_heartbeat (
            account_name TEXT NOT NULL,
            component TEXT NOT NULL,
            status TEXT NOT NULL,
            pid INTEGER,
            details_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (account_name, component)
        );

        CREATE TABLE IF NOT EXISTS ct_config_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor TEXT,
            account_name TEXT,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            restart_required INTEGER NOT NULL DEFAULT 0,
            details_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_leader_summary (
            leader_address TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            total_realized_pnl REAL NOT NULL DEFAULT 0,
            total_unrealized_pnl REAL NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0,
            winning_markets INTEGER NOT NULL DEFAULT 0,
            losing_markets INTEGER NOT NULL DEFAULT 0,
            total_markets INTEGER NOT NULL DEFAULT 0,
            win_rate REAL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (leader_address, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_leader_market_pnl (
            leader_address TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            market_slug TEXT,
            total_realized_pnl REAL NOT NULL DEFAULT 0,
            total_unrealized_pnl REAL NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0,
            market_result TEXT NOT NULL DEFAULT 'flat',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (leader_address, condition_id, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_leader_activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            leader_address TEXT NOT NULL,
            tx_hash TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            ts_epoch INTEGER NOT NULL,
            side TEXT NOT NULL,
            token_id TEXT,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            price REAL,
            size REAL,
            usd REAL,
            fetched_at TEXT NOT NULL,
            UNIQUE(leader_address, tx_hash)
        );

        CREATE TABLE IF NOT EXISTS ct_leader_activity_sync (
            leader_address TEXT PRIMARY KEY,
            max_ts_epoch INTEGER NOT NULL DEFAULT 0,
            total_records INTEGER NOT NULL DEFAULT 0,
            last_synced_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_config_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            leader_address TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            snapshot_reason TEXT NOT NULL,
            config_json TEXT NOT NULL,
            effective_from TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_ct_leader_summary_total_pnl
            ON ct_leader_summary(total_pnl DESC);
        CREATE INDEX IF NOT EXISTS idx_ct_signal_audit_account_time
            ON ct_signal_audit(account_name, created_at);
        CREATE INDEX IF NOT EXISTS idx_ct_signal_attempts_account_status
            ON ct_signal_attempts(account_name, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_ct_signal_attempts_fill
            ON ct_signal_attempts(leader_fill_key, account_name);
        CREATE INDEX IF NOT EXISTS idx_ct_runtime_events_account_time
            ON ct_runtime_events(account_name, created_at);
        CREATE INDEX IF NOT EXISTS idx_ct_leader_market_leader
            ON ct_leader_market_pnl(leader_address);
        CREATE INDEX IF NOT EXISTS idx_ct_leader_market_total_pnl
            ON ct_leader_market_pnl(total_pnl DESC);

        CREATE INDEX IF NOT EXISTS idx_ct_la_leader_ts
            ON ct_leader_activity(leader_address, ts_epoch);
        CREATE INDEX IF NOT EXISTS idx_ct_la_leader_cid
            ON ct_leader_activity(leader_address, condition_id);
        CREATE INDEX IF NOT EXISTS idx_ct_cs_leader_time
            ON ct_config_snapshots(leader_address, effective_from);

        CREATE TABLE IF NOT EXISTS ct_daily_equity (
            date_key TEXT PRIMARY KEY,
            total_equity REAL NOT NULL DEFAULT 0,
            total_realized_pnl REAL NOT NULL DEFAULT 0,
            total_unrealized_pnl REAL NOT NULL DEFAULT 0,
            total_cost_basis REAL NOT NULL DEFAULT 0,
            open_position_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ct_daily_leader_pnl (
            date_key TEXT NOT NULL,
            leader_address TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            realized_pnl REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            total_pnl REAL NOT NULL DEFAULT 0,
            market_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, leader_address, account_name)
        );

        CREATE TABLE IF NOT EXISTS ct_daily_leader_market_leg_pnl (
            date_key TEXT NOT NULL,
            leader_address TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            condition_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            market_slug TEXT,
            outcome TEXT,
            buy_fill_count INTEGER NOT NULL DEFAULT 0,
            buy_size REAL NOT NULL DEFAULT 0,
            buy_cost_usd REAL NOT NULL DEFAULT 0,
            sell_fill_count INTEGER NOT NULL DEFAULT 0,
            sell_size REAL NOT NULL DEFAULT 0,
            sell_proceeds_usd REAL NOT NULL DEFAULT 0,
            settled_size REAL NOT NULL DEFAULT 0,
            open_size_eod REAL NOT NULL DEFAULT 0,
            close_state_eod TEXT NOT NULL DEFAULT 'open',
            realized_pnl_delta REAL NOT NULL DEFAULT 0,
            unrealized_pnl_delta REAL NOT NULL DEFAULT 0,
            total_pnl_delta REAL NOT NULL DEFAULT 0,
            realized_pnl_eod REAL NOT NULL DEFAULT 0,
            unrealized_pnl_eod REAL NOT NULL DEFAULT 0,
            total_pnl_eod REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, leader_address, account_name, condition_id, token_id)
        );

        CREATE INDEX IF NOT EXISTS idx_ct_dlm_leg_lookup
            ON ct_daily_leader_market_leg_pnl(account_name, leader_address, date_key);
        CREATE INDEX IF NOT EXISTS idx_ct_dlm_market_lookup
            ON ct_daily_leader_market_leg_pnl(account_name, date_key, condition_id);

        CREATE TABLE IF NOT EXISTS ct_compare_open_leg_state (
            date_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            scope_kind TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            market_slug TEXT,
            outcome TEXT,
            bod_open_size REAL NOT NULL DEFAULT 0,
            bod_open_cost REAL NOT NULL DEFAULT 0,
            bod_avg_open_price REAL NOT NULL DEFAULT 0,
            bod_mark_price REAL,
            open_size REAL NOT NULL DEFAULT 0,
            open_cost REAL NOT NULL DEFAULT 0,
            avg_open_price REAL NOT NULL DEFAULT 0,
            unrealized_bod REAL NOT NULL DEFAULT 0,
            bod_cumulative_buy_fill_count INTEGER NOT NULL DEFAULT 0,
            bod_cumulative_buy_size REAL NOT NULL DEFAULT 0,
            bod_cumulative_buy_usd REAL NOT NULL DEFAULT 0,
            bod_cumulative_sell_fill_count INTEGER NOT NULL DEFAULT 0,
            bod_cumulative_sell_size REAL NOT NULL DEFAULT 0,
            bod_cumulative_sell_usd REAL NOT NULL DEFAULT 0,
            cumulative_buy_fill_count INTEGER NOT NULL DEFAULT 0,
            cumulative_buy_size REAL NOT NULL DEFAULT 0,
            cumulative_buy_usd REAL NOT NULL DEFAULT 0,
            cumulative_sell_fill_count INTEGER NOT NULL DEFAULT 0,
            cumulative_sell_size REAL NOT NULL DEFAULT 0,
            cumulative_sell_usd REAL NOT NULL DEFAULT 0,
            mark_price_now REAL,
            unrealized_now REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            exclusion_reason TEXT,
            settlement_time TEXT,
            last_event_ts TEXT,
            mark_price_source TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, account_name, leader_address, scope_kind, condition_id, token_id)
        );

        CREATE TABLE IF NOT EXISTS ct_compare_daily_market_leg (
            date_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            token_id TEXT NOT NULL,
            market_slug TEXT,
            outcome TEXT,
            exclusion_reason TEXT,
            leader_buy_fill_count INTEGER NOT NULL DEFAULT 0,
            leader_buy_usd REAL NOT NULL DEFAULT 0,
            leader_buy_avg_price REAL,
            leader_sell_fill_count INTEGER NOT NULL DEFAULT 0,
            leader_sell_usd REAL NOT NULL DEFAULT 0,
            leader_sell_avg_price REAL,
            leader_realized_pnl REAL NOT NULL DEFAULT 0,
            leader_unrealized_change REAL NOT NULL DEFAULT 0,
            leader_total_pnl REAL NOT NULL DEFAULT 0,
            our_buy_fill_count INTEGER NOT NULL DEFAULT 0,
            our_buy_usd REAL NOT NULL DEFAULT 0,
            our_buy_avg_price REAL,
            our_sell_fill_count INTEGER NOT NULL DEFAULT 0,
            our_sell_usd REAL NOT NULL DEFAULT 0,
            our_sell_avg_price REAL,
            our_realized_pnl REAL NOT NULL DEFAULT 0,
            our_unrealized_change REAL NOT NULL DEFAULT 0,
            our_total_pnl REAL NOT NULL DEFAULT 0,
            primary_gap_reason TEXT NOT NULL DEFAULT 'none',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, account_name, leader_address, condition_id, token_id)
        );

        CREATE TABLE IF NOT EXISTS ct_compare_daily_summary (
            date_key TEXT NOT NULL,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            leader_total_pnl REAL NOT NULL DEFAULT 0,
            our_total_pnl REAL NOT NULL DEFAULT 0,
            delta_pnl REAL NOT NULL DEFAULT 0,
            leader_excluded_pnl REAL NOT NULL DEFAULT 0,
            our_excluded_pnl REAL NOT NULL DEFAULT 0,
            visible_leader_pnl REAL NOT NULL DEFAULT 0,
            visible_our_pnl REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, account_name, leader_address)
        );

        CREATE INDEX IF NOT EXISTS idx_ct_compare_open_leg_state_lookup
            ON ct_compare_open_leg_state(date_key, account_name, leader_address, scope_kind);
        CREATE INDEX IF NOT EXISTS idx_ct_compare_daily_market_leg_lookup
            ON ct_compare_daily_market_leg(account_name, date_key, leader_address);
        CREATE INDEX IF NOT EXISTS idx_ct_compare_daily_market_leg_market
            ON ct_compare_daily_market_leg(account_name, date_key, condition_id);
        CREATE INDEX IF NOT EXISTS idx_ct_compare_daily_summary_lookup
            ON ct_compare_daily_summary(account_name, date_key);

        CREATE TABLE IF NOT EXISTS ct_resolved_prices (
            token_id TEXT PRIMARY KEY,
            resolution_price REAL NOT NULL,
            settlement_time TEXT,
            cached_at TEXT NOT NULL
        );

    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_ls_address ON ct_leader_state(address)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_sf_fill ON ct_seen_fills(fill_key)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_exit_orders_pending "
        "ON ct_exit_orders(account_name, status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_exit_orders_trade "
        "ON ct_exit_orders(trade_id, account_name, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_lots_root "
        "ON ct_auto_tp_lots(root_trade_id, account_name, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_lots_group "
        "ON ct_auto_tp_lots(account_name, leader_address, token_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_orders_pending "
        "ON ct_auto_tp_orders(account_name, status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_orders_lot "
        "ON ct_auto_tp_orders(lot_id, account_name, kind, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_bucket_orders_group "
        "ON ct_auto_tp_bucket_orders(account_name, leader_address, token_id, kind, status, bucket_price)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_bucket_orders_pending "
        "ON ct_auto_tp_bucket_orders(account_name, status, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_auto_tp_bucket_order_lots_bucket "
        "ON ct_auto_tp_bucket_order_lots(bucket_order_id, account_name, lot_id)"
    )
    conn.commit()


def _migrate_account_name(conn: sqlite3.Connection) -> None:
    """为已有数据库添加 account_name 列并修正约束（向后兼容迁移）.

    SQLite 不支持 ALTER TABLE 修改约束，所以用 rename-copy-drop 模式重建表。
    """
    # --- ct_daily_spend: 需要 PRIMARY KEY (date_key, account_name) ---
    cursor = conn.execute("PRAGMA table_info(ct_daily_spend)")
    columns = {row[1] for row in cursor.fetchall()}
    needs_rebuild_spend = "account_name" not in columns

    if not needs_rebuild_spend:
        # 列已存在，但检查 PK 是否正确（可能是旧迁移只加了列没改 PK）
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_daily_spend'"
        ).fetchone()
        if sql and "PRIMARY KEY (date_key, account_name)" not in sql[0]:
            needs_rebuild_spend = True

    if needs_rebuild_spend:
        # 检查旧表是否有 account_name 列
        old_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_daily_spend)").fetchall()}
        acct_expr = "COALESCE(account_name, 'default')" if "account_name" in old_cols else "'default'"
        conn.execute("ALTER TABLE ct_daily_spend RENAME TO _ct_daily_spend_old")
        conn.execute("""
            CREATE TABLE ct_daily_spend (
                date_key TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                total_usd REAL NOT NULL DEFAULT 0,
                trade_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, account_name)
            )
        """)
        conn.execute(f"""
            INSERT INTO ct_daily_spend (date_key, account_name, total_usd, trade_count, updated_at)
                SELECT date_key,
                       {acct_expr},
                       total_usd, trade_count, updated_at
                FROM _ct_daily_spend_old
        """)
        conn.execute("DROP TABLE _ct_daily_spend_old")
        conn.commit()

    # --- ct_trades: 需要 UNIQUE(leader_fill_key, leader_address, account_name) ---
    cursor = conn.execute("PRAGMA table_info(ct_trades)")
    columns = {row[1] for row in cursor.fetchall()}
    needs_rebuild_trades = "account_name" not in columns

    if not needs_rebuild_trades:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_trades'"
        ).fetchone()
        if sql and "account_name)" not in sql[0].split("UNIQUE")[-1]:
            needs_rebuild_trades = True

    if needs_rebuild_trades:
        old_trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_trades)").fetchall()}
        acct_expr_t = "COALESCE(account_name, 'default')" if "account_name" in old_trade_cols else "'default'"
        conn.execute("ALTER TABLE ct_trades RENAME TO _ct_trades_old")
        conn.execute("""
            CREATE TABLE ct_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_name TEXT NOT NULL DEFAULT 'default',
                leader_address TEXT NOT NULL,
                leader_tx_hash TEXT NOT NULL,
                leader_fill_key TEXT,
                leader_side TEXT NOT NULL,
                leader_price REAL,
                leader_size REAL,
                leader_usd REAL,
                our_order_id TEXT,
                our_side TEXT,
                our_price REAL,
                our_size REAL,
                our_usd REAL,
                token_id TEXT,
                condition_id TEXT,
                market_slug TEXT,
                outcome TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                skip_reason TEXT,
                exit_status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                exit_usd REAL,
                exit_at TEXT,
                official_settlement_at TEXT,
                profit REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(leader_fill_key, leader_address, account_name)
            )
        """)
        fill_key_expr = (
            "CASE WHEN leader_fill_key IS NULL OR TRIM(CAST(leader_fill_key AS TEXT))='' "
            "THEN 'legacy:' || id ELSE leader_fill_key END"
            if "leader_fill_key" in old_trade_cols
            else "'legacy:' || id"
        )
        conn.execute(f"""
            INSERT INTO ct_trades (
                id, account_name, leader_address, leader_tx_hash, leader_fill_key, leader_side,
                leader_price, leader_size, leader_usd,
                our_order_id, our_side, our_price, our_size, our_usd,
                token_id, condition_id, market_slug, outcome,
                status, skip_reason, exit_status, exit_price, exit_usd, exit_at, official_settlement_at,
                profit, created_at, updated_at
            ) SELECT
                id, {acct_expr_t}, leader_address, leader_tx_hash, {fill_key_expr}, leader_side,
                leader_price, leader_size, leader_usd,
                our_order_id, our_side, our_price, our_size, our_usd,
                token_id, condition_id, market_slug, outcome,
                status, skip_reason, exit_status, exit_price, exit_usd, exit_at,
                {("official_settlement_at" if "official_settlement_at" in old_trade_cols else "NULL")},
                profit, created_at, updated_at
            FROM _ct_trades_old
        """)
        conn.execute("DROP TABLE _ct_trades_old")
        conn.commit()

    # --- ct_leader_summary: 需要 PRIMARY KEY (leader_address, account_name) ---
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_leader_summary)").fetchall()}
    needs_rebuild = "account_name" not in cols
    if not needs_rebuild:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_leader_summary'"
        ).fetchone()
        if sql and "account_name)" not in (sql[0] or ""):
            needs_rebuild = True
    if needs_rebuild:
        old_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_leader_summary)").fetchall()}
        acct = "COALESCE(account_name, 'default')" if "account_name" in old_cols else "'default'"
        conn.execute("ALTER TABLE ct_leader_summary RENAME TO _ct_leader_summary_old")
        conn.execute("""
            CREATE TABLE ct_leader_summary (
                leader_address TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                total_realized_pnl REAL NOT NULL DEFAULT 0,
                total_unrealized_pnl REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                winning_markets INTEGER NOT NULL DEFAULT 0,
                losing_markets INTEGER NOT NULL DEFAULT 0,
                total_markets INTEGER NOT NULL DEFAULT 0,
                win_rate REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (leader_address, account_name)
            )
        """)
        conn.execute(f"""
            INSERT INTO ct_leader_summary (
                leader_address, account_name, total_realized_pnl, total_unrealized_pnl,
                total_pnl, winning_markets, losing_markets, total_markets, win_rate, updated_at
            ) SELECT leader_address, {acct}, total_realized_pnl, total_unrealized_pnl,
                total_pnl, winning_markets, losing_markets, total_markets, win_rate, updated_at
            FROM _ct_leader_summary_old
        """)
        conn.execute("DROP TABLE _ct_leader_summary_old")
        conn.commit()

    # --- ct_leader_market_pnl: 需要 PRIMARY KEY (leader_address, condition_id, account_name) ---
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_leader_market_pnl)").fetchall()}
    needs_rebuild = "account_name" not in cols
    if not needs_rebuild:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_leader_market_pnl'"
        ).fetchone()
        if sql and "account_name)" not in (sql[0] or ""):
            needs_rebuild = True
    if needs_rebuild:
        old_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_leader_market_pnl)").fetchall()}
        acct = "COALESCE(account_name, 'default')" if "account_name" in old_cols else "'default'"
        conn.execute("ALTER TABLE ct_leader_market_pnl RENAME TO _ct_leader_market_pnl_old")
        conn.execute("""
            CREATE TABLE ct_leader_market_pnl (
                leader_address TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                market_slug TEXT,
                total_realized_pnl REAL NOT NULL DEFAULT 0,
                total_unrealized_pnl REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                market_result TEXT NOT NULL DEFAULT 'flat',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (leader_address, condition_id, account_name)
            )
        """)
        conn.execute(f"""
            INSERT INTO ct_leader_market_pnl (
                leader_address, condition_id, account_name, market_slug,
                total_realized_pnl, total_unrealized_pnl, total_pnl, market_result, updated_at
            ) SELECT leader_address, condition_id, {acct}, market_slug,
                total_realized_pnl, total_unrealized_pnl, total_pnl, market_result, updated_at
            FROM _ct_leader_market_pnl_old
        """)
        conn.execute("DROP TABLE _ct_leader_market_pnl_old")
        conn.commit()

    # --- ct_daily_leader_pnl: 需要 PRIMARY KEY (date_key, leader_address, account_name) ---
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_daily_leader_pnl)").fetchall()}
    needs_rebuild = "account_name" not in cols
    if not needs_rebuild:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_daily_leader_pnl'"
        ).fetchone()
        if sql and "account_name)" not in (sql[0] or ""):
            needs_rebuild = True
    if needs_rebuild:
        old_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_daily_leader_pnl)").fetchall()}
        acct = "COALESCE(account_name, 'default')" if "account_name" in old_cols else "'default'"
        conn.execute("ALTER TABLE ct_daily_leader_pnl RENAME TO _ct_daily_leader_pnl_old")
        conn.execute("""
            CREATE TABLE ct_daily_leader_pnl (
                date_key TEXT NOT NULL,
                leader_address TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                realized_pnl REAL NOT NULL DEFAULT 0,
                unrealized_pnl REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                market_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, leader_address, account_name)
            )
        """)
        conn.execute(f"""
            INSERT INTO ct_daily_leader_pnl (
                date_key, leader_address, account_name,
                realized_pnl, unrealized_pnl, total_pnl, market_count, updated_at
            ) SELECT date_key, leader_address, {acct},
                realized_pnl, unrealized_pnl, total_pnl, market_count, updated_at
            FROM _ct_daily_leader_pnl_old
        """)
        conn.execute("DROP TABLE _ct_daily_leader_pnl_old")
        conn.commit()


def _migrate_fill_tracking(conn: sqlite3.Connection) -> None:
    """为 ct_trades 添加部分成交追踪 + 价格追踪 + 聚合标记列."""
    cursor = conn.execute("PRAGMA table_info(ct_trades)")
    columns = {row[1] for row in cursor.fetchall()}

    new_cols = [
        ("leader_fill_key", "TEXT"),
        ("requested_price", "REAL"),
        ("requested_size", "REAL"),
        ("requested_usd", "REAL"),
        ("exchange_order_status", "TEXT"),
        ("filled_size_actual", "REAL"),
        ("filled_usd_actual", "REAL"),
        ("partial_fill_status", "TEXT"),
        ("our_limit_price", "REAL"),
        ("our_filled_price", "REAL"),
        ("is_aggregated_order", "INTEGER DEFAULT 0"),
        ("aggregation_source_count", "INTEGER"),
    ]
    for col_name, col_type in new_cols:
        if col_name not in columns:
            conn.execute(f"ALTER TABLE ct_trades ADD COLUMN {col_name} {col_type}")
    conn.commit()


def _migrate_realization_tracking(conn: sqlite3.Connection) -> None:
    """Add strict settlement-date tracking for realized PnL attribution."""
    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_trades)").fetchall()}
    if trade_cols and "official_settlement_at" not in trade_cols:
        conn.execute("ALTER TABLE ct_trades ADD COLUMN official_settlement_at TEXT")

    resolved_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_resolved_prices)").fetchall()}
    if resolved_cols and "settlement_time" not in resolved_cols:
        conn.execute("ALTER TABLE ct_resolved_prices ADD COLUMN settlement_time TEXT")

    conn.commit()


def _migrate_trade_fill_key_unique(conn: sqlite3.Connection) -> None:
    """Ensure ct_trades uses activity-level unique key and backfills legacy rows."""
    cursor = conn.execute("PRAGMA table_info(ct_trades)")
    cols = {row[1] for row in cursor.fetchall()}
    if not cols:
        return

    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_trades'"
    ).fetchone()
    sql_text = (sql_row[0] or "") if sql_row else ""
    normalized = "".join(sql_text.lower().split())
    wanted = "unique(leader_fill_key,leader_address,account_name)"

    needs_rebuild = ("leader_fill_key" not in cols) or (wanted not in normalized)
    if not needs_rebuild:
        # Fast path: table shape is right, only backfill missing fill keys.
        conn.execute(
            "UPDATE ct_trades "
            "SET leader_fill_key='legacy:' || id "
            "WHERE leader_fill_key IS NULL OR TRIM(CAST(leader_fill_key AS TEXT))=''"
        )
        conn.commit()
        return

    old_cols = cols
    conn.execute("ALTER TABLE ct_trades RENAME TO _ct_trades_fill_old")
    conn.execute("""
        CREATE TABLE ct_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_name TEXT NOT NULL DEFAULT 'default',
            leader_address TEXT NOT NULL,
            leader_tx_hash TEXT NOT NULL,
            leader_fill_key TEXT,
            leader_side TEXT NOT NULL,
            leader_price REAL,
            leader_size REAL,
            leader_usd REAL,
            our_order_id TEXT,
            our_side TEXT,
            our_price REAL,
            our_size REAL,
            our_usd REAL,
            token_id TEXT,
            condition_id TEXT,
            market_slug TEXT,
            outcome TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            skip_reason TEXT,
            exit_status TEXT NOT NULL DEFAULT 'open',
            exit_price REAL,
            exit_usd REAL,
            exit_at TEXT,
            official_settlement_at TEXT,
            profit REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            requested_price REAL,
            requested_size REAL,
            requested_usd REAL,
            exchange_order_status TEXT,
            filled_size_actual REAL,
            filled_usd_actual REAL,
            partial_fill_status TEXT,
            our_limit_price REAL,
            our_filled_price REAL,
            is_aggregated_order INTEGER DEFAULT 0,
            aggregation_source_count INTEGER,
            UNIQUE(leader_fill_key, leader_address, account_name)
        )
    """)

    account_expr = "COALESCE(account_name, 'default')" if "account_name" in old_cols else "'default'"
    fill_key_expr = (
        "CASE WHEN leader_fill_key IS NULL OR TRIM(CAST(leader_fill_key AS TEXT))='' "
        "THEN 'legacy:' || id ELSE leader_fill_key END"
        if "leader_fill_key" in old_cols
        else "'legacy:' || id"
    )
    filled_size_actual_expr = "filled_size_actual" if "filled_size_actual" in old_cols else "NULL"
    filled_usd_actual_expr = "filled_usd_actual" if "filled_usd_actual" in old_cols else "NULL"
    partial_fill_status_expr = "partial_fill_status" if "partial_fill_status" in old_cols else "NULL"
    our_limit_price_expr = "our_limit_price" if "our_limit_price" in old_cols else "NULL"
    our_filled_price_expr = "our_filled_price" if "our_filled_price" in old_cols else "NULL"
    requested_price_expr = "requested_price" if "requested_price" in old_cols else "NULL"
    requested_size_expr = "requested_size" if "requested_size" in old_cols else "NULL"
    requested_usd_expr = "requested_usd" if "requested_usd" in old_cols else "NULL"
    exchange_order_status_expr = "exchange_order_status" if "exchange_order_status" in old_cols else "NULL"
    is_aggregated_order_expr = "COALESCE(is_aggregated_order, 0)" if "is_aggregated_order" in old_cols else "0"
    aggregation_source_count_expr = "aggregation_source_count" if "aggregation_source_count" in old_cols else "NULL"

    conn.execute(f"""
        INSERT INTO ct_trades (
            id, account_name, leader_address, leader_tx_hash, leader_fill_key,
            leader_side, leader_price, leader_size, leader_usd,
            our_order_id, our_side, our_price, our_size, our_usd,
            token_id, condition_id, market_slug, outcome,
            status, skip_reason, exit_status, exit_price, exit_usd, exit_at, official_settlement_at,
            profit, created_at, updated_at,
            requested_price, requested_size, requested_usd, exchange_order_status,
            filled_size_actual, filled_usd_actual, partial_fill_status,
            our_limit_price, our_filled_price, is_aggregated_order, aggregation_source_count
        ) SELECT
            id, {account_expr}, leader_address, leader_tx_hash, {fill_key_expr},
            leader_side, leader_price, leader_size, leader_usd,
            our_order_id, our_side, our_price, our_size, our_usd,
            token_id, condition_id, market_slug, outcome,
            status, skip_reason, exit_status, exit_price, exit_usd, exit_at,
            {("official_settlement_at" if "official_settlement_at" in old_cols else "NULL")},
            profit, created_at, updated_at,
            {requested_price_expr}, {requested_size_expr}, {requested_usd_expr}, {exchange_order_status_expr},
            {filled_size_actual_expr}, {filled_usd_actual_expr}, {partial_fill_status_expr},
            {our_limit_price_expr}, {our_filled_price_expr}, {is_aggregated_order_expr}, {aggregation_source_count_expr}
        FROM _ct_trades_fill_old
    """)
    conn.execute("DROP TABLE _ct_trades_fill_old")
    conn.commit()


def _migrate_account_scoped_monitor_state(conn: sqlite3.Connection) -> None:
    """Scope monitor state to individual accounts while preserving legacy progress."""
    # --- ct_leader_state: PRIMARY KEY (address, account_name) ---
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_leader_state)").fetchall()}
    needs_rebuild = "account_name" not in cols
    if not needs_rebuild:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_leader_state'"
        ).fetchone()
        normalized = "".join((sql[0] or "").lower().split()) if sql else ""
        needs_rebuild = "primarykey(address,account_name)" not in normalized

    if needs_rebuild:
        old_cols = cols
        conn.execute("ALTER TABLE ct_leader_state RENAME TO _ct_leader_state_old")
        conn.execute("""
            CREATE TABLE ct_leader_state (
                address TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                last_seen_ts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (address, account_name)
            )
        """)

        if old_cols:
            acct_expr = "COALESCE(account_name, 'default')" if "account_name" in old_cols else f"'{LEGACY_ACCOUNT_SCOPE}'"
            conn.execute(f"""
                INSERT INTO ct_leader_state(address, account_name, last_seen_ts, updated_at)
                SELECT LOWER(address), {acct_expr}, last_seen_ts, updated_at
                FROM _ct_leader_state_old
            """)
        conn.execute("DROP TABLE _ct_leader_state_old")
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_ls_address ON ct_leader_state(address)")

    # --- ct_seen_fills: PRIMARY KEY (fill_key, account_name) ---
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_seen_fills)").fetchall()}
    needs_rebuild = "account_name" not in cols
    if not needs_rebuild:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='ct_seen_fills'"
        ).fetchone()
        normalized = "".join((sql[0] or "").lower().split()) if sql else ""
        needs_rebuild = "primarykey(fill_key,account_name)" not in normalized

    if needs_rebuild:
        old_cols = cols
        conn.execute("ALTER TABLE ct_seen_fills RENAME TO _ct_seen_fills_old")
        conn.execute("""
            CREATE TABLE ct_seen_fills (
                fill_key TEXT NOT NULL,
                account_name TEXT NOT NULL DEFAULT 'default',
                seen_at TEXT NOT NULL,
                PRIMARY KEY (fill_key, account_name)
            )
        """)

        if old_cols:
            acct_expr = "COALESCE(account_name, 'default')" if "account_name" in old_cols else f"'{LEGACY_ACCOUNT_SCOPE}'"
            conn.execute(f"""
                INSERT INTO ct_seen_fills(fill_key, account_name, seen_at)
                SELECT fill_key, {acct_expr}, seen_at
                FROM _ct_seen_fills_old
                WHERE fill_key IS NOT NULL AND TRIM(CAST(fill_key AS TEXT)) <> ''
            """)
        conn.execute("DROP TABLE _ct_seen_fills_old")

    trade_cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_trades)").fetchall()}
    if trade_cols:
        trade_account_expr = "COALESCE(account_name, 'default')" if "account_name" in trade_cols else "'default'"
        conn.execute(f"""
            INSERT OR IGNORE INTO ct_seen_fills(fill_key, account_name, seen_at)
            SELECT leader_fill_key, {trade_account_expr}, MIN(created_at)
            FROM ct_trades
            WHERE leader_fill_key IS NOT NULL AND TRIM(CAST(leader_fill_key AS TEXT)) <> ''
            GROUP BY leader_fill_key, {trade_account_expr}
        """)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ct_sf_fill ON ct_seen_fills(fill_key)")
    conn.commit()


def _migrate_compare_open_leg_state_v2(conn: sqlite3.Connection) -> None:
    """Add cumulative compare carryover fields without rebuilding the table."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_compare_open_leg_state)").fetchall()}
    if not cols:
        return

    new_cols = [
        ("bod_cumulative_buy_fill_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bod_cumulative_buy_size", "REAL NOT NULL DEFAULT 0"),
        ("bod_cumulative_buy_usd", "REAL NOT NULL DEFAULT 0"),
        ("bod_cumulative_sell_fill_count", "INTEGER NOT NULL DEFAULT 0"),
        ("bod_cumulative_sell_size", "REAL NOT NULL DEFAULT 0"),
        ("bod_cumulative_sell_usd", "REAL NOT NULL DEFAULT 0"),
        ("cumulative_buy_fill_count", "INTEGER NOT NULL DEFAULT 0"),
        ("cumulative_buy_size", "REAL NOT NULL DEFAULT 0"),
        ("cumulative_buy_usd", "REAL NOT NULL DEFAULT 0"),
        ("cumulative_sell_fill_count", "INTEGER NOT NULL DEFAULT 0"),
        ("cumulative_sell_size", "REAL NOT NULL DEFAULT 0"),
        ("cumulative_sell_usd", "REAL NOT NULL DEFAULT 0"),
    ]
    changed = False
    for col_name, col_type in new_cols:
        if col_name in cols:
            continue
        conn.execute(f"ALTER TABLE ct_compare_open_leg_state ADD COLUMN {col_name} {col_type}")
        changed = True
    if changed:
        conn.commit()


def _migrate_auto_tp_sync_tracking(conn: sqlite3.Connection) -> None:
    for table in ("ct_auto_tp_orders", "ct_auto_tp_bucket_orders"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not cols:
            continue
        if "last_sync_ok_at" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN last_sync_ok_at TEXT")
        if "last_sync_source" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN last_sync_source TEXT")
        if "sync_error_count" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN sync_error_count INTEGER")
    conn.commit()


def _migrate_signal_attempt_retry_tracking(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ct_signal_attempts)").fetchall()}
    if not cols:
        return
    new_cols = [
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("retry_after", "TEXT"),
        ("expires_at", "TEXT"),
        ("last_error_code", "TEXT"),
        ("source", "TEXT"),
    ]
    changed = False
    for col_name, col_type in new_cols:
        if col_name in cols:
            continue
        conn.execute(f"ALTER TABLE ct_signal_attempts ADD COLUMN {col_name} {col_type}")
        changed = True
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ct_signal_attempts_retry "
        "ON ct_signal_attempts(account_name, status, retry_after, expires_at)"
    )
    if changed:
        conn.execute("UPDATE ct_signal_attempts SET retry_count=0 WHERE retry_count IS NULL")
    conn.commit()


class CopyTradeDB:
    def __init__(self, db_path: str = "copytrade.sqlite"):
        raw_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        raw_conn.row_factory = sqlite3.Row
        raw_conn.execute("PRAGMA busy_timeout=30000")
        self.conn = _ThreadSafeConnection(raw_conn)
        ensure_schema(self.conn)
        _migrate_account_name(self.conn)
        _migrate_fill_tracking(self.conn)
        _migrate_realization_tracking(self.conn)
        _migrate_trade_fill_key_unique(self.conn)
        _migrate_account_scoped_monitor_state(self.conn)
        _migrate_compare_open_leg_state_v2(self.conn)
        _migrate_auto_tp_sync_tracking(self.conn)
        _migrate_signal_attempt_retry_tracking(self.conn)

    def close(self) -> None:
        self.conn.close()

    # --- observability / audit ---

    @staticmethod
    def _json_dumps_safe(value: Any) -> str:
        try:
            return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            return "{}"

    def record_signal_audit(
        self,
        *,
        account_name: str = "default",
        leader_address: Optional[str] = None,
        leader_fill_key: Optional[str] = None,
        leader_side: Optional[str] = None,
        token_id: Optional[str] = None,
        condition_id: Optional[str] = None,
        source: Optional[str] = None,
        stage: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ct_signal_audit("
            "account_name, leader_address, leader_fill_key, leader_side, token_id, "
            "condition_id, source, stage, reason, details_json, created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_name or "default",
                (leader_address or "").lower() or None,
                leader_fill_key,
                leader_side,
                token_id,
                condition_id,
                source,
                stage,
                reason,
                self._json_dumps_safe(details),
                _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def record_runtime_event(
        self,
        *,
        component: str,
        event_type: str,
        severity: str = "info",
        account_name: Optional[str] = None,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ct_runtime_events("
            "account_name, component, event_type, severity, message, details_json, created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                account_name,
                component,
                event_type,
                severity,
                message,
                self._json_dumps_safe(details),
                _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def upsert_worker_heartbeat(
        self,
        *,
        account_name: str,
        component: str,
        status: str,
        pid: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO ct_worker_heartbeat("
            "account_name, component, status, pid, details_json, updated_at"
            ") VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(account_name, component) DO UPDATE SET "
            "status=excluded.status, pid=excluded.pid, details_json=excluded.details_json, "
            "updated_at=excluded.updated_at",
            (
                account_name or "default",
                component,
                status,
                pid,
                self._json_dumps_safe(details),
                now,
            ),
        )
        self.conn.commit()

    def record_config_audit(
        self,
        *,
        action: str,
        target: str,
        actor: Optional[str] = "admin",
        account_name: Optional[str] = None,
        restart_required: bool = False,
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ct_config_audit("
            "actor, account_name, action, target, restart_required, details_json, created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                actor,
                account_name,
                action,
                target,
                1 if restart_required else 0,
                self._json_dumps_safe(details),
                _now_iso(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def get_runtime_status(self, *, limit_events: int = 100) -> Dict[str, Any]:
        heartbeats = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM ct_worker_heartbeat ORDER BY account_name, component"
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM ct_runtime_events ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit_events or 100)),),
            ).fetchall()
        ]
        return {"heartbeats": heartbeats, "events": events}

    # --- leader state ---

    def get_last_seen_ts(self, address: str, account_name: str = "default") -> int:
        row = self.conn.execute(
            "SELECT last_seen_ts FROM ct_leader_state WHERE address = ? AND account_name = ?",
            (address.lower(), account_name),
        ).fetchone()
        if row is None and account_name != LEGACY_ACCOUNT_SCOPE:
            row = self.conn.execute(
                "SELECT last_seen_ts FROM ct_leader_state WHERE address = ? AND account_name = ?",
                (address.lower(), LEGACY_ACCOUNT_SCOPE),
            ).fetchone()
        return int(row["last_seen_ts"]) if row else 0

    def update_last_seen_ts(self, address: str, ts: int, account_name: str = "default") -> None:
        self.conn.execute(
            "INSERT INTO ct_leader_state(address, account_name, last_seen_ts, updated_at) "
            "VALUES(?, ?, ?, ?) "
            "ON CONFLICT(address, account_name) DO UPDATE SET last_seen_ts=excluded.last_seen_ts, updated_at=excluded.updated_at",
            (address.lower(), account_name, ts, _now_iso()),
        )
        self.conn.commit()

    # --- seen txs ---

    def is_tx_seen(self, tx_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ct_seen_txs WHERE tx_hash = ?", (tx_hash,)
        ).fetchone()
        return row is not None

    def mark_tx_seen(self, tx_hash: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ct_seen_txs(tx_hash, seen_at) VALUES(?, ?)",
            (tx_hash, _now_iso()),
        )
        self.conn.commit()

    def is_fill_seen(self, fill_key: str, account_name: str = "default") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ct_seen_fills WHERE fill_key = ? AND account_name IN (?, ?) LIMIT 1",
            (fill_key, account_name, LEGACY_ACCOUNT_SCOPE),
        ).fetchone()
        return row is not None

    def mark_fill_seen(self, fill_key: str, account_name: str = "default") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO ct_seen_fills(fill_key, account_name, seen_at) VALUES(?, ?, ?)",
            (fill_key, account_name, _now_iso()),
        )
        self.conn.commit()

    def claim_leader_fill(self, trade: Dict[str, Any], account_name: str = "default") -> Optional[int]:
        now = _now_iso()
        leader_address = (trade.get("leader_address") or "").lower()
        fill_key = trade.get("leader_fill_key")
        status = str(trade.get("status") or "detected")
        if not leader_address or not fill_key:
            return None

        self.conn.execute(
            "INSERT OR IGNORE INTO ct_signal_attempts("
            "account_name, leader_address, leader_tx_hash, leader_fill_key, "
            "leader_side, leader_price, leader_size, leader_usd, "
            "token_id, condition_id, market_slug, outcome, "
            "status, source, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                account_name,
                leader_address,
                trade.get("leader_tx_hash") or "",
                fill_key,
                trade.get("leader_side") or "",
                trade.get("leader_price"),
                trade.get("leader_size"),
                trade.get("leader_usd"),
                trade.get("token_id"),
                trade.get("condition_id"),
                trade.get("market_slug"),
                trade.get("outcome"),
                status,
                trade.get("source"),
                now,
                now,
            ),
        )
        inserted = bool(self.conn.execute("SELECT changes()").fetchone()[0])

        row = self.conn.execute(
            "SELECT id, status FROM ct_signal_attempts "
            "WHERE leader_fill_key=? AND leader_address=? AND account_name=?",
            (fill_key, leader_address, account_name),
        ).fetchone()
        if row is None:
            self.conn.commit()
            return None

        if inserted or row["status"] == "pending_retry":
            self.conn.execute(
                "UPDATE ct_signal_attempts SET "
                "leader_tx_hash=?, leader_side=?, leader_price=?, leader_size=?, leader_usd=?, "
                "token_id=?, condition_id=?, market_slug=?, outcome=?, status=?, source=?, updated_at=? "
                "WHERE id=?",
                (
                    trade.get("leader_tx_hash") or "",
                    trade.get("leader_side") or "",
                    trade.get("leader_price"),
                    trade.get("leader_size"),
                    trade.get("leader_usd"),
                    trade.get("token_id"),
                    trade.get("condition_id"),
                    trade.get("market_slug"),
                    trade.get("outcome"),
                    status,
                    trade.get("source"),
                    now,
                    int(row["id"]),
                ),
            )
            self.conn.commit()
            return int(row["id"])

        self.conn.commit()
        return None

    def update_signal_attempt_status(
        self,
        attempt_id: int,
        status: str,
        *,
        reason: Optional[str] = None,
        **fields: Any,
    ) -> None:
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, _now_iso()]
        if reason is not None:
            sets.append("reason = ?")
            vals.append(reason)
        allowed_fields = {
            "leader_tx_hash",
            "leader_side",
            "leader_price",
            "leader_size",
            "leader_usd",
            "token_id",
            "condition_id",
            "market_slug",
            "outcome",
            "retry_count",
            "retry_after",
            "expires_at",
            "last_error_code",
            "source",
        }
        for key, value in fields.items():
            if key not in allowed_fields:
                continue
            sets.append(f"{key} = ?")
            vals.append(value)
        vals.append(int(attempt_id))
        self.conn.execute(
            f"UPDATE ct_signal_attempts SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        self.conn.commit()

    def mark_signal_attempt_maintenance_pending(
        self,
        attempt_id: int,
        *,
        reason: str,
        error_code: str = "balance_allowance",
        expires_at: Optional[str] = None,
        retry_after: Optional[str] = None,
        retry_window_s: int = 15 * 60,
    ) -> bool:
        row = self.conn.execute(
            "SELECT id, created_at FROM ct_signal_attempts WHERE id=?",
            (int(attempt_id),),
        ).fetchone()
        if row is None:
            return False
        if not expires_at:
            base = _parse_iso_utc(row["created_at"]) or datetime.now(timezone.utc)
            expires_at = (base + timedelta(seconds=max(0, int(retry_window_s)))).isoformat()
        self.conn.execute(
            "UPDATE ct_signal_attempts SET "
            "status='maintenance_pending', reason=?, retry_count=0, retry_after=?, "
            "expires_at=?, last_error_code=?, updated_at=? "
            "WHERE id=?",
            (
                reason,
                retry_after,
                expires_at,
                str(error_code or "").strip().lower() or None,
                _now_iso(),
                int(attempt_id),
            ),
        )
        self.conn.commit()
        return True

    def release_maintenance_signal_attempts(self, account_name: str = "default") -> int:
        now = _now_iso()
        self.conn.execute(
            "UPDATE ct_signal_attempts SET retry_after=?, updated_at=? "
            "WHERE account_name=? AND status='maintenance_pending' "
            "AND COALESCE(retry_count, 0)=0 "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (now, now, account_name, now),
        )
        changed = self.conn.execute("SELECT changes()").fetchone()
        self.conn.commit()
        return int(changed[0] if changed else 0)

    def expire_maintenance_signal_attempts(self, account_name: str = "default") -> List[Dict[str, Any]]:
        now = _now_iso()
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM ct_signal_attempts "
                "WHERE account_name=? AND status='maintenance_pending' "
                "AND expires_at IS NOT NULL AND expires_at <= ? "
                "ORDER BY expires_at ASC, id ASC",
                (account_name, now),
            ).fetchall()
        ]
        if not rows:
            return []
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" * len(ids))
        self.conn.execute(
            f"UPDATE ct_signal_attempts SET status='skipped', reason=?, updated_at=? "
            f"WHERE id IN ({placeholders})",
            tuple(["maintenance_retry_expired", now] + ids),
        )
        self.conn.commit()
        return rows

    def get_due_maintenance_signal_attempts(
        self,
        account_name: str = "default",
        *,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        now = _now_iso()
        rows = self.conn.execute(
            "SELECT t.*, 'ct_signal_attempts' AS source_table FROM ct_signal_attempts t "
            "WHERE t.account_name=? AND t.status='maintenance_pending' "
            "AND UPPER(COALESCE(t.leader_side, ''))='BUY' "
            "AND COALESCE(t.retry_count, 0)=0 "
            "AND t.retry_after IS NOT NULL AND t.retry_after <= ? "
            "AND (t.expires_at IS NULL OR t.expires_at > ?) "
            "ORDER BY t.retry_after ASC, t.created_at ASC, t.id ASC LIMIT ?",
            (account_name, now, now, max(1, int(limit or 50))),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_maintenance_signal_attempt_retry(self, attempt_id: int) -> bool:
        now = _now_iso()
        self.conn.execute(
            "UPDATE ct_signal_attempts SET retry_count=COALESCE(retry_count, 0)+1, "
            "retry_after=NULL, updated_at=? "
            "WHERE id=? AND status='maintenance_pending' "
            "AND COALESCE(retry_count, 0)=0 "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (now, int(attempt_id), now),
        )
        changed = self.conn.execute("SELECT changes()").fetchone()
        self.conn.commit()
        return bool(int(changed[0] if changed else 0))

    def get_pending_leader_trades(
        self,
        account_name: str = "default",
        *,
        stale_after_s: int = 30,
        pending_retry_stale_after_s: Optional[int] = None,
        max_age_s: Optional[int] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        detected_cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(0, stale_after_s))
        ).isoformat()
        pending_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(
                seconds=max(
                    0,
                    stale_after_s
                    if pending_retry_stale_after_s is None
                    else pending_retry_stale_after_s,
                )
            )
        ).isoformat()
        sql = (
            "SELECT t.*, 'ct_signal_attempts' AS source_table FROM ct_signal_attempts t "
            "WHERE t.account_name=? AND ("
            "(t.status='detected' AND t.updated_at <= ?) "
            "OR (t.status='pending_retry' AND t.updated_at <= ?)"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM ct_seen_fills sf "
            "WHERE sf.fill_key=t.leader_fill_key AND sf.account_name IN (?, ?)"
            ") "
        )
        params: list = [
            account_name,
            detected_cutoff,
            pending_cutoff,
            account_name,
            LEGACY_ACCOUNT_SCOPE,
        ]
        if max_age_s is not None:
            max_age_cutoff = (
                datetime.now(timezone.utc) - timedelta(seconds=max(0, int(max_age_s)))
            ).isoformat()
            sql += "AND t.created_at >= ? "
            params.append(max_age_cutoff)
        sql += "ORDER BY t.created_at ASC LIMIT ?"
        params.append(limit)
        rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]

        remaining = max(0, int(limit) - len(rows))
        if remaining <= 0:
            return rows

        legacy_sql = (
            "SELECT t.*, 'ct_trades' AS source_table FROM ct_trades t "
            "WHERE t.account_name=? AND ("
            "(t.status='detected' AND t.updated_at <= ?) "
            "OR (t.status='pending_retry' AND t.updated_at <= ?)"
            ") "
            "AND NOT EXISTS ("
            "SELECT 1 FROM ct_seen_fills sf "
            "WHERE sf.fill_key=t.leader_fill_key AND sf.account_name IN (?, ?)"
            ") "
        )
        legacy_params: list = [
            account_name,
            detected_cutoff,
            pending_cutoff,
            account_name,
            LEGACY_ACCOUNT_SCOPE,
        ]
        if max_age_s is not None:
            legacy_sql += "AND t.created_at >= ? "
            legacy_params.append(max_age_cutoff)
        legacy_sql += "ORDER BY t.created_at ASC LIMIT ?"
        legacy_params.append(remaining)
        rows.extend(dict(r) for r in self.conn.execute(legacy_sql, legacy_params).fetchall())
        return rows

    # --- trades ---

    def insert_trade(self, trade: Dict[str, Any]) -> int:
        now = _now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ct_trades("
            "account_name, leader_address, leader_tx_hash, leader_fill_key, leader_side, leader_price, leader_size, leader_usd, "
            "our_order_id, our_side, our_price, our_size, our_usd, "
            "token_id, condition_id, market_slug, outcome, "
            "status, skip_reason, exit_status, "
            "requested_price, requested_size, requested_usd, exchange_order_status, "
            "filled_size_actual, filled_usd_actual, partial_fill_status, "
            "our_limit_price, our_filled_price, is_aggregated_order, aggregation_source_count, "
            "official_settlement_at, "
            "created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                trade.get("account_name", "default"),
                trade.get("leader_address", "").lower(),
                trade.get("leader_tx_hash"),
                trade.get("leader_fill_key"),
                trade.get("leader_side"),
                trade.get("leader_price"),
                trade.get("leader_size"),
                trade.get("leader_usd"),
                trade.get("our_order_id"),
                trade.get("our_side"),
                trade.get("our_price"),
                trade.get("our_size"),
                trade.get("our_usd"),
                trade.get("token_id"),
                trade.get("condition_id"),
                trade.get("market_slug"),
                trade.get("outcome"),
                trade.get("status", "pending"),
                trade.get("skip_reason"),
                trade.get("exit_status", "open"),
                trade.get("requested_price"),
                trade.get("requested_size"),
                trade.get("requested_usd"),
                trade.get("exchange_order_status"),
                trade.get("filled_size_actual"),
                trade.get("filled_usd_actual"),
                trade.get("partial_fill_status"),
                trade.get("our_limit_price"),
                trade.get("our_filled_price"),
                trade.get("is_aggregated_order", 0),
                trade.get("aggregation_source_count"),
                trade.get("official_settlement_at"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def update_trade_status(self, trade_id: int, status: str, **kwargs: Any) -> None:
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, _now_iso()]
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(trade_id)
        self.conn.execute(
            f"UPDATE ct_trades SET {', '.join(sets)} WHERE id = ?", vals
        )
        self.conn.commit()

    def update_trade_exit(
        self, trade_id: int, exit_status: str, exit_price: Optional[float],
        exit_usd: Optional[float], profit: Optional[float],
    ) -> None:
        self.conn.execute(
            "UPDATE ct_trades SET exit_status=?, exit_price=?, exit_usd=?, exit_at=?, profit=?, updated_at=? WHERE id=?",
            (exit_status, exit_price, exit_usd, _now_iso(), profit, _now_iso(), trade_id),
        )
        self.conn.commit()

    def apply_entry_fill(
        self,
        trade_id: int,
        bought_size: float,
        bought_usd: Optional[float],
        *,
        fill_price: Optional[float] = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT our_size, our_usd, our_filled_price FROM ct_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            return

        buy_sz = max(0.0, float(bought_size or 0.0))
        if buy_sz <= 0:
            return
        buy_usd = float(bought_usd or 0.0)
        if buy_usd <= 0 and isinstance(fill_price, (int, float)) and float(fill_price) > 0:
            buy_usd = buy_sz * float(fill_price)

        cur_size = float(row["our_size"] or 0.0)
        cur_usd = float(row["our_usd"] or 0.0)
        new_size = cur_size + buy_sz
        new_usd = cur_usd + max(0.0, buy_usd)
        avg_price = (new_usd / new_size) if new_size > 0 else None
        actual_fill_price = (
            float(fill_price)
            if isinstance(fill_price, (int, float)) and float(fill_price) > 0
            else row["our_filled_price"]
        )

        self.conn.execute(
            "UPDATE ct_trades SET "
            "status=?, our_size=?, our_usd=?, our_price=?, our_filled_price=?, updated_at=? "
            "WHERE id=?",
            (
                "filled",
                new_size,
                new_usd,
                avg_price,
                actual_fill_price,
                _now_iso(),
                trade_id,
            ),
        )
        self.conn.commit()

    def apply_exit_fill(
        self,
        trade_id: int,
        sold_size: float,
        exit_price: Optional[float],
        sold_usd: Optional[float],
        profit_delta: Optional[float],
        close_position: bool,
        cost_basis_usd: Optional[float] = None,
    ) -> None:
        """应用一笔离场成交，支持部分平仓.

        - close_position=True: 标记 exited，剩余仓位归零
        - close_position=False: 维持 open，按卖出比例扣减 our_size/our_usd
        """
        row = self.conn.execute(
            "SELECT our_size, our_usd, exit_usd, profit FROM ct_trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        if not row:
            return

        cur_size = float(row["our_size"] or 0.0)
        cur_usd = float(row["our_usd"] or 0.0)
        prev_exit_usd = float(row["exit_usd"] or 0.0)
        prev_profit = float(row["profit"] or 0.0)

        sell_sz = min(cur_size, max(0.0, float(sold_size or 0.0)))
        sell_usd = float(sold_usd or 0.0)
        if sell_usd <= 0 and sell_sz > 0 and isinstance(exit_price, (int, float)) and float(exit_price) > 0:
            sell_usd = sell_sz * float(exit_price)
        new_exit_usd = prev_exit_usd + sell_usd
        new_profit = prev_profit + float(profit_delta or 0.0)

        # 线性扣减剩余仓位与剩余成本（our_usd）
        rem_size = max(0.0, cur_size - sell_sz)
        if rem_size <= 1e-9:
            rem_usd = 0.0
        elif cost_basis_usd is not None:
            rem_usd = max(0.0, cur_usd - max(0.0, min(cur_usd, float(cost_basis_usd or 0.0))))
        elif cur_size > 0 and cur_usd >= 0:
            unit_cost = cur_usd / cur_size
            rem_usd = max(0.0, cur_usd - sell_sz * unit_cost)
        else:
            rem_usd = max(0.0, cur_usd)

        should_close = bool(close_position or rem_size <= 1e-9)
        now = _now_iso()
        if should_close:
            self.conn.execute(
                "UPDATE ct_trades SET "
                "exit_status=?, exit_price=?, exit_usd=?, exit_at=?, profit=?, "
                "our_size=?, our_usd=?, updated_at=? "
                "WHERE id=?",
                ("exited", exit_price, new_exit_usd, now, new_profit, 0.0, 0.0, now, trade_id),
            )
        else:
            self.conn.execute(
                "UPDATE ct_trades SET "
                "exit_status=?, exit_price=?, exit_usd=?, profit=?, "
                "our_size=?, our_usd=?, updated_at=? "
                "WHERE id=?",
                ("open", exit_price, new_exit_usd, new_profit, rem_size, rem_usd, now, trade_id),
            )
        self.conn.commit()

    def insert_exit_order(self, order: Dict[str, Any]) -> int:
        now = _now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ct_exit_orders("
            "trade_id, account_name, reason, order_id, token_id, side, "
            "requested_price, requested_size, requested_usd, "
            "status, exchange_order_status, filled_size_actual, filled_usd_actual, "
            "filled_price_actual, last_error, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(order.get("trade_id") or 0),
                order.get("account_name", "default"),
                order.get("reason"),
                order.get("order_id"),
                order.get("token_id") or "",
                order.get("side") or "",
                order.get("requested_price"),
                order.get("requested_size"),
                order.get("requested_usd"),
                order.get("status", "submitted"),
                order.get("exchange_order_status"),
                order.get("filled_size_actual"),
                order.get("filled_usd_actual"),
                order.get("filled_price_actual"),
                order.get("last_error"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def has_open_exit_order(self, trade_id: int, account_name: str = "default") -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM ct_exit_orders "
            f"WHERE trade_id=? AND account_name=? AND {OPEN_ORDER_STATUS_SQL} "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested') "
            "LIMIT 1",
            (trade_id, account_name),
        ).fetchone()
        return row is not None

    def get_recent_exit_orders_for_verification(
        self, account_name: str = "default", hours: int = 24, limit: int = 50
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT id, trade_id, order_id, reason, token_id, requested_price, requested_size, "
            "requested_usd, status, exchange_order_status, filled_size_actual, filled_usd_actual, "
            "filled_price_actual, created_at "
            "FROM ct_exit_orders "
            "WHERE account_name=? AND order_id IS NOT NULL AND order_id != '' "
            "  AND created_at >= ? "
            "  AND (status='submitted' OR exchange_order_status IN ('submitted', 'live')) "
            "ORDER BY created_at DESC LIMIT ?",
            (account_name, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def reconcile_exit_order_state(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        exchange_order_status: str,
        matched_size: Optional[float] = None,
        fill_price: Optional[float] = None,
        fee_rate: float = 0.0,
        last_error: Optional[str] = None,
        apply_trade_fill: bool = True,
    ) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT eo.id, eo.trade_id, eo.requested_size, eo.requested_price, eo.filled_size_actual, "
            "eo.filled_usd_actual, eo.filled_price_actual, t.our_price "
            "FROM ct_exit_orders eo "
            "JOIN ct_trades t ON t.id = eo.trade_id "
            "WHERE eo.order_id=? AND eo.account_name=? LIMIT 1",
            (order_id, account_name),
        ).fetchone()
        if row is None:
            return {
                "updated": False,
                "status": None,
                "exchange_order_status": None,
                "delta_size": 0.0,
                "delta_usd": 0.0,
            }

        prev_size = max(0.0, float(row["filled_size_actual"] or 0.0))
        prev_usd = max(0.0, float(row["filled_usd_actual"] or 0.0))
        req_size = max(0.0, float(row["requested_size"] or 0.0))
        matched = max(0.0, float(matched_size or 0.0))
        if matched < prev_size:
            matched = prev_size

        actual_price = fill_price
        if actual_price is None or actual_price <= 0:
            for candidate in (
                row["filled_price_actual"],
                row["requested_price"],
            ):
                if isinstance(candidate, (int, float)) and candidate > 0:
                    actual_price = float(candidate)
                    break
        if actual_price is not None and actual_price <= 0:
            actual_price = None

        actual_usd = matched * actual_price if (matched > 0 and actual_price is not None) else 0.0
        delta_size = max(0.0, matched - prev_size)
        delta_usd = max(0.0, actual_usd - prev_usd)
        status_key = str(exchange_order_status or "").lower()

        status, _ = classify_order_fill_status(
            status_key,
            matched,
            req_size,
            unfilled_terminal_status="exchange",
        )

        entry_price = row["our_price"]
        profit_delta = None
        if delta_size > 0 and isinstance(entry_price, (int, float)):
            profit_delta = delta_usd - (float(entry_price) * delta_size)
            if fee_rate > 0 and delta_usd > 0:
                profit_delta -= fee_rate * delta_usd

        if apply_trade_fill and delta_size > 0:
            delta_exit_price = actual_price
            if (delta_exit_price is None or delta_exit_price <= 0) and delta_usd > 0:
                delta_exit_price = delta_usd / delta_size
            self.apply_exit_fill(
                trade_id=int(row["trade_id"]),
                sold_size=delta_size,
                exit_price=delta_exit_price,
                sold_usd=delta_usd,
                profit_delta=profit_delta,
                close_position=False,
            )

        self.conn.execute(
            "UPDATE ct_exit_orders SET "
            "status=?, exchange_order_status=?, filled_size_actual=?, filled_usd_actual=?, "
            "filled_price_actual=?, last_error=?, updated_at=? "
            "WHERE id=?",
            (
                status,
                status_key or None,
                matched if matched > 0 else None,
                actual_usd if matched > 0 else None,
                actual_price if matched > 0 else None,
                last_error,
                _now_iso(),
                int(row["id"]),
            ),
        )
        self.conn.commit()
        return {
            "updated": True,
            "trade_id": int(row["trade_id"]),
            "status": status,
            "exchange_order_status": status_key or None,
            "delta_size": delta_size,
            "delta_usd": delta_usd,
            "matched_size": matched,
            "actual_price": actual_price,
            "profit_delta": profit_delta,
        }

    def record_exit_order_sync_failure(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        last_error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE ct_exit_orders SET last_error=?, updated_at=? "
            "WHERE order_id=? AND account_name=?",
            (
                last_error,
                _now_iso(),
                str(order_id or "").strip(),
                account_name,
            ),
        )
        self.conn.commit()

    def insert_auto_tp_lot(self, lot: Dict[str, Any]) -> int:
        now = _now_iso()
        cur = self.conn.execute(
            "INSERT INTO ct_auto_tp_lots("
            "account_name, root_trade_id, parent_lot_id, leader_address, token_id, condition_id, market_slug, "
            "outcome, entry_price, original_size, remaining_size, tp_target_size, tp_filled_size, "
            "pending_rebuy_size, status, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                lot.get("account_name", "default"),
                int(lot.get("root_trade_id") or 0),
                lot.get("parent_lot_id"),
                str(lot.get("leader_address") or "").lower(),
                str(lot.get("token_id") or ""),
                lot.get("condition_id"),
                lot.get("market_slug"),
                lot.get("outcome"),
                lot.get("entry_price"),
                lot.get("original_size"),
                lot.get("remaining_size"),
                lot.get("tp_target_size", 0.0),
                lot.get("tp_filled_size", 0.0),
                lot.get("pending_rebuy_size", 0.0),
                lot.get("status", "open"),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def get_auto_tp_lot(self, lot_id: int, *, account_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM ct_auto_tp_lots WHERE id=?"
        params: List[Any] = [int(lot_id)]
        if account_name:
            sql += " AND account_name=?"
            params.append(account_name)
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def get_auto_tp_lots_for_trade(
        self,
        root_trade_id: int,
        *,
        account_name: str = "default",
        include_closed: bool = True,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ct_auto_tp_lots WHERE root_trade_id=? AND account_name=?"
        params: List[Any] = [int(root_trade_id), account_name]
        if not include_closed:
            sql += " AND status NOT IN ('closed', 'leader_closed', 'orderbook_unavailable', 'balance_unavailable')"
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def get_auto_tp_lots_for_group(
        self,
        *,
        account_name: str,
        leader_address: str,
        token_id: str,
        include_closed: bool = True,
    ) -> List[Dict[str, Any]]:
        sql = (
            "SELECT * FROM ct_auto_tp_lots WHERE account_name=? AND leader_address=? AND token_id=?"
        )
        params: List[Any] = [account_name, str(leader_address or "").lower(), str(token_id or "")]
        if not include_closed:
            sql += " AND status NOT IN ('closed', 'leader_closed', 'orderbook_unavailable', 'balance_unavailable')"
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def update_auto_tp_lot(self, lot_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ["updated_at=?"]
        vals: List[Any] = [_now_iso()]
        for key, value in fields.items():
            sets.append(f"{key}=?")
            vals.append(value)
        vals.append(int(lot_id))
        self.conn.execute(
            f"UPDATE ct_auto_tp_lots SET {', '.join(sets)} WHERE id=?",
            tuple(vals),
        )
        self.conn.commit()

    def insert_auto_tp_order(self, order: Dict[str, Any]) -> int:
        now = _now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ct_auto_tp_orders("
            "lot_id, root_trade_id, account_name, kind, order_id, side, requested_price, requested_size, "
            "requested_usd, status, exchange_order_status, filled_size_actual, filled_usd_actual, "
            "filled_price_actual, last_error, last_sync_ok_at, last_sync_source, sync_error_count, "
            "created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(order.get("lot_id") or 0),
                int(order.get("root_trade_id") or 0),
                order.get("account_name", "default"),
                order.get("kind") or "",
                order.get("order_id"),
                order.get("side") or "",
                order.get("requested_price"),
                order.get("requested_size"),
                order.get("requested_usd"),
                order.get("status", "submitted"),
                order.get("exchange_order_status"),
                order.get("filled_size_actual"),
                order.get("filled_usd_actual"),
                order.get("filled_price_actual"),
                order.get("last_error"),
                order.get("last_sync_ok_at"),
                order.get("last_sync_source"),
                order.get("sync_error_count", 0),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def get_open_auto_tp_orders_for_lot(
        self,
        lot_id: int,
        *,
        account_name: str = "default",
        kind: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = (
            f"SELECT * FROM ct_auto_tp_orders WHERE lot_id=? AND account_name=? AND {OPEN_ORDER_STATUS_SQL} "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')"
        )
        params: List[Any] = [int(lot_id), account_name]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY id ASC"
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def has_open_auto_tp_order(
        self,
        lot_id: int,
        *,
        account_name: str = "default",
        kind: Optional[str] = None,
    ) -> bool:
        sql = (
            f"SELECT 1 FROM ct_auto_tp_orders WHERE lot_id=? AND account_name=? AND {OPEN_ORDER_STATUS_SQL} "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')"
        )
        params: List[Any] = [int(lot_id), account_name]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " LIMIT 1"
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return row is not None

    def get_open_auto_tp_orders_for_group(
        self,
        *,
        account_name: str,
        leader_address: str,
        token_id: str,
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT ao.*, lot.status AS lot_status "
            "FROM ct_auto_tp_orders ao "
            "JOIN ct_auto_tp_lots lot ON lot.id = ao.lot_id "
            "WHERE ao.account_name=? AND ao.status IN ('submitted','partially_filled') "
            "AND COALESCE(ao.exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested') "
            "AND lot.account_name=? AND lot.leader_address=? AND lot.token_id=? "
            "ORDER BY ao.id ASC",
            (
                account_name,
                account_name,
                str(leader_address or "").lower(),
                str(token_id or ""),
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_auto_tp_order_status(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        status: str,
        exchange_order_status: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE ct_auto_tp_orders SET status=?, exchange_order_status=?, last_error=?, updated_at=? "
            "WHERE order_id=? AND account_name=?",
            (
                status,
                exchange_order_status,
                last_error,
                _now_iso(),
                str(order_id or "").strip(),
                account_name,
            ),
        )
        self.conn.commit()

    def record_auto_tp_order_sync_failure(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        last_error: Optional[str] = None,
    ) -> None:
        self._record_auto_tp_sync_failure(
            table="ct_auto_tp_orders",
            order_id=order_id,
            account_name=account_name,
            last_error=last_error,
        )

    def get_recent_auto_tp_orders_for_verification(
        self,
        *,
        account_name: str = "default",
        hours: int = 24,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        retry_5m_cutoff, retry_15m_cutoff, retry_1h_cutoff = _auto_tp_sync_retry_cutoffs()
        rows = self.conn.execute(
            "SELECT id, lot_id, root_trade_id, order_id, kind, side, requested_price, requested_size, "
            "requested_usd, status, exchange_order_status, filled_size_actual, filled_usd_actual, "
            "filled_price_actual, created_at, updated_at, sync_error_count "
            "FROM ct_auto_tp_orders "
            "WHERE account_name=? AND order_id IS NOT NULL AND order_id != '' "
            "AND created_at >= ? "
            "AND status IN ('submitted', 'partially_filled') "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested', 'sync_error') "
            "AND (COALESCE(sync_error_count, 0) < 3 "
            "OR (COALESCE(sync_error_count, 0) < 6 AND updated_at <= ?) "
            "OR (COALESCE(sync_error_count, 0) < 10 AND updated_at <= ?) "
            "OR updated_at <= ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (account_name, cutoff, retry_5m_cutoff, retry_15m_cutoff, retry_1h_cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def reconcile_auto_tp_order_state(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        exchange_order_status: str,
        matched_size: Optional[float] = None,
        fill_price: Optional[float] = None,
        last_error: Optional[str] = None,
        sync_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, lot_id, root_trade_id, kind, requested_price, requested_size, filled_size_actual, filled_usd_actual, "
            "filled_price_actual, sync_error_count "
            "FROM ct_auto_tp_orders WHERE order_id=? AND account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        if row is None:
            return {
                "updated": False,
                "status": None,
                "exchange_order_status": None,
                "delta_size": 0.0,
                "delta_usd": 0.0,
            }

        prev_size = max(0.0, float(row["filled_size_actual"] or 0.0))
        prev_usd = max(0.0, float(row["filled_usd_actual"] or 0.0))
        matched = max(0.0, float(matched_size or 0.0))
        if matched < prev_size:
            matched = prev_size

        actual_price = fill_price
        if actual_price is None or actual_price <= 0:
            for candidate in (row["filled_price_actual"], row["requested_price"]):
                if isinstance(candidate, (int, float)) and candidate > 0:
                    actual_price = float(candidate)
                    break
        if actual_price is not None and actual_price <= 0:
            actual_price = None

        actual_usd = matched * actual_price if (matched > 0 and actual_price is not None) else 0.0
        delta_size = max(0.0, matched - prev_size)
        delta_usd = max(0.0, actual_usd - prev_usd)
        status_key = str(exchange_order_status or "").lower()

        status, _ = classify_order_fill_status(
            status_key,
            matched,
            row["requested_size"],
            unfilled_terminal_status="exchange",
        )

        sync_ok_at = _now_iso()
        self.conn.execute(
            "UPDATE ct_auto_tp_orders SET "
            "status=?, exchange_order_status=?, filled_size_actual=?, filled_usd_actual=?, "
            "filled_price_actual=?, last_error=?, last_sync_ok_at=?, last_sync_source=?, "
            "sync_error_count=(CASE WHEN sync_error_count IS NULL THEN NULL ELSE 0 END), updated_at=? "
            "WHERE id=?",
            (
                status,
                status_key or None,
                matched if matched > 0 else None,
                actual_usd if matched > 0 else None,
                actual_price if matched > 0 else None,
                last_error,
                sync_ok_at,
                str(sync_source or "").strip().lower() or None,
                sync_ok_at,
                int(row["id"]),
            ),
        )
        self.conn.commit()
        return {
            "updated": True,
            "lot_id": int(row["lot_id"]),
            "root_trade_id": int(row["root_trade_id"]),
            "kind": str(row["kind"] or ""),
            "status": status,
            "exchange_order_status": status_key or None,
            "delta_size": delta_size,
            "delta_usd": delta_usd,
            "matched_size": matched,
            "actual_price": actual_price,
        }

    def insert_auto_tp_bucket_order(self, order: Dict[str, Any]) -> int:
        now = _now_iso()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO ct_auto_tp_bucket_orders("
            "account_name, leader_address, token_id, condition_id, market_slug, outcome, "
            "kind, side, bucket_price, requested_size, requested_usd, order_id, status, "
            "exchange_order_status, filled_size_actual, filled_usd_actual, filled_price_actual, "
            "last_error, last_sync_ok_at, last_sync_source, sync_error_count, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.get("account_name", "default"),
                str(order.get("leader_address") or "").lower(),
                str(order.get("token_id") or ""),
                order.get("condition_id"),
                order.get("market_slug"),
                order.get("outcome"),
                order.get("kind") or "",
                order.get("side") or "",
                order.get("bucket_price"),
                order.get("requested_size"),
                order.get("requested_usd"),
                order.get("order_id"),
                order.get("status", "submitted"),
                order.get("exchange_order_status"),
                order.get("filled_size_actual"),
                order.get("filled_usd_actual"),
                order.get("filled_price_actual"),
                order.get("last_error"),
                order.get("last_sync_ok_at"),
                order.get("last_sync_source"),
                order.get("sync_error_count", 0),
                now,
                now,
            ),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def insert_auto_tp_bucket_order_lots(
        self,
        bucket_order_id: int,
        rows: List[Dict[str, Any]],
    ) -> None:
        if not rows:
            return
        now = _now_iso()
        self.conn.executemany(
            "INSERT INTO ct_auto_tp_bucket_order_lots("
            "bucket_order_id, lot_id, root_trade_id, account_name, requested_size, "
            "filled_size_allocated, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    int(bucket_order_id),
                    int(row.get("lot_id") or 0),
                    int(row.get("root_trade_id") or 0),
                    row.get("account_name", "default"),
                    float(row.get("requested_size") or 0.0),
                    float(row.get("filled_size_allocated") or 0.0),
                    now,
                    now,
                )
                for row in rows
            ],
        )
        self.conn.commit()

    def get_open_auto_tp_bucket_orders_for_group(
        self,
        *,
        account_name: str,
        leader_address: str,
        token_id: str,
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_auto_tp_bucket_orders "
            "WHERE account_name=? AND leader_address=? AND token_id=? AND status IN ('submitted','partially_filled') "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested') "
            "ORDER BY id ASC",
            (
                account_name,
                str(leader_address or "").lower(),
                str(token_id or ""),
            ),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_auto_tp_bucket_order_status(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        status: str,
        exchange_order_status: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE ct_auto_tp_bucket_orders "
            "SET status=?, exchange_order_status=?, last_error=?, updated_at=? "
            "WHERE order_id=? AND account_name=?",
            (
                status,
                exchange_order_status,
                last_error,
                _now_iso(),
                str(order_id or "").strip(),
                account_name,
            ),
        )
        self.conn.commit()

    def record_auto_tp_bucket_order_sync_failure(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        last_error: Optional[str] = None,
    ) -> None:
        self._record_auto_tp_sync_failure(
            table="ct_auto_tp_bucket_orders",
            order_id=order_id,
            account_name=account_name,
            last_error=last_error,
        )

    def _record_auto_tp_sync_failure(
        self,
        *,
        table: str,
        order_id: str,
        account_name: str,
        last_error: Optional[str],
    ) -> None:
        row = self.conn.execute(
            f"SELECT id, created_at, last_sync_ok_at, sync_error_count "
            f"FROM {table} WHERE order_id=? AND account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        if row is None:
            return

        now = datetime.now(timezone.utc)
        count_value = row["sync_error_count"]
        next_count = None
        if count_value is not None:
            next_count = max(0, int(count_value or 0)) + 1

        stale_ref = _parse_iso_utc(row["last_sync_ok_at"]) or _parse_iso_utc(row["created_at"])
        should_mark_sync_error = bool(
            next_count is not None
            and next_count >= AUTO_TP_SYNC_ERROR_MARK_COUNT
            and stale_ref is not None
            and (now - stale_ref) >= timedelta(minutes=10)
        )
        exchange_status = "sync_error" if should_mark_sync_error else None

        self.conn.execute(
            f"UPDATE {table} SET last_error=?, "
            f"exchange_order_status=COALESCE(?, exchange_order_status), "
            f"sync_error_count=(CASE WHEN sync_error_count IS NULL THEN NULL ELSE ? END), "
            f"updated_at=? WHERE id=?",
            (
                last_error,
                exchange_status,
                next_count,
                _now_iso(),
                int(row["id"]),
            ),
        )
        self.conn.commit()

    def get_recent_auto_tp_bucket_orders_for_verification(
        self,
        *,
        account_name: str = "default",
        hours: int = 24,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        retry_5m_cutoff, retry_15m_cutoff, retry_1h_cutoff = _auto_tp_sync_retry_cutoffs()
        rows = self.conn.execute(
            "SELECT id, account_name, leader_address, token_id, kind, side, bucket_price, "
            "requested_size, requested_usd, order_id, status, exchange_order_status, "
            "filled_size_actual, filled_usd_actual, filled_price_actual, created_at, updated_at, sync_error_count "
            "FROM ct_auto_tp_bucket_orders "
            "WHERE account_name=? AND order_id IS NOT NULL AND order_id != '' "
            "AND created_at >= ? "
            "AND status IN ('submitted', 'partially_filled') "
            "AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested', 'sync_error') "
            "AND (COALESCE(sync_error_count, 0) < 3 "
            "OR (COALESCE(sync_error_count, 0) < 6 AND updated_at <= ?) "
            "OR (COALESCE(sync_error_count, 0) < 10 AND updated_at <= ?) "
            "OR updated_at <= ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (account_name, cutoff, retry_5m_cutoff, retry_15m_cutoff, retry_1h_cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def reconcile_auto_tp_bucket_order_state(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        exchange_order_status: str,
        matched_size: Optional[float] = None,
        fill_price: Optional[float] = None,
        last_error: Optional[str] = None,
        sync_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, kind, side, bucket_price, requested_size, filled_size_actual, "
            "filled_usd_actual, filled_price_actual, sync_error_count "
            "FROM ct_auto_tp_bucket_orders WHERE order_id=? AND account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        if row is None:
            return {
                "updated": False,
                "status": None,
                "exchange_order_status": None,
                "delta_size": 0.0,
                "delta_usd": 0.0,
            }

        prev_size = max(0.0, float(row["filled_size_actual"] or 0.0))
        prev_usd = max(0.0, float(row["filled_usd_actual"] or 0.0))
        matched = max(0.0, float(matched_size or 0.0))
        if matched < prev_size:
            matched = prev_size

        actual_price = fill_price
        if actual_price is None or actual_price <= 0:
            for candidate in (row["filled_price_actual"], row["bucket_price"]):
                if isinstance(candidate, (int, float)) and candidate > 0:
                    actual_price = float(candidate)
                    break
        if actual_price is not None and actual_price <= 0:
            actual_price = None

        actual_usd = matched * actual_price if (matched > 0 and actual_price is not None) else 0.0
        delta_size = max(0.0, matched - prev_size)
        delta_usd = max(0.0, actual_usd - prev_usd)
        status_key = str(exchange_order_status or "").lower()

        status, _ = classify_order_fill_status(
            status_key,
            matched,
            row["requested_size"],
            unfilled_terminal_status="exchange",
        )

        sync_ok_at = _now_iso()
        self.conn.execute(
            "UPDATE ct_auto_tp_bucket_orders SET "
            "status=?, exchange_order_status=?, filled_size_actual=?, filled_usd_actual=?, "
            "filled_price_actual=?, last_error=?, last_sync_ok_at=?, last_sync_source=?, "
            "sync_error_count=(CASE WHEN sync_error_count IS NULL THEN NULL ELSE 0 END), updated_at=? "
            "WHERE id=?",
            (
                status,
                status_key or None,
                matched if matched > 0 else None,
                actual_usd if matched > 0 else None,
                actual_price if matched > 0 else None,
                last_error,
                sync_ok_at,
                str(sync_source or "").strip().lower() or None,
                sync_ok_at,
                int(row["id"]),
            ),
        )
        self.conn.commit()
        return {
            "updated": True,
            "bucket_order_id": int(row["id"]),
            "kind": str(row["kind"] or ""),
            "side": str(row["side"] or ""),
            "bucket_price": float(row["bucket_price"] or 0.0),
            "requested_size": float(row["requested_size"] or 0.0),
            "status": status,
            "exchange_order_status": status_key or None,
            "delta_size": delta_size,
            "delta_usd": delta_usd,
            "matched_size": matched,
            "actual_price": actual_price,
        }

    def get_auto_tp_bucket_order_lot_rows(
        self,
        bucket_order_id: int,
        *,
        account_name: str = "default",
    ) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_auto_tp_bucket_order_lots "
            "WHERE bucket_order_id=? AND account_name=? ORDER BY id ASC",
            (int(bucket_order_id), account_name),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_auto_tp_bucket_order_lot_fill(
        self,
        bucket_order_lot_id: int,
        delta_size: float,
    ) -> None:
        self.conn.execute(
            "UPDATE ct_auto_tp_bucket_order_lots "
            "SET filled_size_allocated = COALESCE(filled_size_allocated, 0) + ?, updated_at=? "
            "WHERE id=?",
            (
                float(delta_size or 0.0),
                _now_iso(),
                int(bucket_order_lot_id),
            ),
        )
        self.conn.commit()

    def get_open_trades(self, token_id: Optional[str] = None, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        if token_id and account_name:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0) "
                "AND token_id=? AND account_name=?",
                (token_id, account_name),
            ).fetchall()
        elif token_id:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0) "
                "AND token_id=?",
                (token_id,),
            ).fetchall()
        elif account_name:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0) "
                "AND account_name=?",
                (account_name,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0)"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all_open_trades(self, account_name: Optional[str] = None) -> List[Dict[str, Any]]:
        if account_name:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0) "
                "AND account_name=?",
                (account_name,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT * FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL} "
                "AND (COALESCE(our_size, 0) > 0 OR COALESCE(filled_size_actual, 0) > 0)"
            ).fetchall()
        return [dict(r) for r in rows]

    def has_filled_buy_for_market(self, condition_id: str, account_name: Optional[str] = None) -> bool:
        return self._has_buy_for_market(
            condition_id,
            account_name=account_name,
            status_filter=FILLED_ORDER_STATUSES,
            filled_only=True,
        )

    def has_filled_buy_for_market_by_leader(
        self,
        condition_id: str,
        leader_address: str,
        account_name: Optional[str] = None,
        *,
        outcome: Optional[str] = None,
    ) -> bool:
        return self._has_buy_for_market(
            condition_id,
            leader_address=leader_address,
            account_name=account_name,
            outcome=outcome,
            status_filter=FILLED_ORDER_STATUSES,
            filled_only=True,
        )

    def has_buy_attempt_for_market(
        self,
        condition_id: str,
        account_name: Optional[str] = None,
        *,
        outcome: Optional[str] = None,
    ) -> bool:
        return self._has_buy_for_market(
            condition_id,
            account_name=account_name,
            outcome=outcome,
            status_filter=("submitted", *FILLED_ORDER_STATUSES),
            filled_only=False,
        )

    def has_buy_attempt_for_market_by_leader(
        self,
        condition_id: str,
        leader_address: str,
        account_name: Optional[str] = None,
        *,
        outcome: Optional[str] = None,
    ) -> bool:
        return self._has_buy_for_market(
            condition_id,
            leader_address=leader_address,
            account_name=account_name,
            outcome=outcome,
            status_filter=("submitted", *FILLED_ORDER_STATUSES),
            filled_only=False,
        )

    def count_buy_entries_for_market_by_leader(
        self,
        condition_id: str,
        leader_address: str,
        account_name: Optional[str] = None,
        *,
        outcome: Optional[str] = None,
    ) -> int:
        sql, params = self._build_market_buy_scope_sql(
            "SELECT COUNT(*) as cnt FROM ct_trades",
            condition_id,
            leader_address=leader_address,
            account_name=account_name,
            outcome=outcome,
            status_filter=("submitted", *FILLED_ORDER_STATUSES),
            filled_only=False,
        )
        row = self.conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    def has_buy_attempt_for_token(
        self,
        token_id: str,
        account_name: Optional[str] = None,
        *,
        leader_address: Optional[str] = None,
    ) -> bool:
        return self._has_buy_for_token(
            token_id,
            leader_address=leader_address,
            account_name=account_name,
            status_filter=("submitted", "partially_filled", "filled"),
            filled_only=False,
        )

    def count_buy_entries_for_token_by_leader(
        self,
        token_id: str,
        leader_address: str,
        account_name: Optional[str] = None,
    ) -> int:
        sql, params = self._build_token_buy_scope_sql(
            "SELECT COUNT(*) as cnt FROM ct_trades",
            token_id,
            leader_address=leader_address,
            account_name=account_name,
            status_filter=("submitted", "partially_filled", "filled"),
            filled_only=False,
        )
        row = self.conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row else 0

    @staticmethod
    def _normalize_outcome_scope(outcome: Optional[str]) -> str:
        return str(outcome or "").strip().lower()

    def _build_market_buy_scope_sql(
        self,
        base_sql: str,
        condition_id: str,
        *,
        leader_address: Optional[str] = None,
        account_name: Optional[str] = None,
        outcome: Optional[str] = None,
        status_filter: Tuple[str, ...],
        filled_only: bool,
    ) -> Tuple[str, Tuple[Any, ...]]:
        clauses = ["condition_id=?", "our_side='BUY'"]
        params: List[Any] = [condition_id]

        if leader_address:
            clauses.append("leader_address=?")
            params.append(leader_address.lower())

        normalized_outcome = self._normalize_outcome_scope(outcome)
        if normalized_outcome:
            clauses.append("LOWER(COALESCE(outcome, ''))=?")
            params.append(normalized_outcome)

        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_filter)

        if filled_only:
            clauses.append("COALESCE(our_size, 0) > 0")

        if account_name:
            clauses.append("account_name=?")
            params.append(account_name)

        sql = f"{base_sql} WHERE {' AND '.join(clauses)}"
        return sql, tuple(params)

    def _build_token_buy_scope_sql(
        self,
        base_sql: str,
        token_id: str,
        *,
        leader_address: Optional[str] = None,
        account_name: Optional[str] = None,
        status_filter: Tuple[str, ...],
        filled_only: bool,
    ) -> Tuple[str, Tuple[Any, ...]]:
        clauses = ["token_id=?", "our_side='BUY'"]
        params: List[Any] = [str(token_id or "")]

        if leader_address:
            clauses.append("leader_address=?")
            params.append(leader_address.lower())

        if status_filter:
            placeholders = ",".join("?" * len(status_filter))
            clauses.append(f"status IN ({placeholders})")
            params.extend(status_filter)

        if filled_only:
            clauses.append("COALESCE(our_size, 0) > 0")

        if account_name:
            clauses.append("account_name=?")
            params.append(account_name)

        sql = f"{base_sql} WHERE {' AND '.join(clauses)}"
        return sql, tuple(params)

    def _has_buy_for_market(
        self,
        condition_id: str,
        *,
        leader_address: Optional[str] = None,
        account_name: Optional[str] = None,
        outcome: Optional[str] = None,
        status_filter: Tuple[str, ...],
        filled_only: bool,
    ) -> bool:
        sql, params = self._build_market_buy_scope_sql(
            "SELECT 1 FROM ct_trades",
            condition_id,
            leader_address=leader_address,
            account_name=account_name,
            outcome=outcome,
            status_filter=status_filter,
            filled_only=filled_only,
        )
        row = self.conn.execute(f"{sql} LIMIT 1", params).fetchone()
        return row is not None

    def _has_buy_for_token(
        self,
        token_id: str,
        *,
        leader_address: Optional[str] = None,
        account_name: Optional[str] = None,
        status_filter: Tuple[str, ...],
        filled_only: bool,
    ) -> bool:
        sql, params = self._build_token_buy_scope_sql(
            "SELECT 1 FROM ct_trades",
            token_id,
            leader_address=leader_address,
            account_name=account_name,
            status_filter=status_filter,
            filled_only=filled_only,
        )
        row = self.conn.execute(f"{sql} LIMIT 1", params).fetchone()
        return row is not None

    def find_latest_market_slug(
        self,
        *,
        condition_id: Optional[str] = None,
        token_id: Optional[str] = None,
    ) -> Optional[str]:
        cid = str(condition_id or "").strip().lower()
        tid = str(token_id or "").strip()
        if not cid and not tid:
            return None

        where_parts = ["market_slug IS NOT NULL", "TRIM(market_slug) != ''"]
        params: List[Any] = []
        if cid:
            where_parts.append("LOWER(condition_id)=?")
            params.append(cid)
        if tid:
            where_parts.append("token_id=?")
            params.append(tid)

        where_sql = " AND ".join(where_parts)
        queries = (
            (
                "SELECT market_slug FROM ct_trades "
                f"WHERE {where_sql} "
                "ORDER BY updated_at DESC, created_at DESC LIMIT 1",
                params,
            ),
            (
                "SELECT market_slug FROM ct_leader_activity "
                f"WHERE {where_sql} "
                "ORDER BY ts_epoch DESC, fetched_at DESC LIMIT 1",
                params,
            ),
        )

        for sql, sql_params in queries:
            row = self.conn.execute(sql, tuple(sql_params)).fetchone()
            if row is None:
                continue
            slug = str(row["market_slug"] or "").strip()
            if slug:
                return slug
        return None

    # --- position tracking ---

    def get_position_usd(self, condition_id: str, account_name: Optional[str] = None) -> float:
        if account_name:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(our_usd), 0) as total FROM ct_trades "
                f"WHERE condition_id=? AND {FILLED_TRADE_STATUS_SQL} AND exit_status='open' "
                "AND our_side='BUY' AND COALESCE(our_size, 0) > 0 AND account_name=?",
                (condition_id, account_name),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(our_usd), 0) as total FROM ct_trades "
                f"WHERE condition_id=? AND {FILLED_TRADE_STATUS_SQL} AND exit_status='open' "
                "AND our_side='BUY' AND COALESCE(our_size, 0) > 0",
                (condition_id,),
            ).fetchone()
        return float(row["total"]) if row else 0.0

    # --- daily spend ---

    def get_daily_spend(self, date_key: Optional[str] = None, account_name: str = "default") -> float:
        if date_key is None:
            date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self.conn.execute(
            "SELECT total_usd FROM ct_daily_spend WHERE date_key = ? AND account_name = ?",
            (date_key, account_name),
        ).fetchone()
        return float(row["total_usd"]) if row else 0.0

    def add_daily_spend(self, usd: float, date_key: Optional[str] = None, account_name: str = "default") -> None:
        if date_key is None:
            date_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO ct_daily_spend(date_key, account_name, total_usd, trade_count, updated_at) "
            "VALUES(?, ?, ?, 1, ?) "
            "ON CONFLICT(date_key, account_name) DO UPDATE SET "
            "total_usd = total_usd + excluded.total_usd, "
            "trade_count = trade_count + 1, "
            "updated_at = excluded.updated_at",
            (date_key, account_name, usd, now),
        )
        self.conn.commit()

    # --- attributions ---

    def insert_attribution(self, attr: Dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO ct_attributions("
            "condition_id, leader_address, weight, profit_share, attributed_profit, created_at"
            ") VALUES(?,?,?,?,?,?)",
            (
                attr["condition_id"],
                attr["leader_address"].lower(),
                attr.get("weight"),
                attr.get("profit_share"),
                attr.get("attributed_profit"),
                _now_iso(),
            ),
        )
        self.conn.commit()

    def get_trades_for_condition(self, condition_id: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT * FROM ct_trades WHERE condition_id=? AND {FILLED_TRADE_STATUS_SQL}",
            (condition_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- leader pnl snapshots ---

    def replace_leader_pnl_snapshots(
        self,
        summary_rows: List[Dict[str, Any]],
        market_rows: List[Dict[str, Any]],
    ) -> None:
        now = _now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM ct_leader_summary")
            self.conn.execute("DELETE FROM ct_leader_market_pnl")

            if summary_rows:
                self.conn.executemany(
                    "INSERT INTO ct_leader_summary("
                    "leader_address, account_name, total_realized_pnl, total_unrealized_pnl, total_pnl, "
                    "winning_markets, losing_markets, total_markets, win_rate, updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            (r.get("leader_address") or "").lower(),
                            r.get("account_name") or "default",
                            float(r.get("total_realized_pnl") or 0.0),
                            float(r.get("total_unrealized_pnl") or 0.0),
                            float(r.get("total_pnl") or 0.0),
                            int(r.get("winning_markets") or 0),
                            int(r.get("losing_markets") or 0),
                            int(r.get("total_markets") or 0),
                            r.get("win_rate"),
                            now,
                        )
                        for r in summary_rows
                    ],
                )

            if market_rows:
                self.conn.executemany(
                    "INSERT INTO ct_leader_market_pnl("
                    "leader_address, condition_id, account_name, market_slug, total_realized_pnl, "
                    "total_unrealized_pnl, total_pnl, market_result, updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            (r.get("leader_address") or "").lower(),
                            r.get("condition_id"),
                            r.get("account_name") or "default",
                            r.get("market_slug"),
                            float(r.get("total_realized_pnl") or 0.0),
                            float(r.get("total_unrealized_pnl") or 0.0),
                            float(r.get("total_pnl") or 0.0),
                            r.get("market_result") or "flat",
                            now,
                        )
                        for r in market_rows
                    ],
                )

    def get_leader_summary_rows(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_leader_summary ORDER BY total_pnl DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_leader_market_rows(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_leader_market_pnl ORDER BY total_pnl DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- daily equity / leader pnl ---

    def upsert_daily_equity(self, row: Dict[str, Any]) -> None:
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO ct_daily_equity("
            "date_key, total_equity, total_realized_pnl, total_unrealized_pnl, "
            "total_cost_basis, open_position_count, updated_at"
            ") VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(date_key) DO UPDATE SET "
            "total_equity=excluded.total_equity, total_realized_pnl=excluded.total_realized_pnl, "
            "total_unrealized_pnl=excluded.total_unrealized_pnl, total_cost_basis=excluded.total_cost_basis, "
            "open_position_count=excluded.open_position_count, updated_at=excluded.updated_at",
            (
                row["date_key"],
                float(row.get("total_equity") or 0),
                float(row.get("total_realized_pnl") or 0),
                float(row.get("total_unrealized_pnl") or 0),
                float(row.get("total_cost_basis") or 0),
                int(row.get("open_position_count") or 0),
                now,
            ),
        )
        self.conn.commit()

    def upsert_daily_leader_pnl(self, rows: List[Dict[str, Any]]) -> None:
        now = _now_iso()
        with self.conn:
            for r in rows:
                self.conn.execute(
                    "INSERT INTO ct_daily_leader_pnl("
                    "date_key, leader_address, account_name, realized_pnl, unrealized_pnl, "
                    "total_pnl, market_count, updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(date_key, leader_address, account_name) DO UPDATE SET "
                    "realized_pnl=excluded.realized_pnl, unrealized_pnl=excluded.unrealized_pnl, "
                    "total_pnl=excluded.total_pnl, market_count=excluded.market_count, "
                    "updated_at=excluded.updated_at",
                    (
                        r["date_key"],
                        (r.get("leader_address") or "").lower(),
                        r.get("account_name") or "default",
                        float(r.get("realized_pnl") or 0),
                        float(r.get("unrealized_pnl") or 0),
                        float(r.get("total_pnl") or 0),
                        int(r.get("market_count") or 0),
                        now,
                    ),
                )

    def replace_daily_leader_pnl(self, rows: List[Dict[str, Any]]) -> None:
        now = _now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM ct_daily_leader_pnl")
            if not rows:
                return
            self.conn.executemany(
                "INSERT INTO ct_daily_leader_pnl("
                "date_key, leader_address, account_name, realized_pnl, unrealized_pnl, "
                "total_pnl, market_count, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                [
                    (
                        r["date_key"],
                        (r.get("leader_address") or "").lower(),
                        r.get("account_name") or "default",
                        float(r.get("realized_pnl") or 0.0),
                        float(r.get("unrealized_pnl") or 0.0),
                        float(r.get("total_pnl") or 0.0),
                        int(r.get("market_count") or 0),
                        now,
                    )
                    for r in rows
                ],
            )

    def get_daily_equity_history(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_daily_equity ORDER BY date_key"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_daily_leader_pnl_history(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_daily_leader_pnl ORDER BY date_key, leader_address"
        ).fetchall()
        return [dict(r) for r in rows]

    def replace_daily_leader_market_leg_pnl(self, rows: List[Dict[str, Any]]) -> None:
        now = _now_iso()
        with self.conn:
            self.conn.execute("DELETE FROM ct_daily_leader_market_leg_pnl")
            if not rows:
                return
            self.conn.executemany(
                "INSERT INTO ct_daily_leader_market_leg_pnl("
                "date_key, leader_address, account_name, condition_id, token_id, market_slug, "
                "outcome, buy_fill_count, buy_size, buy_cost_usd, "
                "sell_fill_count, sell_size, sell_proceeds_usd, "
                "settled_size, open_size_eod, close_state_eod, "
                "realized_pnl_delta, unrealized_pnl_delta, total_pnl_delta, "
                "realized_pnl_eod, unrealized_pnl_eod, total_pnl_eod, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["date_key"],
                        (r.get("leader_address") or "").lower(),
                        r.get("account_name") or "default",
                        r.get("condition_id") or "unknown_market",
                        r.get("token_id") or "unknown_token",
                        r.get("market_slug"),
                        r.get("outcome"),
                        int(r.get("buy_fill_count") or 0),
                        float(r.get("buy_size") or 0),
                        float(r.get("buy_cost_usd") or 0),
                        int(r.get("sell_fill_count") or 0),
                        float(r.get("sell_size") or 0),
                        float(r.get("sell_proceeds_usd") or 0),
                        float(r.get("settled_size") or 0),
                        float(r.get("open_size_eod") or 0),
                        r.get("close_state_eod") or "open",
                        float(r.get("realized_pnl_delta") or 0),
                        float(r.get("unrealized_pnl_delta") or 0),
                        float(r.get("total_pnl_delta") or 0),
                        float(r.get("realized_pnl_eod") or 0),
                        float(r.get("unrealized_pnl_eod") or 0),
                        float(r.get("total_pnl_eod") or 0),
                        now,
                    )
                    for r in rows
                ],
            )

    def get_daily_leader_market_leg_pnl_history(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ct_daily_leader_market_leg_pnl "
            "ORDER BY date_key, leader_address, condition_id, token_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- daily compare tables ---

    def get_compare_open_leg_state_rows(
        self,
        date_key: str,
        account_names: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM ct_compare_open_leg_state WHERE date_key=?"
        params: List[Any] = [date_key]
        if account_names:
            placeholders = ",".join("?" * len(account_names))
            sql += f" AND account_name IN ({placeholders})"
            params.extend(account_names)
        sql += " ORDER BY account_name, leader_address, scope_kind, condition_id, token_id"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def replace_compare_open_leg_state(
        self,
        *,
        date_key: str,
        account_names: List[str],
        rows: List[Dict[str, Any]],
    ) -> None:
        now = _now_iso()
        with self.conn:
            for account_name in sorted({str(a or "default") for a in account_names}):
                self.conn.execute(
                    "DELETE FROM ct_compare_open_leg_state WHERE date_key=? AND account_name=?",
                    (date_key, account_name),
                )
            if not rows:
                return
            self.conn.executemany(
                "INSERT INTO ct_compare_open_leg_state("
                "date_key, account_name, leader_address, scope_kind, condition_id, token_id, "
                "market_slug, outcome, bod_open_size, bod_open_cost, bod_avg_open_price, bod_mark_price, "
                "open_size, open_cost, avg_open_price, unrealized_bod, "
                "bod_cumulative_buy_fill_count, bod_cumulative_buy_size, bod_cumulative_buy_usd, "
                "bod_cumulative_sell_fill_count, bod_cumulative_sell_size, bod_cumulative_sell_usd, "
                "cumulative_buy_fill_count, cumulative_buy_size, cumulative_buy_usd, "
                "cumulative_sell_fill_count, cumulative_sell_size, cumulative_sell_usd, "
                "mark_price_now, unrealized_now, realized_pnl, status, exclusion_reason, "
                "settlement_time, last_event_ts, mark_price_source, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["date_key"],
                        r.get("account_name") or "default",
                        (r.get("leader_address") or "").lower(),
                        r.get("scope_kind") or "leader",
                        r.get("condition_id") or "",
                        r.get("token_id") or "",
                        r.get("market_slug"),
                        r.get("outcome"),
                        float(r.get("bod_open_size") or 0),
                        float(r.get("bod_open_cost") or 0),
                        float(r.get("bod_avg_open_price") or 0),
                        r.get("bod_mark_price"),
                        float(r.get("open_size") or 0),
                        float(r.get("open_cost") or 0),
                        float(r.get("avg_open_price") or 0),
                        float(r.get("unrealized_bod") or 0),
                        int(r.get("bod_cumulative_buy_fill_count") or 0),
                        float(r.get("bod_cumulative_buy_size") or 0),
                        float(r.get("bod_cumulative_buy_usd") or 0),
                        int(r.get("bod_cumulative_sell_fill_count") or 0),
                        float(r.get("bod_cumulative_sell_size") or 0),
                        float(r.get("bod_cumulative_sell_usd") or 0),
                        int(r.get("cumulative_buy_fill_count") or 0),
                        float(r.get("cumulative_buy_size") or 0),
                        float(r.get("cumulative_buy_usd") or 0),
                        int(r.get("cumulative_sell_fill_count") or 0),
                        float(r.get("cumulative_sell_size") or 0),
                        float(r.get("cumulative_sell_usd") or 0),
                        r.get("mark_price_now"),
                        float(r.get("unrealized_now") or 0),
                        float(r.get("realized_pnl") or 0),
                        r.get("status") or "open",
                        r.get("exclusion_reason"),
                        r.get("settlement_time"),
                        r.get("last_event_ts"),
                        r.get("mark_price_source"),
                        now,
                    )
                    for r in rows
                ],
            )

    def replace_compare_daily_market_leg(
        self,
        *,
        date_key: str,
        account_names: List[str],
        rows: List[Dict[str, Any]],
    ) -> None:
        now = _now_iso()
        with self.conn:
            for account_name in sorted({str(a or "default") for a in account_names}):
                self.conn.execute(
                    "DELETE FROM ct_compare_daily_market_leg WHERE date_key=? AND account_name=?",
                    (date_key, account_name),
                )
            if not rows:
                return
            self.conn.executemany(
                "INSERT INTO ct_compare_daily_market_leg("
                "date_key, account_name, leader_address, condition_id, token_id, market_slug, outcome, "
                "exclusion_reason, leader_buy_fill_count, leader_buy_usd, leader_buy_avg_price, "
                "leader_sell_fill_count, leader_sell_usd, leader_sell_avg_price, "
                "leader_realized_pnl, leader_unrealized_change, leader_total_pnl, "
                "our_buy_fill_count, our_buy_usd, our_buy_avg_price, "
                "our_sell_fill_count, our_sell_usd, our_sell_avg_price, "
                "our_realized_pnl, our_unrealized_change, our_total_pnl, primary_gap_reason, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["date_key"],
                        r.get("account_name") or "default",
                        (r.get("leader_address") or "").lower(),
                        r.get("condition_id") or "",
                        r.get("token_id") or "",
                        r.get("market_slug"),
                        r.get("outcome"),
                        r.get("exclusion_reason"),
                        int(r.get("leader_buy_fill_count") or 0),
                        float(r.get("leader_buy_usd") or 0),
                        r.get("leader_buy_avg_price"),
                        int(r.get("leader_sell_fill_count") or 0),
                        float(r.get("leader_sell_usd") or 0),
                        r.get("leader_sell_avg_price"),
                        float(r.get("leader_realized_pnl") or 0),
                        float(r.get("leader_unrealized_change") or 0),
                        float(r.get("leader_total_pnl") or 0),
                        int(r.get("our_buy_fill_count") or 0),
                        float(r.get("our_buy_usd") or 0),
                        r.get("our_buy_avg_price"),
                        int(r.get("our_sell_fill_count") or 0),
                        float(r.get("our_sell_usd") or 0),
                        r.get("our_sell_avg_price"),
                        float(r.get("our_realized_pnl") or 0),
                        float(r.get("our_unrealized_change") or 0),
                        float(r.get("our_total_pnl") or 0),
                        r.get("primary_gap_reason") or "none",
                        now,
                    )
                    for r in rows
                ],
            )

    def replace_compare_daily_summary(
        self,
        *,
        date_key: str,
        account_names: List[str],
        rows: List[Dict[str, Any]],
    ) -> None:
        now = _now_iso()
        with self.conn:
            for account_name in sorted({str(a or "default") for a in account_names}):
                self.conn.execute(
                    "DELETE FROM ct_compare_daily_summary WHERE date_key=? AND account_name=?",
                    (date_key, account_name),
                )
            if not rows:
                return
            self.conn.executemany(
                "INSERT INTO ct_compare_daily_summary("
                "date_key, account_name, leader_address, leader_total_pnl, our_total_pnl, delta_pnl, "
                "leader_excluded_pnl, our_excluded_pnl, visible_leader_pnl, visible_our_pnl, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        r["date_key"],
                        r.get("account_name") or "default",
                        (r.get("leader_address") or "").lower(),
                        float(r.get("leader_total_pnl") or 0),
                        float(r.get("our_total_pnl") or 0),
                        float(r.get("delta_pnl") or 0),
                        float(r.get("leader_excluded_pnl") or 0),
                        float(r.get("our_excluded_pnl") or 0),
                        float(r.get("visible_leader_pnl") or 0),
                        float(r.get("visible_our_pnl") or 0),
                        now,
                    )
                    for r in rows
                ],
            )

    def purge_compare_before(self, min_date_key: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM ct_compare_open_leg_state WHERE date_key < ?",
                (min_date_key,),
            )
            self.conn.execute(
                "DELETE FROM ct_compare_daily_market_leg WHERE date_key < ?",
                (min_date_key,),
            )
            self.conn.execute(
                "DELETE FROM ct_compare_daily_summary WHERE date_key < ?",
                (min_date_key,),
            )

    def purge_compare_accounts(self, account_names: List[str]) -> None:
        normalized = sorted({str(a or "default") for a in account_names})
        if not normalized:
            return
        placeholders = ",".join("?" * len(normalized))
        with self.conn:
            self.conn.execute(
                f"DELETE FROM ct_compare_open_leg_state WHERE account_name IN ({placeholders})",
                normalized,
            )
            self.conn.execute(
                f"DELETE FROM ct_compare_daily_market_leg WHERE account_name IN ({placeholders})",
                normalized,
            )
            self.conn.execute(
                f"DELETE FROM ct_compare_daily_summary WHERE account_name IN ({placeholders})",
                normalized,
            )

    # --- resolved price cache ---

    def get_cached_resolution_prices(self, token_ids: List[str]) -> Dict[str, float]:
        """批量获取已缓存的结算价，返回 {token_id: price}."""
        if not token_ids:
            return {}
        out: Dict[str, float] = {}
        for i in range(0, len(token_ids), 500):
            batch = token_ids[i : i + 500]
            placeholders = ",".join("?" * len(batch))
            rows = self.conn.execute(
                f"SELECT token_id, resolution_price FROM ct_resolved_prices WHERE token_id IN ({placeholders})",
                batch,
            ).fetchall()
            for r in rows:
                out[r["token_id"]] = float(r["resolution_price"])
        return out

    def save_resolution_prices(
        self,
        prices: Dict[str, float],
        settlement_times: Optional[Dict[str, str]] = None,
    ) -> None:
        """批量缓存结算价和官方结算时间。"""
        if not prices:
            return
        now = _now_iso()
        settlement_times = settlement_times or {}
        with self.conn:
            self.conn.executemany(
                "INSERT INTO ct_resolved_prices(token_id, resolution_price, settlement_time, cached_at) "
                "VALUES(?,?,?,?) ON CONFLICT(token_id) DO UPDATE SET "
                "resolution_price=excluded.resolution_price, "
                "settlement_time=COALESCE(excluded.settlement_time, ct_resolved_prices.settlement_time), "
                "cached_at=excluded.cached_at",
                [
                    (tid, price, settlement_times.get(tid), now)
                    for tid, price in prices.items()
                ],
            )

    # --- order verification ---

    def get_active_user_order_condition_ids(
        self,
        account_name: str = "default",
        *,
        recent_gtd_hours: int = 12,
    ) -> List[str]:
        recent_gtd_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max(1, int(recent_gtd_hours or 12)))
        )
        gtd_cutoff = max(recent_gtd_cutoff, CLOB_V2_CUTOVER_AT).isoformat()
        v2_cutover = CLOB_V2_CUTOVER_AT.isoformat()
        rows = self.conn.execute(
            """
            SELECT DISTINCT condition_id FROM (
                SELECT condition_id
                FROM ct_trades
                WHERE account_name=?
                  AND condition_id IS NOT NULL AND TRIM(condition_id) != ''
                  AND status IN ('submitted','partially_filled')
                  AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')
                  AND created_at >= ?
                UNION
                SELECT t.condition_id
                FROM ct_exit_orders eo
                JOIN ct_trades t ON t.id = eo.trade_id
                WHERE eo.account_name=?
                  AND t.account_name=?
                  AND t.condition_id IS NOT NULL AND TRIM(t.condition_id) != ''
                  AND eo.status IN ('submitted','partially_filled')
                  AND COALESCE(eo.exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')
                  AND eo.created_at >= ?
                UNION
                SELECT condition_id
                FROM ct_auto_tp_bucket_orders
                WHERE account_name=?
                  AND condition_id IS NOT NULL AND TRIM(condition_id) != ''
                  AND status IN ('submitted','partially_filled')
                  AND COALESCE(exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')
                  AND created_at >= ?
                UNION
                SELECT lot.condition_id
                FROM ct_auto_tp_orders ao
                JOIN ct_auto_tp_lots lot ON lot.id = ao.lot_id
                WHERE ao.account_name=?
                  AND lot.account_name=?
                  AND lot.condition_id IS NOT NULL AND TRIM(lot.condition_id) != ''
                  AND ao.status IN ('submitted','partially_filled')
                  AND COALESCE(ao.exchange_order_status, 'submitted') IN ('submitted', 'live', 'cancel_requested')
                  AND ao.created_at >= ?
            )
            ORDER BY condition_id ASC
            """,
            (
                account_name,
                gtd_cutoff,
                account_name,
                account_name,
                gtd_cutoff,
                account_name,
                v2_cutover,
                account_name,
                account_name,
                v2_cutover,
            ),
        ).fetchall()
        return [str(row["condition_id"] or "").strip().lower() for row in rows if str(row["condition_id"] or "").strip()]

    def get_trade_order_sync_row(
        self,
        order_id: str,
        *,
        account_name: str = "default",
    ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id, condition_id, requested_size, requested_price, our_size, our_usd, "
            "filled_size_actual, filled_usd_actual, exchange_order_status "
            "FROM ct_trades WHERE our_order_id=? AND account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_exit_order_sync_row(
        self,
        order_id: str,
        *,
        account_name: str = "default",
    ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT eo.id, eo.trade_id, eo.reason, eo.requested_size, eo.requested_price, "
            "eo.filled_size_actual, eo.filled_usd_actual, eo.exchange_order_status, "
            "t.condition_id, t.token_id "
            "FROM ct_exit_orders eo "
            "JOIN ct_trades t ON t.id = eo.trade_id "
            "WHERE eo.order_id=? AND eo.account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_auto_tp_order_sync_row(
        self,
        order_id: str,
        *,
        account_name: str = "default",
    ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT ao.id, ao.lot_id, ao.root_trade_id, ao.kind, ao.requested_size, ao.requested_price, "
            "ao.filled_size_actual, ao.filled_usd_actual, ao.exchange_order_status, lot.condition_id "
            "FROM ct_auto_tp_orders ao "
            "JOIN ct_auto_tp_lots lot ON lot.id = ao.lot_id "
            "WHERE ao.order_id=? AND ao.account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_auto_tp_bucket_order_sync_row(
        self,
        order_id: str,
        *,
        account_name: str = "default",
    ) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id, condition_id, requested_size, bucket_price, filled_size_actual, "
            "filled_usd_actual, exchange_order_status, kind "
            "FROM ct_auto_tp_bucket_orders WHERE order_id=? AND account_name=? LIMIT 1",
            (str(order_id or "").strip(), account_name),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_recent_orders_for_verification(
        self, account_name: str = "default", hours: int = 24, limit: int = 50
    ) -> List[Dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT id, our_order_id, condition_id, market_slug, our_usd, requested_size, requested_price, "
            "exchange_order_status, created_at "
            "FROM ct_trades "
            "WHERE account_name=? AND our_order_id IS NOT NULL AND our_order_id != '' "
            "  AND created_at >= ? "
            "  AND (status='submitted' OR exchange_order_status IN ('submitted', 'live')) "
            "ORDER BY created_at DESC LIMIT ?",
            (account_name, cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_trade_status_by_order_id_scoped(
        self, order_id: str, status: str, *, account_name: str, **kwargs: Any
    ) -> None:
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, _now_iso()]
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.extend([order_id, account_name])
        self.conn.execute(
            f"UPDATE ct_trades SET {', '.join(sets)} WHERE our_order_id = ? AND account_name = ?",
            vals,
        )
        self.conn.commit()

    def reconcile_order_state(
        self,
        order_id: str,
        *,
        account_name: str = "default",
        exchange_order_status: str,
        matched_size: Optional[float] = None,
        fill_price: Optional[float] = None,
        skip_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        row = self.conn.execute(
            "SELECT id, requested_size, requested_price, requested_usd, "
            "our_size, our_usd, our_price, filled_size_actual, filled_usd_actual, "
            "our_limit_price, our_filled_price "
            "FROM ct_trades WHERE our_order_id=? AND account_name=? LIMIT 1",
            (order_id, account_name),
        ).fetchone()
        if row is None:
            return {"updated": False, "usd_delta": 0.0, "status": None, "partial_fill_status": None}

        prev_size = max(
            float(row["filled_size_actual"] or 0.0),
            float(row["our_size"] or 0.0),
        )
        prev_usd = max(
            float(row["filled_usd_actual"] or 0.0),
            float(row["our_usd"] or 0.0),
        )
        req_size = float(row["requested_size"] or 0.0)
        req_usd = float(row["requested_usd"] or 0.0)

        matched = max(0.0, float(matched_size or 0.0))
        if matched < prev_size:
            matched = prev_size

        actual_price = fill_price
        if actual_price is None or actual_price <= 0:
            prev_avg_price = (prev_usd / prev_size) if prev_size > 0 and prev_usd > 0 else None
            requested_avg_price = (req_usd / req_size) if req_size > 0 and req_usd > 0 else None
            for candidate in (
                prev_avg_price,
                requested_avg_price,
                row["requested_price"],
                row["our_filled_price"],
                row["our_price"],
            ):
                if isinstance(candidate, (int, float)) and candidate > 0:
                    actual_price = float(candidate)
                    break
        if actual_price is not None and actual_price <= 0:
            actual_price = None

        if matched > 0 and req_size > 0 and req_usd > 0 and (fill_price is None or fill_price <= 0):
            actual_usd = matched * (req_usd / req_size)
        else:
            actual_usd = matched * actual_price if (matched > 0 and actual_price is not None) else 0.0
        effective_skip_reason = None
        status_key = str(exchange_order_status or "").lower()
        status, partial_fill_status = classify_order_fill_status(status_key, matched, req_size)
        if partial_fill_status == "partial" and status_key in ("expired", "cancelled"):
            effective_skip_reason = skip_reason or f"partial fill: {matched} of {req_size or 'unknown'}"
        elif partial_fill_status == "unfilled":
            effective_skip_reason = skip_reason or f"order {status_key or 'expired'} on exchange"

        if matched <= 0:
            actual_price = None
            actual_usd = 0.0

        delta_size = max(0.0, matched - prev_size)
        usd_delta = max(0.0, actual_usd - prev_usd)
        self.conn.execute(
            "UPDATE ct_trades SET "
            "status=?, exchange_order_status=?, our_size=?, our_usd=?, our_price=?, "
            "filled_size_actual=?, filled_usd_actual=?, partial_fill_status=?, our_filled_price=?, "
            "skip_reason=?, updated_at=? "
            "WHERE our_order_id=? AND account_name=?",
            (
                status,
                status_key or None,
                matched,
                actual_usd,
                actual_price,
                matched if matched > 0 else None,
                actual_usd if matched > 0 else None,
                partial_fill_status,
                actual_price,
                effective_skip_reason,
                _now_iso(),
                order_id,
                account_name,
            ),
        )
        self.conn.commit()
        return {
            "updated": True,
            "trade_id": int(row["id"]),
            "delta_size": delta_size,
            "usd_delta": usd_delta,
            "status": status,
            "partial_fill_status": partial_fill_status,
            "matched_size": matched,
            "actual_price": actual_price,
        }

    def get_recent_filled_orders(self, hours: int = 24, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近 N 小时内 status='filled' 且有 order_id 的记录，用于验证成交状态."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT id, our_order_id, condition_id, market_slug, our_usd, created_at "
            "FROM ct_trades "
            f"WHERE {FILLED_TRADE_STATUS_SQL} AND our_order_id IS NOT NULL AND our_order_id != '' "
            "  AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_trade_status_by_order_id(self, order_id: str, status: str, **kwargs: Any) -> None:
        """按 order_id 更新订单状态."""
        sets = ["status = ?", "updated_at = ?"]
        vals: list = [status, _now_iso()]
        for k, v in kwargs.items():
            sets.append(f"{k} = ?")
            vals.append(v)
        vals.append(order_id)
        self.conn.execute(
            f"UPDATE ct_trades SET {', '.join(sets)} WHERE our_order_id = ?", vals
        )
        self.conn.commit()

    def count_filled_buys_for_market_by_leader(
        self, condition_id: str, leader_address: str, account_name: Optional[str] = None
    ) -> int:
        """统计某 leader + 某市场已成交的 BUY 次数."""
        if account_name:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM ct_trades "
                "WHERE condition_id=? AND leader_address=? AND our_side='BUY' "
                f"AND {FILLED_TRADE_STATUS_SQL} AND COALESCE(our_size, 0) > 0 AND account_name=?",
                (condition_id, leader_address.lower(), account_name),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM ct_trades "
                "WHERE condition_id=? AND leader_address=? AND our_side='BUY' "
                f"AND {FILLED_TRADE_STATUS_SQL} AND COALESCE(our_size, 0) > 0",
                (condition_id, leader_address.lower()),
            ).fetchone()
        return int(row["cnt"]) if row else 0

    def get_status_summary(self, account_name: Optional[str] = None) -> Dict[str, Any]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        acct_filter = " AND account_name=?" if account_name else ""
        acct_params: tuple = (account_name,) if account_name else ()

        open_trades = self.conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(our_usd), 0) as total "
            f"FROM ct_trades WHERE exit_status='open' AND {FILLED_TRADE_STATUS_SQL}{acct_filter}",
            acct_params,
        ).fetchone()
        today_spend = self.get_daily_spend(today, account_name or "default")
        total_profit = self.conn.execute(
            f"SELECT COALESCE(SUM(profit), 0) as total FROM ct_trades WHERE profit IS NOT NULL{acct_filter}",
            acct_params,
        ).fetchone()
        total_trades = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM ct_trades WHERE {FILLED_TRADE_STATUS_SQL}{acct_filter}",
            acct_params,
        ).fetchone()
        skipped = self.conn.execute(
            f"SELECT COUNT(*) as cnt FROM ct_trades WHERE status='skipped'{acct_filter}",
            acct_params,
        ).fetchone()
        return {
            "open_positions": int(open_trades["cnt"]) if open_trades else 0,
            "open_usd": float(open_trades["total"]) if open_trades else 0.0,
            "today_spend": today_spend,
            "total_profit": float(total_profit["total"]) if total_profit else 0.0,
            "total_filled": int(total_trades["cnt"]) if total_trades else 0,
            "total_skipped": int(skipped["cnt"]) if skipped else 0,
        }

    # --- leader activity cache ---

    def insert_leader_activities(self, rows: List[Dict[str, Any]]) -> int:
        """批量写入 leader activity，返回新增条数."""
        now = _now_iso()
        inserted = 0
        with self.conn:
            for r in rows:
                try:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO ct_leader_activity("
                        "leader_address, tx_hash, timestamp_utc, ts_epoch, side, "
                        "token_id, condition_id, market_slug, outcome, "
                        "price, size, usd, fetched_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            (r.get("leader_address") or "").lower(),
                            r.get("tx_hash"),
                            r.get("timestamp_utc"),
                            int(r.get("ts_epoch") or 0),
                            r.get("side"),
                            r.get("token_id"),
                            r.get("condition_id"),
                            r.get("market_slug"),
                            r.get("outcome"),
                            r.get("price"),
                            r.get("size"),
                            r.get("usd"),
                            now,
                        ),
                    )
                    if self.conn.execute("SELECT changes()").fetchone()[0] > 0:
                        inserted += 1
                except sqlite3.IntegrityError:
                    pass
        return inserted

    def get_leader_activity(
        self, leader_address: str, since_ts: Optional[int] = None, side: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """从本地缓存读取 leader activity."""
        sql = "SELECT * FROM ct_leader_activity WHERE leader_address=?"
        params: list = [leader_address.lower()]
        if since_ts:
            sql += " AND ts_epoch >= ?"
            params.append(since_ts)
        if side:
            sql += " AND side = ?"
            params.append(side)
        sql += " ORDER BY ts_epoch"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_activity_sync_watermark(self, leader_address: str) -> int:
        """获取 leader activity 同步水位."""
        row = self.conn.execute(
            "SELECT max_ts_epoch FROM ct_leader_activity_sync WHERE leader_address=?",
            (leader_address.lower(),),
        ).fetchone()
        return int(row["max_ts_epoch"]) if row else 0

    def update_activity_sync_watermark(self, leader_address: str, max_ts: int, total: int) -> None:
        self.conn.execute(
            "INSERT INTO ct_leader_activity_sync(leader_address, max_ts_epoch, total_records, last_synced_at) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(leader_address) DO UPDATE SET "
            "max_ts_epoch=excluded.max_ts_epoch, total_records=excluded.total_records, "
            "last_synced_at=excluded.last_synced_at",
            (leader_address.lower(), max_ts, total, _now_iso()),
        )
        self.conn.commit()

    # --- config snapshots ---

    def insert_config_snapshot(
        self, leader_address: str, account_name: str,
        config_json: str, reason: str = "startup",
    ) -> None:
        now = _now_iso()
        self.conn.execute(
            "INSERT INTO ct_config_snapshots("
            "leader_address, account_name, snapshot_reason, config_json, effective_from, created_at"
            ") VALUES(?,?,?,?,?,?)",
            (leader_address.lower(), account_name, reason, config_json, now, now),
        )
        self.conn.commit()

    def get_config_at(
        self, leader_address: str, account_name: str, timestamp_iso: str
    ) -> Optional[str]:
        """查询某时刻生效的配置 JSON. 优先 per-leader，fallback 全局 '*'."""
        row = self.conn.execute(
            "SELECT config_json FROM ct_config_snapshots "
            "WHERE leader_address IN (?, '*') AND account_name=? AND effective_from <= ? "
            "ORDER BY CASE WHEN leader_address='*' THEN 1 ELSE 0 END, effective_from DESC "
            "LIMIT 1",
            (leader_address.lower(), account_name, timestamp_iso),
        ).fetchone()
        return row["config_json"] if row else None

    def get_latest_config_snapshot(self, leader_address: str, account_name: str) -> Optional[str]:
        """获取最新的配置快照 JSON."""
        row = self.conn.execute(
            "SELECT config_json FROM ct_config_snapshots "
            "WHERE leader_address=? AND account_name=? "
            "ORDER BY effective_from DESC LIMIT 1",
            (leader_address.lower(), account_name),
        ).fetchone()
        return row["config_json"] if row else None

