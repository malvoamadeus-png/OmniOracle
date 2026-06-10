"""
PolySport v2 - Settlement Period & Liquidity Depth
Usage: python polysport_explore.py
"""
import json, sys, time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

SETTLEMENT_WARN_DAYS = 30
DEPTH_WARN_USD = 10_000
DEPTH_SPREAD_PCT = 0.05

SAMPLE_ADDRESSES = [
    "0x5c3a1a602848565bb16165fcd460b00c3d43020b",
    "0x53ecc53e7a69aad0e6dda60264cc2e363092df91",
    "0xe52c0a1327a12edc7bd54ea6f37ce00a4ca96924",
]

session = requests.Session()
session.headers.update({"User-Agent": "PolySport-Explore/1.0"})

def safe_get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=15)
            if r.status_code == 404:
                return None  # 404 = no data, not an error
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                return None
            time.sleep(1)
    return None

def fetch_positions(address):
    print(f"\n[*] Fetching positions: {address[:10]}...")
    all_pos = []
    offset = 0
    while True:
        data = safe_get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": 0, "limit": 100, "offset": offset})
        if not data or not isinstance(data, list) or len(data) == 0:
            break
        all_pos.extend(data)
        print(f"  -> got {len(data)} (total {len(all_pos)})")
        if len(data) < 100:
            break
        offset += 100
        time.sleep(0.3)
    print(f"  OK: {len(all_pos)} positions")
    return all_pos

_cache = {}
def fetch_market_info(condition_id):
    if condition_id in _cache:
        return _cache[condition_id]
    data = safe_get(f"{GAMMA_API}/markets", params={"condition_id": condition_id, "limit": 1})
    if data and isinstance(data, list) and len(data) > 0:
        _cache[condition_id] = data[0]
        return data[0]
    _cache[condition_id] = None
    return None

def calc_days_to_settlement(info):
    now = datetime.now(timezone.utc)
    for field in ["end_date_iso", "endDate", "end_date", "resolution_date",
                   "resolutionDate", "closed_at", "closedAt", "game_start_time"]:
        val = info.get(field)
        if val:
            for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(str(val), fmt).replace(tzinfo=timezone.utc)
                    return round((dt - now).total_seconds() / 86400, 1)
                except ValueError:
                    continue
    return None

def is_market_active(info):
    """Check if market is still active (not resolved)"""
    if not info:
        return False
    # Check various fields that indicate resolution
    if info.get("closed") == True or info.get("resolved") == True:
        return False
    if info.get("active") == False:
        return False
    status = str(info.get("market_status", "") or info.get("status", "")).lower()
    if status in ["resolved", "closed", "settled"]:
        return False
    return True

def fetch_orderbook_depth(token_id):
    data = safe_get(f"{CLOB_API}/book", params={"token_id": token_id})
    if not data or not isinstance(data, dict):
        return None
    bids = data.get("bids", [])
    asks = data.get("asks", [])
    if not bids and not asks:
        return None
    best_bid = float(bids[0].get("price", 0)) if bids else 0
    best_ask = float(asks[0].get("price", 1)) if asks else 1
    mid = (best_bid + best_ask) / 2 if (best_bid > 0 and best_ask > 0) else best_bid or best_ask
    if mid <= 0:
        return None
    spread_pct = (best_ask - best_bid) / mid * 100 if mid > 0 else 999
    lower = mid * (1 - DEPTH_SPREAD_PCT)
    upper = mid * (1 + DEPTH_SPREAD_PCT)
    bid_depth = sum(float(o.get("price",0))*float(o.get("size",0)) for o in bids if float(o.get("price",0)) >= lower)
    ask_depth = sum(float(o.get("price",0))*float(o.get("size",0)) for o in asks if float(o.get("price",0)) <= upper)
    return {"mid": round(mid,4), "bid_depth_usd": round(bid_depth,2), "ask_depth_usd": round(ask_depth,2),
            "total_depth_usd": round(bid_depth+ask_depth,2), "spread_pct": round(spread_pct,2)}

def analyze_address(address):
    positions = fetch_positions(address)
    if not positions:
        return {"address": address, "error": "No positions found", "markets": []}

    market_results = []
    active_count = 0
    settled_count = 0

    for i, pos in enumerate(positions):
        cid = pos.get("market") or pos.get("conditionId") or pos.get("condition_id", "")
        asset = pos.get("asset") or pos.get("tokenId") or pos.get("token_id", "")
        slug = pos.get("slug") or pos.get("market_slug") or pos.get("eventSlug") or ""
        size = float(pos.get("size", 0))
        if not cid and not asset:
            continue

        display = slug[:35] if slug else cid[:20]
        print(f"  [{i+1}/{len(positions)}] {display}...", end="")

        # Step A: Get market info from Gamma API
        market_info = fetch_market_info(cid) if cid else None
        days = None
        question = ""
        volume = None
        active = False

        if market_info:
            days = calc_days_to_settlement(market_info)
            question = market_info.get("question","") or market_info.get("title","") or ""
            v = market_info.get("volume") or market_info.get("volumeNum")
            if v: volume = float(v)
            active = is_market_active(market_info)

        # Step B: Only check orderbook for ACTIVE markets
        depth_info = None
        if active and asset:
            # Try using clobTokenIds from market info first
            clob_ids = None
            if market_info:
                ct = market_info.get("clobTokenIds")
                if ct:
                    clob_ids = ct.split(",") if isinstance(ct, str) else ct

            if clob_ids:
                for cid_try in clob_ids:
                    cid_try = cid_try.strip()
                    if cid_try:
                        depth_info = fetch_orderbook_depth(cid_try)
                        if depth_info:
                            break
            if not depth_info:
                depth_info = fetch_orderbook_depth(asset)

        # Flags
        flags = []
        if days is not None and days < 0:
            flags.append("settled")
            settled_count += 1
        elif days is not None and days > SETTLEMENT_WARN_DAYS:
            flags.append(f"!! FAR({days:.0f}d)")
        
        if active:
            active_count += 1
            if depth_info and depth_info["total_depth_usd"] < DEPTH_WARN_USD:
                flags.append(f"!! LOW_DEPTH(${depth_info['total_depth_usd']:,.0f})")
            elif not depth_info and active:
                flags.append("no_book")

        status = "settled" if not active else "OK" if not flags else " | ".join(flags)
        print(f" {status}")

        market_results.append({
            "slug": slug[:40] if slug else cid[:20] if cid else "?",
            "question": question[:60], "size": size,
            "days_to_settle": days, "active": active,
            "depth_usd": depth_info["total_depth_usd"] if depth_info else None,
            "spread_pct": depth_info["spread_pct"] if depth_info else None,
            "volume": volume, "flags": status,
        })
        time.sleep(0.15)

    # Summary - only count active positions for settlement/liquidity stats
    active_markets = [m for m in market_results if m["active"]]
    sd = [m["days_to_settle"] for m in active_markets if m["days_to_settle"] is not None and m["days_to_settle"] > 0]
    dp = [m["depth_usd"] for m in active_markets if m["depth_usd"] is not None]

    summary = {
        "address": address,
        "total_positions": len(positions),
        "active_positions": active_count,
        "settled_positions": settled_count,
        "analyzed": len(market_results),
        "avg_settle_days": round(sum(sd)/len(sd),1) if sd else None,
        "median_settle_days": round(sorted(sd)[len(sd)//2],1) if sd else None,
        "pct_far_term": round(sum(1 for d in sd if d>SETTLEMENT_WARN_DAYS)/max(len(sd),1)*100,1) if sd else None,
        "median_depth_usd": round(sorted(dp)[len(dp)//2],2) if dp else None,
        "pct_low_liquidity": round(sum(1 for d in dp if d<DEPTH_WARN_USD)/max(len(dp),1)*100,1) if dp else None,
        "markets": market_results,
    }

    verdict = []
    if summary["pct_far_term"] and summary["pct_far_term"] > 50:
        verdict.append("[RED] >50% far-term, bad for copy-trade")
    elif summary["pct_far_term"] and summary["pct_far_term"] > 20:
        verdict.append("[YELLOW] some far-term positions")
    if summary["pct_low_liquidity"] and summary["pct_low_liquidity"] > 50:
        verdict.append("[RED] >50% low liquidity")
    elif summary["pct_low_liquidity"] and summary["pct_low_liquidity"] > 20:
        verdict.append("[YELLOW] some low liquidity")
    if not verdict:
        verdict.append("[GREEN] settlement & liquidity OK")
    summary["verdict"] = " | ".join(verdict)
    return summary

def print_report(result):
    print("\n" + "="*70)
    print(f"  REPORT: {result['address']}")
    print("="*70)
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return
    print(f"  Total positions:   {result['total_positions']}")
    print(f"  Active:            {result['active_positions']}")
    print(f"  Settled:           {result['settled_positions']}")
    print(f"  Avg settle days:   {result.get('avg_settle_days','N/A')}")
    print(f"  Median settle days:{result.get('median_settle_days','N/A')}")
    print(f"  Far-term pct:      {result.get('pct_far_term','N/A')}%")
    print(f"  Median depth(USD): {result.get('median_depth_usd','N/A')}")
    print(f"  Low-liq pct:       {result.get('pct_low_liquidity','N/A')}%")
    print(f"\n  >>> VERDICT: {result['verdict']}")

    # Show active positions detail
    active = [m for m in result["markets"] if m["active"]]
    settled = [m for m in result["markets"] if not m["active"]]

    if active:
        print(f"\n  --- Active Positions ({len(active)}) ---")
        for m in active:
            s = f"{m['days_to_settle']:.0f}d" if m["days_to_settle"] is not None else "?"
            d = f"${m['depth_usd']:,.0f}" if m["depth_usd"] is not None else "?"
            print(f"    {m['slug'][:30]:30s}  settle={s:>6s}  depth={d:>10s}  {m['flags']}")

    print(f"\n  --- Settled Positions ({len(settled)}) --- (skipped depth check)")
    for m in settled[:5]:
        print(f"    {m['slug'][:30]:30s}  {m['flags']}")
    if len(settled) > 5:
        print(f"    ... and {len(settled)-5} more settled positions")

    out_file = f"polysport_report_{result['address'][:10]}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Saved: {out_file}")

if __name__ == "__main__":
    addrs = sys.argv[1:] if len(sys.argv) > 1 else SAMPLE_ADDRESSES
    if not addrs:
        print("No addresses. Usage: python polysport_explore.py 0xADDRESS")
        sys.exit(1)
    print(f"Analyzing {len(addrs)} address(es)...")
    for addr in addrs:
        result = analyze_address(addr)
        print_report(result)
