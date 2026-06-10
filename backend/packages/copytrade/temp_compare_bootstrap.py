from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGES_DIR = _PACKAGE_DIR.parent
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

import copytrade.build_leader_pnl_snapshot as snapshot
from copytrade.db import CopyTradeDB
from copytrade.paths import DEFAULT_DB_PATH, PACKAGE_DIR


LIVE_DB = DEFAULT_DB_PATH
TEMP_DB = PACKAGE_DIR / "copytrade.compare-rebuild.sqlite"
ACCOUNT_NAMES = ["main", "pm-2"]


def _copy_compare_rows(temp_db: CopyTradeDB, live_db: CopyTradeDB, date_key: str) -> None:
    open_rows = [
        dict(row)
        for row in temp_db.conn.execute(
            "SELECT * FROM ct_compare_open_leg_state WHERE date_key=? ORDER BY account_name, leader_address, scope_kind, condition_id, token_id",
            (date_key,),
        ).fetchall()
    ]
    market_rows = [
        dict(row)
        for row in temp_db.conn.execute(
            "SELECT * FROM ct_compare_daily_market_leg WHERE date_key=? ORDER BY account_name, leader_address, condition_id, token_id",
            (date_key,),
        ).fetchall()
    ]
    summary_rows = [
        dict(row)
        for row in temp_db.conn.execute(
            "SELECT * FROM ct_compare_daily_summary WHERE date_key=? ORDER BY account_name, leader_address",
            (date_key,),
        ).fetchall()
    ]
    live_db.replace_compare_open_leg_state(date_key=date_key, account_names=ACCOUNT_NAMES, rows=open_rows)
    live_db.replace_compare_daily_market_leg(date_key=date_key, account_names=ACCOUNT_NAMES, rows=market_rows)
    live_db.replace_compare_daily_summary(date_key=date_key, account_names=ACCOUNT_NAMES, rows=summary_rows)
    snapshot._ensure_ct_meta_table(live_db)
    snapshot._upsert_ct_meta(live_db, "daily_compare_mode", snapshot._DAILY_COMPARE_MODE)


def main() -> None:
    now = datetime.now(timezone.utc)
    date_key = snapshot._compare_date_key(now)

    temp_db = CopyTradeDB(str(TEMP_DB))
    try:
        stats = snapshot.build_daily_compare(
            temp_db,
            account_names=ACCOUNT_NAMES,
            now=now,
            sync_leader_activity=False,
        )
        print({"phase": "build_daily_compare", "stats": stats, "date_key": date_key}, flush=True)
        snapshot.sync_supabase(str(TEMP_DB), compare_only=True)
        print({"phase": "sync_supabase", "date_key": date_key}, flush=True)

        live_db = CopyTradeDB(str(LIVE_DB))
        try:
            _copy_compare_rows(temp_db, live_db, date_key)
        finally:
            live_db.close()
        print({"phase": "copied_back_to_live", "date_key": date_key}, flush=True)
    finally:
        temp_db.close()


if __name__ == "__main__":
    main()
