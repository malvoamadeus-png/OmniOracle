# 跟单归因口径自检（纯归因版）

## 1) 核心原则

- 归因只使用 copytrade 自身可追溯数据，不对齐官方 `/user-pnl`。
- 归因来源仅允许来自“被系统执行的跟单交易”：
  - `ct_trades.status='filled'`
  - `ct_trades.our_side='BUY'`
  - 具备有效 `leader_address`
- 日盈亏只表达归因增量，不表达“账户官方口径”的强制对齐结果。

## 2) 快照计算（leader summary / market）

- Open（未实现）：
  - 真值来自链上持仓（positions API）。
  - 单 token 浮盈：`currentValue - initialValue`；不可用时回退 `cashPnl`。
  - token -> leader 映射来自 open BUY（`exit_status='open'`）并按 `our_usd` 权重分摊。
- Closed（已实现）：
  - 真值来自 `ct_trades.profit`。
  - 先按 `leader + market(condition_id/token fallback)` 聚合，再汇总到 leader。

## 3) 归因主链路（2026-04-23 起）

- 单一真源是 `ct_daily_leader_market_leg_pnl`。
- `ct_daily_leader_pnl` 是派生表，不再直接作为归因输入真值。
- 现行顺序：
  1. `build_snapshots()` 生成最新 `ct_leader_summary / ct_leader_market_pnl`
  2. `_build_daily_leader_deltas()` 只生成“当天 provisional leader target”，用于把当天 summary 分配到 leg
  3. `_rebuild_daily_leader_market_leg_pnl()` 结合 `ct_trades`、链上 open unrealized、settlement time、保留的 open history，重建逐日逐 leg 台账
  4. `_build_daily_leader_rows_from_leg_rows()` 再把 leg 台账按 `date_key + account_name + leader_address` 聚合回 `ct_daily_leader_pnl`
- 强约束：
  - 看板主表和 drilldown 明细必须来自同一套 leg 台账
  - 必须满足 `ct_daily_leader_pnl = SUM(ct_daily_leader_market_leg_pnl)`（按 leader/day 聚合）
- 日切口径为 UTC+8。

## 4) 2026-03-18 基线与历史重建

- 纯归因迁移时，一次性重建历史 daily：
  - 区间：`2026-03-18` 到“昨天”（UTC+8）
  - 仅回放 closed realized：
    - `status='filled' AND our_side='BUY'`
    - `exit_status='exited'`
    - `profit IS NOT NULL`
    - 有效 realized 时间优先使用 `official_settlement_at`，否则使用 `exit_at`
  - 该区间默认 `unrealized_pnl=0`
- `2026-04-08` 起允许保留 open history，用于承接迁移后的未平仓 leg 历史。
- 强约束：不把 `2026-03-18` 之前的 realized 历史注入迁移后的 daily 增量。

## 5) 快速自检

- 检查是否仍有官方缩放逻辑残留：
  - `rg "user-pnl|normalize_daily|official_daily" copytrade/build_leader_pnl_snapshot.py`
- 检查主表是否严格等于 leg 明细聚合：
  - `SELECT d.date_key, d.account_name, d.leader_address, d.realized_pnl, d.unrealized_pnl, x.realized_pnl AS leg_realized, x.unrealized_pnl AS leg_unrealized FROM ct_daily_leader_pnl d JOIN (SELECT date_key, account_name, leader_address, SUM(realized_pnl_delta) AS realized_pnl, SUM(unrealized_pnl_delta) AS unrealized_pnl FROM ct_daily_leader_market_leg_pnl GROUP BY date_key, account_name, leader_address) x ON x.date_key=d.date_key AND x.account_name=d.account_name AND x.leader_address=d.leader_address WHERE ABS(d.realized_pnl-x.realized_pnl)>1e-6 OR ABS(d.unrealized_pnl-x.unrealized_pnl)>1e-6;`
- 检查历史区间 unrealized 是否全为 0：
  - `SELECT COUNT(*) FROM ct_daily_leader_pnl WHERE date_key>='2026-03-18' AND date_key<date('now','localtime') AND ABS(unrealized_pnl)>1e-9;`
- 检查历史 realized 是否可由 exited-profit 聚合复算：
  - 按 `date(realization_at@UTC+8), account_name, leader_address` 与 `ct_daily_leader_pnl` 对比

## 6) Reconcile And Phantom Recovery (2026-03-24)

- `reconcile_redeemed_positions` 不再把“链上消失但未解析”的仓位直接写成 0 盈亏 phantom closed row。
- 对 unresolved missing-on-chain rows 的现行为：
  - 保持 `status='filled'` 且 `exit_status='open'`
  - 标记 `skip_reason='pending_settlement: not on chain and unresolved'`
  - 等待 settlement price / settlement time 补齐后再关闭
- 历史恢复步骤：`repair_phantom_positions`
  - 目标行：`status='expired' AND exit_status='exited' AND skip_reason LIKE 'phantom:%'`
  - 如果现在能拿到 settlement price，则恢复成 realized closed row，并写入非空 `exit_at`
  - 仍不可恢复的行保持不动，但会记录统计
- Settlement date 优先级：
  1. Gamma market settlement / closed time（`closedTime`, `endDate`, `umaEndDate`）
  2. 本地 `updated_at`
  3. 当前 UTC 时间

## 7) Dynamic 14-Day Backfill Window (2026-03-24)

- Daily history rebuild 使用动态 effective start：
  - `effective_start = min(PURE_DAILY_BASELINE_DATE, today_utc8 - 13 days)`
  - rebuild range 为 `effective_start .. yesterday_utc8`
- 这样可以保证至少可重建 14 个自然日，同时保留 baseline anchor。
- `ct_meta` 关键字段：
  - `daily_leader_pure_baseline_date`：固定 baseline anchor
  - `daily_leader_pure_effective_start`：最近一次 rebuild 实际使用的起点
  - `daily_leader_pure_last_rebuild`：最近一次 rebuild 时间

### Validation SQL

- Check effective start marker:
  - `SELECT key, value FROM ct_meta WHERE key IN ('daily_leader_pure_baseline_date','daily_leader_pure_effective_start','daily_leader_pure_last_rebuild');`
- Check daily table covers at least 14 natural days for account `main`:
  - `SELECT MIN(date_key), MAX(date_key), COUNT(DISTINCT date_key) FROM ct_daily_leader_pnl WHERE account_name='main';`
- Check historical unrealized is zero up to yesterday:
  - `SELECT COUNT(*) FROM ct_daily_leader_pnl WHERE date_key < date('now','+8 hours') AND ABS(unrealized_pnl) > 1e-9;`

## 8) One-Shot Daily Gap Diagnosis (2026-03-26 / 2026-03-27)

- Script: `diagnose_leader_official_daily_gap.py`
- Purpose:
  - compare official `/user-pnl` daily deltas vs `ct_daily_leader_pnl` deltas
  - print `ct_meta` rebuild markers for baseline evidence
  - quantify unmapped chain exposure（official includes it, leader-attribution does not）
  - show day-cut timing evidence（official sample timestamp vs local daily row update time）
- Example:
  - `python diagnose_leader_official_daily_gap.py --account main --dates 2026-03-26 2026-03-27 --tz-offset +08:00 --json-out analysis_output/leader_official_gap_20260326_20260327.json`
