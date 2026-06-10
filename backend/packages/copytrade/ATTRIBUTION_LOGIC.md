# Copytrade 归因逻辑说明

本文档说明 `copytrade` 子系统中 Leader 归因与日归属的当前实现口径，重点覆盖以下四张表：

- `ct_leader_summary`
- `ct_leader_market_pnl`
- `ct_daily_leader_pnl`
- `ct_daily_leader_market_leg_pnl`

不覆盖 `compare` 系列表，也不覆盖前端展示层逻辑。

## 1) 总览

当前归因分成两层：

- 快照层：
  - `ct_leader_summary`
  - `ct_leader_market_pnl`
- 日台账层：
  - `ct_daily_leader_market_leg_pnl`
  - `ct_daily_leader_pnl`

其中：

- 快照层回答的是“现在这一刻，每个 leader 归因后累计赚亏多少”
- 日台账层回答的是“某一天，这个 leader / 这个市场 leg 归属于哪一天、当天增量是多少”

当前实现的单一真源约束是：

- 最终落库后的日归因真源是 `ct_daily_leader_market_leg_pnl`
- `ct_daily_leader_pnl` 是从 `ct_daily_leader_market_leg_pnl` 聚合得到的派生表

但在一次构建运行过程中，`ct_daily_leader_pnl` 会先被写入“历史回填 + 当天 provisional target”，供 `ct_daily_leader_market_leg_pnl` 重建时作为 target 输入使用；等 leg 台账重建完成后，再反向聚合覆盖回 `ct_daily_leader_pnl`。

## 2) 基础口径

所有归因只使用 copytrade 自身能追溯的数据：

- 交易来源：`ct_trades`
- 当前持仓真值：链上 positions API
- 市场结算时间：Gamma / event / market metadata

只归因满足以下条件的跟单交易：

- `ct_trades.status='filled'`
- `ct_trades.our_side='BUY'`
- `leader_address` 非空

统一日切口径：

- UTC+8

## 3) 快照层

### 3.1 `ct_leader_summary`

粒度：

- `account_name + leader_address`

含义：

- 截至当前构建时刻，该 leader 在该账户下被归因到的累计 realized / unrealized / total

来源拆分：

- `total_realized_pnl`
  - 来自 `ct_trades.profit`
  - 按 `leader_address` 汇总
- `total_unrealized_pnl`
  - 来自链上持仓接口
  - 先拿到账户当前所有 open token 的 unrealized
  - 再按 token 对应的 leader 权重分摊

leader 权重如何算：

- 只看 `exit_status='open'` 的 BUY 跟单
- 对同一 `token_id`：
  - 先按 `leader_address` 汇总剩余成本
  - 优先用 `our_usd`
  - `our_usd` 缺失时回退 `our_price * our_size`
- 该 leader 在该 token 上的权重 = 该 leader 剩余成本 / 该 token 全部 leader 剩余成本

最终公式：

- `total_pnl = total_realized_pnl + total_unrealized_pnl`

### 3.2 `ct_leader_market_pnl`

粒度：

- `account_name + leader_address + condition_id`

含义：

- 当前时刻，该 leader 在该 market 上被归因到的累计 realized / unrealized / total

realized 部分：

- 来自 `ct_trades.profit`
- 按 `leader + market` 聚合
- market key 优先级：
  - `condition_id`
  - `token_id`
  - `unknown_market`

unrealized 部分：

- 来自链上持仓 `token_id` 的当前 unrealized
- 先按 token 映射到 leader 权重
- 再按 `condition_id` 汇总到 market

与 `ct_leader_summary` 的关系：

- `ct_leader_summary.total_realized_pnl = SUM(ct_leader_market_pnl.total_realized_pnl)` 按 leader 聚合
- `ct_leader_summary.total_unrealized_pnl = SUM(ct_leader_market_pnl.total_unrealized_pnl)` 按 leader 聚合

注意：

- 这两张表都是“当前快照”
- 不是日增量表

## 4) 日台账层

### 4.1 `ct_daily_leader_market_leg_pnl`

粒度：

- `date_key + account_name + leader_address + condition_id + token_id`

这是当前系统中的日归因真源。

它记录的是某个 leader 在某个 market leg（精确到 token）在某一天的：

- 当天发生了什么：
  - `buy_fill_count`
  - `buy_size`
  - `buy_cost_usd`
  - `sell_fill_count`
  - `sell_size`
  - `sell_proceeds_usd`
  - `settled_size`
- 当天归因增量：
  - `realized_pnl_delta`
  - `unrealized_pnl_delta`
  - `total_pnl_delta`
- 当天收盘状态：
  - `open_size_eod`
  - `close_state_eod`
  - `realized_pnl_eod`
  - `unrealized_pnl_eod`
  - `total_pnl_eod`

其中 `close_state_eod` 的语义：

- `open`：收盘仍持有
- `sold`：通过卖出关闭
- `settled`：通过到期 / 赎回关闭
- `redeemable`：市场已结束但仓位仍可赎回
- `mixed`：当天同时存在 open / sell / settled 的混合状态
- `flat`：没有剩余仓位，也无明显持仓状态

### 4.2 `ct_daily_leader_pnl`

粒度：

- `date_key + account_name + leader_address`

含义：

- 该 leader 在这一天的日增量汇总，不是累计快照

公式：

- `realized_pnl = SUM(realized_pnl_delta)` 按 leader/day 聚合
- `unrealized_pnl = SUM(unrealized_pnl_delta)` 按 leader/day 聚合
- `total_pnl = realized_pnl + unrealized_pnl`
- `market_count = 当天有归因记录的 distinct market 数`

当前约束：

- 最终落库结果必须满足  
  `ct_daily_leader_pnl = SUM(ct_daily_leader_market_leg_pnl)` 按 `date_key + account_name + leader_address` 聚合

## 5) 日归属是怎么生成的

### 5.1 历史区间回填

先执行一次历史 daily 回填：

- 基线起点：`2026-03-18`
- 实际回填起点：`min(2026-03-18, today_utc8 - 13 days)`
- 回填终点：昨天（UTC+8）

历史 realized：

- 从 `ct_trades.profit` 回放
- 按有效 realized 日期归到对应 `date_key`
- realized 日期优先级：
  - 若是 resolution exit，则优先 `official_settlement_at`
  - 否则使用 `exit_at`

历史 unrealized：

- 基线期默认不重新推导历史链上浮盈
- 主要依赖保留的 open-history / preserved rows 承接 cutover 之后的未平仓历史

### 5.2 当天 provisional target

构建当天时，会先基于当前快照表生成一份 provisional 的 `ct_daily_leader_pnl`：

- `current realized = ct_leader_summary.total_realized_pnl`
- `current unrealized = ct_leader_summary.total_unrealized_pnl`
- `prev realized = 历史 ct_daily_leader_pnl realized 累加`
- `prev unrealized = 历史 ct_daily_leader_pnl unrealized 累加`
- 得到：
  - `realized_delta = current_realized - prev_realized`
  - `unrealized_delta = current_unrealized - prev_unrealized`

这一步的目标不是最终落库，而是给 leg rebuild 提供“leader/day 目标值”。

### 5.3 重建 `ct_daily_leader_market_leg_pnl`

重建 leg 台账时会综合以下信息：

- `ct_trades`
- 当前 open token 的精确 unrealized
- 结算日期 / settlement time
- 已保留的 open history
- 上一步 provisional `ct_daily_leader_pnl`

核心逻辑：

1. 把每笔 `ct_trades` 规格化成 leg：
   - 归一成 `account_name + leader_address + condition_id + token_id`
   - 同时推导 original size / original cost / sold size / settled size / remaining size
2. 收集每个 leg 在每一天的事件：
   - BUY 发生日
   - SELL 发生日
   - SETTLED 发生日
   - REALIZED 发生日
3. 对历史日：
   - realized 优先按真实 `profit_total` 落到 realized day
   - unrealized 按 open leg / open cost 权重分配到 candidate legs
   - 若市场已结算，历史残留 unrealized 会被冲回 0
4. 对当前日：
   - 若能取到当前 open token 的精确链上 unrealized，则直接以精确 EOD 值为准
   - 不再把 residual 强塞进最后一条 leg
5. 对 preserved open history：
   - 若当天没有真实 close / settle / realized 事件，可以沿用 preserved row
   - 一旦当天确实发生 close / settle / realized，则丢弃旧 preserved row，改用真实事件重建

最后如果当日所有 leg 的和与 target 仍有极小残差：

- `realized_diff` 会补到最后一条 emitted leg
- `unrealized_diff` 只在非 preserved-history、非 exact-current-unrealized 场景下补到最后一条 emitted leg

### 5.4 从 leg 汇总回 `ct_daily_leader_pnl`

leg 台账重建完成后，再按 leader/day 聚合回 `ct_daily_leader_pnl`，并覆盖之前的 provisional daily。

所以最终口径是：

- 运行中：`ct_daily_leader_pnl` 可能暂时是 target
- 运行后：`ct_daily_leader_pnl` 一定是 `ct_daily_leader_market_leg_pnl` 的聚合结果

## 6) 这四张表应该怎么用

如果你要看“当前累计归因”：

- 用 `ct_leader_summary`
- drill 到 market 用 `ct_leader_market_pnl`

如果你要看“某天归到哪一天、哪一个 market leg 出的问题”：

- 先用 `ct_daily_leader_pnl` 找 leader/day
- 再 drill 到 `ct_daily_leader_market_leg_pnl`

如果你要排查归因 bug：

- 优先查 `ct_daily_leader_market_leg_pnl`
- 不要只看 `ct_daily_leader_pnl`
- 因为最终 daily 主表只是 leg 明细的聚合视图

## 7) 当前已有文档情况

目前仓库里已有两份相关文档，但都不是这份文档的替代品：

- `CLAUDE.md`
  - 有长期规范片段
  - 偏项目总规范
- `ATTRIBUTION_SELF_CHECK.md`
  - 偏约束、自检 SQL、迁移说明
  - 不是完整的归因实现说明

本文件 `ATTRIBUTION_LOGIC.md` 才是面向“归因逻辑本身”的说明。
