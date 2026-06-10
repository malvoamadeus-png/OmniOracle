# Legacy address metrics pipeline

这是 PolySport 早期“找好地址/地址测评/批量指标入库”的历史管线，已经不属于当前核心直接跟单产品。

当前核心产品只维护：

- `backend/packages/copytrade/`：直接跟单与跟单详情记录。
- `frontend/dashboard/` 和 `frontend/admin/`：展示和本地管理。

本目录保留这些脚本是为了复盘旧指标口径、必要时重跑历史地址测评，或给 `tools/smart_money_broadcast/` 导出跟单价值阈值快照。

## 运行

```bash
cd tools/legacy_address_metrics
python3 polymarket_master_metrics.py --config polymarket_master_config.json
python3 polymarket_metrics.py --address 0x... --db metrics_fresh.sqlite --progress
```

如果要把跟单价值阈值导出给 smart-money 工具，`polymarket_master_metrics.py` 会写入：

```text
../smart_money_broadcast/config/copytrade_value_thresholds.json
```

少数历史兼容功能仍会访问 PolySport 项目根目录，例如 `--sync-supabase` 会调用 `../../supabase/sync_to_supabase.py`，copytrade 评分会读取 `../../backend/packages/copytrade/accounts/`。这些只用于旧流程兼容，不应作为新工具设计模式。

## 边界

- 不要从核心 backend import 本目录脚本。
- 不要把本目录的新研究逻辑接回生产跟单链路。
- 如果后续要继续做地址测评产品化，应优先在 `tools/smart_money_broadcast/` 内保持独立实现。

## Open 持仓执行适配指标

地址指标表会额外写入两个只展示、不参与跟单价值评分的字段：

- `avg_open_top5_depth_usd`：仅统计 open/unresolved positions。对每个 position 的 token 拉 CLOB orderbook，计算 asks 前 5 档美元深度 + bids 前 5 档美元深度，地址级取算术平均。
- `avg_open_settlement_days`：仅统计 open/unresolved positions。用 position slug 查询 Gamma market/event，取预计结束/结算时间距当前 UTC 的天数，地址级取算术平均。

为节约历史补数和日常更新成本，这两个字段遵循和前端地址页一致的正利润门槛：只有 `total_pnl >= 80000` 的地址才会获取；低于门槛的地址字段保持为空，`details_json.openExecution` 会记录跳过原因。它不会改变 Sharpe、MDD 等既有指标的计算口径。

`details_json` 会保留辅助计数：`open_positions_analyzed`、`open_positions_missing_book`、`open_positions_missing_settlement`。这两个新字段当前只用于前端人工判断，不进入 `copytrade_value_score`，也不会触发 `not_worth_copying` 排除。

已有地址可用轻量 backfill 补数：

```bash
python3 tools/legacy_address_metrics/backfill_open_execution_metrics.py --db metrics_fresh.sqlite
```

回填默认每个地址只按 open position size 从大到小抽样最多 200 个 token/market 估算，并把 orderbook/Gamma 结果缓存到本地 SQLite 表 `pm_open_execution_market_cache`，3 天内复用。常用参数：

- `--max-markets-per-address 200`：单地址最多采样市场数，`0` 表示不限制。
- `--cache-days 3`：本地 market/book 缓存有效天数。
- `--limit N`：只补前 N 个仍缺字段且过利润门槛的地址。
