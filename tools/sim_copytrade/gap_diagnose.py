
from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for _path in (SIM_ROOT, SIM_PARENT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from polymarket_public_api import USER_PNL_METRICS_FIDELITY, USER_PNL_METRICS_INTERVAL, fetch_user_pnl_series
from sim_copytrade import main as sim_main


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(v: Any) -> Optional[float]:
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _as_int(v: Any) -> Optional[int]:
    n = _as_float(v)
    if n is None:
        return None
    return int(round(n))


def _fmt_utc(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _short_address(addr: str) -> str:
    s = (addr or "").strip().lower()
    if not s:
        return "unknown"
    if len(s) >= 14:
        return f"{s[:8]}_{s[-6:]}"
    return "".join(ch for ch in s if ch.isalnum()) or "unknown"


def _to_epoch_from_iso(iso_text: str) -> Optional[int]:
    if not isinstance(iso_text, str) or not iso_text.strip():
        return None
    s = iso_text.strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Diagnose why sim_copytrade results diverge from real account performance"
    )
    ap.add_argument("--sim-json", type=str, required=True, help="Path to sim_results_*.json")
    ap.add_argument("--address", type=str, default="", help="Override address (default from sim json meta)")
    ap.add_argument("--max-activities", type=int, default=None, help="Override max activities (default from sim json)")
    ap.add_argument("--page-limit", type=int, default=1000, help="Per-request page limit")
    ap.add_argument("--top-k", type=int, default=3, help="Top K strategies for deep counterfactual replay")
    ap.add_argument("--out-json", type=str, default="", help="Output JSON path")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    ap.add_argument("--price-workers", type=int, default=16, help="Workers for price fetch during replay")
    return ap.parse_args()


def load_sim_payload(path: Path) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(f"sim json must be an object: {path}")
    meta = raw.get("meta")
    results = raw.get("results")
    if not isinstance(meta, dict):
        raise RuntimeError(f"sim json missing meta object: {path}")
    if not isinstance(results, list):
        raise RuntimeError(f"sim json missing results list: {path}")
    normalized: List[Dict[str, Any]] = []
    for row in results:
        if isinstance(row, dict):
            normalized.append(sim_main.normalize_result_row(row))
    if not normalized:
        raise RuntimeError(f"sim json has no valid strategy rows: {path}")
    return meta, normalized


def interpolate_series_at(
    points: Sequence[Tuple[int, float]],
    target_ts: int,
) -> Dict[str, Any]:
    if not points:
        return {
            "value": None,
            "extrapolated": True,
            "nearest_point_delta_s": None,
            "segment_span_s": None,
            "left_ts": None,
            "right_ts": None,
        }

    ts_list = [p[0] for p in points]

    if target_ts <= ts_list[0]:
        return {
            "value": points[0][1],
            "extrapolated": True,
            "nearest_point_delta_s": int(abs(ts_list[0] - target_ts)),
            "segment_span_s": None,
            "left_ts": ts_list[0],
            "right_ts": ts_list[0],
        }

    if target_ts >= ts_list[-1]:
        return {
            "value": points[-1][1],
            "extrapolated": True,
            "nearest_point_delta_s": int(abs(target_ts - ts_list[-1])),
            "segment_span_s": None,
            "left_ts": ts_list[-1],
            "right_ts": ts_list[-1],
        }

    idx = bisect.bisect_left(ts_list, target_ts)
    left_ts, left_val = points[idx - 1]
    right_ts, right_val = points[idx]

    if right_ts <= left_ts:
        v = left_val
    else:
        ratio = float(target_ts - left_ts) / float(right_ts - left_ts)
        v = left_val + ratio * (right_val - left_val)

    nearest_delta = min(abs(target_ts - left_ts), abs(right_ts - target_ts))
    return {
        "value": v,
        "extrapolated": False,
        "nearest_point_delta_s": int(nearest_delta),
        "segment_span_s": int(max(0, right_ts - left_ts)),
        "left_ts": left_ts,
        "right_ts": right_ts,
    }


def compute_tracked_window_benchmark(
    session: requests.Session,
    address: str,
    *,
    first_ts: Optional[int],
    last_ts: Optional[int],
) -> Dict[str, Any]:
    series = fetch_user_pnl_series(
        session,
        address,
        interval=USER_PNL_METRICS_INTERVAL,
        fidelity=USER_PNL_METRICS_FIDELITY,
    )
    parsed_points: List[Tuple[int, float]] = []
    for ts_iso, pnl in series:
        ts_epoch = _to_epoch_from_iso(ts_iso)
        if ts_epoch is None:
            continue
        parsed_points.append((ts_epoch, float(pnl)))
    parsed_points.sort(key=lambda x: x[0])

    benchmark: Dict[str, Any] = {
        "mode": "tracked_window",
        "actual_window_pnl_delta": None,
        "series_points": len(parsed_points),
        "series_first_ts": parsed_points[0][0] if parsed_points else None,
        "series_last_ts": parsed_points[-1][0] if parsed_points else None,
        "series_first_utc": _fmt_utc(parsed_points[0][0]) if parsed_points else None,
        "series_last_utc": _fmt_utc(parsed_points[-1][0]) if parsed_points else None,
        "interpolation_quality": {
            "start": None,
            "end": None,
        },
    }

    if first_ts is None or last_ts is None or last_ts < first_ts:
        benchmark["error"] = "invalid tracked window timestamps"
        return benchmark

    start_info = interpolate_series_at(parsed_points, int(first_ts))
    end_info = interpolate_series_at(parsed_points, int(last_ts))
    benchmark["interpolation_quality"]["start"] = start_info
    benchmark["interpolation_quality"]["end"] = end_info

    s_val = _as_float(start_info.get("value"))
    e_val = _as_float(end_info.get("value"))
    if s_val is not None and e_val is not None:
        benchmark["actual_window_pnl_delta"] = e_val - s_val
        benchmark["window_start_pnl"] = s_val
        benchmark["window_end_pnl"] = e_val
    else:
        benchmark["error"] = "insufficient user-pnl series for interpolation"

    return benchmark


def analyze_mirror_sell_effectiveness(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    if total <= 0:
        return {
            "strategies_total": 0,
            "strategies_with_mirrored_sells_gt0": 0,
            "mirrored_sell_exec_ratio": None,
            "avg_mirrored_sells_per_strategy": None,
            "avg_unresolved_position_ratio": None,
            "mirror_sell_effective": False,
            "reason": "no strategies available",
        }

    mirrored_positive = 0
    mirrored_total = 0
    unresolved_ratios: List[float] = []
    for row in results:
        mirrored = _as_int(row.get("mirrored_sells")) or 0
        open_positions = _as_int(row.get("open_positions")) or 0
        unresolved_positions = _as_int(row.get("unresolved_positions")) or 0
        mirrored_total += mirrored
        if mirrored > 0:
            mirrored_positive += 1
        if open_positions > 0:
            unresolved_ratios.append(float(unresolved_positions) / float(open_positions))

    exec_ratio = mirrored_positive / float(total)
    avg_mirrored = mirrored_total / float(total)
    avg_unresolved_ratio = statistics.mean(unresolved_ratios) if unresolved_ratios else None
    mirror_effective = mirrored_positive > 0
    if mirror_effective:
        reason = "mirror_sell executed in at least one strategy"
    else:
        reason = "no mirrored sells executed by any strategy"

    return {
        "strategies_total": total,
        "strategies_with_mirrored_sells_gt0": mirrored_positive,
        "mirrored_sell_exec_ratio": exec_ratio,
        "avg_mirrored_sells_per_strategy": avg_mirrored,
        "avg_unresolved_position_ratio": avg_unresolved_ratio,
        "mirror_sell_effective": mirror_effective,
        "reason": reason,
    }


def build_strategy_from_row(row: Dict[str, Any]) -> sim_main.Strategy:
    copy_mode = str(row.get("copy_mode") or "")
    exit_mode = sim_main.ONLY_EXIT_MODE
    fixed_usd = _as_float(row.get("fixed_usd"))
    proportional_pct = _as_float(row.get("proportional_pct")) or 0.0
    proportional_cap_usd = _as_float(row.get("proportional_cap_usd"))
    max_entries = max(1, int(_as_int(row.get("max_entries_per_market")) or 1))
    name = str(row.get("strategy") or "unknown")

    if copy_mode == "fixed_usd":
        proportional_pct = 0.0
        proportional_cap_usd = None
    elif copy_mode == "proportional":
        fixed_usd = None
    else:
        copy_mode = "fixed_usd"
        fixed_usd = fixed_usd if fixed_usd is not None else 10.0
        proportional_pct = 0.0
        proportional_cap_usd = None

    return sim_main.Strategy(
        name=name,
        copy_mode=copy_mode,
        fixed_usd=fixed_usd,
        proportional_pct=proportional_pct,
        proportional_cap_usd=proportional_cap_usd,
        max_entries_per_market=max_entries,
        exit_mode=exit_mode,
    )


def run_single_strategy_scenarios(
    replay_events: List[sim_main.TradeEvent],
    base_strategy: sim_main.Strategy,
    *,
    buy_premium_base: float,
    sell_slip_base: float,
    buy_limits: Tuple[float, float],
    sell_limits: Tuple[float, float],
    timeout_s: float,
    price_workers: int,
    anti_amplification_guard_enabled: bool,
    max_our_vs_leader_per_trade: float,
    max_our_vs_leader_per_market: float,
) -> Dict[str, Any]:
    no_friction_unlimited_strategy = replace(
        base_strategy,
        name=f"{base_strategy.name}|entries9999",
        max_entries_per_market=9999,
    )

    specs = [
        {
            "name": "baseline_like",
            "strategy": base_strategy,
            "buy_premium_pct": buy_premium_base,
            "sell_slippage_pct": sell_slip_base,
            "buy_min": buy_limits[0],
            "buy_max": buy_limits[1],
            "sell_min": sell_limits[0],
            "sell_max": sell_limits[1],
        },
        {
            "name": "buy_premium_off",
            "strategy": base_strategy,
            "buy_premium_pct": 0.0,
            "sell_slippage_pct": sell_slip_base,
            "buy_min": buy_limits[0],
            "buy_max": buy_limits[1],
            "sell_min": sell_limits[0],
            "sell_max": sell_limits[1],
        },
        {
            "name": "sell_slippage_off",
            "strategy": base_strategy,
            "buy_premium_pct": buy_premium_base,
            "sell_slippage_pct": 0.0,
            "buy_min": buy_limits[0],
            "buy_max": buy_limits[1],
            "sell_min": sell_limits[0],
            "sell_max": sell_limits[1],
        },
        {
            "name": "no_friction",
            "strategy": base_strategy,
            "buy_premium_pct": 0.0,
            "sell_slippage_pct": 0.0,
            "buy_min": buy_limits[0],
            "buy_max": buy_limits[1],
            "sell_min": sell_limits[0],
            "sell_max": sell_limits[1],
        },
        {
            "name": "no_friction_unlimited_entries",
            "strategy": no_friction_unlimited_strategy,
            "buy_premium_pct": 0.0,
            "sell_slippage_pct": 0.0,
            "buy_min": buy_limits[0],
            "buy_max": buy_limits[1],
            "sell_min": sell_limits[0],
            "sell_max": sell_limits[1],
        },
        {
            "name": "no_friction_no_price_filter",
            "strategy": base_strategy,
            "buy_premium_pct": 0.0,
            "sell_slippage_pct": 0.0,
            "buy_min": 0.0,
            "buy_max": 1.0,
            "sell_min": 0.0,
            "sell_max": 1.0,
        },
    ]

    scenario_states: Dict[str, sim_main.StrategyState] = {}
    for spec in specs:
        states = sim_main.run_simulation(
            replay_events,
            [spec["strategy"]],
            buy_price_premium_pct=float(spec["buy_premium_pct"]),
            buy_min_price=float(spec["buy_min"]),
            buy_max_price=float(spec["buy_max"]),
            sell_min_price=float(spec["sell_min"]),
            sell_max_price=float(spec["sell_max"]),
            sell_slippage_pct=float(spec["sell_slippage_pct"]),
            anti_amplification_guard_enabled=bool(anti_amplification_guard_enabled),
            max_our_vs_leader_per_trade=float(max_our_vs_leader_per_trade),
            max_our_vs_leader_per_market=float(max_our_vs_leader_per_market),
        )
        scenario_states[spec["name"]] = states[0]

    token_ids: List[str] = []
    for state in scenario_states.values():
        for token_id, pos in state.positions.items():
            if pos.size > 1e-12:
                token_ids.append(token_id)
    token_ids = sorted(set(token_ids))

    price_map = sim_main.fetch_prices_for_tokens(
        token_ids,
        timeout_s=float(timeout_s),
        workers=max(1, int(price_workers)),
    )

    scenario_results: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        name = spec["name"]
        row = sim_main.build_strategy_result(scenario_states[name], price_map)
        row["scenario"] = name
        row["buy_price_premium_pct"] = float(spec["buy_premium_pct"])
        row["sell_slippage_pct"] = float(spec["sell_slippage_pct"])
        row["buy_price_limits"] = [float(spec["buy_min"]), float(spec["buy_max"])]
        row["sell_price_limits"] = [float(spec["sell_min"]), float(spec["sell_max"])]
        scenario_results[name] = row

    def pnl(name: str) -> float:
        return _as_float(scenario_results[name].get("total_pnl")) or 0.0

    friction_total = pnl("no_friction") - pnl("baseline_like")
    buy_premium_impact = pnl("buy_premium_off") - pnl("baseline_like")
    sell_slippage_impact = pnl("sell_slippage_off") - pnl("baseline_like")
    entry_cap_impact = pnl("no_friction_unlimited_entries") - pnl("no_friction")
    price_filter_impact = pnl("no_friction_no_price_filter") - pnl("no_friction")
    residual = friction_total - buy_premium_impact - sell_slippage_impact

    impacts = {
        "friction_total": friction_total,
        "buy_premium": buy_premium_impact,
        "sell_slippage": sell_slippage_impact,
        "friction_residual": residual,
        "entry_cap": entry_cap_impact,
        "price_filter": price_filter_impact,
    }

    return {
        "scenarios": scenario_results,
        "impacts": impacts,
        "price_coverage": {
            "resolved": sum(1 for v in price_map.values() if v.resolved and v.price is not None),
            "midpoint": sum(1 for v in price_map.values() if (not v.resolved) and v.price is not None),
            "missing": sum(1 for v in price_map.values() if v.price is None),
            "total_open_tokens": len(token_ids),
        },
    }


def build_root_cause_ranking(
    counterfactuals: List[Dict[str, Any]],
    *,
    sell_raw_count: int,
    mirror_effectiveness: Dict[str, Any],
) -> Dict[str, Any]:
    if not counterfactuals:
        return {"by_pnl_impact": [], "structural_flags": []}

    def avg_of(key: str) -> float:
        vals = []
        for row in counterfactuals:
            impacts = row.get("impacts") or {}
            n = _as_float(impacts.get(key))
            if n is not None:
                vals.append(n)
        return statistics.mean(vals) if vals else 0.0

    pnl_causes = [
        {
            "cause": "buy_premium",
            "avg_pnl_impact_when_removed": avg_of("buy_premium"),
            "description": "PnL gain from removing buy premium only",
        },
        {
            "cause": "sell_slippage",
            "avg_pnl_impact_when_removed": avg_of("sell_slippage"),
            "description": "PnL gain from removing mirror-sell slippage only",
        },
        {
            "cause": "friction_total",
            "avg_pnl_impact_when_removed": avg_of("friction_total"),
            "description": "PnL gain from removing both buy premium and sell slippage",
        },
        {
            "cause": "entry_cap",
            "avg_pnl_impact_when_relaxed": avg_of("entry_cap"),
            "description": "PnL gain from setting max_entries_per_market=9999 under no-friction",
        },
        {
            "cause": "price_filter",
            "avg_pnl_impact_when_relaxed": avg_of("price_filter"),
            "description": "PnL gain from widening buy/sell price filters to [0,1] under no-friction",
        },
    ]

    for cause in pnl_causes:
        if "avg_pnl_impact_when_removed" in cause:
            cause["score"] = max(0.0, float(cause["avg_pnl_impact_when_removed"]))
        else:
            cause["score"] = max(0.0, float(cause["avg_pnl_impact_when_relaxed"]))

    pnl_causes.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)

    cap_ratios: List[float] = []
    for row in counterfactuals:
        cr = row.get("capital_scale") or {}
        ratio = _as_float(cr.get("capital_ratio_vs_leader_buy_flow"))
        if ratio is not None:
            cap_ratios.append(ratio)
    avg_cap_ratio = statistics.mean(cap_ratios) if cap_ratios else None

    structural_flags: List[Dict[str, Any]] = []
    if avg_cap_ratio is not None:
        structural_flags.append(
            {
                "flag": "capital_scale_gap",
                "avg_capital_ratio_vs_leader_buy_flow": avg_cap_ratio,
                "severity": (
                    "high"
                    if avg_cap_ratio < 0.1
                    else ("medium" if avg_cap_ratio < 0.4 else "low")
                ),
                "description": "Strategy notional may be much smaller than leader flow, limiting absolute PnL",
            }
        )

    mirror_effective = bool(mirror_effectiveness.get("mirror_sell_effective"))
    structural_flags.append(
        {
            "flag": "mirror_sell_execution",
            "enabled_effectively": mirror_effective,
            "sell_events_in_window": sell_raw_count,
            "description": mirror_effectiveness.get("reason"),
            "mirrored_sell_exec_ratio": _as_float(mirror_effectiveness.get("mirrored_sell_exec_ratio")),
            "avg_mirrored_sells_per_strategy": _as_float(
                mirror_effectiveness.get("avg_mirrored_sells_per_strategy")
            ),
        }
    )
    if sell_raw_count == 0:
        structural_flags.append(
            {
                "flag": "buy_only_activity_in_window",
                "severity": "high",
                "description": "No SELL activity detected in tracked window; mirror_sell cannot realize exits",
            }
        )
    avg_unresolved_ratio = _as_float(mirror_effectiveness.get("avg_unresolved_position_ratio"))
    if avg_unresolved_ratio is not None and avg_unresolved_ratio > 0.35:
        structural_flags.append(
            {
                "flag": "high_unresolved_position_ratio",
                "severity": "medium" if avg_unresolved_ratio <= 0.6 else "high",
                "avg_unresolved_position_ratio": avg_unresolved_ratio,
                "description": "Large unresolved/open position ratio means valuation mark has high influence.",
            }
        )

    return {
        "by_pnl_impact": pnl_causes,
        "structural_flags": structural_flags,
    }


def build_conclusions(
    *,
    benchmark: Dict[str, Any],
    root_causes: Dict[str, Any],
    counterfactuals: List[Dict[str, Any]],
    data_quality: Dict[str, Any],
) -> List[str]:
    lines: List[str] = []
    actual_delta = _as_float(benchmark.get("actual_window_pnl_delta"))
    if actual_delta is not None:
        lines.append(f"Tracked-window actual PnL delta: {actual_delta:.2f} USDC.")
    else:
        lines.append("Tracked-window actual PnL delta unavailable (insufficient user-pnl series).")

    top_pnl = (root_causes.get("by_pnl_impact") or [])[:3]
    if top_pnl:
        joined = ", ".join(
            f"{item.get('cause')}={float(item.get('score') or 0.0):.2f}"
            for item in top_pnl
        )
        lines.append(f"Top modeled PnL drivers (avg impact): {joined}.")

    mirror = data_quality.get("mirror_sell_effectiveness") or {}
    if not bool(mirror.get("mirror_sell_effective")):
        lines.append(
            f"Mirror-sell appears inactive: {mirror.get('reason') or 'no effect detected'}."
        )
    else:
        exec_ratio = _as_float(mirror.get("mirrored_sell_exec_ratio"))
        if exec_ratio is not None:
            lines.append(f"Mirror-sell execution ratio across strategies: {exec_ratio * 100:.1f}%.")

    if counterfactuals and actual_delta is not None:
        best_cf = None
        best_gap = None
        for row in counterfactuals:
            no_f = _as_float((row.get("scenarios") or {}).get("no_friction", {}).get("total_pnl"))
            if no_f is None:
                continue
            gap = actual_delta - no_f
            if best_gap is None or abs(gap) < abs(best_gap):
                best_gap = gap
                best_cf = row
        if best_cf is not None and best_gap is not None:
            lines.append(
                f"Even after no-friction counterfactual, nearest gap to actual is {best_gap:.2f} "
                f"USDC (strategy={best_cf.get('strategy')})."
            )

    return lines


def main() -> int:
    args = parse_args()
    sim_json_path = Path(args.sim_json)
    if not sim_json_path.exists():
        raise SystemExit(f"sim json not found: {sim_json_path}")

    meta, results = load_sim_payload(sim_json_path)
    address = (str(args.address or "").strip().lower() or str(meta.get("address") or "").strip().lower())
    if not address:
        raise SystemExit("address missing; pass --address or provide meta.address in sim json")

    max_activities = int(
        args.max_activities
        if args.max_activities is not None
        else (_as_int(meta.get("max_activities")) or 5000)
    )
    page_limit = max(1, int(args.page_limit))
    top_k = max(1, int(args.top_k))
    timeout_s = float(args.timeout)
    price_workers = max(1, int(args.price_workers))

    tracked_first_ts = _as_int(meta.get("tracked_first_trade_ts"))
    tracked_last_ts = _as_int(meta.get("tracked_last_trade_ts"))

    buy_limits = meta.get("buy_price_limits") if isinstance(meta.get("buy_price_limits"), list) else [0.01, 0.99]
    sell_limits = (
        meta.get("sell_price_limits")
        if isinstance(meta.get("sell_price_limits"), list)
        else [0.01, 0.99]
    )
    buy_min = _as_float(buy_limits[0]) if len(buy_limits) > 0 else None
    buy_max = _as_float(buy_limits[1]) if len(buy_limits) > 1 else None
    sell_min = _as_float(sell_limits[0]) if len(sell_limits) > 0 else None
    sell_max = _as_float(sell_limits[1]) if len(sell_limits) > 1 else None
    if buy_min is None:
        buy_min = 0.01
    if buy_max is None:
        buy_max = 0.99
    if sell_min is None:
        sell_min = 0.01
    if sell_max is None:
        sell_max = 0.99

    buy_premium_base = _as_float(meta.get("buy_price_premium_pct")) or 0.0
    sell_slip_base = _as_float(meta.get("mirror_sell_slippage_pct")) or 0.0
    amp_guard = (
        (meta.get("amplification_guard_summary") or {})
        if isinstance(meta.get("amplification_guard_summary"), dict)
        else {}
    )
    anti_amp_enabled = bool(amp_guard.get("enabled", True))
    anti_amp_trade = _as_float(amp_guard.get("per_trade_limit"))
    anti_amp_market = _as_float(amp_guard.get("per_market_limit"))
    if anti_amp_trade is None:
        anti_amp_trade = 1.0
    if anti_amp_market is None:
        anti_amp_market = 1.0

    print("=== Gap Diagnose Start ===")
    print(f"sim_json={sim_json_path}")
    print(f"address={address}")
    print(
        f"tracked_window={_fmt_utc(tracked_first_ts)} ~ {_fmt_utc(tracked_last_ts)} "
        f"(max_activities={max_activities})"
    )

    session = requests.Session()

    t0 = time.time()
    benchmark = compute_tracked_window_benchmark(
        session,
        address,
        first_ts=tracked_first_ts,
        last_ts=tracked_last_ts,
    )
    print(f"[benchmark] series_points={benchmark.get('series_points')} elapsed={time.time() - t0:.2f}s")

    t1 = time.time()
    raw_events = sim_main.fetch_activity_events(
        session,
        address,
        max_activities=max(1, max_activities),
        page_limit=page_limit,
        timeout_s=timeout_s,
    )
    if tracked_first_ts is not None and tracked_last_ts is not None:
        window_events = [e for e in raw_events if tracked_first_ts <= int(e.ts) <= tracked_last_ts]
    else:
        window_events = list(raw_events)
    if not window_events:
        window_events = list(raw_events)

    replay_events = sim_main.build_replay_events_with_maker_like(window_events)
    print(
        f"[events] raw={len(raw_events)} window_raw={len(window_events)} "
        f"replay={len(replay_events)} elapsed={time.time() - t1:.2f}s"
    )

    side_counts: Dict[str, int] = {"BUY": 0, "SELL": 0}
    for event in window_events:
        side = str(event.side or "").upper()
        if side in side_counts:
            side_counts[side] += 1
        else:
            side_counts[side] = side_counts.get(side, 0) + 1

    replay_sell_count = sum(1 for e in replay_events if str(e.side or "").upper() == "SELL")
    replay_buy_copy_count = sum(
        1 for e in replay_events if str(e.side or "").upper() == "BUY" and bool(e.copy_signal)
    )

    markets = {
        (e.condition_id or e.token_id)
        for e in window_events
        if (e.condition_id or e.token_id)
    }
    window_first_ts = int(window_events[0].ts) if window_events else None
    window_last_ts = int(window_events[-1].ts) if window_events else None

    window_coverage = {
        "covers_tracked_start": (
            tracked_first_ts is None
            or (window_first_ts is not None and window_first_ts <= tracked_first_ts)
        ),
        "covers_tracked_end": (
            tracked_last_ts is None
            or (window_last_ts is not None and window_last_ts >= tracked_last_ts)
        ),
    }
    window_coverage["likely_truncated"] = not (
        window_coverage["covers_tracked_start"] and window_coverage["covers_tracked_end"]
    )

    exit_mode_effect = analyze_mirror_sell_effectiveness(results)

    opt_summary = meta.get("optimization_summary") if isinstance(meta.get("optimization_summary"), dict) else {}
    roi_tie_threshold = _as_float(opt_summary.get("roi_tie_threshold")) or 0.001
    sorted_results = sim_main.sort_optimizer_rows(results, roi_tie_threshold=roi_tie_threshold)
    selected_rows = sorted_results[:top_k]
    by_strategy = {str(r.get("strategy")): r for r in results}

    leader_buy_total_usd = None
    signal_stats = meta.get("buy_signal_stats")
    if isinstance(signal_stats, dict):
        leader_buy_total_usd = _as_float(signal_stats.get("buy_signal_total_usd"))
    if leader_buy_total_usd is None:
        leader_buy_total_usd = _as_float(meta.get("buy_signal_total_usd"))
    actual_delta = _as_float(benchmark.get("actual_window_pnl_delta"))

    counterfactuals: List[Dict[str, Any]] = []
    for idx, row in enumerate(selected_rows, start=1):
        strategy_name = str(row.get("strategy") or "")
        print(f"[replay] strategy {idx}/{len(selected_rows)}: {strategy_name}")
        strategy_obj = build_strategy_from_row(row)
        replay_out = run_single_strategy_scenarios(
            replay_events,
            strategy_obj,
            buy_premium_base=buy_premium_base,
            sell_slip_base=sell_slip_base,
            buy_limits=(buy_min, buy_max),
            sell_limits=(sell_min, sell_max),
            timeout_s=timeout_s,
            price_workers=price_workers,
            anti_amplification_guard_enabled=anti_amp_enabled,
            max_our_vs_leader_per_trade=anti_amp_trade,
            max_our_vs_leader_per_market=anti_amp_market,
        )

        baseline_json = by_strategy.get(strategy_name, row)
        baseline_json_pnl = _as_float(baseline_json.get("total_pnl"))
        baseline_json_cost = _as_float(baseline_json.get("total_buy_cost"))
        baseline_scaled = sim_main.compute_scaled_metrics(
            total_pnl=baseline_json_pnl,
            total_buy_cost=baseline_json_cost,
            leader_buy_signal_total_usd=leader_buy_total_usd,
            actual_window_pnl_delta=actual_delta,
        )
        cap_ratio = None
        if leader_buy_total_usd is not None and leader_buy_total_usd > 0 and baseline_json_cost is not None:
            cap_ratio = baseline_json_cost / leader_buy_total_usd

        scenario_rows = replay_out["scenarios"] if isinstance(replay_out.get("scenarios"), dict) else {}
        for scenario_name, scenario_row in scenario_rows.items():
            if not isinstance(scenario_row, dict):
                continue
            scaled = sim_main.compute_scaled_metrics(
                total_pnl=_as_float(scenario_row.get("total_pnl")),
                total_buy_cost=_as_float(scenario_row.get("total_buy_cost")),
                leader_buy_signal_total_usd=leader_buy_total_usd,
                actual_window_pnl_delta=actual_delta,
            )
            scenario_row.update(scaled)
            scenario_row["scenario"] = scenario_name

        strategy_record = {
            "strategy": strategy_name,
            "baseline_from_json": {
                "total_pnl": baseline_json_pnl,
                "roi": _as_float(baseline_json.get("roi")),
                "total_buy_cost": baseline_json_cost,
                "capital_ratio_vs_leader_buy_flow": baseline_scaled.get("capital_ratio_vs_leader_buy_flow"),
                "scaled_benchmark_pnl": baseline_scaled.get("scaled_benchmark_pnl"),
                "normalized_gap": baseline_scaled.get("normalized_gap"),
                "capture_rate": baseline_scaled.get("capture_rate"),
                "copied_buys": _as_int(baseline_json.get("copied_buys")),
                "mirrored_sells": _as_int(baseline_json.get("mirrored_sells")),
                "open_positions": _as_int(baseline_json.get("open_positions")),
                "unresolved_positions": _as_int(baseline_json.get("unresolved_positions")),
            },
            "capital_ratio_vs_leader_buy_flow": baseline_scaled.get("capital_ratio_vs_leader_buy_flow"),
            "scaled_benchmark_pnl": baseline_scaled.get("scaled_benchmark_pnl"),
            "normalized_gap": baseline_scaled.get("normalized_gap"),
            "capture_rate": baseline_scaled.get("capture_rate"),
            "capital_scale": {
                "leader_buy_signal_total_usd": leader_buy_total_usd,
                "strategy_total_buy_cost": baseline_json_cost,
                "capital_ratio_vs_leader_buy_flow": cap_ratio,
            },
            "scenarios": scenario_rows,
            "impacts": replay_out["impacts"],
            "price_coverage": replay_out["price_coverage"],
        }

        if actual_delta is not None:
            baseline_like_pnl = _as_float(replay_out["scenarios"]["baseline_like"].get("total_pnl")) or 0.0
            no_friction_pnl = _as_float(replay_out["scenarios"]["no_friction"].get("total_pnl")) or 0.0
            strategy_record["gap_vs_actual_window"] = {
                "actual_window_pnl_delta": actual_delta,
                "baseline_like_gap": actual_delta - baseline_like_pnl,
                "no_friction_gap": actual_delta - no_friction_pnl,
            }

        counterfactuals.append(strategy_record)

    root_causes = build_root_cause_ranking(
        counterfactuals,
        sell_raw_count=int(side_counts.get("SELL", 0) or 0),
        mirror_effectiveness=exit_mode_effect,
    )

    data_quality = {
        "activity_window": {
            "raw_event_count": len(raw_events),
            "window_event_count": len(window_events),
            "window_first_trade_ts": window_first_ts,
            "window_last_trade_ts": window_last_ts,
            "window_first_trade_utc": _fmt_utc(window_first_ts),
            "window_last_trade_utc": _fmt_utc(window_last_ts),
            "tracked_first_trade_ts": tracked_first_ts,
            "tracked_last_trade_ts": tracked_last_ts,
            "tracked_first_trade_utc": _fmt_utc(tracked_first_ts),
            "tracked_last_trade_utc": _fmt_utc(tracked_last_ts),
            "unique_market_count": len(markets),
            "side_counts": side_counts,
            "window_coverage": window_coverage,
        },
        "replay_stats": {
            "replay_event_count": len(replay_events),
            "replay_buy_copy_signal_count": replay_buy_copy_count,
            "replay_sell_event_count": replay_sell_count,
        },
        "mirror_sell_effectiveness": exit_mode_effect,
    }

    conclusions = build_conclusions(
        benchmark=benchmark,
        root_causes=root_causes,
        counterfactuals=counterfactuals,
        data_quality=data_quality,
    )

    output = {
        "generated_at": now_utc_iso(),
        "input": {
            "sim_json": str(sim_json_path),
            "address": address,
            "max_activities": max_activities,
            "page_limit": page_limit,
            "top_k": top_k,
            "timeout": timeout_s,
            "price_workers": price_workers,
        },
        "meta": meta,
        "benchmark": benchmark,
        "data_quality": data_quality,
        "strategy_counterfactuals": counterfactuals,
        "root_cause_ranking": root_causes,
        "conclusions": conclusions,
    }

    if args.out_json:
        out_path = Path(args.out_json)
    else:
        out_path = (
            Path(__file__).resolve().parent
            / "output"
            / f"gap_analysis_{_short_address(address)}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print("=== Top Root Causes ===")
    for idx, row in enumerate((root_causes.get("by_pnl_impact") or [])[:5], start=1):
        if "avg_pnl_impact_when_removed" in row:
            impact = _as_float(row.get("avg_pnl_impact_when_removed")) or 0.0
        else:
            impact = _as_float(row.get("avg_pnl_impact_when_relaxed")) or 0.0
        print(f"[{idx}] {row.get('cause')}: avg impact {impact:+.2f}")

    for flag in root_causes.get("structural_flags") or []:
        print(f"- {flag.get('flag')}: {flag.get('description')}")

    actual_delta = _as_float(benchmark.get("actual_window_pnl_delta"))
    if actual_delta is not None:
        print(f"actual_window_pnl_delta={actual_delta:.2f} USDC")
    else:
        print("actual_window_pnl_delta=N/A")
    print(f"output_json={out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
