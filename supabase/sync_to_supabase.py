import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

# 榛戝悕鍗曞湴鍧€锛氳繖浜涘湴鍧€涓嶄細鍚屾鍒?Supabase
BLACKLISTED_ADDRESSES = {
    "0xa5ef39c3d3e10d0b270233af41cac69796b12966",
}


def _load_dotenv(dotenv_path: Path) -> None:
    try:
        if not dotenv_path.exists():
            return
        for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if not k:
                continue
            if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                v = v[1:-1]
            os.environ.setdefault(k, v)
    except Exception:
        return


_load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class Cfg:
    sqlite_path: str
    copytrade_sqlite_path: str
    supabase_url: str
    service_role_key: str
    batch_size: int
    http_timeout_s: int
    http_max_retries: int
    http_backoff_s: float
    min_split_batch_size: int
    allow_empty_copytrade_daily_purge: bool
    copytrade_compare_only: bool
    purge_main_metrics: bool


class _UpsertRequestError(RuntimeError):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = bool(retryable)


def _env(name: str) -> Optional[str]:
    v = os.getenv(name)
    if v is None:
        return None
    v = v.strip()
    return v or None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args(argv: Sequence[str]) -> Cfg:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", type=str, default=_env("SQLITE_PATH") or "metrics_fresh.sqlite")
    _project_root = Path(__file__).resolve().parents[1]
    ap.add_argument(
        "--copytrade-sqlite",
        type=str,
        default=_env("COPYTRADE_SQLITE_PATH")
        or str(_project_root / "backend" / "packages" / "copytrade" / "copytrade.sqlite"),
    )
    ap.add_argument("--supabase-url", type=str, default=_env("SUPABASE_URL") or "")
    ap.add_argument("--service-role-key", type=str, default=_env("SUPABASE_SERVICE_ROLE_KEY") or "")
    ap.add_argument("--batch-size", type=int, default=int(_env("BATCH_SIZE") or "500"))
    ap.add_argument("--http-timeout-s", type=int, default=int(_env("SUPABASE_HTTP_TIMEOUT_S") or "90"))
    ap.add_argument("--http-max-retries", type=int, default=int(_env("SUPABASE_HTTP_MAX_RETRIES") or "5"))
    ap.add_argument("--http-backoff-s", type=float, default=float(_env("SUPABASE_HTTP_BACKOFF_S") or "1.0"))
    ap.add_argument("--min-split-batch-size", type=int, default=int(_env("SUPABASE_MIN_SPLIT_BATCH_SIZE") or "50"))
    ap.add_argument(
        "--allow-empty-copytrade-daily-purge",
        action="store_true",
        help=(
            "Allow deleting remote copytrade_daily_leader_pnl when local ct_daily_leader_pnl is empty. "
            "Default is OFF to protect historical rows."
        ),
    )
    ap.add_argument(
        "--copytrade-compare-only",
        action="store_true",
        help="Sync only copytrade compare tables from copytrade sqlite",
    )
    ap.add_argument(
        "--purge-main-metrics",
        action="store_true",
        help="Delete all rows from remote master_results and address_metrics, then exit",
    )
    args = ap.parse_args(list(argv))
    if not args.supabase_url:
        raise SystemExit("--supabase-url or SUPABASE_URL is required")
    if not args.service_role_key:
        if _env("SUPABASE_ANON_KEY"):
            raise SystemExit(
                "--service-role-key or SUPABASE_SERVICE_ROLE_KEY is required "
                "(SUPABASE_ANON_KEY cannot write; it is read-only for the dashboard)"
            )
        raise SystemExit("--service-role-key or SUPABASE_SERVICE_ROLE_KEY is required")
    return Cfg(
        sqlite_path=str(args.sqlite),
        copytrade_sqlite_path=str(args.copytrade_sqlite),
        supabase_url=str(args.supabase_url).rstrip("/"),
        service_role_key=str(args.service_role_key),
        batch_size=int(args.batch_size),
        http_timeout_s=max(10, int(args.http_timeout_s)),
        http_max_retries=max(1, int(args.http_max_retries)),
        http_backoff_s=max(0.0, float(args.http_backoff_s)),
        min_split_batch_size=max(1, int(args.min_split_batch_size)),
        allow_empty_copytrade_daily_purge=bool(
            args.allow_empty_copytrade_daily_purge
            or _env_bool("SUPABASE_ALLOW_EMPTY_COPYTRADE_DAILY_PURGE", False)
        ),
        copytrade_compare_only=bool(args.copytrade_compare_only),
        purge_main_metrics=bool(args.purge_main_metrics),
    )


def _headers(cfg: Cfg) -> Dict[str, str]:
    return {
        "apikey": cfg.service_role_key,
        "authorization": f"Bearer {cfg.service_role_key}",
        "content-type": "application/json",
        "prefer": "resolution=merge-duplicates,return=minimal",
    }


def _request_with_retries(
    cfg: Cfg,
    method: str,
    url: str,
    *,
    table: str,
    timeout_s: Optional[int] = None,
    **kwargs: Any,
) -> requests.Response:
    max_retries = max(1, int(cfg.http_max_retries))
    method_upper = method.upper()
    timeout = cfg.http_timeout_s if timeout_s is None else max(1, int(timeout_s))
    last_error: Optional[str] = None

    for attempt in range(max_retries):
        try:
            r = requests.request(method_upper, url, timeout=timeout, **kwargs)
        except requests.RequestException as e:
            last_error = f"{method_upper} {table} failed: {e}"
            if attempt + 1 < max_retries:
                _sleep_backoff(attempt, cfg.http_backoff_s)
                continue
            raise RuntimeError(f"{last_error} (after {max_retries} attempts)")

        if r.status_code < 400:
            return r
        if _is_retryable_status(r.status_code) and attempt + 1 < max_retries:
            _sleep_backoff(attempt, cfg.http_backoff_s)
            continue
        return r

    raise RuntimeError(last_error or f"{method_upper} {table} failed after {max_retries} attempts")


def _count_rows(cfg: Cfg, table: str, params: Dict[str, str]) -> Optional[int]:
    url = f"{cfg.supabase_url}/rest/v1/{table}"
    headers = {**_headers(cfg), "prefer": "count=exact"}
    q = {"select": "*", "limit": 1, **params}
    r = _request_with_retries(cfg, "GET", url, table=table, headers=headers, params=q, timeout_s=30)
    if r.status_code >= 400:
        return None
    cr = r.headers.get("content-range", "")
    if "/" not in cr:
        return None
    total_part = cr.split("/", 1)[1].strip()
    try:
        return int(total_part)
    except Exception:
        return None


def _delete_by_filter(cfg: Cfg, table: str, params: Dict[str, str]) -> int:
    url = f"{cfg.supabase_url}/rest/v1/{table}"
    headers = {**_headers(cfg), "prefer": "return=minimal"}
    max_retries = max(1, int(cfg.http_max_retries))
    last_error: Optional[str] = None
    for attempt in range(max_retries):
        try:
            r = requests.delete(url, headers=headers, params=params, timeout=60)
        except requests.RequestException as e:
            last_error = f"DELETE {table} failed: {e}"
            if attempt + 1 < max_retries:
                _sleep_backoff(attempt, cfg.http_backoff_s)
                continue
            raise RuntimeError(f"{last_error} (after {max_retries} attempts)")
        if r.status_code < 400:
            try:
                deleted = _count_rows(cfg, table, params)
            except RuntimeError as e:
                print(f"[sync] WARNING count after DELETE {table} failed: {e}", file=sys.stderr)
                return 0
            return 0 if deleted is None else deleted
        last_error = f"DELETE {table} failed: {r.status_code} {r.text[:1000]}"
        if _is_retryable_status(r.status_code) and attempt + 1 < max_retries:
            _sleep_backoff(attempt, cfg.http_backoff_s)
            continue
        if _is_retryable_status(r.status_code):
            raise RuntimeError(f"{last_error} (after {max_retries} attempts)")
        raise RuntimeError(last_error)
    # 鍒犻櫎鍚庡啀鏁颁竴娆★紝杩斿洖鐨勬槸鍒犻櫎鍚庡墿浣欙紱姝ゅ浠呬綔涓烘垚鍔熶俊鍙?    return 0 if deleted is None else deleted


def _purge_entire_table(cfg: Cfg, table: str, key_field: str) -> None:
    _delete_by_filter(cfg, table, {key_field: "not.is.null"})


def _get_max_id(cfg: Cfg, table: str) -> int:
    url = f"{cfg.supabase_url}/rest/v1/{table}"
    r = _request_with_retries(
        cfg,
        "GET",
        url,
        table=table,
        headers=_headers(cfg),
        params={"select": "id", "order": "id.desc", "limit": 1},
        timeout_s=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"GET {table} failed: {r.status_code} {r.text[:500]}")
    data = r.json()
    if isinstance(data, list) and data:
        v = data[0].get("id")
        try:
            return int(v)
        except Exception:
            return 0
    return 0


def _upsert(cfg: Cfg, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> None:
    if not rows:
        return

    pending: List[List[Dict[str, Any]]] = [rows]
    while pending:
        batch = pending.pop(0)
        try:
            _upsert_once(cfg, table, batch, on_conflict=on_conflict)
        except _UpsertRequestError as e:
            # Transient gateway/rate-limit failures can often be recovered by smaller payloads.
            if e.retryable and len(batch) > cfg.min_split_batch_size:
                mid = max(1, len(batch) // 2)
                left = batch[:mid]
                right = batch[mid:]
                print(
                    f"[sync] transient upsert issue on {table}; split batch {len(batch)} -> "
                    f"{len(left)} + {len(right)}"
                )
                if right:
                    pending.insert(0, right)
                if left:
                    pending.insert(0, left)
                continue
            raise RuntimeError(str(e))


def _is_retryable_status(status_code: int) -> bool:
    if status_code in (408, 425, 429, 500, 502, 503, 504):
        return True
    return 520 <= status_code <= 527


def _sleep_backoff(attempt: int, base_s: float) -> None:
    time.sleep(max(0.0, float(base_s)) * (2**attempt))


def _upsert_once(cfg: Cfg, table: str, rows: List[Dict[str, Any]], on_conflict: str) -> None:
    url = f"{cfg.supabase_url}/rest/v1/{table}"
    params = {"on_conflict": on_conflict}
    payload = json.dumps(rows, ensure_ascii=False)
    max_retries = max(1, int(cfg.http_max_retries))

    for attempt in range(max_retries):
        try:
            r = requests.post(
                url,
                headers=_headers(cfg),
                params=params,
                data=payload,
                timeout=cfg.http_timeout_s,
            )
        except requests.RequestException as e:
            if attempt + 1 < max_retries:
                _sleep_backoff(attempt, cfg.http_backoff_s)
                continue
            raise _UpsertRequestError(
                f"UPSERT {table} failed after {max_retries} attempts: {e}",
                retryable=True,
            )

        if r.status_code < 400:
            return

        msg = f"UPSERT {table} failed: {r.status_code} {(r.text or '')[:1000]}"
        if _is_retryable_status(r.status_code):
            if attempt + 1 < max_retries:
                _sleep_backoff(attempt, cfg.http_backoff_s)
                continue
            raise _UpsertRequestError(f"{msg} (after {max_retries} attempts)", retryable=True)

        raise _UpsertRequestError(msg, retryable=False)


def _table_exists(cfg: Cfg, table: str) -> bool:
    url = f"{cfg.supabase_url}/rest/v1/{table}"
    r = _request_with_retries(
        cfg,
        "GET",
        url,
        table=table,
        headers=_headers(cfg),
        params={"select": "*", "limit": 1},
        timeout_s=30,
    )
    return r.status_code < 400


def _chunks(rows: List[Dict[str, Any]], n: int) -> List[List[Dict[str, Any]]]:
    return [rows[i : i + n] for i in range(0, len(rows), n)]


def _safe_json(s: Any) -> Any:
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    if isinstance(s, str):
        t = s.strip()
        if not t:
            return None
        try:
            return json.loads(t)
        except Exception:
            return None
    return None


def _fetch_sqlite_master(conn: sqlite3.Connection, min_id_exclusive: int) -> List[Dict[str, Any]]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(master_results)").fetchall()}
    has_min_trades = "min_trades" in cols
    select_cols = (
        "id, timestamp, sport, "
        + ("min_trades, " if has_min_trades else "")
        + "limit_count, total_holders, successful_metrics, failed_metrics, "
        + "nba_markets_json, top_holders_json, metrics_summary_json, created_at"
    )
    rows = conn.execute(
        f"SELECT {select_cols} FROM master_results WHERE id > ? ORDER BY id ASC",
        (int(min_id_exclusive),),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        idx = 0
        idv = r[idx]; idx += 1
        timestamp = r[idx]; idx += 1
        sport = r[idx]; idx += 1
        if has_min_trades:
            min_trades = r[idx]
            idx += 1
        else:
            min_trades = None
        limit_count = r[idx]; idx += 1
        total_holders = r[idx]; idx += 1
        successful_metrics = r[idx]; idx += 1
        failed_metrics = r[idx]; idx += 1
        nba_markets_json = r[idx]; idx += 1
        top_holders_json = r[idx]; idx += 1
        metrics_summary_json = r[idx]; idx += 1
        created_at = r[idx]
        out.append(
            {
                "id": int(idv),
                "timestamp": timestamp,
                "sport": sport,
                "min_trades": min_trades,
                "limit_count": limit_count,
                "total_holders": total_holders,
                "successful_metrics": successful_metrics,
                "failed_metrics": failed_metrics,
                "nba_markets_json": _safe_json(nba_markets_json),
                "top_holders_json": _safe_json(top_holders_json),
                "metrics_summary_json": _safe_json(metrics_summary_json),
                "created_at": created_at,
            }
        )
    return out


def _fetch_sqlite_address_metrics(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(address_metrics)").fetchall()}
    for name in ("avg_open_top5_depth_usd", "avg_open_settlement_days"):
        if name not in cols:
            conn.execute(f"ALTER TABLE address_metrics ADD COLUMN {name} REAL")
    conn.commit()
    rows = conn.execute(
        "SELECT address, snapshot_utc, total_pnl, realized_pnl, unrealized_pnl, profit_factor, roi, "
        "max_drawdown, sharpe, current_position_value_usd, total_trades, winning_trades, losing_trades, win_rate, "
        "avg_trade_price, realized_edge_score, avg_open_top5_depth_usd, avg_open_settlement_days, "
        "ct_score_total_100, ct_score_roi, ct_score_pf, ct_score_mdd, ct_score_sharpe, ct_score_ui, ct_score_r2, "
        "copytrade_value_score, copytrade_value_level, copytrade_value_exclusion_reason, copytrade_value_score_version, "
        "confidence, details_json, source_tags, ulcer_index, equity_r2, updated_at "
        "FROM address_metrics",
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        (
            address,
            snapshot_utc,
            total_pnl,
            realized_pnl,
            unrealized_pnl,
            profit_factor,
            roi,
            max_drawdown,
            sharpe,
            current_position_value_usd,
            total_trades,
            winning_trades,
            losing_trades,
            win_rate,
            avg_trade_price,
            realized_edge_score,
            avg_open_top5_depth_usd,
            avg_open_settlement_days,
            ct_score_total_100,
            ct_score_roi,
            ct_score_pf,
            ct_score_mdd,
            ct_score_sharpe,
            ct_score_ui,
            ct_score_r2,
            copytrade_value_score,
            copytrade_value_level,
            copytrade_value_exclusion_reason,
            copytrade_value_score_version,
            confidence,
            details_json,
            source_tags,
            ulcer_index,
            equity_r2,
            updated_at,
        ) = r
        out.append(
            {
                "address": (address or "").lower() if isinstance(address, str) else address,
                "total_pnl": total_pnl,
                "realized_pnl": realized_pnl,
                "unrealized_pnl": unrealized_pnl,
                "profit_factor": profit_factor,
                "roi": roi,
                "max_drawdown": max_drawdown,
                "sharpe": sharpe,
                "current_position_value_usd": current_position_value_usd,
                "total_trades": total_trades,
                "winning_trades": winning_trades,
                "losing_trades": losing_trades,
                "win_rate": win_rate,
                "avg_trade_price": avg_trade_price,
                "realized_edge_score": realized_edge_score,
                "avg_open_top5_depth_usd": avg_open_top5_depth_usd,
                "avg_open_settlement_days": avg_open_settlement_days,
                "ct_score_total_100": ct_score_total_100,
                "ct_score_roi": ct_score_roi,
                "ct_score_pf": ct_score_pf,
                "ct_score_mdd": ct_score_mdd,
                "ct_score_sharpe": ct_score_sharpe,
                "ct_score_ui": ct_score_ui,
                "ct_score_r2": ct_score_r2,
                "copytrade_value_score": copytrade_value_score,
                "copytrade_value_level": copytrade_value_level,
                "copytrade_value_exclusion_reason": copytrade_value_exclusion_reason,
                "copytrade_value_score_version": copytrade_value_score_version,
                "confidence": confidence,
                "details_json": _safe_json(details_json),
                "source_tags": source_tags,
                "ulcer_index": ulcer_index,
                "equity_r2": equity_r2,
                "snapshot_utc": snapshot_utc,
                "updated_at": updated_at,
            }
        )
    # 低利润/跳过地址也要同步到 Supabase，否则前端无法展示历史标签。
    # 前端负责用筛选器决定展示范围；这里只过滤明确黑名单地址。
    return [
        r for r in out
        if r.get("address", "").lower() not in BLACKLISTED_ADDRESSES
    ]


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def _fetch_copytrade_leader_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    has_acct = _has_column(conn, "ct_leader_summary", "account_name")
    rows = conn.execute("SELECT * FROM ct_leader_summary").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        la = d.get("leader_address") or ""
        out.append({
            "leader_address": la.lower() if isinstance(la, str) else la,
            "account_name": d.get("account_name", "default") if has_acct else "default",
            "total_realized_pnl": d.get("total_realized_pnl"),
            "total_unrealized_pnl": d.get("total_unrealized_pnl"),
            "total_pnl": d.get("total_pnl"),
            "winning_markets": d.get("winning_markets"),
            "losing_markets": d.get("losing_markets"),
            "total_markets": d.get("total_markets"),
            "win_rate": d.get("win_rate"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _fetch_copytrade_leader_market_pnl(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    has_acct = _has_column(conn, "ct_leader_market_pnl", "account_name")
    rows = conn.execute("SELECT * FROM ct_leader_market_pnl").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        la = d.get("leader_address") or ""
        out.append({
            "leader_address": la.lower() if isinstance(la, str) else la,
            "condition_id": d.get("condition_id"),
            "account_name": d.get("account_name", "default") if has_acct else "default",
            "market_slug": d.get("market_slug"),
            "total_realized_pnl": d.get("total_realized_pnl"),
            "total_unrealized_pnl": d.get("total_unrealized_pnl"),
            "total_pnl": d.get("total_pnl"),
            "market_result": d.get("market_result"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _fetch_copytrade_daily_equity(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _sqlite_table_exists(conn, "ct_daily_equity"):
        return []
    rows = conn.execute(
        "SELECT date_key, total_equity, total_realized_pnl, total_unrealized_pnl, "
        "total_cost_basis, open_position_count, updated_at FROM ct_daily_equity"
    ).fetchall()
    return [
        {
            "date_key": r[0],
            "total_equity": r[1],
            "total_realized_pnl": r[2],
            "total_unrealized_pnl": r[3],
            "total_cost_basis": r[4],
            "open_position_count": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]


def _fetch_copytrade_daily_leader_pnl(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _sqlite_table_exists(conn, "ct_daily_leader_pnl"):
        return []
    has_acct = _has_column(conn, "ct_daily_leader_pnl", "account_name")
    rows = conn.execute("SELECT * FROM ct_daily_leader_pnl").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        la = d.get("leader_address") or ""
        out.append({
            "date_key": d.get("date_key"),
            "leader_address": la.lower() if isinstance(la, str) else la,
            "account_name": d.get("account_name", "default") if has_acct else "default",
            "realized_pnl": d.get("realized_pnl"),
            "unrealized_pnl": d.get("unrealized_pnl"),
            "total_pnl": d.get("total_pnl"),
            "market_count": d.get("market_count"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _fetch_copytrade_daily_leader_market_leg_pnl(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _sqlite_table_exists(conn, "ct_daily_leader_market_leg_pnl"):
        return []
    rows = conn.execute("SELECT * FROM ct_daily_leader_market_leg_pnl").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        leader_address = d.get("leader_address") or ""
        out.append({
            "date_key": d.get("date_key"),
            "leader_address": leader_address.lower() if isinstance(leader_address, str) else leader_address,
            "account_name": d.get("account_name", "default") or "default",
            "condition_id": d.get("condition_id"),
            "token_id": d.get("token_id"),
            "market_slug": d.get("market_slug"),
            "outcome": d.get("outcome"),
            "buy_fill_count": d.get("buy_fill_count"),
            "buy_size": d.get("buy_size"),
            "buy_cost_usd": d.get("buy_cost_usd"),
            "sell_fill_count": d.get("sell_fill_count"),
            "sell_size": d.get("sell_size"),
            "sell_proceeds_usd": d.get("sell_proceeds_usd"),
            "settled_size": d.get("settled_size"),
            "open_size_eod": d.get("open_size_eod"),
            "close_state_eod": d.get("close_state_eod"),
            "realized_pnl_delta": d.get("realized_pnl_delta"),
            "unrealized_pnl_delta": d.get("unrealized_pnl_delta"),
            "total_pnl_delta": d.get("total_pnl_delta"),
            "realized_pnl_eod": d.get("realized_pnl_eod"),
            "unrealized_pnl_eod": d.get("unrealized_pnl_eod"),
            "total_pnl_eod": d.get("total_pnl_eod"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _fetch_copytrade_compare_daily_summary(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _sqlite_table_exists(conn, "ct_compare_daily_summary"):
        return []
    rows = conn.execute("SELECT * FROM ct_compare_daily_summary").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        leader_address = d.get("leader_address") or ""
        out.append({
            "date_key": d.get("date_key"),
            "account_name": d.get("account_name", "default") or "default",
            "leader_address": leader_address.lower() if isinstance(leader_address, str) else leader_address,
            "leader_total_pnl": d.get("leader_total_pnl"),
            "our_total_pnl": d.get("our_total_pnl"),
            "delta_pnl": d.get("delta_pnl"),
            "leader_excluded_pnl": d.get("leader_excluded_pnl"),
            "our_excluded_pnl": d.get("our_excluded_pnl"),
            "visible_leader_pnl": d.get("visible_leader_pnl"),
            "visible_our_pnl": d.get("visible_our_pnl"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _fetch_copytrade_compare_daily_market_leg(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    if not _sqlite_table_exists(conn, "ct_compare_daily_market_leg"):
        return []
    rows = conn.execute("SELECT * FROM ct_compare_daily_market_leg").fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        leader_address = d.get("leader_address") or ""
        out.append({
            "date_key": d.get("date_key"),
            "account_name": d.get("account_name", "default") or "default",
            "leader_address": leader_address.lower() if isinstance(leader_address, str) else leader_address,
            "condition_id": d.get("condition_id"),
            "token_id": d.get("token_id"),
            "market_slug": d.get("market_slug"),
            "outcome": d.get("outcome"),
            "exclusion_reason": d.get("exclusion_reason"),
            "leader_buy_fill_count": d.get("leader_buy_fill_count"),
            "leader_buy_usd": d.get("leader_buy_usd"),
            "leader_buy_avg_price": d.get("leader_buy_avg_price"),
            "leader_sell_fill_count": d.get("leader_sell_fill_count"),
            "leader_sell_usd": d.get("leader_sell_usd"),
            "leader_sell_avg_price": d.get("leader_sell_avg_price"),
            "leader_realized_pnl": d.get("leader_realized_pnl"),
            "leader_unrealized_change": d.get("leader_unrealized_change"),
            "leader_total_pnl": d.get("leader_total_pnl"),
            "our_buy_fill_count": d.get("our_buy_fill_count"),
            "our_buy_usd": d.get("our_buy_usd"),
            "our_buy_avg_price": d.get("our_buy_avg_price"),
            "our_sell_fill_count": d.get("our_sell_fill_count"),
            "our_sell_usd": d.get("our_sell_usd"),
            "our_sell_avg_price": d.get("our_sell_avg_price"),
            "our_realized_pnl": d.get("our_realized_pnl"),
            "our_unrealized_change": d.get("our_unrealized_change"),
            "our_total_pnl": d.get("our_total_pnl"),
            "primary_gap_reason": d.get("primary_gap_reason"),
            "updated_at": d.get("updated_at"),
        })
    return out


def _delete_copytrade_daily_leader_pnl_accounts(cfg: Cfg, rows: List[Dict[str, Any]]) -> None:
    """Mirror mode: delete remote slices by account before full upsert."""
    accounts = {
        str(row.get("account_name") or "default").strip() or "default"
        for row in rows
    }
    if not accounts:
        return
    for account_name in sorted(accounts):
        _delete_by_filter(
            cfg,
            "copytrade_daily_leader_pnl",
            {"account_name": f"eq.{account_name}"},
        )


def _delete_copytrade_daily_leader_market_leg_pnl_accounts(
    cfg: Cfg,
    rows: List[Dict[str, Any]],
) -> None:
    accounts = {
        str(row.get("account_name") or "default").strip() or "default"
        for row in rows
    }
    if not accounts:
        return
    for account_name in sorted(accounts):
        _delete_by_filter(
            cfg,
            "copytrade_daily_leader_market_leg_pnl",
            {"account_name": f"eq.{account_name}"},
        )


def _delete_copytrade_compare_daily_summary_accounts(
    cfg: Cfg,
    rows: List[Dict[str, Any]],
) -> None:
    accounts = {
        str(row.get("account_name") or "default").strip() or "default"
        for row in rows
    }
    if not accounts:
        return
    for account_name in sorted(accounts):
        _delete_by_filter(
            cfg,
            "copytrade_compare_daily_summary",
            {"account_name": f"eq.{account_name}"},
        )


def _delete_copytrade_compare_daily_market_leg_accounts(
    cfg: Cfg,
    rows: List[Dict[str, Any]],
) -> None:
    accounts = {
        str(row.get("account_name") or "default").strip() or "default"
        for row in rows
    }
    if not accounts:
        return
    for account_name in sorted(accounts):
        _delete_by_filter(
            cfg,
            "copytrade_compare_daily_market_leg",
            {"account_name": f"eq.{account_name}"},
        )


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def main(argv: Sequence[str]) -> int:
    cfg = parse_args(argv)
    sqlite_path = Path(cfg.sqlite_path)
    if cfg.purge_main_metrics:
        master_before = _count_rows(cfg, "master_results", {"id": "not.is.null"}) or 0
        metrics_before = _count_rows(cfg, "address_metrics", {"address": "not.is.null"}) or 0
        _purge_entire_table(cfg, "master_results", "id")
        _purge_entire_table(cfg, "address_metrics", "address")
        print(
            json.dumps(
                {
                    "purged": True,
                    "tables": {
                        "master_results_before": master_before,
                        "address_metrics_before": metrics_before,
                        "master_results_after": _count_rows(cfg, "master_results", {"id": "not.is.null"}) or 0,
                        "address_metrics_after": _count_rows(cfg, "address_metrics", {"address": "not.is.null"}) or 0,
                    },
                },
                ensure_ascii=False,
            )
        )
        return 0
    if not cfg.copytrade_compare_only and not sqlite_path.exists():
        raise SystemExit(f"sqlite not found: {sqlite_path}")

    copytrade_sqlite = Path(cfg.copytrade_sqlite_path)

    masters: List[Dict[str, Any]] = []
    address_metrics: List[Dict[str, Any]] = []
    if not cfg.copytrade_compare_only:
        with sqlite3.connect(str(sqlite_path)) as conn:
            conn.row_factory = sqlite3.Row
            max_master = _get_max_id(cfg, "master_results")

            masters = _fetch_sqlite_master(conn, max_master)
            address_metrics = _fetch_sqlite_address_metrics(conn)

    leader_summary: List[Dict[str, Any]] = []
    daily_equity: List[Dict[str, Any]] = []
    daily_leader_pnl: List[Dict[str, Any]] = []
    daily_leader_market_leg_pnl: List[Dict[str, Any]] = []
    compare_daily_summary: List[Dict[str, Any]] = []
    compare_daily_market_leg: List[Dict[str, Any]] = []
    copytrade_local_tables_ok = False
    copytrade_daily_local_table_exists = False
    copytrade_daily_source_ready = False
    copytrade_leg_detail_local_table_exists = False
    copytrade_leg_detail_source_ready = False
    copytrade_compare_summary_local_table_exists = False
    copytrade_compare_market_leg_local_table_exists = False
    if copytrade_sqlite.exists():
        with sqlite3.connect(str(copytrade_sqlite)) as cconn:
            cconn.row_factory = sqlite3.Row
            if not cfg.copytrade_compare_only:
                has_local_summary = _sqlite_table_exists(cconn, "ct_leader_summary")
                copytrade_local_tables_ok = bool(has_local_summary)
                if copytrade_local_tables_ok:
                    leader_summary = _fetch_copytrade_leader_summary(cconn)
                daily_equity = _fetch_copytrade_daily_equity(cconn)
                copytrade_daily_local_table_exists = _sqlite_table_exists(cconn, "ct_daily_leader_pnl")
                if copytrade_daily_local_table_exists:
                    daily_leader_pnl = _fetch_copytrade_daily_leader_pnl(cconn)
                    copytrade_daily_source_ready = True
                copytrade_leg_detail_local_table_exists = _sqlite_table_exists(cconn, "ct_daily_leader_market_leg_pnl")
                if copytrade_leg_detail_local_table_exists:
                    daily_leader_market_leg_pnl = _fetch_copytrade_daily_leader_market_leg_pnl(cconn)
                    copytrade_leg_detail_source_ready = True
            copytrade_compare_summary_local_table_exists = _sqlite_table_exists(cconn, "ct_compare_daily_summary")
            if copytrade_compare_summary_local_table_exists:
                compare_daily_summary = _fetch_copytrade_compare_daily_summary(cconn)
            copytrade_compare_market_leg_local_table_exists = _sqlite_table_exists(cconn, "ct_compare_daily_market_leg")
            if copytrade_compare_market_leg_local_table_exists:
                compare_daily_market_leg = _fetch_copytrade_compare_daily_market_leg(cconn)
    # Clean old skipped_low_pnl rows in Supabase.
    skipped_before = 0
    skipped_after = 0
    if not cfg.copytrade_compare_only:
        skipped_before = _count_rows(cfg, "address_metrics", {"confidence": "eq.skipped_low_pnl"})
        _delete_by_filter(cfg, "address_metrics", {"confidence": "eq.skipped_low_pnl"})
        skipped_after = _count_rows(cfg, "address_metrics", {"confidence": "eq.skipped_low_pnl"})

    if not cfg.copytrade_compare_only:
        if masters:
            for batch in _chunks(masters, cfg.batch_size):
                _upsert(cfg, "master_results", batch, on_conflict="id")
        if address_metrics:
            for batch in _chunks(address_metrics, cfg.batch_size):
                _upsert(cfg, "address_metrics", batch, on_conflict="address")
    copytrade_summary_synced = 0
    has_copytrade_summary_table = (not cfg.copytrade_compare_only) and _table_exists(cfg, "copytrade_leader_summary")

    if not cfg.copytrade_compare_only and leader_summary and has_copytrade_summary_table:
        # Remove legacy default-account rows (already migrated to main).
        try:
            _delete_by_filter(cfg, "copytrade_leader_summary", {"account_name": "eq.default"})
        except Exception as e:
            print(f"[sync] WARNING legacy default cleanup skipped for copytrade_leader_summary: {e}")
        for batch in _chunks(leader_summary, cfg.batch_size):
            _upsert(cfg, "copytrade_leader_summary", batch, on_conflict="leader_address,account_name")
        copytrade_summary_synced = len(leader_summary)

    daily_equity_synced = 0
    daily_leader_pnl_synced = 0
    daily_leader_market_leg_pnl_synced = 0
    compare_daily_summary_synced = 0
    compare_daily_market_leg_synced = 0
    if not cfg.copytrade_compare_only and daily_equity and _table_exists(cfg, "copytrade_daily_equity"):
        for batch in _chunks(daily_equity, cfg.batch_size):
            _upsert(cfg, "copytrade_daily_equity", batch, on_conflict="date_key")
        daily_equity_synced = len(daily_equity)
    if not cfg.copytrade_compare_only and _table_exists(cfg, "copytrade_daily_leader_pnl"):
        if not copytrade_daily_source_ready:
            print(
                "[sync] WARNING skip copytrade_daily_leader_pnl mirror: "
                "local ct_daily_leader_pnl is unavailable"
            )
        elif not daily_leader_pnl:
            if cfg.allow_empty_copytrade_daily_purge:
                _delete_by_filter(cfg, "copytrade_daily_leader_pnl", {})
                print(
                    "[sync] local ct_daily_leader_pnl is empty; "
                    "remote rows purged (--allow-empty-copytrade-daily-purge)"
                )
            else:
                print(
                    "[sync] WARNING local ct_daily_leader_pnl is empty; "
                    "skip remote delete/upsert to protect existing history"
                )
        else:
            _delete_copytrade_daily_leader_pnl_accounts(cfg, daily_leader_pnl)
            for batch in _chunks(daily_leader_pnl, cfg.batch_size):
                _upsert(cfg, "copytrade_daily_leader_pnl", batch, on_conflict="date_key,leader_address,account_name")
            daily_leader_pnl_synced = len(daily_leader_pnl)
    if not cfg.copytrade_compare_only and _table_exists(cfg, "copytrade_daily_leader_market_leg_pnl"):
        if not copytrade_leg_detail_source_ready:
            print(
                "[sync] WARNING skip copytrade_daily_leader_market_leg_pnl mirror: "
                "local ct_daily_leader_market_leg_pnl is unavailable"
            )
        elif not daily_leader_market_leg_pnl:
            print(
                "[sync] WARNING local ct_daily_leader_market_leg_pnl is empty; "
                "skip remote delete/upsert to protect existing history"
            )
        else:
            _delete_copytrade_daily_leader_market_leg_pnl_accounts(cfg, daily_leader_market_leg_pnl)
            for batch in _chunks(daily_leader_market_leg_pnl, cfg.batch_size):
                _upsert(
                    cfg,
                    "copytrade_daily_leader_market_leg_pnl",
                    batch,
                    on_conflict="date_key,leader_address,account_name,condition_id,token_id",
                )
            daily_leader_market_leg_pnl_synced = len(daily_leader_market_leg_pnl)

    if _table_exists(cfg, "copytrade_compare_daily_summary"):
        if not copytrade_compare_summary_local_table_exists:
            print(
                "[sync] WARNING skip copytrade_compare_daily_summary mirror: "
                "local ct_compare_daily_summary is unavailable"
            )
        elif not compare_daily_summary:
            print(
                "[sync] WARNING local ct_compare_daily_summary is empty; "
                "skip remote delete/upsert to protect existing history"
            )
        else:
            _delete_copytrade_compare_daily_summary_accounts(cfg, compare_daily_summary)
            for batch in _chunks(compare_daily_summary, cfg.batch_size):
                _upsert(
                    cfg,
                    "copytrade_compare_daily_summary",
                    batch,
                    on_conflict="date_key,account_name,leader_address",
                )
            compare_daily_summary_synced = len(compare_daily_summary)

    if _table_exists(cfg, "copytrade_compare_daily_market_leg"):
        if not copytrade_compare_market_leg_local_table_exists:
            print(
                "[sync] WARNING skip copytrade_compare_daily_market_leg mirror: "
                "local ct_compare_daily_market_leg is unavailable"
            )
        elif not compare_daily_market_leg:
            print(
                "[sync] WARNING local ct_compare_daily_market_leg is empty; "
                "skip remote delete/upsert to protect existing history"
            )
        else:
            _delete_copytrade_compare_daily_market_leg_accounts(cfg, compare_daily_market_leg)
            for batch in _chunks(compare_daily_market_leg, cfg.batch_size):
                _upsert(
                    cfg,
                    "copytrade_compare_daily_market_leg",
                    batch,
                    on_conflict="date_key,account_name,leader_address,condition_id,token_id",
                )
            compare_daily_market_leg_synced = len(compare_daily_market_leg)

    print(
        json.dumps(
            {
                "sqlite": str(sqlite_path),
                "copytradeSqlite": str(copytrade_sqlite),
                "supabaseUrl": cfg.supabase_url,
                "synced": {
                    "master_results": len(masters),
                    "address_metrics": len(address_metrics),
                    "copytrade_leader_summary": copytrade_summary_synced,
                    "copytrade_daily_equity": daily_equity_synced,
                    "copytrade_daily_leader_pnl": daily_leader_pnl_synced,
                    "copytrade_daily_leader_market_leg_pnl": daily_leader_market_leg_pnl_synced,
                    "copytrade_compare_daily_summary": compare_daily_summary_synced,
                    "copytrade_compare_daily_market_leg": compare_daily_market_leg_synced,
                },
                "cleanup": {
                    "skipped_low_pnl_before": skipped_before,
                    "skipped_low_pnl_after": skipped_after,
                },
                "tables": {
                    "copytrade_local_tables_ok": copytrade_local_tables_ok,
                    "copytrade_leader_summary_exists": has_copytrade_summary_table,
                    "copytrade_daily_local_table_exists": copytrade_daily_local_table_exists,
                    "copytrade_leg_detail_local_table_exists": copytrade_leg_detail_local_table_exists,
                    "copytrade_compare_summary_local_table_exists": copytrade_compare_summary_local_table_exists,
                    "copytrade_compare_market_leg_local_table_exists": copytrade_compare_market_leg_local_table_exists,
                },
                "safety": {
                    "allow_empty_copytrade_daily_purge": cfg.allow_empty_copytrade_daily_purge,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv[1:]))

