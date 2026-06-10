from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from copytrade_value import apply_copytrade_value, load_threshold_config, threshold_status
from http_client import DATA_API, USER_PNL_API, ApiClient, normalize_address, to_float


RESOLUTION_EPSILON = 0.05
DEFAULT_CLOSED_POSITIONS_LIMIT = 7500
USER_PNL_INTERVAL = "all"
USER_PNL_FIDELITY = "12h"
METRICS_COMPAT_VERSION = "pm_aligned_user_pnl_all_12h_v2"


@dataclass
class MetricsResult:
    address: str
    metrics: Dict[str, Any]
    details: Dict[str, Any]


def is_plausible_probability(value: Any, *, upper: float = 1.05) -> bool:
    raw = to_float(value)
    return raw is not None and 0.0 <= raw <= upper


def mean(values: Iterable[float]) -> float:
    vals = list(values)
    return sum(vals) / len(vals) if vals else 0.0


def std(values: Iterable[float]) -> float:
    vals = list(values)
    if len(vals) < 2:
        return 0.0
    m = mean(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


def parse_ts(value: Any) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            raw = to_float(text)
            if raw is not None:
                return datetime.fromtimestamp(raw, tz=timezone.utc)
    return None


def fetch_user_pnl_points(
    client: ApiClient,
    address: str,
    *,
    interval: str = USER_PNL_INTERVAL,
    fidelity: str = USER_PNL_FIDELITY,
) -> List[Tuple[datetime, float]]:
    data = client.get_json(
        f"{USER_PNL_API}/user-pnl",
        params={"user_address": normalize_address(address), "interval": interval, "fidelity": fidelity},
        timeout_s=20.0,
        max_retries=3,
    )
    points: List[Tuple[datetime, float]] = []
    if not isinstance(data, list):
        return points
    for row in data:
        if not isinstance(row, dict):
            continue
        ts = to_float(row.get("t"))
        pnl = to_float(row.get("p"))
        if ts is None or pnl is None:
            continue
        points.append((datetime.fromtimestamp(ts, tz=timezone.utc), pnl))
    points.sort(key=lambda item: item[0])
    return points


def interpolate_pnl_at(points: List[Tuple[datetime, float]], target: datetime) -> Optional[float]:
    if not points:
        return None
    pts = sorted(points, key=lambda item: item[0])
    if target <= pts[0][0]:
        return pts[0][1]
    if target >= pts[-1][0]:
        return pts[-1][1]
    for i in range(1, len(pts)):
        left_t, left_v = pts[i - 1]
        right_t, right_v = pts[i]
        if left_t <= target <= right_t:
            total = (right_t - left_t).total_seconds()
            if total <= 0:
                return right_v
            ratio = (target - left_t).total_seconds() / total
            return left_v + (right_v - left_v) * ratio
    return None


def compute_pnl_30d(points: List[Tuple[datetime, float]], now: Optional[datetime] = None) -> Optional[float]:
    if not points:
        return None
    current = points[-1][1]
    target = (now or datetime.now(timezone.utc)) - timedelta(days=30)
    start = interpolate_pnl_at(points, target)
    return None if start is None else current - start


def compute_drawdown_sharpe(
    points: List[Tuple[datetime, float]],
) -> Tuple[Optional[float], Optional[float]]:
    if len(points) < 3:
        return None, None
    vals = [float(v) for _, v in points]
    peak: Optional[float] = None
    max_dd_usd = 0.0
    max_dd_ratio: Optional[float] = None
    for value in vals:
        if peak is None or value > peak:
            peak = value
        if peak is None:
            continue
        dd_usd = peak - value
        if dd_usd > max_dd_usd:
            max_dd_usd = dd_usd
            max_dd_ratio = dd_usd / peak if peak > 0 else None
    changes = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    s = std(changes)
    sharpe = (mean(changes) / s) * math.sqrt(730.0) if s > 0 else None
    if max_dd_usd <= 0:
        return 0.0, sharpe
    return max_dd_ratio, sharpe


def compute_ulcer_index(points: List[Tuple[datetime, float]], total_pnl: float = 0.0) -> Optional[float]:
    if len(points) < 3:
        return None
    vals = [float(v) for _, v in points]
    hwm = vals[0]
    dd_sq_sum = 0.0
    counted = 0
    hwm_floor = max(abs(float(total_pnl)) * 0.005, 100.0)
    for value in vals:
        if value > hwm:
            hwm = value
        if hwm >= hwm_floor:
            d = (value - hwm) / hwm * 100.0
            dd_sq_sum += d * d
            counted += 1
    if counted < 3:
        return None
    return math.sqrt(dd_sq_sum / counted)


def fetch_user_stats(client: ApiClient, address: str) -> Dict[str, Any]:
    data = client.get_json(
        f"{DATA_API}/v1/user-stats",
        params={"proxyAddress": normalize_address(address)},
        timeout_s=15.0,
        max_retries=3,
    )
    return data if isinstance(data, dict) else {}


def fetch_total_value(client: ApiClient, address: str) -> Optional[float]:
    data = client.get_json(
        f"{DATA_API}/value",
        params={"user": normalize_address(address)},
        timeout_s=15.0,
        max_retries=2,
    )
    if isinstance(data, list) and data and isinstance(data[0], dict):
        value = to_float(data[0].get("value"))
        return float(value) if value is not None else None
    return None


def fetch_positions(client: ApiClient, address: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    offset = 0
    limit = 500
    while True:
        data = client.get_json(
            f"{DATA_API}/positions",
            params={"user": normalize_address(address), "sizeThreshold": 0, "limit": limit, "offset": offset},
            timeout_s=25.0,
        )
        if not isinstance(data, list) or not data:
            break
        out.extend(row for row in data if isinstance(row, dict))
        if len(data) < limit or len(out) >= 5000:
            break
        offset += limit
    return out


def fetch_closed_positions(
    client: ApiClient,
    address: str,
    closed_positions_limit: Optional[int] = DEFAULT_CLOSED_POSITIONS_LIMIT,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    limit = 50
    offset = 0
    max_rows = None if closed_positions_limit is None else max(1, int(closed_positions_limit))
    max_pages = 80 if max_rows is None else max(1, math.ceil(max_rows / limit))
    for _ in range(max_pages):
        data = client.get_json(
            f"{DATA_API}/closed-positions",
            params={
                "user": normalize_address(address),
                "limit": limit,
                "offset": offset,
                "sortBy": "TIMESTAMP",
                "sortDirection": "ASC",
            },
            timeout_s=25.0,
        )
        if not isinstance(data, list) or not data:
            break
        for row in data:
            if not isinstance(row, dict):
                continue
            out.append(row)
            if max_rows is not None and len(out) >= max_rows:
                return out
        if len(data) < limit:
            break
        offset += limit
    return out


def extract_position_fields(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    token_id = row.get("asset") or row.get("asset_id") or row.get("tokenId") or row.get("token_id")
    market = row.get("market") or row.get("conditionId") or row.get("condition_id")
    slug = row.get("slug") or row.get("market_slug") or row.get("eventSlug") or row.get("marketSlug")
    outcome = row.get("outcome") or row.get("title") or row.get("name")

    size = None
    for key in ("size", "shares", "balance", "quantity", "qty", "amount"):
        size = to_float(row.get(key))
        if size is not None:
            break

    total_bought = to_float(row.get("totalBought") or row.get("total_bought"))
    initial_value = to_float(row.get("initialValue") or row.get("initial_value"))
    current_value = to_float(row.get("currentValue") or row.get("current_value"))
    avg_price = to_float(row.get("avgPrice") or row.get("avg_price") or row.get("price"))

    cost_basis = None
    for key in ("initialValue", "initial_value", "costBasis", "cost_basis", "avgCost", "avg_cost", "cost"):
        cost_basis = to_float(row.get(key))
        if cost_basis is not None:
            break
    if cost_basis is None and avg_price is not None and size is not None:
        cost_basis = abs(size) * avg_price

    cash_pnl = None
    for key in ("cashPnl", "cash_pnl", "pnl", "profit"):
        cash_pnl = to_float(row.get(key))
        if cash_pnl is not None:
            break

    realized_pnl = to_float(row.get("realizedPnl") or row.get("realized_pnl"))
    cur_price = to_float(row.get("curPrice") or row.get("currentPrice") or row.get("cur_price"))
    closed_flag = bool(
        row.get("redeemed")
        or row.get("closed")
        or (row.get("redeemable") is True and row.get("size") in (0, "0", 0.0))
    )

    if size is None and total_bought is not None and total_bought > 0:
        size = total_bought

    if avg_price is None and cost_basis is not None and size is not None and abs(size) > 0:
        inferred_avg_price = cost_basis / abs(size)
        if is_plausible_probability(inferred_avg_price):
            avg_price = inferred_avg_price

    if size is None and cost_basis is not None and avg_price is not None and avg_price > 0:
        inferred_size = cost_basis / avg_price
        if inferred_size > 0:
            size = inferred_size

    if cost_basis is None and avg_price is not None and size is not None and abs(size) > 0:
        cost_basis = abs(size) * avg_price

    if size is None and total_bought is not None and total_bought > 0 and avg_price is None and cost_basis is not None:
        inferred_avg_price = cost_basis / total_bought
        if is_plausible_probability(inferred_avg_price):
            avg_price = inferred_avg_price
            size = total_bought

    if cur_price is None and current_value is not None and size is not None and abs(size) > 0:
        inferred_cur_price = current_value / abs(size)
        if is_plausible_probability(inferred_cur_price):
            cur_price = inferred_cur_price

    return {
        "market": str(market) if market else None,
        "slug": str(slug) if slug else None,
        "token_id": str(token_id) if token_id else None,
        "outcome": str(outcome) if isinstance(outcome, str) else None,
        "size": size,
        "total_bought": total_bought,
        "initial_value": initial_value,
        "current_value": current_value,
        "avg_price": avg_price,
        "cost_basis": cost_basis,
        "cash_pnl": cash_pnl,
        "realized_pnl": realized_pnl,
        "cur_price": cur_price,
        "current_value": current_value,
        "closed": closed_flag,
    }


def normalize_position(row: Dict[str, Any], *, closed: bool) -> Dict[str, Any]:
    parsed = extract_position_fields(row)
    if parsed is None:
        return {
            "token_id": None,
            "market": "unknown",
            "slug": "",
            "outcome": None,
            "outcome_index": row.get("outcomeIndex") if isinstance(row.get("outcomeIndex"), int) else row.get("outcome_index"),
            "avg_price": None,
            "cur_price": None,
            "total_bought": None,
            "cost_basis_usd": None,
            "cash_pnl": None,
            "realized_pnl": None,
            "size": None,
            "current_value": None,
            "closed": closed,
        }
    avg_price = parsed.get("avg_price")
    cur_price = parsed.get("cur_price")
    cash_pnl = parsed.get("cash_pnl")
    realized_pnl = parsed.get("realized_pnl")
    if closed and realized_pnl is None:
        realized_pnl = cash_pnl
    if closed and cash_pnl is None:
        # `closed-positions` often exposes only realizedPnl. Treat it as the
        # position PnL so closed trades participate in win-rate / ROI stats.
        cash_pnl = realized_pnl
    return {
        "token_id": parsed.get("token_id"),
        "market": str(parsed.get("market") or parsed.get("slug") or "unknown"),
        "slug": str(parsed.get("slug") or ""),
        "outcome": parsed.get("outcome"),
        "outcome_index": row.get("outcomeIndex") if isinstance(row.get("outcomeIndex"), int) else row.get("outcome_index"),
        "avg_price": avg_price,
        "cur_price": cur_price if cur_price is not None else avg_price,
        "total_bought": parsed.get("total_bought"),
        "cost_basis_usd": parsed.get("cost_basis"),
        "cash_pnl": cash_pnl,
        "realized_pnl": realized_pnl if closed else to_float(row.get("realizedPnl") or row.get("realized_pnl")),
        "size": parsed.get("size"),
        "current_value": parsed.get("current_value"),
        "closed": bool(closed or parsed.get("closed")),
    }


def derive_cost_basis_usd(position: Dict[str, Any]) -> Optional[float]:
    cost_basis = to_float(position.get("cost_basis_usd"))
    if cost_basis is not None and cost_basis > 0:
        return cost_basis
    total_bought = to_float(position.get("total_bought"))
    if total_bought is not None and total_bought > 0:
        return total_bought
    avg_price = to_float(position.get("avg_price"))
    size = to_float(position.get("size"))
    if avg_price is not None and size is not None and abs(size) > 0:
        return abs(size) * avg_price
    return None


def quantize_binary_resolution(value: Any, epsilon: float = RESOLUTION_EPSILON) -> Optional[float]:
    raw = to_float(value)
    if raw is None or not math.isfinite(raw):
        return None
    if abs(raw - 0.0) <= epsilon:
        return 0.0
    if abs(raw - 1.0) <= epsilon:
        return 1.0
    return None


def resolve_position_payout(position: Dict[str, Any]) -> Tuple[Optional[float], str]:
    cur_price_raw = to_float(position.get("cur_price"))
    is_closed = bool(position.get("closed"))
    if not is_closed and cur_price_raw is not None and is_plausible_probability(cur_price_raw):
        return cur_price_raw, "cur_price"
    current_value = to_float(position.get("current_value"))
    size = to_float(position.get("size"))
    if not is_closed and current_value is not None and size is not None and abs(size) > 0:
        inferred = current_value / abs(size)
        if is_plausible_probability(inferred):
            return inferred, "current_value"
    cur_price = quantize_binary_resolution(cur_price_raw)
    if cur_price is not None:
        return cur_price, "cur_price"
    avg_price = to_float(position.get("avg_price"))
    realized_pnl = to_float(position.get("realized_pnl"))
    if avg_price is not None and realized_pnl is not None and size is not None and abs(size) > 0:
        inferred = avg_price + realized_pnl / abs(size)
        resolved = quantize_binary_resolution(inferred)
        if resolved is not None:
            return resolved, "economics"
    return None, "ambiguous"


def compute_position_metrics(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_market: Dict[str, float] = {}
    realized = 0.0
    total_bought_sum = 0.0
    total_bought_known = False
    winning = 0
    losing = 0
    considered = 0
    weighted_price_sum = 0.0
    weighted_price_den = 0.0
    realized_edge_num = 0.0
    realized_edge_den = 0.0
    edge_samples = 0
    skipped_missing_fields = 0
    skipped_ambiguous_resolution = 0
    resolution_sources = {"cur_price": 0, "current_value": 0, "economics": 0}

    for pos in positions:
        pnl = to_float(pos.get("cash_pnl"))
        market = str(pos.get("market") or pos.get("slug") or "unknown")
        if pnl is not None:
            per_market[market] = per_market.get(market, 0.0) + pnl
            considered += 1
            if pnl > 0:
                winning += 1
            elif pnl < 0:
                losing += 1
        realized_pnl = to_float(pos.get("realized_pnl"))
        if realized_pnl is not None:
            realized += realized_pnl
        cost_basis_usd = derive_cost_basis_usd(pos)
        total_bought = to_float(pos.get("total_bought"))
        if total_bought is not None and total_bought > 0:
            total_bought_sum += total_bought
            total_bought_known = True
        if cost_basis_usd is not None and cost_basis_usd > 0:
            avg_price = to_float(pos.get("avg_price"))
            if avg_price is not None:
                weighted_price_sum += avg_price * cost_basis_usd
                weighted_price_den += cost_basis_usd
        entry_price = to_float(pos.get("avg_price"))
        if entry_price is None:
            skipped_missing_fields += 1
            continue
        if cost_basis_usd is None or cost_basis_usd <= 0:
            skipped_missing_fields += 1
            continue
        resolution, source = resolve_position_payout(pos)
        if resolution is None:
            skipped_ambiguous_resolution += 1
            continue
        realized_edge_num += (resolution - entry_price) * cost_basis_usd
        realized_edge_den += cost_basis_usd
        edge_samples += 1
        resolution_sources[source] = resolution_sources.get(source, 0) + 1

    total_pnl = sum(per_market.values())
    gross_profit = sum(v for v in per_market.values() if v > 0)
    gross_loss = sum(-v for v in per_market.values() if v < 0)
    return {
        "total_pnl": total_pnl,
        "realized_pnl": realized,
        "unrealized_pnl": total_pnl - realized,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "roi": total_pnl / total_bought_sum if total_bought_known and total_bought_sum > 0 else None,
        "total_trades": len(positions),
        "winning_trades": winning,
        "losing_trades": losing,
        "win_rate": winning / considered if considered > 0 else None,
        "avg_trade_price": weighted_price_sum / weighted_price_den if weighted_price_den > 0 else None,
        "realized_edge_score": realized_edge_num / realized_edge_den if realized_edge_den > 0 else None,
        "position_edge": {
            "edge_samples": edge_samples,
            "edge_weight_usd": realized_edge_den if realized_edge_den > 0 else None,
            "skipped_missing_fields": skipped_missing_fields,
            "skipped_ambiguous_resolution": skipped_ambiguous_resolution,
            "resolution_sources": resolution_sources,
            "resolution_epsilon": RESOLUTION_EPSILON,
        },
    }


def is_metrics_compatible(details: Optional[Dict[str, Any]], closed_positions_limit: int = DEFAULT_CLOSED_POSITIONS_LIMIT) -> bool:
    if not isinstance(details, dict):
        return False
    version = details.get("metrics_compat_version")
    if version != METRICS_COMPAT_VERSION:
        return False
    raw_limit = details.get("closed_positions_limit")
    try:
        return int(raw_limit) == int(closed_positions_limit)
    except Exception:
        return False


def compute_address_metrics(
    client: ApiClient,
    address: str,
    closed_positions_limit: int = DEFAULT_CLOSED_POSITIONS_LIMIT,
) -> MetricsResult:
    addr = normalize_address(address)
    threshold_config = load_threshold_config()
    threshold_warning = threshold_status()
    pnl_points = fetch_user_pnl_points(
        client,
        addr,
        interval=USER_PNL_INTERVAL,
        fidelity=USER_PNL_FIDELITY,
    )
    open_rows = fetch_positions(client, addr)
    closed_rows = fetch_closed_positions(client, addr, closed_positions_limit=closed_positions_limit)
    positions = [normalize_position(row, closed=False) for row in open_rows]
    positions.extend(normalize_position(row, closed=True) for row in closed_rows)
    metrics = compute_position_metrics(positions)
    max_drawdown, sharpe = compute_drawdown_sharpe(pnl_points)
    metrics["max_drawdown"] = max_drawdown
    metrics["sharpe"] = sharpe
    metrics["pnl_30d"] = compute_pnl_30d(pnl_points)
    metrics["snapshot_utc"] = datetime.now(timezone.utc).isoformat()
    if pnl_points:
        metrics["user_pnl_latest"] = pnl_points[-1][1]
        metrics["total_pnl"] = pnl_points[-1][1]
    metrics["ulcer_index"] = compute_ulcer_index(pnl_points, total_pnl=float(metrics.get("total_pnl") or 0.0))
    metrics["current_position_value_usd"] = fetch_total_value(client, addr)
    metrics.update(apply_copytrade_value(metrics, threshold_config))
    stats = fetch_user_stats(client, addr)
    trades = to_float(stats.get("trades"))
    if trades is not None:
        metrics["user_stats_trades"] = int(trades)
    return MetricsResult(
        address=addr,
        metrics=metrics,
        details={
            "open_positions": len(open_rows),
            "closed_positions": len(closed_rows),
            "closed_positions_limit": int(closed_positions_limit),
            "closed_positions_truncated": len(closed_rows) >= int(closed_positions_limit),
            "metrics_compat_version": METRICS_COMPAT_VERSION,
            "user_pnl_interval": USER_PNL_INTERVAL,
            "user_pnl_fidelity": USER_PNL_FIDELITY,
            "pnl_points": len(pnl_points),
            "copytrade_value_threshold_warning": threshold_warning or None,
        },
    )


def address_profile_for_gate(client: ApiClient, address: str) -> Dict[str, Any]:
    addr = normalize_address(address)
    points = fetch_user_pnl_points(client, addr, interval="max", fidelity="1d")
    stats = fetch_user_stats(client, addr)
    trades = to_float(stats.get("trades"))
    first_activity = points[0][0] if points else None
    now = datetime.now(timezone.utc)
    age_days = ((now - first_activity).total_seconds() / 86400.0) if first_activity else None
    latest_pnl = points[-1][1] if points else None
    return {
        "address": addr,
        "first_activity_utc": first_activity.isoformat() if first_activity else None,
        "address_age_days": age_days,
        "user_stats_trades": int(trades) if trades is not None else None,
        "total_pnl_latest": latest_pnl,
        "pnl_points": len(points),
    }
