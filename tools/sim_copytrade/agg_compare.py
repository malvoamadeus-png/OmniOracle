"""Compare: raw events vs maker-like aggregated events for delayed follow."""
import sys, gzip, json, statistics
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

events = []
with gzip.open('output/events_cache_0xee613b.jsonl.gz', 'rt', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            events.append(json.loads(line))

# Build per-market buy events
markets = defaultdict(lambda: {'buys': [], 'total_buy_usd': 0})
for ev in events:
    cid = ev.get('condition_id') or ev.get('token_id', '')
    if not cid or ev['side'] != 'BUY':
        continue
    usd = ev.get('usd') or 0
    if usd <= 0:
        usd = (ev.get('price') or 0) * (ev.get('size') or 0)
    markets[cid]['buys'].append({'ts': ev['ts'], 'usd': usd})
    markets[cid]['total_buy_usd'] += usd

# Simulate two approaches for threshold=$2000, window=60min
THR = 2000
WIN = 60 * 60

def count_triggers_raw(buys, thr, win):
    """Raw events: each individual buy contributes to the window."""
    if not buys:
        return 0
    ws = buys[0]['ts']
    wu = 0
    triggers = 0
    for b in buys:
        if b['ts'] - ws <= win:
            wu += b['usd']
        else:
            ws = b['ts']
            wu = b['usd']
        if wu >= thr:
            triggers += 1
            ws = b['ts'] + 1
            wu = 0
    return triggers

def count_triggers_makerlike(buys, thr, win, ml_threshold=300, ml_window=1800):
    """
    First do maker-like aggregation (small trades within ml_window summed to >= ml_threshold),
    then count delayed-follow triggers on the aggregated events.
    """
    if not buys:
        return 0
    # Step 1: maker-like aggregation
    aggregated = []
    agg_start = buys[0]['ts']
    agg_usd = 0
    for b in buys:
        if b['ts'] - agg_start <= ml_window:
            agg_usd += b['usd']
        else:
            if agg_usd >= ml_threshold:
                aggregated.append({'ts': agg_start, 'usd': agg_usd})
            agg_start = b['ts']
            agg_usd = b['usd']
        if agg_usd >= ml_threshold:
            aggregated.append({'ts': b['ts'], 'usd': agg_usd})
            agg_start = b['ts'] + 1
            agg_usd = 0
    if agg_usd >= ml_threshold:
        aggregated.append({'ts': agg_start, 'usd': agg_usd})

    # Step 2: delayed-follow triggers on aggregated
    return count_triggers_raw(aggregated, thr, win)

# Compare for all markets
big_markets = {cid: m for cid, m in markets.items() if m['total_buy_usd'] >= 10000}
small_markets = {cid: m for cid, m in markets.items() if m['total_buy_usd'] < 10000}

print(f"Markets: {len(markets):,} total, {len(big_markets):,} big, {len(small_markets):,} small")
print(f"Config: threshold=${THR}, window={WIN//60}min")
print()

for label, subset in [("BIG (>=10K)", big_markets), ("SMALL (<10K)", small_markets)]:
    raw_triggers = []
    ml_triggers = []
    raw_reached_8 = 0
    ml_reached_8 = 0

    for cid, m in subset.items():
        buys = sorted(m['buys'], key=lambda b: b['ts'])
        rt = count_triggers_raw(buys, THR, WIN)
        mt = count_triggers_makerlike(buys, THR, WIN)
        raw_triggers.append(rt)
        ml_triggers.append(mt)
        if rt >= 8:
            raw_reached_8 += 1
        if mt >= 8:
            ml_reached_8 += 1

    print(f"=== {label} ({len(subset):,} markets) ===")
    print(f"  Raw events approach:")
    print(f"    Avg triggers: {statistics.mean(raw_triggers):.1f}")
    print(f"    Reached 8+: {raw_reached_8} ({raw_reached_8/len(subset)*100:.1f}%)")
    print(f"    Reached 1+: {sum(1 for t in raw_triggers if t >= 1)} ({sum(1 for t in raw_triggers if t >= 1)/len(subset)*100:.1f}%)")
    print(f"  Maker-like first, then delayed-follow:")
    print(f"    Avg triggers: {statistics.mean(ml_triggers):.1f}")
    print(f"    Reached 8+: {ml_reached_8} ({ml_reached_8/len(subset)*100:.1f}%)")
    print(f"    Reached 1+: {sum(1 for t in ml_triggers if t >= 1)} ({sum(1 for t in ml_triggers if t >= 1)/len(subset)*100:.1f}%)")
    print()

# Also check: what does the raw event USD distribution look like?
all_buy_usds = []
for m in markets.values():
    for b in m['buys']:
        if b['usd'] > 0:
            all_buy_usds.append(b['usd'])
all_buy_usds.sort()
n = len(all_buy_usds)
print(f"=== Raw BUY event USD distribution ({n:,} events) ===")
for p in [10, 25, 50, 75, 90, 95, 99]:
    idx = min(int(n * p / 100), n - 1)
    print(f"  p{p}: ${all_buy_usds[idx]:.2f}")
below_300 = sum(1 for u in all_buy_usds if u < 300)
print(f"  <$300: {below_300:,} ({below_300/n*100:.1f}%)")
print(f"  <$2000: {sum(1 for u in all_buy_usds if u < 2000):,} ({sum(1 for u in all_buy_usds if u < 2000)/n*100:.1f}%)")

# The key insight: with $2000 threshold and 60min window,
# the delayed-follow's OWN aggregation already handles the small trades.
# It sums up all buys within 60min until they reach $2000.
# Maker-like aggregation ($300/30min) would pre-filter, reducing the number
# of events that reach the delayed-follow, but the delayed-follow window
# is WIDER (60min vs 30min) and HIGHER threshold ($2000 vs $300).
print()
print("=== CONCLUSION ===")
print("The delayed-follow's own aggregation (e.g. $2000/60min) is a SUPERSET")
print("of maker-like aggregation ($300/30min). Small trades naturally accumulate")
print("within the 60min window to reach $2000. Pre-aggregating with maker-like")
print("would reduce trigger count because it discards sub-$300 windows.")
