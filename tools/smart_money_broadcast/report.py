from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from copytrade_value import compute_resilience_scores
from http_client import normalize_address

OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def fmt_number(value: Any, digits: int = 4) -> str:
    if not isinstance(value, (int, float)):
        return "暂无数据"
    return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")


def fmt_wan_usd(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "暂无数据"
    return fmt_number(float(value) / 10000.0, 2)


def fmt_rate(value: Any, digits: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "暂无数据"
    return f"{float(value) * 100:.{digits}f}".rstrip("0").rstrip(".") + "%"


def percentile_rank(metrics: Dict[str, Any], cohort: List[Dict[str, Any]], field: str, *, lower_is_better: bool) -> Optional[float]:
    value = metrics.get(field)
    if not isinstance(value, (int, float)):
        return None
    values = [row.get(field) for row in cohort if isinstance(row.get(field), (int, float))]
    if len(values) < 2:
        return None
    if lower_is_better:
        better_or_equal = sum(1 for item in values if float(item) <= float(value))
    else:
        better_or_equal = sum(1 for item in values if float(item) >= float(value))
    return better_or_equal / len(values) * 100.0


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "暂无排行"
    return fmt_number(value, 1)


def fmt_copytrade_value(metrics: Dict[str, Any]) -> str:
    level = metrics.get("copytrade_value_level")
    score = metrics.get("copytrade_value_score")
    reason = metrics.get("copytrade_value_exclusion_reason")
    if level == "not_worth_copying":
        suffix = f"（{reason}）" if isinstance(reason, str) and reason else ""
        return f"不值得跟单{suffix}"
    if not isinstance(level, str) or not level:
        return "暂无数据"
    return f"{level}（{fmt_number(score, 1)}分）"


def resilience_rank_pct(address: str, metrics: Dict[str, Any], cohort: List[Dict[str, Any]]) -> Optional[float]:
    addr = normalize_address(address)
    rows = [dict(row) for row in cohort]
    if not any(normalize_address(row.get("address")) == addr for row in rows):
        row = dict(metrics)
        row["address"] = addr
        rows.append(row)
    scores = compute_resilience_scores(rows)
    value = scores.get(addr)
    if not isinstance(value, (int, float)):
        return None
    score_rows = [{"resilience_score": score} for score in scores.values()]
    return percentile_rank({"resilience_score": value}, score_rows, "resilience_score", lower_is_better=False)


def render_report(address: str, metrics: Dict[str, Any], cohort: List[Dict[str, Any]], board: str = "NBA") -> str:
    accuracy_pct = percentile_rank(metrics, cohort, "realized_edge_score", lower_is_better=False)
    profit_pct = percentile_rank(metrics, cohort, "total_pnl", lower_is_better=False)
    drawdown_pct = resilience_rank_pct(address, metrics, cohort)
    board_label = str(board or "NBA").upper()
    return (
        f"# {board_label} 聪明钱播报 - {normalize_address(address)}\n\n"
        f"过去30日盈利{fmt_wan_usd(metrics.get('pnl_30d'))}万美元，总盈利{fmt_wan_usd(metrics.get('total_pnl'))}万美元\n\n"
        f"精准预测能力在{board_label}板块排行前{fmt_pct(accuracy_pct)}%\n\n"
        f"盈利能力在{board_label}板块排行前{fmt_pct(profit_pct)}%\n\n"
        f"抗回撤能力在{board_label}板块排行前{fmt_pct(drawdown_pct)}%\n\n"
        f"Realized Edge分数{fmt_number(metrics.get('realized_edge_score'))}，"
        f"投注回报率{fmt_number(metrics.get('roi'))}，"
        f"夏普比{fmt_number(metrics.get('sharpe'))}，"
        f"最大回撤{fmt_number(metrics.get('max_drawdown'))}，"
        f"溃疡指标{fmt_number(metrics.get('ulcer_index'))}\n\n"
        f"跟单价值：{fmt_copytrade_value(metrics)}\n\n"
        f"地址总盈亏{fmt_wan_usd(metrics.get('total_pnl'))}万美元，胜率{fmt_rate(metrics.get('win_rate'))}\n"
    )


def write_report(address: str, metrics: Dict[str, Any], cohort: List[Dict[str, Any]], board: str = "NBA", output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = normalize_address(address)
    if len(short) > 14:
        short = f"{short[:8]}_{short[-6:]}"
    board_label = str(board or "NBA").upper().replace(" ", "_")
    path = output_dir / f"{ts}_{board_label}_{short}.md"
    path.write_text(render_report(address, metrics, cohort, board), encoding="utf-8")
    return path
