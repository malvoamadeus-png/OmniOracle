"""
Strategy Grid v2: 同时优化 ROI 和绝对收益，细化金额参数。
排序标准：ROI > 2% 的前提下，按绝对 PnL 排序。
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


def load_events(path):
    events = []
    with gzip.open(str(path), 'rt', encoding='utf-8') as f:
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
        if ev['side'] == 'BUY':
            m['total_buy_usd'] += usd
            m['tokens'][tid]['buy_usd'] += usd
            m['tokens'][tid]['buy_shares'] += size
        elif ev['side'] == 'SELL':
            m['tokens'][tid]['sell_usd'] += usd
            m['tokens'][tid]['sell_shares'] += size
        m['events'].append(ev)
    return markets


def compute_pnl(markets, price_map):
    for cid, m in markets.items():
        realized = settlement = unrealized = 0.0
        for tid, t in m['tokens'].items():
            bs, ss, bu, su = t['buy_shares'], t['sell_shares'], t['buy_usd'], t['sell_usd']
            rem = bs - ss
            if bs > 0 and ss > 0:
                realized += su - ss * (bu / bs)
            if rem > 1e-9 and bs > 0:
                rc = rem * (bu / bs)
                pi = price_map.get(tid)
                if pi and pi.price is not None:
                    v = rem * pi.price
                    if pi.resolved:
                        settlement += v - rc
                    else:
                        unrealized += v - rc
        m['total_pnl'] = realized + settlement + unrealized


def simulate(markets, agg_thr, agg_win_s, skip_n, copy_mode, copy_amt, copy_cap, max_follows):
    total_copy = 0.0
    total_pnl = 0.0
    mkts_followed = 0
    total_trades = 0

    for cid, m in markets.items():
        buys = sorted([e for e in m['events'] if e['side'] == 'BUY'], key=lambda e: e['ts'])
        if not buys:
            continue
        ws = buys[0]['ts']
        wu = 0.0
        tc = 0
        fc = 0
        mc = 0.0

        for ev in buys:
            usd = ev.get('usd') or 0
            if usd <= 0:
                usd = (ev.get('price') or 0) * (ev.get('size') or 0)
            if ev['ts'] - ws <= agg_win_s:
                wu += usd
            else:
                ws = ev['ts']
                wu = usd
            if wu >= agg_thr:
                tc += 1
                if tc > skip_n and fc < max_follows:
                    if copy_mode == 'fixed':
                        c = copy_amt
                    else:
                        c = min(wu * copy_amt, copy_cap)
                    mc += c
                    fc += 1
                ws = ev['ts'] + 1
                wu = 0.0

        if fc > 0:
            mkts_followed += 1
            total_trades += fc
            total_copy += mc
            if m['total_buy_usd'] > 0:
                total_pnl += m.get('total_pnl', 0) * (mc / m['total_buy_usd'])

    roi = total_pnl / total_copy if total_copy > 0 else None
    return {
        'mkts': mkts_followed,
        'trades': total_trades,
        'copy_usd': round(total_copy, 2),
        'pnl': round(total_pnl, 2),
        'roi': round(roi, 6) if roi is not None else None,
    }


def main():
    t0 = time.time()
    cache = SIM_ROOT / 'output' / 'events_cache_0xee613b.jsonl.gz'
    print("Loading...")
    events = load_events(cache)
    markets = build_markets(events)
    print(f"{len(events):,} events, {len(markets):,} markets")

    tokens_need = set()
    for m in markets.values():
        for tid, t in m['tokens'].items():
            if t['buy_shares'] - t['sell_shares'] > 1e-9:
                tokens_need.add(tid)
    print(f"Fetching {len(tokens_need):,} prices...")
    pm = fetch_prices_for_tokens(sorted(tokens_need), timeout_s=30.0, workers=16)
    compute_pnl(markets, pm)
    leader_pnl = sum(m.get('total_pnl', 0) for m in markets.values())
    print(f"Leader PnL: ${leader_pnl:+,.0f}")

    # Full grid
    thresholds = [200, 300, 500, 1000, 2000]
    windows = [15*60, 30*60, 60*60]
    skips = [0, 3, 5, 8, 10, 15]
    copy_cfgs = [
        # (mode, amount, cap, label)
        ('fixed', 10, 10, 'fix$10'),
        ('fixed', 25, 25, 'fix$25'),
        ('fixed', 50, 50, 'fix$50'),
        ('fixed', 75, 75, 'fix$75'),
        ('fixed', 100, 100, 'fix$100'),
        ('fixed', 150, 150, 'fix$150'),
        ('prop', 0.005, 75, 'p0.5%c75'),
        ('prop', 0.01, 75, 'p1%c75'),
        ('prop', 0.01, 150, 'p1%c150'),
        ('prop', 0.02, 75, 'p2%c75'),
        ('prop', 0.02, 150, 'p2%c150'),
        ('prop', 0.05, 75, 'p5%c75'),
        ('prop', 0.05, 150, 'p5%c150'),
    ]
    max_follows_list = [5, 10, 20, 50, 999]

    results = []
    total_combos = len(thresholds) * len(windows) * len(skips) * len(copy_cfgs) * len(max_follows_list)
    print(f"Running {total_combos:,} combos...")
    done = 0

    for thr in thresholds:
        for win in windows:
            for skip in skips:
                for mode, amt, cap, label in copy_cfgs:
                    for mf in max_follows_list:
                        r = simulate(markets, thr, win, skip, mode, amt, cap, mf)
                        r['thr'] = thr
                        r['win'] = win // 60
                        r['skip'] = skip
                        r['copy'] = label
                        r['maxf'] = mf
                        results.append(r)
                        done += 1
                        if done % 5000 == 0:
                            print(f"  {done}/{total_combos}...")

    # Sort: ROI >= 2% first, then by absolute PnL descending
    viable = [r for r in results if r['roi'] is not None and r['roi'] >= 0.02]
    viable.sort(key=lambda x: x['pnl'], reverse=True)

    # Also get pure ROI top
    by_roi = sorted([r for r in results if r['roi'] is not None], key=lambda x: x['roi'], reverse=True)

    print(f"\n{'='*130}")
    print(f"TOP 30 BY ABSOLUTE PNL (ROI >= 2%)")
    hdr = f"{'Thr':>5} {'Win':>4} {'Skip':>5} {'Copy':>10} {'MaxF':>5} {'Mkts':>5} {'Trades':>7} {'CopyUSD':>12} {'PnL':>10} {'ROI':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in viable[:30]:
        roi_s = f"{r['roi']*100:+.1f}%"
        print(f"${r['thr']:>4} {r['win']:>3}m {r['skip']:>5} {r['copy']:>10} {r['maxf']:>5} {r['mkts']:>5} {r['trades']:>7} ${r['copy_usd']:>11,.0f} ${r['pnl']:>+9,.0f} {roi_s:>7}")

    print(f"\nTOP 30 BY ROI")
    print(hdr)
    print("-" * len(hdr))
    for r in by_roi[:30]:
        roi_s = f"{r['roi']*100:+.1f}%"
        print(f"${r['thr']:>4} {r['win']:>3}m {r['skip']:>5} {r['copy']:>10} {r['maxf']:>5} {r['mkts']:>5} {r['trades']:>7} ${r['copy_usd']:>11,.0f} ${r['pnl']:>+9,.0f} {roi_s:>7}")

    # Sweet spot: ROI >= 3% AND PnL >= $3000
    sweet = [r for r in results if r['roi'] is not None and r['roi'] >= 0.03 and r['pnl'] >= 3000]
    sweet.sort(key=lambda x: (x['pnl'], x['roi']), reverse=True)
    print(f"\nSWEET SPOT (ROI >= 3% AND PnL >= $3,000)")
    print(hdr)
    print("-" * len(hdr))
    for r in sweet[:30]:
        roi_s = f"{r['roi']*100:+.1f}%"
        print(f"${r['thr']:>4} {r['win']:>3}m {r['skip']:>5} {r['copy']:>10} {r['maxf']:>5} {r['mkts']:>5} {r['trades']:>7} ${r['copy_usd']:>11,.0f} ${r['pnl']:>+9,.0f} {roi_s:>7}")

    # Save
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = SIM_ROOT / 'output' / f'strategy_grid_v2_0xee613b_{ts}.json'
    payload = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'leader_pnl': round(leader_pnl, 2),
        'total_combos': total_combos,
        'top30_by_pnl_roi2pct': viable[:30],
        'top30_by_roi': by_roi[:30],
        'sweet_spot': sweet[:30],
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    print(f"\n[output] {out}")
    print(f"[done] {time.time()-t0:.0f}s")


if __name__ == '__main__':
    main()
