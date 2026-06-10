import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
db = sqlite3.connect(PROJECT_ROOT / "backend" / "packages" / "copytrade" / "copytrade.sqlite")
db.row_factory = sqlite3.Row
cur = db.cursor()

# 查询 ct_trades 中的这些交易记录
print('=== nba-uta-phx-2026-03-28 的交易概览 ===\n')

cur.execute('''
SELECT 
    account_name,
    leader_side, leader_price, leader_size, leader_usd,
    our_side, our_price, our_size, our_usd,
    status, skip_reason, exit_status,
    created_at
FROM ct_trades
WHERE market_slug = 'nba-uta-phx-2026-03-28'
ORDER BY created_at DESC
''')

results = cur.fetchall()
print(f"找到 {len(results)} 条交易记录\n")

# 按状态分类统计
status_summary = {}
skip_reasons = {}
total_leader_usd = 0
total_our_usd = 0

for row in results:
    status = row['status']
    if status not in status_summary:
        status_summary[status] = 0
    status_summary[status] += 1
    
    if row['skip_reason']:
        if row['skip_reason'] not in skip_reasons:
            skip_reasons[row['skip_reason']] = 0
        skip_reasons[row['skip_reason']] += 1
    
    if row['leader_usd']:
        total_leader_usd += row['leader_usd']
    if row['our_usd']:
        total_our_usd += row['our_usd']

print("=== 📊 交易统计摘要 ===")
print(f"总交易条数:         {len(results)}")
print(f"Leader总投入金额:   ${total_leader_usd:,.2f}")
print(f"我们的总投入金额:   ${total_our_usd:,.2f}")
print(f"金额差异:          ${total_leader_usd - total_our_usd:,.2f}")
print(f"执行率:            {(total_our_usd/total_leader_usd*100 if total_leader_usd > 0 else 0):.2f}%\n")

print("✅ 交易状态分布:")
for status, count in sorted(status_summary.items()):
    pct = (count / len(results) * 100)
    print(f"   {status:10s}: {count:3d} ({pct:5.1f}%)")

if skip_reasons:
    print("\n❌ Skipped交易的原因 (Skip了{}条):\n".format(status_summary.get('skipped', 0)))
    sorted_reasons = sorted(skip_reasons.items(), key=lambda x: -x[1])
    for reason, count in sorted_reasons[:10]:
        pct = (count / len(results) * 100)
        # 截断长的理由
        if len(reason) > 60:
            reason = reason[:57] + '...'
        print(f"   [{count:2d}] {reason}")

# 查看Filled的交易成功了多少
cur.execute('''
SELECT COUNT(*) as total_filled
FROM ct_trades
WHERE market_slug = 'nba-uta-phx-2026-03-28' AND status = 'filled'
''')
filled = cur.fetchone()['total_filled']

print(f"\n\n✅ 成功成交(Filled)的交易信息 ({filled}笔):")

cur.execute('''
SELECT our_size, our_price, our_usd, created_at
FROM ct_trades
WHERE market_slug = 'nba-uta-phx-2026-03-28' AND status = 'filled'
ORDER BY created_at DESC
LIMIT 20
''')

results = cur.fetchall()
total_filled_usd = 0
for i, row in enumerate(results[:15], 1):
    if row['our_usd']:
        total_filled_usd += row['our_usd']
    size = row['our_size'] if row['our_size'] else 'N/A'
    price = f"${row['our_price']:.4f}" if row['our_price'] else 'N/A'
    usd = f"${row['our_usd']:.2f}" if row['our_usd'] else 'N/A'
    print(f"   {i}. {size:>8} @ {price:>10} = {usd:>10} ({row['created_at'][:19]})")

print(f"\n   成交总金额: ${total_filled_usd:,.2f}")

db.close()
