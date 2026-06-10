from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import requests

TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from polymarket_metrics import (  # noqa: E402
    CLOB_API,
    GAMMA_API,
    PNL_SKIP_THRESHOLD,
    _LIMITERS,
    compute_open_position_execution_stats,
    compute_orderbook_top5_depth_usd,
    ensure_schema,
    http_get_json,
)

CACHE_TABLE = "pm_open_execution_market_cache"
CACHE_BOOK_DEPTH = "book_depth_top5"
CACHE_GAMMA_MARKET = "gamma_market"


def _ensure_cache_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
            cache_type TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            payload_json TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (cache_type, cache_key)
        )
        """
    )
    conn.commit()


def _chunked(items: Sequence[str], size: int = 900) -> Sequence[Sequence[str]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def _load_cached_payloads(
    conn: sqlite3.Connection,
    cache_type: str,
    keys: Sequence[str],
    *,
    cache_days: int,
) -> Dict[str, Optional[Dict[str, Any]]]:
    uniq = [str(key).strip() for key in dict.fromkeys(keys) if str(key).strip()]
    if not uniq:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, int(cache_days)))).isoformat()
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for batch in _chunked(uniq):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT cache_key, payload_json
            FROM {CACHE_TABLE}
            WHERE cache_type=?
              AND updated_at>=?
              AND cache_key IN ({placeholders})
            """,
            (cache_type, cutoff, *batch),
        ).fetchall()
        for key, payload_json in rows:
            if payload_json is None:
                out[str(key)] = None
                continue
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = None
            out[str(key)] = payload if isinstance(payload, dict) else None
    return out


def _save_cached_payloads(
    conn: sqlite3.Connection,
    cache_type: str,
    payloads: Dict[str, Optional[Dict[str, Any]]],
) -> None:
    if not payloads:
        return
    updated_at = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        f"""
        INSERT OR REPLACE INTO {CACHE_TABLE} (cache_type, cache_key, payload_json, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                cache_type,
                str(key),
                json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else None,
                updated_at,
            )
            for key, value in payloads.items()
        ],
    )
    conn.commit()


def _load_open_position_count(conn: sqlite3.Connection, address: str) -> int:
    return int(
        conn.execute(
            """
            SELECT count(*)
            FROM pm_open_positions_cache
            WHERE address=?
              AND token_id IS NOT NULL
              AND token_id != ''
              AND abs(coalesce(size, 0)) > 0
            """,
            (address.lower(),),
        ).fetchone()[0]
    )


def _load_open_positions(conn: sqlite3.Connection, address: str, max_markets: int) -> List[Dict[str, Any]]:
    limit_sql = "" if max_markets <= 0 else f"LIMIT {int(max_markets)}"
    rows = conn.execute(
        f"""
        SELECT token_id, market_slug, size
        FROM pm_open_positions_cache
        WHERE address=?
          AND token_id IS NOT NULL
          AND token_id != ''
          AND abs(coalesce(size, 0)) > 0
        ORDER BY abs(coalesce(size, 0)) DESC, market_slug, token_id
        {limit_sql}
        """,
        (address.lower(),),
    ).fetchall()
    return [
        {
            "token_id": row[0],
            "slug": row[1],
            "size": row[2],
            "closed": False,
        }
        for row in rows
    ]


def _parse_details(raw: Any) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                details = parsed
        except Exception:
            details = {}
    return details


def _merge_details(raw: Any, stats: Dict[str, Any]) -> str:
    details = _parse_details(raw)
    details["openExecution"] = stats
    details["open_positions_analyzed"] = stats.get("open_positions_analyzed")
    details["open_positions_missing_book"] = stats.get("open_positions_missing_book")
    details["open_positions_missing_settlement"] = stats.get("open_positions_missing_settlement")
    return json.dumps(details, ensure_ascii=False)


def _merge_low_pnl_skip_details(raw: Any, total_pnl: Optional[float]) -> str:
    details = _parse_details(raw)
    total_pnl_value = float(total_pnl) if isinstance(total_pnl, (int, float)) else None
    details["openExecution"] = {
        "skipped": True,
        "skipReason": "total_pnl_below_profit_threshold",
        "threshold": PNL_SKIP_THRESHOLD,
        "totalPnl": total_pnl_value,
    }
    details["open_positions_analyzed"] = 0
    details["open_positions_missing_book"] = None
    details["open_positions_missing_settlement"] = None
    return json.dumps(details, ensure_ascii=False)


def _select_addresses(conn: sqlite3.Connection, *, only_missing: bool, limit: int) -> List[str]:
    clauses = ["coalesce(total_pnl, 0) >= ?"]
    if only_missing:
        clauses.append("avg_open_top5_depth_usd IS NULL AND avg_open_settlement_days IS NULL")
    where = "WHERE " + " AND ".join(clauses)
    limit_sql = "" if limit <= 0 else f"LIMIT {int(limit)}"
    rows = conn.execute(
        f"""
        SELECT address
        FROM address_metrics
        {where}
        ORDER BY updated_at DESC, address
        {limit_sql}
        """,
        (PNL_SKIP_THRESHOLD,),
    ).fetchall()
    return [str(row[0]).lower() for row in rows if row[0]]


def _clear_below_threshold_open_metrics(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT address, total_pnl, details_json
        FROM address_metrics
        WHERE coalesce(total_pnl, 0) < ?
          AND (avg_open_top5_depth_usd IS NOT NULL OR avg_open_settlement_days IS NOT NULL)
        """,
        (PNL_SKIP_THRESHOLD,),
    ).fetchall()
    if not rows:
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        UPDATE address_metrics
        SET avg_open_top5_depth_usd=NULL,
            avg_open_settlement_days=NULL,
            details_json=?,
            updated_at=?
        WHERE address=?
        """,
        [
            (
                _merge_low_pnl_skip_details(details_json, total_pnl),
                now_iso,
                str(address).lower(),
            )
            for address, total_pnl, details_json in rows
        ],
    )
    conn.commit()
    return len(rows)


def _prefetch_books(
    token_ids: Sequence[str],
    *,
    workers: int,
    timeout_s: float,
    max_retries: int,
    cache_writer: Optional[Callable[[Dict[str, Optional[Dict[str, float]]]], None]] = None,
) -> Dict[str, Optional[Dict[str, float]]]:
    out: Dict[str, Optional[Dict[str, float]]] = {}
    tokens = [str(token_id).strip() for token_id in dict.fromkeys(token_ids) if str(token_id).strip()]
    if not tokens:
        return out

    def load(token_id: str) -> tuple[str, Optional[Dict[str, float]]]:
        session = requests.Session()
        try:
            try:
                book = http_get_json(
                    session,
                    f"{CLOB_API}/book",
                    params={"token_id": token_id},
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                )
            except BaseException:
                book = None
            return token_id, compute_orderbook_top5_depth_usd(book) if book else None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = [executor.submit(load, token_id) for token_id in tokens]
        total = len(futures)
        pending_write: Dict[str, Optional[Dict[str, float]]] = {}
        for idx, future in enumerate(as_completed(futures), start=1):
            token_id, depth = future.result()
            out[token_id] = depth
            pending_write[token_id] = depth
            if cache_writer is not None and len(pending_write) >= 1000:
                cache_writer(pending_write)
                pending_write = {}
            if idx == 1 or idx % 1000 == 0 or idx == total:
                print(f"[open-exec] prefetched books {idx}/{total}", flush=True)
        if cache_writer is not None and pending_write:
            cache_writer(pending_write)
    return out


def _prefetch_markets(
    slugs: Sequence[str],
    *,
    workers: int,
    timeout_s: float,
    max_retries: int,
    cache_writer: Optional[Callable[[Dict[str, Optional[Dict[str, Any]]]], None]] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    keys = [str(slug).strip() for slug in dict.fromkeys(slugs) if str(slug).strip()]
    if not keys:
        return out

    def load(slug: str) -> tuple[str, Optional[Dict[str, Any]]]:
        session = requests.Session()
        try:
            out: Optional[Dict[str, Any]] = None
            try:
                data = http_get_json(
                    session,
                    f"{GAMMA_API}/markets",
                    params={"slug": slug, "limit": 1},
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                )
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    out = data[0]
            except BaseException:
                out = None

            if out is None:
                try:
                    data = http_get_json(
                        session,
                        f"{GAMMA_API}/events",
                        params={"slug": slug, "limit": 1},
                        timeout_s=timeout_s,
                        max_retries=max_retries,
                    )
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        event = data[0]
                        markets = event.get("markets")
                        if isinstance(markets, list) and markets and isinstance(markets[0], dict):
                            out = markets[0]
                        else:
                            out = event
                except BaseException:
                    out = None
            return slug, out
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = [executor.submit(load, slug) for slug in keys]
        total = len(futures)
        pending_write: Dict[str, Optional[Dict[str, Any]]] = {}
        for idx, future in enumerate(as_completed(futures), start=1):
            slug, market = future.result()
            out[slug] = market
            pending_write[slug] = market
            if cache_writer is not None and len(pending_write) >= 1000:
                cache_writer(pending_write)
                pending_write = {}
            if idx == 1 or idx % 1000 == 0 or idx == total:
                print(f"[open-exec] prefetched markets {idx}/{total}", flush=True)
        if cache_writer is not None and pending_write:
            cache_writer(pending_write)
    return out


def _configure_rate_limits(*, clob_qps: float, gamma_qps: float) -> None:
    if clob_qps > 0 and "clob" in _LIMITERS:
        _LIMITERS["clob"].qps = float(clob_qps)
    if gamma_qps > 0 and "gamma" in _LIMITERS:
        _LIMITERS["gamma"].qps = float(gamma_qps)


def backfill(
    db_path: Path,
    *,
    only_missing: bool = True,
    limit: int = 0,
    workers: int = 8,
    timeout_s: float = 5.0,
    max_retries: int = 1,
    max_markets_per_address: int = 200,
    cache_days: int = 3,
    clob_qps: float = 25.0,
    gamma_qps: float = 15.0,
) -> Dict[str, Any]:
    _configure_rate_limits(clob_qps=clob_qps, gamma_qps=gamma_qps)
    session = requests.Session()
    book_cache: Dict[str, Optional[Dict[str, float]]] = {}
    market_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    updated = 0
    failed: List[Dict[str, str]] = []
    cleared_below_threshold = 0

    with sqlite3.connect(str(db_path), timeout=60.0) as conn:
        ensure_schema(conn)
        _ensure_cache_schema(conn)
        cleared_below_threshold = _clear_below_threshold_open_metrics(conn)
        addresses = _select_addresses(conn, only_missing=only_missing, limit=limit)
        total = len(addresses)
        open_position_counts = {address: _load_open_position_count(conn, address) for address in addresses}
        positions_by_address = {
            address: _load_open_positions(conn, address, max_markets_per_address)
            for address in addresses
        }

        token_ids: List[str] = []
        slugs: List[str] = []
        for positions in positions_by_address.values():
            for pos in positions:
                token_id = str(pos.get("token_id") or "").strip()
                slug = str(pos.get("slug") or "").strip()
                if token_id:
                    token_ids.append(token_id)
                if slug:
                    slugs.append(slug)

        print(
            f"[open-exec] selected={total} unique_tokens={len(dict.fromkeys(token_ids))} "
            f"unique_slugs={len(dict.fromkeys(slugs))} workers={max(1, int(workers))} "
            f"timeout={float(timeout_s):.1f}s retries={int(max_retries)} "
            f"max_markets_per_address={int(max_markets_per_address)} cache_days={int(cache_days)} "
            f"clob_qps={float(clob_qps):.1f} gamma_qps={float(gamma_qps):.1f} "
            f"profit_threshold={float(PNL_SKIP_THRESHOLD):.0f} cleared_low_pnl={cleared_below_threshold}",
            flush=True,
        )
        cached_books = _load_cached_payloads(
            conn,
            CACHE_BOOK_DEPTH,
            token_ids,
            cache_days=cache_days,
        )
        cached_markets = _load_cached_payloads(
            conn,
            CACHE_GAMMA_MARKET,
            slugs,
            cache_days=cache_days,
        )
        book_cache.update(cached_books)  # type: ignore[arg-type]
        market_cache.update(cached_markets)

        unique_tokens = [str(token_id).strip() for token_id in dict.fromkeys(token_ids) if str(token_id).strip()]
        unique_slugs = [str(slug).strip() for slug in dict.fromkeys(slugs) if str(slug).strip()]
        missing_book_tokens = [token_id for token_id in unique_tokens if token_id not in book_cache]
        missing_market_slugs = [slug for slug in unique_slugs if slug not in market_cache]
        print(
            f"[open-exec] cache_hits books={len(cached_books)}/{len(unique_tokens)} "
            f"markets={len(cached_markets)}/{len(unique_slugs)}",
            flush=True,
        )

        fetched_books = _prefetch_books(
            missing_book_tokens,
            workers=workers,
            timeout_s=timeout_s,
            max_retries=max_retries,
            cache_writer=lambda payloads: _save_cached_payloads(conn, CACHE_BOOK_DEPTH, payloads),
        )
        book_cache.update(fetched_books)
        _save_cached_payloads(conn, CACHE_BOOK_DEPTH, fetched_books)  # type: ignore[arg-type]

        fetched_markets = _prefetch_markets(
            missing_market_slugs,
            workers=workers,
            timeout_s=timeout_s,
            max_retries=max_retries,
            cache_writer=lambda payloads: _save_cached_payloads(conn, CACHE_GAMMA_MARKET, payloads),
        )
        market_cache.update(fetched_markets)
        _save_cached_payloads(conn, CACHE_GAMMA_MARKET, fetched_markets)

        for idx, address in enumerate(addresses, start=1):
            print(f"[open-exec] {idx}/{total} {address[:10]}...", flush=True)
            try:
                positions = positions_by_address.get(address, [])
                stats = compute_open_position_execution_stats(
                    session,
                    positions,
                    book_cache=book_cache,
                    market_cache=market_cache,
                )
                stats["open_positions_total_cached"] = open_position_counts.get(address, len(positions))
                stats["open_positions_sampled"] = len(positions)
                stats["open_positions_sample_limit"] = int(max_markets_per_address)
                row = conn.execute(
                    "SELECT details_json FROM address_metrics WHERE address=? LIMIT 1",
                    (address,),
                ).fetchone()
                details_json = _merge_details(row[0] if row else None, stats)
                conn.execute(
                    """
                    UPDATE address_metrics
                    SET avg_open_top5_depth_usd=?,
                        avg_open_settlement_days=?,
                        details_json=?,
                        updated_at=?
                    WHERE address=?
                    """,
                    (
                        stats.get("avg_open_top5_depth_usd"),
                        stats.get("avg_open_settlement_days"),
                        details_json,
                        datetime.now(timezone.utc).isoformat(),
                        address,
                    ),
                )
                conn.commit()
                updated += 1
            except Exception as exc:  # noqa: BLE001
                failed.append({"address": address, "error": str(exc)})
                conn.rollback()
                print(f"[open-exec] failed {address}: {exc}", file=sys.stderr, flush=True)

    session.close()
    return {
        "db_path": str(db_path),
        "selected": updated + len(failed),
        "updated": updated,
        "failed": len(failed),
        "failures": failed[:20],
        "book_cache_size": len(book_cache),
        "market_cache_size": len(market_cache),
        "cache_days": int(cache_days),
        "max_markets_per_address": int(max_markets_per_address),
        "clob_qps": float(clob_qps),
        "gamma_qps": float(gamma_qps),
        "profit_threshold": float(PNL_SKIP_THRESHOLD),
        "cleared_below_threshold": int(cleared_below_threshold),
    }


def main(argv: Sequence[str]) -> int:
    ap = argparse.ArgumentParser(description="Backfill open-position liquidity depth and settlement days.")
    ap.add_argument("--db", type=str, default="metrics_fresh.sqlite")
    ap.add_argument("--all", action="store_true", help="Recompute all addresses, not just missing rows.")
    ap.add_argument("--limit", type=int, default=0, help="Max addresses to process; 0 means no limit.")
    ap.add_argument("--workers", type=int, default=16, help="Concurrent workers for API prefetch.")
    ap.add_argument("--timeout", type=float, default=5.0, help="Per-request timeout seconds for API prefetch.")
    ap.add_argument("--retries", type=int, default=1, help="Per-request retry count for API prefetch.")
    ap.add_argument(
        "--max-markets-per-address",
        type=int,
        default=200,
        help="Max open position tokens sampled per address; 0 means no cap.",
    )
    ap.add_argument("--cache-days", type=int, default=3, help="Reuse local market/book cache within N days.")
    ap.add_argument("--clob-qps", type=float, default=25.0, help="CLOB API rate limit for this backfill.")
    ap.add_argument("--gamma-qps", type=float, default=15.0, help="Gamma API rate limit for this backfill.")
    args = ap.parse_args(list(argv))
    summary = backfill(
        Path(args.db),
        only_missing=not bool(args.all),
        limit=int(args.limit),
        workers=max(1, int(args.workers)),
        timeout_s=max(1.0, float(args.timeout)),
        max_retries=max(1, int(args.retries)),
        max_markets_per_address=max(0, int(args.max_markets_per_address)),
        cache_days=max(0, int(args.cache_days)),
        clob_qps=max(1.0, float(args.clob_qps)),
        gamma_qps=max(1.0, float(args.gamma_qps)),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
