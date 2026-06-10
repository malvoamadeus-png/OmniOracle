"""离场策略 — 镜像平仓 / 持有至结算 / 获利了结."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from copytrade.polymarket_public_api import DATA_API, extract_position_fields, http_get_json

from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.domain import OrderFillEvent
from copytrade.executor import OrderExecutor, OrderParams, OrderResult
from copytrade.monitor import LeaderTrade
from copytrade.user_order_hub import UserOrderEvent

EPS = 1e-9
ORDER_SYNC_TRANSPORT_COOLDOWN_S = 30.0
AUTO_TP_ACTIVE_LOT_STATUSES = {"open", "min_size_pending"}
PAUSED_TRADE_SKIP_PREFIXES = (
    "pending_clob_balance:",
    "pending_settlement:",
    "min_size_pending:",
)


@dataclass
class ExitAction:
    trade_id: int
    token_id: str
    side: str
    size: float
    price: float
    reason: str
    result: Optional[OrderResult] = None


@dataclass
class _TPPlan:
    target_price: float
    sell_ratio: float


class ExitManager:
    def __init__(
        self,
        session: requests.Session,
        config: CopyTradeConfig,
        db: CopyTradeDB,
        executor: OrderExecutor,
        account_name: str = "default",
        on_condition_activated: Optional[Callable[[str], None]] = None,
    ):
        self.session = session
        self.config = config
        self.db = db
        self.executor = executor
        self.account_name = account_name
        self._leader_position_cache = {}
        self._on_condition_activated = on_condition_activated
        self._sync_error_log_ts: Dict[Tuple[str, str], float] = {}
        self._ws_fill_progress: Dict[str, Dict[str, float]] = {}
        self._order_sync_rest_pause_until = 0.0

    def process_exits(self, new_leader_trades: List[LeaderTrade]) -> List[ExitAction]:
        self._leader_position_cache = {}
        self._verify_recent_exit_orders()
        grouped_sells = self._group_leader_sells(new_leader_trades)
        if self._auto_tp_enabled():
            self._verify_recent_auto_tp_orders()
            if grouped_sells:
                self._sync_auto_tp_before_leader_sells(grouped_sells)
        strategy = self.config.exit_strategy
        if strategy == "mirror_sell":
            actions = self._mirror_sell(grouped_sells)
        elif strategy == "hold_to_resolution":
            actions = []
        else:
            actions = []
        if self._auto_tp_enabled():
            self._refresh_auto_tp_orders()
        return actions

    def _auto_tp_enabled(self) -> bool:
        return bool(getattr(self.config, "auto_tp_enabled", False))

    def _group_leader_sells(self, new_leader_trades: List[LeaderTrade]) -> Dict[Tuple[str, str], Dict[str, Any]]:
        grouped_sells: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for trade in [t for t in new_leader_trades if t.side == "SELL"]:
            key = ((trade.leader_address or "").lower(), trade.token_id)
            payload = grouped_sells.setdefault(
                key,
                {"trade": trade, "sell_size": 0.0, "notional": 0.0, "leader_remaining": None},
            )
            if isinstance(trade.size, (int, float)) and trade.size > 0:
                sell_size = float(trade.size)
                payload["sell_size"] += sell_size
                payload["notional"] += sell_size * float(trade.price or 0.0)
                payload["trade"] = trade
        return grouped_sells

    def _sync_auto_tp_before_leader_sells(
        self,
        grouped_sells: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        for (leader_address, token_id), payload in grouped_sells.items():
            leader_remaining = self._get_leader_remaining_size(leader_address, token_id)
            payload["leader_remaining"] = leader_remaining
            leader_closed = leader_remaining is not None and leader_remaining <= EPS

            for order in self.db.get_open_auto_tp_orders_for_group(
                account_name=self.account_name,
                leader_address=leader_address,
                token_id=token_id,
            ):
                order_id = str(order.get("order_id") or "").strip()
                if not order_id:
                    continue
                if self.executor.cancel_order(order_id):
                    self.db.update_auto_tp_order_status(
                        order_id,
                        account_name=self.account_name,
                        status="submitted",
                        exchange_order_status="cancel_requested",
                    )

            for lot in self.db.get_auto_tp_lots_for_group(
                account_name=self.account_name,
                leader_address=leader_address,
                token_id=token_id,
                include_closed=False,
            ):
                self.db.update_auto_tp_lot(
                    int(lot["id"]),
                    pending_rebuy_size=0.0,
                    status=("leader_closed" if leader_closed else "syncing"),
                )

    def _refresh_auto_tp_orders(self) -> None:
        rows = self.db.conn.execute(
            "SELECT id FROM ct_auto_tp_lots WHERE account_name=? AND status='open' ORDER BY id ASC",
            (self.account_name,),
        ).fetchall()
        for row in rows:
            lot = self.db.get_auto_tp_lot(int(row["id"]), account_name=self.account_name)
            if lot:
                self._maybe_place_orders_for_lot(lot)

    def _maybe_place_orders_for_lot(self, lot: Dict[str, Any]) -> None:
        if str(lot.get("status") or "") != "open":
            return
        if self.db.has_open_exit_order(int(lot["root_trade_id"]), account_name=self.account_name):
            return

        remaining_size = float(lot.get("remaining_size") or 0.0)
        pending_rebuy = float(lot.get("pending_rebuy_size") or 0.0)
        if remaining_size <= EPS and pending_rebuy <= EPS:
            self.db.update_auto_tp_lot(int(lot["id"]), status="closed")
            return

        if pending_rebuy > EPS and not self.db.has_open_auto_tp_order(
            int(lot["id"]),
            account_name=self.account_name,
            kind="rebuy_buy",
        ):
            self._submit_auto_tp_order(lot, kind="rebuy_buy")

        remaining_tp = max(
            0.0,
            min(
                remaining_size,
                float(lot.get("tp_target_size") or 0.0) - float(lot.get("tp_filled_size") or 0.0),
            ),
        )
        if remaining_tp > EPS and not self.db.has_open_auto_tp_order(
            int(lot["id"]),
            account_name=self.account_name,
            kind="tp_sell",
        ):
            self._submit_auto_tp_order(lot, kind="tp_sell")

    def _submit_auto_tp_order(self, lot: Dict[str, Any], *, kind: str) -> None:
        if kind == "tp_sell":
            plan = self._build_tp_plan(float(lot.get("entry_price") or 0.0))
            if plan is None:
                return
            size = max(
                0.0,
                min(
                    float(lot.get("remaining_size") or 0.0),
                    float(lot.get("tp_target_size") or 0.0) - float(lot.get("tp_filled_size") or 0.0),
                ),
            )
            side = "SELL"
            price = plan.target_price
            purpose = "auto_tp"
        else:
            size = max(0.0, float(lot.get("pending_rebuy_size") or 0.0))
            side = "BUY"
            price = float(lot.get("entry_price") or 0.0)
            purpose = "auto_rebuy"
        if size <= EPS or price <= 0:
            return

        params = OrderParams(
            token_id=str(lot.get("token_id") or ""),
            side=side,
            price=price,
            size=size,
            usd=size * price,
            condition_id=str(lot.get("condition_id") or ""),
            market_slug=lot.get("market_slug"),
            outcome=lot.get("outcome"),
            order_purpose=purpose,
        )
        result = self.executor.execute_order(params)
        if not result.success:
            self._log_order_failure(params, result)
            if self._is_sell_balance_unavailable_result(params, result):
                self._mark_auto_tp_lots_balance_unavailable(
                    lot_ids=[int(lot["id"])],
                    token_id=str(lot.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            if self._is_min_order_size_result(result):
                self._mark_auto_tp_lots_min_size_pending(
                    lot_ids=[int(lot["id"])],
                    token_id=str(lot.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            if self._is_orderbook_unavailable_result(result):
                self._mark_auto_tp_lots_orderbook_unavailable(
                    lot_ids=[int(lot["id"])],
                    token_id=str(lot.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            return

        immediate_fill = self._is_immediate_fill(result)
        order_id = str(getattr(result, "order_id", "") or "").strip()
        exchange_status = str(getattr(result, "exchange_status", "") or ("matched" if immediate_fill else "submitted"))
        self.db.insert_auto_tp_order(
            {
                "lot_id": int(lot["id"]),
                "root_trade_id": int(lot["root_trade_id"]),
                "account_name": self.account_name,
                "kind": kind,
                "order_id": order_id or None,
                "side": side,
                "requested_price": price,
                "requested_size": size,
                "requested_usd": size * price,
                "status": ("filled" if immediate_fill else "submitted"),
                "exchange_order_status": exchange_status,
                "filled_size_actual": (result.filled_size if immediate_fill else None),
                "filled_usd_actual": (result.filled_usd if immediate_fill else None),
                "filled_price_actual": (result.filled_price if immediate_fill else None),
            }
        )
        self._notify_condition_activated(lot.get("condition_id"))
        if immediate_fill:
            self._handle_auto_tp_fill(
                lot_id=int(lot["id"]),
                root_trade_id=int(lot["root_trade_id"]),
                kind=kind,
                delta_size=float(result.filled_size or 0.0),
                delta_usd=float(result.filled_usd or 0.0),
                actual_price=self._resolve_fill_price(
                    result.filled_price,
                    result.filled_usd,
                    float(result.filled_size or 0.0),
                    fallback=price,
                ),
            )

    def register_entry_fill(
        self,
        trade_id: int,
        *,
        filled_size: float,
        filled_usd: Optional[float],
        fill_price: Optional[float],
    ) -> None:
        if not self._auto_tp_enabled():
            return
        size_value = max(0.0, float(filled_size or 0.0))
        if size_value <= EPS:
            return

        trade = self._get_trade(trade_id)
        if not trade:
            return

        entry_price = self._resolve_fill_price(
            fill_price,
            filled_usd,
            size_value,
            fallback=trade.get("our_price"),
        )
        if entry_price is None or entry_price <= 0:
            return

        plan = self._build_tp_plan(entry_price)
        if plan is None:
            return

        lot_id = self.db.insert_auto_tp_lot(
            {
                "account_name": self.account_name,
                "root_trade_id": int(trade_id),
                "parent_lot_id": None,
                "leader_address": str(trade.get("leader_address") or "").lower(),
                "token_id": trade.get("token_id") or "",
                "condition_id": trade.get("condition_id"),
                "market_slug": trade.get("market_slug"),
                "outcome": trade.get("outcome"),
                "entry_price": entry_price,
                "original_size": size_value,
                "remaining_size": size_value,
                "tp_target_size": size_value * plan.sell_ratio,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )
        lot = self.db.get_auto_tp_lot(lot_id, account_name=self.account_name)
        if lot:
            self._maybe_place_orders_for_lot(lot)

    def _mirror_sell(self, grouped_sells: Dict[Tuple[str, str], Dict[str, Any]]) -> List[ExitAction]:
        """检查 leader 的 SELL 操作，按 leader 卖出比例对其来源仓位做部分平仓."""
        actions: List[ExitAction] = []

        for payload in grouped_sells.values():
            lt = payload["trade"]
            all_positions = self.db.get_open_trades(token_id=lt.token_id, account_name=self.account_name)
            our_positions = [
                p for p in all_positions
                if (p.get("leader_address") or "").lower() == (lt.leader_address or "").lower()
                and not self._is_trade_operationally_paused(p)
            ]
            if not our_positions:
                continue

            leader_sell_size = float(payload["sell_size"]) if float(payload["sell_size"]) > 0 else None
            if leader_sell_size is None:
                continue

            leader_remaining = payload.get("leader_remaining")
            if leader_remaining is None:
                leader_remaining = self._get_leader_remaining_size(lt.leader_address, lt.token_id)
                payload["leader_remaining"] = leader_remaining
            if leader_remaining is None:
                # 无法获取 leader 当前持仓时，跳过本次镜像卖出，避免错误全平
                continue

            # 估算 leader 卖出比例：sold / (remaining + sold)
            denom = leader_remaining + leader_sell_size
            if denom <= 0:
                continue
            sell_ratio = max(0.0, min(1.0, leader_sell_size / denom))
            if sell_ratio <= 0:
                continue
            leader_closed = leader_remaining <= EPS

            for pos in our_positions:
                if self.db.has_open_exit_order(int(pos["id"]), account_name=self.account_name):
                    continue
                our_size = self._available_size_for_trade(pos)
                if not our_size or our_size <= 0:
                    continue

                # 仅卖掉该 leader 带来的仓位比例
                sell_size = our_size * sell_ratio
                if sell_size <= 0:
                    continue
                if sell_size > our_size:
                    sell_size = our_size

                sell_price = (
                    float(payload["notional"]) / leader_sell_size
                    if leader_sell_size > 0 and float(payload["notional"]) > 0
                    else (lt.price or 0.5)
                )
                params = OrderParams(
                    token_id=lt.token_id,
                    side="SELL",
                    price=sell_price,
                    size=sell_size,
                    usd=sell_size * sell_price,
                    condition_id=pos.get("condition_id", ""),
                    market_slug=pos.get("market_slug"),
                    outcome=pos.get("outcome"),
                    order_purpose="mirror_sell",
                )

                result = self.executor.execute_order(params)
                immediate_fill = self._is_immediate_fill(result)

                action = ExitAction(
                    trade_id=pos["id"],
                    token_id=lt.token_id,
                    side="SELL",
                    size=sell_size,
                    price=sell_price,
                    reason="mirror_sell",
                    result=result,
                )
                actions.append(action)

                if result.success and immediate_fill:
                    fill_price = self._resolve_fill_price(
                        result.filled_price,
                        result.filled_usd,
                        sell_size,
                        fallback=sell_price,
                    )
                    sold_usd = float(result.filled_usd or 0.0)
                    if sold_usd <= 0 and fill_price is not None:
                        sold_usd = sell_size * fill_price
                    cost_basis = self._consume_lot_cost_for_mirror_sell(
                        int(pos["id"]),
                        sell_size,
                        leader_closed=leader_closed,
                    )
                    if cost_basis is None:
                        cost_basis = self._average_cost_for_trade(pos) * sell_size
                    profit = sold_usd - cost_basis
                    if self.config.fee_rate > 0 and sold_usd > 0:
                        profit -= self.config.fee_rate * sold_usd
                    self.db.apply_exit_fill(
                        trade_id=pos["id"],
                        sold_size=sell_size,
                        exit_price=fill_price,
                        sold_usd=sold_usd,
                        profit_delta=profit,
                        close_position=False,
                        cost_basis_usd=cost_basis,
                    )
                elif result.success:
                    self._record_submitted_exit_order(
                        pos=pos,
                        action=action,
                        result=result,
                    )
                else:
                    self._log_order_failure(params, result)
                    if self._is_sell_balance_unavailable_result(params, result):
                        self._mark_trade_sell_balance_unavailable(pos, params, result)
                    if self._is_min_order_size_result(result):
                        self._mark_trade_min_size_pending(pos, params, result)

        return actions

        """遍历所有 open 仓位，检查是否达到获利了结条件."""
    def _handle_auto_tp_fill(
        self,
        *,
        lot_id: int,
        root_trade_id: int,
        kind: str,
        delta_size: float,
        delta_usd: float,
        actual_price: Optional[float],
    ) -> None:
        lot = self.db.get_auto_tp_lot(lot_id, account_name=self.account_name)
        if not lot:
            return

        if kind == "tp_sell":
            cost_basis = float(lot.get("entry_price") or 0.0) * delta_size
            profit = delta_usd - cost_basis
            if self.config.fee_rate > 0 and delta_usd > 0:
                profit -= self.config.fee_rate * delta_usd
            self.db.apply_exit_fill(
                trade_id=root_trade_id,
                sold_size=delta_size,
                exit_price=actual_price,
                sold_usd=delta_usd,
                profit_delta=profit,
                close_position=False,
                cost_basis_usd=cost_basis,
            )

            remaining = max(0.0, float(lot.get("remaining_size") or 0.0) - delta_size)
            tp_filled = float(lot.get("tp_filled_size") or 0.0) + delta_size
            pending_rebuy = float(lot.get("pending_rebuy_size") or 0.0)
            if str(lot.get("status") or "") == "open":
                pending_rebuy += delta_size * 0.5
            status = self._lot_status_after_update(
                current_status=str(lot.get("status") or ""),
                remaining_size=remaining,
                pending_rebuy_size=pending_rebuy,
            )
            self.db.update_auto_tp_lot(
                lot_id,
                remaining_size=remaining,
                tp_filled_size=tp_filled,
                pending_rebuy_size=pending_rebuy,
                status=status,
            )
        elif kind == "rebuy_buy":
            self.db.apply_entry_fill(
                root_trade_id,
                bought_size=delta_size,
                bought_usd=delta_usd,
                fill_price=actual_price,
            )
            pending_rebuy = max(0.0, float(lot.get("pending_rebuy_size") or 0.0) - delta_size)
            status = self._lot_status_after_update(
                current_status=str(lot.get("status") or ""),
                remaining_size=float(lot.get("remaining_size") or 0.0),
                pending_rebuy_size=pending_rebuy,
            )
            self.db.update_auto_tp_lot(lot_id, pending_rebuy_size=pending_rebuy, status=status)

            entry_price = float(lot.get("entry_price") or 0.0)
            plan = self._build_tp_plan(entry_price)
            if plan is None or delta_size <= EPS:
                return
            child_lot_id = self.db.insert_auto_tp_lot(
                {
                    "account_name": self.account_name,
                    "root_trade_id": root_trade_id,
                    "parent_lot_id": lot_id,
                    "leader_address": str(lot.get("leader_address") or "").lower(),
                    "token_id": lot.get("token_id") or "",
                    "condition_id": lot.get("condition_id"),
                    "market_slug": lot.get("market_slug"),
                    "outcome": lot.get("outcome"),
                    "entry_price": entry_price,
                    "original_size": delta_size,
                    "remaining_size": delta_size,
                    "tp_target_size": delta_size * plan.sell_ratio,
                    "tp_filled_size": 0.0,
                    "pending_rebuy_size": 0.0,
                    "status": "open",
                }
            )
            child_lot = self.db.get_auto_tp_lot(child_lot_id, account_name=self.account_name)
            if child_lot:
                self._maybe_place_orders_for_lot(child_lot)

    def _consume_lot_cost_for_mirror_sell(
        self,
        root_trade_id: int,
        sold_size: float,
        *,
        leader_closed: bool,
    ) -> Optional[float]:
        lots = [
            lot
            for lot in self.db.get_auto_tp_lots_for_trade(
                root_trade_id,
                account_name=self.account_name,
                include_closed=True,
            )
            if str(lot.get("status") or "") != "closed" and float(lot.get("remaining_size") or 0.0) > EPS
        ]
        if not lots:
            return None

        total_remaining = sum(float(lot.get("remaining_size") or 0.0) for lot in lots)
        if total_remaining <= EPS:
            return None

        target_sell = min(total_remaining, max(0.0, float(sold_size or 0.0)))
        remaining_to_allocate = target_sell
        consumed_cost = 0.0

        for idx, lot in enumerate(lots):
            lot_remaining = float(lot.get("remaining_size") or 0.0)
            if lot_remaining <= EPS:
                continue
            if idx == len(lots) - 1:
                alloc = min(lot_remaining, remaining_to_allocate)
            else:
                alloc = min(lot_remaining, target_sell * lot_remaining / total_remaining)
                remaining_to_allocate = max(0.0, remaining_to_allocate - alloc)
            new_remaining = max(0.0, lot_remaining - alloc)
            consumed_cost += alloc * float(lot.get("entry_price") or 0.0)
            current_status = str(lot.get("status") or "")
            self.db.update_auto_tp_lot(
                int(lot["id"]),
                remaining_size=new_remaining,
                pending_rebuy_size=0.0,
                status=(
                    "leader_closed"
                    if leader_closed
                    else (
                        current_status
                    if current_status in {"orderbook_unavailable", "balance_unavailable"}
                    else "syncing"
                    )
                ),
            )

        if not leader_closed:
            self._reopen_lots_after_mirror_sell(root_trade_id, leader_closed=False)
        return consumed_cost

    def _reopen_lots_after_mirror_sell(self, root_trade_id: int, *, leader_closed: bool) -> None:
        lots = self.db.get_auto_tp_lots_for_trade(
            root_trade_id,
            account_name=self.account_name,
            include_closed=True,
        )
        for lot in lots:
            current_status = str(lot.get("status") or "")
            if current_status in {"closed", "orderbook_unavailable", "balance_unavailable"}:
                continue
            remaining = float(lot.get("remaining_size") or 0.0)
            fields: Dict[str, Any] = {"pending_rebuy_size": 0.0}
            if leader_closed:
                fields["status"] = "leader_closed"
            else:
                plan = self._build_tp_plan(float(lot.get("entry_price") or 0.0))
                fields["tp_target_size"] = (remaining * plan.sell_ratio) if (plan is not None and remaining > EPS) else 0.0
                fields["status"] = "open" if remaining > EPS else "closed"
            self.db.update_auto_tp_lot(int(lot["id"]), **fields)

    @staticmethod
    def _lot_status_after_update(
        *,
        current_status: str,
        remaining_size: float,
        pending_rebuy_size: float,
    ) -> str:
        if current_status == "leader_closed":
            return "leader_closed"
        if current_status == "orderbook_unavailable" and (
            remaining_size > EPS or pending_rebuy_size > EPS
        ):
            return "orderbook_unavailable"
        if current_status == "balance_unavailable" and (
            remaining_size > EPS or pending_rebuy_size > EPS
        ):
            return "balance_unavailable"
        if current_status == "min_size_pending" and (
            remaining_size > EPS or pending_rebuy_size > EPS
        ):
            return "min_size_pending"
        if remaining_size <= EPS and pending_rebuy_size <= EPS:
            return "closed"
        return current_status or "open"

    @staticmethod
    def _average_cost_for_trade(trade: Dict[str, Any]) -> float:
        size_value = float(trade.get("our_size", 0.0) or 0.0)
        usd_value = float(trade.get("our_usd", 0.0) or 0.0)
        if size_value <= EPS:
            matched_size = float(trade.get("filled_size_actual", 0.0) or 0.0)
            matched_usd = float(trade.get("filled_usd_actual", 0.0) or 0.0)
            if matched_size > EPS and matched_usd > EPS:
                return matched_usd / matched_size
            return float(trade.get("our_price", 0.0) or trade.get("our_filled_price", 0.0) or 0.0)
        return usd_value / size_value

    @staticmethod
    def _available_size_for_trade(trade: Dict[str, Any]) -> float:
        live_size = float(trade.get("our_size", 0.0) or 0.0)
        if live_size > EPS:
            return live_size
        matched_size = float(trade.get("filled_size_actual", 0.0) or 0.0)
        if matched_size > EPS:
            return matched_size
        return 0.0

    def _is_leader_closed_for_trade(self, trade: Dict[str, Any]) -> bool:
        leader = str(trade.get("leader_address") or "").lower()
        token_id = str(trade.get("token_id") or "")
        if not leader or not token_id:
            return False
        try:
            remaining = self._get_leader_remaining_size(leader, token_id)
        except Exception:
            return False
        return remaining is not None and remaining <= EPS

    def _get_trade(self, trade_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.conn.execute(
            "SELECT * FROM ct_trades WHERE id=? AND account_name=? LIMIT 1",
            (int(trade_id), self.account_name),
        ).fetchone()
        return dict(row) if row else None

    def _build_tp_plan(self, entry_price: float) -> Optional[_TPPlan]:
        price = float(entry_price or 0.0)
        if price <= 0 or price > 0.7 + EPS:
            return None
        if price < 0.4:
            return _TPPlan(target_price=min(1.0, price * 2.0), sell_ratio=0.40)

        ratio = min(1.0, max(0.0, (price - 0.4) / 0.3))
        return _TPPlan(target_price=0.8 + (0.1 * ratio), sell_ratio=0.4 - (0.2 * ratio))

    @staticmethod
    def _resolve_fill_price(
        price: Optional[float],
        usd: Optional[float],
        size: float,
        *,
        fallback: Optional[float],
    ) -> Optional[float]:
        if isinstance(price, (int, float)) and float(price) > 0:
            return float(price)
        if size > EPS and isinstance(usd, (int, float)) and float(usd) > 0:
            return float(usd) / size
        if isinstance(fallback, (int, float)) and float(fallback) > 0:
            return float(fallback)
        return None

    @staticmethod
    def _is_immediate_fill(result: OrderResult) -> bool:
        status = str(getattr(result, "exchange_status", "") or "").lower()
        if status == "matched":
            return True
        try:
            return float(getattr(result, "filled_size", 0) or 0) > 0
        except Exception:
            return False

    def _record_submitted_exit_order(
        self,
        *,
        pos: dict,
        action: ExitAction,
        result: OrderResult,
    ) -> None:
        order_id = str(getattr(result, "order_id", "") or "").strip()
        if not order_id:
            return
        self.db.insert_exit_order(
            {
                "trade_id": int(pos["id"]),
                "account_name": self.account_name,
                "reason": action.reason,
                "order_id": order_id,
                "token_id": action.token_id,
                "side": action.side,
                "requested_price": action.price,
                "requested_size": action.size,
                "requested_usd": action.price * action.size,
                "status": "submitted",
                "exchange_order_status": str(getattr(result, "exchange_status", "") or "submitted"),
            }
        )
        self._notify_condition_activated(pos.get("condition_id"))

    def _notify_condition_activated(self, condition_id: Optional[str]) -> None:
        normalized = str(condition_id or "").strip().lower()
        callback = self._on_condition_activated
        if not normalized or not callable(callback):
            return
        try:
            callback(normalized)
        except Exception:
            pass

    @staticmethod
    def _normalize_exchange_status(value: Any) -> str:
        status = str(value or "").strip().lower()
        if status == "canceled":
            return "cancelled"
        return status

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_clob_transport_error(exc: Exception) -> bool:
        return exc.__class__.__name__ == "PolyApiException" and getattr(exc, "status_code", None) is None

    def _order_sync_rest_paused(self) -> bool:
        return time.time() < float(self._order_sync_rest_pause_until or 0.0)

    def _note_order_sync_exception(self, exc: Exception) -> bool:
        if not self._is_clob_transport_error(exc):
            return False
        pause_until = time.time() + ORDER_SYNC_TRANSPORT_COOLDOWN_S
        if pause_until > float(self._order_sync_rest_pause_until or 0.0):
            self._order_sync_rest_pause_until = pause_until
        self._log_sync_warning(
            "rest",
            "transport",
            f"CLOB get_order transport error; pausing REST verification for {int(ORDER_SYNC_TRANSPORT_COOLDOWN_S)}s: {exc}",
        )
        return True

    def _log_sync_warning(self, scope: str, key: str, message: str, *, throttle_s: float = 60.0) -> None:
        cache_key = (str(scope or ""), str(key or ""))
        now = time.time()
        last_ts = float(self._sync_error_log_ts.get(cache_key, 0.0) or 0.0)
        if last_ts and (now - last_ts) < throttle_s:
            return
        self._sync_error_log_ts[cache_key] = now
        sys.stderr.write(f"[{self.account_name}] [order_sync] {scope} {message}\n")
        sys.stderr.flush()

    def _log_order_failure(self, params: OrderParams, result: OrderResult, *, throttle_s: float = 30.0) -> None:
        error = str(getattr(result, "error", "") or "").strip()
        scope = str(getattr(params, "order_purpose", "") or "copytrade").strip() or "copytrade"
        token_id = str(getattr(params, "token_id", "") or "").strip()
        cache_key = ("order_submit", scope, token_id, error[:120])
        now = time.time()
        last_ts = float(self._sync_error_log_ts.get(cache_key, 0.0) or 0.0)
        if last_ts and (now - last_ts) < throttle_s:
            return
        self._sync_error_log_ts[cache_key] = now
        sys.stderr.write(
            f"[{self.account_name}] [order_submit] failed "
            f"purpose={scope} side={getattr(params, 'side', '')} "
            f"market={getattr(params, 'market_slug', None) or getattr(params, 'condition_id', '')} "
            f"token={token_id[:16]}... size={float(getattr(params, 'size', 0.0) or 0.0):.6f} "
            f"usd={float(getattr(params, 'usd', 0.0) or 0.0):.6f} error={error}\n"
        )
        sys.stderr.flush()

    @staticmethod
    def _is_orderbook_unavailable_result(result: OrderResult) -> bool:
        if str(getattr(result, "error_code", "") or "").strip().lower() == "orderbook_unavailable":
            return True
        text = str(getattr(result, "error", "") or "").lower()
        return (
            "clob_orderbook_unavailable" in text
            or (
                "orderbook" in text
                and (
                    "does not exist" in text
                    or "no orderbook exists" in text
                    or "not found" in text
                    or "404" in text
                )
            )
        )

    @staticmethod
    def _is_sell_balance_unavailable_result(params: OrderParams, result: OrderResult) -> bool:
        if str(getattr(params, "side", "") or "").strip().upper() != "SELL":
            return False
        if str(getattr(result, "error_code", "") or "").strip().lower() == "balance_allowance":
            return True
        text = str(getattr(result, "error", "") or "").lower()
        return (
            "insufficient_clob_balance asset=conditional" in text
            or (
                "not enough balance / allowance" in text
                and "balance: 0" in text
            )
        )

    @staticmethod
    def _is_min_order_size_result(result: OrderResult) -> bool:
        if str(getattr(result, "error_code", "") or "").strip().lower() == "min_order_size":
            return True
        text = str(getattr(result, "error", "") or "").lower()
        return "clob_min_order_size" in text or (
            "lower than the minimum" in text and "size" in text
        )

    @staticmethod
    def _is_trade_operationally_paused(trade: Dict[str, Any]) -> bool:
        reason = str(trade.get("skip_reason") or "").strip().lower()
        return any(reason.startswith(prefix) for prefix in PAUSED_TRADE_SKIP_PREFIXES)

    def _mark_trade_sell_balance_unavailable(
        self,
        trade: Dict[str, Any],
        params: OrderParams,
        result: OrderResult,
    ) -> None:
        trade_id = int(trade.get("id") or 0)
        if trade_id <= 0:
            return
        token_id = str(getattr(params, "token_id", "") or trade.get("token_id") or "").strip()
        reason = (
            "pending_clob_balance: conditional token balance unavailable during mirror_sell"
        )
        self.db.update_trade_status(
            trade_id,
            str(trade.get("status") or "filled"),
            skip_reason=reason,
        )
        self._log_sync_warning(
            "mirror_sell_balance",
            f"{trade_id}:{token_id}",
            (
                f"paused mirror sell for trade={trade_id} token={token_id}; "
                f"kept exit_status=open: {getattr(result, 'error', '')}"
            ),
            throttle_s=300.0,
        )

    def _mark_trade_min_size_pending(
        self,
        trade: Dict[str, Any],
        params: OrderParams,
        result: OrderResult,
    ) -> None:
        trade_id = int(trade.get("id") or 0)
        if trade_id <= 0:
            return
        token_id = str(getattr(params, "token_id", "") or trade.get("token_id") or "").strip()
        submitted_size = getattr(result, "submitted_size", None)
        min_order_size = getattr(result, "min_order_size", None)
        reason = (
            "min_size_pending: "
            f"mirror_sell size={float(submitted_size if submitted_size is not None else getattr(params, 'size', 0.0) or 0.0):.6f} "
            f"min={float(min_order_size if min_order_size is not None else 0.0):.6f}"
        )
        self.db.update_trade_status(
            trade_id,
            str(trade.get("status") or "filled"),
            skip_reason=reason,
        )
        self._log_sync_warning(
            "mirror_sell_min_size",
            f"{trade_id}:{token_id}",
            (
                f"paused mirror sell below CLOB minimum for trade={trade_id} "
                f"token={token_id}: {getattr(result, 'error', '')}"
            ),
            throttle_s=300.0,
        )

    def _mark_auto_tp_lots_balance_unavailable(
        self,
        *,
        lot_ids: List[int],
        token_id: str,
        reason: str,
    ) -> None:
        normalized_ids = sorted(
            {
                int(lot_id)
                for lot_id in lot_ids
                if str(lot_id or "").strip().isdigit() and int(lot_id) > 0
            }
        )
        if not normalized_ids:
            return
        for lot_id in normalized_ids:
            self.db.update_auto_tp_lot(
                lot_id,
                pending_rebuy_size=0.0,
                status="balance_unavailable",
            )
        self._log_sync_warning(
            "auto_tp_balance",
            token_id,
            (
                f"conditional balance unavailable token={token_id}; "
                f"marked {len(normalized_ids)} auto-TP lot(s) inactive: {reason}"
            ),
            throttle_s=300.0,
        )

    def _mark_auto_tp_lots_min_size_pending(
        self,
        *,
        lot_ids: List[int],
        token_id: str,
        reason: str,
    ) -> None:
        normalized_set = set()
        for lot_id in lot_ids:
            try:
                normalized_id = int(lot_id or 0)
            except (TypeError, ValueError):
                continue
            if normalized_id > 0:
                normalized_set.add(normalized_id)
        normalized_ids = sorted(normalized_set)
        if not normalized_ids:
            return
        for lot_id in normalized_ids:
            self.db.update_auto_tp_lot(
                lot_id,
                status="min_size_pending",
            )
        self._log_sync_warning(
            "auto_tp_min_size",
            token_id,
            (
                f"auto-TP size below CLOB minimum token={token_id}; "
                f"marked {len(normalized_ids)} lot(s) pending aggregation: {reason}"
            ),
            throttle_s=300.0,
        )

    def _mark_auto_tp_lots_orderbook_unavailable(
        self,
        *,
        lot_ids: List[int],
        token_id: str,
        reason: str,
    ) -> None:
        normalized_set = set()
        for lot_id in lot_ids:
            try:
                normalized_id = int(lot_id or 0)
            except (TypeError, ValueError):
                continue
            if normalized_id > 0:
                normalized_set.add(normalized_id)
        normalized_ids = sorted(normalized_set)
        if not normalized_ids:
            return
        for lot_id in normalized_ids:
            self.db.update_auto_tp_lot(
                lot_id,
                pending_rebuy_size=0.0,
                status="orderbook_unavailable",
            )
        self._log_sync_warning(
            "auto_tp_orderbook",
            token_id,
            (
                f"orderbook unavailable token={token_id}; "
                f"marked {len(normalized_ids)} auto-TP lot(s) inactive: {reason}"
            ),
            throttle_s=300.0,
        )

    @staticmethod
    def _row_matched_size(row: Optional[Dict[str, Any]]) -> float:
        if not row:
            return 0.0
        candidates = [
            row.get("filled_size_actual"),
            row.get("our_size"),
        ]
        for candidate in candidates:
            try:
                value = float(candidate or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    def _get_ws_fill_progress(self, order_id: str, current_matched: float) -> Dict[str, float]:
        progress = self._ws_fill_progress.get(order_id)
        if progress is None:
            progress = {
                "floor_size": max(0.0, float(current_matched or 0.0)),
                "trade_extra_size": 0.0,
                "suppressed_trade_size": 0.0,
            }
            self._ws_fill_progress[order_id] = progress
            return progress

        known_total = float(progress.get("floor_size") or 0.0) + float(progress.get("trade_extra_size") or 0.0)
        current_value = max(0.0, float(current_matched or 0.0))
        if current_value > known_total + EPS:
            progress["floor_size"] = current_value
            progress["trade_extra_size"] = 0.0
        return progress

    def _record_snapshot_progress(self, order_id: str, matched_size: float, current_matched: float) -> float:
        snapshot_size = max(0.0, float(matched_size or 0.0), float(current_matched or 0.0))
        progress = self._get_ws_fill_progress(order_id, current_matched)
        known_total = float(progress.get("floor_size") or 0.0) + float(progress.get("trade_extra_size") or 0.0)
        if snapshot_size > known_total + EPS:
            progress["suppressed_trade_size"] = float(progress.get("suppressed_trade_size") or 0.0) + (
                snapshot_size - known_total
            )
        progress["floor_size"] = snapshot_size
        progress["trade_extra_size"] = 0.0
        return snapshot_size

    def _record_trade_progress(self, order_id: str, delta_size: float, current_matched: float) -> Optional[float]:
        delta_value = max(0.0, float(delta_size or 0.0))
        if delta_value <= EPS:
            return None

        progress = self._get_ws_fill_progress(order_id, current_matched)
        suppressed = max(0.0, float(progress.get("suppressed_trade_size") or 0.0))
        if suppressed > EPS:
            consumed = min(suppressed, delta_value)
            progress["suppressed_trade_size"] = suppressed - consumed
            delta_value -= consumed
        if delta_value <= EPS:
            return None

        floor_size = max(0.0, float(progress.get("floor_size") or 0.0))
        extra_size = max(0.0, float(progress.get("trade_extra_size") or 0.0))
        current_total = max(max(0.0, float(current_matched or 0.0)), floor_size + extra_size)
        if current_total > floor_size + extra_size + EPS:
            floor_size = current_total
            extra_size = 0.0
            progress["floor_size"] = floor_size
        next_total = current_total + delta_value
        progress["trade_extra_size"] = max(0.0, next_total - floor_size)
        return next_total

    def _estimate_fill_event_usd(
        self,
        row: Dict[str, Any],
        cumulative_size: float,
        fill_price: Optional[float],
    ) -> float:
        matched = max(0.0, float(cumulative_size or 0.0))
        if matched <= EPS:
            return 0.0

        price = fill_price if isinstance(fill_price, (int, float)) and float(fill_price) > 0 else None
        if price is None:
            for key in ("filled_price_actual", "requested_price", "bucket_price"):
                candidate = row.get(key)
                if isinstance(candidate, (int, float)) and float(candidate) > 0:
                    price = float(candidate)
                    break
        if price is None:
            return 0.0
        return matched * float(price)

    def _build_order_fill_event(
        self,
        *,
        order_id: str,
        row: Dict[str, Any],
        exchange_order_status: str,
        matched_size: float,
        fill_price: Optional[float],
        source: str,
        is_delta: bool = False,
    ) -> Optional[OrderFillEvent]:
        current_matched = self._row_matched_size(row)
        requested_size = max(0.0, self._safe_float(row.get("requested_size")))

        if is_delta:
            cumulative_size = self._record_trade_progress(order_id, matched_size, current_matched)
            if cumulative_size is None or cumulative_size <= current_matched + EPS:
                return None
            status_key = "matched" if requested_size > 0 and cumulative_size + EPS >= requested_size else "live"
        else:
            cumulative_size = self._record_snapshot_progress(order_id, matched_size, current_matched)
            status_key = self._normalize_exchange_status(exchange_order_status)
            if status_key not in ("live", "matched", "expired", "cancelled"):
                status_key = "matched" if requested_size > 0 and cumulative_size + EPS >= requested_size else "live"

        normalized_price = self._safe_float(fill_price)
        actual_price = normalized_price if normalized_price > 0 else None
        return OrderFillEvent(
            account_name=self.account_name,
            order_id=order_id,
            cumulative_size=max(0.0, float(cumulative_size or 0.0)),
            cumulative_usd=self._estimate_fill_event_usd(row, cumulative_size, actual_price),
            source=str(source or "").strip().lower() or "unknown",
            filled_price=actual_price,
            exchange_order_status=status_key,
        )

    def _fill_event_from_remote_state(
        self,
        *,
        order_id: str,
        row: Dict[str, Any],
        remote: Dict[str, Any],
        source: str = "rest",
    ) -> Optional[OrderFillEvent]:
        return self._build_order_fill_event(
            order_id=order_id,
            row=row,
            exchange_order_status=str(remote.get("status") or ""),
            matched_size=float(remote.get("matched_size") or 0.0),
            fill_price=remote.get("price"),
            source=source,
            is_delta=False,
        )

    def _fill_event_from_user_event(
        self,
        *,
        order_id: str,
        row: Dict[str, Any],
        event: UserOrderEvent,
    ) -> Optional[OrderFillEvent]:
        return self._build_order_fill_event(
            order_id=order_id,
            row=row,
            exchange_order_status=str(getattr(event, "exchange_order_status", "") or ""),
            matched_size=float(getattr(event, "matched_size", 0.0) or 0.0),
            fill_price=getattr(event, "price", None),
            source="ws",
            is_delta=bool(getattr(event, "is_delta", False)),
        )

    def _load_remote_order_state(self, order_id: str) -> Optional[Dict[str, Any]]:
        client = getattr(self.executor, "_client", None)
        if client is None:
            return None

        order_info = client.get_order(order_id)
        if not isinstance(order_info, dict):
            return None

        api_status = self._normalize_exchange_status(order_info.get("status"))
        if api_status not in ("live", "matched", "expired", "cancelled"):
            return None

        size_matched = order_info.get("size_matched") or order_info.get("sizeMatched")
        matched = max(0.0, self._safe_float(size_matched))
        price_value = (
            order_info.get("avg_price")
            or order_info.get("avgPrice")
            or order_info.get("average_price")
            or order_info.get("averagePrice")
        )
        actual_price = self._safe_float(price_value) or None
        return {
            "status": api_status,
            "matched_size": matched,
            "price": actual_price,
            "order_type": str(order_info.get("order_type") or "").upper().strip(),
        }

    def _apply_entry_order_sync(
        self,
        *,
        order_id: str,
        exchange_order_status: str,
        matched_size: float,
        fill_price: Optional[float],
    ) -> Dict[str, Any]:
        event = OrderFillEvent(
            account_name=self.account_name,
            order_id=order_id,
            cumulative_size=max(0.0, float(matched_size or 0.0)),
            cumulative_usd=max(0.0, float(matched_size or 0.0))
            * max(0.0, self._safe_float(fill_price)),
            source="compat",
            filled_price=self._safe_float(fill_price) if self._safe_float(fill_price) > 0 else None,
            exchange_order_status=self._normalize_exchange_status(exchange_order_status),
        )
        return self._apply_entry_fill_event(event)

    def _apply_entry_fill_event(self, event: OrderFillEvent) -> Dict[str, Any]:
        status_key = self._normalize_exchange_status(event.exchange_order_status)
        req_size = 0.0
        row = self.db.get_trade_order_sync_row(event.order_id, account_name=self.account_name)
        if row:
            req_size = max(0.0, self._safe_float(row.get("requested_size")))
        skip_reason = None
        if status_key in ("expired", "cancelled") and event.cumulative_size > EPS:
            skip_reason = f"partial fill: {event.cumulative_size} of {req_size or 'unknown'}"

        recon = self.db.reconcile_order_state(
            event.order_id,
            account_name=self.account_name,
            exchange_order_status=status_key,
            matched_size=event.cumulative_size,
            fill_price=event.filled_price,
            skip_reason=skip_reason,
        )
        if not recon.get("updated"):
            return {"updated": 0, "buy_fill_count": 0}

        buy_fill_count = 0
        usd_delta = max(0.0, float(recon.get("usd_delta") or 0.0))
        delta_size = max(0.0, float(recon.get("delta_size") or 0.0))
        if usd_delta > EPS and delta_size > EPS:
            self.db.add_daily_spend(usd_delta, account_name=self.account_name)
            self.register_entry_fill(
                int(recon["trade_id"]),
                filled_size=delta_size,
                filled_usd=usd_delta,
                fill_price=recon.get("actual_price"),
            )
            buy_fill_count = 1
        return {"updated": 1, "buy_fill_count": buy_fill_count}

    def _apply_exit_order_sync(
        self,
        *,
        order_id: str,
        exchange_order_status: str,
        matched_size: float,
        fill_price: Optional[float],
        last_error: Optional[str] = None,
    ) -> int:
        event = OrderFillEvent(
            account_name=self.account_name,
            order_id=order_id,
            cumulative_size=max(0.0, float(matched_size or 0.0)),
            cumulative_usd=max(0.0, float(matched_size or 0.0))
            * max(0.0, self._safe_float(fill_price)),
            source="compat",
            filled_price=self._safe_float(fill_price) if self._safe_float(fill_price) > 0 else None,
            exchange_order_status=self._normalize_exchange_status(exchange_order_status),
        )
        return self._apply_exit_fill_event(event, last_error=last_error)

    def _apply_exit_fill_event(self, event: OrderFillEvent, *, last_error: Optional[str] = None) -> int:
        recon = self.db.reconcile_exit_order_state(
            event.order_id,
            account_name=self.account_name,
            exchange_order_status=self._normalize_exchange_status(event.exchange_order_status),
            matched_size=event.cumulative_size,
            fill_price=event.filled_price,
            fee_rate=float(getattr(self.config, "fee_rate", 0.0) or 0.0),
            last_error=last_error,
            apply_trade_fill=False,
        )
        if not recon.get("updated"):
            return 0

        trade = self._get_trade(int(recon["trade_id"]))
        if not trade:
            return 1

        delta_size = float(recon.get("delta_size") or 0.0)
        delta_usd = float(recon.get("delta_usd") or 0.0)
        exit_row = self.db.get_exit_order_sync_row(event.order_id, account_name=self.account_name) or {}
        reason = str(exit_row.get("reason") or "")
        if delta_size > EPS:
            if reason == "mirror_sell":
                leader_closed = self._is_leader_closed_for_trade(trade)
                cost_basis = self._consume_lot_cost_for_mirror_sell(
                    int(recon["trade_id"]),
                    delta_size,
                    leader_closed=leader_closed,
                )
                if cost_basis is None:
                    cost_basis = self._average_cost_for_trade(trade) * delta_size
                profit = delta_usd - cost_basis
                if self.config.fee_rate > 0 and delta_usd > 0:
                    profit -= self.config.fee_rate * delta_usd
                self.db.apply_exit_fill(
                    trade_id=int(recon["trade_id"]),
                    sold_size=delta_size,
                    exit_price=recon.get("actual_price"),
                    sold_usd=delta_usd,
                    profit_delta=profit,
                    close_position=False,
                    cost_basis_usd=cost_basis,
                )
            else:
                self.db.apply_exit_fill(
                    trade_id=int(recon["trade_id"]),
                    sold_size=delta_size,
                    exit_price=recon.get("actual_price"),
                    sold_usd=delta_usd,
                    profit_delta=recon.get("profit_delta"),
                    close_position=False,
                )
        elif reason == "mirror_sell" and str(recon.get("status") or "") in {"expired", "cancelled"}:
            if not self._is_leader_closed_for_trade(trade):
                self._reopen_lots_after_mirror_sell(int(recon["trade_id"]), leader_closed=False)
        return 1

    def _apply_auto_tp_order_sync(
        self,
        *,
        order_id: str,
        exchange_order_status: str,
        matched_size: float,
        fill_price: Optional[float],
        sync_source: str,
        last_error: Optional[str] = None,
    ) -> int:
        event = OrderFillEvent(
            account_name=self.account_name,
            order_id=order_id,
            cumulative_size=max(0.0, float(matched_size or 0.0)),
            cumulative_usd=max(0.0, float(matched_size or 0.0))
            * max(0.0, self._safe_float(fill_price)),
            source=str(sync_source or "compat").strip().lower() or "compat",
            filled_price=self._safe_float(fill_price) if self._safe_float(fill_price) > 0 else None,
            exchange_order_status=self._normalize_exchange_status(exchange_order_status),
        )
        return self._apply_auto_tp_fill_event(event, last_error=last_error)

    def _apply_auto_tp_fill_event(self, event: OrderFillEvent, *, last_error: Optional[str] = None) -> int:
        recon = self.db.reconcile_auto_tp_order_state(
            event.order_id,
            account_name=self.account_name,
            exchange_order_status=self._normalize_exchange_status(event.exchange_order_status),
            matched_size=event.cumulative_size,
            fill_price=event.filled_price,
            last_error=last_error,
            sync_source=event.source,
        )
        if not recon.get("updated"):
            return 0
        if float(recon.get("delta_size") or 0.0) > EPS:
            self._handle_auto_tp_fill(
                lot_id=int(recon["lot_id"]),
                root_trade_id=int(recon["root_trade_id"]),
                kind=str(recon.get("kind") or ""),
                delta_size=float(recon.get("delta_size") or 0.0),
                delta_usd=float(recon.get("delta_usd") or 0.0),
                actual_price=recon.get("actual_price"),
            )
        return 1

    def _apply_auto_tp_bucket_order_sync(
        self,
        *,
        order_id: str,
        exchange_order_status: str,
        matched_size: float,
        fill_price: Optional[float],
        sync_source: str,
        last_error: Optional[str] = None,
    ) -> int:
        event = OrderFillEvent(
            account_name=self.account_name,
            order_id=order_id,
            cumulative_size=max(0.0, float(matched_size or 0.0)),
            cumulative_usd=max(0.0, float(matched_size or 0.0))
            * max(0.0, self._safe_float(fill_price)),
            source=str(sync_source or "compat").strip().lower() or "compat",
            filled_price=self._safe_float(fill_price) if self._safe_float(fill_price) > 0 else None,
            exchange_order_status=self._normalize_exchange_status(exchange_order_status),
        )
        return self._apply_auto_tp_bucket_fill_event(event, last_error=last_error)

    def _apply_auto_tp_bucket_fill_event(self, event: OrderFillEvent, *, last_error: Optional[str] = None) -> int:
        recon = self.db.reconcile_auto_tp_bucket_order_state(
            event.order_id,
            account_name=self.account_name,
            exchange_order_status=self._normalize_exchange_status(event.exchange_order_status),
            matched_size=event.cumulative_size,
            fill_price=event.filled_price,
            last_error=last_error,
            sync_source=event.source,
        )
        if not recon.get("updated"):
            return 0
        if float(recon.get("delta_size") or 0.0) > EPS:
            self._handle_auto_tp_bucket_fill(
                bucket_order_id=int(recon["bucket_order_id"]),
                kind=str(recon.get("kind") or ""),
                bucket_price=float(recon.get("bucket_price") or 0.0),
                delta_size=float(recon.get("delta_size") or 0.0),
                actual_price=self._resolve_fill_price(
                    recon.get("actual_price"),
                    recon.get("delta_usd"),
                    float(recon.get("delta_size") or 0.0),
                    fallback=recon.get("bucket_price"),
                ),
            )
        return 1

    def _verify_recent_entry_orders(self, *, source: str = "rest") -> int:
        if source != "rest":
            return 0
        client = getattr(self.executor, "_client", None)
        if client is None:
            return 0
        if self._order_sync_rest_paused():
            return 0

        updated = 0
        recent = self.db.get_recent_orders_for_verification(account_name=self.account_name, hours=24, limit=30)
        for row in recent:
            if self._order_sync_rest_paused():
                break
            order_id = str(row.get("our_order_id") or "").strip()
            if not order_id:
                continue
            try:
                remote = self._load_remote_order_state(order_id)
            except Exception as e:
                self._log_sync_warning("entry", order_id, f"fallback get_order failed order={order_id}: {e}")
                if self._note_order_sync_exception(e):
                    break
                continue
            if remote is None:
                continue
            sync_row = self.db.get_trade_order_sync_row(order_id, account_name=self.account_name) or row
            event = self._fill_event_from_remote_state(
                order_id=order_id,
                row=sync_row,
                remote=remote,
                source=source,
            )
            if event is None:
                continue
            result = self._apply_entry_fill_event(event)
            updated += int(result.get("buy_fill_count") or 0)
        return updated

    def _verify_recent_exit_orders(self, *, source: str = "rest") -> int:
        if source != "rest":
            return 0
        client = getattr(self.executor, "_client", None)
        if client is None:
            return 0
        if self._order_sync_rest_paused():
            return 0

        updated = 0
        recent = self.db.get_recent_exit_orders_for_verification(account_name=self.account_name, hours=24, limit=30)
        for row in recent:
            if self._order_sync_rest_paused():
                break
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            try:
                remote = self._load_remote_order_state(order_id)
            except Exception as e:
                message = f"fallback get_order failed order={order_id}: {e}"
                self.db.record_exit_order_sync_failure(
                    order_id,
                    account_name=self.account_name,
                    last_error=message,
                )
                self._log_sync_warning("exit", order_id, message)
                if self._note_order_sync_exception(e):
                    break
                continue
            if remote is None:
                continue
            event = self._fill_event_from_remote_state(
                order_id=order_id,
                row=row,
                remote=remote,
                source=source,
            )
            if event is None:
                continue
            updated += self._apply_exit_fill_event(event)
        return updated

    def _verify_recent_auto_tp_orders(self, *, source: str = "rest") -> int:
        if source != "rest":
            return 0
        client = getattr(self.executor, "_client", None)
        if client is None:
            return 0
        if self._order_sync_rest_paused():
            return 0

        updated = 0
        recent = self.db.get_recent_auto_tp_orders_for_verification(account_name=self.account_name, hours=24, limit=100)
        for row in recent:
            if self._order_sync_rest_paused():
                break
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            try:
                remote = self._load_remote_order_state(order_id)
            except Exception as e:
                message = f"fallback get_order failed order={order_id}: {e}"
                self.db.record_auto_tp_order_sync_failure(
                    order_id,
                    account_name=self.account_name,
                    last_error=message,
                )
                self._log_sync_warning("auto_tp_legacy", order_id, message)
                if self._note_order_sync_exception(e):
                    break
                continue
            if remote is None:
                continue
            if str(remote.get("status") or "") == "live" and str(remote.get("order_type") or "") == "GTD":
                if str(row.get("exchange_order_status") or "").lower() != "cancel_requested":
                    if self.executor.cancel_order(order_id):
                        self.db.update_auto_tp_order_status(
                            order_id,
                            account_name=self.account_name,
                            status="submitted",
                            exchange_order_status="cancel_requested",
                        )
                continue
            event = self._fill_event_from_remote_state(
                order_id=order_id,
                row=row,
                remote=remote,
                source=source,
            )
            if event is None:
                continue
            updated += self._apply_auto_tp_fill_event(event)
        return updated

    def process_exits(
        self,
        new_leader_trades: List[LeaderTrade],
        *,
        skip_verification: bool = False,
    ) -> List[ExitAction]:
        self._leader_position_cache = {}
        if not skip_verification:
            self.verify_recent_order_state(source="rest")
        grouped_sells = self._group_leader_sells(new_leader_trades)
        if self._auto_tp_enabled() and grouped_sells:
            self._sync_auto_tp_before_leader_sells(grouped_sells)
        strategy = self.config.exit_strategy
        if strategy == "mirror_sell":
            actions = self._mirror_sell(grouped_sells)
        elif strategy == "hold_to_resolution":
            actions = []
        else:
            actions = []
        if self._auto_tp_enabled():
            self._refresh_auto_tp_orders()
        return actions

    def verify_recent_order_state(self, *, source: str = "rest") -> Dict[str, Any]:
        summary = {"buy_fill_count": 0, "updated": 0}
        summary["buy_fill_count"] += int(self._verify_recent_entry_orders(source=source) or 0)
        summary["updated"] += int(self._verify_recent_exit_orders(source=source) or 0)
        if self._auto_tp_enabled():
            summary["updated"] += int(self._verify_recent_auto_tp_bucket_orders(source=source) or 0)
            summary["updated"] += int(self._verify_recent_auto_tp_orders(source=source) or 0)
        return summary

    def process_user_order_events(self, events: List[UserOrderEvent]) -> Dict[str, Any]:
        summary = {"buy_fill_count": 0, "updated": 0, "event_count": len(events or [])}
        if not events:
            return summary

        ordered_events = sorted(
            list(events),
            key=lambda event: (
                0 if str(getattr(event, "channel_event", "")).lower() == "trade" else 1,
                str(getattr(event, "order_id", "") or ""),
                str(getattr(event, "raw_id", "") or ""),
            ),
        )
        for event in ordered_events:
            order_id = str(getattr(event, "order_id", "") or "").strip()
            if not order_id:
                continue
            if self._process_trade_user_event(event, summary):
                continue
            if self._process_exit_user_event(event, summary):
                continue
            if self._process_auto_tp_bucket_user_event(event, summary):
                continue
            self._process_auto_tp_legacy_user_event(event, summary)
        return summary

    def _resolve_user_event_state(
        self,
        order_id: str,
        row: Dict[str, Any],
        event: UserOrderEvent,
    ) -> Optional[Tuple[str, float, Optional[float]]]:
        fill_event = self._fill_event_from_user_event(order_id=order_id, row=row, event=event)
        if fill_event is None:
            return None
        return fill_event.exchange_order_status, fill_event.cumulative_size, fill_event.filled_price

    def _process_trade_user_event(self, event: UserOrderEvent, summary: Dict[str, Any]) -> bool:
        order_id = str(getattr(event, "order_id", "") or "").strip()
        row = self.db.get_trade_order_sync_row(order_id, account_name=self.account_name)
        if row is None:
            return False
        fill_event = self._fill_event_from_user_event(order_id=order_id, row=row, event=event)
        if fill_event is None:
            return True
        result = self._apply_entry_fill_event(fill_event)
        summary["buy_fill_count"] += int(result.get("buy_fill_count") or 0)
        summary["updated"] += int(result.get("updated") or 0)
        return True

    def _process_exit_user_event(self, event: UserOrderEvent, summary: Dict[str, Any]) -> bool:
        order_id = str(getattr(event, "order_id", "") or "").strip()
        row = self.db.get_exit_order_sync_row(order_id, account_name=self.account_name)
        if row is None:
            return False
        fill_event = self._fill_event_from_user_event(order_id=order_id, row=row, event=event)
        if fill_event is None:
            return True
        summary["updated"] += self._apply_exit_fill_event(fill_event)
        return True

    def _process_auto_tp_bucket_user_event(self, event: UserOrderEvent, summary: Dict[str, Any]) -> bool:
        order_id = str(getattr(event, "order_id", "") or "").strip()
        row = self.db.get_auto_tp_bucket_order_sync_row(order_id, account_name=self.account_name)
        if row is None:
            return False
        fill_event = self._fill_event_from_user_event(order_id=order_id, row=row, event=event)
        if fill_event is None:
            return True
        summary["updated"] += self._apply_auto_tp_bucket_fill_event(fill_event)
        return True

    def _process_auto_tp_legacy_user_event(self, event: UserOrderEvent, summary: Dict[str, Any]) -> bool:
        order_id = str(getattr(event, "order_id", "") or "").strip()
        row = self.db.get_auto_tp_order_sync_row(order_id, account_name=self.account_name)
        if row is None:
            return False
        fill_event = self._fill_event_from_user_event(order_id=order_id, row=row, event=event)
        if fill_event is None:
            return True
        summary["updated"] += self._apply_auto_tp_fill_event(fill_event)
        return True

    def _sync_auto_tp_before_leader_sells(
        self,
        grouped_sells: Dict[Tuple[str, str], Dict[str, Any]],
    ) -> None:
        for (leader_address, token_id), payload in grouped_sells.items():
            leader_remaining = self._get_leader_remaining_size(leader_address, token_id)
            payload["leader_remaining"] = leader_remaining
            leader_closed = leader_remaining is not None and leader_remaining <= EPS

            for order in self.db.get_open_auto_tp_bucket_orders_for_group(
                account_name=self.account_name,
                leader_address=leader_address,
                token_id=token_id,
            ):
                order_id = str(order.get("order_id") or "").strip()
                if not order_id:
                    continue
                if self.executor.cancel_order(order_id):
                    self.db.update_auto_tp_bucket_order_status(
                        order_id,
                        account_name=self.account_name,
                        status="submitted",
                        exchange_order_status="cancel_requested",
                    )

            for order in self.db.get_open_auto_tp_orders_for_group(
                account_name=self.account_name,
                leader_address=leader_address,
                token_id=token_id,
            ):
                order_id = str(order.get("order_id") or "").strip()
                if not order_id:
                    continue
                if self.executor.cancel_order(order_id):
                    self.db.update_auto_tp_order_status(
                        order_id,
                        account_name=self.account_name,
                        status="submitted",
                        exchange_order_status="cancel_requested",
                    )

            for lot in self.db.get_auto_tp_lots_for_group(
                account_name=self.account_name,
                leader_address=leader_address,
                token_id=token_id,
                include_closed=False,
            ):
                self.db.update_auto_tp_lot(
                    int(lot["id"]),
                    pending_rebuy_size=0.0,
                    status=("leader_closed" if leader_closed else "syncing"),
                )

    def _refresh_auto_tp_orders(self) -> None:
        rows = self.db.conn.execute(
            "SELECT DISTINCT leader_address, token_id "
            "FROM ct_auto_tp_lots WHERE account_name=? AND status='open' "
            "ORDER BY leader_address ASC, token_id ASC",
            (self.account_name,),
        ).fetchall()
        for row in rows:
            leader_address = str(row["leader_address"] or "").lower()
            token_id = str(row["token_id"] or "")
            if leader_address and token_id:
                self._refresh_auto_tp_group(leader_address, token_id)

    def register_entry_fill(
        self,
        trade_id: int,
        *,
        filled_size: float,
        filled_usd: Optional[float],
        fill_price: Optional[float],
    ) -> None:
        if not self._auto_tp_enabled():
            return
        size_value = max(0.0, float(filled_size or 0.0))
        if size_value <= EPS:
            return

        trade = self._get_trade(trade_id)
        if not trade:
            return

        entry_price = self._resolve_fill_price(
            fill_price,
            filled_usd,
            size_value,
            fallback=trade.get("our_price"),
        )
        if entry_price is None or entry_price <= 0:
            return

        plan = self._build_tp_plan(entry_price)
        if plan is None:
            return

        self.db.insert_auto_tp_lot(
            {
                "account_name": self.account_name,
                "root_trade_id": int(trade_id),
                "parent_lot_id": None,
                "leader_address": str(trade.get("leader_address") or "").lower(),
                "token_id": trade.get("token_id") or "",
                "condition_id": trade.get("condition_id"),
                "market_slug": trade.get("market_slug"),
                "outcome": trade.get("outcome"),
                "entry_price": entry_price,
                "original_size": size_value,
                "remaining_size": size_value,
                "tp_target_size": size_value * plan.sell_ratio,
                "tp_filled_size": 0.0,
                "pending_rebuy_size": 0.0,
                "status": "open",
            }
        )
        leader_address = str(trade.get("leader_address") or "").lower()
        token_id = str(trade.get("token_id") or "")
        if leader_address and token_id:
            self._refresh_auto_tp_group(leader_address, token_id)

    def _verify_recent_auto_tp_bucket_orders(self, *, source: str = "rest") -> int:
        if source != "rest":
            return 0
        client = getattr(self.executor, "_client", None)
        if client is None:
            return 0
        if self._order_sync_rest_paused():
            return 0

        updated = 0
        recent = self.db.get_recent_auto_tp_bucket_orders_for_verification(
            account_name=self.account_name,
            hours=24,
            limit=100,
        )
        for row in recent:
            if self._order_sync_rest_paused():
                break
            order_id = str(row.get("order_id") or "").strip()
            if not order_id:
                continue
            try:
                remote = self._load_remote_order_state(order_id)
            except Exception as e:
                message = f"fallback get_order failed order={order_id}: {e}"
                self.db.record_auto_tp_bucket_order_sync_failure(
                    order_id,
                    account_name=self.account_name,
                    last_error=message,
                )
                self._log_sync_warning("auto_tp_bucket", order_id, message)
                if self._note_order_sync_exception(e):
                    break
                continue
            if remote is None:
                continue
            event = self._fill_event_from_remote_state(
                order_id=order_id,
                row=row,
                remote=remote,
                source=source,
            )
            if event is None:
                continue
            updated += self._apply_auto_tp_bucket_fill_event(event)
        return updated

    def _refresh_auto_tp_group(self, leader_address: str, token_id: str) -> None:
        lots = self.db.get_auto_tp_lots_for_group(
            account_name=self.account_name,
            leader_address=leader_address,
            token_id=token_id,
            include_closed=False,
        )
        if not lots:
            return

        if self.db.get_open_auto_tp_orders_for_group(
            account_name=self.account_name,
            leader_address=leader_address,
            token_id=token_id,
        ):
            return

        live_bucket_orders = self.db.get_open_auto_tp_bucket_orders_for_group(
            account_name=self.account_name,
            leader_address=leader_address,
            token_id=token_id,
        )
        live_keys = {
            (str(order.get("kind") or ""), float(order.get("bucket_price") or 0.0))
            for order in live_bucket_orders
        }

        tick_size, min_order_size = self._get_market_constraints(token_id)
        buckets = self._build_auto_tp_buckets(
            lots=lots,
            tick_size=tick_size,
            min_order_size=min_order_size,
        )
        for bucket in buckets:
            key = (bucket["kind"], bucket["bucket_price"])
            if key in live_keys:
                continue
            self._submit_auto_tp_bucket(bucket)

    def _build_auto_tp_buckets(
        self,
        *,
        lots: List[Dict[str, Any]],
        tick_size: float,
        min_order_size: float,
    ) -> List[Dict[str, Any]]:
        buckets: Dict[Tuple[str, float], Dict[str, Any]] = {}
        for lot in lots:
            if str(lot.get("status") or "") not in AUTO_TP_ACTIVE_LOT_STATUSES:
                continue
            root_trade_id = int(lot.get("root_trade_id") or 0)
            if root_trade_id and self.db.has_open_exit_order(root_trade_id, account_name=self.account_name):
                continue

            remaining_size = float(lot.get("remaining_size") or 0.0)
            pending_rebuy = float(lot.get("pending_rebuy_size") or 0.0)
            if remaining_size <= EPS and pending_rebuy <= EPS:
                self.db.update_auto_tp_lot(int(lot["id"]), status="closed")
                continue

            tp_remaining = max(
                0.0,
                min(
                    remaining_size,
                    float(lot.get("tp_target_size") or 0.0) - float(lot.get("tp_filled_size") or 0.0),
                ),
            )
            if tp_remaining > EPS:
                plan = self._build_tp_plan(float(lot.get("entry_price") or 0.0))
                if plan is not None:
                    bucket_price = self._normalize_bucket_price(plan.target_price, tick_size, kind="tp_sell")
                    key = ("tp_sell", bucket_price)
                    bucket = buckets.setdefault(
                        key,
                        {
                            "account_name": self.account_name,
                            "leader_address": str(lot.get("leader_address") or "").lower(),
                            "token_id": str(lot.get("token_id") or ""),
                            "condition_id": lot.get("condition_id"),
                            "market_slug": lot.get("market_slug"),
                            "outcome": lot.get("outcome"),
                            "kind": "tp_sell",
                            "side": "SELL",
                            "bucket_price": bucket_price,
                            "requested_size_total": 0.0,
                            "submit_size": 0.0,
                            "min_order_size": min_order_size,
                            "contributions": [],
                        },
                    )
                    bucket["requested_size_total"] += tp_remaining
                    bucket["contributions"].append(
                        {
                            "lot_id": int(lot["id"]),
                            "root_trade_id": root_trade_id,
                            "account_name": self.account_name,
                            "requested_size": tp_remaining,
                        }
                    )

            if pending_rebuy > EPS:
                bucket_price = self._normalize_bucket_price(
                    float(lot.get("entry_price") or 0.0),
                    tick_size,
                    kind="rebuy_buy",
                )
                key = ("rebuy_buy", bucket_price)
                bucket = buckets.setdefault(
                    key,
                    {
                        "account_name": self.account_name,
                        "leader_address": str(lot.get("leader_address") or "").lower(),
                        "token_id": str(lot.get("token_id") or ""),
                        "condition_id": lot.get("condition_id"),
                        "market_slug": lot.get("market_slug"),
                        "outcome": lot.get("outcome"),
                        "kind": "rebuy_buy",
                        "side": "BUY",
                        "bucket_price": bucket_price,
                        "requested_size_total": 0.0,
                        "submit_size": 0.0,
                        "min_order_size": min_order_size,
                        "contributions": [],
                    },
                )
                bucket["requested_size_total"] += pending_rebuy
                bucket["contributions"].append(
                    {
                        "lot_id": int(lot["id"]),
                        "root_trade_id": root_trade_id,
                        "account_name": self.account_name,
                        "requested_size": pending_rebuy,
                    }
                )

        out: List[Dict[str, Any]] = []
        for bucket in buckets.values():
            submit_size = self._floor_share_size(float(bucket.get("requested_size_total") or 0.0))
            bucket["submit_size"] = submit_size
            if submit_size <= EPS:
                continue
            if submit_size + EPS < float(bucket.get("min_order_size") or 0.0):
                self._mark_auto_tp_lots_min_size_pending(
                    lot_ids=[
                        int(contribution["lot_id"])
                        for contribution in list(bucket.get("contributions") or [])
                        if contribution.get("lot_id") is not None
                    ],
                    token_id=str(bucket.get("token_id") or ""),
                    reason=(
                        f"submit_size={submit_size:.6f} "
                        f"min={float(bucket.get('min_order_size') or 0.0):.6f}"
                    ),
                )
                continue
            out.append(bucket)
        out.sort(key=lambda bucket: (str(bucket["kind"]), float(bucket["bucket_price"])))
        return out

    def _submit_auto_tp_bucket(self, bucket: Dict[str, Any]) -> None:
        submit_size = float(bucket.get("submit_size") or 0.0)
        bucket_price = float(bucket.get("bucket_price") or 0.0)
        if submit_size <= EPS or bucket_price <= 0:
            return

        purpose = "auto_tp" if bucket["kind"] == "tp_sell" else "auto_rebuy"
        params = OrderParams(
            token_id=str(bucket.get("token_id") or ""),
            side=str(bucket.get("side") or ""),
            price=bucket_price,
            size=submit_size,
            usd=submit_size * bucket_price,
            condition_id=str(bucket.get("condition_id") or ""),
            market_slug=bucket.get("market_slug"),
            outcome=bucket.get("outcome"),
            order_purpose=purpose,
        )
        result = self.executor.execute_order(params)
        if not result.success:
            self._log_order_failure(params, result)
            if self._is_sell_balance_unavailable_result(params, result):
                self._mark_auto_tp_lots_balance_unavailable(
                    lot_ids=[
                        int(contribution["lot_id"])
                        for contribution in list(bucket.get("contributions") or [])
                        if contribution.get("lot_id") is not None
                    ],
                    token_id=str(bucket.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            if self._is_min_order_size_result(result):
                self._mark_auto_tp_lots_min_size_pending(
                    lot_ids=[
                        int(contribution["lot_id"])
                        for contribution in list(bucket.get("contributions") or [])
                        if contribution.get("lot_id") is not None
                    ],
                    token_id=str(bucket.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            if self._is_orderbook_unavailable_result(result):
                self._mark_auto_tp_lots_orderbook_unavailable(
                    lot_ids=[
                        int(contribution["lot_id"])
                        for contribution in list(bucket.get("contributions") or [])
                        if contribution.get("lot_id") is not None
                    ],
                    token_id=str(bucket.get("token_id") or ""),
                    reason=str(getattr(result, "error", "") or ""),
                )
            return

        immediate_fill = self._is_immediate_fill(result)
        order_id = str(getattr(result, "order_id", "") or "").strip()
        if not order_id and not immediate_fill:
            return

        bucket_order_id = self.db.insert_auto_tp_bucket_order(
            {
                "account_name": self.account_name,
                "leader_address": bucket.get("leader_address"),
                "token_id": bucket.get("token_id"),
                "condition_id": bucket.get("condition_id"),
                "market_slug": bucket.get("market_slug"),
                "outcome": bucket.get("outcome"),
                "kind": bucket.get("kind"),
                "side": bucket.get("side"),
                "bucket_price": bucket_price,
                "requested_size": submit_size,
                "requested_usd": submit_size * bucket_price,
                "order_id": order_id or None,
                "status": ("filled" if immediate_fill else "submitted"),
                "exchange_order_status": str(
                    getattr(result, "exchange_status", "") or ("matched" if immediate_fill else "submitted")
                ),
                "filled_size_actual": (result.filled_size if immediate_fill else None),
                "filled_usd_actual": (result.filled_usd if immediate_fill else None),
                "filled_price_actual": (result.filled_price if immediate_fill else None),
            }
        )
        if bucket_order_id <= 0:
            return
        self.db.insert_auto_tp_bucket_order_lots(
            bucket_order_id,
            list(bucket.get("contributions") or []),
        )
        self._notify_condition_activated(bucket.get("condition_id"))
        if immediate_fill:
            self._handle_auto_tp_bucket_fill(
                bucket_order_id=bucket_order_id,
                kind=str(bucket.get("kind") or ""),
                bucket_price=bucket_price,
                delta_size=float(result.filled_size or 0.0),
                actual_price=self._resolve_fill_price(
                    result.filled_price,
                    result.filled_usd,
                    float(result.filled_size or 0.0),
                    fallback=bucket_price,
                ),
            )

    def _handle_auto_tp_bucket_fill(
        self,
        *,
        bucket_order_id: int,
        kind: str,
        bucket_price: float,
        delta_size: float,
        actual_price: Optional[float],
    ) -> None:
        mapping_rows = self.db.get_auto_tp_bucket_order_lot_rows(
            bucket_order_id,
            account_name=self.account_name,
        )
        if not mapping_rows:
            return
        allocations = self._allocate_bucket_fill(mapping_rows, delta_size)
        if not allocations:
            return

        fill_price = self._resolve_fill_price(
            actual_price,
            None,
            delta_size,
            fallback=bucket_price,
        ) or bucket_price

        for mapping_row, allocated_size in allocations:
            if allocated_size <= EPS:
                continue
            self.db.add_auto_tp_bucket_order_lot_fill(int(mapping_row["id"]), allocated_size)
            lot = self.db.get_auto_tp_lot(int(mapping_row["lot_id"]), account_name=self.account_name)
            if not lot:
                continue

            if kind == "tp_sell":
                cost_basis = float(lot.get("entry_price") or 0.0) * allocated_size
                sold_usd = allocated_size * fill_price
                profit = sold_usd - cost_basis
                if self.config.fee_rate > 0 and sold_usd > 0:
                    profit -= self.config.fee_rate * sold_usd
                self.db.apply_exit_fill(
                    trade_id=int(mapping_row["root_trade_id"]),
                    sold_size=allocated_size,
                    exit_price=fill_price,
                    sold_usd=sold_usd,
                    profit_delta=profit,
                    close_position=False,
                    cost_basis_usd=cost_basis,
                )

                remaining = max(0.0, float(lot.get("remaining_size") or 0.0) - allocated_size)
                tp_filled = float(lot.get("tp_filled_size") or 0.0) + allocated_size
                pending_rebuy = float(lot.get("pending_rebuy_size") or 0.0)
                if str(lot.get("status") or "") in AUTO_TP_ACTIVE_LOT_STATUSES:
                    pending_rebuy += allocated_size * 0.5
                status = self._lot_status_after_update(
                    current_status=str(lot.get("status") or ""),
                    remaining_size=remaining,
                    pending_rebuy_size=pending_rebuy,
                )
                self.db.update_auto_tp_lot(
                    int(lot["id"]),
                    remaining_size=remaining,
                    tp_filled_size=tp_filled,
                    pending_rebuy_size=pending_rebuy,
                    status=status,
                )
            elif kind == "rebuy_buy":
                bought_usd = allocated_size * fill_price
                self.db.apply_entry_fill(
                    int(mapping_row["root_trade_id"]),
                    bought_size=allocated_size,
                    bought_usd=bought_usd,
                    fill_price=fill_price,
                )
                pending_rebuy = max(0.0, float(lot.get("pending_rebuy_size") or 0.0) - allocated_size)
                status = self._lot_status_after_update(
                    current_status=str(lot.get("status") or ""),
                    remaining_size=float(lot.get("remaining_size") or 0.0),
                    pending_rebuy_size=pending_rebuy,
                )
                self.db.update_auto_tp_lot(
                    int(lot["id"]),
                    pending_rebuy_size=pending_rebuy,
                    status=status,
                )

                child_entry_price = bucket_price if bucket_price > 0 else fill_price
                if child_entry_price is None or child_entry_price <= 0:
                    continue
                plan = self._build_tp_plan(float(child_entry_price))
                if plan is None:
                    continue
                self.db.insert_auto_tp_lot(
                    {
                        "account_name": self.account_name,
                        "root_trade_id": int(mapping_row["root_trade_id"]),
                        "parent_lot_id": int(lot["id"]),
                        "leader_address": str(lot.get("leader_address") or "").lower(),
                        "token_id": lot.get("token_id") or "",
                        "condition_id": lot.get("condition_id"),
                        "market_slug": lot.get("market_slug"),
                        "outcome": lot.get("outcome"),
                        "entry_price": child_entry_price,
                        "original_size": allocated_size,
                        "remaining_size": allocated_size,
                        "tp_target_size": allocated_size * plan.sell_ratio,
                        "tp_filled_size": 0.0,
                        "pending_rebuy_size": 0.0,
                        "status": "open",
                    }
                )

    def _allocate_bucket_fill(
        self,
        mapping_rows: List[Dict[str, Any]],
        delta_size: float,
    ) -> List[Tuple[Dict[str, Any], float]]:
        unit = Decimal("0.01")
        delta_units = int(
            (Decimal(str(max(0.0, float(delta_size or 0.0)))) / unit).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )
        if delta_units <= 0:
            return []

        rows: List[Dict[str, Any]] = []
        total_capacity_units = 0
        for row in mapping_rows:
            remaining = max(
                0.0,
                float(row.get("requested_size") or 0.0) - float(row.get("filled_size_allocated") or 0.0),
            )
            capacity_units = int((Decimal(str(remaining)) / unit).to_integral_value(rounding=ROUND_FLOOR))
            if capacity_units <= 0:
                continue
            total_capacity_units += capacity_units
            rows.append(
                {
                    "row": row,
                    "capacity_units": capacity_units,
                    "base_units": 0,
                    "remainder": Decimal("0"),
                }
            )

        if not rows or total_capacity_units <= 0:
            return []

        target_units = min(delta_units, total_capacity_units)
        allocated_units = 0
        total_capacity_decimal = Decimal(total_capacity_units)
        target_units_decimal = Decimal(target_units)

        for entry in rows:
            exact = (target_units_decimal * Decimal(entry["capacity_units"])) / total_capacity_decimal
            base_units = min(
                entry["capacity_units"],
                int(exact.to_integral_value(rounding=ROUND_FLOOR)),
            )
            entry["base_units"] = base_units
            entry["remainder"] = exact - Decimal(base_units)
            allocated_units += base_units

        leftover = target_units - allocated_units
        rows.sort(
            key=lambda entry: (
                -float(entry["remainder"]),
                int(entry["row"]["id"]),
            )
        )
        idx = 0
        while leftover > 0 and rows:
            entry = rows[idx % len(rows)]
            if entry["base_units"] < entry["capacity_units"]:
                entry["base_units"] += 1
                leftover -= 1
            idx += 1
            if idx > len(rows) * 4 and all(
                row["base_units"] >= row["capacity_units"] for row in rows
            ):
                break

        allocations: List[Tuple[Dict[str, Any], float]] = []
        for entry in rows:
            if entry["base_units"] <= 0:
                continue
            allocations.append((entry["row"], float(unit * entry["base_units"])))

        allocations.sort(key=lambda item: int(item[0]["id"]))
        return allocations

    def _get_market_constraints(self, token_id: str) -> Tuple[float, float]:
        getter = getattr(self.executor, "get_market_constraints", None)
        if callable(getter):
            try:
                tick_size, min_order_size = getter(token_id)
                tick_value = float(tick_size or 0.01)
                min_value = max(0.0, float(min_order_size or 0.0))
                return (tick_value if tick_value > 0 else 0.01, min_value)
            except Exception:
                pass
        return 0.01, 0.0

    @staticmethod
    def _normalize_bucket_price(price: float, tick_size: float, *, kind: str) -> float:
        tick = Decimal(str(tick_size if tick_size and tick_size > 0 else 0.01))
        px = Decimal(str(max(0.0, float(price or 0.0)))).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_HALF_UP,
        )
        if px <= 0:
            return 0.0
        rounding = ROUND_CEILING if kind == "tp_sell" else ROUND_FLOOR
        normalized = (px / tick).to_integral_value(rounding=rounding) * tick
        normalized = max(tick, min(Decimal("1"), normalized))
        return float(normalized)

    @staticmethod
    def _floor_share_size(size: float) -> float:
        normalized = Decimal(str(max(0.0, float(size or 0.0)))).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_HALF_UP,
        )
        return float(normalized.quantize(Decimal("0.01"), rounding=ROUND_FLOOR))

    def _get_leader_remaining_size(self, leader_address: str, token_id: str) -> Optional[float]:
        key = (leader_address.lower(), token_id)
        if key in self._leader_position_cache:
            return self._leader_position_cache[key]

        offset = 0
        limit = 200
        while True:
            data = http_get_json(
                self.session,
                f"{DATA_API}/positions",
                params={"user": leader_address.lower(), "sizeThreshold": 0, "limit": limit, "offset": offset},
            )
            if not isinstance(data, list) or not data:
                break

            for row in data:
                if not isinstance(row, dict):
                    continue
                p = extract_position_fields(row)
                if not p:
                    continue
                if str(p.get("token_id") or "") != str(token_id):
                    continue
                size = p.get("size")
                if isinstance(size, (int, float)):
                    v = max(0.0, abs(float(size)))
                    self._leader_position_cache[key] = v
                    return v

            if len(data) < limit:
                break
            offset += limit

        self._leader_position_cache[key] = 0.0
        return 0.0
