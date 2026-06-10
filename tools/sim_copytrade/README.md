# sim_copytrade

用于 Polymarket 领单地址的回放模拟、参数搜索、报告输出与 AI 解释。

这是一个研究工具，不属于生产直接跟单链路。复制整个 `tools/sim_copytrade/`
文件夹到其他机器后，只要安装 Python 依赖并可访问 Polymarket 公开 API，就可以独立运行。

## 独立性边界

- 不 import PolySport 根目录脚本。
- 不 import `backend/` 或生产 `copytrade` 包。
- 不读取生产跟单 SQLite、账户 TOML 或 Supabase。
- 公共 API 读取函数保存在本目录 `polymarket_public_api.py`。

## 当前核心口径
- 先做 maker-like BUY 聚合，再触发跟单信号。
- 仅保留 `mirror_sell` 出场。
- 优化目标：`ROI 主目标 + 规模次目标(total_buy_cost)`。
- ROI 同档阈值：`0.001`（0.10%）。
- 默认策略网格：
  - `fixed_usd`: `10,50,100`
  - `proportional_pct`: `0.01,0.03,0.05`
  - `proportional_cap_usd`: `10,50,100`
  - `max_entries_per_market`: `1..10`

## 反虚假放大约束（默认开启）
- 单笔约束：`our_usd <= leader_usd * per_trade_limit`
- 单市场累计约束：`our_market_buy_usd <= leader_market_buy_usd * per_market_limit`
- 超限行为：优先裁剪，额度不足则跳过
- 新增参数：
  - `--anti-amplification-guard` / `--no-anti-amplification-guard`
  - `--max-our-vs-leader-per-trade`（默认 `1.0`）
  - `--max-our-vs-leader-per-market`（默认 `1.0`）

## 运行示例
```bash
python tools/sim_copytrade/main.py \
  --address 0xYourLeaderAddress \
  --max-activities 300000 \
  --ai-execute-improve \
  --ai-improve-rounds 8 \
  --ai-improve-top-candidates 64 \
  --ai-improve-budget-minutes 45 \
  --ai-improve-bound-profile aggressive
```

## 输出文件
- `sim_results_*.json`
- `sim_results_*.csv`
- `report_*.pdf`
- `report_*.html`
- `analysis_*.md`
- `analysis_*.json`

## 新增关键输出字段
- `meta.best_by_objective`（唯一主冠军）
- `meta.best_by_raw_roi`（次要参考）
- `meta.amplification_guard_summary`
- `meta.oversize_event_rate`
- `meta.entries_depth_evidence.top_market_contributors`

## AI 报告
- live 运行后默认自动生成 `analysis_*.md/.json`
- 支持历史结果重生：
```bash
python tools/sim_copytrade/ai_report.py \
  --sim-json tools/sim_copytrade/output/sim_results_xxx.json \
  --gap-json tools/sim_copytrade/output/gap_analysis_xxx.json
```

## 诊断工具（可选）
```bash
python tools/sim_copytrade/gap_diagnose.py \
  --sim-json tools/sim_copytrade/output/sim_results_xxx.json \
  --top-k 3
```

## 方向差额补单回测（PnL 优先）
```bash
python tools/sim_copytrade/directional_topup_grid.py
```

默认口径：
- `threshold=2000`, `window=60m`, `skip_n=8`, `max_follows=20`, `fixed_copy_usd=150`
- 补单公式：`min(cap, max(0, A_usd - B_usd) * k)`，其中 `A/B` 为观察期前 8 次触发的两方向累计 USD
- 预算约束：`total_copy <= 2x baseline`

可选参数示例：
```bash
python tools/sim_copytrade/directional_topup_grid.py \
  --copy-multiplier-limit 2.0 \
  --k-values "0.002,0.005,0.01,0.02,0.04,0.06,0.08" \
  --cap-values "75,150,300,600,900,1200,1800,2400"
```

输出：
- `output/directional_topup_grid_*.json`
- 包含全局指标（Copy/PnL/ROI/Delta）、补单分布统计（p50/p75/p90/p95）和对向拖累 Top 市场。
