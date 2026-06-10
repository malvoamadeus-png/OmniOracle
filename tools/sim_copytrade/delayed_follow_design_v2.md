# 延迟跟单策略（Delayed-Follow）— 功能设计文档

## 背景：为什么需要这个功能

### 问题

部分 leader（如 0xee613b）在 Polymarket 上长期稳定盈利（+$3.2M），但现有跟单系统跟他的所有策略都亏钱。

经过对 80 万笔历史交易的深度分析，发现根因：

1. **这类 leader 的交易极度高频**（日均数万笔），大量是小额试探性下注
2. **利润集中在"大仓位"市场**（累积投入 >$10K 的市场 ROI +2.45%），小仓位市场 ROI 仅 +1.06%
3. **现有跟单系统无差别跟随所有信号**，导致大量资金浪费在低质量的小仓位上，手续费和摩擦吃掉了微薄的 alpha
4. **这类 leader 还会双向下注**（同一市场买 YES 和 NO），进一步稀释了跟单收益

### 核心发现

通过参数网格搜索（门槛 × 窗口 × 跳过次数 × 跟单方式），在 0xee613b 的 80 万笔历史交易上回测发现：

- **聚合门槛从 $300 提高到 $2000**：过滤掉大量小额噪声
- **跳过前 15 次触发**：等 leader 在一个市场上累积投入 ~$30K 后才开始跟，此时有 96% 的概率这是一个"大仓位"市场
- **最优 ROI 达到 +8.42%**，而无差别跟单的 ROI 约 +1-2%

关键数据支撑：

| 跳过次数 | 跟的市场数 | 精度(→大仓位) | ROI |
|---------|-----------|-------------|-----|
| 0（不跳过） | 13,194 | 25.5% | ~1.5% |
| 8 | 4,541 | 69.8% | ~3.3% |
| 15 | 2,089 | 96.3% | ~8.4% |

## 功能定义

### 这是什么

**延迟跟单（Delayed-Follow）是一个针对特定 leader 地址单独开启的特殊功能**，不是通用跟单策略。它通过"先观察、后跟随"的方式，只跟 leader 真正重仓投入的市场，过滤掉大量低质量信号。

### 不是什么

- 不是替代现有跟单逻辑，而是一个可选的增强模式
- 不适用于所有 leader，只适用于"高频 + 大量小单 + 利润集中在大仓位"类型的 leader
- 需要先通过 `deep_investigate.py` 和 `strategy_grid.py` 分析确认该 leader 适合此模式

## 具体实现方案

### 配置结构

在 leader_overrides 中新增 `delayed_follow` 配置块：

```toml
[leader_overrides.0xee613b3fc183ee44f9da9c05f53e2da107e3debf]
# 现有配置保持不变...
copy_mode = "proportional"
proportional_pct = 0.005
min_trade_size_usd = 300
# ...

# 新增：延迟跟单模式（开启后覆盖上面的常规跟单逻辑）
delayed_follow_enabled = true
delayed_follow_agg_threshold = 2000      # 聚合触发门槛（USD），回测范围 200~3000
delayed_follow_agg_window_minutes = 60   # 聚合时间窗口（分钟），回测范围 15~120
delayed_follow_skip_n = 8               # 前 N 次触发只观察不下单，回测范围 0~15
delayed_follow_copy_mode = "proportional" # "fixed" 或 "proportional"
delayed_follow_fixed_usd = 150          # fixed 模式下每次下单金额
delayed_follow_proportional_pct = 0.05   # proportional 模式下的比例
delayed_follow_proportional_cap = 150    # proportional 模式下的上限（USD）
delayed_follow_max_follows = 20          # 开始跟单后最多跟几次（锁死 20）
```

### 核心逻辑

#### 状态维护

对每个 `(leader_address, condition_id)` 维护一个计数器：

```
delayed_follow_state = {
    "condition_id": "0x...",
    "trigger_count": 0,          # 当前已触发次数
    "follow_count": 0,           # 已跟单次数
    "agg_window_start": null,    # 当前聚合窗口起始时间
    "agg_window_usd": 0,         # 当前窗口累积 USD
    "status": "observing"        # observing | following | done
}
```

#### 处理流程

当收到该 leader 的一笔 BUY 信号时：

```
1. 检查是否开启了 delayed_follow_enabled
   - 否 → 走常规跟单逻辑
   - 是 → 进入延迟跟单流程

2. 获取该 (leader, condition_id) 的 delayed_follow_state

3. 聚合窗口（与现有 maker-like 聚合逻辑相同）：
   - 将 15 分钟内的连续小单累加为一个"聚合窗口"
   - 收到新 BUY 信号时：
     - 如果距离窗口起始时间 > agg_window_minutes：说明中间断了，重置窗口（start = 这笔信号的时间, usd = 当前这笔）
     - 如果在窗口内：累加 agg_window_usd += leader_trade_usd
   - 示例：15min 内连续买了 $50+$30+$200+$1800 = $2080，算一次聚合

4. 触发判断：
   - 如果 agg_window_usd >= agg_threshold（如 $2000）：
     trigger_count += 1
     重置窗口，开始下一轮聚合
   - 如果窗口超时（>15min）仍未达标：窗口作废，不计入 trigger_count

   说明：`agg_threshold` 是**每次触发门槛**，不是“只在前期生效一次”的开闸门槛。
   也就是说，每次下单机会都需要先在当前窗口内再次累计到 `agg_threshold`。

5. 跟单判断：
   - 如果 trigger_count <= skip_n：
     status = "observing"，不下单，只记录
   - 如果 trigger_count > skip_n 且 follow_count < max_follows：
      status = "following"
      执行下单（fixed $75 或 proportional）
      follow_count += 1
   - 如果 follow_count >= max_follows：
     status = "done"，不再跟单

   示例（threshold=$2000, window=60m, skip_n=8）：
   - 第 1~8 次触发（每次都满足窗口累计 >= $2000）只观察不下单
   - 第 9 次触发开始下第一单
   - 此后也仍是“每次触发下 1 单”：例如某窗口只累计到 $1000 不触发不下单，下一窗口累计到 $2100 才触发并下单
```

#### 下单方式

开始跟单后（trigger_count > skip_n），每次触发时：

- **fixed 模式**：直接下单 `delayed_follow_fixed_usd`
- **proportional 模式**：下单 `leader_agg_window_usd × proportional_pct`，上限 `proportional_cap`
  其中 `leader_agg_window_usd` 指“本次触发窗口累计额”（即触发当刻的窗口总额），不是最后一笔 BUY 的单笔金额

下单方向：**不需要判断主方向**。直接跟随 leader 当前买的 token（YES 或 NO），与现有跟单逻辑一致。leader 买什么你就跟什么，两边都正常跟。

### 与现有系统的关系

**延迟跟单不需要跳过 maker-like 聚合。** 经过对比测试，在 maker-like 聚合后的信号上做延迟跟单计数，与直接在原始事件上计数的结果几乎一致（大仓位触发 8+ 次的市场数：952 vs 1001，差异 5%）。原因是延迟跟单的聚合窗口（$2000/60min）比 maker-like（$300/30min）更宽更高，小单在 60 分钟内自然累加到 $2000。

实现细节上，**延迟跟单窗口使用“拿到的信号对象时间”计算**，而不是本地程序实际处理到这条信号的时间。也就是说，只看 `LeaderTrade` 自带的时间戳；至于底层真实成交是否隔了 2 小时、程序是否晚轮询到，都不影响 delayed-follow 的窗口判定。

因此实现时直接在现有 maker-like 聚合信号之后接入延迟跟单逻辑即可，不需要改信号处理链路：

```
收到 leader BUY 信号
    │
    ├─ delayed_follow_enabled = false
    │   └─ 走现有跟单逻辑（maker-like 聚合、min_trade_size、比例跟单等）
    │
    └─ delayed_follow_enabled = true
        └─ 仍然经过 maker-like 聚合
            └─ 聚合后的信号进入延迟跟单逻辑（累积计数 → 跳过 → 跟单）
               copy_mode / market_limit_mode / max_entries_per_market 等常规跟单参数被忽略
               delayed_follow_max_follows 是唯一的重复跟单上限
               min_trade_size_usd 仍会影响前置 maker-like 聚合，因此会间接影响 delayed trigger 次数
               其它实际下单金额由 delayed_follow_* 参数控制
```

### 持久化

`delayed_follow_state` 需要持久化到数据库（建议新建表 `ct_delayed_follow_state`），因为：
- 跟单系统可能重启
- trigger_count 需要跨重启保持
- 需要能查询当前哪些市场在 observing / following / done 状态

```sql
CREATE TABLE ct_delayed_follow_state (
    leader_address TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    account_name TEXT NOT NULL DEFAULT 'main',
    trigger_count INTEGER NOT NULL DEFAULT 0,
    follow_count INTEGER NOT NULL DEFAULT 0,
    agg_window_start TEXT,
    agg_window_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'observing',  -- observing / following / done
    first_trigger_at TEXT,
    last_trigger_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (leader_address, condition_id, account_name)
);
```

## 参数说明与回测参考（基于 0xee613b 的 80 万笔交易）

### 参数说明

| 参数 | 含义 | 回测范围 | 影响 |
|------|------|---------|------|
| agg_threshold | 聚合窗口内累积多少 USD 才算"一次触发"（每次触发都重新判断） | $200~$3000 | **最敏感**。越高越能过滤小额噪声，但太高会漏掉中等仓位 |
| agg_window_minutes | 聚合窗口时长，窗口内的连续买入累加 | 15~120 min | 越短越严格（只捕获密集建仓），越长越宽松 |
| skip_n | 前 N 次触发只观察不下单 | 0~15 | **最敏感**。越大精度越高（跟的市场越少但越准），但绝对收益下降 |
| copy_mode | fixed（固定金额）或 proportional（比例跟单） | — | 比例跟单在小触发时自动减少金额，更安全；固定金额绝对收益更高 |
| fixed_usd | fixed 模式下每次下单金额 | $10~$150 | 越大绝对收益越高，但资金占用也越大 |
| proportional_pct | proportional 模式下跟随 leader 触发额的比例 | 0.5%~20% | 注意：5% × 触发额中位数 $737 ≈ $37/次，需要较高比例才能接近 fix$150 的效果 |
| proportional_cap | proportional 模式下单笔上限 | $50~$150 | 防止单笔过大 |
| max_follows | 开始跟单后最多跟几次 | 5~50 | 控制单个市场的最大资金敞口。20 次 × $150 = 最大 $3,000/市场 |

### 参数之间的关系

三个核心参数（threshold、window、skip_n）共同决定"精度 vs 覆盖"的权衡：

```
高门槛 + 短窗口 + 多跳过 = 极少市场、极高 ROI、低绝对收益
低门槛 + 长窗口 + 少跳过 = 很多市场、低 ROI、高绝对收益
```

copy_mode 和金额参数在 ROI 上差异不大，主要影响绝对收益和资金占用。

### 回测结果参考（maxf=20 锁定）

以下是不同风格的参考配置，基于 0xee613b 的 80 万笔历史交易回测。**参数需要根据具体 leader 重新调优。**

#### 按绝对收益排序（ROI >= 2%）

| 门槛 | 窗口 | 跳过 | 跟单方式 | 市场数 | 总投入 | PnL | ROI |
|------|------|------|---------|--------|--------|-----|-----|
| $2000 | 120m | 5 | fix$150 | 1,699 | $1,230K | +$25,957 | 2.1% |
| $2000 | 60m | 5 | fix$150 | 1,596 | $1,144K | +$24,329 | 2.1% |
| $2000 | 60m | 5 | p5%c150 | 1,596 | $1,007K | +$21,393 | 2.1% |
| $2000 | 120m | 8 | fix$150 | 845 | $624K | +$16,604 | 2.7% |
| $2000 | 60m | 8 | fix$150 | 782 | $577K | +$15,864 | 2.7% |
| $2000 | 60m | 8 | p5%c150 | 782 | $508K | +$13,969 | 2.7% |

#### 按 ROI 排序

| 门槛 | 窗口 | 跳过 | 跟单方式 | 市场数 | 总投入 | PnL | ROI |
|------|------|------|---------|--------|--------|-----|-----|
| $3000 | 15m | 15 | p0.5%c150 | 51 | $7K | +$854 | 11.5% |
| $3000 | 15m | 15 | p2%c150 | 51 | $26K | +$2,617 | 10.2% |
| $3000 | 15m | 15 | fix$150 | 51 | $44K | +$4,342 | 9.9% |
| $2000 | 15m | 15 | p0.5%c75 | 127 | $13K | +$1,039 | 8.2% |
| $2000 | 15m | 15 | fix$150 | 127 | $119K | +$8,967 | 7.5% |

#### Sweet Spot（ROI >= 2.5% 且 PnL >= $2,000）

| 门槛 | 窗口 | 跳过 | 跟单方式 | 市场数 | 总投入 | PnL | ROI | $/市场 |
|------|------|------|---------|--------|--------|-----|-----|--------|
| $2000 | 60m | 8 | fix$150 | 782 | $577K | +$15,864 | 2.7% | $20 |
| $2000 | 60m | 10 | fix$150 | 468 | $376K | +$13,358 | 3.6% | $29 |
| $2000 | 30m | 10 | fix$150 | 424 | $338K | +$12,449 | 3.7% | $29 |
| $2000 | 60m | 12 | fix$150 | 298 | $251K | +$11,630 | 4.6% | $39 |
| $2000 | 30m | 12 | fix$150 | 263 | $227K | +$11,355 | 5.0% | $43 |
| $2000 | 60m | 15 | fix$150 | 158 | $161K | +$10,222 | 6.4% | $65 |

#### 比例跟单 vs 固定金额（同一组参数下对比）

以 $2000/60m/skip8/max20 为例：

| 跟单方式 | 实际每次金额 | 总投入 | PnL | ROI |
|---------|------------|--------|-----|-----|
| fix$150 | $150 | $577K | +$15,864 | 2.7% |
| p5%c150 | ~$37（中位数） | $508K | +$13,969 | 2.7% |
| p10%c150 | ~$74 → cap $150 | $577K | +$15,864 | 2.7% |
| fix$100 | $100 | $385K | +$10,576 | 2.7% |
| fix$50 | $50 | $192K | +$5,288 | 2.7% |

结论：ROI 几乎不受金额影响（都是 2.7%），金额只影响绝对收益。比例跟单 5% cap $150 实际平均 ~$37/次，如果想接近 fix$150 的效果需要 10%+ 的比例。

## 注意事项

1. **这不是通用策略**。每个 leader 的最优参数不同，需要先用分析工具（`deep_investigate.py` + `strategy_grid.py`）确认适用性和最优参数。
2. **参数之间有强关联**。门槛、窗口、跳过次数三者共同决定精度，不能只调一个。
3. **双向下注的处理**。该 leader 会同时买 YES 和 NO，延迟跟单通过"等足够多次触发"自然过滤了大部分对冲噪声，但下单时仍需判断主方向。
4. **市场生命周期**。体育赛事市场有明确的结束时间，如果 leader 在赛前 1 小时才开始建仓，skip 15 次可能来不及。这种情况下 skip_n 需要适当降低。
5. **超限跟单与定价模式**。当 `super_follow_enabled = true` 时，有效定价模式固定为抢单（`aggressive`）。前端应灰掉原价/抢单切换，后端也应忽略 `pricing_mode = "original"`。

## 分析工具说明

以下脚本位于 `tools/sim_copytrade/` 目录，用于分析 leader 是否适合延迟跟单以及最优参数：

- `deep_investigate.py`：从 Data API 拉取大量历史交易，自算 per-market PnL，分析大仓位 vs 小仓位特征差异
- `early_signal.py`：分析在建仓早期（$500/$1K/$2K/$5K checkpoint）能否区分大小仓位
- `strategy_grid.py`：在缓存数据上做参数网格搜索，找最优的门槛/窗口/跳过次数/跟单方式组合
- `threshold_analysis.py`：对比不同门槛对对冲单的过滤效果

典型分析流程：
```bash
# 1. 拉取历史交易并缓存（约 15-20 分钟）
python deep_investigate.py --address 0x... --max-activities 800000

# 2. 早期信号分析（几秒）
python early_signal.py --address 0x...

# 3. 参数网格搜索（约 2-3 分钟）
python strategy_grid.py
# 注意：strategy_grid.py 目前硬编码了 0xee613b 的缓存路径，
# 用于其他地址时需要修改 cache_path
```
