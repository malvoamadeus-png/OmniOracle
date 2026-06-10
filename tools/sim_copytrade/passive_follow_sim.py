"""
Passive Follow Simulation — 验证"0门槛跟每笔小单 + 限价单(0滑点) + 随机漏单"的可行性。

用法:
    python tools/sim_copytrade/passive_follow_sim.py --address 0x68146921df11eab44296dc4e58025ca84741a9e7

复用 main.py 的数据获取和 run_simulation 引擎，但:
  - 不做 maker-like 聚合，直接跟原始信号
  - buy_premium = 0, sell_slippage = 0（限价单）
  - 手续费保留
  - 每笔 BUY 以 fill_probability 概率成交（模拟漏单）
  - 多 seed 取平均
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for _path in (SIM_ROOT, SIM_PARENT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from main import (
    Strategy,
    TradeEvent,
    StrategyState,
    Position,
    PriceInfo,
    ONLY_EXIT_MODE,
    SPORTS_FEE_RATE,
    SPORTS_FEE_EXPONENT,
    as_float,
    fetch_activity_events,
    fetch_prices_for_tokens,
    compute_tracked_window_benchmark,
    build_strategy_result,
    leader_trade_size,
    leader_trade_usd,
    compute_trade_fee_usdc,
    format_utc_from_epoch,
)


# ---------------------------------------------------------------------------
# Passive simulation engine (no premium, no slippage, random fill drop)
# ---------------------------------------------------------------------------

def run_passive_simulation(
    events: List[TradeEvent],
    strategies: List[Strategy],
    *,
    fill_probability: float = 1.0,
    random_seed: int = 42,
    fee_enabled: bool = True,
    fee_rate: float = SPORTS_FEE_RATE,
    fee_exponent: float = SPORTS_FEE_EXPONENT,
    buy_min_price: float = 0.01,
    buy_max_price: float = 0.99,
    sell_min_price: float = 0.01,
    sell_max_price: float = 0.99,
) -> List[StrategyState]:
    """Like run_simulation but: 0 premium, 0 slippage, random fill drop."""
    rng = random.Random(random_seed)
    states = [StrategyState(s) for s in strategies]
    eff_fee_rate = max(0.0, float(fee_rate))
    eff_fee_exp = max(0.0, float(fee_exponent))

    for event in events:
        if not event.token_id:
            continue
        event_size = leader_trade_size(event)
        event_usd = leader_trade_usd(event)

        for state in states:
            leader_open = state.leader_open_sizes.get(event.token_id, 0.0)

            if event.side == "BUY":
                if event.is_leader_position_event and event_size and event_size > 0:
                    state.leader_open_sizes[event.token_id] = leader_open + event_size

                # In passive mode every event is a signal (no copy_signal filter)
                market_key = event.condition_id or event.token_id
                current_entries = state.buy_counts.get(market_key, 0)
                if current_entries >= state.strategy.max_entries_per_market:
                    state.skipped_entry_limit += 1
                    continue

                if event.price is None or event.price <= 0:
                    state.skipped_missing_value += 1
                    continue
                if event.price < buy_min_price or event.price > buy_max_price:
                    state.skipped_buy_price += 1
                    continue

                # Random fill drop
                if rng.random() >= fill_probability:
                    state.skipped_missing_value += 1  # reuse counter for "dropped"
                    continue

                # Passive: buy at leader's exact price (limit order, 0 premium)
                our_buy_price = event.price

                # Proportional sizing
                if state.strategy.copy_mode == "fixed_usd":
                    our_usd = state.strategy.fixed_usd or 0.0
                else:
                    if event.usd is None or event.usd <= 0:
                        state.skipped_missing_value += 1
                        continue
                    our_usd = event.usd * state.strategy.proportional_pct
                    cap = state.strategy.proportional_cap_usd
                    if cap is not None and cap > 0:
                        our_usd = min(our_usd, cap)

                if our_usd <= 0:
                    state.skipped_missing_value += 1
                    continue

                gross_buy_size = our_usd / our_buy_price
                fee_buy_usdc = (
                    compute_trade_fee_usdc(
                        share_qty=gross_buy_size,
                        price=our_buy_price,
                        fee_rate=eff_fee_rate,
                        fee_exponent=eff_fee_exp,
                    )
                    if fee_enabled and eff_fee_rate > 0
                    else 0.0
                )
                fee_buy_shares = (fee_buy_usdc / our_buy_price) if fee_buy_usdc > 0 and our_buy_price > 0 else 0.0
                our_size = gross_buy_size - fee_buy_shares
                if our_size <= 1e-12:
                    state.skipped_missing_value += 1
                    continue

                pos = state.positions.get(event.token_id)
                if pos is None:
                    pos = Position(size=0.0, cost=0.0, market_key=market_key)
                    state.positions[event.token_id] = pos
                pos.size += our_size
                pos.cost += our_usd
                state.total_buy_cost += our_usd
                state.copied_buys += 1
                state.buy_counts[market_key] = current_entries + 1
                state.our_market_buy_usd[market_key] = state.our_market_buy_usd.get(market_key, 0.0) + our_usd
                state.market_follow_buys[market_key] = state.market_follow_buys.get(market_key, 0) + 1
                state.market_buy_cost[market_key] = state.market_buy_cost.get(market_key, 0.0) + our_usd

            else:  # SELL
                sell_ratio = None
                if event.is_leader_position_event and event_size and event_size > 0:
                    if leader_open > 1e-12:
                        sell_ratio = min(1.0, event_size / leader_open)
                    state.leader_open_sizes[event.token_id] = max(0.0, leader_open - event_size)

                if event.price is None or event.price <= 0:
                    continue
                # Passive: sell at leader's exact price (0 slippage)
                actual_sell_price = event.price
                if actual_sell_price < sell_min_price or actual_sell_price > sell_max_price:
                    continue
                if sell_ratio is None or sell_ratio <= 0:
                    continue

                pos = state.positions.get(event.token_id)
                if pos is None or pos.size <= 1e-12:
                    continue

                sell_size = min(pos.size * sell_ratio, pos.size)
                if sell_size <= 1e-12:
                    continue

                avg_cost = pos.cost / pos.size if pos.size > 0 else 0.0
                fee_sell_usdc = (
                    compute_trade_fee_usdc(
                        share_qty=sell_size,
                        price=actual_sell_price,
                        fee_rate=eff_fee_rate,
                        fee_exponent=eff_fee_exp,
                    )
                    if fee_enabled and eff_fee_rate > 0
                    else 0.0
                )
                realized = sell_size * (actual_sell_price - avg_cost) - fee_sell_usdc
                state.realized_pnl += realized
                rmk = pos.market_key or event.condition_id or event.token_id
                state.market_realized_pnl[rmk] = state.market_realized_pnl.get(rmk, 0.0) + realized

                pos.size -= sell_size
                pos.cost -= avg_cost * sell_size
                state.mirrored_sells += 1
                if pos.size <= 1e-12:
                    state.positions.pop(event.token_id, None)

    return states


# ---------------------------------------------------------------------------
# Strategy grid + main entry
# ---------------------------------------------------------------------------

def generate_passive_strategies(
    cap_options: List[Optional[float]],
    max_entries: int = 999,
) -> List[Strategy]:
    """Proportional 100% with cap sweep, unlimited entries."""
    strategies: List[Strategy] = []
    for cap in cap_options:
        cap_label = f"cap${cap:.0f}" if cap is not None else "nocap"
        name = f"passive|prop100%+{cap_label}|entries{max_entries}|{ONLY_EXIT_MODE}"
        strategies.append(Strategy(
            name=name,
            copy_mode="proportional",
            fixed_usd=None,
            proportional_pct=1.0,
            proportional_cap_usd=cap,
            max_entries_per_market=max_entries,
            exit_mode=ONLY_EXIT_MODE,
        ))
    return strategies


def prepare_raw_events(events: List[TradeEvent]) -> List[TradeEvent]:
    """Sort events chronologically and ensure all BUYs are copy signals."""
    ordered = sorted(events, key=lambda x: (x.ts, x.tx_hash, x.token_id, x.side))
    return ordered


def run_sweep(
    events: List[TradeEvent],
    price_map: Dict[str, PriceInfo],
    strategies: List[Strategy],
    fill_rates: List[float],
    seeds_per_rate: int = 5,
) -> List[Dict[str, Any]]:
    """Run simulation for each (strategy, fill_rate, seed) combo."""
    all_rows: List[Dict[str, Any]] = []
    total_combos = len(strategies) * len(fill_rates) * seeds_per_rate
    done = 0

    for fr in fill_rates:
        seed_results: Dict[str, List[Dict[str, Any]]] = {}  # strategy_name -> list of results
        for seed_idx in range(seeds_per_rate):
            seed = 1000 + seed_idx
            states = run_passive_simulation(
                events, strategies,
                fill_probability=fr,
                random_seed=seed,
            )
            for state in states:
                result = build_strategy_result(state, price_map)
                result["fill_probability"] = fr
                result["random_seed"] = seed
                result["seed_idx"] = seed_idx
                all_rows.append(result)
                done += 1

        if done % 10 == 0 or done == total_combos:
            print(f"  [sweep] {done}/{total_combos} combos done")

    return all_rows


def build_summary(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate results by (strategy, fill_rate) across seeds."""
    groups: Dict[Tuple[str, float], List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r["strategy"], r["fill_probability"])
        groups.setdefault(key, []).append(r)

    summary = []
    for (strat_name, fr), group in sorted(groups.items()):
        rois = [r["roi"] for r in group if r["roi"] is not None]
        pnls = [r["total_pnl"] for r in group if r["total_pnl"] is not None]
        costs = [r["total_buy_cost"] for r in group if r["total_buy_cost"] is not None]
        buys = [r["copied_buys"] for r in group]
        sells = [r["mirrored_sells"] for r in group]

        summary.append({
            "strategy": strat_name,
            "fill_probability": fr,
            "seeds": len(group),
            "avg_roi": round(statistics.mean(rois), 6) if rois else None,
            "std_roi": round(statistics.stdev(rois), 6) if len(rois) > 1 else 0.0,
            "avg_total_pnl": round(statistics.mean(pnls), 2) if pnls else None,
            "avg_total_buy_cost": round(statistics.mean(costs), 2) if costs else None,
            "avg_copied_buys": round(statistics.mean(buys), 1),
            "avg_mirrored_sells": round(statistics.mean(sells), 1),
        })
    return summary


def print_summary_table(summary: List[Dict[str, Any]]) -> None:
    """Print a readable summary table."""
    print("\n" + "=" * 100)
    print(f"{'Strategy':<45s} {'FillRate':>8s} {'AvgROI':>9s} {'StdROI':>8s} {'AvgPnL':>12s} {'AvgCost':>12s} {'Buys':>7s}")
    print("-" * 100)
    for r in summary:
        roi_str = f"{r['avg_roi']*100:+.2f}%" if r['avg_roi'] is not None else "N/A"
        std_str = f"{r['std_roi']*100:.2f}%" if r['std_roi'] is not None else "N/A"
        pnl_str = f"${r['avg_total_pnl']:+,.2f}" if r['avg_total_pnl'] is not None else "N/A"
        cost_str = f"${r['avg_total_buy_cost']:,.2f}" if r['avg_total_buy_cost'] is not None else "N/A"
        print(f"{r['strategy']:<45s} {r['fill_probability']:>7.0%} {roi_str:>9s} {std_str:>8s} {pnl_str:>12s} {cost_str:>12s} {r['avg_copied_buys']:>7.0f}")
    print("=" * 100)


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Passive follow simulation with fill-rate sweep")
    ap.add_argument("--address", required=True, help="Leader address")
    ap.add_argument("--max-activities", type=int, default=300000)
    ap.add_argument("--page-limit", type=int, default=1000)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--price-workers", type=int, default=16)
    ap.add_argument("--seeds", type=int, default=5, help="Random seeds per fill rate")
    ap.add_argument("--fill-rates", type=str, default="0.3,0.4,0.5,0.6,0.7",
                    help="Comma-separated fill probabilities")
    ap.add_argument("--caps", type=str, default="5,10,15,20,50",
                    help="Comma-separated cap USD values (0 = no cap)")
    ap.add_argument("--out-dir", type=str, default="")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    address = args.address.lower().strip()
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    fill_rates = [float(x) for x in args.fill_rates.split(",")]
    cap_values: List[Optional[float]] = []
    for x in args.caps.split(","):
        v = float(x)
        cap_values.append(None if v <= 0 else v)

    print(f"=== Passive Follow Simulation ===")
    print(f"address={address}")
    print(f"fill_rates={fill_rates}, caps={cap_values}, seeds={args.seeds}")

    session = requests.Session()

    # 1. Fetch raw events (no aggregation)
    t0 = time.time()
    raw_events = fetch_activity_events(
        session, address,
        max_activities=args.max_activities,
        page_limit=args.page_limit,
        timeout_s=args.timeout,
    )
    if not raw_events:
        print("ERROR: No events fetched")
        return 1
    print(f"[fetch] {len(raw_events)} events in {time.time()-t0:.1f}s")

    # 2. Prepare events (sort, keep all as signals)
    events = prepare_raw_events(raw_events)
    buy_count = sum(1 for e in events if e.side == "BUY")
    sell_count = sum(1 for e in events if e.side == "SELL")
    buy_usd = sum(e.usd for e in events if e.side == "BUY" and e.usd and e.usd > 0)
    print(f"[events] BUY={buy_count} (${buy_usd:,.0f}), SELL={sell_count}")

    first_ts = events[0].ts if events else None
    last_ts = events[-1].ts if events else None
    print(f"[window] {format_utc_from_epoch(first_ts)} ~ {format_utc_from_epoch(last_ts)}")

    # 3. Benchmark
    benchmark = compute_tracked_window_benchmark(session, address, first_ts=first_ts, last_ts=last_ts)
    leader_pnl = as_float(benchmark.get("actual_window_pnl_delta"))
    print(f"[benchmark] leader window PnL delta: ${leader_pnl:+,.2f}" if leader_pnl else "[benchmark] N/A")

    # 4. Fetch prices
    price_tokens = sorted({e.token_id for e in events if e.side == "BUY" and e.token_id})
    print(f"[price] fetching {len(price_tokens)} tokens...")
    t_p = time.time()
    price_map = fetch_prices_for_tokens(price_tokens, timeout_s=args.timeout, workers=args.price_workers)
    print(f"[price] done in {time.time()-t_p:.1f}s")

    # 5. Generate strategies
    strategies = generate_passive_strategies(cap_values)
    print(f"[strategies] {len(strategies)} strategies × {len(fill_rates)} fill_rates × {args.seeds} seeds = {len(strategies)*len(fill_rates)*args.seeds} combos")

    # 6. Run sweep
    t_sim = time.time()
    all_rows = run_sweep(events, price_map, strategies, fill_rates, seeds_per_rate=args.seeds)
    print(f"[sim] done in {time.time()-t_sim:.1f}s")

    # 7. Build summary
    summary = build_summary(all_rows)
    print_summary_table(summary)

    # 8. Save results
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_addr = f"{address[:8]}_{address[-6:]}"
    json_path = out_dir / f"passive_sim_{short_addr}_{ts_str}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "address": address,
        "mode": "passive_follow",
        "fill_rates": fill_rates,
        "caps": cap_values,
        "seeds_per_rate": args.seeds,
        "events_total": len(events),
        "buy_count": buy_count,
        "buy_usd_total": round(buy_usd, 2),
        "leader_window_pnl_delta": leader_pnl,
        "summary": summary,
        "detail_count": len(all_rows),
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[output] {json_path}")

    # 9. Find breakeven
    print("\n=== Breakeven Analysis ===")
    for cap_label in sorted(set(r["strategy"] for r in summary)):
        cap_rows = [r for r in summary if r["strategy"] == cap_label]
        cap_rows.sort(key=lambda x: x["fill_probability"])
        positive = [r for r in cap_rows if r["avg_roi"] is not None and r["avg_roi"] > 0]
        if positive:
            best = min(positive, key=lambda x: x["fill_probability"])
            print(f"  {cap_label}: breakeven at fill_rate >= {best['fill_probability']:.0%} (ROI={best['avg_roi']*100:+.2f}%)")
        else:
            print(f"  {cap_label}: NO breakeven found (all negative)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
