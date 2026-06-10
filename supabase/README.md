# Supabase 操作说明

本目录只放两类东西：

1. `schema.sql`：远端表结构、RLS、grant、轻量迁移。
2. `sync_to_supabase.py`：把本地 SQLite 的只读展示数据同步到 Supabase。

## 推荐链路

先更新远端 schema，再同步数据：

```bash
bash supabase/run_sql.sh supabase/schema.sql
python3 supabase/sync_to_supabase.py
```

## 环境变量

根 `.env` 至少需要：

```bash
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_DB_URL=postgresql://...
SQLITE_PATH=metrics_fresh.sqlite
```

- `SUPABASE_DB_URL` 用于执行 `schema.sql`。
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` 用于 REST 同步。

## run_sql.sh

`run_sql.sh` 的目标是避免“本机没有 psql / pip / node / pg 驱动”时还得临场折腾环境。

行为：

- 优先使用系统自带 `psql`。
- 若系统没有 `psql`，自动下载并缓存一个便携版 PostgreSQL client 到 `tmp/portable-psql/`。
- 默认执行 `supabase/schema.sql`，也可以传入别的 SQL 文件：

```bash
bash supabase/run_sql.sh supabase/update_lol_tags.sql
```

## 原则

- `schema.sql` 只放 dashboard/同步链路需要的远端对象和权限。
- 生产交易运行依赖本地 SQLite，不依赖 Supabase 写回。
- 若是一次性数据修复，优先单独建小 SQL 文件，不要把历史修复无限堆进 `schema.sql`。
