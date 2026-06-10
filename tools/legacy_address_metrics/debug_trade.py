import sqlite3
import json
from datetime import datetime
from pathlib import Path

# 查询 copytrade 数据库
PROJECT_ROOT = Path(__file__).resolve().parents[2]
db = sqlite3.connect(PROJECT_ROOT / "backend" / "packages" / "copytrade" / "copytrade.sqlite")
db.row_factory = sqlite3.Row
cur = db.cursor()

# 首先找到这场比赛对应的 market/token
# UTA vs PHX 2026-03-28，Suns spread -16.5
print('=== 查找比赛信息 ===')
print('Looking for UTA vs PHX 2026-03-28, Suns -16.5...\n')

# 从 ct_leader_activity 中查询匹配的活动
cur.execute('''
SELECT DISTINCT market_id, condition_id, token, token_desc, COUNT(*) as activity_count
FROM ct_leader_activity
WHERE token_desc LIKE '%Suns%' OR token_desc LIKE '%UTA%' OR token_desc LIKE '%PHX%'
  OR token_desc LIKE '%16.5%'
GROUP BY market_id, condition_id, token
ORDER BY activity_count DESC
LIMIT 20
''')

results = cur.fetchall()
if results:
    print(f"Found {len(results)} matches:")
    for row in results:
        print(f"\nMarket: {row['market_id']}")
        print(f"Condition: {row['condition_id']}")
        print(f"Token: {row['token']}")
        print(f"Description: {row['token_desc']}")
        print(f"Leader activities: {row['activity_count']}")
else:
    print('No direct matches found. Searching by date range...\n')
    
# 尝试按日期范围查询
cur.execute('''
SELECT DISTINCT market_id, condition_id, token, token_desc, timestamp, COUNT(*) as count
FROM ct_leader_activity
WHERE DATE(timestamp) = '2026-03-28' OR DATE(timestamp) = '2026-03-29'
GROUP BY market_id, condition_id, token
ORDER BY timestamp DESC
LIMIT 30
''')

results = cur.fetchall()
print(f'Activities from 2026-03-28 to 2026-03-29 (showing first 15):\n')
for i, row in enumerate(results[:15]):
    print(f"{i+1}. {row['token_desc']}")
    print(f"   Token: {row['token']}, Market: {row['market_id']}")
    print(f"   Activities: {row['count']}\n")

db.close()
