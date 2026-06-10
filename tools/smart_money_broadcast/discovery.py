from __future__ import annotations

import json
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from boards import BoardConfig, get_board, normalize_board_names
from http_client import DATA_API, GAMMA_API, ApiClient, normalize_address
from metrics import DEFAULT_CLOSED_POSITIONS_LIMIT, METRICS_COMPAT_VERSION, address_profile_for_gate, compute_address_metrics, is_metrics_compatible
from store import SmartMoneyStore

MAX_USER_TRADES = 30000
DATA_TRADES_MAX_OFFSET = 3000
MAX_METRICS_WORKERS = 5


@dataclass
class MarketInfo:
    condition_id: str
    market_id: Optional[str]
    slug: str
    title: str
    board: str = "NBA"


@dataclass
class DiscoveryResult:
    run_id: int
    selected: List[Dict[str, Any]]
    failure_reasons: Dict[str, int]


def parse_json_list_maybe(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
        if value.strip():
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def is_unfinished_market(market: Dict[str, Any]) -> bool:
    if market.get("closed") is True:
        return False
    if market.get("ended") is True:
        return False
    events = market.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get("ended") is True:
                return False
            if str(event.get("gameStatus") or "").lower() in {"final", "ended"}:
                return False
    return True


def is_moneyline_market(market: Dict[str, Any]) -> bool:
    q = str(market.get("question") or market.get("title") or "").lower()
    if " vs." not in q and " vs " not in q:
        return False
    if ":" in q:
        return False
    banned = ("1h", "1st half", "first half", "2h", "quarter", "spread", "o/u", "over/under", "total")
    return not any(token in q for token in banned)


def market_to_info(market: Dict[str, Any], board: str) -> Optional[MarketInfo]:
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if not condition_id:
        return None
    return MarketInfo(
        condition_id=condition_id,
        market_id=str(market.get("id")) if market.get("id") is not None else None,
        slug=str(market.get("slug") or ""),
        title=str(market.get("title") or market.get("question") or ""),
        board=str(board).upper(),
    )


def fetch_sports(client: ApiClient) -> List[Dict[str, Any]]:
    data = client.get_json(f"{GAMMA_API}/sports", timeout_s=20.0)
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    raise RuntimeError("Unexpected Gamma /sports response")


def get_series_id_for_sport_code(sports: List[Dict[str, Any]], sport_code: str) -> Optional[str]:
    code = sport_code.lower().strip()
    for row in sports:
        if str(row.get("sport") or "").lower().strip() == code:
            series = row.get("series")
            return str(series) if series is not None else None
    return None


def iter_sport_markets(client: ApiClient, cfg: BoardConfig, *, max_markets: Optional[int] = None) -> Iterable[MarketInfo]:
    if not cfg.sport:
        return
    sports = fetch_sports(client)
    series_id = get_series_id_for_sport_code(sports, cfg.sport)
    if not series_id:
        raise RuntimeError(f"series not found for sport={cfg.sport}")
    series = client.get_json(f"{GAMMA_API}/series/{series_id}", timeout_s=20.0)
    events = series.get("events") if isinstance(series, dict) else None
    if not isinstance(events, list):
        return
    active_events = [
        event
        for event in events
        if isinstance(event, dict) and event.get("active") is True and event.get("closed") is not True and event.get("ended") is not True
    ]
    seen: set[str] = set()
    yielded = 0
    limit = max_markets if max_markets is not None else cfg.max_games
    for event in active_events:
        if limit is not None and yielded >= limit:
            break
        event_id = event.get("id")
        if event_id is None:
            continue
        event_obj = client.get_json(f"{GAMMA_API}/events/{event_id}", timeout_s=20.0)
        markets = event_obj.get("markets") if isinstance(event_obj, dict) else None
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict) or not is_unfinished_market(market):
                continue
            if cfg.market_kind == "moneyline" and not is_moneyline_market(market):
                continue
            info = market_to_info(market, cfg.name)
            if info is None or info.condition_id in seen:
                continue
            seen.add(info.condition_id)
            yield info
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def candidate_page_limits(preferred_limit: int) -> List[int]:
    out: List[int] = []
    for value in (preferred_limit, 100, 50, 20, 10, 5, 1):
        iv = int(value)
        if 1 <= iv <= 100 and iv not in out:
            out.append(iv)
    return out


def tag_query_candidates(tag_id: int, related_tags: bool, preferred_limit: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    related_variants: List[Optional[str]] = ["true"] if related_tags else [None, "false"]
    for limit in candidate_page_limits(preferred_limit):
        for endpoint in ("events", "markets"):
            for related_value in related_variants:
                params: Dict[str, Any] = {
                    "tag_id": int(tag_id),
                    "active": "true",
                    "closed": "false",
                    "limit": limit,
                    "offset": 0,
                }
                if related_value is not None:
                    params["related_tags"] = related_value
                candidates.append({"endpoint": endpoint, "limit": limit, "params": params})
    return candidates


def resolve_tag_query_strategy(client: ApiClient, cfg: BoardConfig) -> Dict[str, Any]:
    if cfg.tag_id is None:
        raise RuntimeError(f"tag_id missing for board={cfg.name}")
    preferred_limit = 100 if cfg.gamma_page_limit >= 100 else 10
    errors: List[str] = []
    for candidate in tag_query_candidates(cfg.tag_id, cfg.related_tags, preferred_limit):
        endpoint = str(candidate["endpoint"])
        params = dict(candidate["params"])
        try:
            data = client.get_json(f"{GAMMA_API}/{endpoint}", params=params, timeout_s=20.0)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{endpoint} {params} -> {exc}")
            continue
        if not isinstance(data, list):
            errors.append(f"{endpoint} {params} -> unexpected {type(data)}")
            continue
        return {
            "endpoint": endpoint,
            "limit": int(candidate["limit"]),
            "params": {k: v for k, v in params.items() if k not in {"limit", "offset"}},
            "first_page": data,
        }
    raise RuntimeError(f"all tag query strategies failed for {cfg.name}: {'; '.join(errors[:4])}")


def iter_tag_markets(client: ApiClient, cfg: BoardConfig, *, max_markets: Optional[int] = None) -> Iterable[MarketInfo]:
    strategy = resolve_tag_query_strategy(client, cfg)
    offset = 0
    yielded = 0
    seen: set[str] = set()
    limit_markets = max_markets if max_markets is not None else cfg.max_games
    while True:
        if limit_markets is not None and yielded >= limit_markets:
            break
        if offset == 0:
            data = strategy["first_page"]
        else:
            params = dict(strategy["params"])
            params.update({"limit": int(strategy["limit"]), "offset": offset})
            data = client.get_json(f"{GAMMA_API}/{strategy['endpoint']}", params=params, timeout_s=20.0)
        if not isinstance(data, list) or not data:
            break
        market_iter: List[Dict[str, Any]] = []
        if str(strategy["endpoint"]) == "markets":
            market_iter = [row for row in data if isinstance(row, dict)]
        else:
            for event in data:
                markets = event.get("markets") if isinstance(event, dict) else None
                if isinstance(markets, list):
                    market_iter.extend(row for row in markets if isinstance(row, dict))
        for market in market_iter:
            if not is_unfinished_market(market):
                continue
            if cfg.market_kind == "moneyline" and not is_moneyline_market(market):
                continue
            info = market_to_info(market, cfg.name)
            if info is None or info.condition_id in seen:
                continue
            seen.add(info.condition_id)
            yield info
            yielded += 1
            if limit_markets is not None and yielded >= limit_markets:
                return
        if len(data) < int(strategy["limit"]):
            break
        offset += int(strategy["limit"])


def current_period_ts(interval_minutes: int = 15) -> int:
    now = datetime.now(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    total_seconds = int((now - epoch).total_seconds())
    interval_seconds = max(1, int(interval_minutes)) * 60
    return (total_seconds // interval_seconds) * interval_seconds


def hourly_et_slug_suffix() -> str:
    et = timezone(timedelta(hours=-4))
    now_et = datetime.now(et).replace(minute=0, second=0, microsecond=0)
    month_name = now_et.strftime("%B").lower()
    hour_12 = now_et.strftime("%I").lstrip("0")
    ampm = now_et.strftime("%p").lower()
    return f"{month_name}-{now_et.day}-{now_et.year}-{hour_12}{ampm}-et"


def fetch_event_by_slug(client: ApiClient, slug: str) -> Optional[Dict[str, Any]]:
    data = client.get_json(f"{GAMMA_API}/events", params={"slug": slug}, timeout_s=20.0)
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return None


def iter_slug_prefix_markets(client: ApiClient, cfg: BoardConfig, *, max_markets: Optional[int] = None) -> Iterable[MarketInfo]:
    suffix = hourly_et_slug_suffix() if cfg.slug_format == "hourly-et" else str(current_period_ts(cfg.slug_interval_minutes))
    yielded = 0
    limit = max_markets if max_markets is not None else cfg.max_games
    seen: set[str] = set()
    for prefix in cfg.slug_prefixes:
        if limit is not None and yielded >= limit:
            break
        event = fetch_event_by_slug(client, f"{prefix}-{suffix}")
        markets = event.get("markets") if isinstance(event, dict) else None
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict) or not is_unfinished_market(market):
                continue
            info = market_to_info(market, cfg.name)
            if info is None or info.condition_id in seen:
                continue
            seen.add(info.condition_id)
            yield info
            yielded += 1
            if limit is not None and yielded >= limit:
                return


def iter_board_markets(client: ApiClient, board: str, *, max_markets: Optional[int] = None) -> Iterable[MarketInfo]:
    cfg = get_board(board)
    if cfg.source_kind == "sport":
        yield from iter_sport_markets(client, cfg, max_markets=max_markets)
    elif cfg.source_kind == "tag":
        yield from iter_tag_markets(client, cfg, max_markets=max_markets)
    elif cfg.source_kind == "slug_prefix":
        yield from iter_slug_prefix_markets(client, cfg, max_markets=max_markets)
    else:
        raise RuntimeError(f"unsupported board source kind: {cfg.source_kind}")


def fetch_market_trade_addresses(
    client: ApiClient,
    condition_id: str,
    *,
    limit_per_page: int = 1000,
    max_pages: int = 8,
    directions: str = "both",
) -> List[str]:
    page_limit = max(1, int(limit_per_page))
    out: List[str] = []
    seen: set[str] = set()
    sort_directions = ["DESC"] if directions == "desc" else ["DESC", "ASC"]
    for direction in sort_directions:
        offset = 0
        for _ in range(max(1, int(max_pages))):
            if offset > DATA_TRADES_MAX_OFFSET:
                break
            data = client.get_json(
                f"{DATA_API}/trades",
                params={
                    "market": condition_id,
                    "limit": page_limit,
                    "offset": offset,
                    "sortBy": "TIMESTAMP",
                    "sortDirection": direction,
                },
                timeout_s=20.0,
            )
            if not isinstance(data, list) or not data:
                break
            for row in data:
                if not isinstance(row, dict):
                    continue
                address = normalize_address(row.get("proxyWallet"))
                if not address or address in seen:
                    continue
                seen.add(address)
                out.append(address)
            if len(data) < page_limit:
                break
            offset += page_limit
    return out


def passes_gate(profile: Dict[str, Any], min_age_days: float, min_trades: int) -> tuple[bool, str]:
    if not profile.get("pnl_points"):
        return False, "no_pnl_points"
    age_days = profile.get("address_age_days")
    if not isinstance(age_days, (int, float)) or float(age_days) < float(min_age_days):
        return False, "address_age_lt_threshold"
    trades = profile.get("user_stats_trades")
    if not isinstance(trades, int):
        return False, "user_stats_missing_or_invalid"
    if trades <= int(min_trades):
        return False, "user_trades_le_threshold"
    if trades > MAX_USER_TRADES:
        return False, "user_trades_gt_threshold"
    return True, ""


def discover_addresses(
    client: ApiClient,
    store: SmartMoneyStore,
    *,
    boards: Sequence[str],
    target_count: int,
    min_age_days: float,
    min_trades: int,
    old_address_policy: str = "reuse_old_metrics",
    max_markets: Optional[int] = None,
    closed_positions_limit: int = DEFAULT_CLOSED_POSITIONS_LIMIT,
    metrics_max_workers: int = MAX_METRICS_WORKERS,
) -> DiscoveryResult:
    if old_address_policy not in {"reuse_old_metrics", "skip_old", "refresh_old_metrics"}:
        raise ValueError("old_address_policy must be reuse_old_metrics, skip_old, or refresh_old_metrics")
    board_names = normalize_board_names(boards)
    run_id = store.create_run(board_names, target_count, min_age_days, min_trades, old_address_policy)
    selected: List[Dict[str, Any]] = []
    selected_addresses: set[str] = set()
    failure_reasons: Counter[str] = Counter()
    processed_by_board: set[tuple[str, str]] = set()
    max_workers = max(1, min(MAX_METRICS_WORKERS, int(metrics_max_workers)))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for board in board_names:
            if len(selected) >= target_count:
                break
            try:
                markets = iter_board_markets(client, board, max_markets=max_markets)
                for market in markets:
                    if len(selected) >= target_count:
                        break
                    try:
                        addresses = fetch_market_trade_addresses(client, market.condition_id)
                    except Exception as exc:  # noqa: BLE001
                        failure_reasons[f"{board}:market_fetch_failed:{type(exc).__name__}"] += 1
                        continue

                    candidates: List[Dict[str, Any]] = []
                    pending_addresses = set(selected_addresses)
                    for address in addresses:
                        if len(selected) + len(candidates) >= target_count:
                            break
                        board_key = str(board).upper()
                        identity = (board_key, address)
                        if identity in processed_by_board:
                            continue
                        processed_by_board.add(identity)
                        old_board_member = store.has_address(address, board_key)
                        if old_board_member and old_address_policy == "skip_old":
                            store.record_run_address(
                                run_id,
                                address,
                                "skipped_old",
                                "old_address",
                                market.condition_id,
                                market.slug,
                                board_key,
                            )
                            failure_reasons[f"{board}:old_address_skipped"] += 1
                            continue
                        try:
                            profile = address_profile_for_gate(client, address)
                        except Exception as exc:  # noqa: BLE001
                            store.record_run_address(
                                run_id,
                                address,
                                "filtered",
                                f"profile_error:{type(exc).__name__}",
                                market.condition_id,
                                market.slug,
                                board_key,
                            )
                            failure_reasons[f"{board}:profile_error"] += 1
                            continue
                        ok, reason = passes_gate(profile, min_age_days, min_trades)
                        if not ok:
                            store.record_run_address(run_id, address, "filtered", reason, market.condition_id, market.slug, board_key)
                            failure_reasons[f"{board}:{reason}"] += 1
                            continue
                        row = {
                            **profile,
                            "board": board_key,
                            "condition_id": market.condition_id,
                            "market_id": market.market_id,
                            "slug": market.slug,
                            "title": market.title,
                            "old_address": old_board_member,
                        }
                        store.upsert_address(row, board_key)
                        if address in pending_addresses:
                            store.record_run_address(run_id, address, "selected_duplicate_board", "already_selected", market.condition_id, market.slug, board_key)
                            continue
                        pending_addresses.add(address)

                        metrics = None
                        if old_board_member and old_address_policy == "reuse_old_metrics":
                            latest = store.latest_metrics_with_details(address, board_key)
                            if latest is not None and is_metrics_compatible(latest.get("details"), closed_positions_limit):
                                metrics = latest.get("metrics")
                        future: Optional[Future[Any]] = None
                        copied_details = None
                        if metrics is None:
                            cached_any = store.latest_metrics_any_board_with_details(address) if old_address_policy == "reuse_old_metrics" else None
                            if cached_any is not None and is_metrics_compatible(cached_any.get("details"), closed_positions_limit):
                                metrics = cached_any.get("metrics")
                                copied_details = {
                                    "copied_from_latest_address_cache": True,
                                    "closed_positions_limit": int(closed_positions_limit),
                                    "metrics_compat_version": METRICS_COMPAT_VERSION,
                                }
                            else:
                                future = pool.submit(
                                    compute_address_metrics,
                                    client,
                                    address,
                                    closed_positions_limit=closed_positions_limit,
                                )
                        candidates.append(
                            {
                                "row": row,
                                "address": address,
                                "board_key": board_key,
                                "old_board_member": old_board_member,
                                "metrics": metrics,
                                "copied_details": copied_details,
                                "future": future,
                            }
                        )

                    for candidate in candidates:
                        if len(selected) >= target_count:
                            break
                        address = candidate["address"]
                        board_key = candidate["board_key"]
                        metrics = candidate["metrics"]
                        copied_details = candidate["copied_details"]
                        future = candidate["future"]
                        if copied_details is not None:
                            store.save_metrics(address, metrics, copied_details, board_key)
                        elif future is not None:
                            metric_result = future.result()
                            metrics = metric_result.metrics
                            store.save_metrics(address, metrics, metric_result.details, board_key)
                        row = candidate["row"]
                        row["metrics"] = metrics
                        selected.append(row)
                        selected_addresses.add(address)
                        store.record_run_address(
                            run_id,
                            address,
                            "selected",
                            "old" if candidate["old_board_member"] else "new",
                            row["condition_id"],
                            row["slug"],
                            board_key,
                        )
            except Exception as exc:  # noqa: BLE001
                failure_reasons[f"{board}:board_scan_failed:{type(exc).__name__}"] += 1
                continue

    store.finish_run(run_id, len(selected), dict(failure_reasons))
    return DiscoveryResult(run_id=run_id, selected=selected, failure_reasons=dict(failure_reasons))
