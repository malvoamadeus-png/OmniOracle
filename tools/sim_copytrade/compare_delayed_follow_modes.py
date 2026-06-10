"""
Compare delayed-follow variants on the cached 0xee613b 800k-event dataset.

Requested comparisons:
1) Baseline split-$2000 trigger counting (skip 8 triggers).
2) Cumulative $16,000 gate (no split into $2000) then follow.
3) Same as (2), but backfill threshold exposure (150 * 8) when gate is crossed.

Also includes two extra "disambiguation" variants:
- cumulative gate -> continue with split-$2000 triggers (no backfill / backfill).
"""

import gzip
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for p in [str(SIM_PARENT), str(SIM_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from main import fetch_prices_for_tokens


@dataclass
class SimResult:
    mode: str
    mkts: int
    trades: int
    copy_usd: float
    pnl: float
    roi: float | None

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "mkts": self.mkts,
            "trades": self.trades,
            "copy_usd": round(self.copy_usd, 2),
            "pnl": round(self.pnl, 2),
            "roi": round(self.roi, 6) if self.roi is not None else None,
        }


def load_events(path: Path) -> list[dict]:
    events: list[dict] = []
    with gzip.open(str(path), "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events


def trade_usd(ev: dict) -> float:
    usd = ev.get("usd") or 0.0
    if usd <= 0:
        usd = (ev.get("price") or 0.0) * (ev.get("size") or 0.0)
    return float(usd)


def build_markets(events: list[dict]) -> dict:
    markets = defaultdict(
        lambda: {
            "events": [],
            "total_buy_usd": 0.0,
            "tokens": defaultdict(
                lambda: {
                    "buy_usd": 0.0,
                    "buy_shares": 0.0,
                    "sell_usd": 0.0,
                    "sell_shares": 0.0,
                }
            ),
        }
    )

    for ev in events:
        cid = ev.get("condition_id") or ev.get("token_id", "")
        tid = ev.get("token_id", "")
        if not cid or not tid:
            continue
        m = markets[cid]
        usd = trade_usd(ev)
        size = float(ev.get("size") or 0.0)

        if ev.get("side") == "BUY":
            m["total_buy_usd"] += usd
            m["tokens"][tid]["buy_usd"] += usd
            m["tokens"][tid]["buy_shares"] += size
        elif ev.get("side") == "SELL":
            m["tokens"][tid]["sell_usd"] += usd
            m["tokens"][tid]["sell_shares"] += size

        m["events"].append(ev)
    return markets


def compute_market_pnl(markets: dict, price_map: dict) -> None:
    for m in markets.values():
        realized = 0.0
        settlement = 0.0
        unrealized = 0.0
        for tid, t in m["tokens"].items():
            buy_shares = t["buy_shares"]
            sell_shares = t["sell_shares"]
            buy_usd = t["buy_usd"]
            sell_usd = t["sell_usd"]
            remaining = buy_shares - sell_shares

            if buy_shares > 0 and sell_shares > 0:
                realized += sell_usd - sell_shares * (buy_usd / buy_shares)

            if remaining > 1e-9 and buy_shares > 0:
                rem_cost = remaining * (buy_usd / buy_shares)
                pi = price_map.get(tid)
                if pi and pi.price is not None:
                    rem_val = remaining * pi.price
                    if pi.resolved:
                        settlement += rem_val - rem_cost
                    else:
                        unrealized += rem_val - rem_cost

        m["total_pnl"] = realized + settlement + unrealized


def summarize(mode: str, per_market: list[tuple[float, int, float, float]]) -> SimResult:
    mkts = 0
    trades = 0
    total_copy = 0.0
    total_pnl = 0.0

    for market_copy, market_trades, market_buy_usd, market_pnl in per_market:
        if market_trades <= 0 or market_copy <= 0:
            continue
        mkts += 1
        trades += market_trades
        total_copy += market_copy
        if market_buy_usd > 0:
            total_pnl += market_pnl * (market_copy / market_buy_usd)

    roi = total_pnl / total_copy if total_copy > 0 else None
    return SimResult(mode=mode, mkts=mkts, trades=trades, copy_usd=total_copy, pnl=total_pnl, roi=roi)


def simulate_baseline_split_2000(
    markets: dict,
    agg_threshold: float,
    agg_window_s: int,
    skip_n: int,
    copy_usd: float,
    max_follows: int,
) -> SimResult:
    rows: list[tuple[float, int, float, float]] = []
    for m in markets.values():
        buys = sorted((e for e in m["events"] if e.get("side") == "BUY"), key=lambda e: e["ts"])
        if not buys:
            continue

        window_start = buys[0]["ts"]
        window_usd = 0.0
        trigger_count = 0
        follow_count = 0
        market_copy = 0.0

        for ev in buys:
            usd = trade_usd(ev)
            if ev["ts"] - window_start <= agg_window_s:
                window_usd += usd
            else:
                window_start = ev["ts"]
                window_usd = usd

            if window_usd >= agg_threshold:
                trigger_count += 1
                if trigger_count > skip_n and follow_count < max_follows:
                    market_copy += copy_usd
                    follow_count += 1
                window_start = ev["ts"] + 1
                window_usd = 0.0

        rows.append((market_copy, follow_count, m["total_buy_usd"], m.get("total_pnl", 0.0)))

    return summarize("split_2000_skip8", rows)


def simulate_cum16000_then_follow_each_buy(
    markets: dict,
    gate_usd: float,
    skip_n: int,
    copy_usd: float,
    max_follows: int,
    backfill_threshold_copy: bool,
) -> SimResult:
    rows: list[tuple[float, int, float, float]] = []
    for m in markets.values():
        buys = sorted((e for e in m["events"] if e.get("side") == "BUY"), key=lambda e: e["ts"])
        if not buys:
            continue

        gate_cum = 0.0
        unlocked = False
        follow_count = 0
        market_copy = 0.0

        for ev in buys:
            usd = trade_usd(ev)

            if not unlocked:
                gate_cum += usd
                if gate_cum >= gate_usd:
                    unlocked = True
                    if backfill_threshold_copy and follow_count < max_follows:
                        backfill_n = min(skip_n, max_follows - follow_count)
                        market_copy += copy_usd * backfill_n
                        follow_count += backfill_n
                    # The crossing trade itself is still "threshold phase", next trade starts following.
                    continue
                continue

            if follow_count >= max_follows:
                break
            market_copy += copy_usd
            follow_count += 1

        rows.append((market_copy, follow_count, m["total_buy_usd"], m.get("total_pnl", 0.0)))

    suffix = "with_backfill" if backfill_threshold_copy else "no_backfill"
    return summarize(f"cum16000_then_each_buy_{suffix}", rows)


def simulate_cum16000_then_split_2000(
    markets: dict,
    gate_usd: float,
    agg_threshold: float,
    agg_window_s: int,
    skip_n: int,
    copy_usd: float,
    max_follows: int,
    backfill_threshold_copy: bool,
) -> SimResult:
    rows: list[tuple[float, int, float, float]] = []
    for m in markets.values():
        buys = sorted((e for e in m["events"] if e.get("side") == "BUY"), key=lambda e: e["ts"])
        if not buys:
            continue

        gate_cum = 0.0
        unlocked = False
        follow_count = 0
        market_copy = 0.0
        window_start = 0
        window_usd = 0.0

        for ev in buys:
            usd = trade_usd(ev)

            if not unlocked:
                gate_cum += usd
                if gate_cum >= gate_usd:
                    unlocked = True
                    if backfill_threshold_copy and follow_count < max_follows:
                        backfill_n = min(skip_n, max_follows - follow_count)
                        market_copy += copy_usd * backfill_n
                        follow_count += backfill_n
                    window_start = ev["ts"] + 1
                    window_usd = 0.0
                continue

            if follow_count >= max_follows:
                break

            if ev["ts"] - window_start <= agg_window_s:
                window_usd += usd
            else:
                window_start = ev["ts"]
                window_usd = usd

            if window_usd >= agg_threshold:
                market_copy += copy_usd
                follow_count += 1
                window_start = ev["ts"] + 1
                window_usd = 0.0

        rows.append((market_copy, follow_count, m["total_buy_usd"], m.get("total_pnl", 0.0)))

    suffix = "with_backfill" if backfill_threshold_copy else "no_backfill"
    return summarize(f"cum16000_then_split2000_{suffix}", rows)


def main() -> int:
    t0 = time.time()
    cache = SIM_ROOT / "output" / "events_cache_0xee613b.jsonl.gz"
    if not cache.exists():
        print(f"[error] cache not found: {cache}")
        return 1

    # Match the discussed reference setup.
    agg_threshold = 2000.0
    agg_window_s = 60 * 60
    skip_n = 8
    gate_usd = agg_threshold * skip_n  # 16,000
    copy_usd = 150.0
    max_follows = 20

    print("Loading events...")
    events = load_events(cache)
    markets = build_markets(events)
    print(f"  events={len(events):,}, markets={len(markets):,}")

    tokens_need: set[str] = set()
    for m in markets.values():
        for tid, t in m["tokens"].items():
            if t["buy_shares"] - t["sell_shares"] > 1e-9:
                tokens_need.add(tid)
    print(f"Fetching prices for {len(tokens_need):,} tokens...")
    price_map = fetch_prices_for_tokens(sorted(tokens_need), timeout_s=30.0, workers=16)
    compute_market_pnl(markets, price_map)
    leader_pnl = sum(m.get("total_pnl", 0.0) for m in markets.values())
    print(f"Leader total pnl: ${leader_pnl:+,.2f}")

    results: list[SimResult] = []
    results.append(
        simulate_baseline_split_2000(
            markets=markets,
            agg_threshold=agg_threshold,
            agg_window_s=agg_window_s,
            skip_n=skip_n,
            copy_usd=copy_usd,
            max_follows=max_follows,
        )
    )
    results.append(
        simulate_cum16000_then_follow_each_buy(
            markets=markets,
            gate_usd=gate_usd,
            skip_n=skip_n,
            copy_usd=copy_usd,
            max_follows=max_follows,
            backfill_threshold_copy=False,
        )
    )
    results.append(
        simulate_cum16000_then_follow_each_buy(
            markets=markets,
            gate_usd=gate_usd,
            skip_n=skip_n,
            copy_usd=copy_usd,
            max_follows=max_follows,
            backfill_threshold_copy=True,
        )
    )

    # Extra disambiguation variants.
    results.append(
        simulate_cum16000_then_split_2000(
            markets=markets,
            gate_usd=gate_usd,
            agg_threshold=agg_threshold,
            agg_window_s=agg_window_s,
            skip_n=skip_n,
            copy_usd=copy_usd,
            max_follows=max_follows,
            backfill_threshold_copy=False,
        )
    )
    results.append(
        simulate_cum16000_then_split_2000(
            markets=markets,
            gate_usd=gate_usd,
            agg_threshold=agg_threshold,
            agg_window_s=agg_window_s,
            skip_n=skip_n,
            copy_usd=copy_usd,
            max_follows=max_follows,
            backfill_threshold_copy=True,
        )
    )

    print()
    hdr = f"{'Mode':<38} {'Mkts':>6} {'Trades':>8} {'CopyUSD':>13} {'PnL':>12} {'ROI':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        roi_s = f"{r.roi * 100:+.2f}%" if r.roi is not None else "N/A"
        print(
            f"{r.mode:<38} {r.mkts:>6} {r.trades:>8} "
            f"${r.copy_usd:>12,.2f} ${r.pnl:>+11,.2f} {roi_s:>8}"
        )

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_file": str(cache),
        "params": {
            "agg_threshold": agg_threshold,
            "agg_window_s": agg_window_s,
            "skip_n": skip_n,
            "gate_usd": gate_usd,
            "copy_usd": copy_usd,
            "max_follows": max_follows,
        },
        "leader_pnl": round(leader_pnl, 2),
        "results": [r.as_dict() for r in results],
    }
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = SIM_ROOT / "output" / f"compare_delayed_follow_modes_{ts}.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[output] {out_path}")
    print(f"[done] {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
