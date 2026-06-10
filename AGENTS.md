# AGENTS.md

This file provides guidance to Codex when working in this repository.

## Project Focus

PolySport 当前只有两个核心产品能力：

1. **直接跟单**：监听 leader 信号、风控、下单、退出、赎回和运行维护。
2. **跟单详情记录/展示**：记录跟单生命周期、Leader PnL 归因、日盈亏与前端展示。

模拟跟单、地址测评、聪明钱播报、参数研究、市场探索都属于非核心工具能力，必须放在 `tools/` 下，并保持可独立复制运行。

## Repository Map

### 核心生产代码

- `backend/packages/copytrade/`：直接跟单核心逻辑的 canonical 位置。
- `backend/src/`：CLI / API / worker / watchdog / admin 入口层，只做参数解析和依赖装配。
- `frontend/dashboard/`：公开只读前端，只读 Supabase 展示跟单详情、Leader 归因、日盈亏。
- `frontend/admin/`：本地管理前端，通过本地 admin API 管理配置、状态和维护动作。

### 非核心工具代码

- `tools/sim_copytrade/`
- `tools/smart_money_broadcast/`
- `tools/legacy_address_metrics/`
- `tools/getEndDateandLiquidity/`

旧根目录 `copytrade/` 已删除；默认不要把新逻辑放回根目录脚本。

## Tools Independence Rule

`tools/` 下每个子目录都要尽量做到“复制整个文件夹到别处即可运行”。新增或修改工具时必须遵守：

- 不得 import `backend.*`、生产 `copytrade.*`、根目录业务脚本，或通过 `sys.path` 指向项目根目录。
- 不得直接读取生产跟单 SQLite、账户 TOML、生产 `.env`、Supabase 主表或根目录 `metrics_fresh.sqlite`。
- 工具依赖的 helper、指标口径、配置快照，应复制或封装在工具目录内部。
- 工具运行数据只写入自己的 `runtime/`、`output/`、`.cache/` 等目录。
- `tools/legacy_address_metrics/` 是历史兼容例外；不要把这个例外扩散到新工具。

## Data Boundaries

核心跟单数据流：

Polymarket public APIs / CLOB / websocket → `backend/packages/copytrade` → local copytrade SQLite → Supabase sync → `frontend/dashboard`

工具数据流：

Polymarket public APIs → `tools/<tool_name>/runtime` 或 `tools/<tool_name>/output`

两条链路不要互相写库或互相 import。

## Supabase Workflow

远端结构/权限更新：

```bash
bash supabase/run_sql.sh supabase/schema.sql
```

本地数据同步：

```bash
python3 supabase/sync_to_supabase.py
```

说明：

- `SUPABASE_DB_URL` 用于执行 schema SQL。
- `SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY` 用于 REST 同步。
- 一次性修复优先单独建小 SQL 文件，不要无限膨胀 `supabase/schema.sql`。
- `supabase/README.md` 是 Supabase 运维入口文档。

## Commands

### Core copytrade backend

```bash
python backend/src/copytrade_worker.py
python backend/src/copytrade_watchdog.py
python backend/src/copytrade_admin.py
```

### Frontend

```bash
cd frontend/dashboard
npm install
npm run dev
npm run build

cd frontend/admin
npm install
npm run dev
npm run build
```

### Tools

```bash
python tools/sim_copytrade/main.py --address 0x...

cd tools/smart_money_broadcast
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python cli.py
```

## Environment Variables

- 根 `.env`：`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`、`SUPABASE_DB_URL`、`SQLITE_PATH` 等。
- 前端 `.env`：`VITE_SUPABASE_URL`、`VITE_SUPABASE_ANON_KEY`。
- `tools/` 不应依赖生产 `.env`；如需配置，放在工具目录自己的 `.env.example` / README。

## Conventions

- 文档和注释使用中文；代码标识符使用英文。
- Python CLI 默认使用 `argparse`。
- 业务逻辑留在 `backend/packages/copytrade`，`backend/src` 保持入口层。
- 公开 dashboard 不得读取本地凭证、账户 TOML 或 admin API。
- 研究/模拟/地址评估代码统一进 `tools/`。

## Copytrade Long-Term Rules

- 账户盈亏恒等式必须保持：`AccountPnL = ClosedRealized + OpenUnrealized`。
- Leader 归因只来自被系统实际执行的 leader 交易，不允许为了贴近官方 `/user-pnl` 做人为缩放。
- `ct_daily_leader_pnl` 是日增量表，前端禁止再次做 delta 转换。
- 修改归因、历史基线、动态回补或 gap analytics 前，先读：
  - `docs/architecture/copytrade_refactor_alignment.md`
  - `docs/architecture/copytrade_operating_norms.md`
