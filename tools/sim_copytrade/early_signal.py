"""
Early Signal Analysis — 从缓存的 800K 事件中，
分析大仓位在建仓早期有什么可识别的特征，
设计"在形成大仓位之前就判断出来"的筛选规则。

用法:
    python early_signal.py --address 0xee613b3fc183ee44f9da9c05f53e2da107e3debf
"""
from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for p in [str(SIM_PARENT), str(SIM_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Load cached events
# ---------------------------------------------------------------------------

def load_cached_events(cache_path: Path) -> List[Dict[str, Any]]:
    events = []
    opener = gzip.open if str(cache_path).endswith('.gz') else open
    with opener(str(cache_path), 'rt', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Build per-market timeline: replay events chronologically
# ---------------------------------------------------------------------------

def build_market_timelines(events: List[Dict]) -> Dict[str, Dict[str, Any]]:
    """
    For each market (condition_id), build a chronological timeline of
    cumulative buy USD per token, and classify final size.
    """
    markets: Dict[str, Dict[str, Any]] = {}

    for ev in events:
        cid = ev.get("condition_id") or ev.get("token_id", "")
        tid = ev.get("token_id", "")
        if not cid or not tid:
            continue

        if cid not in markets:
            markets[cid] = {
                "condition_id": cid,
                "slug": ev.get("market_slug", ""),
                "tokens": {},
                "timeline": [],  # (ts, cumulative_buy_usd, event_side, event_usd)
                "total_buy_usd": 0.0,
                "total_sell_usd": 0.0,
                "buy_count": 0,
                "sell_count": 0,
                "first_ts": ev["ts"],
                "last_ts": ev["ts"],
            }
        m = markets[cid]
        if not m["slug"] and ev.get("market_slug"):
            m["slug"] = ev["market_slug"]

        usd = ev.get("usd") or 0.0
        price = ev.get("price") or 0.0
        size = ev.get("size") or 0.0
        if usd <= 0 and price > 0 and size > 0:
            usd = price * size

        if ev["side"] == "BUY":
            m["total_buy_usd"] += usd
            m["buy_count"] += 1
            # Track per-token buys
            if tid not in m["tokens"]:
                m["tokens"][tid] = {"buy_usd": 0.0, "sell_usd": 0.0, "buy_count": 0}
            m["tokens"][tid]["buy_usd"] += usd
            m["tokens"][tid]["buy_count"] += 1
        elif ev["side"] == "SELL":
            m["total_sell_usd"] += usd
            m["sell_count"] += 1
            if tid not in m["tokens"]:
                m["tokens"][tid] = {"buy_usd": 0.0, "sell_usd": 0.0, "buy_count": 0}
            m["tokens"][tid]["sell_usd"] += usd

        m["last_ts"] = max(m["last_ts"], ev["ts"])
        m["first_ts"] = min(m["first_ts"], ev["ts"])

        # Timeline entry
        m["timeline"].append({
            "ts": ev["ts"],
            "side": ev["side"],
            "usd": usd,
            "price": price,
            "token_id": tid,
            "cum_buy_usd": m["total_buy_usd"],
        })

    return markets


# ---------------------------------------------------------------------------
# Early signal: at each checkpoint, what features distinguish big from small?
# ---------------------------------------------------------------------------

def extract_early_features_at_checkpoint(
    timeline: List[Dict], checkpoint_usd: float,
) -> Optional[Dict[str, Any]]:
    """
    Replay timeline until cumulative buy USD reaches checkpoint.
    Extract features at that moment.
    Returns None if checkpoint never reached.
    """
    cum = 0.0
    buy_usds = []
    buy_prices = []
    buy_timestamps = []
    tokens_seen = set()
    buy_count = 0

    for entry in timeline:
        if entry["side"] == "BUY":
            cum += entry["usd"]
            buy_count += 1
            if entry["usd"] > 0:
                buy_usds.append(entry["usd"])
            if entry["price"] and entry["price"] > 0:
                buy_prices.append(entry["price"])
            buy_timestamps.append(entry["ts"])
            tokens_seen.add(entry["token_id"])

        if cum >= checkpoint_usd:
            duration_h = (buy_timestamps[-1] - buy_timestamps[0]) / 3600.0 if len(buy_timestamps) >= 2 else 0
            return {
                "buy_count": buy_count,
                "cum_usd": round(cum, 2),
                "avg_buy_usd": round(statistics.mean(buy_usds), 2) if buy_usds else 0,
                "median_buy_usd": round(statistics.median(buy_usds), 2) if buy_usds else 0,
                "max_buy_usd": round(max(buy_usds), 2) if buy_usds else 0,
                "p75_buy_usd": round(sorted(buy_usds)[int(len(buy_usds) * 0.75)] if len(buy_usds) >= 4 else max(buy_usds, default=0), 2),
                "avg_price": round(statistics.mean(buy_prices), 4) if buy_prices else 0,
                "median_price": round(statistics.median(buy_prices), 4) if buy_prices else 0,
                "duration_hours": round(duration_h, 2),
                "frequency": round(buy_count / max(duration_h, 0.01), 2),
                "token_count": len(tokens_seen),
                "has_both_sides": len(tokens_seen) >= 2,
                "time_to_checkpoint_hours": round(duration_h, 2),
                "trades_to_checkpoint": buy_count,
            }
    return None


def analyze_early_signals(
    markets: Dict[str, Dict[str, Any]],
    big_threshold: float = 10000.0,
    checkpoints: List[float] = None,
) -> Dict[str, Any]:
    """
    For each checkpoint ($500, $1K, $2K, $5K), compare features of
    markets that eventually become big vs those that stay small.
    """
    if checkpoints is None:
        checkpoints = [500, 1000, 2000, 5000]

    results = {}
    for cp in checkpoints:
        big_features = []
        small_features = []

        for cid, m in markets.items():
            is_big = m["total_buy_usd"] >= big_threshold
            feat = extract_early_features_at_checkpoint(m["timeline"], cp)
            if feat is None:
                continue  # never reached this checkpoint
            feat["final_buy_usd"] = round(m["total_buy_usd"], 2)
            feat["is_big"] = is_big
            if is_big:
                big_features.append(feat)
            else:
                small_features.append(feat)

        def _agg(group: List[Dict], label: str) -> Dict:
            if not group:
                return {"label": label, "count": 0}
            return {
                "label": label,
                "count": len(group),
                "avg_buy_usd_per_trade": round(statistics.mean([f["avg_buy_usd"] for f in group]), 2),
                "median_buy_usd_per_trade": round(statistics.median([f["median_buy_usd"] for f in group]), 2),
                "avg_max_buy_usd": round(statistics.mean([f["max_buy_usd"] for f in group]), 2),
                "avg_p75_buy_usd": round(statistics.mean([f["p75_buy_usd"] for f in group]), 2),
                "avg_price": round(statistics.mean([f["avg_price"] for f in group if f["avg_price"] > 0]), 4),
                "median_price": round(statistics.median([f["median_price"] for f in group if f["median_price"] > 0]), 4),
                "avg_duration_hours": round(statistics.mean([f["duration_hours"] for f in group]), 2),
                "avg_frequency": round(statistics.mean([f["frequency"] for f in group]), 2),
                "avg_trades_to_cp": round(statistics.mean([f["trades_to_checkpoint"] for f in group]), 1),
                "both_sides_pct": round(sum(1 for f in group if f["has_both_sides"]) / len(group), 4),
                "avg_token_count": round(statistics.mean([f["token_count"] for f in group]), 2),
            }

        total_reached = len(big_features) + len(small_features)
        precision = len(big_features) / total_reached if total_reached > 0 else 0

        results[f"${int(cp)}"] = {
            "checkpoint_usd": cp,
            "total_reached": total_reached,
            "big_reached": len(big_features),
            "small_reached": len(small_features),
            "precision_if_follow_all": round(precision, 4),
            "big": _agg(big_features, "big"),
            "small": _agg(small_features, "small"),
        }

    return results


def find_discriminating_rules(early_signals: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Based on early signal analysis, propose filtering rules that
    increase precision (% of followed markets that become big).
    """
    rules = []
    for cp_key, cp_data in early_signals.items():
        big = cp_data["big"]
        small = cp_data["small"]
        if big["count"] == 0 or small["count"] == 0:
            continue

        # Rule candidates: features where big differs significantly from small
        candidates = []

        # 1. avg_buy_usd: big tends to have higher per-trade USD?
        if big.get("avg_buy_usd_per_trade", 0) > 0 and small.get("avg_buy_usd_per_trade", 0) > 0:
            ratio = big["avg_buy_usd_per_trade"] / small["avg_buy_usd_per_trade"]
            candidates.append(("avg_buy_usd", ratio, big["avg_buy_usd_per_trade"], small["avg_buy_usd_per_trade"]))

        # 2. frequency: big tends to have higher frequency?
        if big.get("avg_frequency", 0) > 0 and small.get("avg_frequency", 0) > 0:
            ratio = big["avg_frequency"] / small["avg_frequency"]
            candidates.append(("frequency", ratio, big["avg_frequency"], small["avg_frequency"]))

        # 3. both_sides: big tends to have more both-sides?
        if big.get("both_sides_pct", 0) > 0:
            ratio = big["both_sides_pct"] / max(small.get("both_sides_pct", 0.01), 0.01)
            candidates.append(("both_sides_pct", ratio, big["both_sides_pct"], small.get("both_sides_pct", 0)))

        # 4. duration: big tends to be longer?
        if big.get("avg_duration_hours", 0) > 0 and small.get("avg_duration_hours", 0) > 0:
            ratio = big["avg_duration_hours"] / small["avg_duration_hours"]
            candidates.append(("duration_hours", ratio, big["avg_duration_hours"], small["avg_duration_hours"]))

        # 5. price range: big tends to buy at certain prices?
        if big.get("avg_price", 0) > 0 and small.get("avg_price", 0) > 0:
            ratio = big["avg_price"] / small["avg_price"]
            candidates.append(("avg_price", ratio, big["avg_price"], small["avg_price"]))

        # 6. max single trade
        if big.get("avg_max_buy_usd", 0) > 0 and small.get("avg_max_buy_usd", 0) > 0:
            ratio = big["avg_max_buy_usd"] / small["avg_max_buy_usd"]
            candidates.append(("max_single_trade", ratio, big["avg_max_buy_usd"], small["avg_max_buy_usd"]))

        candidates.sort(key=lambda x: abs(x[1] - 1.0), reverse=True)
        rules.append({
            "checkpoint": cp_key,
            "base_precision": cp_data["precision_if_follow_all"],
            "discriminators": [
                {"feature": c[0], "big_vs_small_ratio": round(c[1], 2), "big_val": c[2], "small_val": c[3]}
                for c in candidates
            ],
        })

    return rules


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Early signal analysis for big position detection")
    ap.add_argument("--address", required=True)
    ap.add_argument("--cache-path", type=str, default="")
    ap.add_argument("--big-threshold", type=float, default=10000.0)
    ap.add_argument("--out-dir", type=str, default="")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    address = args.address.lower().strip()
    short = f"{address[:8]}_{address[-6:]}"
    out_dir = Path(args.out_dir) if args.out_dir else SIM_ROOT / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_path = Path(args.cache_path) if args.cache_path else out_dir / f"events_cache_{address[:8]}.jsonl.gz"
    if not cache_path.exists():
        print(f"ERROR: Cache not found at {cache_path}")
        print("Run deep_investigate.py first, or provide --cache-path")
        return 1

    print(f"=== Early Signal Analysis: {short} ===")
    t0 = time.time()

    # Load events
    print(f"[load] {cache_path}...")
    events = load_cached_events(cache_path)
    print(f"[load] {len(events):,} events in {time.time()-t0:.1f}s")

    # Build timelines
    print("[build] building market timelines...")
    markets = build_market_timelines(events)
    big_count = sum(1 for m in markets.values() if m["total_buy_usd"] >= args.big_threshold)
    small_count = len(markets) - big_count
    print(f"[build] {len(markets):,} markets (big>=${args.big_threshold:,.0f}: {big_count}, small: {small_count})")

    # Early signal analysis
    print("[analyze] computing early signals at checkpoints...")
    early = analyze_early_signals(markets, big_threshold=args.big_threshold)

    # Print results
    print("\n" + "=" * 120)
    print(f"EARLY SIGNAL ANALYSIS (big threshold = ${args.big_threshold:,.0f})")
    print(f"{'Checkpoint':>12} {'Reached':>8} {'Big':>6} {'Small':>7} {'Precision':>10} | {'BigAvg$/tr':>11} {'SmAvg$/tr':>11} {'BigFreq':>8} {'SmFreq':>8} {'BigBoth%':>9} {'SmBoth%':>9} {'BigHrs':>7} {'SmHrs':>7}")
    print("-" * 120)
    for cp_key, d in early.items():
        b, s = d["big"], d["small"]
        if b["count"] == 0:
            continue
        print(
            f"{cp_key:>12} {d['total_reached']:>8} {d['big_reached']:>6} {d['small_reached']:>7} {d['precision_if_follow_all']:>9.0%} | "
            f"${b.get('avg_buy_usd_per_trade', 0):>10.1f} ${s.get('avg_buy_usd_per_trade', 0):>10.1f} "
            f"{b.get('avg_frequency', 0):>8.1f} {s.get('avg_frequency', 0):>8.1f} "
            f"{b.get('both_sides_pct', 0):>8.0%} {s.get('both_sides_pct', 0):>8.0%} "
            f"{b.get('avg_duration_hours', 0):>7.1f} {s.get('avg_duration_hours', 0):>7.1f}"
        )

    # Discriminating rules
    rules = find_discriminating_rules(early)
    print(f"\nDISCRIMINATING FEATURES (big/small ratio, further from 1.0 = more discriminating)")
    for r in rules:
        print(f"\n  {r['checkpoint']} (base precision={r['base_precision']:.0%}):")
        for d in r["discriminators"][:5]:
            direction = "higher" if d["big_vs_small_ratio"] > 1 else "lower"
            print(f"    {d['feature']:<25} big={d['big_val']:<10} small={d['small_val']:<10} ratio={d['big_vs_small_ratio']:.2f}x ({direction} in big)")

    # Save
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"early_signal_{short}_{ts_str}.json"
    payload = {
        "generated_at": _now_iso(),
        "address": address,
        "big_threshold": args.big_threshold,
        "events_count": len(events),
        "markets_count": len(markets),
        "big_count": big_count,
        "small_count": small_count,
        "early_signals": early,
        "discriminating_rules": rules,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n[output] {json_path}")
    print(f"[done] {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
