"""
Deep Investigation v2 — 从 800K 笔 activity 自算每个市场的 cost/PnL，
分析大仓位 vs 小仓位的交易特征差异。

用法:
    python deep_investigate.py --address 0xee613b3fc183ee44f9da9c05f53e2da107e3debf
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for _path in (SIM_ROOT, SIM_PARENT):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

import requests

from main import (
    TradeEvent,
    PriceInfo,
    fetch_activity_events,
    fetch_prices_for_tokens,
    as_float,
)
from polymarket_public_api import USER_PNL_METRICS_FIDELITY, USER_PNL_METRICS_INTERVAL, fetch_user_pnl_series


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Phase 1: Build per-market positions from raw activity events
# ---------------------------------------------------------------------------

def build_market_positions(events: List[TradeEvent]) -> Dict[str, Dict[str, Any]]:
    """
    From raw events, compute per-(condition_id, token_id) position:
    cost basis, shares held, buy/sell counts, trade details.
    Then group by condition_id to get per-market view.
    """
    # Per-token accumulation
    tokens: Dict[str, Dict[str, Any]] = {}  # key = token_id

    for ev in events:
        if not ev.token_id:
            continue
        tid = ev.token_id
        if tid not in tokens:
            tokens[tid] = {
                "token_id": tid,
                "condition_id": ev.condition_id or "",
                "market_slug": ev.market_slug or "",
                "total_buy_usd": 0.0,
                "total_buy_shares": 0.0,
                "total_sell_usd": 0.0,
                "total_sell_shares": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "buy_trades": [],  # (ts, price, usd, size)
                "sell_trades": [],
                "first_ts": ev.ts,
                "last_ts": ev.ts,
            }
        t = tokens[tid]
        if not t["condition_id"] and ev.condition_id:
            t["condition_id"] = ev.condition_id
        if not t["market_slug"] and ev.market_slug:
            t["market_slug"] = ev.market_slug
        t["last_ts"] = max(t["last_ts"], ev.ts)
        t["first_ts"] = min(t["first_ts"], ev.ts)

        price = ev.price or 0.0
        size = ev.size or 0.0
        usd = ev.usd or (price * size if price > 0 and size > 0 else 0.0)

        if ev.side == "BUY":
            t["total_buy_usd"] += usd
            t["total_buy_shares"] += size
            t["buy_count"] += 1
            t["buy_trades"].append((ev.ts, price, usd, size))
        elif ev.side == "SELL":
            t["total_sell_usd"] += usd
            t["total_sell_shares"] += size
            t["sell_count"] += 1
            t["sell_trades"].append((ev.ts, price, usd, size))

    # Group tokens by condition_id → market
    markets: Dict[str, Dict[str, Any]] = {}
    for tid, t in tokens.items():
        cid = t["condition_id"] or tid  # fallback to token_id if no condition_id
        if cid not in markets:
            markets[cid] = {
                "condition_id": cid,
                "slug": t["market_slug"],
                "tokens": {},
                "total_buy_usd": 0.0,
                "total_sell_usd": 0.0,
                "net_cost": 0.0,  # buy - sell
                "shares_held": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "first_ts": t["first_ts"],
                "last_ts": t["last_ts"],
            }
        m = markets[cid]
        if not m["slug"] and t["market_slug"]:
            m["slug"] = t["market_slug"]
        m["tokens"][tid] = t
        m["total_buy_usd"] += t["total_buy_usd"]
        m["total_sell_usd"] += t["total_sell_usd"]
        m["net_cost"] += t["total_buy_usd"] - t["total_sell_usd"]
        m["shares_held"] += t["total_buy_shares"] - t["total_sell_shares"]
        m["buy_count"] += t["buy_count"]
        m["sell_count"] += t["sell_count"]
        m["first_ts"] = min(m["first_ts"], t["first_ts"])
        m["last_ts"] = max(m["last_ts"], t["last_ts"])

    return markets


def compute_market_pnl(
    markets: Dict[str, Dict[str, Any]],
    price_map: Dict[str, PriceInfo],
) -> None:
    """Compute PnL for each market using resolution/midpoint prices."""
    for cid, m in markets.items():
        realized_pnl = 0.0  # from sells: sell_usd - proportional_cost
        unrealized_pnl = 0.0  # from remaining shares × current_price - remaining_cost
        settlement_pnl = 0.0
        resolved_count = 0
        unresolved_count = 0

        for tid, t in m["tokens"].items():
            buy_usd = t["total_buy_usd"]
            sell_usd = t["total_sell_usd"]
            buy_shares = t["total_buy_shares"]
            sell_shares = t["total_sell_shares"]
            remaining_shares = buy_shares - sell_shares

            # Realized from sells
            if buy_shares > 0 and sell_shares > 0:
                avg_cost_per_share = buy_usd / buy_shares
                realized_pnl += sell_usd - (sell_shares * avg_cost_per_share)

            # Unrealized from remaining shares
            if remaining_shares > 1e-9:
                avg_cost_per_share = buy_usd / buy_shares if buy_shares > 0 else 0
                remaining_cost = remaining_shares * avg_cost_per_share
                pi = price_map.get(tid)
                if pi and pi.price is not None:
                    current_value = remaining_shares * pi.price
                    if pi.resolved:
                        settlement_pnl += current_value - remaining_cost
                        resolved_count += 1
                    else:
                        unrealized_pnl += current_value - remaining_cost
                        unresolved_count += 1
                else:
                    unresolved_count += 1

        m["realized_pnl"] = round(realized_pnl, 4)
        m["settlement_pnl"] = round(settlement_pnl, 4)
        m["unrealized_pnl"] = round(unrealized_pnl, 4)
        m["total_pnl"] = round(realized_pnl + settlement_pnl + unrealized_pnl, 4)
        m["resolved_count"] = resolved_count
        m["unresolved_count"] = unresolved_count


# ---------------------------------------------------------------------------
# Phase 2: Trade feature extraction per market
# ---------------------------------------------------------------------------

def extract_trade_features(m: Dict[str, Any]) -> Dict[str, Any]:
    """Extract trading pattern features for a single market."""
    all_buy_trades = []
    all_sell_trades = []
    for t in m["tokens"].values():
        all_buy_trades.extend(t["buy_trades"])
        all_sell_trades.extend(t["sell_trades"])

    buy_usds = [tr[2] for tr in all_buy_trades if tr[2] > 0]
    buy_prices = [tr[1] for tr in all_buy_trades if tr[1] and tr[1] > 0]
    buy_timestamps = [tr[0] for tr in all_buy_trades]

    total_trades = m["buy_count"] + m["sell_count"]
    duration_h = (m["last_ts"] - m["first_ts"]) / 3600.0 if m["last_ts"] > m["first_ts"] else 0.0

    avg_buy_usd = statistics.mean(buy_usds) if buy_usds else 0
    max_buy_usd = max(buy_usds) if buy_usds else 0
    median_buy_usd = statistics.median(buy_usds) if buy_usds else 0

    # Price stats
    avg_buy_price = statistics.mean(buy_prices) if buy_prices else 0
    median_buy_price = statistics.median(buy_prices) if buy_prices else 0

    # Buildup pattern
    n_buys = m["buy_count"]
    if n_buys <= 3 and avg_buy_usd > 1000:
        pattern = "single_large"
    elif n_buys >= 10 and avg_buy_usd < 500 and duration_h > 1:
        pattern = "gradual_accumulation"
    elif n_buys >= 10 and avg_buy_usd < 500 and duration_h <= 1:
        pattern = "burst_accumulation"
    elif n_buys >= 5 and avg_buy_usd >= 500:
        pattern = "large_accumulation"
    else:
        pattern = "mixed"

    # Token count (how many different tokens = how many sides)
    token_count = len(m["tokens"])
    has_both_sides = token_count >= 2

    # Per-token direction analysis
    token_costs = []
    for tid, t in m["tokens"].items():
        token_costs.append((tid, t["total_buy_usd"] - t["total_sell_usd"]))
    token_costs.sort(key=lambda x: -x[1])
    dominant_cost = token_costs[0][1] if token_costs else 0
    secondary_cost = token_costs[1][1] if len(token_costs) > 1 else 0
    direction_ratio = dominant_cost / (dominant_cost + abs(secondary_cost)) if (dominant_cost + abs(secondary_cost)) > 0 else 1.0

    freq = n_buys / max(duration_h, 0.01)

    return {
        "total_trades": total_trades,
        "buy_count": n_buys,
        "sell_count": m["sell_count"],
        "duration_hours": round(duration_h, 2),
        "avg_buy_usd": round(avg_buy_usd, 2),
        "median_buy_usd": round(median_buy_usd, 2),
        "max_buy_usd": round(max_buy_usd, 2),
        "avg_buy_price": round(avg_buy_price, 4),
        "median_buy_price": round(median_buy_price, 4),
        "buy_frequency_per_hour": round(freq, 2),
        "buildup_pattern": pattern,
        "token_count": token_count,
        "has_both_sides": has_both_sides,
        "direction_ratio": round(direction_ratio, 4),
    }


# ---------------------------------------------------------------------------
# Phase 3: Classification + comparison
# ---------------------------------------------------------------------------

def classify_and_compare(
    markets: Dict[str, Dict[str, Any]],
    threshold: float = 10000.0,
) -> Dict[str, Any]:
    """Classify by total_buy_usd, compare trade features."""
    big, small = [], []
    for m in markets.values():
        feat = extract_trade_features(m)
        entry = {
            "condition_id": m["condition_id"],
            "slug": m["slug"],
            "total_buy_usd": round(m["total_buy_usd"], 2),
            "total_sell_usd": round(m["total_sell_usd"], 2),
            "net_cost": round(m["net_cost"], 2),
            "total_pnl": m.get("total_pnl", 0),
            "realized_pnl": m.get("realized_pnl", 0),
            "settlement_pnl": m.get("settlement_pnl", 0),
            "unrealized_pnl": m.get("unrealized_pnl", 0),
            "features": feat,
        }
        if m["total_buy_usd"] >= threshold:
            big.append(entry)
        else:
            small.append(entry)

    def _group_stats(group: List[Dict], label: str) -> Dict[str, Any]:
        if not group:
            return {"count": 0, "label": label}
        pnls = [g["total_pnl"] for g in group]
        costs = [g["total_buy_usd"] for g in group]
        feats = [g["features"] for g in group]
        total_pnl = sum(pnls)
        total_cost = sum(costs)
        win = sum(1 for p in pnls if p > 0)
        loss = sum(1 for p in pnls if p < 0)

        def _avg(key):
            vals = [f[key] for f in feats if f.get(key) is not None and f[key] > 0]
            return round(statistics.mean(vals), 2) if vals else 0

        def _median(key):
            vals = [f[key] for f in feats if f.get(key) is not None and f[key] > 0]
            return round(statistics.median(vals), 2) if vals else 0

        patterns = defaultdict(int)
        for f in feats:
            patterns[f.get("buildup_pattern", "?")] += 1
        both_sides = sum(1 for f in feats if f.get("has_both_sides"))

        return {
            "label": label,
            "count": len(group),
            "total_pnl": round(total_pnl, 2),
            "total_cost": round(total_cost, 2),
            "roi": round(total_pnl / total_cost, 6) if total_cost > 0 else None,
            "win": win, "loss": loss,
            "win_rate": round(win / len(group), 4) if group else 0,
            "avg_pnl": round(statistics.mean(pnls), 2),
            "median_pnl": round(statistics.median(pnls), 2),
            "avg_buy_count": _avg("buy_count"),
            "median_buy_count": _median("buy_count"),
            "avg_buy_usd_per_trade": _avg("avg_buy_usd"),
            "median_buy_usd_per_trade": _median("median_buy_usd"),
            "avg_max_buy_usd": _avg("max_buy_usd"),
            "avg_duration_hours": _avg("duration_hours"),
            "median_duration_hours": _median("duration_hours"),
            "avg_buy_price": _avg("avg_buy_price"),
            "avg_frequency": _avg("buy_frequency_per_hour"),
            "both_sides_count": both_sides,
            "both_sides_pct": round(both_sides / len(group), 4) if group else 0,
            "avg_direction_ratio": _avg("direction_ratio"),
            "patterns": dict(patterns),
        }

    return {
        "threshold": threshold,
        "big": _group_stats(big, "big"),
        "small": _group_stats(small, "small"),
    }


def concentration_analysis(markets: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    ranked = sorted(markets.values(), key=lambda m: m.get("total_pnl", 0), reverse=True)
    total_pnl = sum(m.get("total_pnl", 0) for m in ranked)
    pos_pnl = sum(m.get("total_pnl", 0) for m in ranked if m.get("total_pnl", 0) > 0)

    def top_n(n):
        return sum(m.get("total_pnl", 0) for m in ranked[:n] if m.get("total_pnl", 0) > 0)

    return {
        "total_markets": len(ranked),
        "total_pnl": round(total_pnl, 2),
        "positive_markets": sum(1 for m in ranked if m.get("total_pnl", 0) > 0),
        "negative_markets": sum(1 for m in ranked if m.get("total_pnl", 0) < 0),
        "top5_pnl": round(top_n(5), 2),
        "top10_pnl": round(top_n(10), 2),
        "top20_pnl": round(top_n(20), 2),
        "top50_pnl": round(top_n(50), 2),
        "top5_share": round(top_n(5) / pos_pnl, 4) if pos_pnl > 0 else 0,
        "top10_share": round(top_n(10) / pos_pnl, 4) if pos_pnl > 0 else 0,
        "top20_share": round(top_n(20) / pos_pnl, 4) if pos_pnl > 0 else 0,
        "top50_share": round(top_n(50) / pos_pnl, 4) if pos_pnl > 0 else 0,
    }


def cumulative_trigger_backtest(markets: Dict[str, Dict[str, Any]], thresholds: List[float]) -> List[Dict[str, Any]]:
    results = []
    for thr in thresholds:
        triggered = [m for m in markets.values() if m["total_buy_usd"] >= thr]
        pnl = sum(m.get("total_pnl", 0) for m in triggered)
        cost = sum(m["total_buy_usd"] for m in triggered)
        results.append({
            "threshold": thr,
            "markets": len(triggered),
            "pnl": round(pnl, 2),
            "cost": round(cost, 2),
            "roi": round(pnl / cost, 6) if cost > 0 else None,
        })
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_summary(
    conc: Dict, classification: Dict, top_markets: List[Dict],
    backtests: List[Dict], total_events: int,
) -> None:
    print("\n" + "=" * 120)
    print(f"EVENTS: {total_events:,} | MARKETS: {conc['total_markets']:,} ({conc['positive_markets']} win, {conc['negative_markets']} loss)")
    print(f"TOTAL PNL: ${conc['total_pnl']:+,.2f}")
    print(f"CONCENTRATION: top5={conc['top5_share']:.0%} top10={conc['top10_share']:.0%} top20={conc['top20_share']:.0%} top50={conc['top50_share']:.0%}")

    print(f"\nTOP 20 MARKETS BY PNL")
    hdr = f"{'#':>3} {'Market':<45} {'PnL':>12} {'BuyCost':>12} {'Buys':>6} {'AvgBuy$':>9} {'MaxBuy$':>9} {'Hours':>7} {'Pattern':<22} {'BothSides':>9} {'DirRatio':>8}"
    print(hdr)
    print("-" * len(hdr))
    for i, m in enumerate(top_markets[:20], 1):
        f = m["features"]
        print(
            f"{i:>3} {m['slug'][:45]:<45} ${m['total_pnl']:>+11,.2f} ${m['total_buy_usd']:>11,.2f} "
            f"{f['buy_count']:>6} ${f['avg_buy_usd']:>8,.1f} ${f['max_buy_usd']:>8,.1f} "
            f"{f['duration_hours']:>7.1f} {f['buildup_pattern']:<22} "
            f"{'Y' if f['has_both_sides'] else 'N':>9} {f['direction_ratio']:>8.2f}"
        )

    print(f"\nBIG vs SMALL (threshold=${classification['threshold']:,.0f} total_buy_usd)")
    for key in ["big", "small"]:
        s = classification[key]
        roi_str = f"{s['roi']*100:+.2f}%" if s.get('roi') is not None else "N/A"
        print(f"  {s['label'].upper():>5}: {s['count']:>6} mkts | PnL=${s['total_pnl']:>+12,.2f} | ROI={roi_str:>8} | WinRate={s['win_rate']:.0%}")
        print(f"         avg_buys={s['avg_buy_count']:.0f} med_buys={s['median_buy_count']:.0f} | avg$/trade={s['avg_buy_usd_per_trade']:.1f} | avg_hours={s['avg_duration_hours']:.1f} | avg_price={s['avg_buy_price']:.3f}")
        print(f"         both_sides={s['both_sides_pct']:.0%} | dir_ratio={s['avg_direction_ratio']:.2f} | patterns={s['patterns']}")

    print(f"\nCUMULATIVE TRIGGER BACKTEST")
    for bt in backtests:
        roi_str = f"{bt['roi']*100:+.2f}%" if bt.get('roi') is not None else "N/A"
        print(f"  >=${bt['threshold']:>8,.0f}: {bt['markets']:>5} mkts | PnL=${bt['pnl']:>+12,.2f} | ROI={roi_str}")
    print("=" * 120)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Deep investigation v2: activity-based PnL + trade features")
    ap.add_argument("--address", required=True)
    ap.add_argument("--max-activities", type=int, default=800000)
    ap.add_argument("--page-limit", type=int, default=1000)
    ap.add_argument("--conviction-threshold", type=float, default=10000.0)
    ap.add_argument("--top-markets", type=int, default=20)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--price-workers", type=int, default=16)
    ap.add_argument("--out-dir", type=str, default="")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    address = args.address.lower().strip()
    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== Deep Investigation v2: {address[:10]}...{address[-6:]} ===")
    print(f"max_activities={args.max_activities:,}")
    session = requests.Session()
    t0 = time.time()

    # Phase 1: Fetch events
    events = fetch_activity_events(
        session, address,
        max_activities=args.max_activities,
        page_limit=args.page_limit,
        timeout_s=args.timeout,
    )
    if not events:
        print("ERROR: No events fetched")
        return 1
    buy_count = sum(1 for e in events if e.side == "BUY")
    sell_count = sum(1 for e in events if e.side == "SELL")
    print(f"[events] {len(events):,} total (BUY={buy_count:,}, SELL={sell_count:,}), elapsed={time.time()-t0:.1f}s")

    # Build per-market positions from events
    markets = build_market_positions(events)
    print(f"[markets] {len(markets):,} unique markets")

    # Fetch prices for all tokens with remaining shares
    tokens_needing_price = set()
    for m in markets.values():
        for tid, t in m["tokens"].items():
            remaining = t["total_buy_shares"] - t["total_sell_shares"]
            if remaining > 1e-9:
                tokens_needing_price.add(tid)
    print(f"[price] fetching prices for {len(tokens_needing_price):,} tokens with open positions...")
    t_price = time.time()
    price_map = fetch_prices_for_tokens(
        sorted(tokens_needing_price),
        timeout_s=args.timeout,
        workers=args.price_workers,
    )
    print(f"[price] done in {time.time()-t_price:.1f}s")

    # Compute PnL
    compute_market_pnl(markets, price_map)
    total_pnl = sum(m.get("total_pnl", 0) for m in markets.values())
    print(f"[pnl] computed: ${total_pnl:+,.2f}")

    # Verify
    try:
        series = fetch_user_pnl_series(
            session,
            address,
            interval=USER_PNL_METRICS_INTERVAL,
            fidelity=USER_PNL_METRICS_FIDELITY,
        )
        if series:
            latest = float(series[-1][1])
            print(f"[verify] user-pnl=${latest:+,.2f}, activity-based=${total_pnl:+,.2f}, gap=${latest-total_pnl:+,.2f}")
    except Exception as e:
        print(f"[verify] failed: {e}")

    # Phase 2: Extract features for ALL markets
    print("[features] extracting trade features for all markets...")
    top_entries = []
    for cid, m in markets.items():
        feat = extract_trade_features(m)
        top_entries.append({
            "condition_id": cid,
            "slug": m["slug"],
            "total_buy_usd": round(m["total_buy_usd"], 2),
            "net_cost": round(m["net_cost"], 2),
            "total_pnl": m.get("total_pnl", 0),
            "features": feat,
        })
    top_entries.sort(key=lambda x: x["total_pnl"], reverse=True)

    # Phase 3: Classification
    conc = concentration_analysis(markets)
    classification = classify_and_compare(markets, threshold=args.conviction_threshold)
    backtests = cumulative_trigger_backtest(
        markets, [1000, 2000, 5000, 10000, 20000, 50000, 100000],
    )

    # Print
    print_summary(conc, classification, top_entries, backtests, len(events))

    # Save
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short = f"{address[:8]}_{address[-6:]}"
    json_path = out_dir / f"deep_investigate_{short}_{ts_str}.json"
    payload = {
        "generated_at": _now_iso(),
        "address": address,
        "max_activities": args.max_activities,
        "events_fetched": len(events),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "markets_count": len(markets),
        "total_pnl": round(total_pnl, 2),
        "concentration": conc,
        "classification": {
            "threshold": classification["threshold"],
            "big": classification["big"],
            "small": classification["small"],
        },
        "cumulative_trigger_backtests": backtests,
        "top_markets": [e for e in top_entries[:50]],
        "bottom_markets": [e for e in top_entries[-20:]],
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n[output] {json_path}")
    print(f"[done] total elapsed={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
