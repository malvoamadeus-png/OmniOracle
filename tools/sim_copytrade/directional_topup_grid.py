"""
Directional top-up backtest for delayed-follow.

Goal (PnL-first under budget):
  supplement_usd = min(cap, max(0, A_usd - B_usd) * k)

Where:
  - A/B are the two-side cumulative USD during observing phase (first skip_n triggers).
  - Supplement is applied once, at unlock (first trigger after observing phase).
  - After supplement, strategy continues normal delayed-follow (no direction lock).

Outputs:
  1) Global metrics: Copy / PnL / ROI / deltas vs baseline.
  2) Structural metrics: supplement distribution p50/p75/p90/p95 + supplemented market count.
  3) Risk metrics: per-market opposite-drag top list.
"""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.stdout.reconfigure(encoding="utf-8")

SIM_ROOT = Path(__file__).resolve().parent
SIM_PARENT = SIM_ROOT.parent
for p in (str(SIM_PARENT), str(SIM_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from main import fetch_prices_for_tokens


@dataclass
class Trigger:
    token_id: str
    window_usd: float
    ts: int


@dataclass
class SimulationResult:
    k: float
    cap: float
    mkts: int
    trades: int
    copy_usd: float
    pnl: float
    roi: float
    supplement_market_count: int
    supplement_usd_total: float
    supplement_usd_values: list[float]
    market_details: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "cap": self.cap,
            "mkts": self.mkts,
            "trades": self.trades,
            "copy_usd": round(self.copy_usd, 2),
            "pnl": round(self.pnl, 2),
            "roi": round(self.roi, 6),
            "supplement_market_count": self.supplement_market_count,
            "supplement_usd_total": round(self.supplement_usd_total, 2),
        }


def parse_csv_floats(raw: str) -> list[float]:
    out: list[float] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    return out


def quantile_from_sorted(values: list[float], q: float) -> float | None:
    if not values:
        return None
    qq = max(0.0, min(1.0, float(q)))
    if len(values) == 1:
        return float(values[0])
    idx = (len(values) - 1) * qq
    lo = int(idx)
    hi = min(lo + 1, len(values) - 1)
    frac = idx - lo
    return float(values[lo] + (values[hi] - values[lo]) * frac)


def trade_usd(ev: dict[str, Any]) -> float:
    usd = ev.get("usd") or 0.0
    if usd <= 0:
        usd = (ev.get("price") or 0.0) * (ev.get("size") or 0.0)
    return float(usd)


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with gzip.open(str(path), "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                events.append(json.loads(line))
    return events


def build_markets(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    markets = defaultdict(
        lambda: {
            "events": [],
            "slug": "",
            "tokens": defaultdict(
                lambda: {
                    "buy_usd": 0.0,
                    "buy_shares": 0.0,
                    "sell_usd": 0.0,
                    "sell_shares": 0.0,
                    "total_pnl": 0.0,
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
        if not m["slug"] and ev.get("market_slug"):
            m["slug"] = str(ev.get("market_slug") or "")
        m["events"].append(ev)
        usd = trade_usd(ev)
        size = float(ev.get("size") or 0.0)
        if ev.get("side") == "BUY":
            m["tokens"][tid]["buy_usd"] += usd
            m["tokens"][tid]["buy_shares"] += size
        elif ev.get("side") == "SELL":
            m["tokens"][tid]["sell_usd"] += usd
            m["tokens"][tid]["sell_shares"] += size
    return markets


def compute_token_pnl(markets: dict[str, dict[str, Any]], price_map: dict[str, Any]) -> None:
    for m in markets.values():
        for tid, t in m["tokens"].items():
            buy_shares = t["buy_shares"]
            sell_shares = t["sell_shares"]
            buy_usd = t["buy_usd"]
            sell_usd = t["sell_usd"]
            remaining = buy_shares - sell_shares

            realized = 0.0
            settlement = 0.0
            unrealized = 0.0

            if buy_shares > 0 and sell_shares > 0:
                realized += sell_usd - sell_shares * (buy_usd / buy_shares)

            if remaining > 1e-9 and buy_shares > 0:
                rem_cost = remaining * (buy_usd / buy_shares)
                pi = price_map.get(tid)
                if pi and pi.price is not None:
                    rem_value = remaining * pi.price
                    if pi.resolved:
                        settlement += rem_value - rem_cost
                    else:
                        unrealized += rem_value - rem_cost

            t["total_pnl"] = realized + settlement + unrealized


def build_triggers_for_market(
    events: list[dict[str, Any]],
    agg_threshold: float,
    agg_window_s: int,
) -> list[Trigger]:
    buys = sorted((e for e in events if e.get("side") == "BUY"), key=lambda e: e["ts"])
    if not buys:
        return []

    triggers: list[Trigger] = []
    window_start = int(buys[0]["ts"])
    window_usd = 0.0

    for ev in buys:
        usd = trade_usd(ev)
        ts = int(ev["ts"])
        if ts - window_start <= agg_window_s:
            window_usd += usd
        else:
            window_start = ts
            window_usd = usd

        if window_usd >= agg_threshold:
            triggers.append(
                Trigger(
                    token_id=str(ev.get("token_id") or ""),
                    window_usd=float(window_usd),
                    ts=ts,
                )
            )
            window_start = ts + 1
            window_usd = 0.0

    return triggers


def prepare_streams(
    markets: dict[str, dict[str, Any]],
    agg_threshold: float,
    agg_window_s: int,
    skip_n: int,
) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for cid, m in markets.items():
        triggers = build_triggers_for_market(m["events"], agg_threshold, agg_window_s)
        if len(triggers) <= skip_n:
            continue

        obs = triggers[:skip_n]
        post = triggers[skip_n:]
        observe_token_usd = defaultdict(float)
        for t in obs:
            if t.token_id:
                observe_token_usd[t.token_id] += t.window_usd

        dominant_token = None
        dominant_usd = 0.0
        secondary_usd = 0.0
        if observe_token_usd:
            ordered = sorted(observe_token_usd.items(), key=lambda kv: kv[1], reverse=True)
            dominant_token = ordered[0][0]
            dominant_usd = float(ordered[0][1])
            secondary_usd = float(ordered[1][1]) if len(ordered) > 1 else 0.0

        diff_usd = max(0.0, dominant_usd - secondary_usd)
        streams.append(
            {
                "condition_id": cid,
                "market_slug": m["slug"] or cid[:16],
                "post": post,
                "dominant_token": dominant_token,
                "dominant_usd": dominant_usd,
                "secondary_usd": secondary_usd,
                "diff_usd": diff_usd,
                "tokens": m["tokens"],
            }
        )
    return streams


def token_level_pnl(tokens: dict[str, dict[str, float]], copy_by_token: dict[str, float]) -> float:
    pnl = 0.0
    for tid, copy_usd in copy_by_token.items():
        if copy_usd <= 0:
            continue
        t = tokens.get(tid)
        if not t:
            continue
        buy_usd = float(t.get("buy_usd") or 0.0)
        if buy_usd <= 0:
            continue
        pnl += float(t.get("total_pnl") or 0.0) * (copy_usd / buy_usd)
    return pnl


def simulate(
    streams: list[dict[str, Any]],
    *,
    fixed_copy_usd: float,
    max_follows: int,
    k: float,
    cap: float,
    collect_market_details: bool,
) -> SimulationResult:
    mkts = 0
    trades = 0
    total_copy = 0.0
    total_pnl = 0.0

    supplement_market_count = 0
    supplement_usd_total = 0.0
    supplement_values: list[float] = []
    market_details: list[dict[str, Any]] = []

    for s in streams:
        copy_by_token = defaultdict(float)
        dominant_token = s["dominant_token"]

        supplement_usd = 0.0
        if dominant_token and k > 0 and cap > 0:
            supplement_usd = min(float(cap), float(s["diff_usd"]) * float(k))
            if supplement_usd > 0:
                copy_by_token[dominant_token] += supplement_usd
                supplement_market_count += 1
                supplement_usd_total += supplement_usd
                supplement_values.append(supplement_usd)

        follow_count = 0
        for trig in s["post"]:
            if follow_count >= max_follows:
                break
            tok = trig.token_id
            if not tok:
                continue
            copy_by_token[tok] += fixed_copy_usd
            follow_count += 1

        market_copy = sum(copy_by_token.values())
        if market_copy <= 0:
            continue

        mkts += 1
        trades += follow_count
        total_copy += market_copy
        market_pnl = token_level_pnl(s["tokens"], copy_by_token)
        total_pnl += market_pnl

        if collect_market_details:
            same_copy = (
                {dominant_token: copy_by_token.get(dominant_token, 0.0)}
                if dominant_token
                else {}
            )
            same_pnl = token_level_pnl(s["tokens"], same_copy)
            opp_copy_usd = market_copy - sum(same_copy.values())
            opp_trade_count = sum(1 for t in s["post"][:max_follows] if t.token_id and t.token_id != dominant_token)
            market_details.append(
                {
                    "condition_id": s["condition_id"],
                    "market_slug": s["market_slug"],
                    "dominant_usd": round(float(s["dominant_usd"]), 4),
                    "secondary_usd": round(float(s["secondary_usd"]), 4),
                    "diff_usd": round(float(s["diff_usd"]), 4),
                    "supplement_usd": round(float(supplement_usd), 4),
                    "trades": int(follow_count),
                    "copy_usd": round(float(market_copy), 4),
                    "pnl_all": round(float(market_pnl), 4),
                    "pnl_same_only": round(float(same_pnl), 4),
                    "opposite_drag": round(float(same_pnl - market_pnl), 4),
                    "opposite_copy_usd": round(float(opp_copy_usd), 4),
                    "opposite_trade_count": int(max(0, opp_trade_count)),
                }
            )

    roi = total_pnl / total_copy if total_copy > 0 else 0.0
    return SimulationResult(
        k=float(k),
        cap=float(cap),
        mkts=int(mkts),
        trades=int(trades),
        copy_usd=float(total_copy),
        pnl=float(total_pnl),
        roi=float(roi),
        supplement_market_count=int(supplement_market_count),
        supplement_usd_total=float(supplement_usd_total),
        supplement_usd_values=supplement_values,
        market_details=market_details,
    )


def run_sanity_checks() -> None:
    # Scenario 1 & 2: single-side / dual-side + cap correctness.
    test_stream = [
        {
            "condition_id": "c1",
            "market_slug": "s1",
            "post": [Trigger(token_id="A", window_usd=2000, ts=1), Trigger(token_id="B", window_usd=2200, ts=2)],
            "dominant_token": "A",
            "dominant_usd": 8000.0,
            "secondary_usd": 0.0,
            "diff_usd": 8000.0,
            "tokens": {
                "A": {"buy_usd": 10000.0, "total_pnl": 1000.0},
                "B": {"buy_usd": 10000.0, "total_pnl": -1000.0},
            },
        },
        {
            "condition_id": "c2",
            "market_slug": "s2",
            "post": [Trigger(token_id="A", window_usd=2100, ts=1)],
            "dominant_token": "A",
            "dominant_usd": 4000.0,
            "secondary_usd": 2000.0,
            "diff_usd": 2000.0,
            "tokens": {"A": {"buy_usd": 10000.0, "total_pnl": 500.0}},
        },
    ]

    r = simulate(test_stream, fixed_copy_usd=150.0, max_follows=20, k=0.1, cap=500.0, collect_market_details=True)
    # c1 supplement: min(500, 8000*0.1) = 500; c2 supplement=min(500,2000*0.1)=200
    expected_supp = 700.0
    assert abs(r.supplement_usd_total - expected_supp) < 1e-9, "supplement formula/cap mismatch"
    assert r.supplement_market_count == 2, "supplement should happen once per unlocked market"

    # Scenario 3: supplement should be one-time at unlock, while normal follow continues.
    assert r.trades == 3, "normal follow trades should continue unchanged"

    # Scenario 4: k=0 or cap=0 should match baseline behavior.
    b1 = simulate(test_stream, fixed_copy_usd=150.0, max_follows=20, k=0.0, cap=9999.0, collect_market_details=False)
    b2 = simulate(test_stream, fixed_copy_usd=150.0, max_follows=20, k=0.5, cap=0.0, collect_market_details=False)
    b0 = simulate(test_stream, fixed_copy_usd=150.0, max_follows=20, k=0.0, cap=0.0, collect_market_details=False)
    assert abs(b1.copy_usd - b0.copy_usd) < 1e-9 and abs(b1.pnl - b0.pnl) < 1e-9
    assert abs(b2.copy_usd - b0.copy_usd) < 1e-9 and abs(b2.pnl - b0.pnl) < 1e-9

    # Scenario 5: reproducibility.
    r2 = simulate(test_stream, fixed_copy_usd=150.0, max_follows=20, k=0.1, cap=500.0, collect_market_details=True)
    assert abs(r.copy_usd - r2.copy_usd) < 1e-9 and abs(r.pnl - r2.pnl) < 1e-9


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Directional top-up grid for delayed-follow")
    ap.add_argument("--cache", type=str, default=str(SIM_ROOT / "output" / "events_cache_0xee613b.jsonl.gz"))
    ap.add_argument("--agg-threshold", type=float, default=2000.0)
    ap.add_argument("--agg-window-min", type=int, default=60)
    ap.add_argument("--skip-n", type=int, default=8)
    ap.add_argument("--fixed-copy-usd", type=float, default=150.0)
    ap.add_argument("--max-follows", type=int, default=20)
    ap.add_argument("--copy-multiplier-limit", type=float, default=2.0)
    ap.add_argument(
        "--k-values",
        type=str,
        default="0.002,0.003,0.005,0.0075,0.01,0.0125,0.015,0.02,0.025,0.03,0.04,0.05,0.06,0.07,0.08",
    )
    ap.add_argument(
        "--cap-values",
        type=str,
        default="75,100,150,200,300,450,600,750,900,1050,1200,1350,1500,1800,2100,2400",
    )
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--skip-sanity-checks", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.time()

    if not args.skip_sanity_checks:
        run_sanity_checks()

    cache_path = Path(args.cache)
    if not cache_path.exists():
        print(f"[error] cache not found: {cache_path}")
        return 1

    print("Loading events...")
    events = load_events(cache_path)
    markets = build_markets(events)
    print(f"  events={len(events):,}, markets={len(markets):,}")

    tokens_need: set[str] = set()
    for m in markets.values():
        for tid, t in m["tokens"].items():
            if t["buy_shares"] - t["sell_shares"] > 1e-9:
                tokens_need.add(tid)

    print(f"Fetching prices for {len(tokens_need):,} tokens...")
    price_map = fetch_prices_for_tokens(sorted(tokens_need), timeout_s=30.0, workers=16)
    compute_token_pnl(markets, price_map)

    streams = prepare_streams(
        markets=markets,
        agg_threshold=float(args.agg_threshold),
        agg_window_s=int(args.agg_window_min) * 60,
        skip_n=int(args.skip_n),
    )
    print(f"Prepared streams: {len(streams):,} markets with unlock potential")

    baseline = simulate(
        streams,
        fixed_copy_usd=float(args.fixed_copy_usd),
        max_follows=int(args.max_follows),
        k=0.0,
        cap=0.0,
        collect_market_details=True,
    )
    copy_limit = baseline.copy_usd * float(args.copy_multiplier_limit)

    k_values = parse_csv_floats(args.k_values)
    cap_values = parse_csv_floats(args.cap_values)
    if not k_values or not cap_values:
        print("[error] empty k-values or cap-values")
        return 1

    # Real-data regression check: k=0 or cap=0 must equal baseline.
    control_k0 = simulate(
        streams,
        fixed_copy_usd=float(args.fixed_copy_usd),
        max_follows=int(args.max_follows),
        k=0.0,
        cap=max(cap_values),
        collect_market_details=False,
    )
    control_cap0 = simulate(
        streams,
        fixed_copy_usd=float(args.fixed_copy_usd),
        max_follows=int(args.max_follows),
        k=max(k_values),
        cap=0.0,
        collect_market_details=False,
    )
    eps = 1e-8
    if (
        abs(control_k0.copy_usd - baseline.copy_usd) > eps
        or abs(control_k0.pnl - baseline.pnl) > eps
        or abs(control_cap0.copy_usd - baseline.copy_usd) > eps
        or abs(control_cap0.pnl - baseline.pnl) > eps
    ):
        raise RuntimeError("Regression check failed: k=0/cap=0 should match baseline")

    print(
        f"Sweeping {len(k_values) * len(cap_values):,} combos "
        f"(copy<= {args.copy_multiplier_limit:.2f}x baseline)..."
    )
    feasible: list[SimulationResult] = []
    for k in k_values:
        for cap in cap_values:
            r = simulate(
                streams,
                fixed_copy_usd=float(args.fixed_copy_usd),
                max_follows=int(args.max_follows),
                k=float(k),
                cap=float(cap),
                collect_market_details=False,
            )
            if r.copy_usd <= copy_limit + 1e-9:
                feasible.append(r)

    if not feasible:
        print("[error] no feasible result under copy-limit")
        return 1

    feasible.sort(key=lambda x: (x.pnl, x.roi), reverse=True)
    best = feasible[0]
    # Re-run best with market details for risk output.
    best_with_details = simulate(
        streams,
        fixed_copy_usd=float(args.fixed_copy_usd),
        max_follows=int(args.max_follows),
        k=best.k,
        cap=best.cap,
        collect_market_details=True,
    )

    # Structural supplement stats on best config.
    supp_sorted = sorted(best_with_details.supplement_usd_values)
    supp_stats = {
        "count": len(supp_sorted),
        "total": round(float(sum(supp_sorted)), 2),
        "avg": round(float(statistics.mean(supp_sorted)), 4) if supp_sorted else 0.0,
        "p50": round(float(quantile_from_sorted(supp_sorted, 0.50) or 0.0), 4),
        "p75": round(float(quantile_from_sorted(supp_sorted, 0.75) or 0.0), 4),
        "p90": round(float(quantile_from_sorted(supp_sorted, 0.90) or 0.0), 4),
        "p95": round(float(quantile_from_sorted(supp_sorted, 0.95) or 0.0), 4),
        "max": round(float(supp_sorted[-1]), 4) if supp_sorted else 0.0,
    }

    # Risk: opposite drag top markets (positive means opposite side hurt pnl).
    drag_rows = sorted(
        best_with_details.market_details,
        key=lambda x: float(x.get("opposite_drag") or 0.0),
        reverse=True,
    )
    top_n = max(1, int(args.top_n))
    top_opposite_drag = drag_rows[:top_n]

    # Global deltas.
    delta_pnl = best_with_details.pnl - baseline.pnl
    delta_roi_pp = (best_with_details.roi - baseline.roi) * 100.0
    delta_copy = best_with_details.copy_usd - baseline.copy_usd

    print("\n=== BASELINE ===")
    print(
        f"copy=${baseline.copy_usd:,.2f} pnl=${baseline.pnl:+,.2f} "
        f"roi={baseline.roi*100:+.3f}% mkts={baseline.mkts} trades={baseline.trades}"
    )
    print("\n=== BEST UNDER COPY LIMIT ===")
    print(
        f"k={best_with_details.k:.4f} cap={best_with_details.cap:.0f} "
        f"copy=${best_with_details.copy_usd:,.2f} pnl=${best_with_details.pnl:+,.2f} "
        f"roi={best_with_details.roi*100:+.3f}% "
        f"(Δpnl={delta_pnl:+,.2f}, Δroi={delta_roi_pp:+.3f}pp, Δcopy={delta_copy:+,.2f})"
    )
    print(
        f"supplement markets={best_with_details.supplement_market_count:,}, "
        f"supplement total=${best_with_details.supplement_usd_total:,.2f}"
    )

    ranked = []
    for r in feasible[:top_n]:
        ranked.append(
            {
                **r.as_dict(),
                "delta_pnl": round(r.pnl - baseline.pnl, 2),
                "delta_roi_pp": round((r.roi - baseline.roi) * 100.0, 4),
                "delta_copy": round(r.copy_usd - baseline.copy_usd, 2),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cache_file": str(cache_path),
        "params": {
            "agg_threshold": float(args.agg_threshold),
            "agg_window_minutes": int(args.agg_window_min),
            "skip_n": int(args.skip_n),
            "fixed_copy_usd": float(args.fixed_copy_usd),
            "max_follows": int(args.max_follows),
            "copy_multiplier_limit": float(args.copy_multiplier_limit),
            "k_values": k_values,
            "cap_values": cap_values,
        },
        "baseline": baseline.as_dict(),
        "best": {
            **best_with_details.as_dict(),
            "delta_pnl": round(delta_pnl, 2),
            "delta_roi_pp": round(delta_roi_pp, 4),
            "delta_copy": round(delta_copy, 2),
            "supplement_stats": supp_stats,
        },
        "top_candidates_by_pnl": ranked,
        "risk_opposite_drag_top_markets": top_opposite_drag,
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = SIM_ROOT / "output" / f"directional_topup_grid_{ts}.json"
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[output] {out_path}")
    print(f"[done] {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
