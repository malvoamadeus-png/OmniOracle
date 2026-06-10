"""
PolySport v3 - Settlement Period & Liquidity Depth
Fix: use slug to query Gamma API instead of condition_id
Polymarket hierarchy: event > market(conditionID) > asset(yes/no token)
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
                return None
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

# ─── Step 1: 抓持仓 ───
def fetch_positions(address):
    print(f"\n[*] Fetching positions: {address[:10]}...")
    all_pos = []
    offset = 0
    while True:
        data = safe_get(f"{DATA_API}/positions",
                       params={"user": address, "sizeThreshold": 0, "limit": 100, "offset": offset})
        if not data or not isinstance(data, list) or len(data) == 0:
            break
        all_pos.extend(data)
        if len(data) < 100:
            break
        offset += 100
        time.sleep(0.3)
    print(f"  OK: {len(all_pos)} positions")
    return all_pos

# ─── Step 2: 用 slug 查 Gamma API 拿市场信息 ───
# 南枳指出: Polymarket 层级是 event > market(conditionID) > asset(yes/no)
# Gamma API 应该用 slug 查，不是用 condition_id
_cache = {}

def fetch_market_by_slug(slug):
    """用 slug 查 Gamma API 的 /markets 端点"""
    if slug in _cache:
        return _cache[slug]

    # 方法1: 用 slug 查
    data = safe_get(f"{GAMMA_API}/markets", params={"slug": slug, "limit": 1})
    if data and isinstance(data, list) and len(data) > 0:
        _cache[slug] = data[0]
        return data[0]

    # 方法2: 有些 slug 可能需要去 events 端点
    data = safe_get(f"{GAMMA_API}/events", params={"slug": slug, "limit": 1})
    if data and isinstance(data, list) and len(data) > 0:
        event = data[0]
        # event 下面可能有多个 markets
        markets = event.get("markets", [])
        if markets:
            _cache[slug] = markets[0]
            return markets[0]
        _cache[slug] = event
        return event

    _cache[slug] = None
    return None

def calc_days_to_settlement(info):
    """从市场/事件信息里提取距结算的天数"""
    now = datetime.now(timezone.utc)
    # 尝试各种可能的字段名
    for field in ["end_date_iso", "endDate", "end_date", "resolution_date",
                   "resolutionDate", "closed_at", "closedAt",
                   "game_start_time", "startDate", "start_date_iso"]:
        val = info.get(field)
        if val:
            for fmt in ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(str(val), fmt)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return round((dt - now).total_seconds() / 86400, 1)
                except ValueError:
                    continue
    return None

def is_resolved(info):
    """判断市场是否已经结算"""
    if not info:
        return True
    if info.get("closed") == True or info.get("resolved") == True:
        return True
    if info.get("active") == False:
        return True
    status = str(info.get("market_status", "") or info.get("status", "")).lower()
    if status in ["resolved", "closed", "settled"]:
        return True
    return False

# ─── Step 3: 用 CLOB API 查订单簿深度 ───
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
    lower, upper = mid * (1 - DEPTH_SPREAD_PCT), mid * (1 + DEPTH_SPREAD_PCT)
    bid_depth = sum(float(o.get("price",0))*float(o.get("size",0)) for o in bids if float(o.get("price",0)) >= lower)
    ask_depth = sum(float(o.get("price",0))*float(o.get("size",0)) for o in asks if float(o.get("price",0)) <= upper)
    return {"mid": round(mid,4), "total_depth_usd": round(bid_depth+ask_depth,2), "spread_pct": round(spread_pct,2)}

# ─── Step 4: 分析地址 ───
def analyze_address(address):
    positions = fetch_positions(address)
    if not positions:
        return {"address": address, "error": "No positions found", "markets": []}

    market_results = []
    active_count = 0
    settled_count = 0

    # 先打印一条 position 的原始数据结构，方便调试
    if positions:
        print(f"  [DEBUG] Position keys: {list(positions[0].keys())}")
        sample = positions[0]
        for k in ["slug", "market_slug", "eventSlug", "market", "conditionId",
                   "condition_id", "asset", "tokenId", "token_id", "proxyTicker"]:
            if sample.get(k):
                print(f"  [DEBUG] {k} = {str(sample[k])[:60]}")

    for i, pos in enumerate(positions):
        # 提取关键字段
        slug = pos.get("slug") or pos.get("market_slug") or pos.get("eventSlug") or pos.get("proxyTicker") or ""
        condition_id = pos.get("market") or pos.get("conditionId") or pos.get("condition_id", "")
        asset = pos.get("asset") or pos.get("tokenId") or pos.get("token_id", "")
        size = float(pos.get("size", 0))

        if not slug and not condition_id:
            continue

        display = slug[:35] if slug else condition_id[:20]
        print(f"  [{i+1}/{len(positions)}] {display}...", end="")

        # 用 slug 查 Gamma API（核心修复）
        market_info = fetch_market_by_slug(slug) if slug else None

        # 如果 slug 查不到，尝试打印原始数据帮助调试
        if not market_info and slug:
            print(f" (slug miss)", end="")

        days = None
        question = ""
        volume = None
        resolved = True

        if market_info:
            days = calc_days_to_settlement(market_info)
            question = market_info.get("question","") or market_info.get("title","") or ""
            v = market_info.get("volume") or market_info.get("volumeNum")
            if v: volume = float(v)
            resolved = is_resolved(market_info)

            # 尝试从 market_info 里拿 clobTokenIds 来查深度
            clob_tokens = market_info.get("clobTokenIds", "")
            if isinstance(clob_tokens, str) and clob_tokens:
                clob_tokens = [t.strip() for t in clob_tokens.split(",") if t.strip()]
            elif not isinstance(clob_tokens, list):
                clob_tokens = []
        else:
            clob_tokens = []

        # 查订单簿深度（只对未结算市场查）
        depth_info = None
        if not resolved:
            active_count += 1
            # 优先用 Gamma 返回的 clobTokenIds
            for tid in clob_tokens:
                depth_info = fetch_orderbook_depth(tid)
                if depth_info:
                    break
            # 备选用 asset
            if not depth_info and asset:
                depth_info = fetch_orderbook_depth(asset)
        else:
            settled_count += 1

        # 标记
        flags = []
        if resolved:
            flags.append("settled")
        else:
            if days is not None and days > SETTLEMENT_WARN_DAYS:
                flags.append(f"FAR({days:.0f}d)")
            if depth_info and depth_info["total_depth_usd"] < DEPTH_WARN_USD:
                flags.append(f"LOW_DEPTH(${depth_info['total_depth_usd']:,.0f})")
            elif not depth_info:
                flags.append("no_book")

        status = " | ".join(flags) if flags else "OK"
        print(f" [{status}]  days={days}  q={question[:30]}")

        market_results.append({
            "slug": slug[:40], "question": question[:60], "size": size,
            "days_to_settle": days, "resolved": resolved,
            "depth_usd": depth_info["total_depth_usd"] if depth_info else None,
            "spread_pct": depth_info["spread_pct"] if depth_info else None,
            "volume": volume, "flags": status,
        })
        time.sleep(0.15)

    # 汇总 - 只看未结算持仓
    active_markets = [m for m in market_results if not m["resolved"]]
    sd = [m["days_to_settle"] for m in active_markets if m["days_to_settle"] is not None and m["days_to_settle"] > 0]
    dp = [m["depth_usd"] for m in active_markets if m["depth_usd"] is not None]

    summary = {
        "address": address, "total_positions": len(positions),
        "active_positions": active_count, "settled_positions": settled_count,
        "avg_settle_days": round(sum(sd)/len(sd),1) if sd else None,
        "median_settle_days": round(sorted(sd)[len(sd)//2],1) if sd else None,
        "pct_far_term": round(sum(1 for d in sd if d>SETTLEMENT_WARN_DAYS)/max(len(sd),1)*100,1) if sd else None,
        "median_depth_usd": round(sorted(dp)[len(dp)//2],2) if dp else None,
        "pct_low_liquidity": round(sum(1 for d in dp if d<DEPTH_WARN_USD)/max(len(dp),1)*100,1) if dp else None,
        "markets": market_results,
    }
    verdict = []
    if summary["pct_far_term"] and summary["pct_far_term"] > 50:
        verdict.append("[RED] >50% far-term")
    elif summary["pct_far_term"] and summary["pct_far_term"] > 20:
        verdict.append("[YELLOW] some far-term")
    if summary["pct_low_liquidity"] and summary["pct_low_liquidity"] > 50:
        verdict.append("[RED] >50% low liquidity")
    elif summary["pct_low_liquidity"] and summary["pct_low_liquidity"] > 20:
        verdict.append("[YELLOW] some low liquidity")
    if not verdict:
        verdict.append("[GREEN] OK")
    summary["verdict"] = " | ".join(verdict)
    return summary

def print_report(result):
    print("\n" + "="*70)
    print(f"  REPORT: {result['address']}")
    print("="*70)
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        return
    print(f"  Total: {result['total_positions']}  Active: {result['active_positions']}  Settled: {result['settled_positions']}")
    print(f"  Avg settle days:    {result.get('avg_settle_days','N/A')}")
    print(f"  Median settle days: {result.get('median_settle_days','N/A')}")
    print(f"  Far-term pct:       {result.get('pct_far_term','N/A')}%")
    print(f"  Median depth(USD):  {result.get('median_depth_usd','N/A')}")
    print(f"  Low-liq pct:        {result.get('pct_low_liquidity','N/A')}%")
    print(f"  >>> VERDICT: {result['verdict']}")

    active = [m for m in result["markets"] if not m["resolved"]]
    settled = [m for m in result["markets"] if m["resolved"]]

    if active:
        print(f"\n  --- Active ({len(active)}) ---")
        for m in active:
            s = f"{m['days_to_settle']:.0f}d" if m["days_to_settle"] is not None else "?"
            d = f"${m['depth_usd']:,.0f}" if m["depth_usd"] is not None else "?"
            print(f"    {m['slug'][:32]:32s} settle={s:>6s} depth={d:>10s} {m['flags']}")

    if settled:
        print(f"\n  --- Settled ({len(settled)}) ---")
        for m in settled[:3]:
            print(f"    {m['slug'][:32]:32s} (settled)")
        if len(settled) > 3:
            print(f"    ... +{len(settled)-3} more")

    out = f"polysport_report_{result['address'][:10]}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n  Saved: {out}")

if __name__ == "__main__":
    addrs = sys.argv[1:] if len(sys.argv) > 1 else SAMPLE_ADDRESSES
    if not addrs:
        print("Usage: python polysport_explore.py 0xADDRESS")
        sys.exit(1)
    print(f"Analyzing {len(addrs)} address(es)...")
    for addr in addrs:
        print_report(analyze_address(addr))