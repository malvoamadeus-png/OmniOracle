# Copytrade 运行口径补充

本文件存放 `copytrade` 的长期口径、历史基线和诊断边界。  
`AGENTS.md` / `CLAUDE.md` 只保留高层原则；若修改归因、日表、重建或差距分析实现，再回到本文件核对。

## Leader PnL 归因

- 任意时点都满足：
  - `AccountPnL = ClosedRealized + OpenUnrealized`
- Leader 归因只来自“由该 leader 触发且被系统执行”的交易。
- 不允许为了贴近官方 `/user-pnl` 曲线，对 leader 归因做人为缩放。

## Open / Closed 口径

- Open Unrealized：
  - 真值来自链上持仓接口。
  - 单 token 优先用 `currentValue - initialValue`，缺失时回退 `cashPnl`。
  - 同 token 多个 leader 按 `our_usd` 权重分摊。

- Closed Realized：
  - 真值来自 `ct_trades.profit`。
  - 先按 `leader + market` 聚合，再汇总到 leader。

## 日表语义

- `ct_daily_leader_pnl` 存的是“日增量”，不是累计快照。
- 当日恒等式：
  - `daily_total_pnl = daily_realized_increment + daily_unrealized_change`
- 前端禁止再次对日表做 delta 转换。
- 日切边界按 UTC+8，只用于归因统计。

## 历史基线

- 2026-03-18 是纯归因口径的历史基线。
- 历史重建区间只回放 closed realized；该区间 unrealized 固定为 0。
- 未来若调整实现，必须保持这条基线语义，除非显式写迁移文档。

## 动态回补

- 纯归因日表需要保证最近 14 天窗口可用。
- effective rebuild start：
  - `min(PURE_DAILY_BASELINE_DATE, today(UTC+8)-13d)`

## 同步规则

- 同步到 Supabase 时，按 `account_name` 先删后写，保持镜像。
- Supabase 只承接 dashboard 需要的只读展示数据，不承接本地交易配置或凭证。

## 跟单差距分析工具

这类能力属于 `tools/`，不是核心交易链路。

- 目标：解释“我们为什么和某 leader 收益有差距”。
- 输入核心表：
  - `ct_trades`
  - `ct_leader_activity`
  - `ct_leader_market_pnl`
  - `ct_config_snapshots`
- 当前诊断指标：
  - `fill_efficiency`
  - `sizing_difference`
  - `trade_count_diff`
  - `coverage_rate`

## 已知边界

- `--metric` 参数当前仍会跑全部分析器。
- leader BUY 聚合预处理还没有真正注入各 analyzer。
- 部分配置读取仍是“当前配置”，不是“信号发生时配置”。
- 部分错失收益估算依赖在线价格查询，弱网时会变慢。
