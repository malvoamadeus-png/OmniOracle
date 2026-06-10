# 用户指标说明（user_metrics JSON）

输出文件由 `export_user_positions_and_metrics.py` 生成，包含两个顶层部分：`metrics`（数据库存储指标）和 `pnlStats`（实时 PnL 曲线衍生指标）。

---

## metrics — 数据库指标

| 字段 | 类型 | 说明 |
|------|------|------|
| address | string | 用户钱包地址（小写） |
| snapshotUtc | string | 指标快照时间（UTC ISO） |
| updatedAt | string | 最后更新时间（UTC ISO） |
| totalPnl | float | 总盈亏（USD），按市场聚合的 cashPnl 之和 |
| realizedPnl | float | 已实现盈亏（已平仓部分） |
| unrealizedPnl | float | 未实现盈亏（totalPnl - realizedPnl） |
| roi | float | 投资回报率 = totalPnl / costBasis（所有 BUY 的 USD 总额） |
| profitFactor | float | 盈利因子 = 盈利市场总利润 / 亏损市场总亏损，>1 表示整体盈利 |
| maxDrawdown | float | 最大回撤比率，基于 PnL 曲线的峰值到谷底跌幅百分比 |
| sharpe | float | 夏普比率 = mean(每期PnL变化) / std(每期PnL变化) × √年化周期数，衡量风险调整后收益 |
| ulcerIndex | float | 溃疡指数 = sqrt(mean(D²))，D = (当前值 - 历史最高值) / 历史最高值 × 100；越小越好，衡量下行波动的持续性和深度 |
| equityR2 | float | 净值曲线 R²（决定系数），PnL 曲线与完美直线的拟合度；1.0 = 完美线性增长，越高说明盈利越稳定 |
| totalTrades | int | 总交易次数（活跃仓位数 + 已关闭仓位数） |
| winningTrades | int | 盈利仓位数（cashPnl > 0） |
| losingTrades | int | 亏损仓位数（cashPnl < 0） |
| winRate | float | 胜率 = winningTrades / 有 cashPnl 的仓位总数 |
| avgTradePrice | float | 加权平均交易价格，权重为每笔仓位的 totalBought（USD） |
| currentPositionValueUsd | float | 当前持仓总市值（USD），来自 Data API /value 接口 |
| confidence | string | 数据置信度：`high`（完整数据）、`medium`（缺少 costBasis）、`low`（无市场PnL）、`skipped_low_pnl`（总PnL绝对值 < 80000 跳过深度计算） |
| sourceTags | string | 数据来源标签，逗号分隔（如 `NBA,CLIMATE`） |
| detailsJson | object | 扩展详情，包含 pnlCurveLast、maxDrawdownUsd、drawdownPeakPnlUsd、positionBased 等子字段 |

---

## pnlStats — PnL 曲线衍生指标

数据来源：`USER_PNL_API /user-pnl`（interval=all, fidelity=12h）

| 字段 | 类型 | 说明 |
|------|------|------|
| currentPnl | float | 当前累计 PnL（曲线最新值） |
| accountAgeDays | int | 账号活跃天数 = 当前时间 - PnL 曲线最早数据点，单位天 |
| firstActivityUtc | string | 最早活动时间（UTC ISO），即 PnL 曲线的起点 |
| pnlCurvePoints | int | PnL 曲线数据点总数 |
| pnl30d | float | 近 30 天 PnL = currentPnl - 30天前的PnL值 |
| pnl30dGrowthRate | float | 近 30 天 PnL 增长率 = currentPnl / pnl_30d_ago - 1；例如 0.5 表示增长 50% |
| pnl90d | float | 近 90 天 PnL = currentPnl - 90天前的PnL值 |
| pnl90dGrowthRate | float | 近 90 天 PnL 增长率 = currentPnl / pnl_90d_ago - 1 |

注：如果账号历史不足 30/90 天（曲线中找不到对应时间点的数据），对应字段为 `null`。
