import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from polymarket_metrics import compute_realized_edge_score, ensure_schema, load_cached_positions_for_address


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _load_addresses(conn: sqlite3.Connection, only_missing: bool, limit: int) -> List[str]:
    where = "WHERE realized_edge_score IS NULL" if only_missing else ""
    limit_sql = "LIMIT ?" if limit > 0 else ""
    params: List[Any] = [limit] if limit > 0 else []
    rows = conn.execute(
        f"SELECT address FROM address_metrics {where} ORDER BY updated_at DESC {limit_sql}",
        params,
    ).fetchall()
    return [str(row[0]).lower() for row in rows if row and row[0]]


def _merge_position_edge_details(details_json: Any, edge_details: Dict[str, Any]) -> str:
    details: Dict[str, Any]
    if isinstance(details_json, str) and details_json.strip():
        try:
            parsed = json.loads(details_json)
            details = parsed if isinstance(parsed, dict) else {}
        except Exception:
            details = {}
    else:
        details = {}
    details["positionEdge"] = edge_details
    return json.dumps(details, ensure_ascii=False)


def backfill_realized_edge_score(
    db_path: Path,
    *,
    only_missing: bool,
    limit: int,
    batch_size: int,
    dry_run: bool,
) -> Dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        ensure_schema(conn)
        addresses = _load_addresses(conn, only_missing=only_missing, limit=limit)
        now_iso = datetime.now(timezone.utc).isoformat()
        scanned = 0
        updated = 0
        with_value = 0
        no_cached_positions = 0
        no_edge = 0

        for batch in _chunks(addresses, max(1, batch_size)):
            updates = []
            for address in batch:
                scanned += 1
                positions, open_count, closed_count = load_cached_positions_for_address(conn, address)
                if not open_count and not closed_count:
                    no_cached_positions += 1
                    continue

                edge_stats = compute_realized_edge_score(positions)
                edge_score = edge_stats.get("realized_edge_score")
                edge_details = edge_stats.get("details") if isinstance(edge_stats.get("details"), dict) else {}
                if isinstance(edge_score, (int, float)):
                    with_value += 1
                else:
                    no_edge += 1

                row = conn.execute(
                    "SELECT details_json FROM address_metrics WHERE address=? LIMIT 1",
                    (address,),
                ).fetchone()
                details_json = _merge_position_edge_details(row[0] if row else None, edge_details)
                updates.append((edge_score, details_json, now_iso, address))

            if updates and not dry_run:
                conn.executemany(
                    "UPDATE address_metrics SET realized_edge_score=?, details_json=?, updated_at=? WHERE address=?",
                    updates,
                )
                conn.commit()
                updated += len(updates)
            elif updates:
                updated += len(updates)

        return {
            "db_path": str(db_path),
            "dry_run": dry_run,
            "only_missing": only_missing,
            "scanned": scanned,
            "updated": updated,
            "with_realized_edge": with_value,
            "without_realized_edge": no_edge,
            "without_cached_positions": no_cached_positions,
        }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Backfill realized_edge_score from local cached Polymarket positions.")
    ap.add_argument("--db", type=str, default="metrics_fresh.sqlite", help="SQLite DB path")
    ap.add_argument("--all", action="store_true", help="Recompute all address_metrics rows, not just missing scores")
    ap.add_argument("--limit", type=int, default=0, help="Max addresses to process; 0 means no limit")
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true", help="Compute summary without writing updates")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    summary = backfill_realized_edge_score(
        Path(args.db),
        only_missing=not bool(args.all),
        limit=max(0, int(args.limit)),
        batch_size=max(1, int(args.batch_size)),
        dry_run=bool(args.dry_run),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
