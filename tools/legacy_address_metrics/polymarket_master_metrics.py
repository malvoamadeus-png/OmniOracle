import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from polymarket_metrics import (
    Config as MetricsConfig,
    USER_PNL_METRICS_COMPAT_VERSION,
    USER_PNL_METRICS_FIDELITY,
    USER_PNL_METRICS_INTERVAL,
    compute_and_save_metrics,
    compute_equity_r_squared,
    compute_pnl_drawdown_sharpe,
    compute_ulcer_index,
    ensure_schema as ensure_metrics_schema,
    fetch_user_pnl_series,
    is_user_pnl_metrics_compatible,
)

try:
    import tomllib
except Exception:  # pragma: no cover
    import tomli as tomllib  # type: ignore

TOOL_DIR = Path(__file__).resolve().parent
TOOLS_DIR = TOOL_DIR.parent
PROJECT_ROOT = TOOLS_DIR.parent

DEFAULT_MASTER_CONFIG = TOOL_DIR / "polymarket_master_config.json"
METRICS_SCRIPT = TOOL_DIR / "polymarket_metrics.py"
SMART_MONEY_DIR = TOOLS_DIR / "smart_money_broadcast"
SMART_MONEY_CONFIG_DIR = SMART_MONEY_DIR / "config"
SMART_MONEY_THRESHOLDS_JSON = SMART_MONEY_CONFIG_DIR / "copytrade_value_thresholds.json"
ADDRESS_TAGS_CACHE_TABLE = "address_tags_cache"
TAGS_SYNC_PAGE_SIZE = 1000
EXCLUDED_TAGS = {"\u6392\u9664", "\u7279\u6b8a\u7b56\u7565"}
IGNORED_SOURCE_TAGS = {"BACKFILL"}
CT_SCORE_MIN_TOTAL_PNL = 80000.0
CT_SCORE_BLACKLISTED_ADDRESSES = {
    "0xa5ef39c3d3e10d0b270233af41cac69796b12966",
}
COPYTRADE_VALUE_SCORE_VERSION = "copytrade_value_v1"
COPYTRADE_VALUE_COLUMNS = [
    "copytrade_value_score",
    "copytrade_value_level",
    "copytrade_value_exclusion_reason",
    "copytrade_value_score_version",
]
COPYTRADE_VALUE_METRICS: List[Tuple[str, str]] = [
    ("sharpe", "high"),
    ("realized_edge_score", "high"),
    ("roi", "high"),
    ("profit_factor", "high"),
    ("max_drawdown", "low"),
    ("ulcer_index", "low"),
]
COPYTRADE_VALUE_MIN_AVAILABLE_METRICS = 3
COPYTRADE_VALUE_HIGH_RATIO = 0.33
COPYTRADE_VALUE_MEDIUM_RATIO = 0.66
MASTER_METRICS_MAX_WORKERS = 5
CT_SCORE_COLUMNS = [
    "ct_score_total_100",
    "ct_score_roi",
    "ct_score_pf",
    "ct_score_mdd",
    "ct_score_sharpe",
    "ct_score_ui",
    "ct_score_r2",
]
CT_SCORE_METRICS: List[Tuple[str, str, str]] = [
    ("roi", "ct_score_roi", "high"),
    ("profit_factor", "ct_score_pf", "high"),
    ("max_drawdown", "ct_score_mdd", "low"),
    ("sharpe", "ct_score_sharpe", "high"),
    ("ulcer_index", "ct_score_ui", "low"),
    ("equity_r2", "ct_score_r2", "high"),
]


def _load_dotenv(dotenv_path: Path) -> None:
    try:
        if not dotenv_path.exists():
            return
        for raw in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
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


_load_dotenv(TOOL_DIR / ".env")


def _normalize_source_tag(tag: str) -> str:
    return (tag or "").strip().upper()


def _merge_source_tags(existing: str, new_tag: str) -> str:
    nt = _normalize_source_tag(new_tag)
    tags = [t.strip().upper() for t in (existing or "").split(",") if t.strip()]
    tags = [t for t in tags if t not in IGNORED_SOURCE_TAGS]
    if nt in IGNORED_SOURCE_TAGS:
        return ",".join(tags)
    if nt and nt not in tags:
        tags.append(nt)
    return ",".join(tags)


def ensure_address_tags_cache_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {ADDRESS_TAGS_CACHE_TABLE} (
                address TEXT PRIMARY KEY,
                tag TEXT NOT NULL,
                tag_updated_at TEXT,
                synced_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _supabase_headers_for_read() -> Optional[Dict[str, str]]:
    supabase_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not key:
        key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if not supabase_url or not key:
        return None
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "accept": "application/json",
    }


def _fetch_address_tags_from_supabase() -> Optional[Dict[str, Tuple[str, Optional[str]]]]:
    supabase_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    headers = _supabase_headers_for_read()
    if not supabase_url or headers is None:
        return None

    out: Dict[str, Tuple[str, Optional[str]]] = {}
    offset = 0
    while True:
        params = {
            "select": "address,tag,updated_at",
            "limit": str(TAGS_SYNC_PAGE_SIZE),
            "offset": str(offset),
        }
        resp = requests.get(
            f"{supabase_url}/rest/v1/address_tags",
            headers=headers,
            params=params,
            timeout=30,
        )
        if resp.status_code >= 400:
            return None
        data = resp.json()
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if not isinstance(row, dict):
                continue
            addr = row.get("address")
            tag = row.get("tag")
            if not isinstance(addr, str) or not addr.strip():
                continue
            if not isinstance(tag, str) or not tag.strip():
                continue
            updated_at = row.get("updated_at")
            out[addr.lower()] = (tag.strip(), str(updated_at) if updated_at is not None else None)
        if len(data) < TAGS_SYNC_PAGE_SIZE:
            break
        offset += TAGS_SYNC_PAGE_SIZE
    return out


def _replace_local_address_tags_cache(
    db_path: Path, tags_map: Dict[str, Tuple[str, Optional[str]]]
) -> None:
    ensure_address_tags_cache_schema(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    rows = [
        (
            addr.lower(),
            tag,
            tag_updated_at,
            now_iso,
        )
        for addr, (tag, tag_updated_at) in tags_map.items()
    ]
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"DELETE FROM {ADDRESS_TAGS_CACHE_TABLE}")
        if rows:
            conn.executemany(
                f"INSERT INTO {ADDRESS_TAGS_CACHE_TABLE}(address, tag, tag_updated_at, synced_at) VALUES(?, ?, ?, ?)",
                rows,
            )
        conn.commit()


def _load_local_address_tags_cache(db_path: Path) -> Dict[str, str]:
    ensure_address_tags_cache_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT address, tag FROM {ADDRESS_TAGS_CACHE_TABLE}"
        ).fetchall()
    out: Dict[str, str] = {}
    for address, tag in rows:
        if isinstance(address, str) and address.strip() and isinstance(tag, str) and tag.strip():
            out[address.lower()] = tag.strip()
    return out


def load_effective_address_tags(db_path: Path) -> Tuple[Dict[str, str], str]:
    remote = _fetch_address_tags_from_supabase()
    if remote is not None:
        _replace_local_address_tags_cache(db_path, remote)
        return {k: v[0] for k, v in remote.items()}, "remote"
    local = _load_local_address_tags_cache(db_path)
    return local, "local_cache"


@dataclass
class SourceConfig:
    name: str
    script: str
    args: Dict[str, Any]


@dataclass
class MasterConfig:
    db_path: str
    sync_supabase: bool
    per_address_sleep_s: float
    metrics_timeout_s: int
    cache_max_age_days: int
    metrics_max_workers: int
    sources: List[SourceConfig]


def load_master_config(path: Path) -> MasterConfig:
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("配置文件格式错误：根对象必须是 JSON object")
    raw_sources = data.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SystemExit("配置文件错误：sources 必须是非空数组")

    sources: List[SourceConfig] = []
    for idx, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"sources[{idx}] 必须是 object")
        name = str(item.get("name") or "").strip()
        script = str(item.get("script") or "").strip()
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        if not name or not script:
            raise SystemExit(f"sources[{idx}] 缺少 name 或 script")
        sources.append(SourceConfig(name=name, script=script, args=dict(args)))

    return MasterConfig(
        db_path=str(data.get("db_path") or "metrics_fresh.sqlite"),
        sync_supabase=bool(data.get("sync_supabase", False)),
        per_address_sleep_s=float(data.get("per_address_sleep_s", 0.1)),
        metrics_timeout_s=int(data.get("metrics_timeout_s", 300)),
        cache_max_age_days=int(data.get("cache_max_age_days", 30)),
        metrics_max_workers=int(data.get("metrics_max_workers", MASTER_METRICS_MAX_WORKERS)),
        sources=sources,
    )


def ensure_address_source_schema(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_metrics_schema(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS copytrade_value_thresholds (
                score_version TEXT PRIMARY KEY,
                high_threshold REAL,
                medium_threshold REAL,
                calibration_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def ensure_address_source_tags_for_one(db_path: Path, address: str, source_tags: List[str]) -> None:
    tags = [_normalize_source_tag(t) for t in source_tags if _normalize_source_tag(t)]
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM address_metrics WHERE address=? LIMIT 1", (address.lower(),)).fetchone()
        if row is None:
            return
        # 使用“本次地址精确来源”覆盖，避免全局 merge 造成来源污染
        normalized = ",".join(sorted(set(tags))) if tags else None
        conn.execute(
            "UPDATE address_metrics SET source_tags=?, updated_at=? WHERE address=?",
            (normalized, datetime.now(timezone.utc).isoformat(), address.lower()),
        )
        conn.commit()


def backfill_all_address_sources(db_path: Path, source_tags: Set[str]) -> None:
    tags = [_normalize_source_tag(t) for t in source_tags if _normalize_source_tag(t)]
    if not tags:
        return
    ensure_address_source_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT address, source_tags FROM address_metrics").fetchall()
        now_iso = datetime.now(timezone.utc).isoformat()
        updates = []
        for address, existing in rows:
            if not address:
                continue
            merged = str(existing or "")
            for t in tags:
                merged = _merge_source_tags(merged, t)
            if merged != (existing or ""):
                updates.append((merged, now_iso, address))
        if updates:
            conn.executemany("UPDATE address_metrics SET source_tags=?, updated_at=? WHERE address=?", updates)
            conn.commit()


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def load_cached_metrics(address: str, db_path: Path, max_age_days: int) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    with sqlite3.connect(db_path) as conn:
        ensure_metrics_schema(conn)
        row = conn.execute(
            "SELECT snapshot_utc, total_pnl, details_json, updated_at FROM address_metrics WHERE address=? LIMIT 1",
            (address.lower(),),
        ).fetchone()
        if not row:
            return {"hit": False}
        snapshot_utc, total_pnl, details_json, updated_at = row
        # 缓存新鲜度只基于指标快照时间，避免 source_tags 更新影响 30 天失效逻辑。
        freshness_dt = _parse_dt(snapshot_utc) if snapshot_utc else _parse_dt(updated_at)
        if freshness_dt < cutoff:
            return {"hit": False}
        details = None
        if isinstance(details_json, str) and details_json.strip():
            try:
                details = json.loads(details_json)
            except Exception:
                details = None
        if not is_user_pnl_metrics_compatible(details):
            return {"hit": False, "reason": "user_pnl_compat_mismatch"}
        final_pnl = total_pnl
        if details and isinstance(details.get("pnlCurveLast"), (list, tuple)) and len(details["pnlCurveLast"]) >= 2:
            final_pnl = details["pnlCurveLast"][1]
        return {"hit": True, "metrics": {"total_pnl": final_pnl, "details": details}, "snapshot_utc": snapshot_utc}


def _arg_key_to_cli(name: str) -> str:
    key = str(name).strip().replace("_", "-")
    return key if key.startswith("--") else f"--{key}"


def _has_nonempty_slug_prefixes(args: Dict[str, Any]) -> bool:
    value = args.get("slug_prefixes")
    if isinstance(value, str):
        return any(part.strip() for part in value.split(","))
    if isinstance(value, (list, tuple)):
        return any(isinstance(part, str) and part.strip() for part in value)
    return False


def _is_crypto_source(source: SourceConfig) -> bool:
    return _has_nonempty_slug_prefixes(source.args)


def _build_cmd_for_source(source: SourceConfig, out_file: Path) -> List[str]:
    cmd = [sys.executable, source.script]
    for k, v in source.args.items():
        flag = _arg_key_to_cli(k)
        if isinstance(v, bool):
            if v:
                cmd.append(flag)
            continue
        if v is None:
            continue
        cmd.extend([flag, str(v)])
    cmd.extend(["--out", str(out_file)])
    return cmd


def run_source_top_holders(source: SourceConfig) -> Dict[str, Any]:
    out_file = TOOL_DIR / f"_top_holders_{source.name.lower()}_{int(time.time())}.json"
    cmd = _build_cmd_for_source(source, out_file)
    p = subprocess.run(cmd, check=False)
    if p.returncode != 0:
        return {"success": False, "error": f"source={source.name} exit={p.returncode}"}
    if not out_file.exists():
        return {"success": False, "error": f"source={source.name} output file not found"}
    data = json.loads(out_file.read_text(encoding="utf-8"))
    try:
        out_file.unlink()
    except Exception:
        pass
    return {"success": True, "data": data}


def extract_unique_addresses(markets_payload: Dict[str, Any]) -> List[str]:
    addresses: Set[str] = set()
    for market in markets_payload.get("markets", []):
        if not isinstance(market, dict):
            continue
        for token_data in market.get("topHoldersByToken", []):
            if not isinstance(token_data, dict):
                continue
            for holder in token_data.get("holders", []):
                if isinstance(holder, dict):
                    addr = holder.get("proxyWallet")
                    if isinstance(addr, str) and addr.strip():
                        addresses.add(addr.lower())
    return list(addresses)


def run_metrics_for_address(cfg: MasterConfig, address: str, source_tags: List[str], db_path: Path) -> Dict[str, Any]:
    cached = load_cached_metrics(address, db_path=db_path, max_age_days=cfg.cache_max_age_days)
    tags_norm = [_normalize_source_tag(t) for t in source_tags if _normalize_source_tag(t)]
    if cached.get("hit"):
        ensure_address_source_tags_for_one(db_path, address, tags_norm)
        return {"success": True, "address": address, "metrics": cached.get("metrics", {}), "summary": {"cached": True}}

    primary_tag = tags_norm[0] if tags_norm else "UNKNOWN"
    try:
        result = compute_and_save_metrics(
            MetricsConfig(
                address=str(address).lower(),
                db_path=str(db_path),
                min_usd=1.0,
                price_history_days=90,
                mdd_mode="cashflow",
                debug=False,
                progress=True,
                source_tag=primary_tag,
            )
        )
    except Exception as e:
        return {"success": False, "address": address, "error": f"subprocess exception: {e}"}
    metrics = result.get("metrics", {})
    details = metrics.get("details")
    if details and isinstance(details.get("pnlCurveLast"), (list, tuple)) and len(details["pnlCurveLast"]) >= 2:
        metrics["total_pnl"] = details["pnlCurveLast"][1]
    ensure_address_source_tags_for_one(db_path, address, tags_norm)
    return {"success": True, "address": address, "metrics": metrics, "summary": result.get("summary", {})}


def _extract_final_pnl(metric_result: Dict[str, Any]) -> float:
    m = metric_result.get("metrics", {})
    d = m.get("details")
    if d and isinstance(d.get("pnlCurveLast"), (list, tuple)) and len(d["pnlCurveLast"]) >= 2:
        return float(d["pnlCurveLast"][1])
    return float(m.get("total_pnl", 0.0) or 0.0)


def _to_float_or_zero(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0.0
        try:
            return float(s)
        except Exception:
            return 0.0
    return 0.0


def _to_optional_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v if math.isfinite(v) else None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            v = float(s)
            return v if math.isfinite(v) else None
        except Exception:
            return None
    return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _linear_quantile(sorted_values: List[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    q = max(0.0, min(1.0, float(q)))
    pos = (len(sorted_values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    weight = pos - lo
    return float(sorted_values[lo] * (1.0 - weight) + sorted_values[hi] * weight)


def _rank_score(values: List[float], value: float, direction: str) -> Optional[float]:
    finite = [float(v) for v in values if math.isfinite(float(v))]
    if not finite or not math.isfinite(float(value)):
        return None
    if direction == "high":
        better_or_equal = sum(1 for item in finite if item <= float(value))
    else:
        better_or_equal = sum(1 for item in finite if item >= float(value))
    return max(0.0, min(100.0, better_or_equal / len(finite) * 100.0))


def _threshold_from_scores(sorted_scores_desc: List[float], ratio: float) -> Optional[float]:
    if not sorted_scores_desc:
        return None
    idx = max(0, math.ceil(len(sorted_scores_desc) * float(ratio)) - 1)
    idx = min(idx, len(sorted_scores_desc) - 1)
    return float(sorted_scores_desc[idx])


def _copytrade_value_exclusion_reason(row: sqlite3.Row) -> Optional[str]:
    total_trades = _to_optional_float(row["total_trades"])
    max_drawdown = _to_optional_float(row["max_drawdown"])
    avg_trade_price = _to_optional_float(row["avg_trade_price"])
    current_value = _to_optional_float(row["current_position_value_usd"])

    if total_trades is None:
        return "missing_total_trades"
    if total_trades < 50.0:
        return "total_trades_lt_50"
    if max_drawdown is None:
        return "missing_max_drawdown"
    if max_drawdown > 1.0:
        return "max_drawdown_gt_100pct"
    if avg_trade_price is None:
        return "missing_avg_trade_price"
    if avg_trade_price > 0.9:
        return "avg_trade_price_gt_0.9"
    if avg_trade_price < 0.1:
        return "avg_trade_price_lt_0.1"
    if current_value is None:
        return "missing_current_position_value_usd"
    if current_value < 500.0:
        return "current_position_value_usd_lt_500"
    return None


def _write_copytrade_value_threshold_snapshot(payload: Dict[str, Any]) -> None:
    SMART_MONEY_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SMART_MONEY_THRESHOLDS_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_copytrade_value_thresholds(
    conn: sqlite3.Connection,
    *,
    score_version: str,
    high_threshold: Optional[float],
    medium_threshold: Optional[float],
    calibration_payload: Dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO copytrade_value_thresholds(score_version, high_threshold, medium_threshold, calibration_json, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(score_version) DO UPDATE SET
            high_threshold=excluded.high_threshold,
            medium_threshold=excluded.medium_threshold,
            calibration_json=excluded.calibration_json,
            updated_at=excluded.updated_at
        """,
        (
            score_version,
            high_threshold,
            medium_threshold,
            json.dumps(calibration_payload, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def _merge_copytrade_value_summary_into_latest_master_result(
    conn: sqlite3.Connection,
    summary: Dict[str, Any],
) -> None:
    table_row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='master_results' LIMIT 1"
    ).fetchone()
    if table_row is None:
        return
    row = conn.execute(
        "SELECT id, metrics_summary_json FROM master_results ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    metrics_summary: Dict[str, Any] = {}
    raw_summary = row[1]
    if isinstance(raw_summary, str) and raw_summary.strip():
        try:
            parsed = json.loads(raw_summary)
            if isinstance(parsed, dict):
                metrics_summary = parsed
        except Exception:
            metrics_summary = {}
    metrics_summary["copytrade_value_thresholds"] = summary
    conn.execute(
        "UPDATE master_results SET metrics_summary_json=? WHERE id=?",
        (json.dumps(metrics_summary, ensure_ascii=False), int(row[0])),
    )


def apply_copytrade_value_scoring(db_path: Path) -> Dict[str, Any]:
    ensure_address_source_schema(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE address_metrics SET "
            "copytrade_value_score=NULL, copytrade_value_level=NULL, "
            "copytrade_value_exclusion_reason=NULL, copytrade_value_score_version=NULL"
        )

        rows = conn.execute(
            "SELECT address, confidence, current_position_value_usd, total_trades, avg_trade_price, "
            "realized_edge_score, roi, profit_factor, max_drawdown, sharpe, ulcer_index "
            "FROM address_metrics"
        ).fetchall()

        metric_values: Dict[str, List[float]] = {metric: [] for metric, _ in COPYTRADE_VALUE_METRICS}
        eligible_rows: List[sqlite3.Row] = []
        for row in rows:
            address = str(row["address"] or "").strip().lower()
            if not address or address in CT_SCORE_BLACKLISTED_ADDRESSES:
                continue
            if str(row["confidence"] or "").strip().lower() == "skipped_low_pnl":
                continue
            reason = _copytrade_value_exclusion_reason(row)
            if reason is not None:
                continue
            eligible_rows.append(row)
            for metric, _direction in COPYTRADE_VALUE_METRICS:
                value = _to_optional_float(row[metric])
                if value is not None:
                    metric_values[metric].append(value)

        scored_rows: List[Tuple[str, Optional[float], str, Optional[str]]] = []
        calibration_scores: List[float] = []
        for row in rows:
            address = str(row["address"] or "").strip().lower()
            if not address:
                continue
            reason = _copytrade_value_exclusion_reason(row)
            if reason is not None:
                scored_rows.append((address, None, "not_worth_copying", reason))
                continue

            component_scores: Dict[str, float] = {}
            for metric, direction in COPYTRADE_VALUE_METRICS:
                value = _to_optional_float(row[metric])
                if value is None:
                    continue
                score = _rank_score(metric_values[metric], value, direction)
                if score is not None:
                    component_scores[metric] = score

            if len(component_scores) < COPYTRADE_VALUE_MIN_AVAILABLE_METRICS:
                scored_rows.append((address, None, "not_worth_copying", "insufficient_score_metrics"))
                continue

            total_score = sum(component_scores.values()) / len(component_scores)
            scored_rows.append((address, total_score, "", None))
            calibration_scores.append(total_score)

        calibration_scores.sort(reverse=True)
        high_threshold = _threshold_from_scores(calibration_scores, COPYTRADE_VALUE_HIGH_RATIO)
        medium_threshold = _threshold_from_scores(calibration_scores, COPYTRADE_VALUE_MEDIUM_RATIO)

        updates: List[Tuple[Optional[float], str, Optional[str], str, str]] = []
        scored_count = 0
        excluded_count = 0
        for address, score, level, reason in scored_rows:
            resolved_level = level
            if score is not None:
                scored_count += 1
                if high_threshold is not None and score >= high_threshold:
                    resolved_level = "high"
                elif medium_threshold is not None and score >= medium_threshold:
                    resolved_level = "medium"
                else:
                    resolved_level = "low"
            else:
                excluded_count += 1
                resolved_level = resolved_level or "not_worth_copying"
            updates.append((score, resolved_level, reason, COPYTRADE_VALUE_SCORE_VERSION, address))

        if updates:
            conn.executemany(
                "UPDATE address_metrics SET "
                "copytrade_value_score=?, copytrade_value_level=?, "
                "copytrade_value_exclusion_reason=?, copytrade_value_score_version=? "
                "WHERE address=?",
                updates,
            )

        summary = {
            "score_version": COPYTRADE_VALUE_SCORE_VERSION,
            "calibration_pool_size": len(eligible_rows),
            "scored_addresses": scored_count,
            "excluded_addresses": excluded_count,
            "high_threshold": high_threshold,
            "medium_threshold": medium_threshold,
            "metrics": [
                {"field": metric, "direction": direction}
                for metric, direction in COPYTRADE_VALUE_METRICS
            ],
            "hard_exclusions": [
                "total_trades < 50",
                "max_drawdown > 1.0",
                "avg_trade_price > 0.9",
                "avg_trade_price < 0.1",
                "current_position_value_usd < 500",
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        calibration_payload = {
            **summary,
            "metric_samples": {
                metric: sorted(float(v) for v in values if math.isfinite(float(v)))
                for metric, values in metric_values.items()
            },
        }
        _save_copytrade_value_thresholds(
            conn,
            score_version=COPYTRADE_VALUE_SCORE_VERSION,
            high_threshold=high_threshold,
            medium_threshold=medium_threshold,
            calibration_payload=calibration_payload,
        )
        _merge_copytrade_value_summary_into_latest_master_result(conn, summary)
        conn.commit()
    _write_copytrade_value_threshold_snapshot(calibration_payload)
    return summary


def _load_copytrade_leader_addresses(accounts_dir: Path) -> Set[str]:
    leaders: Set[str] = set()
    if not accounts_dir.exists():
        return leaders
    for toml_file in sorted(accounts_dir.glob("*.toml")):
        if toml_file.name.startswith("_"):
            continue
        try:
            cfg = tomllib.loads(toml_file.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"failed to parse {toml_file}: {e}") from e
        for addr in cfg.get("leader_addresses", []):
            if not isinstance(addr, str):
                continue
            s = addr.strip().lower()
            if s:
                leaders.add(s)
    return leaders


def apply_copytrade_scoring(db_path: Path) -> Dict[str, Any]:
    ensure_address_source_schema(db_path)
    leader_addresses = _load_copytrade_leader_addresses(PROJECT_ROOT / "backend" / "packages" / "copytrade" / "accounts")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE address_metrics SET "
            "ct_score_total_100=NULL, ct_score_roi=NULL, ct_score_pf=NULL, ct_score_mdd=NULL, "
            "ct_score_sharpe=NULL, ct_score_ui=NULL, ct_score_r2=NULL"
        )

        if not leader_addresses:
            conn.commit()
            return {
                "leaders_configured": 0,
                "leaders_found_in_metrics": 0,
                "targets_scored": 0,
                "targets_eligible": 0,
                "degenerate_metrics": [m for m, _, _ in CT_SCORE_METRICS],
            }

        leader_list = sorted(leader_addresses)
        leader_placeholders = ",".join("?" for _ in leader_list)
        leader_rows = conn.execute(
            "SELECT address, roi, profit_factor, max_drawdown, sharpe, ulcer_index, equity_r2 "
            f"FROM address_metrics WHERE address IN ({leader_placeholders})",
            leader_list,
        ).fetchall()

        metric_quantiles: Dict[str, Dict[str, Any]] = {}
        degenerate_metrics: List[str] = []
        for metric, _, _ in CT_SCORE_METRICS:
            vals = sorted(
                v
                for v in (_to_optional_float(row[metric]) for row in leader_rows)
                if v is not None
            )
            p25 = _linear_quantile(vals, 0.25)
            p75 = _linear_quantile(vals, 0.75)
            degenerate = (
                p25 is None
                or p75 is None
                or not math.isfinite(float(p25))
                or not math.isfinite(float(p75))
                or abs(float(p75) - float(p25)) <= 1e-12
            )
            if degenerate:
                degenerate_metrics.append(metric)
            metric_quantiles[metric] = {
                "p25": p25,
                "p75": p75,
                "degenerate": degenerate,
            }

        target_rows = conn.execute(
            "SELECT address, total_pnl, confidence, roi, profit_factor, max_drawdown, sharpe, ulcer_index, equity_r2 "
            "FROM address_metrics"
        ).fetchall()

        updates: List[Tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], str]] = []
        for row in target_rows:
            address = str(row["address"] or "").strip().lower()
            if not address:
                continue
            if address in CT_SCORE_BLACKLISTED_ADDRESSES:
                continue
            if str(row["confidence"] or "").strip().lower() == "skipped_low_pnl":
                continue
            total_pnl = _to_optional_float(row["total_pnl"])
            if total_pnl is not None and total_pnl < CT_SCORE_MIN_TOTAL_PNL:
                continue

            row_scores: Dict[str, Optional[float]] = {}
            available_scores: List[float] = []
            for metric, score_col, direction in CT_SCORE_METRICS:
                q = metric_quantiles[metric]
                p25 = _to_optional_float(q.get("p25"))
                p75 = _to_optional_float(q.get("p75"))
                if bool(q.get("degenerate")):
                    score = 0.5
                else:
                    value = _to_optional_float(row[metric])
                    if value is None or p25 is None or p75 is None:
                        score = None
                    else:
                        denom = p75 - p25
                        if direction == "high":
                            score = _clamp01((value - p25) / denom)
                        else:
                            score = _clamp01((p75 - value) / denom)
                row_scores[score_col] = score
                if score is not None and math.isfinite(score):
                    available_scores.append(score)

            total_score = (sum(available_scores) / len(available_scores) * 100.0) if available_scores else None
            updates.append(
                (
                    total_score,
                    row_scores.get("ct_score_roi"),
                    row_scores.get("ct_score_pf"),
                    row_scores.get("ct_score_mdd"),
                    row_scores.get("ct_score_sharpe"),
                    row_scores.get("ct_score_ui"),
                    row_scores.get("ct_score_r2"),
                    address,
                )
            )

        if updates:
            conn.executemany(
                "UPDATE address_metrics SET "
                "ct_score_total_100=?, ct_score_roi=?, ct_score_pf=?, ct_score_mdd=?, "
                "ct_score_sharpe=?, ct_score_ui=?, ct_score_r2=? "
                "WHERE address=?",
                updates,
            )

        conn.commit()
        return {
            "leaders_configured": len(leader_addresses),
            "leaders_found_in_metrics": len(leader_rows),
            "targets_scored": len(updates),
            "targets_eligible": len(updates),
            "degenerate_metrics": degenerate_metrics,
        }


def save_master_results(
    db_path: Path,
    source_names: List[str],
    merged_payload: Dict[str, Any],
    all_metrics: List[Dict[str, Any]],
    source_market_counts: Dict[str, int],
    source_address_counts: Dict[str, int],
    excluded_count: int = 0,
) -> None:
    successful = [m for m in all_metrics if m.get("success")]
    successful_count = len(successful)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS master_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                sport TEXT,
                limit_count INTEGER,
                total_holders INTEGER,
                successful_metrics INTEGER,
                failed_metrics INTEGER,
                nba_markets_json TEXT,
                top_holders_json TEXT,
                metrics_summary_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO master_results (
                timestamp, sport, limit_count, total_holders,
                successful_metrics, failed_metrics, nba_markets_json,
                top_holders_json, metrics_summary_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                ",".join(source_names),
                None,
                len(extract_unique_addresses(merged_payload)),
                successful_count,
                sum(1 for m in all_metrics if not m.get("success")),
                json.dumps(
                    [
                        {"id": m.get("id"), "conditionId": m.get("conditionId"), "question": m.get("question"), "slug": m.get("slug")}
                        for m in merged_payload.get("markets", [])
                        if isinstance(m, dict)
                    ],
                    ensure_ascii=False,
                ),
                json.dumps(extract_unique_addresses(merged_payload), ensure_ascii=False),
                json.dumps(
                    {
                        "total_addresses": len(all_metrics),
                        "excluded_by_tag": int(excluded_count),
                        "successful": successful_count,
                        "failed": sum(1 for m in all_metrics if not m.get("success")),
                        "avg_pnl": sum(_extract_final_pnl(m) for m in successful) / max(successful_count, 1),
                        "avg_roi": sum(_to_float_or_zero(m.get("metrics", {}).get("roi")) for m in successful)
                        / max(successful_count, 1),
                        "sources": source_names,
                        "source_market_counts": source_market_counts,
                        "source_address_counts": source_address_counts,
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()


def compact_db(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS raw_trades")
        conn.execute("DROP TABLE IF EXISTS raw_positions")
        conn.execute("DROP TABLE IF EXISTS metrics_runs")
        conn.execute("DROP TABLE IF EXISTS metrics_results")
        conn.execute("DROP TABLE IF EXISTS holder_metrics")
        conn.commit()
        conn.execute("VACUUM")


def reset_realized_edge_data(db_path: Path) -> Dict[str, Any]:
    dropped_tables: List[str] = []
    with sqlite3.connect(str(db_path)) as conn:
        for table in (
            "address_metrics",
            "master_results",
            "pm_open_positions_cache",
            "pm_closed_positions_cache",
            "pm_closed_sync_state",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
            dropped_tables.append(table)
        conn.execute("DROP TABLE IF EXISTS copytrade_value_thresholds")
        dropped_tables.append("copytrade_value_thresholds")
        conn.commit()
        conn.execute("VACUUM")

    smart_money_db = SMART_MONEY_DIR / "runtime" / "smart_money.sqlite"
    if smart_money_db.exists():
        smart_money_db.unlink()

    removed_reports = 0
    output_dir = SMART_MONEY_DIR / "output"
    if output_dir.exists():
        for path in output_dir.glob("*.md"):
            path.unlink()
            removed_reports += 1

    if SMART_MONEY_THRESHOLDS_JSON.exists():
        SMART_MONEY_THRESHOLDS_JSON.unlink()

    return {
        "sqlite": str(db_path),
        "dropped_tables": dropped_tables,
        "smart_money_db_deleted": not smart_money_db.exists(),
        "smart_money_reports_deleted": removed_reports,
    }


def purge_supabase_main_metrics(required: bool) -> None:
    supabase_url = (os.getenv("SUPABASE_URL") or "").strip()
    service_role_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not supabase_url or not service_role_key:
        if required:
            anon_key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
            if anon_key and not service_role_key:
                raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY (SUPABASE_ANON_KEY is dashboard read-only)")
            raise RuntimeError("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return
    script = PROJECT_ROOT / "supabase" / "sync_to_supabase.py"
    p = subprocess.run([sys.executable, str(script), "--purge-main-metrics"], check=False)
    if p.returncode != 0:
        raise RuntimeError(f"Supabase purge failed: exit code {p.returncode}")


def sync_supabase(db_path: Path, required: bool) -> None:
    supabase_url = (os.getenv("SUPABASE_URL") or "").strip()
    service_role_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not supabase_url or not service_role_key:
        if required:
            anon_key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
            if anon_key and not service_role_key:
                raise RuntimeError("Missing SUPABASE_SERVICE_ROLE_KEY (SUPABASE_ANON_KEY is dashboard read-only)")
            raise RuntimeError("Missing SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        return
    script = PROJECT_ROOT / "supabase" / "sync_to_supabase.py"
    p = subprocess.run([sys.executable, str(script), "--sqlite", str(db_path)], check=False)
    if p.returncode != 0:
        raise RuntimeError(f"Supabase sync failed: exit code {p.returncode}")


def repair_user_pnl_metrics(db_path: Path) -> Dict[str, Any]:
    ensure_address_source_schema(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        ensure_metrics_schema(conn)
        rows = conn.execute(
            "SELECT address, total_pnl, details_json, confidence "
            "FROM address_metrics "
            "WHERE lower(coalesce(confidence, '')) != 'skipped_low_pnl' "
            "ORDER BY address"
        ).fetchall()

        total = len(rows)
        if total <= 0:
            return {
                "updated": 0,
                "failed": 0,
                "addresses": 0,
                "compat_version": USER_PNL_METRICS_COMPAT_VERSION,
                "interval": USER_PNL_METRICS_INTERVAL,
                "fidelity": USER_PNL_METRICS_FIDELITY,
            }

        session = requests.Session()
        updated = 0
        failed: List[Dict[str, str]] = []
        try:
            for idx, row in enumerate(rows, start=1):
                address = str(row["address"] or "").strip().lower()
                if not address:
                    continue
                print(f"[repair-user-pnl] {idx}/{total} {address[:10]}...")
                try:
                    details = None
                    details_json = row["details_json"]
                    if isinstance(details_json, str) and details_json.strip():
                        try:
                            details = json.loads(details_json)
                        except Exception:
                            details = None
                    if not isinstance(details, dict):
                        details = {}

                    pnl_series = fetch_user_pnl_series(
                        session,
                        address,
                        interval=USER_PNL_METRICS_INTERVAL,
                        fidelity=USER_PNL_METRICS_FIDELITY,
                    )
                    max_drawdown = None
                    sharpe = None
                    dd_usd = None
                    dd_peak = None
                    ulcer_index = None
                    equity_r2 = None
                    final_pnl = float(row["total_pnl"] or 0.0)
                    if pnl_series:
                        final_pnl = float(pnl_series[-1][1])
                        max_drawdown, dd_usd, dd_peak, sharpe = compute_pnl_drawdown_sharpe(
                            pnl_series,
                            annualization_periods=730.0,
                        )
                        ulcer_index = compute_ulcer_index(pnl_series, total_pnl=final_pnl)
                        equity_r2 = compute_equity_r_squared(pnl_series)

                    details["userPnlCompatVersion"] = USER_PNL_METRICS_COMPAT_VERSION
                    details["pnlCurveInterval"] = USER_PNL_METRICS_INTERVAL
                    details["pnlCurveFidelity"] = USER_PNL_METRICS_FIDELITY
                    details["pnlCurveAnnualizationPeriods"] = 730.0 if pnl_series else None
                    details["pnlCurvePoints"] = len(pnl_series) if pnl_series else 0
                    if pnl_series:
                        details["pnlCurveLast"] = pnl_series[-1]
                        details["maxDrawdownUsd"] = dd_usd
                        details["drawdownPeakPnlUsd"] = dd_peak
                    details["ulcerIndex"] = ulcer_index
                    details["equityR2"] = equity_r2

                    conn.execute(
                        "UPDATE address_metrics SET "
                        "max_drawdown=?, sharpe=?, ulcer_index=?, equity_r2=?, details_json=?, updated_at=? "
                        "WHERE address=?",
                        (
                            max_drawdown,
                            sharpe,
                            ulcer_index,
                            equity_r2,
                            json.dumps(details, ensure_ascii=False),
                            datetime.now(timezone.utc).isoformat(),
                            address,
                        ),
                    )
                    updated += 1
                except Exception as exc:  # noqa: BLE001
                    failed.append({"address": address, "error": str(exc)})
            conn.commit()
        finally:
            session.close()

    scoring_summary = apply_copytrade_scoring(db_path)
    value_summary = apply_copytrade_value_scoring(db_path)
    return {
        "updated": updated,
        "failed": len(failed),
        "addresses": total,
        "compat_version": USER_PNL_METRICS_COMPAT_VERSION,
        "interval": USER_PNL_METRICS_INTERVAL,
        "fidelity": USER_PNL_METRICS_FIDELITY,
        "failures": failed[:20],
        "scoring": scoring_summary,
        "copytrade_value": value_summary,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Polymarket 主控程序（配置驱动）")
    ap.add_argument("--config", type=str, default=str(DEFAULT_MASTER_CONFIG), help="配置文件路径")
    ap.add_argument("--db", type=str, default=None, help="覆盖配置中的 db_path")
    ap.add_argument("--compact-db", action="store_true", help="清理过程数据并压缩数据库后退出")
    ap.add_argument("--reset-realized-edge-data", action="store_true", help="清空主指标本地数据、CLI 运行数据，并清理 Supabase 主指标表后退出")
    ap.add_argument("--repair-user-pnl-metrics", action="store_true", help="重算本地库中所有 user-pnl 派生指标并刷新 copytrade 评分")
    ap.add_argument("--sync-supabase", action="store_true", help="强制本次运行后同步到 Supabase")
    ap.add_argument("--crypto-only", action="store_true", help="仅处理 Crypto 来源（sources[].args.slug_prefixes）")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_master_config(Path(args.config))
    db_path = Path(args.db or cfg.db_path)
    ensure_address_source_schema(db_path)

    if args.reset_realized_edge_data:
        summary = reset_realized_edge_data(db_path)
        purge_supabase_main_metrics(required=True)
        print(json.dumps({**summary, "supabase_main_metrics_purged": True}, ensure_ascii=False))
        return 0

    if args.compact_db:
        compact_db(db_path)
        return 0

    if args.repair_user_pnl_metrics:
        summary = repair_user_pnl_metrics(db_path)
        should_sync = bool(cfg.sync_supabase or args.sync_supabase)
        sync_supabase(db_path, required=should_sync)
        print(json.dumps({**summary, "supabase_synced": should_sync}, ensure_ascii=False))
        return 0

    sources_to_run = cfg.sources
    if args.crypto_only:
        sources_to_run = [s for s in cfg.sources if _is_crypto_source(s)]
        if not sources_to_run:
            print("[run] --crypto-only enabled but no crypto sources found in config")
            return 0
        print(f"[run] --crypto-only enabled, sources={','.join(s.name for s in sources_to_run)}")

    source_markets: Dict[str, List[Dict[str, Any]]] = {}
    source_addresses: Dict[str, Set[str]] = {}
    for source in sources_to_run:
        tag = _normalize_source_tag(source.name)
        source_script = Path(source.script)
        if not source_script.is_absolute():
            source_script = TOOL_DIR / source_script
        source_run = SourceConfig(name=source.name, script=str(source_script), args=source.args)
        if not source_script.exists():
            raise SystemExit(f"来源脚本不存在: {source_script}")
        top_result = run_source_top_holders(source_run)
        if not top_result.get("success"):
            raise SystemExit(f"来源 {source.name} 抓取失败: {top_result.get('error')}")
        payload = top_result.get("data", {})
        source_markets[tag] = [m for m in payload.get("markets", []) if isinstance(m, dict)]
        source_addresses[tag] = set(extract_unique_addresses(payload))

    address_to_sources: Dict[str, Set[str]] = {}
    for tag, addr_set in source_addresses.items():
        for addr in addr_set:
            address_to_sources.setdefault(addr, set()).add(tag)

    unique_addresses = sorted(address_to_sources.keys())
    if not unique_addresses:
        return 0

    tags_map, tags_source = load_effective_address_tags(db_path)
    excluded_addresses = {
        addr
        for addr in unique_addresses
        if str(tags_map.get(addr) or "").strip() in EXCLUDED_TAGS
    }
    if tags_source == "remote":
        print(f"[tags] synced from Supabase: {len(tags_map)}")
    else:
        print(f"[tags] Supabase unavailable, fallback to local cache: {len(tags_map)}")
    if excluded_addresses:
        print(f"[tags] excluded addresses ({','.join(sorted(EXCLUDED_TAGS))}): {len(excluded_addresses)}")

    target_addresses = [addr for addr in unique_addresses if addr not in excluded_addresses]
    if not target_addresses:
        print("[run] no address to process after exclude-tag filtering")
        return 0

    all_metrics: List[Dict[str, Any]] = []
    max_workers = max(1, min(MASTER_METRICS_MAX_WORKERS, int(cfg.metrics_max_workers)))
    print(f"[run] metrics workers={max_workers}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for idx, address in enumerate(target_addresses, start=1):
            print(f"[{idx}/{len(target_addresses)}] queued {address[:10]}...")
            futures[
                pool.submit(run_metrics_for_address, cfg, address, sorted(address_to_sources[address]), db_path)
            ] = (idx, address)
            time.sleep(max(0.0, cfg.per_address_sleep_s))
        for future in as_completed(futures):
            idx, address = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {"success": False, "address": address, "error": f"worker exception: {exc}"}
            all_metrics.append(result)
            if result.get("success"):
                print(f"[{idx}/{len(target_addresses)}] -> ok {address[:10]}...")
            else:
                print(f"[{idx}/{len(target_addresses)}] -> fail {address[:10]}... | {result.get('error', 'unknown')}")

    merged_markets: List[Dict[str, Any]] = []
    for v in source_markets.values():
        merged_markets.extend(v)
    merged_payload = {"markets": merged_markets}

    source_names_sorted = sorted(source_markets.keys())
    source_market_counts = {k: len(v) for k, v in source_markets.items()}
    source_address_counts = {k: len(v) for k, v in source_addresses.items()}
    save_master_results(
        db_path=db_path,
        source_names=source_names_sorted,
        merged_payload=merged_payload,
        all_metrics=all_metrics,
        source_market_counts=source_market_counts,
        source_address_counts=source_address_counts,
        excluded_count=len(excluded_addresses),
    )

    scoring_summary = apply_copytrade_scoring(db_path)
    value_summary = apply_copytrade_value_scoring(db_path)
    print(
        "[score] "
        f"leaders={scoring_summary.get('leaders_found_in_metrics', 0)}/{scoring_summary.get('leaders_configured', 0)} "
        f"targets={scoring_summary.get('targets_scored', 0)} "
        f"degenerate={','.join(scoring_summary.get('degenerate_metrics', [])) or '-'}"
    )
    print(
        "[copytrade-value] "
        f"scored={value_summary.get('scored_addresses', 0)} "
        f"excluded={value_summary.get('excluded_addresses', 0)} "
        f"high={value_summary.get('high_threshold')} "
        f"medium={value_summary.get('medium_threshold')}"
    )

    should_sync = bool(cfg.sync_supabase or args.sync_supabase)
    sync_supabase(db_path, required=should_sync)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

