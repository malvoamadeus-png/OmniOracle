"""Diagnose activity capture coverage: Data API activity vs ct_trades rows.

Read-only tool:
- Fetch BUY activities for configured leaders
- Build leader_fill_key per activity
- Compare with ct_trades.leader_fill_key by account/leader/time window
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

_PACKAGE_DIR = Path(__file__).resolve().parent
_PACKAGES_DIR = _PACKAGE_DIR.parent
_PROJECT_ROOT = _PACKAGE_DIR.parents[2]
for _path in (str(_PROJECT_ROOT), str(_PACKAGES_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from copytrade.account_config import load_all_accounts
from copytrade.monitor import build_leader_fill_key
from copytrade.paths import DEFAULT_DB_PATH, PACKAGE_DIR, PROJECT_ROOT, ensure_import_paths
from copytrade.polymarket_public_api import DATA_API, extract_trade_fields

ensure_import_paths()
SCRIPT_DIR = str(PACKAGE_DIR)
PROJECT_ROOT = str(PROJECT_ROOT)


@dataclass
class ExpectedFill:
    fill_key: str
    tx_hash: str
    ts_int: int
    usd: float
    condition_id: str
    token_id: str
    slug: str
    side: str
    outcome_index: Optional[int]
    price: Optional[float]
    size: Optional[float]


def _parse_ts_int(ts: Any) -> Optional[int]:
    if isinstance(ts, (int, float)):
        return int(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if s.isdigit():
            n = int(s)
            if n > 10_000_000_000:
                n = n // 1000
            return n
        try:
            return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    return None


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def fetch_expected_fills(
    session: requests.Session,
    leader_address: str,
    start_ts: int,
    end_ts: int,
) -> Tuple[List[ExpectedFill], bool]:
    expected: List[ExpectedFill] = []
    truncated = False
    offset = 0
    limit = 100

    while True:
        params = {
            "user": leader_address,
            "type": "TRADE",
            "limit": limit,
            "offset": offset,
            "startTs": start_ts,
        }
        resp = session.get(f"{DATA_API}/activity", params=params, timeout=20)
        if resp.status_code != 200:
            txt = resp.text.lower()
            if "max historical activity offset" in txt:
                truncated = True
            break

        data = resp.json()
        if not isinstance(data, list) or not data:
            break

        hit_old = False
        for row in data:
            if not isinstance(row, dict):
                continue
            parsed = extract_trade_fields(row)
            if parsed is None:
                continue
            if parsed.get("side") != "BUY":
                continue
            token_id = parsed.get("token_id")
            if not token_id:
                continue

            ts_int = _parse_ts_int(parsed.get("ts"))
            if ts_int is None:
                continue
            if ts_int < start_ts:
                hit_old = True
                continue
            if ts_int > end_ts:
                continue

            fill_key = build_leader_fill_key(leader_address, parsed)
            expected.append(
                ExpectedFill(
                    fill_key=fill_key,
                    tx_hash=str(parsed.get("tx") or ""),
                    ts_int=ts_int,
                    usd=_as_float(parsed.get("usd"), 0.0),
                    condition_id=str(parsed.get("market") or ""),
                    token_id=str(parsed.get("token_id") or ""),
                    slug=str(parsed.get("slug") or ""),
                    side=str(parsed.get("side") or ""),
                    outcome_index=parsed.get("outcome_index"),
                    price=parsed.get("price"),
                    size=parsed.get("size"),
                )
            )

        if hit_old or len(data) < limit:
            break
        offset += limit

    # Deduplicate exact fill_keys from API side
    uniq: Dict[str, ExpectedFill] = {}
    for item in expected:
        if item.fill_key not in uniq:
            uniq[item.fill_key] = item
    return list(uniq.values()), truncated


def load_actual_fill_keys(
    conn: sqlite3.Connection,
    account_name: str,
    leader_address: str,
    start_iso: str,
    end_iso: str,
) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id,
            leader_fill_key,
            leader_tx_hash,
            leader_usd,
            condition_id,
            token_id,
            market_slug,
            status,
            created_at
        FROM ct_trades
        WHERE account_name=?
          AND leader_address=?
          AND created_at>=?
          AND created_at<=?
        """,
        (account_name, leader_address.lower(), start_iso, end_iso),
    ).fetchall()

    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        fill_key = row["leader_fill_key"] or f"legacy:{row['id']}"
        if fill_key not in out:
            out[fill_key] = dict(row)
    return out


def _iter_selected_accounts(
    include_accounts: Optional[Iterable[str]],
    include_leaders: Optional[Iterable[str]],
):
    include_accounts_set = {a.strip() for a in include_accounts or [] if a.strip()}
    include_leaders_set = {a.strip().lower() for a in include_leaders or [] if a.strip()}

    for acc in load_all_accounts():
        if include_accounts_set and acc.name not in include_accounts_set:
            continue
        leaders = [x.lower() for x in acc.config.leader_addresses]
        if include_leaders_set:
            leaders = [x for x in leaders if x in include_leaders_set]
        if not leaders:
            continue
        yield acc.name, leaders


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Diagnose activity capture coverage by fill_key")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite path")
    ap.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    ap.add_argument("--accounts", default="", help="Comma-separated account names (default: all)")
    ap.add_argument("--leaders", default="", help="Comma-separated leader addresses (default: all in selected accounts)")
    ap.add_argument("--show-missing", type=int, default=10, help="Show top N missing tx groups per leader")
    ap.add_argument("--json", action="store_true", help="Also print compact JSON summary")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=max(1, int(args.days)))
    start_ts = int(start_dt.timestamp())
    end_ts = int(now.timestamp())
    start_iso = start_dt.isoformat()
    end_iso = now.isoformat()

    include_accounts = [x.strip() for x in args.accounts.split(",") if x.strip()]
    include_leaders = [x.strip() for x in args.leaders.split(",") if x.strip()]

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    session = requests.Session()

    summary_rows: List[Dict[str, Any]] = []
    total_expected = 0
    total_actual = 0
    total_missing = 0

    try:
        for account_name, leaders in _iter_selected_accounts(include_accounts, include_leaders):
            print(f"\n=== account={account_name} leaders={len(leaders)} ===")
            for leader in leaders:
                expected, truncated = fetch_expected_fills(session, leader, start_ts, end_ts)
                actual = load_actual_fill_keys(conn, account_name, leader, start_iso, end_iso)

                exp_map = {x.fill_key: x for x in expected}
                exp_keys = set(exp_map.keys())
                act_keys = set(actual.keys())
                missing_keys = sorted(exp_keys - act_keys)
                extra_keys = sorted(act_keys - exp_keys)

                expected_usd = sum(x.usd for x in expected)
                actual_matched_usd = sum(exp_map[k].usd for k in exp_keys & act_keys)
                missing_usd = sum(exp_map[k].usd for k in missing_keys)

                total_expected += len(expected)
                total_actual += len(act_keys)
                total_missing += len(missing_keys)

                row = {
                    "account": account_name,
                    "leader": leader,
                    "expected_count": len(expected),
                    "actual_count": len(act_keys),
                    "missing_count": len(missing_keys),
                    "extra_count": len(extra_keys),
                    "expected_usd": round(expected_usd, 6),
                    "actual_matched_usd": round(actual_matched_usd, 6),
                    "missing_usd": round(missing_usd, 6),
                    "truncated": truncated,
                }
                summary_rows.append(row)
                print(
                    f"[leader={leader[:10]}...] "
                    f"expected={row['expected_count']} actual={row['actual_count']} "
                    f"missing={row['missing_count']} missing_usd={row['missing_usd']:.4f} "
                    f"extra={row['extra_count']} truncated={row['truncated']}"
                )

                if missing_keys and args.show_missing > 0:
                    by_tx: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "usd": 0.0, "samples": []})
                    for k in missing_keys:
                        item = exp_map[k]
                        tx = item.tx_hash or "<no-tx>"
                        by_tx[tx]["count"] += 1
                        by_tx[tx]["usd"] += item.usd
                        if len(by_tx[tx]["samples"]) < 3:
                            by_tx[tx]["samples"].append(
                                {
                                    "fill_key": k,
                                    "usd": round(item.usd, 6),
                                    "ts": item.ts_int,
                                    "condition": item.condition_id,
                                    "slug": item.slug,
                                }
                            )
                    print("  missing_by_tx:")
                    shown = 0
                    for tx, grp in sorted(by_tx.items(), key=lambda kv: kv[1]["usd"], reverse=True):
                        print(f"    tx={tx[:18]}... count={grp['count']} usd={grp['usd']:.6f}")
                        for sample in grp["samples"]:
                            print(
                                "      "
                                + json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
                            )
                        shown += 1
                        if shown >= args.show_missing:
                            break

        print("\n=== total ===")
        print(
            f"expected={total_expected} actual={total_actual} missing={total_missing} "
            f"missing_rate={(total_missing / total_expected if total_expected else 0.0):.4%}"
        )

        if args.json:
            payload = {
                "start_iso": start_iso,
                "end_iso": end_iso,
                "total_expected": total_expected,
                "total_actual": total_actual,
                "total_missing": total_missing,
                "rows": summary_rows,
            }
            print(json.dumps(payload, ensure_ascii=False))
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
