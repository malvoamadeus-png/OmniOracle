"""赎回已结算仓位 — 支持 Proxy Wallet 和 EOA 双模式.

Proxy 模式 (Google/Email 登录):
  通过 Relayer 发送 gasless 交易。
  依赖: py-clob-client, poly-web3, py-builder-relayer-client, py-builder-signing-sdk
  环境变量: PRIVATE_KEY, FUNDER_ADDRESS, CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE

EOA 模式 (自托管钱包):
  优先通过 Relayer API Key gasless 赎回（需要 FUNDER_ADDRESS 即 Safe 地址）。
  无 Relayer Key 时 fallback 到直接链上调用（需要 MATIC）。
  依赖: py-clob-client, web3
  环境变量: PRIVATE_KEY, FUNDER_ADDRESS (Safe地址), RELAYER_API_KEY

支持 --account SUFFIX 参数，读取带后缀的环境变量（多账号模式）。
支持 --wallet-type proxy|eoa 参数。
"""

import argparse
import concurrent.futures
import json
import os
import sys
from pathlib import Path
from typing import Optional

_PACKAGES_DIR = Path(__file__).resolve().parent.parent
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

from copytrade.paths import DOTENV_PATH


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(int(minimum), int(float(raw)))
    except ValueError:
        return int(default)


# relay_client.execute() 没有内置超时，用线程池强制超时
RELAY_EXECUTE_TIMEOUT_S = _env_int("COPYTRADE_RELAY_EXECUTE_TIMEOUT_S", 45, minimum=5)
MAX_REDEEM_PER_RUN = _env_int("COPYTRADE_MAX_REDEEM_PER_RUN", 20, minimum=1)  # 每次最多 redeem 20 个，避免超时
MAX_MERGE_PER_RUN = _env_int("COPYTRADE_MAX_MERGE_PER_RUN", 8, minimum=1)



def _load_dotenv():
    if not DOTENV_PATH.exists():
        return
    for raw in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


def _refresh_runtime_limits_from_env() -> None:
    global RELAY_EXECUTE_TIMEOUT_S, MAX_REDEEM_PER_RUN, MAX_MERGE_PER_RUN
    RELAY_EXECUTE_TIMEOUT_S = _env_int("COPYTRADE_RELAY_EXECUTE_TIMEOUT_S", RELAY_EXECUTE_TIMEOUT_S, minimum=5)
    MAX_REDEEM_PER_RUN = _env_int("COPYTRADE_MAX_REDEEM_PER_RUN", MAX_REDEEM_PER_RUN, minimum=1)
    MAX_MERGE_PER_RUN = _env_int("COPYTRADE_MAX_MERGE_PER_RUN", MAX_MERGE_PER_RUN, minimum=1)


def _run_with_timeout(fn, timeout_s: int):
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        result = future.result(timeout=max(1, int(timeout_s)))
    except concurrent.futures.TimeoutError:
        future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    except Exception:
        pool.shutdown(wait=True)
        raise
    else:
        pool.shutdown(wait=True)
        return result


def _fetch_all_positions(user_address: str) -> list:
    """直接调用 data-api 获取所有仓位（不限 redeemable）."""
    import requests as _req
    url = "https://data-api.polymarket.com/positions"
    params = {"user": user_address, "sizeThreshold": "0.01", "limit": 200}
    try:
        resp = _req.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Fetch positions for merge failed: {e}", file=sys.stderr, flush=True)
        return []


def _find_mergeable_pairs(positions: list) -> list:
    """找出同一 conditionId 下同时持有两个 outcome 的仓位对.

    返回: [{"conditionId": str, "amount": float, "title": str}, ...]
    """
    from collections import defaultdict
    by_condition = defaultdict(dict)  # conditionId -> {outcomeIndex: size}
    titles = {}
    neg_risk_by_condition = {}
    for p in positions:
        cid = p.get("conditionId")
        idx = p.get("outcomeIndex")
        size = float(p.get("size") or 0)
        mergeable = p.get("mergeable", False)
        if not cid or idx is None or size <= 0 or not mergeable:
            continue
        by_condition[cid][idx] = by_condition[cid].get(idx, 0) + size
        if cid not in titles:
            titles[cid] = p.get("title", "")
        neg_risk_by_condition[cid] = bool(neg_risk_by_condition.get(cid) or _is_negative_risk_position(p))

    pairs = []
    for cid, outcomes in by_condition.items():
        if len(outcomes) >= 2:
            # merge 数量 = 两边最小值，截断到小数点后 2 位
            amount = int(min(outcomes.values()) * 100) / 100.0
            if amount >= 0.1:
                pairs.append({
                    "conditionId": cid,
                    "amount": amount,
                    "title": titles.get(cid, ""),
                    "negativeRisk": bool(neg_risk_by_condition.get(cid)),
                })
    return pairs


def _run_proxy(private_key: str, _env, args) -> int:
    """Proxy Wallet 模式: 通过 relayer 提交 v2 adapter 赎回/merge。"""
    funder = _env("FUNDER_ADDRESS")
    clob_key = _env("CLOB_API_KEY")
    clob_secret = _env("CLOB_SECRET")
    clob_pass = _env("CLOB_PASS_PHRASE")

    if not funder:
        print(json.dumps({"ok": False, "error": "FUNDER_ADDRESS required for proxy mode"}))
        return 1
    if not (clob_key and clob_secret and clob_pass):
        print(json.dumps({"ok": False, "error": "CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE required for proxy mode"}))
        return 1

    try:
        from web3 import Web3
    except ImportError:
        print(json.dumps({"ok": False, "error": "web3 not installed"}))
        return 1

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    relay_client = _build_relay_client(private_key, clob_key, clob_secret, clob_pass)
    user_addr = funder
    positions = _fetch_positions_api(user_addr)
    redeemable = [p for p in positions if p.get("redeemable")]
    if len(redeemable) > MAX_REDEEM_PER_RUN:
        redeemable = sorted(redeemable, key=lambda p: float(p.get("size") or 0), reverse=True)[:MAX_REDEEM_PER_RUN]
        print(f"Capped redeemable to {MAX_REDEEM_PER_RUN} (by size desc)", file=sys.stderr, flush=True)

    return _execute_redeem_merge(
        user_addr,
        redeemable,
        args,
        redeem_fn=lambda: _eoa_official_redeem_all(w3, relay_client, redeemable),
        merge_many_fn=lambda pairs: _eoa_official_merge_many(w3, relay_client, pairs),
        merge_fn=lambda cid, amt, negative_risk=False: _eoa_official_merge(
            w3,
            relay_client,
            cid,
            amt,
            negative_risk=negative_risk,
        ),
    )


# --- EOA 模式 ---

# Polymarket CTF (Conditional Tokens Framework) 合约地址 on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
# USDC.e on Polygon (collateral token)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Polymarket CLOB v2 collateral/adapters on Polygon
PUSD_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
COLLATERAL_ONRAMP_ADDRESS = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
COLLATERAL_OFFRAMP_ADDRESS = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
CTF_COLLATERAL_ADAPTER_ADDRESS = "0xADa100874d00e3331D00F2007a9c336a65009718"
NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS = "0xAdA200001000ef00D07553cEE7006808F895c6F1"
# 最小 ABI: redeemPositions + mergePositions
CTF_ABI = json.loads("""[
  {
    "inputs": [
      {"name": "collateralToken", "type": "address"},
      {"name": "parentCollectionId", "type": "bytes32"},
      {"name": "conditionId", "type": "bytes32"},
      {"name": "indexSets", "type": "uint256[]"}
    ],
    "name": "redeemPositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "collateralToken", "type": "address"},
      {"name": "parentCollectionId", "type": "bytes32"},
      {"name": "conditionId", "type": "bytes32"},
      {"name": "partition", "type": "uint256[]"},
      {"name": "amount", "type": "uint256"}
    ],
    "name": "mergePositions",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  }
]""")
ERC20_ABI = json.loads("""[
  {"name":"approve","type":"function","stateMutability":"nonpayable","inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"outputs":[{"type":"bool"}]},
  {"name":"allowance","type":"function","stateMutability":"view","inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"outputs":[{"type":"uint256"}]},
  {"name":"balanceOf","type":"function","stateMutability":"view","inputs":[{"name":"account","type":"address"}],"outputs":[{"type":"uint256"}]}
]""")
PUSD_RAMP_ABI = json.loads("""[
  {"name":"wrap","type":"function","stateMutability":"nonpayable","inputs":[{"name":"_asset","type":"address"},{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"outputs":[]},
  {"name":"unwrap","type":"function","stateMutability":"nonpayable","inputs":[{"name":"_asset","type":"address"},{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"outputs":[]}
]""")

POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
PARENT_COLLECTION_ID = "0x" + "00" * 32  # 顶层 parentCollectionId = bytes32(0)


def _is_negative_risk_position(position: dict) -> bool:
    return bool(
        position.get("negativeRisk")
        or position.get("negRisk")
        or position.get("negative_risk")
    )


def _adapter_address(*, negative_risk: bool = False) -> str:
    return NEG_RISK_CTF_COLLATERAL_ADAPTER_ADDRESS if negative_risk else CTF_COLLATERAL_ADAPTER_ADDRESS


def _condition_bytes(condition_id: str) -> bytes:
    return bytes.fromhex(str(condition_id or "").replace("0x", ""))


def _amount_to_base_units(amount: float) -> int:
    return int(float(amount or 0.0) * 1_000_000)


def _build_redeem_calldata(w3, condition_id: str, *, negative_risk: bool = False) -> tuple[str, str]:
    from web3 import Web3

    adapter_addr = _adapter_address(negative_risk=negative_risk)
    adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=CTF_ABI)
    calldata = adapter.functions.redeemPositions(
        Web3.to_checksum_address(PUSD_ADDRESS),
        bytes.fromhex(PARENT_COLLECTION_ID.replace("0x", "")),
        _condition_bytes(condition_id),
        [1, 2],
    )._encode_transaction_data()
    return adapter_addr, calldata


def _build_merge_calldata(w3, condition_id: str, amount: float, *, negative_risk: bool = False) -> tuple[str, str]:
    from web3 import Web3

    adapter_addr = _adapter_address(negative_risk=negative_risk)
    adapter = w3.eth.contract(address=Web3.to_checksum_address(adapter_addr), abi=CTF_ABI)
    calldata = adapter.functions.mergePositions(
        Web3.to_checksum_address(PUSD_ADDRESS),
        bytes.fromhex(PARENT_COLLECTION_ID.replace("0x", "")),
        _condition_bytes(condition_id),
        [1, 2],
        _amount_to_base_units(amount),
    )._encode_transaction_data()
    return adapter_addr, calldata


def _build_wrap_calldata(w3, recipient: str, amount_raw: int) -> str:
    from web3 import Web3

    ramp = w3.eth.contract(address=Web3.to_checksum_address(COLLATERAL_ONRAMP_ADDRESS), abi=PUSD_RAMP_ABI)
    return ramp.functions.wrap(
        Web3.to_checksum_address(USDC_ADDRESS),
        Web3.to_checksum_address(recipient),
        int(amount_raw),
    )._encode_transaction_data()


def _build_unwrap_calldata(w3, recipient: str, amount_raw: int) -> str:
    from web3 import Web3

    ramp = w3.eth.contract(address=Web3.to_checksum_address(COLLATERAL_OFFRAMP_ADDRESS), abi=PUSD_RAMP_ABI)
    return ramp.functions.unwrap(
        Web3.to_checksum_address(USDC_ADDRESS),
        Web3.to_checksum_address(recipient),
        int(amount_raw),
    )._encode_transaction_data()


def _build_approve_calldata(w3, spender: str, amount_raw: int) -> str:
    from web3 import Web3

    token = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)
    return token.functions.approve(
        Web3.to_checksum_address(spender),
        int(amount_raw),
    )._encode_transaction_data()


def _build_pusd_approve_calldata(w3, spender: str, amount_raw: int) -> str:
    from web3 import Web3

    token = w3.eth.contract(address=Web3.to_checksum_address(PUSD_ADDRESS), abi=ERC20_ABI)
    return token.functions.approve(
        Web3.to_checksum_address(spender),
        int(amount_raw),
    )._encode_transaction_data()


def _run_eoa(private_key: str, _env, args) -> int:
    """EOA 模式: 优先通过官方 RelayClient (CLOB凭证) gasless 赎回，quota 耗尽时 fallback 到 Safe.execTransaction 自付 gas."""
    proxy_wallet = _env("FUNDER_ADDRESS")  # Polymarket 为 EOA 创建的 Gnosis Safe 地址
    clob_key = _env("CLOB_API_KEY")
    clob_secret = _env("CLOB_SECRET")
    clob_pass = _env("CLOB_PASS_PHRASE")

    try:
        from web3 import Web3
        from eth_account import Account
    except ImportError:
        print(json.dumps({"ok": False, "error": "web3 未安装，请运行 pip install web3"}))
        return 1

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    account = Account.from_key(private_key)
    user_addr = account.address
    print(f"EOA User: {user_addr}", file=sys.stderr, flush=True)

    use_official_relayer = bool(clob_key and clob_secret and clob_pass and proxy_wallet)

    if use_official_relayer:
        print(f"使用官方 RelayClient (gasless), Safe: {proxy_wallet[:10]}...", file=sys.stderr, flush=True)
    else:
        if not proxy_wallet:
            print(f"警告: 缺少 FUNDER_ADDRESS (Safe地址)，退回直接链上调用", file=sys.stderr, flush=True)
        elif not (clob_key and clob_secret and clob_pass):
            print(f"警告: 缺少 CLOB 凭证，退回直接链上调用", file=sys.stderr, flush=True)
        balance_wei = w3.eth.get_balance(user_addr)
        balance_matic = w3.from_wei(balance_wei, "ether")
        print(f"使用直接链上调用 (MATIC: {balance_matic:.4f})", file=sys.stderr, flush=True)
        if balance_matic < 0.01:
            print(f"警告: MATIC 余额过低，可能无法支付 gas", file=sys.stderr, flush=True)

    # 用 proxy_wallet 或 user_addr 查仓位
    query_addr = proxy_wallet if proxy_wallet else user_addr
    positions = _fetch_positions_api(query_addr)
    redeemable = [p for p in positions if p.get("redeemable")]
    if len(redeemable) > MAX_REDEEM_PER_RUN:
        redeemable = sorted(redeemable, key=lambda p: float(p.get("size") or 0), reverse=True)[:MAX_REDEEM_PER_RUN]
        print(f"Capped redeemable to {MAX_REDEEM_PER_RUN} (by size desc)", file=sys.stderr, flush=True)

    if use_official_relayer:
        relay_client = _build_relay_client(private_key, clob_key, clob_secret, clob_pass)

        def _redeem_with_fallback():
            results = _eoa_official_redeem_all(w3, relay_client, redeemable)
            # 检查是否触发了 quota 耗尽或 relay 超时
            quota_hit = any(
                "quota exceeded" in r.get("error", "").lower()
                or "relay timeout" in r.get("error", "").lower()
                for r in results
            )
            if quota_hit and proxy_wallet:
                balance_matic = w3.from_wei(w3.eth.get_balance(user_addr), "ether")
                print(f"  Relay quota 耗尽，fallback 到 Safe.execTransaction 自付 gas (MATIC: {balance_matic:.4f})",
                      file=sys.stderr, flush=True)
                if balance_matic < 0.01:
                    print("  MATIC 余额不足，跳过 gas fallback", file=sys.stderr, flush=True)
                    return results
                # 找出所有没有成功 redeem 的 position（relay 只处理了第一个就 break）
                succeeded_cids = {r["conditionId"] for r in results if r.get("status") == "ok"}
                for p in redeemable:
                    cid = p.get("conditionId", "")
                    if any(cid.startswith(s) for s in succeeded_cids):
                        continue  # relay 已成功，跳过
                    try:
                        adapter_addr, calldata = _build_redeem_calldata(
                            w3,
                            cid,
                            negative_risk=_is_negative_risk_position(p),
                        )
                        tx_hash = _safe_exec_transaction(w3, account, proxy_wallet, adapter_addr, calldata)
                        print(f"  Redeemed (gas fallback) {cid[:20]}... {tx_hash[:16]}", file=sys.stderr, flush=True)
                        results.append({"conditionId": cid[:20], "status": "ok", "tx": tx_hash})
                    except Exception as e:
                        print(f"  Gas fallback failed {cid[:20]}... {e}", file=sys.stderr, flush=True)
                        results.append({"conditionId": cid[:20], "status": "error", "error": str(e)[:100]})
            return results

        redeem_fn = _redeem_with_fallback
        merge_fn = lambda cid, amt, negative_risk=False: _eoa_official_merge(
            w3,
            relay_client,
            cid,
            amt,
            negative_risk=negative_risk,
        )
        merge_many_fn = lambda pairs: _eoa_official_merge_many(w3, relay_client, pairs)
    else:
        redeem_fn = lambda: _eoa_direct_redeem_all(w3, account, redeemable)
        merge_fn = lambda cid, amt, negative_risk=False: _eoa_direct_merge(
            w3,
            account,
            cid,
            amt,
            negative_risk=negative_risk,
        )
        merge_many_fn = None

    return _execute_redeem_merge(
        query_addr,
        redeemable,
        args,
        redeem_fn=redeem_fn,
        merge_many_fn=merge_many_fn,
        merge_fn=merge_fn,
    )


# --- EOA 官方 RelayClient gasless 操作 ---

def _build_relay_client(private_key: str, clob_key: str, clob_secret: str, clob_pass: str):
    """构建官方 RelayClient 实例."""
    from py_builder_relayer_client.client import RelayClient
    from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds
    from py_builder_signing_sdk.config import BuilderConfig

    builder_config = BuilderConfig(
        local_builder_creds=BuilderApiKeyCreds(
            key=clob_key, secret=clob_secret, passphrase=clob_pass,
        )
    )
    relay_client = RelayClient(
        "https://relayer-v2.polymarket.com", 137, private_key, builder_config,
    )
    # 加速轮询
    _original_poll = relay_client.poll_until_state
    def _fast_poll(transaction_id, states, fail_state, max_polls=None, poll_frequency=None):
        return _original_poll(transaction_id=transaction_id, states=states,
                              fail_state=fail_state, max_polls=3, poll_frequency=poll_frequency)
    relay_client.poll_until_state = _fast_poll
    return relay_client


def _safe_tx_result_id(resp) -> str:
    return resp.transaction_id if hasattr(resp, "transaction_id") else str(resp)


def _submit_safe_txs(relay_client, safe_txs: list, *, metadata: str, timeout_s: Optional[int] = None):
    return _run_with_timeout(
        lambda: relay_client.execute(safe_txs, metadata=metadata),
        timeout_s or RELAY_EXECUTE_TIMEOUT_S,
    )


def _eoa_official_redeem_all(w3, relay_client, redeemable) -> list:
    """通过官方 RelayClient 执行 Safe 交易赎回."""
    from web3 import Web3
    from py_builder_relayer_client.models import SafeTransaction, OperationType

    built = []
    for p in redeemable:
        cid = p.get("conditionId")
        if not cid:
            continue
        try:
            adapter_addr, calldata = _build_redeem_calldata(
                w3,
                cid,
                negative_risk=_is_negative_risk_position(p),
            )

            safe_tx = SafeTransaction(
                to=Web3.to_checksum_address(adapter_addr),
                operation=OperationType.Call,
                data=calldata,
                value="0",
            )
            built.append((p, safe_tx))
        except Exception as e:
            err_str = str(e)
            built.append((p, err_str))

    results = [
        {"conditionId": str(p.get("conditionId") or "")[:20], "status": "error", "error": item[:100]}
        for p, item in built
        if isinstance(item, str)
    ]
    pending = [(p, item) for p, item in built if not isinstance(item, str)]
    if not pending:
        return results

    try:
        resp = _submit_safe_txs(
            relay_client,
            [safe_tx for _p, safe_tx in pending],
            metadata="redeem",
            timeout_s=RELAY_EXECUTE_TIMEOUT_S,
        )
        tx_id = _safe_tx_result_id(resp)
        for p, _safe_tx in pending:
            cid = str(p.get("conditionId") or "")
            results.append({"conditionId": cid[:20], "status": "ok", "tx": tx_id})
        print(f"  Redeemed batch (official) count={len(pending)} ok", file=sys.stderr, flush=True)
        return results
    except concurrent.futures.TimeoutError:
        for p, _safe_tx in pending:
            cid = str(p.get("conditionId") or "")
            results.append({"conditionId": cid[:20], "status": "error", "error": "relay timeout"})
        print(f"  Redeem batch timeout count={len(pending)} relay hung >{RELAY_EXECUTE_TIMEOUT_S}s", file=sys.stderr, flush=True)
        return results
    except Exception as batch_exc:
        print(f"  Redeem batch failed, fallback to singles: {batch_exc}", file=sys.stderr, flush=True)

    for p, safe_tx in pending:
        cid = str(p.get("conditionId") or "")
        try:
            resp = _submit_safe_txs(relay_client, [safe_tx], metadata="redeem")
            tx_id = _safe_tx_result_id(resp)
            results.append({"conditionId": cid[:20], "status": "ok", "tx": tx_id})
            print(f"  Redeemed (official) {cid[:20]}... ok", file=sys.stderr, flush=True)
        except concurrent.futures.TimeoutError:
            results.append({"conditionId": cid[:20], "status": "error", "error": "relay timeout"})
            print(f"  Redeem timeout {cid[:20]}... relay hung >{RELAY_EXECUTE_TIMEOUT_S}s", file=sys.stderr, flush=True)
            print("  Relay timeout, aborting remaining redeems", file=sys.stderr, flush=True)
            break
        except Exception as e:
            err_str = str(e)
            results.append({"conditionId": cid[:20], "status": "error", "error": err_str[:100]})
            print(f"  Redeem failed {cid[:20]}... {e}", file=sys.stderr, flush=True)
            if "quota exceeded" in err_str.lower():
                print("  Relay quota exhausted, aborting remaining redeems", file=sys.stderr, flush=True)
                break
    return results


def _build_merge_safe_transaction(w3, condition_id: str, amount: float, *, negative_risk: bool = False):
    """通过官方 RelayClient 执行 Safe 交易 merge."""
    from web3 import Web3
    from py_builder_relayer_client.models import SafeTransaction, OperationType

    adapter_addr, calldata = _build_merge_calldata(
        w3,
        condition_id,
        amount,
        negative_risk=negative_risk,
    )

    safe_tx = SafeTransaction(
        to=Web3.to_checksum_address(adapter_addr),
        operation=OperationType.Call,
        data=calldata,
        value="0",
    )
    return safe_tx


def _eoa_official_merge(w3, relay_client, condition_id: str, amount: float, *, negative_risk: bool = False):
    safe_tx = _build_merge_safe_transaction(
        w3,
        condition_id,
        amount,
        negative_risk=negative_risk,
    )
    _submit_safe_txs(relay_client, [safe_tx], metadata="merge")


def _eoa_official_merge_many(w3, relay_client, pairs: list) -> list:
    if not pairs:
        return []
    built = []
    errors = []
    for pair in pairs:
        try:
            safe_tx = _build_merge_safe_transaction(
                w3,
                pair["conditionId"],
                pair["amount"],
                negative_risk=bool(pair.get("negativeRisk")),
            )
            built.append((pair, safe_tx))
        except Exception as e:
            errors.append({"conditionId": str(pair.get("conditionId") or "")[:20], "error": str(e)[:100]})
    if not built:
        return [{"pair": None, "ok": False, "error": err["error"], "conditionId": err["conditionId"]} for err in errors]

    results = []
    try:
        _submit_safe_txs(
            relay_client,
            [safe_tx for _pair, safe_tx in built],
            metadata="merge",
            timeout_s=RELAY_EXECUTE_TIMEOUT_S,
        )
        for pair, _safe_tx in built:
            results.append({"pair": pair, "ok": True})
        if built:
            print(f"  Merged batch (official) count={len(built)} ok", file=sys.stderr, flush=True)
    except concurrent.futures.TimeoutError:
        for pair, _safe_tx in built:
            results.append({"pair": pair, "ok": False, "error": "relay timeout"})
        print(f"  Merge batch timeout count={len(built)} relay hung >{RELAY_EXECUTE_TIMEOUT_S}s", file=sys.stderr, flush=True)
    except Exception as batch_exc:
        print(f"  Merge batch failed, fallback to singles: {batch_exc}", file=sys.stderr, flush=True)
        for pair, safe_tx in built:
            try:
                _submit_safe_txs(relay_client, [safe_tx], metadata="merge")
                results.append({"pair": pair, "ok": True})
            except Exception as e:
                results.append({"pair": pair, "ok": False, "error": str(e)[:100]})
    for err in errors:
        results.append({"pair": None, "ok": False, "error": err["error"], "conditionId": err["conditionId"]})
    return results


# --- EOA Relayer API gasless 操作 (旧版, 保留作 fallback) ---

RELAYER_URL = "https://relayer-v2.polymarket.com"

# Gnosis Safe nonce ABI
SAFE_NONCE_ABI = json.loads('[{"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"}]')

# Gnosis Safe execTransaction ABI (自付 gas 直接上链)
SAFE_EXEC_ABI = json.loads('[{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"stateMutability":"payable","type":"function"}]')

# EIP-712 type hashes for Gnosis Safe
_SAFE_TX_TYPEHASH = bytes.fromhex(
    # keccak256("SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)")
    "bb8310d486368db6bd6f849402fdd73ad53d316b5a4b2644ad6efe0f941286d8"
)
_DOMAIN_SEPARATOR_TYPEHASH = bytes.fromhex(
    # keccak256("EIP712Domain(uint256 chainId,address verifyingContract)")
    "47e79534a245952e8b16893a336b85a3d9ea9fa8c573f3d803afb92a79469218"
)
_ZERO_ADDR = "0x" + "00" * 20


def _get_safe_nonce(w3, safe_address: str) -> int:
    """从 Gnosis Safe 合约读取当前 nonce."""
    from web3 import Web3
    safe = w3.eth.contract(
        address=Web3.to_checksum_address(safe_address), abi=SAFE_NONCE_ABI)
    return safe.functions.nonce().call()


def _compute_safe_tx_hash(w3, safe_address: str, to: str, data: bytes, nonce: int) -> bytes:
    """计算 Gnosis Safe EIP-712 交易哈希."""
    from web3 import Web3

    data_hash = Web3.keccak(data)

    # safeTxHash = keccak256(abi.encode(SAFE_TX_TYPEHASH, to, 0, keccak256(data), 0, 0, 0, 0, addr(0), addr(0), nonce))
    encoded_tx = (
        _SAFE_TX_TYPEHASH
        + bytes(12) + bytes.fromhex(to[2:])                    # to (padded to 32)
        + (0).to_bytes(32, "big")                               # value = 0
        + data_hash                                             # keccak256(data)
        + (0).to_bytes(32, "big")                               # operation = 0 (CALL)
        + (0).to_bytes(32, "big")                               # safeTxGas = 0
        + (0).to_bytes(32, "big")                               # baseGas = 0
        + (0).to_bytes(32, "big")                               # gasPrice = 0
        + bytes(12) + bytes(20)                                 # gasToken = address(0)
        + bytes(12) + bytes(20)                                 # refundReceiver = address(0)
        + nonce.to_bytes(32, "big")                             # nonce
    )
    safe_tx_hash = Web3.keccak(encoded_tx)

    # domainSeparator = keccak256(abi.encode(DOMAIN_SEPARATOR_TYPEHASH, chainId, safeAddress))
    encoded_domain = (
        _DOMAIN_SEPARATOR_TYPEHASH
        + (137).to_bytes(32, "big")                             # chainId = 137 (Polygon)
        + bytes(12) + bytes.fromhex(safe_address[2:])           # verifyingContract
    )
    domain_separator = Web3.keccak(encoded_domain)

    # messageHash = keccak256(0x19 || 0x01 || domainSeparator || safeTxHash)
    message = b"\x19\x01" + domain_separator + safe_tx_hash
    return Web3.keccak(message)


def _submit_to_relayer(w3, account, safe_address: str, to_addr: str,
                       calldata_hex: str, relayer_api_key: str) -> str:
    """按官方 API spec 构建 Safe 交易并通过 Relayer API 提交."""
    import requests as _req
    from web3 import Web3

    safe_address = Web3.to_checksum_address(safe_address)
    nonce = _get_safe_nonce(w3, safe_address)

    calldata_bytes = bytes.fromhex(calldata_hex.replace("0x", ""))
    msg_hash = _compute_safe_tx_hash(w3, safe_address, to_addr, calldata_bytes, nonce)

    # 签名 (eth_sign 风格: v + r + s)
    signed = account.unsafe_sign_hash(msg_hash)
    # Safe 签名格式: r(32) + s(32) + v(1)
    sig_hex = "0x" + signed.r.to_bytes(32, "big").hex() + signed.s.to_bytes(32, "big").hex() + hex(signed.v)[2:]

    payload = {
        "from": account.address,
        "to": Web3.to_checksum_address(to_addr),
        "proxyWallet": safe_address,
        "data": calldata_hex if calldata_hex.startswith("0x") else "0x" + calldata_hex,
        "nonce": str(nonce),
        "signature": sig_hex,
        "signatureParams": {
            "gasPrice": "0",
            "operation": "0",
            "safeTxnGas": "0",
            "baseGas": "0",
            "gasToken": _ZERO_ADDR,
            "refundReceiver": _ZERO_ADDR,
        },
        "type": "SAFE",
    }
    headers = {
        "Content-Type": "application/json",
        "RELAYER_API_KEY": relayer_api_key,
        "RELAYER_API_KEY_ADDRESS": account.address,
    }

    resp = _req.post(f"{RELAYER_URL}/submit", json=payload, headers=headers, timeout=30)
    if resp.status_code != 200:
        err = resp.text[:200]
        raise RuntimeError(f"Relayer API {resp.status_code}: {err}")
    result = resp.json()
    return result.get("transactionID") or result.get("transactionHash") or ""


def _safe_exec_transaction(w3, account, safe_address: str, to_addr: str,
                           calldata_hex: str) -> str:
    """直接调用 Safe.execTransaction 自付 MATIC gas（Relay quota 耗尽时的 fallback）."""
    from web3 import Web3

    safe_address = Web3.to_checksum_address(safe_address)
    nonce = _get_safe_nonce(w3, safe_address)

    calldata_bytes = bytes.fromhex(calldata_hex.replace("0x", ""))
    msg_hash = _compute_safe_tx_hash(w3, safe_address, to_addr, calldata_bytes, nonce)

    # _compute_safe_tx_hash 已包含 \x19\x01 前缀，直接用 unsafe_sign_hash，v 保持 27/28
    signed = account.unsafe_sign_hash(msg_hash)
    sig_bytes = signed.r.to_bytes(32, "big") + signed.s.to_bytes(32, "big") + bytes([signed.v])

    safe = w3.eth.contract(address=safe_address, abi=SAFE_EXEC_ABI)
    tx = safe.functions.execTransaction(
        Web3.to_checksum_address(to_addr),  # to
        0,                                   # value
        calldata_bytes,                      # data
        0,                                   # operation (CALL)
        0,                                   # safeTxGas
        0,                                   # baseGas
        0,                                   # gasPrice
        Web3.to_checksum_address(_ZERO_ADDR),  # gasToken
        Web3.to_checksum_address(_ZERO_ADDR),  # refundReceiver
        sig_bytes,                           # signatures
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 400000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    })
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"execTransaction reverted, tx={tx_hash.hex()}")
    return tx_hash.hex()


def _build_calldata(ctf_fn_call, from_addr: str) -> str:
    """从合约函数调用构建 calldata hex."""
    tx = ctf_fn_call.build_transaction({"from": from_addr, "gas": 0, "gasPrice": 0})
    return tx["data"]


def _eoa_relayer_redeem_all(w3, account, redeemable, relayer_api_key: str, proxy_wallet: str) -> list:
    results = []
    for p in redeemable:
        cid = p.get("conditionId")
        if not cid:
            continue
        try:
            adapter_addr, calldata = _build_redeem_calldata(
                w3,
                cid,
                negative_risk=_is_negative_risk_position(p),
            )
            tx_id = _submit_to_relayer(w3, account, proxy_wallet, adapter_addr, calldata, relayer_api_key)
            results.append({"conditionId": cid[:20], "status": "ok", "tx": tx_id})
            print(f"  Redeemed (relayer) {cid[:20]}... ok", file=sys.stderr, flush=True)
        except Exception as e:
            results.append({"conditionId": cid[:20], "status": "error", "error": str(e)[:100]})
            print(f"  Redeem failed {cid[:20]}... {e}", file=sys.stderr, flush=True)
    return results


def _eoa_relayer_merge(
    w3,
    account,
    condition_id: str,
    amount: float,
    relayer_api_key: str,
    proxy_wallet: str,
    *,
    negative_risk: bool = False,
):
    adapter_addr, calldata = _build_merge_calldata(
        w3,
        condition_id,
        amount,
        negative_risk=negative_risk,
    )
    _submit_to_relayer(w3, account, proxy_wallet, adapter_addr, calldata, relayer_api_key)


# --- EOA 直接链上调用 (fallback, 需要 MATIC) ---

def _eoa_direct_redeem_all(w3, account, redeemable) -> list:
    results = []
    for p in redeemable:
        cid = p.get("conditionId")
        if not cid:
            continue
        try:
            adapter_addr, calldata = _build_redeem_calldata(
                w3,
                cid,
                negative_risk=_is_negative_risk_position(p),
            )
            tx = {
                "to": adapter_addr,
                "from": account.address,
                "data": calldata,
                "value": 0,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 300000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            }
            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            status = "ok" if receipt["status"] == 1 else "reverted"
            results.append({"conditionId": cid[:20], "status": status, "tx": tx_hash.hex()})
            print(f"  Redeemed (direct) {cid[:20]}... {status}", file=sys.stderr, flush=True)
        except Exception as e:
            results.append({"conditionId": cid[:20], "status": "error", "error": str(e)[:100]})
            print(f"  Redeem failed {cid[:20]}... {e}", file=sys.stderr, flush=True)
    return results


def _eoa_direct_merge(w3, account, condition_id: str, amount: float, *, negative_risk: bool = False):
    adapter_addr, calldata = _build_merge_calldata(
        w3,
        condition_id,
        amount,
        negative_risk=negative_risk,
    )
    tx = {
        "to": adapter_addr,
        "data": calldata,
        "value": 0,
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": 300000,
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    }
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"merge tx reverted: {tx_hash.hex()}")


def _send_direct_tx(w3, account, to_addr: str, calldata_hex: str, *, gas: int = 250000) -> str:
    from web3 import Web3

    tx = {
        "to": Web3.to_checksum_address(to_addr),
        "from": account.address,
        "data": calldata_hex,
        "value": 0,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": int(gas),
        "maxFeePerGas": w3.eth.gas_price * 2,
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    }
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt["status"] != 1:
        raise RuntimeError(f"tx reverted: {tx_hash.hex()}")
    return tx_hash.hex()


def _run_pusd_operation(private_key: str, _env, args) -> int:
    from eth_account import Account
    from web3 import Web3

    wrap_amount = getattr(args, "wrap_usdce", None)
    unwrap_amount = getattr(args, "unwrap_pusd", None)
    if wrap_amount is not None and unwrap_amount is not None:
        print(json.dumps({"ok": False, "error": "--wrap-usdce and --unwrap-pusd are mutually exclusive"}))
        return 1

    amount = float(wrap_amount if wrap_amount is not None else unwrap_amount)
    if amount <= 0:
        print(json.dumps({"ok": False, "error": "amount must be > 0"}))
        return 1

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    account = Account.from_key(private_key)
    owner = _env("FUNDER_ADDRESS") or account.address
    amount_raw = _amount_to_base_units(amount)
    is_wrap = wrap_amount is not None

    if is_wrap:
        approval_token = USDC_ADDRESS
        approval_spender = COLLATERAL_ONRAMP_ADDRESS
        approval_data = _build_approve_calldata(w3, approval_spender, amount_raw)
        action_to = COLLATERAL_ONRAMP_ADDRESS
        action_data = _build_wrap_calldata(w3, owner, amount_raw)
        action = "wrap_usdce"
    else:
        approval_token = PUSD_ADDRESS
        approval_spender = COLLATERAL_OFFRAMP_ADDRESS
        approval_data = _build_pusd_approve_calldata(w3, approval_spender, amount_raw)
        action_to = COLLATERAL_OFFRAMP_ADDRESS
        action_data = _build_unwrap_calldata(w3, owner, amount_raw)
        action = "unwrap_pusd"

    preview = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "action": action,
        "owner": owner,
        "amount": amount,
        "amount_raw": amount_raw,
        "approval": {
            "token": approval_token,
            "spender": approval_spender,
            "data": approval_data,
        },
        "transaction": {
            "to": action_to,
            "data": action_data,
        },
    }
    if args.dry_run:
        print(json.dumps(preview))
        return 0

    if owner.lower() != account.address.lower():
        approve_tx = _safe_exec_transaction(w3, account, owner, approval_token, approval_data)
        action_tx = _safe_exec_transaction(w3, account, owner, action_to, action_data)
    else:
        approve_tx = _send_direct_tx(w3, account, approval_token, approval_data)
        action_tx = _send_direct_tx(w3, account, action_to, action_data, gas=300000)

    preview["approval_tx"] = approve_tx
    preview["tx"] = action_tx
    print(json.dumps(preview))
    return 0


def _fetch_positions_api(user_address: str) -> list:
    """通过 Polymarket Positions API 获取仓位（含 redeemable 标记）."""
    import requests as _req
    url = "https://data-api.polymarket.com/positions"
    params = {"user": user_address, "sizeThreshold": "0.01", "limit": 200}
    try:
        resp = _req.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Fetch positions failed: {e}", file=sys.stderr, flush=True)
        return []


def _maintenance_timeout_budget_s(*, redeemable_count: int, merge_count: int) -> int:
    ops = max(1, int(redeemable_count or 0) + int(merge_count or 0))
    return RELAY_EXECUTE_TIMEOUT_S * ops


def _execute_redeem_merge(user_addr, redeemable, args, *, redeem_fn, merge_fn, merge_many_fn=None) -> int:
    """通用的 redeem + merge 执行逻辑，proxy 和 eoa 共用."""
    print(f"User: {user_addr}", file=sys.stderr, flush=True)
    print(f"Redeemable: {len(redeemable)}", file=sys.stderr, flush=True)

    for p in redeemable:
        print(f"  {p.get('outcome')} size={p.get('size')}", file=sys.stderr, flush=True)

    # Merge 阶段
    all_positions = _fetch_all_positions(user_addr)
    merge_pairs = _find_mergeable_pairs(all_positions)
    print(f"Mergeable pairs: {len(merge_pairs)}", file=sys.stderr, flush=True)
    for pair in merge_pairs:
        neg_label = " neg-risk" if pair.get("negativeRisk") else ""
        print(f"  merge {pair['amount']:.2f}{neg_label} | {pair['title'][:50]}", file=sys.stderr, flush=True)

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True,
                          "redeemable": len(redeemable),
                          "items": [{"outcome": p.get("outcome"), "size": p.get("size"),
                                     "conditionId": p.get("conditionId")} for p in redeemable],
                          "mergeable": len(merge_pairs),
                          "merge_items": [{"conditionId": p["conditionId"][:20], "amount": p["amount"],
                                           "title": p["title"][:40],
                                           "negativeRisk": bool(p.get("negativeRisk"))} for p in merge_pairs]}))
        return 0

    total_mergeable = len(merge_pairs)
    if total_mergeable > MAX_MERGE_PER_RUN:
        merge_pairs = merge_pairs[:MAX_MERGE_PER_RUN]
        print(f"Capped mergeable to {MAX_MERGE_PER_RUN} (from {total_mergeable})", file=sys.stderr, flush=True)
    budget_s = _maintenance_timeout_budget_s(
        redeemable_count=len(redeemable),
        merge_count=len(merge_pairs),
    )

    # 先执行 Merge（数量少、释放资金快）
    merged_count = 0
    merge_errors = []
    if merge_many_fn is not None and merge_pairs:
        try:
            merge_results = _run_with_timeout(lambda: merge_many_fn(merge_pairs), budget_s)
        except concurrent.futures.TimeoutError:
            merge_results = [
                {"pair": pair, "ok": False, "error": "relay timeout"}
                for pair in merge_pairs
            ]
        except Exception as e:
            merge_results = [
                {"pair": None, "ok": False, "conditionId": "", "error": str(e)[:100]}
            ]
        for result in merge_results:
            pair = result.get("pair") if isinstance(result, dict) else None
            if result.get("ok"):
                merged_count += 1
            else:
                cid = str((pair or {}).get("conditionId") or result.get("conditionId") or "")[:20]
                merge_errors.append({"conditionId": cid, "error": str(result.get("error") or "")[:100]})
    else:
        for pair in merge_pairs:
            try:
                _run_with_timeout(
                    lambda p=pair: merge_fn(
                        p["conditionId"],
                        p["amount"],
                        bool(p.get("negativeRisk")),
                    ),
                    RELAY_EXECUTE_TIMEOUT_S,
                )
                merged_count += 1
                print(f"  Merged {pair['amount']:.2f} | {pair['title'][:40]}", file=sys.stderr, flush=True)
            except Exception as e:
                merge_errors.append({"conditionId": pair["conditionId"][:20], "error": str(e)[:100]})
                print(f"  Merge failed: {pair['conditionId'][:20]}... {e}", file=sys.stderr, flush=True)

    # 再执行 Redeem（已在调用方按 size 降序截断到 MAX_REDEEM_PER_RUN）
    redeem_results = []
    if redeemable:
        try:
            redeem_results = _run_with_timeout(redeem_fn, budget_s)
        except concurrent.futures.TimeoutError:
            print(f"  redeem_fn timeout", file=sys.stderr, flush=True)
            redeem_results = [{"status": "error", "error": "relay timeout"}]
        except Exception as e:
            print(f"  redeem_fn error: {e}", file=sys.stderr, flush=True)
            redeem_results = []

    print(json.dumps({
        "ok": True,
        "redeemed_batches": len(redeem_results) if isinstance(redeem_results, list) else redeem_results,
        "redeemable": len(redeemable),
        "mergeable": total_mergeable,
        "merge_attempted": len(merge_pairs),
        "timeout_budget_s": budget_s,
        "results": redeem_results,
        "merged": merged_count,
        "merge_errors": merge_errors,
    }, default=str))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", type=str, default="", help="账号 env 后缀，如 MAIN → PRIVATE_KEY_MAIN")
    ap.add_argument("--wallet-type", type=str, default="proxy", choices=["proxy", "eoa"],
                     help="钱包类型: proxy (Google登录) 或 eoa (自托管钱包)")
    pusd_group = ap.add_mutually_exclusive_group()
    pusd_group.add_argument("--wrap-usdce", type=float, default=None,
                            help="explicitly wrap USDC.e into pUSD")
    pusd_group.add_argument("--unwrap-pusd", type=float, default=None,
                            help="explicitly unwrap pUSD into USDC.e")
    ap.add_argument("--dry-run", action="store_true")
    args, _unknown = ap.parse_known_args()

    _load_dotenv()
    _refresh_runtime_limits_from_env()

    suffix = args.account.strip().upper() if args.account else ""

    def _env(base_key: str) -> str:
        """按后缀读取环境变量，fallback 到无后缀版本."""
        if suffix:
            val = os.environ.get(f"{base_key}_{suffix}", "").strip()
            if val:
                return val
        return os.environ.get(base_key, "").strip()

    private_key = _env("PRIVATE_KEY")
    if not private_key:
        print(json.dumps({"ok": False, "error": "PRIVATE_KEY required"}))
        return 1

    if args.wrap_usdce is not None or args.unwrap_pusd is not None:
        return _run_pusd_operation(private_key, _env, args)

    if args.wallet_type == "eoa":
        return _run_eoa(private_key, _env, args)
    else:
        return _run_proxy(private_key, _env, args)


if __name__ == "__main__":
    raise SystemExit(main())
