"""
Delayed-Follow Strategy Grid Search
在缓存的 800K 事件上，sweep 所有参数组合，找最优方案。

参数空间：
- agg_threshold: 聚合触发门槛 ($100, $200, $300, $500, $1000, $2000)
- agg_window: 聚合时间窗口 (15min, 30min, 60min, 120min)
- skip_n: 前 N 次触发跳过 (0, 3, 5, 8, 10, 15)
- copy_mode: fixed_usd / proportional
- copy_amount: 固定金额 or 比例
- max_follows: 触发后最多跟几次 (5, 10, 20, 999)
"""
import sys, gzip, json, statistics, time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for p in [str(SIM_PARENT), str(SIM_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from main import PriceInfo, fetch_prices_for_tokens


def load_events(cache_path):
    events = []
    with gzip.open(str(cache_path), 'rt', encoding='utf-8') as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events


def build_markets(events):
    markets = defaultdict(lambda: {
        'events': [], 'total_buy_usd': 0.0,
        'tokens': defaultdict(lambda: {'buy_usd': 0, 'buy_shares': 0, 'sell_usd': 0, 'sell_shares': 0}),
    })
    for ev in events:
        cid = ev.get('condition_id') or ev.get('token_id', '')
        tid = ev.get('token_id', '')
        if not cid or not tid:
            continue
        m = markets[cid]
        usd = ev.get('usd') or 0
        if usd <= 0:
            usd = (ev.get('price') or 0) * (ev.get('size') or 0)
        size = ev.get('size') or 0
        price = ev.get('price') or 0
        if ev['side'] == 'BUY':
            m['total_buy_usd'] += usd
            m['tokens'][tid]['buy_usd'] += usd
            m['tokens'][tid]['buy_shares'] += size
        elif ev['side'] == 'SELL':
            m['tokens'][tid]['sell_usd'] += usd
            m['tokens'][tid]['sell_shares'] += size
        m['events'].append(ev)
    return markets


def compute_market_pnl(markets, price_map):
    """Compute realized + settlement/unrealized PnL per market."""
    for cid, m in markets.items():
        realized = 0.0
        settlement = 0.0
        unrealized = 0.0
        for tid, t in m['tokens'].items():
            buy_s = t['buy_shares']
            sell_s = t['sell_shares']
            buy_u = t['buy_usd']
            sell_u = t['sell_usd']
            remaining = buy_s - sell_s
            if buy_s > 0 and sell_s > 0:
                avg_cost = buy_u / buy_s
                realized += sell_u - sell_s * avg_cost
            if remaining > 1e-9 and buy_s > 0:
                avg_cost = buy_u / buy_s
                rem_cost = remaining * avg_cost
                pi = price_map.get(tid)
                if pi and pi.price is not None:
                    val = remaining * pi.price
                    if pi.resolved:
                        settlement += val - rem_cost
                    else:
                        unrealized += val - rem_cost
        m['total_pnl'] = realized + settlement + unrealized
        m['realized_pnl'] = realized
        m['settlement_pnl'] = settlement
        m['unrealized_pnl'] = unrealized


def simulate_strategy(markets, agg_threshold, agg_window_s, skip_n,
                      copy_mode, copy_amount, max_follows):
    """
    Simulate delayed-follow strategy on all markets.
    Returns aggregate stats.
    """
    total_copy_usd = 0.0
    total_pnl_captured = 0.0
    markets_followed = 0
    markets_skipped = 0
    total_follow_trades = 0

    for cid, m in markets.items():
        buys = sorted([e for e in m['events'] if e['side'] == 'BUY'], key=lambda e: e['ts'])
        if not buys:
            continue

        # Simulate aggregation triggers
        window_start = buys[0]['ts']
        window_usd = 0.0
        trigger_count = 0
        follow_count = 0
        market_copy_usd = 0.0

        for ev in buys:
            usd = ev.get('usd') or 0
            if usd <= 0:
                usd = (ev.get('price') or 0) * (ev.get('size') or 0)

            if ev['ts'] - window_start <= agg_window_s:
                window_usd += usd
            else:
                window_start = ev['ts']
                window_usd = usd

            if window_usd >= agg_threshold:
                trigger_count += 1

                if trigger_count > skip_n and follow_count < max_follows:
                    # Execute copy
                    if copy_mode == 'fixed':
                        this_copy = copy_amount
                    else:  # proportional
                        this_copy = min(window_usd * copy_amount, 75.0)  # cap $75
                    market_copy_usd += this_copy
                    follow_count += 1

                window_start = ev['ts'] + 1
                window_usd = 0.0

        if follow_count > 0:
            markets_followed += 1
            total_follow_trades += follow_count
            total_copy_usd += market_copy_usd
            # PnL attribution: proportional to our copy vs leader's total buy
            if m['total_buy_usd'] > 0:
                our_share = market_copy_usd / m['total_buy_usd']
                total_pnl_captured += m.get('total_pnl', 0) * our_share
        else:
            markets_skipped += 1

    roi = total_pnl_captured / total_copy_usd if total_copy_usd > 0 else None
    return {
        'markets_followed': markets_followed,
        'markets_skipped': markets_skipped,
        'total_copy_usd': round(total_copy_usd, 2),
        'total_pnl': round(total_pnl_captured, 2),
        'roi': round(roi, 6) if roi is not None else None,
        'avg_follow_trades': round(total_follow_trades / markets_followed, 1) if markets_followed > 0 else 0,
    }


def main():
    t0 = time.time()
    cache_path = SIM_ROOT / 'output' / 'events_cache_0xee613b.jsonl.gz'
    if not cache_path.exists():
        print(f"Cache not found: {cache_path}")
        return 1

    print("Loading events...")
    events = load_events(cache_path)
    print(f"Loaded {len(events):,} events in {time.time()-t0:.1f}s")

    print("Building markets...")
    markets = build_markets(events)
    print(f"{len(markets):,} markets")

    # Fetch prices for tokens with remaining shares
    tokens_need = set()
    for m in markets.values():
        for tid, t in m['tokens'].items():
            if t['buy_shares'] - t['sell_shares'] > 1e-9:
                tokens_need.add(tid)
    print(f"Fetching prices for {len(tokens_need):,} tokens...")
    t_p = time.time()
    price_map = fetch_prices_for_tokens(sorted(tokens_need), timeout_s=30.0, workers=16)
    print(f"Prices done in {time.time()-t_p:.1f}s")

    compute_market_pnl(markets, price_map)
    total_leader_pnl = sum(m.get('total_pnl', 0) for m in markets.values())
    print(f"Leader total PnL: ${total_leader_pnl:+,.2f}")

    # Parameter grid
    agg_thresholds = [100, 200, 300, 500, 1000, 2000]
    agg_windows = [15*60, 30*60, 60*60, 120*60]
    skip_ns = [0, 3, 5, 8, 10, 15]
    copy_configs = [
        ('fixed', 10),
        ('fixed', 25),
        ('fixed', 50),
        ('fixed', 75),
        ('prop', 0.005),   # 0.5%
        ('prop', 0.01),    # 1%
        ('prop', 0.02),    # 2%
        ('prop', 0.05),    # 5%
    ]
    max_follows_list = [5, 10, 20, 999]

    # Full grid is huge. Do a smart 2-phase search:
    # Phase 1: Fix copy_mode=prop/0.5%/max999, sweep threshold x window x skip
    print("\n=== Phase 1: Sweep threshold x window x skip (prop 0.5%, cap $75, unlimited follows) ===")
    phase1_results = []
    for thr in agg_thresholds:
        for win in agg_windows:
            for skip in skip_ns:
                r = simulate_strategy(markets, thr, win, skip, 'prop', 0.005, 999)
                r['threshold'] = thr
                r['window_min'] = win // 60
                r['skip_n'] = skip
                phase1_results.append(r)

    # Sort by ROI
    phase1_results.sort(key=lambda x: x['roi'] if x['roi'] is not None else -999, reverse=True)

    print(f"\n{'Threshold':>9} {'Window':>7} {'Skip':>5} {'Followed':>9} {'CopyUSD':>12} {'PnL':>12} {'ROI':>8} {'AvgTrades':>10}")
    print("-" * 85)
    for r in phase1_results[:30]:
        roi_str = f"{r['roi']*100:+.2f}%" if r['roi'] is not None else "N/A"
        print(f"${r['threshold']:>8} {r['window_min']:>5}min {r['skip_n']:>5} {r['markets_followed']:>9} ${r['total_copy_usd']:>11,.0f} ${r['total_pnl']:>+11,.0f} {roi_str:>8} {r['avg_follow_trades']:>10.1f}")

    # Phase 2: Take top 5 threshold/window/skip combos, sweep copy configs + max_follows
    print("\n=== Phase 2: Sweep copy config + max_follows on top combos ===")
    top5_combos = []
    seen = set()
    for r in phase1_results:
        key = (r['threshold'], r['window_min'], r['skip_n'])
        if key not in seen and r['roi'] is not None and r['roi'] > 0:
            seen.add(key)
            top5_combos.append(key)
            if len(top5_combos) >= 5:
                break

    phase2_results = []
    for thr, win_min, skip in top5_combos:
        for mode, amount in copy_configs:
            for mf in max_follows_list:
                r = simulate_strategy(markets, thr, win_min * 60, skip, mode, amount, mf)
                label = f"fixed${amount}" if mode == 'fixed' else f"prop{amount*100:.1f}%"
                r['combo'] = f"${thr}/{win_min}m/skip{skip}"
                r['copy_label'] = label
                r['max_follows'] = mf
                phase2_results.append(r)

    phase2_results.sort(key=lambda x: x['roi'] if x['roi'] is not None else -999, reverse=True)

    print(f"\n{'Combo':<22} {'Copy':>12} {'MaxF':>5} {'Followed':>8} {'CopyUSD':>12} {'PnL':>12} {'ROI':>8}")
    print("-" * 90)
    for r in phase2_results[:30]:
        roi_str = f"{r['roi']*100:+.2f}%" if r['roi'] is not None else "N/A"
        print(f"{r['combo']:<22} {r['copy_label']:>12} {r['max_follows']:>5} {r['markets_followed']:>8} ${r['total_copy_usd']:>11,.0f} ${r['total_pnl']:>+11,.0f} {roi_str:>8}")

    # Save
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = SIM_ROOT / 'output' / f'strategy_grid_0xee613b_{ts}.json'
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'leader_pnl': round(total_leader_pnl, 2),
        'events': len(events),
        'markets': len(markets),
        'phase1_top30': phase1_results[:30],
        'phase2_top30': phase2_results[:30],
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    print(f"\n[output] {out_path}")
    print(f"[done] {time.time()-t0:.1f}s")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
