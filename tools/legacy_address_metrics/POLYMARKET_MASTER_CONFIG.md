# polymarket_master_metrics 配置说明

`polymarket_master_metrics.py` 现在由 `polymarket_master_config.json` 驱动。

## 使用方式

- 默认运行：`python polymarket_master_metrics.py`
- 指定配置：`python polymarket_master_metrics.py --config polymarket_master_config.json`
- 覆盖数据库：`python polymarket_master_metrics.py --db metrics_fresh.sqlite`
- 强制同步：`python polymarket_master_metrics.py --sync-supabase`
- 仅跑 Crypto 来源：`python polymarket_master_metrics.py --crypto-only`

`--crypto-only` 规则：
- 仅保留 `sources[].args.slug_prefixes` 非空的 source（当前配置下通常是 `15M`、`1H`）。
- 不改变输出结构和字段，仅缩小本次参与统计的 source 范围。

## 顶层参数说明

- `db_path`
  - 类型：`string`
  - 默认：`metrics_fresh.sqlite`
  - 作用：主程序写入的本地 SQLite 路径（`address_metrics`、`master_results`）。
- `sync_supabase`
  - 类型：`boolean`
  - 默认：`false`
  - 作用：主程序结束后是否自动执行 `supabase/sync_to_supabase.py`。
  - 优先级：CLI `--sync-supabase` 为强制开启。
- `per_address_sleep_s`
  - 类型：`number`
  - 默认：`0.1`
  - 作用：每个地址计算间隔（秒），用于限流与降压。
- `metrics_timeout_s`
  - 类型：`integer`
  - 默认：`300`
  - 作用：单地址运行 `polymarket_metrics.py` 的超时秒数。
- `cache_max_age_days`
  - 类型：`integer`
  - 默认：`30`
  - 作用：地址缓存有效期（天）。在有效期内命中缓存就不重复抓取。
- `sources`
  - 类型：`array`
  - 作用：多来源抓取配置。每个来源独立抓取，再合并去重地址后统一跑指标。

## `sources[]` 参数说明

- `name`
  - 类型：`string`
  - 作用：来源名（如 `NBA`、`CLIMATE`），会写入 `address_metrics.source_tags`。
- `script`
  - 类型：`string`
  - 作用：抓取脚本路径（相对路径相对于项目根目录）。
- `args`
  - 类型：`object`
  - 作用：透传给 `script` 的参数。键名用 `snake_case`，会自动转成 CLI 参数。
  - 例：`holders_limit` -> `--holders-limit`，`tag_id` -> `--tag-id`。

## `sources[].args` 可用字段（对应 `market_top_holders.py`）

- `sport`：体育/分类代码（如 `nba`、`climate`）。
- `series_id`：指定系列 ID（可选；不填时按 `sport` 自动推导）。
- `tag_id`：按 tag 抓市场（如气候 `87`）。
- `related_tags`：是否包含关联 tag（`true/false`）。
- `validate_tag_id`：抓取后再做 tag 校验（建议与 `tag_id` 相同）。
- `holders_limit`：每个 token 返回前 N 位 holder，API 上限 20。
- `max_games`：本次最多抓取的市场数量。
- `market_kind`：`moneyline` 或 `all`。
- `gamma_page_limit`：Gamma `/markets` 分页大小。

## 不建议在配置里放的字段

- `out`
  - 主程序会自动给每个 source 生成临时输出文件并清理，不需要手动配置。
- `pretty`
  - 仅影响脚本直接输出格式，对主程序无必要。

## 推荐模板

- NBA（sport/series 路径）
  - `script: "market_top_holders.py"`
  - `args.sport: "nba"`
  - `args.market_kind: "moneyline"`
- CLIMATE（tag 路径）
  - `script: "market_top_holders.py"`
  - `args.tag_id: 87`
  - `args.market_kind: "all"`
  - `args.validate_tag_id: 87`

## 重要变更

- `min_trades` 已移除：
  - 不再作为 CLI 参数、配置参数、主流程写入字段。
  - 历史 SQLite / Supabase 里若存在旧列，读取仍兼容。

