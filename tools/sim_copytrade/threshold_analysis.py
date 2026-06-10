"""Quick analysis: threshold comparison + hedging impact."""
import sys, gzip, json, statistics
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

events = []
with gzip.open('output/events_cache_0xee613b.jsonl.gz', 'rt', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            events.append(json.loads(line))

markets = defaultdict(lambda: {'events': [], 'total_buy_usd': 0, 'slug': ''})
for ev in events:
    cid = ev.get('condition_id') or ev.get('token_id', '')
    if not cid:
        continue
    m = markets[cid]
    if not m['slug'] and ev.get('market_slug'):
        m['slug'] = ev['market_slug']
    usd = ev.get('usd') or 0
    if usd <= 0:
        usd = (ev.get('price') or 0) * (ev.get('size') or 0)
    if ev['side'] == 'BUY':
        m['total_buy_usd'] += usd
    m['events'].append(ev)

print(f"Loaded {len(events):,} events, {len(markets):,} markets")

def simulate_threshold(markets, min_usd, window_s):
    """Per-TOKEN window aggregation (not per-market)."""
    triggered = {}
    for cid, m in markets.items():
        buys = sorted([e for e in m['events'] if e['side'] == 'BUY'], key=lambda e: e['ts'])
        if not buys:
            continue
        token_windows = defaultdict(lambda: {'start': 0, 'usd': 0})
        trigger_tokens = defaultdict(int)
        total_triggers = 0
        for ev in buys:
            tid = ev.get('token_id', '')
            usd = ev.get('usd') or 0
            if usd <= 0:
                usd = (ev.get('price') or 0) * (ev.get('size') or 0)
            tw = token_windows[tid]
            if tw['start'] == 0 or ev['ts'] - tw['start'] > window_s:
                tw['start'] = ev['ts']
                tw['usd'] = usd
            else:
                tw['usd'] += usd
            if tw['usd'] >= min_usd:
                total_triggers += 1
                trigger_tokens[tid] += 1
                tw['start'] = ev['ts'] + 1
                tw['usd'] = 0
        if total_triggers > 0:
            triggered[cid] = {
                'triggers': total_triggers,
                'total_buy_usd': m['total_buy_usd'],
                'is_big': m['total_buy_usd'] >= 10000,
                'trigger_tokens': dict(trigger_tokens),
            }
    return triggered

configs = [
    ('Current $300/30m', 300, 30*60),
    ('$1000/60m', 1000, 60*60),
    ('$3000/120m', 3000, 120*60),
    ('$5000/180m', 5000, 180*60),
]

total_big = sum(1 for m in markets.values() if m['total_buy_usd'] >= 10000)

print(f"\n{'Config':<20} {'Triggered':>9} {'Big':>5} {'Small':>6} {'Precision':>10} {'Recall':>8} {'BigAvgTrig':>10} {'SmAvgTrig':>10} {'MultiTok%':>10}")
print("-" * 100)

for name, min_usd, window_s in configs:
    t = simulate_threshold(markets, min_usd, window_s)
    big = sum(1 for v in t.values() if v['is_big'])
    small = sum(1 for v in t.values() if not v['is_big'])
    total = len(t)
    prec = big / total if total > 0 else 0
    rec = big / total_big if total_big > 0 else 0
    big_avg = statistics.mean([v['triggers'] for v in t.values() if v['is_big']]) if big > 0 else 0
    sm_avg = statistics.mean([v['triggers'] for v in t.values() if not v['is_big']]) if small > 0 else 0
    multi = sum(1 for v in t.values() if len(v['trigger_tokens']) >= 2)
    multi_pct = multi / total if total > 0 else 0
    print(f"{name:<20} {total:>9,} {big:>5} {small:>6} {prec:>9.1%} {rec:>7.1%} {big_avg:>10.1f} {sm_avg:>10.1f} {multi_pct:>9.0%}")

# Deep dive: $3000/120m hedging analysis
print("\n=== $3000/120m: Hedging Deep Dive ===")
t3k = simulate_threshold(markets, 3000, 120*60)

single_big, multi_big = [], []
single_small, multi_small = [], []
for cid, v in t3k.items():
    multi = len(v['trigger_tokens']) >= 2
    if v['is_big']:
        (multi_big if multi else single_big).append(v)
    else:
        (multi_small if multi else single_small).append(v)

print(f"Big + single-token (clean):  {len(single_big)}")
print(f"Big + multi-token (hedged):  {len(multi_big)}")
print(f"Small + single-token:        {len(single_small)}")
print(f"Small + multi-token:         {len(multi_small)}")

# If we ONLY follow single-token triggers (no hedging)
single_total = len(single_big) + len(single_small)
single_prec = len(single_big) / single_total if single_total > 0 else 0
print(f"\nIf only follow single-token triggers:")
print(f"  Followed: {single_total}, Big: {len(single_big)}, Precision: {single_prec:.1%}, Recall: {len(single_big)/total_big:.1%}")

# Dominant token ratio in multi-token big markets
if multi_big:
    dom_ratios = []
    for v in multi_big:
        counts = sorted(v['trigger_tokens'].values(), reverse=True)
        dom_ratios.append(counts[0] / sum(counts))
    print(f"\nMulti-token big markets: dominant side ratio")
    print(f"  avg={statistics.mean(dom_ratios):.2f}, med={statistics.median(dom_ratios):.2f}")
    print(f"  >80% on one side: {sum(1 for r in dom_ratios if r > 0.8)}/{len(dom_ratios)}")
    print(f"  >60% on one side: {sum(1 for r in dom_ratios if r > 0.6)}/{len(dom_ratios)}")

# Combined strategy: $3000 threshold + only follow dominant token
print("\n=== Combined: $3000/120m + follow dominant token only ===")
# For multi-token, only count triggers on the dominant token
combined_big = len(single_big)  # single-token big: always follow
combined_small = len(single_small)
for v in multi_big:
    counts = sorted(v['trigger_tokens'].values(), reverse=True)
    if counts[0] / sum(counts) > 0.6:  # dominant side > 60%
        combined_big += 1
for v in multi_small:
    counts = sorted(v['trigger_tokens'].values(), reverse=True)
    if counts[0] / sum(counts) > 0.6:
        combined_small += 1

combined_total = combined_big + combined_small
combined_prec = combined_big / combined_total if combined_total > 0 else 0
print(f"  Followed: {combined_total}, Big: {combined_big}, Precision: {combined_prec:.1%}, Recall: {combined_big/total_big:.1%}")

# Your concern: high threshold kills hedging detection
# Let's check: in big markets with hedging, what % of the HEDGE side reaches $3000?
print("\n=== Hedge side analysis in big multi-token markets ===")
hedge_reaches_3k = 0
hedge_below_3k = 0
for v in multi_big:
    counts = sorted(v['trigger_tokens'].items(), key=lambda x: -x[1])
    if len(counts) >= 2:
        secondary_triggers = counts[1][1]
        if secondary_triggers > 0:
            hedge_reaches_3k += 1
        else:
            hedge_below_3k += 1

print(f"  Hedge side also triggers $3K: {hedge_reaches_3k}/{len(multi_big)}")
print(f"  Hedge side below $3K (filtered out): {hedge_below_3k}/{len(multi_big)}")
