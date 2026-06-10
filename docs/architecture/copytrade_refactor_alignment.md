# Copytrade 重构对齐文档

本文件是 `copytrade` 后续重构、测试补齐和迁移实现的准绳。若代码实现与本文件冲突，以本文件为准，先改测试和文档再改交易路径。

## 已锁定范围

- `copytrade` 是唯一核心交易域；根目录其他 Python 脚本、公开 dashboard、同步脚本都视为辅助能力。
- 目标目录按 `初始架构.md` 收敛为 `backend/`、`frontend/`、`data/`。核心交易代码已迁入 `backend/packages/copytrade`；旧根路径 `copytrade/` 仅作为临时 junction 兼容入口。
- `backend/src` 只放 CLI、API、scheduler、worker 启动入口；核心交易逻辑进入 `backend/packages/copytrade`。
- 前端按权限边界拆分为两个 app：`frontend/admin` 是本地管理后台，可写配置、凭证和维护操作；`frontend/dashboard` 是对外只读展示，继续只读 Supabase，不接触本地凭证和管理 API。
- 旧 gap analytics 产品彻底移除，只保留“日盈亏对比”和“Leader 归因”两条展示与同步链路。

## 配置规则

- 多账户唯一来源为 `backend/packages/copytrade/accounts/{account}.toml`，默认值来自 `backend/packages/copytrade/accounts/_defaults.toml`。
- 合并优先级固定为：代码默认值 < `_defaults.toml` < account TOML < `leader_overrides`。
- `leader_overrides` 继续放在账户 TOML 中，但不得覆盖账户级 Auto TP 开关。
- 凭证留在根 `.env`，管理后台可以写入，但展示和审计必须脱敏。
- 缺凭证账号标记为 disabled 并告警，不影响其他账号启动。
- 配置保存失败必须保留旧配置；配置、凭证、维护操作都写审计记录，审计不得记录明文密钥。
- `leader_addresses` 修改后继续要求 worker 重启生效，后台需要明确标记 `restart_required`。

## 信号规则

- 保留来源：`activity`、`subgraph`、`hybrid`、`stream`、`stream_hybrid`。
- `subgraph` 是正式来源；`stream_hybrid = stream 实时 + activity 补漏`。
- 所有来源统一归一为 `LeaderSignal`，并用 `leader_fill_key` 去重。
- pending 信号最多等待 5 分钟；补漏回看窗口为 15 分钟。
- 多来源冲突时按 `subgraph > stream > activity` 补全字段。
- 未进入真实下单生命周期的信号不得写入 `ct_trades`，只进入 signal audit 或 signal attempt。

## 聚合与决策

- 保留 `maker-like` 和 `execution_episode` 两种聚合模式。
- 聚合后产生一个逻辑 BUY 信号。
- `aggregated_leader_usd` 只表示 leader 聚合成交金额，只用于最小信号阈值和 `proportional` sizing。
- `fixed_usd` 模式下，每个聚合信号只下固定金额一次，不按聚合金额放大。
- 未达到聚合阈值的碎单写轻量 signal audit，不进入 `ct_trades`、仓位、PnL 或进入次数。
- 删除 `delayed_follow`、`additional_usd_amount`、`additional_proportional_cap`、`fixed_shares`。
- 正常 copy mode 只保留 `fixed_usd` 和 `proportional`。
- `market_limit_mode` 和 `max_entries_per_market` 改为 token 级；聚合信号通过风控并提交交易所订单时，才消耗一次进入次数。

## 执行与约束

- 所有订单目的共用交易所约束：普通 BUY、mirror SELL、Auto TP、Auto rebuy。
- 最小 shares 优先从市场或 orderbook 读取，失败回退 5 shares。
- BUY 不足最小 shares 直接跳过并记录；SELL、TP 不足则进入 `min_size_pending`，可按同 token 同价格桶聚合。
- pricing mode 只保留 `aggressive`、`original`、`passive`，删除 `super_follow`。
- 普通跟单 TIF 为 2 小时 GTD；Auto TP 和 Auto rebuy 为 GTC。
- 余额或 allowance preflight 失败时触发维护；维护后重新检查价格、市场、风控、余额，并在有效窗口内只重试原信号一次。
- 所有 fill 来源统一为 `OrderFillEvent`；只接受正向 cumulative matched size 增量。
- partial fill 必须立即更新 spend、position 和 Auto TP lot，订单状态显式包含 `partially_filled`。

## Auto TP 与退出

- Auto TP 是账户级配置，不支持 per-leader override。
- 任何 BUY fill delta 都创建 TP lot。
- entry > 0.7 不创建 TP；entry < 0.4 时目标价为 `min(1, entry * 2)`，卖出 40%；0.4 到 0.7 之间线性映射到目标价 0.8 到 0.9、卖出比例 40% 到 20%。
- leader SELL 或 mirror SELL 优先于 TP：先取消或暂停同 token TP/rebuy，再执行 mirror sell。
- 退出策略只保留 `mirror_sell` 和 `hold_to_resolution`，旧轮询 `take_profit` 删除；主动止盈统一由 Auto TP 负责。
- leader partial SELL 按 `leader_sell_size / (leader_remaining + leader_sell_size)` 计算比例；没有 remaining 时跳过并记录。
- redeem/merge 每 2 小时自动运行，也在资金不足时触发；不自动 wrap/unwrap；失败记录并下次重试，不阻塞交易。

## PnL 与同步

- `ct_daily_leader_market_leg_pnl` 是日归因真值。
- `ct_daily_leader_pnl` 只做汇总，不再承载底层归因逻辑。
- 保留 2026-03-18 baseline、动态 14 天 backfill、2026-04-08 open cutover。
- 官方 `/user-pnl` 只用于对比诊断，绝不缩放或分配 leader 归因。
- 同步到 Supabase 时只同步 dashboard 需要的只读表或视图：Leader 归因、日盈亏对比、必要状态摘要。
- Supabase 不暴露本地配置、凭证或管理 API；按 `account_name` 先删后写，保持镜像同步。

## 数据契约

- 核心域类型：`LeaderSignal`、`AggregatedSignal`、`CopyDecision`、`OrderPlan`、`OrderFillEvent`、`PositionLot`、`RuntimeEvent`。
- `ct_trades` 只记录已进入真实交易所下单或订单生命周期的交易。
- 未触发、风控拒绝、pending unresolved、min size skip、order failed 等进入 `ct_signal_attempts` 或 `ct_signal_audit`。
- 运行可观测数据统一进入结构化心跳、运行事件、告警状态、配置审计；日志文件继续保留原始异常上下文。

## 目标目录

```text
backend/
  src/
    README.md
  packages/
    copytrade/
      README.md
frontend/
  admin/
    README.md
  dashboard/
    README.md
data/
  raw/
  processed/
  exports/
```

## 当前验证

- `copytrade`: `python -m py_compile domain.py config.py db.py executor.py risk.py monitor.py worker.py web\server.py exit_manager.py account_config.py main.py signal_hub.py user_order_hub.py ws_proxy.py`
- `copytrade`: `python -m pytest`
- `dashboard`: `npm run build`
