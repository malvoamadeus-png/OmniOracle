# 聪明钱播报工具

这是一个可复制运行的独立 Python CLI 工具，用来发现多个 Polymarket 板块下满足门控的地址、计算地址指标，并为单地址分析生成 Markdown 播报报告。

它不依赖 PolySport 根目录，不 import 外层脚本，不需要 Supabase，也不需要已有 SQLite。复制整个 `tools/smart_money_broadcast/` 文件夹到任意机器后，只要能访问 Polymarket 公开 API，就可以运行。

## 安装与启动

```bash
cd tools/smart_money_broadcast
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python cli.py
```

Windows PowerShell 激活命令：

```powershell
.\.venv\Scripts\Activate.ps1
```

Windows cmd 激活命令：

```bat
.venv\Scripts\activate
```

正常入口只有：

```bash
python cli.py
```

程序启动后会进入菜单，不需要记 `--target-count`、`--address` 之类参数。

## 菜单功能

主菜单包含：

1. 批量发现并计算指标
2. 单地址分析并生成报告
3. 查看本地缓存概况
4. 清空本地运行数据
5. 退出

## 批量发现

批量发现流程会依次询问：

- 要扫描哪些板块，可以选择一个或多个。
- 目标地址总数，多板块时是所有板块共享的总数。
- 最小地址年龄天数。
- 最小交易次数门控。
- 旧地址策略。

旧地址策略：

- `reuse_old_metrics`：默认。旧地址计入本次目标数，优先复用本工具 SQLite 中保存的上次指标；如果没有本地指标缓存，会重新计算。
- `skip_old`：旧地址不计入本次目标数，继续扫描新地址。
- `refresh_old_metrics`：旧地址计入本次目标数，但重新计算指标。

多板块扫描按用户选择顺序执行。同一个地址在多个板块命中时，本次目标数只计入一次，但会记录它属于哪些板块。

批量发现只负责拿地址、计算或复用指标并写入本地 SQLite，不生成 Markdown 排名报告。排名报告只在“单地址分析并生成报告”功能里生成。

## 单地址报告

单地址分析流程会依次询问：

- 地址。
- 对比板块，例如 NBA、LOL、MLB。
- 是否强制刷新该地址指标。

报告排行池只使用用户选择的单一板块，不做跨板块混排。

报告模板：

```md
过去30日盈利{pnl_30d_wan}万美元，总盈利{total_pnl_wan}万美元

信息优势能力在{board}板块排行前{edge_pct}%

盈利能力在{board}板块排行前{profit_pct}%

抗回撤能力在{board}板块排行前{drawdown_pct}%

Realized Edge分数{realized_edge_score}，投注回报率{roi}，夏普比{sharpe}，最大回撤{max_drawdown}

地址总盈亏{total_pnl_wan}万美元，胜率{win_rate}
```

缺失指标会显示 `暂无数据`，样本池不足或不可排行时显示 `暂无排行`。

## 内置板块目录

当前内置板块镜像自 PolySport 根目录主控预设：

- `NBA`
- `CLIMATE`
- `LOL`
- `CS2`
- `UCL`
- `CHAMPIONS LEAGUE`
- `SOCCER`
- `15M`
- `1H`
- `NHL`
- `CBB`
- `MLB`
- `CRICKET`

板块目录保存在本文件夹自己的代码中，不读取 PolySport 根目录配置。后续如果主控新增板块，需要手工同步这一份内置目录。

## 数据与缓存

运行数据全部保存在当前文件夹内：

- `runtime/smart_money.sqlite`：地址、板块归属、发现批次、指标快照、单地址报告记录。
- `output/`：生成的 Markdown 报告。

这两个目录的运行产物默认被 `.gitignore` 忽略。

## 发现逻辑

发现引擎支持三类板块来源：

- `sport/series`：通过 Gamma API `/sports` 找 series，再遍历 series 下的 events 和 markets。
- `tag/related_tags`：通过 Gamma API 按 tag 拉取 events 或 markets。
- `slug_prefix`：用于 15M、1H 这类滚动市场，按当前时间生成 slug 后拉取 event。

每个 market 会调用 Data API `/trades`，从交易记录中抽取 `proxyWallet` 候选地址，再应用门控。

门控口径：

- 地址年龄：最早 `user-pnl` 点到当前时间的天数，必须 `>= min-age-days`。
- 交易次数：`/v1/user-stats` 的 `trades` 必须 `> min_trades`。
- 默认隐藏上限：`trades <= 30000`，避免极端高频地址污染。

如果发现数量不足，CLI 会输出实际数量和过滤失败原因摘要。

## 指标口径

指标计算在 `metrics.py` 内自包含实现，不调用外部 PolySport 脚本。

当前实现已尽量与根目录 `polymarket_metrics.py` 的主指标口径对齐，但这里只保留本 CLI 实际使用的那批指标；不会额外引入主脚本里未展示的扩展指标。

为控制大地址耗时，`closed-positions` 读取条数默认上限为 `7500`。这是一项可调参数，当前默认值会写入本地指标快照，用于避免旧口径缓存被误复用。

### 与主脚本对齐范围

当前版本可以理解为“核心展示指标口径对齐”，而不是“完整复制主脚本全部能力”。

已对齐或基本对齐的部分：

- `roi / profit_factor / win_rate / avg_trade_price` 都基于 open + closed positions 计算。
- `closed-positions` 在指标层会把 `realizedPnl` 视作 closed row 的 `cash_pnl`，与主脚本一致。
- `realized_edge_score` 会计入 open + closed positions：
  - open position 优先用 `cur_price`，必要时回退 `current_value / size`
  - closed position 优先看 `0/1` 兑付，再回退 `realized_pnl` 的经济推断
- `max_drawdown / sharpe / pnl_30d` 都来自 `user-pnl` 曲线。
- `ulcer_index` 复用主脚本的 user-pnl 曲线口径。
- `current_position_value_usd` 来自 Data API `/value`，用于跟单价值硬性排除。

有意保留的差异：

- 报告里的 `total_pnl` / 总盈利展示，优先使用官方 `user-pnl` 最新值。
- 报告里的 `roi` / 投注回报率，仍然使用 position 聚合口径：
  - 分子是 position 聚合后的 `cash_pnl total_pnl`
  - 分母是 open + closed positions 的 `total_bought` 之和
- 因此报告里“总盈利”和“投注回报率”不是同一个数据源：
  - 总盈利偏向“官方账户总盈亏展示”
  - ROI 偏向“可控样本下的仓位收益率”
- 为控制大地址耗时，`closed-positions` 默认只读取前 `7500` 条；主脚本的全量缓存同步能力没有完整搬入这里。
- 主脚本中的部分扩展能力，例如 `equity_r2`、更重的缓存同步状态与增量回补机制，没有在本工具里暴露到 CLI 或报告中。

当前输出指标：

- `total_pnl`：优先使用 user-pnl 曲线最新值。
- `pnl_30d`：当前 user-pnl 减去 30 天前插值 user-pnl。
- `profit_factor`：市场级盈利总额 / 市场级亏损绝对值。
- `roi`：总 PnL / `total_bought` 成本基数之和。
- `max_drawdown`：user-pnl 曲线峰值到谷值最大回撤比例。
- `ulcer_index`：user-pnl 曲线回撤深度的均方根风险指标。
- `sharpe`：user-pnl 曲线相邻点变化的年化夏普近似。
- `total_trades`：本次可取到的 open + closed position 记录数。
- `win_rate`：可计算 PnL 的 position 记录中盈利占比。
- `avg_trade_price`：按投入金额加权的平均价格。
- `realized_edge_score`：统计 open + closed positions，按 USD 成本加权计算 `resolution/current_price - entry_price`；优先使用 `cur_price`，必要时回退到 `current_value/size` 或 closed position 的 `realized_pnl` 经济推断。
- `copytrade_value_score / level / exclusion_reason`：读取本文件夹内部 `config/copytrade_value_thresholds.json`，按主流程刷新出的固定阈值打分；如果阈值文件缺失、为空或版本不匹配，报告显示“跟单价值：暂无数据”，CLI 输出明确提示。

排行口径：

- 信息优势能力：`realized_edge_score` 越高越好。
- 盈利能力：`total_pnl` 越高越好。
- 抗回撤能力：基于板块 cohort 内 `max_drawdown` 与 `ulcer_index` 的综合 `resilience_score`，分数越高越好。

抗回撤综合分仅在 `smart_money_broadcast` 报告层计算，不写回主库、Supabase 或前端表字段：

- `max_drawdown` 与 `ulcer_index` 都属于“越小越好”的风险指标。
- 抗回撤综合能力将不再只看 `max_drawdown`，而是基于两项排名分数组合得到 `resilience_score`。
- 组合公式为：

```text
resilience_score = 2 * mdd_score * ui_score / (mdd_score + ui_score)
```

- 其中：
  - `mdd_score` 表示把“最大回撤越小越好”转换后的同板块相对得分
  - `ui_score` 表示把“溃疡指标越小越好”转换后的同板块相对得分
- 采用该公式的目的，是让两项都优秀的地址获得更高抗回撤排名；若其中一项明显偏弱，综合分会被拉低，避免单项掩盖另一项短板。

## 跟单价值阈值文件

`smart_money_broadcast` 不会自建校准池，也不会运行时读取根目录 `metrics_fresh.sqlite` 或根目录配置。主流程每次刷新跟单价值阈值后，会把快照导出到：

```text
tools/smart_money_broadcast/config/copytrade_value_thresholds.json
```

复制 `tools/smart_money_broadcast/` 整个文件夹到其他机器时，这个 JSON 会随文件夹一起走。若该文件还是占位状态、缺失或版本不匹配，单地址报告不会强行打分，而是显示“跟单价值：暂无数据”。

## 测试

测试不访问真实网络，使用 mock API 和临时 SQLite：

```bash
cd tools/smart_money_broadcast
python -m unittest discover -s tests
```

也可以做语法检查：

```bash
python -m py_compile *.py
```

## 独立性边界

本工具只依赖：

- Python 标准库
- `requests`

禁止依赖：

- PolySport 根目录脚本
- `polymarket_metrics.py`
- `research/`
- `tools/sim_copytrade/`
- Supabase
- 外部已有 SQLite
