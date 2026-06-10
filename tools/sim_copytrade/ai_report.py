from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union


REQUIRED_ANALYSIS_KEYS = [
    "objective",
    "what_was_executed",
    "key_results",
    "gap_to_leader",
    "root_causes",
    "executed_experiments",
    "best_improvement",
    "rejected_trials",
    "final_winner_reason",
    "entries_depth_evidence",
    "why_entries_gt_avg",
    "amplification_guard_findings",
    "final_actionable_next_step",
    "caveats",
]


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> Optional[int]:
    n = _as_float(value)
    return int(round(n)) if n is not None else None


def _fmt_money(value: Any) -> str:
    n = _as_float(value)
    return f"{n:,.2f}" if n is not None else "N/A"


def _fmt_pct(value: Any) -> str:
    n = _as_float(value)
    return f"{n * 100:.2f}%" if n is not None else "N/A"


def _fmt_ratio(value: Any, digits: int = 3) -> str:
    n = _as_float(value)
    return f"{n:.{digits}f}" if n is not None else "N/A"


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON root must be object: {path}")
    return payload


def _default_output_paths(sim_json_path: Path, out_md: Optional[Path], out_json: Optional[Path]) -> tuple[Path, Path]:
    stem = sim_json_path.stem
    if stem.startswith("sim_results_"):
        base = "analysis_" + stem[len("sim_results_") :]
    else:
        base = f"analysis_{stem}"
    md_path = out_md if out_md is not None else (sim_json_path.parent / f"{base}.md")
    if out_json is not None:
        json_path = out_json
    elif out_md is not None:
        json_path = out_md.with_suffix(".json")
    else:
        json_path = sim_json_path.parent / f"{base}.json"
    return md_path, json_path


def _optimizer_sort(results: Sequence[Dict[str, Any]], tie: float) -> List[Dict[str, Any]]:
    rows = [row for row in results if isinstance(row, dict)]
    rows = sorted(
        rows,
        key=lambda row: (
            _as_float(row.get("roi")) if _as_float(row.get("roi")) is not None else float("-inf"),
            _as_float(row.get("total_buy_cost")) if _as_float(row.get("total_buy_cost")) is not None else float("-inf"),
            _as_float(row.get("total_pnl")) if _as_float(row.get("total_pnl")) is not None else float("-inf"),
        ),
        reverse=True,
    )
    tie = max(0.0, float(tie))
    if tie <= 0 or len(rows) <= 1:
        return rows
    out = [rows[0]]
    for row in rows[1:]:
        prev = out[-1]
        prev_roi = _as_float(prev.get("roi"))
        cur_roi = _as_float(row.get("roi"))
        if prev_roi is None or cur_roi is None or abs(cur_roi - prev_roi) > tie:
            out.append(row)
            continue
        prev_cost = _as_float(prev.get("total_buy_cost")) or float("-inf")
        cur_cost = _as_float(row.get("total_buy_cost")) or float("-inf")
        if cur_cost > prev_cost:
            out[-1] = row
        else:
            out.append(row)
    return out


def _sort_by_pnl(results: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        [row for row in results if isinstance(row, dict)],
        key=lambda row: (
            _as_float(row.get("total_pnl")) if _as_float(row.get("total_pnl")) is not None else float("-inf"),
            _as_float(row.get("roi")) if _as_float(row.get("roi")) is not None else float("-inf"),
            _as_float(row.get("total_buy_cost")) if _as_float(row.get("total_buy_cost")) is not None else float("-inf"),
        ),
        reverse=True,
    )


def _find_row(results: Sequence[Dict[str, Any]], strategy_name: Any) -> Optional[Dict[str, Any]]:
    target = str(strategy_name or "").strip()
    if not target:
        return None
    for row in results:
        if str(row.get("strategy") or "") == target:
            return row
    return None


def _row_brief(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {
        "strategy": row.get("strategy"),
        "copy_mode": row.get("copy_mode"),
        "fixed_usd": _as_float(row.get("fixed_usd")),
        "proportional_pct": _as_float(row.get("proportional_pct")),
        "proportional_cap_usd": _as_float(row.get("proportional_cap_usd")),
        "max_entries_per_market": _as_int(row.get("max_entries_per_market")),
        "roi": _as_float(row.get("roi")),
        "total_pnl": _as_float(row.get("total_pnl")),
        "total_buy_cost": _as_float(row.get("total_buy_cost")),
        "copied_buys": _as_int(row.get("copied_buys")),
        "mirrored_sells": _as_int(row.get("mirrored_sells")),
        "scaled_benchmark_pnl": _as_float(row.get("scaled_benchmark_pnl")),
        "normalized_gap": _as_float(row.get("normalized_gap")),
        "capture_rate": _as_float(row.get("capture_rate")),
        "guard_trimmed_count": _as_int(row.get("guard_trimmed_count")),
        "guard_trimmed_usd": _as_float(row.get("guard_trimmed_usd")),
        "guard_skipped_count": _as_int(row.get("guard_skipped_count")),
        "oversize_event_rate_before_guard": _as_float(row.get("oversize_event_rate_before_guard")),
        "oversize_event_rate_after_guard": _as_float(row.get("oversize_event_rate_after_guard")),
    }


def _build_entries_fallback(results: Sequence[Dict[str, Any]], avg_bets: Optional[float], tie: float) -> Dict[str, Any]:
    ranked = _optimizer_sort(results, tie)
    by_entries: Dict[int, Dict[str, Any]] = {}
    for row in ranked:
        entries = _as_int(row.get("max_entries_per_market"))
        if entries is None or entries <= 0:
            continue
        if entries not in by_entries:
            by_entries[entries] = row

    curve = [
        {
            "entries": entries,
            "strategy": row.get("strategy"),
            "roi": _as_float(row.get("roi")),
            "total_pnl": _as_float(row.get("total_pnl")),
            "total_buy_cost": _as_float(row.get("total_buy_cost")),
        }
        for entries, row in sorted(by_entries.items())
    ]
    segments: List[Dict[str, Any]] = []
    for idx in range(1, len(curve)):
        prev = curve[idx - 1]
        cur = curve[idx]
        segments.append(
            {
                "from_entries": prev.get("entries"),
                "to_entries": cur.get("entries"),
                "delta_total_pnl": (_as_float(cur.get("total_pnl")) or 0.0) - (_as_float(prev.get("total_pnl")) or 0.0),
                "delta_roi": (_as_float(cur.get("roi")) or 0.0) - (_as_float(prev.get("roi")) or 0.0),
            }
        )
    best_entries = _as_int(ranked[0].get("max_entries_per_market")) if ranked else None
    why = {
        "avg_bets_per_market": avg_bets,
        "best_optimizer_entries": best_entries,
        "entries_minus_avg": (float(best_entries) - avg_bets) if (best_entries is not None and avg_bets is not None) else None,
        "high_entries_zone_threshold": int(math.floor(avg_bets)) if avg_bets is not None else None,
    }
    return {
        "market_bet_count_distribution": {},
        "entries_curve": curve,
        "marginal_segments": segments,
        "top_market_contributors": [],
        "why_entries_gt_avg": why,
    }


def _build_analysis_payload(sim_json_path: Path, sim_payload: Dict[str, Any], gap_payload: Optional[Dict[str, Any]], gap_json_path: Optional[Path]) -> Dict[str, Any]:
    meta = sim_payload.get("meta") if isinstance(sim_payload.get("meta"), dict) else {}
    results_raw = sim_payload.get("results")
    if not isinstance(results_raw, list):
        raise RuntimeError(f"sim json missing results list: {sim_json_path}")
    results = [row for row in results_raw if isinstance(row, dict)]
    if not results:
        raise RuntimeError(f"sim json has no valid strategy rows: {sim_json_path}")

    opt_summary = meta.get("optimization_summary") if isinstance(meta.get("optimization_summary"), dict) else {}
    ai_summary = meta.get("ai_improvement_summary") if isinstance(meta.get("ai_improvement_summary"), dict) else {}
    buy_signal_stats = meta.get("buy_signal_stats") if isinstance(meta.get("buy_signal_stats"), dict) else {}
    tie = _as_float(opt_summary.get("roi_tie_threshold")) or 0.001

    ranked_obj = _optimizer_sort(results, tie)
    ranked_pnl = _sort_by_pnl(results)
    best_obj = _find_row(results, (meta.get("best_by_objective") or {}).get("strategy")) if isinstance(meta.get("best_by_objective"), dict) else None
    if best_obj is None:
        best_obj = ranked_obj[0] if ranked_obj else (ranked_pnl[0] if ranked_pnl else {})
    best_pnl = ranked_pnl[0] if ranked_pnl else best_obj
    best_raw_roi = _find_row(results, (meta.get("best_by_raw_roi") or {}).get("strategy")) if isinstance(meta.get("best_by_raw_roi"), dict) else None
    if best_raw_roi is None:
        roi_rows = [row for row in results if _as_float(row.get("roi")) is not None]
        roi_rows = sorted(
            roi_rows,
            key=lambda row: (
                _as_float(row.get("roi")) if _as_float(row.get("roi")) is not None else float("-inf"),
                _as_float(row.get("total_buy_cost")) if _as_float(row.get("total_buy_cost")) is not None else float("-inf"),
                _as_float(row.get("total_pnl")) if _as_float(row.get("total_pnl")) is not None else float("-inf"),
            ),
            reverse=True,
        )
        best_raw_roi = roi_rows[0] if roi_rows else best_obj

    entries_depth_raw = meta.get("entries_depth_evidence") if isinstance(meta.get("entries_depth_evidence"), dict) else {}
    if not entries_depth_raw:
        entries_depth_raw = _build_entries_fallback(results, _as_float(buy_signal_stats.get("avg_bets_per_market")), tie)
    top_market_contributors_raw = (
        entries_depth_raw.get("top_market_contributors")
        if isinstance(entries_depth_raw.get("top_market_contributors"), list)
        else []
    )
    top_market_contributors = sorted(
        [row for row in top_market_contributors_raw if isinstance(row, dict)],
        key=lambda row: abs(_as_float(row.get("delta_pnl")) or 0.0),
        reverse=True,
    )[:10]
    marginal_segments_raw = (
        entries_depth_raw.get("marginal_segments")
        if isinstance(entries_depth_raw.get("marginal_segments"), list)
        else []
    )
    marginal_segments = sorted(
        [row for row in marginal_segments_raw if isinstance(row, dict)],
        key=lambda row: abs(_as_float(row.get("delta_total_pnl")) or 0.0),
        reverse=True,
    )[:10]
    entries_curve_raw = entries_depth_raw.get("entries_curve") if isinstance(entries_depth_raw.get("entries_curve"), list) else []
    entries_curve = [row for row in entries_curve_raw if isinstance(row, dict)][:12]
    entries_depth = {
        "market_bet_count_distribution": (
            entries_depth_raw.get("market_bet_count_distribution")
            if isinstance(entries_depth_raw.get("market_bet_count_distribution"), dict)
            else {}
        ),
        "entries_curve": entries_curve,
        "marginal_segments": marginal_segments,
        "top_market_contributors": top_market_contributors,
        "why_entries_gt_avg": (
            entries_depth_raw.get("why_entries_gt_avg")
            if isinstance(entries_depth_raw.get("why_entries_gt_avg"), dict)
            else {}
        ),
    }
    why_entries = entries_depth.get("why_entries_gt_avg") if isinstance(entries_depth.get("why_entries_gt_avg"), dict) else {}

    executed_raw = ai_summary.get("executed_experiments") if isinstance(ai_summary.get("executed_experiments"), list) else []

    def _slim_experiment(row: Dict[str, Any]) -> Dict[str, Any]:
        before = row.get("before") if isinstance(row.get("before"), dict) else {}
        after = row.get("after") if isinstance(row.get("after"), dict) else {}
        delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
        return {
            "round": _as_int(row.get("round")),
            "improved": bool(row.get("improved")),
            "candidates_executed": _as_int(row.get("candidates_executed")),
            "elapsed_s": _as_float(row.get("elapsed_s")),
            "before": {
                "strategy": before.get("strategy"),
                "roi": _as_float(before.get("roi")),
                "total_buy_cost": _as_float(before.get("total_buy_cost")),
                "total_pnl": _as_float(before.get("total_pnl")),
            },
            "after": {
                "strategy": after.get("strategy"),
                "roi": _as_float(after.get("roi")),
                "total_buy_cost": _as_float(after.get("total_buy_cost")),
                "total_pnl": _as_float(after.get("total_pnl")),
            },
            "delta": {
                "roi": _as_float(delta.get("roi")),
                "total_buy_cost": _as_float(delta.get("total_buy_cost")),
                "total_pnl": _as_float(delta.get("total_pnl")),
            },
            "candidate_samples": [str(v) for v in (row.get("candidate_samples") or [])[:5]],
            "candidate_mutations": [
                {
                    "strategy": (m.get("strategy") if isinstance(m, dict) else None),
                    "parent_strategy": (m.get("parent_strategy") if isinstance(m, dict) else None),
                    "mutation": (m.get("mutation") if isinstance(m, dict) else None),
                }
                for m in (row.get("candidate_mutations") or [])[:5]
                if isinstance(m, dict)
            ],
        }

    executed = [_slim_experiment(row) for row in executed_raw if isinstance(row, dict)][:20]
    improved = [row for row in executed if isinstance(row, dict) and bool(row.get("improved"))]
    rejected = [row for row in executed if isinstance(row, dict) and not bool(row.get("improved"))]
    best_improvement = None
    if improved:
        best_improvement = max(
            improved,
            key=lambda row: (
                _as_float(((row.get("delta") or {}).get("roi"))) or float("-inf"),
                _as_float(((row.get("delta") or {}).get("total_buy_cost"))) or float("-inf"),
                _as_float(((row.get("delta") or {}).get("total_pnl"))) or float("-inf"),
            ),
        )

    amp_summary = meta.get("amplification_guard_summary") if isinstance(meta.get("amplification_guard_summary"), dict) else {}
    amp_agg = amp_summary.get("aggregate") if isinstance(amp_summary.get("aggregate"), dict) else {}
    oversize = meta.get("oversize_event_rate") if isinstance(meta.get("oversize_event_rate"), dict) else {}
    amp_findings = {
        "enabled": bool(amp_summary.get("enabled", False)),
        "per_trade_limit": _as_float(amp_summary.get("per_trade_limit")),
        "per_market_limit": _as_float(amp_summary.get("per_market_limit")),
        "aggregate": {
            "copied_buys": _as_int(amp_agg.get("copied_buys")),
            "trimmed_count": _as_int(amp_agg.get("trimmed_count")),
            "trimmed_usd": _as_float(amp_agg.get("trimmed_usd")),
            "skipped_guard_count": _as_int(amp_agg.get("skipped_guard_count")),
            "oversize_event_rate_before_guard": _as_float(amp_agg.get("oversize_event_rate_before_guard")),
            "oversize_event_rate_after_guard": _as_float(amp_agg.get("oversize_event_rate_after_guard")),
        },
        "objective_winner": {
            "strategy": best_obj.get("strategy"),
            "guard_trimmed_count": _as_int(best_obj.get("guard_trimmed_count")),
            "guard_trimmed_usd": _as_float(best_obj.get("guard_trimmed_usd")),
            "guard_skipped_count": _as_int(best_obj.get("guard_skipped_count")),
            "oversize_event_rate_before_guard": _as_float(best_obj.get("oversize_event_rate_before_guard")),
            "oversize_event_rate_after_guard": _as_float(best_obj.get("oversize_event_rate_after_guard")),
        },
        "oversize_event_rate": {
            "objective_before_guard": _as_float(oversize.get("objective_before_guard")),
            "objective_after_guard": _as_float(oversize.get("objective_after_guard")),
            "aggregate_before_guard": _as_float(oversize.get("aggregate_before_guard")),
            "aggregate_after_guard": _as_float(oversize.get("aggregate_after_guard")),
        },
    }

    actual_delta = _as_float(meta.get("actual_window_pnl_delta"))
    best_obj_pnl = _as_float(best_obj.get("total_pnl"))
    gap_summary: Dict[str, Any] = {"provided": False}
    if isinstance(gap_payload, dict):
        gap_summary = {
            "provided": True,
            "root_cause_ranking": gap_payload.get("root_cause_ranking") if isinstance(gap_payload.get("root_cause_ranking"), dict) else {},
            "conclusions": gap_payload.get("conclusions") if isinstance(gap_payload.get("conclusions"), list) else [],
            "data_quality": gap_payload.get("data_quality") if isinstance(gap_payload.get("data_quality"), dict) else {},
        }

    ai_summary_slim = {
        "enabled": bool(ai_summary.get("enabled")),
        "objective": ai_summary.get("objective"),
        "roi_tie_threshold": _as_float(ai_summary.get("roi_tie_threshold")),
        "bound_profile": ai_summary.get("bound_profile"),
        "budget_minutes": _as_float(ai_summary.get("budget_minutes")),
        "rounds_requested": _as_int(ai_summary.get("rounds_requested")),
        "rounds_executed": _as_int(ai_summary.get("rounds_executed")),
        "improved_rounds": _as_int(ai_summary.get("improved_rounds")),
        "top_candidates_per_round": _as_int(ai_summary.get("top_candidates_per_round")),
        "stop_reason": ai_summary.get("stop_reason"),
        "final_winner": (
            _row_brief(ai_summary.get("final_winner"))
            if isinstance(ai_summary.get("final_winner"), dict)
            else {}
        ),
        "executed_experiments_count": len(executed_raw),
    }

    window_meta = meta.get("window_analysis") if isinstance(meta.get("window_analysis"), dict) else {}
    window_top_n = _as_int(window_meta.get("top_n")) or 10
    window_rows_raw = window_meta.get("windows") if isinstance(window_meta.get("windows"), list) else []

    def _brief_rows(rows_raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(rows_raw, list):
            return []
        rows: List[Dict[str, Any]] = []
        for row in rows_raw[: max(1, int(window_top_n))]:
            if not isinstance(row, dict):
                continue
            rows.append(_row_brief(row))
        return rows

    window_rows: List[Dict[str, Any]] = []
    for idx, win_raw in enumerate(window_rows_raw[:5], start=1):
        if not isinstance(win_raw, dict):
            continue
        rows_full = [row for row in (win_raw.get("rows") or []) if isinstance(row, dict)]
        top_total_pnl = _brief_rows(win_raw.get("top10_total_pnl"))
        if not top_total_pnl and rows_full:
            top_total_pnl = [_row_brief(row) for row in _sort_by_pnl(rows_full)[: max(1, int(window_top_n))]]
        top_roi = _brief_rows(win_raw.get("top10_roi"))
        if not top_roi and rows_full:
            roi_ranked = sorted(
                [row for row in rows_full if _as_float(row.get("roi")) is not None],
                key=lambda row: (
                    _as_float(row.get("roi")) if _as_float(row.get("roi")) is not None else float("-inf"),
                    _as_float(row.get("total_buy_cost")) if _as_float(row.get("total_buy_cost")) is not None else float("-inf"),
                    _as_float(row.get("total_pnl")) if _as_float(row.get("total_pnl")) is not None else float("-inf"),
                ),
                reverse=True,
            )
            top_roi = [_row_brief(row) for row in roi_ranked[: max(1, int(window_top_n))]]
        window_rows.append(
            {
                "window_id": str(win_raw.get("window_id") or f"window_{idx}"),
                "title": str(win_raw.get("title") or f"窗口{idx}"),
                "activity_range": str(win_raw.get("activity_range") or ""),
                "count": _as_int(win_raw.get("count")),
                "start_utc": win_raw.get("start_utc"),
                "end_utc": win_raw.get("end_utc"),
                "best_by_objective": (
                    _row_brief(win_raw.get("best_by_objective"))
                    if isinstance(win_raw.get("best_by_objective"), dict)
                    else {}
                ),
                "top10_total_pnl": top_total_pnl,
                "top10_roi": top_roi,
            }
        )

    window_analysis = {
        "window_count": _as_int(window_meta.get("window_count")),
        "split_basis": window_meta.get("split_basis"),
        "top_n": window_top_n,
        "windows": window_rows,
    }

    return {
        "source": {"sim_json": str(sim_json_path), "gap_json": str(gap_json_path) if gap_json_path is not None else None},
        "objective": "roi_then_scale",
        "run_meta": {
            "address": str(meta.get("address") or ""),
            "max_activities": _as_int(meta.get("max_activities")),
            "fetched_events": _as_int(meta.get("fetched_events")),
            "replay_events": _as_int(meta.get("replay_events")),
            "tracked_first_trade_utc": meta.get("tracked_first_trade_utc"),
            "tracked_last_trade_utc": meta.get("tracked_last_trade_utc"),
            "tracked_span_days": _as_float(meta.get("tracked_span_days")),
            "actual_window_pnl_delta": actual_delta,
            "leader_buy_signal_total_usd": _as_float(meta.get("leader_buy_signal_total_usd")),
        },
        "optimizer": {
            "objective": opt_summary.get("objective") or "roi_then_scale",
            "roi_tie_threshold": tie,
            "enabled": bool(opt_summary.get("enabled")),
            "rounds_executed": _as_int(opt_summary.get("rounds_executed")),
            "expansion_rounds": _as_int(opt_summary.get("expansion_rounds")),
        },
        "ai_improvement_summary": ai_summary_slim,
        "executed_experiments": executed,
        "best_improvement": best_improvement,
        "rejected_trials": rejected,
        "entries_depth_evidence": entries_depth,
        "why_entries_gt_avg": why_entries,
        "amplification_guard_findings": amp_findings,
        "result_summary": {
            "best_by_objective": _row_brief(best_obj),
            "best_by_total_pnl": _row_brief(best_pnl),
            "best_by_raw_roi": _row_brief(best_raw_roi),
            "best_objective_gap_vs_actual_window": (actual_delta - best_obj_pnl) if (actual_delta is not None and best_obj_pnl is not None) else None,
            "top5_by_objective": [_row_brief(row) for row in ranked_obj[:5]],
            "top5_by_total_pnl": [_row_brief(row) for row in ranked_pnl[:5]],
        },
        "window_analysis": window_analysis,
        "gap_diagnose": gap_summary,
        "narrative_constraints": {
            "suppress_as_main_causes": {
                "capital_scale_mismatch": True,
                "short_window_days": True,
                "mirror_sell_count": True,
            },
            "must_cite_evidence_fields": [
                "entries_depth_evidence.top_market_contributors",
                "entries_depth_evidence.market_bet_count_distribution",
                "entries_depth_evidence.marginal_segments",
                "amplification_guard_findings",
                "window_analysis.windows",
            ],
        },
    }


def _ensure_required_fields(analysis: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in REQUIRED_ANALYSIS_KEYS:
        value = analysis.get(key)
        if value is None:
            if key in {"root_causes", "executed_experiments", "rejected_trials", "final_actionable_next_step", "caveats"}:
                out[key] = []
            elif key in {"best_improvement", "entries_depth_evidence", "why_entries_gt_avg", "amplification_guard_findings"}:
                out[key] = {}
            else:
                out[key] = ""
        else:
            out[key] = value
    return out


def _fallback_analysis(payload: Dict[str, Any], error_message: Optional[str]) -> Dict[str, Any]:
    summary = payload.get("result_summary") if isinstance(payload.get("result_summary"), dict) else {}
    run_meta = payload.get("run_meta") if isinstance(payload.get("run_meta"), dict) else {}
    best = summary.get("best_by_objective") if isinstance(summary.get("best_by_objective"), dict) else {}
    entries_depth = payload.get("entries_depth_evidence") if isinstance(payload.get("entries_depth_evidence"), dict) else {}
    why_entries = payload.get("why_entries_gt_avg") if isinstance(payload.get("why_entries_gt_avg"), dict) else {}
    amp_findings = payload.get("amplification_guard_findings") if isinstance(payload.get("amplification_guard_findings"), dict) else {}
    executed = payload.get("executed_experiments") if isinstance(payload.get("executed_experiments"), list) else []

    why_text = (
        f"平均每市场下注次数约 {_fmt_ratio(why_entries.get('avg_bets_per_market'))}，"
        f"但最优 entries 为 {_fmt_ratio(why_entries.get('best_optimizer_entries'), 0)}。"
        f"分位数上 p90={_fmt_ratio((entries_depth.get('market_bet_count_distribution') or {}).get('p90'))}、"
        f"p95={_fmt_ratio((entries_depth.get('market_bet_count_distribution') or {}).get('p95'))}，"
        "说明少数高频市场对最终收益的边际贡献显著，最优解会主动提高 entries 深度以覆盖这些尾部机会。"
    )
    amp_agg = amp_findings.get("aggregate") if isinstance(amp_findings.get("aggregate"), dict) else {}
    amp_text = (
        f"防放大约束生效后，超限事件率由 {_fmt_pct(amp_agg.get('oversize_event_rate_before_guard'))} "
        f"下降到 {_fmt_pct(amp_agg.get('oversize_event_rate_after_guard'))}，"
        f"累计裁剪金额约 {_fmt_money(amp_agg.get('trimmed_usd'))} USDC。"
    )
    executed_simple = []
    for row in executed[:8]:
        if not isinstance(row, dict):
            continue
        delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
        executed_simple.append(
            {
                "round": row.get("round"),
                "improved": bool(row.get("improved")),
                "delta_roi": _as_float(delta.get("roi")),
                "delta_total_buy_cost": _as_float(delta.get("total_buy_cost")),
                "delta_total_pnl": _as_float(delta.get("total_pnl")),
            }
        )
    caveats = ["本次报告由本地降级模板生成（模型调用异常），结论来自同一份运行数据与证据字段。"]
    if error_message:
        caveats.append(f"降级原因：{error_message}")

    return _ensure_required_fields(
        {
            "objective": "本研究以“ROI 主目标 + 规模次目标(total_buy_cost)”为唯一优化准则，目标是在可执行约束下解释并提升模拟跟单表现。",
            "what_was_executed": "程序先完成基线回放，再执行多轮参数实验并记录每轮 before/after 与增量结果，输出的是已执行证据而非建议清单。",
            "key_results": (
                f"当前冠军策略为 {best.get('strategy') or 'N/A'}，"
                f"ROI={_fmt_pct(best.get('roi'))}，投入={_fmt_money(best.get('total_buy_cost'))} USDC，"
                f"总收益={_fmt_money(best.get('total_pnl'))} USDC。"
            ),
            "gap_to_leader": (
                f"在 tracked-window 口径下，Leader 实际收益增量约 {_fmt_money(run_meta.get('actual_window_pnl_delta'))} USDC，"
                f"冠军策略收益约 {_fmt_money(best.get('total_pnl'))} USDC；两者差距主要通过执行证据与市场级贡献拆解。"
            ),
            "root_causes": [
                "entries 深度并非“平均下注次数”的线性延伸，而是用于覆盖尾部高频市场的关键机制。",
                "防放大约束有效抑制了超过 leader 尺寸导致的虚假放大收益，使结果更接近可执行现实。",
                "执行摩擦会压缩收益，但默认不作为主因，需在阈值触发时才进入附注解释。",
            ],
            "executed_experiments": executed_simple,
            "best_improvement": payload.get("best_improvement") if isinstance(payload.get("best_improvement"), dict) else {},
            "rejected_trials": payload.get("rejected_trials") if isinstance(payload.get("rejected_trials"), list) else [],
            "final_winner_reason": "冠军在 ROI 同档阈值内保持更高规模且总收益不劣，满足“ROI主、规模次”的主目标函数，因此被选为最终方案。",
            "entries_depth_evidence": entries_depth,
            "why_entries_gt_avg": {**why_entries, "explanation": why_text},
            "amplification_guard_findings": {**amp_findings, "summary": amp_text},
            "final_actionable_next_step": [
                "在冠军同 sizing 条件下继续做 entries ±2 的单维实验，验证边际收益是否仍为正。",
                "维持 ROI 同档阈值 0.001，并在同档内优先更大 total_buy_cost。",
                "保持防放大约束开启，避免实验结果被超尺寸运单扭曲。",
            ],
            "caveats": caveats,
        }
    )


def _as_text(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, str):
        s = value.strip()
        return s if s else "N/A"
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                t = item.strip()
                if t:
                    parts.append(t)
            elif isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "；".join(parts) if parts else "N/A"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _markdown_table(headers: List[str], rows: List[List[str]]) -> List[str]:
    if not rows:
        return ["（无）"]
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


def _window_top_rows_for_metric_md(
    window_row: Dict[str, Any],
    *,
    metric_key: str,
    top_n: int,
) -> List[Dict[str, Any]]:
    key = "top10_total_pnl" if metric_key == "total_pnl" else "top10_roi"
    rows_raw = window_row.get(key)
    if not isinstance(rows_raw, list):
        return []
    rows = [row for row in rows_raw if isinstance(row, dict)]
    return rows[: max(1, int(top_n))]


def _render_window_analysis_markdown(window_analysis: Dict[str, Any]) -> List[str]:
    windows = window_analysis.get("windows") if isinstance(window_analysis.get("windows"), list) else []
    if not windows:
        return []
    top_n = _as_int(window_analysis.get("top_n")) or 10

    lines: List[str] = [
        "## 窗口分析（全量 + 4切片）",
        "",
        f"- 切分基准: {window_analysis.get('split_basis') or 'deduped_trade_activities'}",
        f"- 窗口数量: {_as_int(window_analysis.get('window_count')) or len(windows)}",
        f"- 每窗口展示: Top{top_n}",
        "",
    ]
    for window in windows:
        if not isinstance(window, dict):
            continue
        lines.append(
            f"- {window.get('title') or window.get('window_id')}: {window.get('activity_range') or 'N/A'} | "
            f"{window.get('start_utc') or 'N/A'} ~ {window.get('end_utc') or 'N/A'}"
        )
    lines.append("")

    window_titles = [
        str(window.get("title") or window.get("window_id") or f"窗口{idx}").replace("|", "\\|")
        for idx, window in enumerate(windows, start=1)
    ]
    pnl_rows = [_window_top_rows_for_metric_md(window, metric_key="total_pnl", top_n=top_n) for window in windows]
    roi_rows = [_window_top_rows_for_metric_md(window, metric_key="roi", top_n=top_n) for window in windows]

    def _build_metric_table(metric_key: str, sampled_rows: List[List[Dict[str, Any]]]) -> List[List[str]]:
        rows: List[List[str]] = []
        for rank_idx in range(top_n):
            row_cells = [str(rank_idx + 1)]
            for each_window_rows in sampled_rows:
                if rank_idx >= len(each_window_rows):
                    row_cells.append("-")
                    continue
                item = each_window_rows[rank_idx]
                strategy = str(item.get("strategy") or "N/A").replace("|", "\\|")
                if metric_key == "total_pnl":
                    value = _fmt_money(item.get("total_pnl"))
                else:
                    value = _fmt_pct(item.get("roi"))
                row_cells.append(f"{strategy} / {value}")
            rows.append(row_cells)
        return rows

    lines.append(f"### 策略总收益 Top{top_n}（全量 + 4切片）")
    lines.append("")
    lines.extend(_markdown_table(["排名"] + window_titles, _build_metric_table("total_pnl", pnl_rows)))
    lines.append("")
    lines.append(f"### 策略 ROI Top{top_n}（全量 + 4切片）")
    lines.append("")
    lines.extend(_markdown_table(["排名"] + window_titles, _build_metric_table("roi", roi_rows)))
    lines.append("")
    return lines


def _render_markdown(report_obj: Dict[str, Any]) -> str:
    analysis = report_obj.get("analysis") if isinstance(report_obj.get("analysis"), dict) else {}
    payload = report_obj.get("analysis_payload") if isinstance(report_obj.get("analysis_payload"), dict) else {}
    run_meta = payload.get("run_meta") if isinstance(payload.get("run_meta"), dict) else {}
    summary = payload.get("result_summary") if isinstance(payload.get("result_summary"), dict) else {}
    best = summary.get("best_by_objective") if isinstance(summary.get("best_by_objective"), dict) else {}
    entries_depth = analysis.get("entries_depth_evidence") if isinstance(analysis.get("entries_depth_evidence"), dict) else {}
    entries_dist = entries_depth.get("market_bet_count_distribution") if isinstance(entries_depth.get("market_bet_count_distribution"), dict) else {}
    top_markets = entries_depth.get("top_market_contributors") if isinstance(entries_depth.get("top_market_contributors"), list) else []
    amp_findings = analysis.get("amplification_guard_findings") if isinstance(analysis.get("amplification_guard_findings"), dict) else {}
    amp_agg = amp_findings.get("aggregate") if isinstance(amp_findings.get("aggregate"), dict) else {}
    executed = analysis.get("executed_experiments") if isinstance(analysis.get("executed_experiments"), list) else []
    window_analysis = payload.get("window_analysis") if isinstance(payload.get("window_analysis"), dict) else {}

    lines = [
        "# 模拟跟单研究报告（执行版）",
        "",
        f"生成时间（UTC）：{report_obj.get('generated_at')}",
        f"地址：{run_meta.get('address') or 'N/A'}",
        f"追踪窗口：{run_meta.get('tracked_first_trade_utc') or 'N/A'} ~ {run_meta.get('tracked_last_trade_utc') or 'N/A'}",
        f"模型：{report_obj.get('model') or 'fallback'}（status={report_obj.get('status')}）",
        "",
        "## 摘要",
        "",
        (
            f"本次冠军策略为“{best.get('strategy') or 'N/A'}”，"
            f"ROI 为 {_fmt_pct(best.get('roi'))}，投入 {_fmt_money(best.get('total_buy_cost'))} USDC，"
            f"总收益 {_fmt_money(best.get('total_pnl'))} USDC。"
            f"报告以下述主线展开：先界定目标与执行过程，再给出差距归因与改进结论，最后附上可复核证据。"
        ),
        "",
        "## 研究目标",
        "",
        _as_text(analysis.get("objective")),
        "",
        "## 方法与执行过程",
        "",
        _as_text(analysis.get("what_was_executed")),
        "",
        "## 关键发现",
        "",
        _as_text(analysis.get("key_results")),
        "",
        "## 与 Leader 的差距解释",
        "",
        _as_text(analysis.get("gap_to_leader")),
        "",
        "## 根因归纳",
        "",
        _as_text(analysis.get("root_causes")),
        "",
        "## 策略改进与结论",
        "",
        _as_text(analysis.get("final_winner_reason")),
        "",
        _as_text(analysis.get("final_actionable_next_step")),
        "",
        "## 附录A：关键数值锚点",
        "",
        f"- tracked-window 实际收益增量：{_fmt_money(run_meta.get('actual_window_pnl_delta'))} USDC",
        f"- 领单 BUY 总流量：{_fmt_money(run_meta.get('leader_buy_signal_total_usd'))} USDC",
        f"- 冠军 ROI：{_fmt_pct(best.get('roi'))}",
        f"- 冠军总投入：{_fmt_money(best.get('total_buy_cost'))} USDC",
        f"- 冠军总收益：{_fmt_money(best.get('total_pnl'))} USDC",
        "",
        "## 附录B：已执行实验摘要",
        "",
    ]

    exp_rows: List[List[str]] = []
    for row in executed[:10]:
        if not isinstance(row, dict):
            continue
        delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
        exp_rows.append(
            [
                str(row.get("round") or "N/A"),
                "是" if bool(row.get("improved")) else "否",
                _fmt_pct(delta.get("roi")),
                _fmt_money(delta.get("total_buy_cost")),
                _fmt_money(delta.get("total_pnl")),
            ]
        )
    lines.extend(_markdown_table(["轮次", "是否提升", "ΔROI", "Δ投入(USDC)", "Δ收益(USDC)"], exp_rows))
    lines.extend(["", "## 附录C：Entries 市场级证据（Top10）", ""])

    market_rows: List[List[str]] = []
    for row in top_markets[:10]:
        if not isinstance(row, dict):
            continue
        market_rows.append(
            [
                str(row.get("market_key") or "N/A"),
                str(_as_int(row.get("leader_buy_signals")) or 0),
                str(_as_int(row.get("copied_buys_current")) or 0),
                str(_as_int(row.get("copied_buys_baseline")) or 0),
                _fmt_money(row.get("delta_pnl")),
            ]
        )
    lines.extend(_markdown_table(["市场", "Leader买入信号", "当前跟单次数", "对照跟单次数", "Δ收益(USDC)"], market_rows))
    lines.extend(
        [
            "",
            f"分位数参考：p50={_fmt_ratio(entries_dist.get('p50'))}，p75={_fmt_ratio(entries_dist.get('p75'))}，"
            f"p90={_fmt_ratio(entries_dist.get('p90'))}，p95={_fmt_ratio(entries_dist.get('p95'))}，"
            f"max={_fmt_ratio(entries_dist.get('max'))}",
            "",
            "## 附录D：防放大约束证据",
            "",
            f"- 约束开关：{'开启' if bool(amp_findings.get('enabled')) else '关闭'}",
            f"- 单笔上限倍数：{_fmt_ratio(amp_findings.get('per_trade_limit'))}",
            f"- 单市场上限倍数：{_fmt_ratio(amp_findings.get('per_market_limit'))}",
            f"- 裁剪次数：{_as_int(amp_agg.get('trimmed_count')) or 0}",
            f"- 裁剪金额：{_fmt_money(amp_agg.get('trimmed_usd'))} USDC",
            f"- 超限率（前→后）：{_fmt_pct(amp_agg.get('oversize_event_rate_before_guard'))} → {_fmt_pct(amp_agg.get('oversize_event_rate_after_guard'))}",
            "",
            "## 附录E：说明",
            "",
            _as_text(analysis.get("caveats")),
            "",
        ]
    )
    lines.extend(_render_window_analysis_markdown(window_analysis))
    return "\n".join(lines).strip() + "\n"


def generate_ai_reports(
    *,
    sim_json_path: Union[str, Path],
    gap_json_path: Optional[Union[str, Path]] = None,
    out_md: Optional[Union[str, Path]] = None,
    out_json: Optional[Union[str, Path]] = None,
    language: str = "zh-CN",
) -> Dict[str, Any]:
    sim_path = Path(sim_json_path)
    if not sim_path.exists():
        raise RuntimeError(f"sim json not found: {sim_path}")
    gap_path = Path(gap_json_path) if gap_json_path else None
    if gap_path is not None and not gap_path.exists():
        raise RuntimeError(f"gap json not found: {gap_path}")

    md_arg = Path(out_md).expanduser() if out_md else None
    json_arg = Path(out_json).expanduser() if out_json else None
    md_path, json_path = _default_output_paths(sim_path, md_arg, json_arg)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    sim_payload = _load_json(sim_path)
    gap_payload = _load_json(gap_path) if gap_path is not None else None
    analysis_payload = _build_analysis_payload(sim_path, sim_payload, gap_payload, gap_path)

    status = "success"
    provider = "openai"
    model = ""
    request_id: Optional[str] = None
    usage: Dict[str, int] = {}
    error_message: Optional[str] = None
    try:
        try:
            from sim_copytrade.openai_client import OpenAIAnalysisClient  # type: ignore
        except Exception:
            from openai_client import OpenAIAnalysisClient  # type: ignore
        client = OpenAIAnalysisClient()
        result = client.generate_copytrade_analysis(
            analysis_payload=analysis_payload,
            language=language,
            required_keys=REQUIRED_ANALYSIS_KEYS,
        )
        analysis = _ensure_required_fields(result.parsed)
        model = result.model
        request_id = result.request_id
        usage = result.usage
    except Exception as exc:  # noqa: BLE001
        status = "fallback"
        provider = "fallback"
        error_message = str(exc)
        analysis = _fallback_analysis(analysis_payload, error_message)

    report_obj: Dict[str, Any] = {
        "generated_at": _now_utc_iso(),
        "status": status,
        "provider": provider,
        "model": model,
        "request_id": request_id,
        "usage": usage,
        "input": {
            "sim_json": str(sim_path),
            "gap_json": str(gap_path) if gap_path is not None else None,
            "language": language,
        },
        "analysis_payload": analysis_payload,
        "analysis": analysis,
    }
    if error_message:
        report_obj["error"] = error_message

    json_path.write_text(json.dumps(report_obj, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report_obj), encoding="utf-8")
    return {
        "status": status,
        "provider": provider,
        "model": model or "fallback",
        "md_path": str(md_path),
        "json_path": str(json_path),
        "error": error_message,
    }


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Generate execution-oriented AI report for sim_copytrade")
    ap.add_argument("--sim-json", type=str, required=True, help="Path to sim_results_*.json")
    ap.add_argument("--gap-json", type=str, default="", help="Optional gap_analysis_*.json")
    ap.add_argument("--out-md", type=str, default="", help="Optional output markdown path")
    ap.add_argument("--out-json", type=str, default="", help="Optional output json path")
    ap.add_argument("--language", type=str, default="zh-CN", help="Language hint")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    sim_path = Path(args.sim_json)
    if not sim_path.exists():
        raise SystemExit(f"sim json not found: {sim_path}")
    gap_path = Path(args.gap_json) if str(args.gap_json or "").strip() else None
    if gap_path is not None and not gap_path.exists():
        raise SystemExit(f"gap json not found: {gap_path}")
    out_md = Path(args.out_md).expanduser() if str(args.out_md or "").strip() else None
    out_json = Path(args.out_json).expanduser() if str(args.out_json or "").strip() else None
    out = generate_ai_reports(
        sim_json_path=sim_path,
        gap_json_path=gap_path,
        out_md=out_md,
        out_json=out_json,
        language=str(args.language or "zh-CN"),
    )
    print(f"status={out.get('status')}")
    print(f"provider={out.get('provider')}")
    print(f"model={out.get('model')}")
    print(f"md={out.get('md_path')}")
    print(f"json={out.get('json_path')}")
    if out.get("error"):
        print(f"error={out.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
