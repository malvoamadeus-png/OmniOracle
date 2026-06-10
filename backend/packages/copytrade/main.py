"""CLI 入口 — 组装所有模块，主循环."""

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# 确保项目根目录在 path 中
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGES_DIR = os.path.dirname(_SCRIPT_DIR)
if _PACKAGES_DIR not in sys.path:
    sys.path.insert(0, _PACKAGES_DIR)

from dotenv import load_dotenv

from copytrade.aggregation import prepare_copy_signals_live
from copytrade.attribution import attribute_profits
from copytrade.account_config import ACCOUNTS_DIR, AccountInfo, has_accounts, load_all_accounts, load_single_account
from copytrade.config import CopyTradeConfig, interactive_setup, load_config, save_config, validate_copytrade_config
from copytrade.db import CopyTradeDB
from copytrade.domain import classify_order_fill_status
from copytrade.executor import DryRunExecutor, OrderExecutor
from copytrade.exit_manager import ExitManager
from copytrade.monitor import LeaderTrade, TradeMonitor
from copytrade.risk import RiskManager
from copytrade.signal_hub import LeaderSignalHub
from copytrade.worker import AccountWorker
from copytrade.paths import (
    DEFAULT_CONFIG_PATH as PATH_DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH as PATH_DEFAULT_DB_PATH,
    DOTENV_PATH,
    PACKAGE_DIR,
    PROJECT_ROOT as PATH_PROJECT_ROOT,
    ensure_import_paths,
)

import requests

ensure_import_paths()

LEADER_ATTR_INTERVAL_S = 3 * 60 * 60  # 3 hours
COMPARE_INTERVAL_S = 3 * 60 * 60  # 3 hours
REDEEM_INTERVAL_S = 4 * 60 * 60  # 4 小时
HOURLY_REPORT_S = 3600
SIGNAL_HUB_STATUS_INTERVAL_S = 30.0
BACKGROUND_TASK_SLOW_LOG_S = 10 * 60
SCRIPT_DIR = str(PACKAGE_DIR)
PROJECT_ROOT = str(PATH_PROJECT_ROOT)
DEFAULT_CONFIG_PATH = str(PATH_DEFAULT_CONFIG_PATH)
DEFAULT_DB_PATH = str(PATH_DEFAULT_DB_PATH)

# 小时汇总计数器
_hourly_stats: Dict[str, Any] = {
    "buy_ok": 0,
    "buy_fail": 0,
    "exit_count": 0,
    "maker_like_count": 0,
    "last_report_ts": 0.0,
}


def _signal_hub_status_logging_enabled() -> bool:
    raw = str(os.getenv("COPYTRADE_SIGNAL_HUB_STATUS_LOG", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _background_task_logging_enabled() -> bool:
    raw = str(os.getenv("COPYTRADE_BACKGROUND_TASK_LOG", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Polymarket 跟单系统")
    ap.add_argument("--db", type=str, default=DEFAULT_DB_PATH, help="数据库路径")
    ap.add_argument("--setup", action="store_true", help="强制重新配置")
    ap.add_argument("--dry-run", action="store_true", help="模拟运行，不实际下单")
    ap.add_argument("--once", action="store_true", help="只轮询一次后退出")
    ap.add_argument("--status", action="store_true", help="显示当前持仓和今日统计")
    ap.add_argument("--account", type=str, default=None, help="只运行指定账号（多账号模式）")
    ap.add_argument("--attribute", type=str, default=None, metavar="CONDITION_ID",
                     help="对指定市场进行利润归因")
    return ap.parse_args()


def show_status(db: CopyTradeDB) -> None:
    s = db.get_status_summary()
    sys.stdout.write("\n=== 跟单系统状态 ===\n")
    sys.stdout.write(f"  持仓数量:   {s['open_positions']}\n")
    sys.stdout.write(f"  持仓金额:   ${s['open_usd']:.2f}\n")
    sys.stdout.write(f"  今日消费:   ${s['today_spend']:.2f}\n")
    sys.stdout.write(f"  累计利润:   ${s['total_profit']:.2f}\n")
    sys.stdout.write(f"  已成交:     {s['total_filled']}\n")
    sys.stdout.write(f"  已跳过:     {s['total_skipped']}\n")
    sys.stdout.write("\n")

    # 显示 open 仓位明细
    open_trades = db.get_all_open_trades()
    if open_trades:
        sys.stdout.write("  --- 持仓明细 ---\n")
        for t in open_trades:
            sys.stdout.write(
                f"  #{t['id']} {t.get('our_side','?')} {t.get('our_size',0):.2f} "
                f"@ ${t.get('our_price',0):.4f} = ${t.get('our_usd',0):.2f} "
                f"| {t.get('market_slug') or t.get('condition_id','?')[:20]} "
                f"| leader={t.get('leader_address','?')[:10]}...\n"
            )
    sys.stdout.write("\n")


def _disable_quick_edit():
    """Windows: 禁用 CMD 快速编辑模式，防止点击窗口冻结进程."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value &= ~0x0040  # 清除 ENABLE_QUICK_EDIT_MODE
        mode.value |= 0x0080   # 保留 ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


def _base_trade_record(lt: LeaderTrade, account_name: str = "default") -> Dict[str, Any]:
    return {
        "account_name": account_name,
        "leader_address": lt.leader_address,
        "leader_tx_hash": lt.tx_hash,
        "leader_fill_key": getattr(lt, "fill_key", None),
        "leader_side": lt.side,
        "leader_price": lt.price,
        "leader_size": lt.size,
        "leader_usd": lt.usd_amount,
        "token_id": lt.token_id,
        "condition_id": lt.condition_id,
        "market_slug": lt.market_slug,
        "outcome": lt.outcome,
    }


def _persist_trade_record(db: CopyTradeDB, lt: LeaderTrade, status: str, **fields: Any) -> int:
    payload = _base_trade_record(lt)
    payload.update(fields)
    payload["status"] = status

    trade_id = getattr(lt, "trade_id", None)
    if trade_id:
        payload.pop("account_name", None)
        payload.pop("status", None)
        db.update_trade_status(int(trade_id), status, **payload)
        return int(trade_id)
    return db.insert_trade(payload)


def _is_immediate_fill(result) -> bool:
    status = str(getattr(result, "exchange_status", "") or "").lower()
    if status == "matched":
        return True
    try:
        return float(getattr(result, "filled_size", 0) or 0) > 0
    except Exception:
        return False


def main() -> int:
    # 固定在 copytrade 目录运行，避免相对路径读到根目录同名配置
    original_cwd = os.getcwd()
    os.chdir(SCRIPT_DIR)
    _disable_quick_edit()
    args = parse_args()
    if getattr(args, "db", None) and not os.path.isabs(args.db):
        args.db = os.path.abspath(os.path.join(original_cwd, args.db))

    # 加载 .env
    load_dotenv(dotenv_path=DOTENV_PATH, override=False)
    load_dotenv(override=False)

    # --- status 模式 ---
    if args.status:
        db = CopyTradeDB(args.db)
        try:
            show_status(db)
        finally:
            db.close()
        return 0

    # --- attribution 模式 ---
    if args.attribute:
        db = CopyTradeDB(args.db)
        try:
            results = attribute_profits(db, args.attribute)
            sys.stdout.write(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        finally:
            db.close()
        return 0

    # --- 多账号模式：accounts/ 目录存在且有 .toml 文件 ---
    if has_accounts():
        return _run_multi_account(args)

    # --- 单账号回退模式（向后兼容） ---
    return _run_single_account(args)


def _is_stream_signal_source(signal_source: str) -> bool:
    return str(signal_source or "").strip().lower() in {"stream", "stream_hybrid"}


def _ensure_wss_for_accounts(accounts: List[AccountInfo]) -> bool:
    if not any(_is_stream_signal_source(acct.config.signal_source) for acct in accounts):
        return True
    if os.getenv("POLYGON_WSS_URL", "").strip():
        return True
    sys.stderr.write(
        "错误: 当前有账号使用 stream/stream_hybrid，但未设置 POLYGON_WSS_URL，启动已中止\n"
    )
    return False


def _build_signal_hub(
    accounts: List[AccountInfo],
    db: CopyTradeDB,
) -> Tuple[Optional[LeaderSignalHub], Dict[str, Any]]:
    if not any(_is_stream_signal_source(acct.config.signal_source) for acct in accounts):
        return None, {}

    wss_url = os.getenv("POLYGON_WSS_URL", "").strip()
    if not wss_url:
        raise RuntimeError("POLYGON_WSS_URL is required for stream signal modes")

    hub = LeaderSignalHub(wss_url, db)
    signal_queues: Dict[str, Any] = {}
    for acct in accounts:
        if not _is_stream_signal_source(acct.config.signal_source):
            continue
        signal_queues[acct.name] = hub.register_account(acct.name, acct.config.leader_addresses)
    sys.stderr.write(
        f"  实时链路: Polygon WSS -> CTF Exchange {hub.provider_host} "
        f"(accounts={len(signal_queues)} leaders={len({addr.lower() for acct in accounts if _is_stream_signal_source(acct.config.signal_source) for addr in acct.config.leader_addresses})})\n"
    )
    hub.start()
    return hub, signal_queues


def _maybe_log_signal_hub_status(
    hub: Optional[LeaderSignalHub],
    last_log_ts: float,
) -> float:
    if hub is None:
        return last_log_ts
    if not _signal_hub_status_logging_enabled():
        return last_log_ts
    now = time.time()
    if last_log_ts and (now - last_log_ts) < SIGNAL_HUB_STATUS_INTERVAL_S:
        return last_log_ts
    sys.stderr.write(f"[signal_hub] status {hub.format_status_line()}\n")
    sys.stderr.flush()
    return now


def _run_multi_account(args: argparse.Namespace) -> int:
    """多账号模式：每个账号一个 worker 线程."""
    try:
        if args.account:
            accounts = [load_single_account(ACCOUNTS_DIR, args.account)]
        else:
            accounts = load_all_accounts()
    except Exception as e:
        sys.stderr.write(f"错误: 配置校验失败: {e}\n")
        return 1

    if not accounts:
        sys.stderr.write("错误: accounts/ 目录下没有找到账号配置\n")
        return 1

    if not _ensure_wss_for_accounts(accounts):
        return 1

    db = CopyTradeDB(args.db)
    workers = []
    hub = None
    signal_queues: Dict[str, Any] = {}
    signal_hub_status_last_log_ts = 0.0

    sys.stderr.write(f"\n=== 跟单系统启动 [多账号] ===\n")
    sys.stderr.write(f"  账号数量: {len(accounts)}\n")
    for acct in accounts:
        sys.stderr.write(f"  - {acct.name}: {len(acct.config.leader_addresses)} leaders, env={acct.env_suffix}\n")
    sys.stderr.write(f"  数据库:   {args.db}\n\n")

    try:
        hub, signal_queues = _build_signal_hub(accounts, db)
        for acct in accounts:
            w = AccountWorker(
                acct,
                db,
                dry_run=args.dry_run,
                once=args.once,
                signal_queue=signal_queues.get(acct.name),
            )
            workers.append(w)
            w.start()

        dry_run = args.dry_run
        compare_account_names = [acct.name for acct in accounts]

        # 主线程负责归因快照（redeem 由各 worker 自行处理）
        while any(w.is_alive() for w in workers):
            _maybe_run_leader_attr_snapshot(args.db, dry_run)
            _maybe_run_compare_snapshot(args.db, dry_run, compare_account_names)
            signal_hub_status_last_log_ts = _maybe_log_signal_hub_status(hub, signal_hub_status_last_log_ts)
            time.sleep(1)
    except KeyboardInterrupt:
        sys.stderr.write("\n跟单系统停止中...\n")
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=5)
    finally:
        if hub is not None:
            hub.stop()
            hub.join(timeout=5)
        db.close()

    return 0


def _run_single_account(args: argparse.Namespace) -> int:
    """单账号回退模式（向后兼容 copytrade_config.json）."""
    cfg = None
    try:
        if not args.setup:
            cfg = load_config(DEFAULT_CONFIG_PATH)
    except Exception as e:
        sys.stderr.write(f"错误: 配置校验失败: {e}\n")
        return 1

    if cfg is None or args.setup:
        cfg = interactive_setup(cfg)
        try:
            validate_copytrade_config(cfg, context=DEFAULT_CONFIG_PATH)
        except Exception as e:
            sys.stderr.write(f"错误: 配置校验失败: {e}\n")
            return 1
        save_config(cfg, DEFAULT_CONFIG_PATH)

    if args.dry_run:
        cfg.dry_run = True

    if not cfg.leader_addresses:
        sys.stderr.write("错误: 未配置跟单地址，请运行 --setup\n")
        return 1

    db = CopyTradeDB(args.db)
    account = AccountInfo(name="default", env_suffix="", config=cfg)
    accounts = [account]
    if not _ensure_wss_for_accounts(accounts):
        db.close()
        return 1

    hub = None
    signal_queue = None
    signal_hub_status_last_log_ts = 0.0

    mode_label = "DRY-RUN" if cfg.dry_run else "LIVE"
    sys.stderr.write(f"\n=== 跟单系统启动 [{mode_label}] ===\n")
    sys.stderr.write(f"  跟单地址: {len(cfg.leader_addresses)} 个\n")
    sys.stderr.write(f"  跟单模式: {cfg.copy_mode}\n")
    sys.stderr.write(f"  离场策略: {cfg.exit_strategy}\n")
    if _is_stream_signal_source(cfg.signal_source):
        sys.stderr.write(f"  实时信号: {cfg.signal_source} (idle=1s reconcile={cfg.signal_reconcile_interval_s}s)\n")
    else:
        sys.stderr.write(f"  轮询间隔: {cfg.poll_interval_s}s\n")
    sys.stderr.write(f"  信号源:   {cfg.signal_source} (workers={cfg.signal_fetch_workers})\n")
    sys.stderr.write(f"  配置文件: {DEFAULT_CONFIG_PATH}\n")
    sys.stderr.write(f"  数据库:   {args.db}\n\n")

    try:
        hub, signal_queues = _build_signal_hub(accounts, db)
        signal_queue = signal_queues.get("default")
        worker = AccountWorker(
            account,
            db,
            dry_run=args.dry_run,
            once=args.once,
            signal_queue=signal_queue,
        )
        worker.start()
        while worker.is_alive():
            _maybe_run_leader_attr_snapshot(args.db, cfg.dry_run)
            signal_hub_status_last_log_ts = _maybe_log_signal_hub_status(hub, signal_hub_status_last_log_ts)
            time.sleep(1)
    except KeyboardInterrupt:
        sys.stderr.write("\n跟单系统停止中...\n")
        try:
            worker.stop()  # type: ignore[name-defined]
            worker.join(timeout=5)  # type: ignore[name-defined]
        except Exception:
            pass
    finally:
        if hub is not None:
            hub.stop()
            hub.join(timeout=5)
        db.close()

    return 0


def _poll_cycle(
    monitor: TradeMonitor,
    risk: RiskManager,
    executor,
    exit_mgr: ExitManager,
    db: CopyTradeDB,
    cfg: CopyTradeConfig,
) -> None:
    new_trades = monitor.poll_once()
    copy_signals = _prepare_copy_signals(new_trades, cfg)

    _maybe_report_hourly()

    for lt in copy_signals:
        # 只跟 BUY 交易
        if lt.side != "BUY":
            _record_reject("not_buy_side")
            continue

        # 计算下单参数
        try:
            params = executor.compute_order_params(lt, db=db)
        except Exception as e:
            _record_reject("compute_params_error")
            _persist_trade_record(db, lt, "failed_internal", skip_reason=f"compute_params_error: {e}")
            continue

        if params is None:
            _record_reject("cannot_compute_params")
            _persist_trade_record(db, lt, "skipped", skip_reason="cannot_compute_params")
            continue

        # 风控检查
        try:
            ok, reason = risk.check_all(lt, params.usd)
        except Exception as e:
            _record_reject("risk_check_error")
            _persist_trade_record(
                db,
                lt,
                "failed_internal",
                skip_reason=f"risk_check_error: {e}",
                requested_price=params.price,
                requested_size=params.size,
                requested_usd=params.usd,
                token_id=params.token_id,
                condition_id=params.condition_id,
                market_slug=params.market_slug,
                outcome=params.outcome,
            )
            continue

        if not ok:
            _record_reject(_classify_risk_reason(reason))
            _persist_trade_record(
                db,
                lt,
                "skipped",
                skip_reason=reason,
                requested_price=params.price,
                requested_size=params.size,
                requested_usd=params.usd,
            )
            continue

        # 执行下单
        result = executor.execute_order(params)
        immediate_fill = _is_immediate_fill(result)
        exchange_status = str(getattr(result, "exchange_status", "") or ("matched" if immediate_fill else "submitted"))
        immediate_filled_size = result.filled_size
        if result.success and immediate_fill and immediate_filled_size is None and exchange_status.lower() == "matched":
            immediate_filled_size = params.size
        trade_status, partial_fill_status = (
            classify_order_fill_status(exchange_status, immediate_filled_size, params.size)
            if result.success and immediate_fill
            else (("submitted", None) if result.success else ("failed", None))
        )
        _persist_trade_record(
            db,
            lt,
            trade_status,
            our_order_id=result.order_id,
            our_side=params.side,
            our_price=(result.filled_price if immediate_fill else None),
            our_size=(result.filled_size if immediate_fill else 0.0),
            our_usd=(result.filled_usd if immediate_fill else 0.0),
            requested_price=params.price,
            requested_size=params.size,
            requested_usd=params.usd,
            exchange_order_status=exchange_status,
            filled_size_actual=(result.filled_size if immediate_fill else None),
            filled_usd_actual=(result.filled_usd if immediate_fill else None),
            partial_fill_status=partial_fill_status,
            our_limit_price=result.limit_price or params.price,
            our_filled_price=(result.filled_price if immediate_fill else None),
            token_id=params.token_id,
            condition_id=params.condition_id,
            market_slug=params.market_slug,
            outcome=params.outcome,
            skip_reason=result.error if not result.success else None,
            is_aggregated_order=1 if getattr(lt, "is_maker_like_aggregated", False) else 0,
            aggregation_source_count=getattr(lt, "aggregation_source_count", None),
        )

        if result.success and immediate_fill:
            db.add_daily_spend(result.filled_usd or 0.0)
            _hourly_stats["buy_ok"] += 1
        elif not result.success:
            _hourly_stats["buy_fail"] += 1
            err_lower = (result.error or "").lower()
            if "not enough balance" in err_lower or "allowance" in err_lower:
                _trigger_emergency_redeem()
        continue

    # 处理离场
    exit_actions = exit_mgr.process_exits(new_trades)
    if exit_actions:
        _hourly_stats["exit_count"] += len(exit_actions)

    if getattr(executor, "_client", None) is not None:
        _verify_recent_orders(db, executor)

def _verify_recent_orders(db: CopyTradeDB, executor) -> None:
    recent = db.get_recent_orders_for_verification(account_name="default", hours=24, limit=30)
    if not recent:
        return

    updated = 0
    for row in recent:
        order_id = row.get("our_order_id")
        if not order_id:
            continue
        try:
            order_info = executor._client.get_order(order_id)
            if not isinstance(order_info, dict):
                continue
            api_status = (order_info.get("status") or "").lower()
            if api_status not in ("live", "matched", "expired", "cancelled"):
                continue

            size_matched = order_info.get("size_matched") or order_info.get("sizeMatched")
            original_size = order_info.get("original_size") or order_info.get("size")
            try:
                matched = float(size_matched) if size_matched is not None else 0.0
            except (ValueError, TypeError):
                matched = 0.0

            price = (
                order_info.get("avg_price")
                or order_info.get("avgPrice")
                or order_info.get("average_price")
                or order_info.get("averagePrice")
            )
            try:
                actual_price = float(price) if price is not None else None
            except (ValueError, TypeError):
                actual_price = None

            recon = db.reconcile_order_state(
                order_id,
                account_name="default",
                exchange_order_status=api_status,
                matched_size=matched,
                fill_price=actual_price,
                skip_reason=f"partial fill: {matched} of {original_size}" if api_status in ("expired", "cancelled") and matched > 0 else None,
            )
            if not recon.get("updated"):
                continue

            usd_delta = float(recon.get("usd_delta") or 0.0)
            if usd_delta > 0:
                db.add_daily_spend(usd_delta)
                _hourly_stats["buy_ok"] += 1

            slug = row.get("market_slug") or order_id[:12]
            partial_state = recon.get("partial_fill_status")
            if api_status in ("expired", "cancelled") and recon.get("status") == "expired":
                _logln(f"[verify] {slug} order actually {api_status}, marked expired")
                updated += 1
            elif partial_state == "partial":
                _logln(f"[verify] {slug} partial fill {matched}/{original_size}")
                updated += 1
            elif api_status == "matched":
                updated += 1
        except Exception:
            pass

    if updated:
        _logln(f"[verify] updated {updated} recent order states")

def _prepare_copy_signals(new_trades: List[LeaderTrade], cfg: CopyTradeConfig) -> List[LeaderTrade]:
    if not hasattr(_prepare_copy_signals, "_maker_like_state"):
        _prepare_copy_signals._maker_like_state = {}  # type: ignore[attr-defined]

    states = _prepare_copy_signals._maker_like_state  # type: ignore[attr-defined]
    out = prepare_copy_signals_live(new_trades, cfg, states)
    if out:
        _hourly_stats["maker_like_count"] += sum(
            1 for trade in out if getattr(trade, "is_maker_like_aggregated", False)
        )
    return out


def _compute_maker_like_score(
    *,
    count: int,
    span_s: int,
    max_piece_usd: float,
    min_trade_size_usd: float,
    window_s: int,
) -> float:
    """maker_like_score_threshold 对应的分值函数（0~1）.

    经验规则：
    - 成交越碎（count 越大）分越高；
    - 单笔越小（max_piece_usd 相对阈值越低）分越高；
    - 时间上越连续（span 相对窗口越短）分越高。
    """
    frag = min(1.0, max(0.0, (count - 1) / 4.0))
    piece_ratio = max_piece_usd / max(min_trade_size_usd, 1e-9)
    small_piece = 1.0 - min(1.0, piece_ratio)
    continuity = 1.0 - min(1.0, span_s / max(window_s, 1))
    score = 0.45 * frag + 0.35 * small_piece + 0.20 * continuity
    return max(0.0, min(1.0, score))


def _classify_risk_reason(reason: str) -> str:
    s = (reason or "").lower()
    if s.startswith("leader_market_once"):
        return "leader_market_once"
    if s.startswith("leader usd"):
        return "min_trade_size"
    if s.startswith("price ") and "< min" in s:
        return "price_below_min"
    if s.startswith("price ") and "> max" in s:
        return "price_above_max"
    if s.startswith("settlement in"):
        return "settlement_too_far"
    if s.startswith("global_once"):
        return "market_global_once"
    if s.startswith("per_address_once"):
        return "market_per_address_once"
    if s.startswith("position "):
        return "position_limit"
    return "risk_other"


def _record_reject(kind: str) -> None:
    if not hasattr(_poll_cycle, "_reject_counts"):
        _poll_cycle._reject_counts = {}  # type: ignore[attr-defined]
    if not hasattr(_poll_cycle, "_reject_window_start"):
        _poll_cycle._reject_window_start = time.time()  # type: ignore[attr-defined]

    counts: Dict[str, int] = _poll_cycle._reject_counts  # type: ignore[attr-defined]
    counts[kind] = counts.get(kind, 0) + 1


def _logln(msg: str) -> None:
    sys.stderr.write(msg.rstrip() + "\n")
    sys.stderr.flush()


def _background_task_is_running(task: Any) -> bool:
    proc = getattr(task, "_proc", None)
    return proc is not None and proc.poll() is None


def _normalize_compare_accounts(account_names: List[str]) -> List[str]:
    seen = set()
    normalized: List[str] = []
    for raw in account_names:
        name = str(raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def _maybe_run_leader_attr_snapshot(copytrade_db_path: str, dry_run: bool) -> None:
    if dry_run:
        return
    if not hasattr(_maybe_run_leader_attr_snapshot, "_last_run_ts"):
        _maybe_run_leader_attr_snapshot._last_run_ts = 0.0  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_leader_attr_snapshot, "_proc"):
        _maybe_run_leader_attr_snapshot._proc = None  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_leader_attr_snapshot, "_started_at_ts"):
        _maybe_run_leader_attr_snapshot._started_at_ts = 0.0  # type: ignore[attr-defined]

    proc = _maybe_run_leader_attr_snapshot._proc  # type: ignore[attr-defined]
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return
        started_at = float(_maybe_run_leader_attr_snapshot._started_at_ts)  # type: ignore[attr-defined]
        elapsed_s = max(0.0, time.time() - started_at) if started_at > 0 else 0.0
        verbose = _background_task_logging_enabled()
        if rc != 0:
            _logln(f"[leader-attribution] task failed: exit={rc} elapsed={elapsed_s:.0f}s")
        elif verbose or elapsed_s >= BACKGROUND_TASK_SLOW_LOG_S:
            _logln(f"[leader-attribution] task completed in {elapsed_s:.0f}s")
        _maybe_run_leader_attr_snapshot._proc = None  # type: ignore[attr-defined]
        _maybe_run_leader_attr_snapshot._started_at_ts = 0.0  # type: ignore[attr-defined]

    now = time.time()
    last = float(_maybe_run_leader_attr_snapshot._last_run_ts)  # type: ignore[attr-defined]
    if now - last < LEADER_ATTR_INTERVAL_S:
        return
    if _background_task_is_running(_maybe_run_compare_snapshot):
        return

    script_path = os.path.join(SCRIPT_DIR, "build_leader_pnl_snapshot.py")
    if not os.path.exists(script_path):
        _maybe_run_leader_attr_snapshot._last_run_ts = now  # type: ignore[attr-defined]
        return

    try:
        p = subprocess.Popen(
            [sys.executable, script_path, "--db", copytrade_db_path],
            stdout=subprocess.DEVNULL,
        )
        _maybe_run_leader_attr_snapshot._proc = p  # type: ignore[attr-defined]
        _maybe_run_leader_attr_snapshot._started_at_ts = now  # type: ignore[attr-defined]
        if _background_task_logging_enabled():
            _logln(f"[leader-attribution] task launched, next run not earlier than {int(LEADER_ATTR_INTERVAL_S)}s")
    except Exception as e:
        _logln(f"[leader-attribution] launch failed: {e}")
    finally:
        _maybe_run_leader_attr_snapshot._last_run_ts = now  # type: ignore[attr-defined]


def _maybe_run_compare_snapshot(copytrade_db_path: str, dry_run: bool, account_names: List[str]) -> None:
    if dry_run:
        return
    normalized_accounts = _normalize_compare_accounts(account_names)
    if not normalized_accounts:
        return
    if not hasattr(_maybe_run_compare_snapshot, "_last_run_ts"):
        _maybe_run_compare_snapshot._last_run_ts = 0.0  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_compare_snapshot, "_proc"):
        _maybe_run_compare_snapshot._proc = None  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_compare_snapshot, "_started_at_ts"):
        _maybe_run_compare_snapshot._started_at_ts = 0.0  # type: ignore[attr-defined]

    proc = _maybe_run_compare_snapshot._proc  # type: ignore[attr-defined]
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return
        started_at = float(_maybe_run_compare_snapshot._started_at_ts)  # type: ignore[attr-defined]
        elapsed_s = max(0.0, time.time() - started_at) if started_at > 0 else 0.0
        verbose = _background_task_logging_enabled()
        if rc != 0:
            _logln(f"[compare-refresh] task failed: exit={rc} elapsed={elapsed_s:.0f}s")
        elif verbose or elapsed_s >= BACKGROUND_TASK_SLOW_LOG_S:
            _logln(f"[compare-refresh] task completed in {elapsed_s:.0f}s")
        _maybe_run_compare_snapshot._proc = None  # type: ignore[attr-defined]
        _maybe_run_compare_snapshot._started_at_ts = 0.0  # type: ignore[attr-defined]

    now = time.time()
    last = float(_maybe_run_compare_snapshot._last_run_ts)  # type: ignore[attr-defined]
    if now - last < COMPARE_INTERVAL_S:
        return
    if _background_task_is_running(_maybe_run_leader_attr_snapshot):
        return

    script_path = os.path.join(SCRIPT_DIR, "build_leader_pnl_snapshot.py")
    if not os.path.exists(script_path):
        _maybe_run_compare_snapshot._last_run_ts = now  # type: ignore[attr-defined]
        return

    try:
        p = subprocess.Popen(
            [
                sys.executable,
                script_path,
                "--db",
                copytrade_db_path,
                "--compare-only",
                "--accounts",
                ",".join(normalized_accounts),
            ],
            stdout=subprocess.DEVNULL,
        )
        _maybe_run_compare_snapshot._proc = p  # type: ignore[attr-defined]
        _maybe_run_compare_snapshot._started_at_ts = now  # type: ignore[attr-defined]
        if _background_task_logging_enabled():
            _logln(
                "[compare-refresh] task launched "
                f"for accounts={','.join(normalized_accounts)} "
                f"next run not earlier than {int(COMPARE_INTERVAL_S)}s"
            )
    except Exception as e:
        _logln(f"[compare-refresh] launch failed: {e}")
    finally:
        _maybe_run_compare_snapshot._last_run_ts = now  # type: ignore[attr-defined]


def _maybe_report_hourly() -> None:
    if not hasattr(_poll_cycle, "_reject_counts"):
        _poll_cycle._reject_counts = {}  # type: ignore[attr-defined]

    now = time.time()
    if _hourly_stats["last_report_ts"] == 0.0:
        _hourly_stats["last_report_ts"] = now
        return

    if now - _hourly_stats["last_report_ts"] < HOURLY_REPORT_S:
        return

    counts: Dict[str, int] = _poll_cycle._reject_counts  # type: ignore[attr-defined]
    reject_total = sum(counts.values())
    parts = [f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    reject_detail = ", ".join(parts) if parts else "无"

    _logln(
        f"[hourly] 买入={_hourly_stats['buy_ok']} 失败={_hourly_stats['buy_fail']} "
        f"离场={_hourly_stats['exit_count']} 聚合={_hourly_stats['maker_like_count']} "
        f"拒绝={reject_total} | {reject_detail}"
    )

    # 重置
    _hourly_stats["buy_ok"] = 0
    _hourly_stats["buy_fail"] = 0
    _hourly_stats["exit_count"] = 0
    _hourly_stats["maker_like_count"] = 0
    _hourly_stats["last_report_ts"] = now
    _poll_cycle._reject_counts = {}  # type: ignore[attr-defined]


def _maybe_run_periodic_redeem(dry_run: bool) -> None:
    if dry_run:
        return
    if not hasattr(_maybe_run_periodic_redeem, "_last_run_ts"):
        _maybe_run_periodic_redeem._last_run_ts = 0.0  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_periodic_redeem, "_proc"):
        _maybe_run_periodic_redeem._proc = None  # type: ignore[attr-defined]

    proc = _maybe_run_periodic_redeem._proc  # type: ignore[attr-defined]
    if proc is not None:
        rc = proc.poll()
        if rc is None:
            return
        try:
            out, err = proc.communicate(timeout=1)
        except Exception:
            out, err = "", ""
        _maybe_run_periodic_redeem._proc = None  # type: ignore[attr-defined]
        if rc != 0:
            err_short = (err or out or "").strip()
            if err_short:
                _logln(f"[redeem] 任务失败: {err_short[:300]}")
            else:
                _logln(f"[redeem] 任务失败: exit={rc}")
        else:
            try:
                payload = json.loads((out or "").strip() or "{}")
                redeemed_batches = int(payload.get("redeemed_batches") or 0)
                redeemable = int(payload.get("redeemable") or 0)
                merged = int(payload.get("merged") or 0)
                if redeemed_batches > 0 or merged > 0:
                    _logln(f"[redeem] 完成: redeemable={redeemable} batches={redeemed_batches} merged={merged}")
                elif redeemable == 0 and merged == 0:
                    pass  # 静默：没有可 redeem/merge 的
            except Exception:
                pass

    now = time.time()
    last = float(_maybe_run_periodic_redeem._last_run_ts)  # type: ignore[attr-defined]
    if now - last < REDEEM_INTERVAL_S:
        return

    script_path = os.path.join(SCRIPT_DIR, "redeem_proxy.py")
    if not os.path.exists(script_path):
        _maybe_run_periodic_redeem._last_run_ts = now  # type: ignore[attr-defined]
        return

    try:
        p = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _maybe_run_periodic_redeem._proc = p  # type: ignore[attr-defined]
    except Exception as e:
        _logln(f"[redeem] 启动失败: {e}")
    finally:
        _maybe_run_periodic_redeem._last_run_ts = now  # type: ignore[attr-defined]


def _trigger_emergency_redeem() -> None:
    """余额不足时紧急触发 redeem+merge（非阻塞）."""
    if not hasattr(_maybe_run_periodic_redeem, "_proc"):
        _maybe_run_periodic_redeem._proc = None  # type: ignore[attr-defined]
    if not hasattr(_maybe_run_periodic_redeem, "_last_run_ts"):
        _maybe_run_periodic_redeem._last_run_ts = 0.0  # type: ignore[attr-defined]

    # 已有子进程在跑，不重复启动
    proc = _maybe_run_periodic_redeem._proc  # type: ignore[attr-defined]
    if proc is not None and proc.poll() is None:
        return

    script_path = os.path.join(SCRIPT_DIR, "redeem_proxy.py")
    if not os.path.exists(script_path):
        return

    _logln("[redeem] 紧急触发: 余额不足，启动 redeem+merge")
    try:
        p = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _maybe_run_periodic_redeem._proc = p  # type: ignore[attr-defined]
    except Exception as e:
        _logln(f"[redeem] 紧急启动失败: {e}")
    finally:
        _maybe_run_periodic_redeem._last_run_ts = time.time()  # type: ignore[attr-defined]


if __name__ == "__main__":
    raise SystemExit(main())
