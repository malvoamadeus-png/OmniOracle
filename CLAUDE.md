# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PolySport 当前唯一核心产品能力是：

1. **直接跟单**：监听 leader 信号、风控、下单、退出、赎回和运行维护。
2. **跟单详情记录/展示**：记录跟单生命周期、Leader PnL 归因、日盈亏与前端展示。

模拟跟单、地址测评、聪明钱播报、参数研究、市场探索等都属于非核心研究/工具能力，必须放在 `tools/` 下，并保持可独立复制运行。

## Architecture

**核心生产代码：**

- `backend/packages/copytrade/` — 直接跟单业务包，也是后端核心逻辑的 canonical 位置。
- `backend/src/` — CLI/API/worker/watchdog/admin 入口层，只做参数解析、依赖装配和转发。
- `frontend/dashboard/` — 公开只读前端，读取 Supabase 展示跟单详情、Leader 归因、日盈亏等。
- `frontend/admin/` — 本地管理前端，通过本地 admin API 管理配置、状态和维护动作。

**非核心工具代码：**

- `tools/sim_copytrade/` — 模拟跟单、延迟跟单、参数搜索、差距诊断。
- `tools/smart_money_broadcast/` — 地址发现、地址测评、单地址 Markdown 播报。
- `tools/legacy_address_metrics/` — 早期批量地址测评/指标入库管线，仅作历史兼容和复盘。
- `tools/getEndDateandLiquidity/` — 一次性市场结束时间/流动性探索脚本。

根目录旧 `copytrade/` 已删除；新改动默认落在 `backend/packages/copytrade/`。旧数据管线脚本已归档到 `tools/legacy_address_metrics/`，属于历史地址测评/数据研究能力，除非明确维护兼容，否则不要扩展为核心链路。

## Tools Independence Rule

`tools/` 下每个子目录都要尽量做到“复制整个文件夹到别处即可运行”。工具代码不得 import `backend.*`、生产 `copytrade.*`、根目录业务脚本，或通过 `sys.path` 指向 PolySport 项目根目录。工具运行数据只写入自己的 `runtime/`、`output/`、`.cache/` 等目录。`tools/legacy_address_metrics/` 是历史兼容例外，少数旧脚本会读取 backend copytrade SQLite/accounts；不要把这个例外复制到新工具。

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
npm run dev       # Vite dev server
npm run build     # tsc -b && vite build
npm run preview   # Preview production build

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

- Backend/root `.env`: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SQLITE_PATH` 等。
- Frontend `.env`: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`。
- `tools/` 不应依赖生产 `.env`；如需密钥或配置，放在工具目录自己的 `.env.example`/README 中说明。

## Conventions

- Documentation and comments are in Chinese; code identifiers are in English
- Python scripts use `argparse` for CLI when applicable.
- Backend business logic stays under `backend/packages/copytrade`; `backend/src` remains an entry layer.
- Frontend public dashboard must not read local credentials, account TOML, or admin APIs.
- Research/simulation/address-evaluation code goes under `tools/` and follows the independence rule above.

## Copytrade Leader PnL 归因口径（长期规范）

本节为 `copytrade` 子系统中 Leader 归因与日盈亏口径的唯一规范。

### 1) 会计恒等式与约束

- 在任意快照时点 `t`，账户盈亏必须满足：
  - `AccountPnL(t) = ClosedRealized(t) + OpenUnrealized(t)`
- Leader 归因是“纯归因口径”：
  - 归因来源仅允许来自“由该 Leader 触发并被系统执行”的交易记录。
  - 仅对可归因 PnL 做分配，不做人为缩放去对齐官方账户曲线（`/user-pnl`）。

### 2) 归因拆分定义与数据来源

- Open 部分（`OpenUnrealized`）：
  - 真值来源：链上持仓接口（positions API）。
  - 单 token 浮盈定义：`currentValue - initialValue`；若该字段不可用，则回退到 `cashPnl`。
  - Leader 映射来源：`ct_trades` 中 open BUY 记录（`status='filled'`、`our_side='BUY'`、`exit_status='open'`）。
  - 分摊规则：对同一 token，按各 Leader 对应 `our_usd` 权重分摊浮盈。

- Closed 部分（`ClosedRealized`）：
  - 真值来源：`ct_trades.profit`（copytrade 生命周期内的已实现结果）。
  - 聚合维度：先按 `leader + market(condition_id/token fallback)` 聚合，再汇总到 leader 级别。

### 3) 日表语义（必须遵守）

- `ct_daily_leader_pnl` 存储的是“当日增量（delta）”，不是累计快照。
- 当日恒等式：
  - `daily_total_pnl = daily_realized_increment + daily_unrealized_change`
- 前端展示规则：直接展示表内日值，严禁再次做 delta 转换。
- 日切边界：按 UTC+8 口径切日，仅用于归因统计，不用于官方收益对齐。

### 4) 2026-03-18 基线事件（关键历史事实）

- 在切换到纯归因口径时，执行一次性历史重建：
  - 时间范围：**2026-03-18（UTC+8）到昨天**；
  - 只回放 closed positions 的 realized（`exit_status='exited'` 且 `exit_at` 存在）；
  - 该区间 `unrealized_pnl = 0`；
  - 从“今天”开始恢复正常口径（realized + unrealized）。
- 强约束：
  - 严禁把 2026-03-18 之前的历史 realized 注入重建后的日增量；
  - 历史重建完成后，首日需避免累计 realized 一次性打入当天。

### 5) 实施保护规则

- 未来若调整归因实现，必须保持 2026-03-18 基线语义；除非明确进行“新基线迁移”并补充迁移文档。
- 同步到 Supabase 的 `copytrade_daily_leader_pnl` 时，按 `account_name` 先删后写（镜像同步），防止旧口径残留脏行污染前端。

## Copytrade Analytics 跟单差距分析规范（tools）

本节定义跟单差距诊断工具的分析目标、执行流程、指标计算方式与精度边界。此类能力属于诊断/研究工具，应放在 `tools/` 下，不要恢复到生产核心包 `backend/packages/copytrade/analytics`。

### 1) 分析目标

- 此类工具用于回答“为什么我们跟某个 leader 的收益有差距”，属于**诊断分析**。
- 此类工具不用于替代会计口径的日盈亏结算；会计真值以 Leader PnL 归因口径为准。

### 2) 核心输入数据

- `ct_trades`：我方跟单执行记录（filled / expired / failed / skipped / partial 等状态）。
- `ct_leader_activity`：leader 原始活动流（BUY/SELL），由 Data API 同步并本地缓存。
- `ct_leader_market_pnl`：leader 市场级盈亏，用于推导市场 return%。
- `ct_config_snapshots`：配置快照，用于读取固定跟单金额、次数上限、市场限制模式等配置参数。

### 3) 执行流程（单 leader）

1. 可选同步 leader activity（`--sync-activity`，支持 watermark 增量同步与 `--force-sync` 全量同步）。
2. 对 leader BUY activity 进行碎成交聚合预处理（maker-like merge）以压缩“拆单噪声”。
3. 运行四个诊断指标：
   - `fill_efficiency`（成交效率）
   - `sizing_difference`（Sizing Alpha）
   - `trade_count_diff`（交易次数差异）
   - `coverage_rate`（信号覆盖率）
4. 生成 `LeaderReport`（可终端输出、JSON 导出、写入 `ct_analytics_reports`）。
5. 批量模式（`--all`）会额外生成跨 leader 汇总。

### 4) 四个指标的计算定义

- `fill_efficiency`：
  - 统计 filled/partial/expired/failed；
  - 滑点：`(our_price - leader_price) / leader_price`；
  - 未成交机会成本：对 expired/failed 订单，使用参考终点价估算（优先 leader SELL 加权价，回退 midpoint/结算价）。

- `sizing_difference`：
  - 场景 A：每笔固定金额（`fixed_usd`）；
  - 场景 B：按 leader 实际投入金额；
  - 在同一市场 return% 下比较两场景 PnL，得到 `sizing_impact`。

- `trade_count_diff`：
  - 对每个市场比较 `leader BUY 次数` 与 `我方 BUY 记录数`；
  - 差异拆分为 `config_limited / system_issues / beyond_cap / not_captured`；
  - 各分类错失收益按 `ret_pct * fixed_usd * count` 估算。

- `coverage_rate`：
  - 统计 leader 信号总数与我方响应结构（filled/expired_or_failed/partial/skipped/not_captured）；
  - 输出覆盖率、响应率、skip 原因分布与按类别估算错失收益。

### 5) 输出解释规则

- 报告中的“错失收益/影响收益”字段是**估算值**，用于排序优先级和定位问题，不等同于账务已实现盈亏。
- 若用于运营决策，应结合真实成交日志、风控拦截日志和日归因结果交叉验证。

### 6) 当前实现的已知边界（必须知晓）

- 当前 `--metric` 参数已定义但未生效，CLI 仍会执行全部四个指标。
- 报告构建阶段虽执行了“碎成交聚合预处理”，但各 analyzer 目前仍直接读取 `ct_leader_activity` 原始表；聚合结果尚未真正注入指标计算。
- 多处配置读取使用“当前时刻配置”而非“信号发生时配置”，历史配置回放存在口径漂移风险。
- 部分错失收益估算依赖在线价格查询（midpoint/结算价兜底），在网络受限环境下可能显著变慢。

### Copytrade Reconcile Update (2026-03-24)

- econcile_redeemed_positions must be non-destructive:
  - do not convert unresolved missing-on-chain open rows into phantom zero rows.
  - unresolved rows stay open with pending marker until settlement price is available.
- Add epair_phantom_positions before daily rebuild:
  - attempt to recover historical phantom rows using settlement data.
  - recovered rows are restored into status='filled', exit_status='exited', with computed profit and valid exit_at.
- Daily history rebuild in pure mode supports forced rebuild when phantom repair modifies historical rows.

### Copytrade Dynamic 14-Day Backfill Rule (2026-03-24)

- Daily rebuild for pure attribution must guarantee a usable recent 14-day window.
- Effective rebuild start is now:
  - `min(PURE_DAILY_BASELINE_DATE, today(UTC+8)-13d)`
- Rebuild interval remains closed-realized only for historical days (`effective_start .. yesterday`), and normal open+closed delta applies from today onward.
- Persist both baseline and effective start in `ct_meta` to avoid silent scope drift.
