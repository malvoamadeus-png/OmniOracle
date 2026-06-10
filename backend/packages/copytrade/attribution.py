"""结算后利润归因."""

import sys
from typing import Dict, List

from copytrade.db import CopyTradeDB


def attribute_profits(db: CopyTradeDB, condition_id: str) -> List[Dict]:
    """对指定市场的所有 copy trades 进行利润归因.

    按 leader_address 分组，权重 = sum(price * size) per leader，
    按权重比例分配总利润。
    """
    trades = db.get_trades_for_condition(condition_id)
    if not trades:
        sys.stderr.write(f"[attribution] 未找到 condition_id={condition_id} 的交易\n")
        return []

    # 按 leader 分组计算权重
    leader_weights: Dict[str, float] = {}
    for t in trades:
        addr = t.get("leader_address", "")
        price = t.get("leader_price") or 0
        size = t.get("leader_size") or 0
        leader_weights[addr] = leader_weights.get(addr, 0) + (price * size)

    total_weight = sum(leader_weights.values())
    if total_weight <= 0:
        sys.stderr.write(f"[attribution] 总权重为 0，无法归因\n")
        return []

    # 计算总利润
    total_profit = 0.0
    for t in trades:
        p = t.get("profit")
        if isinstance(p, (int, float)):
            total_profit += float(p)

    # 按权重分配
    results = []
    for addr, weight in leader_weights.items():
        share = weight / total_weight
        attributed = total_profit * share

        attr = {
            "condition_id": condition_id,
            "leader_address": addr,
            "weight": weight,
            "profit_share": share,
            "attributed_profit": attributed,
        }
        db.insert_attribution(attr)
        results.append(attr)

        sys.stderr.write(
            f"[attribution] {addr[:10]}... "
            f"权重={weight:.2f} 占比={share:.1%} "
            f"归因利润=${attributed:.2f}\n"
        )

    sys.stderr.write(
        f"[attribution] 总利润=${total_profit:.2f} 分配给 {len(results)} 个 leader\n"
    )
    return results
