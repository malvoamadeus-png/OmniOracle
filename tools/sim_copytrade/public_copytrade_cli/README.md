# public_copytrade_cli

Public-facing Polymarket copytrade CLI extracted from the larger simulator.

## What It Does

- Fetches a leader address's historical `TRADE` activity.
- Deduplicates events and builds `maker-like BUY` replay signals.
- Replays a fixed 400-strategy grid with `mirror_sell` exits.
- Keeps the legacy fee model, midpoint/resolution pricing, and anti-amplification guard.
- Writes a single JSON summary for downstream programs.

This migration intentionally does not run auto-optimization, AI improvement, PDF generation, CSV generation, HTML generation, AI reports, or 4-window split analysis.

## Inputs

Required:

- `address`

Optional:

- `max_activities` (default `50000`)
- `premium` (default `0.03`, meaning 3%)
- `mirror_sell_slippage` (default `0.01`, meaning 1%)

## Usage

```bash
python public_copytrade_cli/main.py --address 0xYourLeaderAddress
```

```bash
python public_copytrade_cli/main.py \
  --address 0xYourLeaderAddress \
  --max-activities 80000 \
  --premium 0.02 \
  --mirror-sell-slippage 0.015
```

## Fixed Strategy Grid

- `fixed_usd = [5, 20, 50, 100]`
- `proportional_pct = [0.005, 0.01, 0.03, 0.05]`
- `proportional_cap_usd = [5, 20, 50, 100]`
- `max_entries_per_market = 1..20`

Total strategies: `400`

## Legacy Execution Assumptions Kept

- `maker-like BUY` aggregation stays on.
- Exit mode stays `mirror_sell`.
- Fee model stays on:
  - `fee_rate = 0.03`
  - `fee_exponent = 1.0`
- Anti-amplification guard stays on and is not exposed publicly:
  - per-trade cap = `1.0x`
  - per-market cumulative cap = `1.0x`
- Buy/sell price bounds stay:
  - buy `[0.01, 0.99]`
  - sell `[0.01, 0.99]`

## Output

The CLI writes one JSON file under:

- `public_copytrade_cli/output/public_copytrade_<short_address>_<timestamp>.json`

Price cache lives separately at:

- `public_copytrade_cli/.cache/price_cache.sqlite`

The JSON shape is:

```json
{
  "generated_at": "...",
  "input": {},
  "summary": {},
  "best_returns": {},
  "top_strategies": {}
}
```

### `input`

- `address`
- `max_activities`
- `premium`
- `mirror_sell_slippage`

### `summary`

- `address`
- `backtest_span_days`
- `trade_count`
- `window_real_pnl`
- `avg_bets_per_market`
- `avg_usd_per_market`
- `fetched_events`
- `replay_events`
- `buy_signal_count`
- `aggregated_buy_signal_count`
- `benchmark_error`

Important metric notes:

- `trade_count` is the number of deduplicated historical `TRADE` activities.
- `window_real_pnl` is the leader's real PnL delta over the same tracked window from the current `user-pnl-api` endpoint.
- If benchmark fetch fails, `window_real_pnl` becomes `null` and `benchmark_error` contains the error message.

### `best_returns`

- `best_by_roi`
- `best_by_total_pnl`

### `top_strategies`

- `top5_by_roi`
- `top5_by_total_pnl`

Each strategy row includes:

- `strategy`
- `copy_mode`
- `fixed_usd`
- `proportional_pct`
- `proportional_cap_usd`
- `max_entries_per_market`
- `total_pnl`
- `roi`
- `total_buy_cost`
- `copied_buys`
- `mirrored_sells`

Ranking rules:

- `top5_by_roi`: `roi desc`, then `total_pnl desc`, then `total_buy_cost desc`
- `top5_by_total_pnl`: `total_pnl desc`, then `roi desc`, then `total_buy_cost desc`

## Differences From `tools/sim_copytrade/main.py`

- No auto-optimization.
- No AI improvement loop.
- No PDF, CSV, HTML, or AI outputs.
- No 4-window split analysis.
- `premium` and `mirror_sell_slippage` are true public inputs and directly affect replay.
- Output is intentionally compact for downstream program consumption.
