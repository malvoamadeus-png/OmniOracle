"""Analyze what happens after 8th trigger + execution design options."""
import sys, gzip, json, statistics
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')

events = []
with gzip.open('output/events_cache_0xee613b.jsonl.gz', 'rt', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            events.append(json.loads(line))

AGG_WINDOW_S = 30 * 60
MIN_TRADE_SIZE = 300

markets = defaultdict(lambda: {'events': [], 'total_buy_usd': 0})
for ev in events:
    cid = ev.get('condition_id') or ev.get('token_id', '')
    if not cid:
        continue
    usd = ev.get('usd') or 0
    if usd <= 0:
        usd = (ev.get('price') or 0) * (ev.get('size') or 0)
    if ev['side'] == 'BUY':
        markets[cid]['total_buy_usd'] += usd
    markets[cid]['events'].append(ev)

results = []
for cid, m in markets.items():
    if m['total_buy_usd'] < 10000:
        continue
    buys = sorted([e for e in m['events'] if e['side'] == 'BUY'], key=lambda e: e['ts'])
    if not buys:
        continue
    window_start = buys[0]['ts']
    window_usd = 0
    trigger_count = 0
    cum_at_triggers = []
    trigger_usds = []
    for ev in buys:
        usd = ev.get('usd') or 0
        if usd <= 0:
            usd = (ev.get('price') or 0) * (ev.get('size') or 0)
        if ev['ts'] - window_start <= AGG_WINDOW_S:
            window_usd += usd
        else:
            window_start = ev['ts']
            window_usd = usd
        if window_usd >= MIN_TRADE_SIZE:
            trigger_count += 1
            trigger_usds.append(window_usd)
            cum_at_triggers.append(sum(trigger_usds))
            window_start = ev['ts'] + 1
            window_usd = 0
    if trigger_count >= 8:
        cum_at_8 = cum_at_triggers[7]
        remaining = m['total_buy_usd'] - cum_at_8
        results.append({
            'total_buy': m['total_buy_usd'],
            'cum_at_8': cum_at_8,
            'remaining_usd': remaining,
            'remaining_pct': remaining / m['total_buy_usd'],
            'total_triggers': trigger_count,
            'remaining_triggers': trigger_count - 8,
            'trigger_usds': trigger_usds,
            'avg_trigger_usd': statistics.mean(trigger_usds),
            'max_trigger_usd': max(trigger_usds),
        })

print(f"Big markets with >= 8 triggers: {len(results)}")
cum8 = [r['cum_at_8'] for r in results]
rem_pct = [r['remaining_pct'] for r in results]
rem_trig = [r['remaining_triggers'] for r in results]
avg_trig = [r['avg_trigger_usd'] for r in results]

print(f"\nAt 8th trigger:")
print(f"  Cumulative invested: avg=${statistics.mean(cum8):,.0f}, med=${statistics.median(cum8):,.0f}")
print(f"  Remaining to invest: avg={statistics.mean(rem_pct):.0%}, med={statistics.median(rem_pct):.0%}")
print(f"  Remaining triggers:  avg={statistics.mean(rem_trig):.0f}, med={statistics.median(rem_trig):.0f}")

print(f"\nPer-trigger window USD:")
print(f"  avg=${statistics.mean(avg_trig):,.0f}, med=${statistics.median(avg_trig):,.0f}")

buckets = {'<20%': 0, '20-40%': 0, '40-60%': 0, '60-80%': 0, '>80%': 0}
for r in rem_pct:
    if r < 0.2: buckets['<20%'] += 1
    elif r < 0.4: buckets['20-40%'] += 1
    elif r < 0.6: buckets['40-60%'] += 1
    elif r < 0.8: buckets['60-80%'] += 1
    else: buckets['>80%'] += 1
print(f"\nRemaining % after 8th trigger:")
for k, v in buckets.items():
    pct = v / len(results) * 100
    print(f"  {k}: {v} ({pct:.0f}%)")

prop = 0.005
cap = 75
avg_t = statistics.mean(avg_trig)
avg_c8 = statistics.mean(cum8)
print(f"\n=== Execution Options ===")
print(f"\nOption A: From 9th trigger, follow normally (0.5%, cap $75)")
print(f"  Remaining triggers: avg {statistics.mean(rem_trig):.0f}")
print(f"  Per trigger: avg ${avg_t:,.0f} x 0.5% = ${avg_t*prop:.1f}, capped at $75")
print(f"  Total copy per market: ~${min(avg_t*prop, cap) * statistics.mean(rem_trig):,.0f}")

print(f"\nOption B: At 8th trigger, lump sum + continue")
print(f"  Lump = 0.5% of cum_at_8 = avg ${avg_c8*prop:,.1f}, capped at $75")
print(f"  Then continue from 9th as Option A")

print(f"\nOption C: From 9th trigger, higher proportion (e.g. 2%)")
print(f"  Per trigger: avg ${avg_t:,.0f} x 2% = ${avg_t*0.02:.1f}, capped at $75")

# Trigger USD distribution
all_usds = []
for r in results:
    all_usds.extend(r['trigger_usds'])
all_usds.sort()
n = len(all_usds)
print(f"\n=== Trigger USD distribution (all {n:,} triggers in big markets) ===")
for p in [10, 25, 50, 75, 90, 95, 99]:
    idx = min(int(n * p / 100), n - 1)
    print(f"  p{p}: ${all_usds[idx]:,.0f}")
big_triggers = sum(1 for u in all_usds if u > 1000)
huge_triggers = sum(1 for u in all_usds if u > 3000)
print(f"  >$1000: {big_triggers} ({big_triggers/n*100:.1f}%)")
print(f"  >$3000: {huge_triggers} ({huge_triggers/n*100:.1f}%)")
