import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
db = sqlite3.connect(PROJECT_ROOT / "backend" / "packages" / "copytrade" / "copytrade.sqlite")
cur = db.cursor()

# 查看所有表
cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
tables = cur.fetchall()
print("=== 数据库表 ===")
for table in tables:
    print(f"  {table[0]}")

# 查看 ct_leader_activity 结构
print("\n=== ct_leader_activity 表结构 ===")
cur.execute("PRAGMA table_info(ct_leader_activity);")
columns = cur.fetchall()
for col in columns:
    print(f"  {col[1]}: {col[2]}")

# 查看 ct_trades 结构
print("\n=== ct_trades 表结构 ===")
cur.execute("PRAGMA table_info(ct_trades);")
columns = cur.fetchall()
for col in columns:
    print(f"  {col[1]}: {col[2]}")

db.close()
