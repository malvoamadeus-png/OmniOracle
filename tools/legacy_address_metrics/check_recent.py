import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

# 查询 copytrade 数据库
PROJECT_ROOT = Path(__file__).resolve().parents[2]
db = sqlite3.connect(PROJECT_ROOT / "backend" / "packages" / "copytrade" / "copytrade.sqlite")
db.row_factory = sqlite3.Row
cur = db.cursor()

print('=== 查询最近活动 ===\n')

# 查询ctleader_activity最新的活动
cur.execute('''
SELECT timestamp_utc, market_slug, COUNT(*) as count
FROM ct_leader_activity
GROUP BY DATE(timestamp_utc), market_slug
ORDER BY timestamp_utc DESC
LIMIT 30
''')

results = cur.fetchall()
print(f"最近的活动 (前30个):\n")
for i, row in enumerate(results, 1):
    print(f"{i}. {row['timestamp_utc'][:10]} | {row['market_slug']} (活动: {row['count']})")


# 现在查询 ct_trades 中的活动
print("\n\n=== ct_trades 最近交易 ===\n")
cur.execute('''
SELECT created_at, market_slug, outcome, COUNT(*) as count, SUM(our_usd) as total_usd
FROM ct_trades
GROUP BY DATE(created_at), market_slug
ORDER BY created_at DESC
LIMIT 20
''')

results = cur.fetchall()
for i, row in enumerate(results, 1):
    print(f"{i}. {row['created_at'][:10]} | {row['market_slug']} ({row['outcome']}) | Trades: {row['count']}, USD: {row['total_usd']:.2f if row['total_usd'] else 0}")

db.close()
