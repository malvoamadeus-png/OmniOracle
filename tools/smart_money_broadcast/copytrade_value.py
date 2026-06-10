from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from http_client import to_float


THRESHOLDS_PATH = Path(__file__).resolve().parent / "config" / "copytrade_value_thresholds.json"
COPYTRADE_VALUE_SCORE_VERSION = "copytrade_value_v1"
COPYTRADE_VALUE_METRICS: List[Tuple[str, str]] = [
    ("sharpe", "high"),
    ("realized_edge_score", "high"),
    ("roi", "high"),
    ("profit_factor", "high"),
    ("max_drawdown", "low"),
    ("ulcer_index", "low"),
]
MIN_AVAILABLE_SCORE_METRICS = 3


def load_threshold_config(path: Path = THRESHOLDS_PATH) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("score_version") or "") != COPYTRADE_VALUE_SCORE_VERSION:
        return None
    high = to_float(payload.get("high_threshold"))
    medium = to_float(payload.get("medium_threshold"))
    if high is None or medium is None:
        return None
    samples_by_metric = payload.get("metric_samples")
    if not isinstance(samples_by_metric, dict):
        return None
    metrics_with_samples = 0
    for metric, _direction in COPYTRADE_VALUE_METRICS:
        samples = samples_by_metric.get(metric)
        if not isinstance(samples, list):
            continue
        finite = [to_float(item) for item in samples]
        if any(item is not None for item in finite):
            metrics_with_samples += 1
    if metrics_with_samples < MIN_AVAILABLE_SCORE_METRICS:
        return None
    return payload


def threshold_status(path: Path = THRESHOLDS_PATH) -> str:
    if not path.exists():
        return f"阈值文件不存在: {path}"
    payload = load_threshold_config(path)
    if payload is None:
        return f"阈值文件不可用或版本不匹配: {path}"
    return ""


def _rank_score(samples: List[float], value: float, direction: str) -> Optional[float]:
    finite = [float(v) for v in samples if math.isfinite(float(v))]
    if not finite or not math.isfinite(float(value)):
        return None
    if direction == "high":
        better_or_equal = sum(1 for item in finite if item <= float(value))
    else:
        better_or_equal = sum(1 for item in finite if item >= float(value))
    return max(0.0, min(100.0, better_or_equal / len(finite) * 100.0))


def exclusion_reason(metrics: Dict[str, Any]) -> Optional[str]:
    total_trades = to_float(metrics.get("total_trades"))
    max_drawdown = to_float(metrics.get("max_drawdown"))
    avg_trade_price = to_float(metrics.get("avg_trade_price"))
    current_value = to_float(metrics.get("current_position_value_usd"))

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


def apply_copytrade_value(metrics: Dict[str, Any], config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if config is None:
        return {
            "copytrade_value_score": None,
            "copytrade_value_level": None,
            "copytrade_value_exclusion_reason": "threshold_config_missing",
            "copytrade_value_score_version": None,
        }
    reason = exclusion_reason(metrics)
    if reason is not None:
        return {
            "copytrade_value_score": None,
            "copytrade_value_level": "not_worth_copying",
            "copytrade_value_exclusion_reason": reason,
            "copytrade_value_score_version": COPYTRADE_VALUE_SCORE_VERSION,
        }

    samples_by_metric = config.get("metric_samples")
    if not isinstance(samples_by_metric, dict):
        samples_by_metric = {}

    scores: List[float] = []
    for metric, direction in COPYTRADE_VALUE_METRICS:
        value = to_float(metrics.get(metric))
        samples_raw = samples_by_metric.get(metric)
        if value is None or not isinstance(samples_raw, list):
            continue
        samples = [to_float(item) for item in samples_raw]
        score = _rank_score([item for item in samples if item is not None], value, direction)
        if score is not None:
            scores.append(score)

    if len(scores) < MIN_AVAILABLE_SCORE_METRICS:
        return {
            "copytrade_value_score": None,
            "copytrade_value_level": "not_worth_copying",
            "copytrade_value_exclusion_reason": "insufficient_score_metrics",
            "copytrade_value_score_version": COPYTRADE_VALUE_SCORE_VERSION,
        }

    total_score = sum(scores) / len(scores)
    high = to_float(config.get("high_threshold"))
    medium = to_float(config.get("medium_threshold"))
    if high is not None and total_score >= high:
        level = "high"
    elif medium is not None and total_score >= medium:
        level = "medium"
    else:
        level = "low"
    return {
        "copytrade_value_score": total_score,
        "copytrade_value_level": level,
        "copytrade_value_exclusion_reason": None,
        "copytrade_value_score_version": COPYTRADE_VALUE_SCORE_VERSION,
    }


def compute_resilience_scores(cohort: List[Dict[str, Any]]) -> Dict[str, float]:
    mdd_values = [to_float(row.get("max_drawdown")) for row in cohort]
    ui_values = [to_float(row.get("ulcer_index")) for row in cohort]
    mdd_samples = [v for v in mdd_values if v is not None]
    ui_samples = [v for v in ui_values if v is not None]
    out: Dict[str, float] = {}
    for row in cohort:
        address = str(row.get("address") or "").lower()
        mdd = to_float(row.get("max_drawdown"))
        ui = to_float(row.get("ulcer_index"))
        if not address or mdd is None or ui is None:
            continue
        mdd_score = _rank_score(mdd_samples, mdd, "low")
        ui_score = _rank_score(ui_samples, ui, "low")
        if mdd_score is None or ui_score is None or (mdd_score + ui_score) <= 0:
            continue
        out[address] = 2.0 * mdd_score * ui_score / (mdd_score + ui_score)
    return out
