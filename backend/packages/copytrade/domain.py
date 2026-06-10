"""Core copytrade domain types and policy constants.

This module is intentionally small and dependency-free. It gives the refactor a
single place for names that must mean the same thing across signal collection,
decisioning, execution, accounting, and admin/status surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


COPY_MODE_FIXED_USD = "fixed_usd"
COPY_MODE_PROPORTIONAL = "proportional"
ALLOWED_COPY_MODES = {COPY_MODE_FIXED_USD, COPY_MODE_PROPORTIONAL}

PRICING_MODE_AGGRESSIVE = "aggressive"
PRICING_MODE_ORIGINAL = "original"
PRICING_MODE_PASSIVE = "passive"
ALLOWED_PRICING_MODES = {
    PRICING_MODE_AGGRESSIVE,
    PRICING_MODE_ORIGINAL,
    PRICING_MODE_PASSIVE,
}

EXIT_STRATEGY_MIRROR_SELL = "mirror_sell"
EXIT_STRATEGY_HOLD_TO_RESOLUTION = "hold_to_resolution"
ALLOWED_EXIT_STRATEGIES = {
    EXIT_STRATEGY_MIRROR_SELL,
    EXIT_STRATEGY_HOLD_TO_RESOLUTION,
}

ORDER_STATUS_SUBMITTED = "submitted"
ORDER_STATUS_FILLED = "filled"
ORDER_STATUS_PARTIALLY_FILLED = "partially_filled"
ORDER_STATUS_EXPIRED = "expired"
ORDER_STATUS_FAILED = "failed"
FILLED_ORDER_STATUSES = (ORDER_STATUS_FILLED, ORDER_STATUS_PARTIALLY_FILLED)


def classify_order_fill_status(
    exchange_order_status: Any,
    matched_size: Any = None,
    requested_size: Any = None,
    *,
    unfilled_terminal_status: str = ORDER_STATUS_EXPIRED,
) -> tuple[str, Optional[str]]:
    """Map exchange state + cumulative matched size to our canonical order status."""
    status_key = str(exchange_order_status or "").strip().lower()
    try:
        matched = max(0.0, float(matched_size or 0.0))
    except (TypeError, ValueError):
        matched = 0.0
    try:
        requested = max(0.0, float(requested_size or 0.0))
    except (TypeError, ValueError):
        requested = 0.0

    if matched > 0:
        if status_key == "matched":
            return ORDER_STATUS_FILLED, "full"
        is_full = requested <= 0 or matched + 1e-9 >= requested
        return (
            ORDER_STATUS_FILLED if is_full else ORDER_STATUS_PARTIALLY_FILLED,
            "full" if is_full else "partial",
        )

    if status_key in ("expired", "cancelled"):
        status = status_key if unfilled_terminal_status == "exchange" else unfilled_terminal_status
        return status, "unfilled"

    return ORDER_STATUS_SUBMITTED, None

SIGNAL_PENDING_MAX_AGE_S = 5 * 60
STREAM_HYBRID_BACKFILL_LOOKBACK_S = 15 * 60
SIGNAL_SOURCE_PRIORITY = {
    "subgraph": 30,
    "stream": 20,
    "activity": 10,
}

DEFAULT_CLOB_MIN_ORDER_SIZE = 5.0

DEPRECATED_CONFIG_FIELDS = {
    "delayed_follow_enabled",
    "delayed_follow_agg_threshold",
    "delayed_follow_agg_window_minutes",
    "delayed_follow_skip_n",
    "delayed_follow_copy_mode",
    "delayed_follow_fixed_usd",
    "delayed_follow_proportional_pct",
    "delayed_follow_proportional_cap",
    "delayed_follow_max_follows",
    "additional_usd_amount",
    "additional_proportional_cap",
    "fixed_shares",
    "fixed_shares_count",
    "super_follow_enabled",
    "super_follow_market_budget_usd",
    "super_follow_price_chase_enabled",
    "super_follow_price_chase_cap_abs",
    "super_follow_price_chase_cap_bps",
    "super_follow_price_chase_taper_start_price",
    "super_follow_price_chase_disable_above_price",
    "take_profit_pct",
}

ACCOUNT_LEVEL_ONLY_CONFIG_FIELDS = {
    "auto_tp_enabled",
}


@dataclass(frozen=True)
class LeaderSignal:
    account_name: str
    leader_address: str
    side: str
    token_id: str
    condition_id: str
    leader_fill_key: str
    tx_hash: str = ""
    price: Optional[float] = None
    size: Optional[float] = None
    usd: Optional[float] = None
    outcome: Optional[str] = None
    market_slug: Optional[str] = None
    timestamp: Optional[str] = None
    source: str = "activity"
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AggregatedSignal:
    signal: LeaderSignal
    aggregated_leader_usd: float
    source_count: int
    aggregation_kind: str
    score: Optional[float] = None


@dataclass(frozen=True)
class CopyDecision:
    should_submit: bool
    reason: str
    target_usd: float = 0.0
    target_size: float = 0.0


@dataclass(frozen=True)
class OrderPlan:
    purpose: str
    side: str
    token_id: str
    price: float
    size: float
    usd: float
    tif: str
    expiration_ts: Optional[int] = None


@dataclass(frozen=True)
class OrderFillEvent:
    account_name: str
    order_id: str
    cumulative_size: float
    cumulative_usd: float
    source: str
    filled_price: Optional[float] = None
    exchange_order_status: str = ""


@dataclass(frozen=True)
class PositionLot:
    account_name: str
    trade_id: int
    leader_address: str
    token_id: str
    entry_price: float
    original_size: float
    remaining_size: float


@dataclass(frozen=True)
class RuntimeEvent:
    event_type: str
    severity: str
    component: str
    account_name: Optional[str] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
