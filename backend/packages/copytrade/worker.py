"""单账号 worker 线程 — 每个账号独立运行 poll 循环."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from copytrade.aggregation import prepare_copy_signals_live
from copytrade.account_config import AccountInfo
from copytrade.config import CopyTradeConfig
from copytrade.db import CopyTradeDB
from copytrade.domain import RuntimeEvent, classify_order_fill_status
from copytrade.executor import DryRunExecutor, OrderExecutor
from copytrade.exit_manager import ExitManager
from copytrade.monitor import LeaderTrade, TradeMonitor
from copytrade.risk import RiskManager
from copytrade.user_order_hub import UserOrderHub

HOURLY_REPORT_S = 3600
REDEEM_INTERVAL_S = 4 * 60 * 60
REDEEM_DEFAULT_RELAY_TIMEOUT_S = 45
REDEEM_DEFAULT_MAX_PER_RUN = 20
MERGE_DEFAULT_MAX_PER_RUN = 8
REDEEM_TIMEOUT_BUFFER_S = 3 * 60
REDEEM_TIMEOUT_S = 5 * 60  # 外层兜底下限；实际超时按 relay 超时和批次上限动态推导
EMERGENCY_REDEEM_COOLDOWN_S = 15 * 60  # 紧急 redeem 冷却 15 分钟
STREAM_IDLE_WAIT_S = 1.0
ORDER_VERIFY_INTERVAL_DEGRADED_S = 30.0
ORDER_VERIFY_INTERVAL_READY_S = 300.0
USER_ORDER_SUBSCRIPTION_REFRESH_S = 30.0
MAINTENANCE_RETRY_WINDOW_S = 15 * 60


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        return max(int(minimum), int(float(raw)))
    except ValueError:
        return int(default)


def _redeem_timeout_s() -> int:
    configured = os.getenv("COPYTRADE_REDEEM_TIMEOUT_S")
    if configured:
        return _env_int("COPYTRADE_REDEEM_TIMEOUT_S", REDEEM_TIMEOUT_S, minimum=60)
    relay_timeout = _env_int(
        "COPYTRADE_RELAY_EXECUTE_TIMEOUT_S",
        REDEEM_DEFAULT_RELAY_TIMEOUT_S,
        minimum=5,
    )
    max_redeem = _env_int("COPYTRADE_MAX_REDEEM_PER_RUN", REDEEM_DEFAULT_MAX_PER_RUN, minimum=1)
    max_merge = _env_int("COPYTRADE_MAX_MERGE_PER_RUN", MERGE_DEFAULT_MAX_PER_RUN, minimum=1)
    expected = relay_timeout * (max_redeem + max_merge) + REDEEM_TIMEOUT_BUFFER_S
    return max(REDEEM_TIMEOUT_S, expected)


def _logln(account_name: str, msg: str) -> None:
    sys.stderr.write(f"[{account_name}] {msg.rstrip()}\n")
    sys.stderr.flush()


class AccountWorker(threading.Thread):
    def __init__(
        self,
        account: AccountInfo,
        db: CopyTradeDB,
        *,
        dry_run: bool = False,
        once: bool = False,
        signal_queue=None,
    ):
        super().__init__(daemon=True, name=f"worker-{account.name}")
        self.account = account
        self.db = db
        self.dry_run = dry_run
        self.once = once
        self.signal_queue = signal_queue
        self._stop_event = threading.Event()

        self._hourly_stats: Dict[str, Any] = {
            "buy_ok": 0, "buy_fail": 0, "balance_pending": 0, "exit_count": 0,
            "maker_like_count": 0, "last_report_ts": 0.0,
        }
        self._reject_counts: Dict[str, int] = {}
        self._redeem_proc = None
        self._redeem_start_ts = 0.0
        self._redeem_last_ts = 0.0
        self._emergency_redeem_last_ts = 0.0
        self._maintenance_retry_pause_until = 0.0
        self._maker_like_state: Dict[Tuple, Dict[str, Any]] = {}
        self._last_verify_ts = 0.0
        self._last_user_order_subscription_refresh_ts = 0.0
        self._user_order_hub: Optional[UserOrderHub] = None

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _get_env_value(base_name: str, suffix: str) -> str:
        suffix = str(suffix or "").strip()
        if suffix:
            suffixed = os.environ.get(f"{base_name}_{suffix}", "").strip()
            if suffixed:
                return suffixed
        return os.environ.get(base_name, "").strip()

    def run(self) -> None:
        acct = self.account
        cfg = acct.config
        name = acct.name

        if self.dry_run:
            cfg.dry_run = True

        # 读取凭证
        suffix = str(acct.env_suffix or "").strip()
        pk = self._get_env_value("PRIVATE_KEY", suffix)
        funder = self._get_env_value("FUNDER_ADDRESS", suffix)

        missing_credentials = []
        if not pk:
            missing_credentials.append(f"PRIVATE_KEY_{suffix}" if suffix else "PRIVATE_KEY")
        if str(getattr(cfg, "wallet_type", "proxy") or "proxy").strip().lower() == "proxy" and not funder:
            missing_credentials.append(f"FUNDER_ADDRESS_{suffix}" if suffix else "FUNDER_ADDRESS")
        if missing_credentials and not cfg.dry_run:
            message = "missing live credentials: " + ", ".join(missing_credentials)
            _logln(name, f"[disabled] {message}")
            self._record_runtime_event(
                "credentials_missing",
                severity="error",
                message=message,
                details={"missing": missing_credentials, "wallet_type": getattr(cfg, "wallet_type", None)},
            )
            self.db.upsert_worker_heartbeat(
                account_name=name,
                component="worker",
                status="disabled_missing_credentials",
                pid=os.getpid(),
                details={"missing": missing_credentials},
            )
            return

        self.db.upsert_worker_heartbeat(
            account_name=name,
            component="worker",
            status="starting",
            pid=os.getpid(),
            details={"dry_run": bool(cfg.dry_run), "signal_source": cfg.signal_source},
        )

        # 初始化模块
        session = requests.Session()
        if cfg.dry_run:
            executor = DryRunExecutor(cfg)
        else:
            executor = OrderExecutor(cfg, pk=pk, funder=funder, wallet_type=cfg.wallet_type, env_suffix=suffix)

        monitor = TradeMonitor(
            session,
            self.db,
            cfg.leader_addresses,
            account_name=name,
            signal_source=cfg.signal_source,
            fetch_workers=cfg.signal_fetch_workers,
            signal_queue=self.signal_queue,
            signal_reconcile_interval_s=cfg.signal_reconcile_interval_s,
        )
        risk = RiskManager(session, cfg, self.db, account_name=name)
        user_order_hub: Optional[UserOrderHub] = None
        if not cfg.dry_run and getattr(executor, "_client", None) is not None:
            get_api_creds = getattr(executor, "get_api_creds", None)
            if callable(get_api_creds):
                try:
                    creds = get_api_creds()
                    if creds:
                        user_order_hub = UserOrderHub(
                            name,
                            api_key=str(creds.get("api_key") or ""),
                            api_secret=str(creds.get("api_secret") or ""),
                            api_passphrase=str(creds.get("api_passphrase") or ""),
                        )
                        user_order_hub.start()
                        self._user_order_hub = user_order_hub
                except Exception as e:
                    _logln(name, f"[warn] user order hub startup failed: {e}")
        exit_mgr = ExitManager(
            session,
            cfg,
            self.db,
            executor,
            account_name=name,
            on_condition_activated=(user_order_hub.ensure_market if user_order_hub is not None else None),
        )

        mode_label = "DRY-RUN" if cfg.dry_run else "LIVE"
        interval_label = f"idle={STREAM_IDLE_WAIT_S:.0f}s reconcile={cfg.signal_reconcile_interval_s}s" if monitor.is_stream_mode() else f"interval={cfg.poll_interval_s}s"
        _logln(
            name,
            f"启动 [{mode_label}] leaders={len(cfg.leader_addresses)} mode={cfg.copy_mode} "
            f"{interval_label} signal={cfg.signal_source} workers={cfg.signal_fetch_workers}",
        )

        try:
            while not self._stop_event.is_set():
                try:
                    self.db.upsert_worker_heartbeat(
                        account_name=name,
                        component="worker",
                        status="running",
                        pid=os.getpid(),
                        details={"dry_run": bool(cfg.dry_run), "signal_source": cfg.signal_source},
                    )
                    if monitor.is_stream_mode() and not self.once:
                        monitor.wait_for_signal(timeout=STREAM_IDLE_WAIT_S)
                    self._poll_cycle(monitor, risk, executor, exit_mgr, cfg)
                    self._maybe_run_redeem(cfg, suffix)
                except Exception as e:
                    _logln(name, f"[error] 轮询异常: {e}")
                    self._record_runtime_event(
                        "poll_cycle_error",
                        severity="error",
                        message=str(e),
                    )

                if self.once:
                    break
                if not monitor.is_stream_mode():
                    self._stop_event.wait(cfg.poll_interval_s)
        finally:
            if user_order_hub is not None:
                try:
                    user_order_hub.stop()
                    user_order_hub.join(timeout=5.0)
                except Exception:
                    pass
            self._user_order_hub = None
            self.db.upsert_worker_heartbeat(
                account_name=name,
                component="worker",
                status="stopped",
                pid=os.getpid(),
                details={"dry_run": bool(cfg.dry_run), "signal_source": cfg.signal_source},
            )

        _logln(name, "stopped")

    # ------------------------------------------------------------------
    # poll cycle
    # ------------------------------------------------------------------

    def _poll_cycle(
        self, monitor: TradeMonitor, risk: RiskManager,
        executor, exit_mgr: ExitManager, cfg: CopyTradeConfig,
    ) -> None:
        name = self.account.name
        user_order_hub = self._user_order_hub
        if user_order_hub is not None:
            events = user_order_hub.drain_events()
            if events:
                summary = exit_mgr.process_user_order_events(events)
                self._hourly_stats["buy_ok"] += int(summary.get("buy_fill_count") or 0)
            self._maybe_refresh_user_order_subscriptions()
        new_trades = monitor.poll_once()
        copy_signals = self._prepare_copy_signals(new_trades, cfg)

        self._maybe_report_hourly()
        self._expire_maintenance_retries()
        self._process_maintenance_retries(risk, executor, exit_mgr, cfg, user_order_hub)

        for lt in copy_signals:
            self._process_buy_signal(
                lt,
                risk,
                executor,
                exit_mgr,
                user_order_hub,
                retrying_maintenance=False,
            )

        exit_actions = exit_mgr.process_exits(new_trades, skip_verification=True)
        if exit_actions:
            self._hourly_stats["exit_count"] += len(exit_actions)

        # 验证近期订单真实成交状态（仅 LIVE 模式）
        if getattr(executor, '_client', None) is not None:
            self._maybe_verify_recent_orders(executor, exit_mgr)

    # ------------------------------------------------------------------
    def _process_buy_signal(
        self,
        lt: LeaderTrade,
        risk: RiskManager,
        executor,
        exit_mgr: ExitManager,
        user_order_hub: Optional[UserOrderHub],
        *,
        retrying_maintenance: bool = False,
    ) -> None:
        name = self.account.name
        if str(getattr(lt, "side", "") or "").upper() != "BUY":
            self._record_reject("not_buy_side")
            self._record_signal_audit(lt, stage="ignored", reason="not_buy_side")
            return

        crypto_check = getattr(risk, "check_crypto_only", None)
        if callable(crypto_check):
            try:
                ok, reason = crypto_check(lt)
            except Exception as e:
                self._record_reject("crypto_only_error")
                self._record_signal_audit(lt, stage="failed_internal", reason=f"crypto_only_error: {e}")
                self._finalize_signal_attempt(lt, "failed_internal", reason=f"crypto_only_error: {e}")
                return
            if not ok:
                self._record_reject(self._classify_risk_reason(reason))
                self._record_signal_audit(lt, stage="risk_rejected", reason=reason)
                self._finalize_signal_attempt(lt, "risk_rejected", reason=reason)
                return

        try:
            params = executor.compute_order_params(lt, db=self.db, account_name=self.account.name)
        except Exception as e:
            self._record_reject("compute_params_error")
            self._record_signal_audit(lt, stage="failed_internal", reason=f"compute_params_error: {e}")
            self._finalize_signal_attempt(lt, "failed_internal", reason=f"compute_params_error: {e}")
            return

        if params is None:
            self._record_reject("cannot_compute_params")
            self._record_signal_audit(lt, stage="skipped", reason="cannot_compute_params")
            self._finalize_signal_attempt(lt, "skipped", reason="cannot_compute_params")
            return

        try:
            ok, reason = risk.check_all(lt, params.usd)
        except Exception as e:
            self._record_reject("risk_check_error")
            self._record_signal_audit(lt, stage="failed_internal", reason=f"risk_check_error: {e}")
            self._finalize_signal_attempt(lt, "failed_internal", reason=f"risk_check_error: {e}")
            return

        if not ok:
            self._record_reject(self._classify_risk_reason(reason))
            self._record_signal_audit(
                lt,
                stage="risk_rejected",
                reason=reason,
                details={"requested_usd": params.usd, "requested_size": params.size},
            )
            self._finalize_signal_attempt(lt, "risk_rejected", reason=reason)
            return

        result = executor.execute_order(params)
        if not result.success:
            error_code = str(getattr(result, "error_code", "") or "").strip().lower()
            reason = str(result.error or error_code or "order_failed")
            if retrying_maintenance:
                self._record_signal_audit(
                    lt,
                    stage="maintenance_retry_failed",
                    reason=reason,
                    details={
                        "error_code": error_code,
                        "requested_usd": params.usd,
                        "requested_size": params.size,
                        "submitted_size": getattr(result, "submitted_size", None),
                        "min_order_size": getattr(result, "min_order_size", None),
                        "limit_price": getattr(result, "limit_price", None),
                    },
                )
                self._finalize_signal_attempt(
                    lt,
                    "order_failed",
                    reason=reason,
                    last_error_code=error_code or None,
                )
                self._hourly_stats["buy_fail"] += 1
                return
            if str(getattr(params, "side", "") or "").strip().upper() == "BUY" and error_code == "min_order_size":
                self._record_reject("min_size_skipped")
                self._record_signal_audit(
                    lt,
                    stage="min_size_skipped",
                    reason=reason,
                    details={
                        "requested_usd": params.usd,
                        "requested_size": params.size,
                        "submitted_size": getattr(result, "submitted_size", None),
                        "min_order_size": getattr(result, "min_order_size", None),
                        "limit_price": getattr(result, "limit_price", None),
                    },
                )
                self._finalize_signal_attempt(lt, "skipped", reason=reason)
                return
            if str(getattr(params, "side", "") or "").strip().upper() == "BUY" and error_code == "balance_allowance":
                maintenance_state = self._mark_signal_maintenance_pending(lt, params, result)
                if maintenance_state == "pending":
                    self._hourly_stats["balance_pending"] = self._hourly_stats.get("balance_pending", 0) + 1
                    self._trigger_emergency_redeem(self.account.env_suffix)
                    return
                if maintenance_state == "handled":
                    return

            self._record_signal_audit(
                lt,
                stage="order_failed",
                reason=reason,
                details={"requested_usd": params.usd, "requested_size": params.size, "error_code": error_code},
            )
            self._finalize_signal_attempt(
                lt,
                "order_failed",
                reason=reason,
                last_error_code=error_code or None,
            )
            self._hourly_stats["buy_fail"] += 1
            err_lower = (result.error or "").lower()
            if (
                "insufficient_clob" in err_lower
                or "not enough balance" in err_lower
                or "allowance" in err_lower
            ):
                self._hourly_stats["balance_pending"] = self._hourly_stats.get("balance_pending", 0) + 1
            return

        immediate_fill = self._is_immediate_fill(result)
        exchange_status = str(getattr(result, "exchange_status", "") or ("matched" if immediate_fill else "submitted"))
        immediate_filled_size = result.filled_size
        if immediate_fill and immediate_filled_size is None and exchange_status.lower() == "matched":
            immediate_filled_size = params.size
        trade_status, partial_fill_status = (
            classify_order_fill_status(exchange_status, immediate_filled_size, params.size)
            if immediate_fill
            else ("submitted", None)
        )
        trade_id = self._persist_trade(
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
            skip_reason=None,
            token_id=params.token_id,
            condition_id=params.condition_id,
            market_slug=params.market_slug,
            outcome=params.outcome,
            is_aggregated_order=1 if getattr(lt, "is_maker_like_aggregated", False) else 0,
            aggregation_source_count=getattr(lt, "aggregation_source_count", None),
        )
        if retrying_maintenance:
            self._record_signal_audit(
                lt,
                stage="maintenance_retry_submitted",
                reason="maintenance_retry_submitted",
                details={
                    "order_id": result.order_id,
                    "exchange_status": exchange_status,
                    "requested_usd": params.usd,
                    "requested_size": params.size,
                },
            )
            self._finalize_signal_attempt(
                lt,
                "filled" if immediate_fill else "submitted",
                reason="maintenance_retry_submitted",
                last_error_code=None,
            )
        else:
            self._finalize_signal_attempt(
                lt,
                "filled" if immediate_fill else "submitted",
                reason=None,
                last_error_code=None,
            )

        if result.success and immediate_fill:
            self.db.add_daily_spend(result.filled_usd or 0.0, account_name=name)
            self._hourly_stats["buy_ok"] += 1
            register_entry_fill = getattr(exit_mgr, "register_entry_fill", None)
            if callable(register_entry_fill):
                register_entry_fill(
                    trade_id,
                    filled_size=float(result.filled_size or 0.0),
                    filled_usd=result.filled_usd,
                    fill_price=result.filled_price,
                )
        if result.success and user_order_hub is not None:
            user_order_hub.ensure_market(params.condition_id)

    def _mark_signal_maintenance_pending(self, lt: LeaderTrade, params, result) -> str:
        attempt_id = getattr(lt, "signal_attempt_id", None)
        if not attempt_id:
            return ""
        reason = str(getattr(result, "error", "") or "balance_allowance")
        expires_at = self._maintenance_retry_expires_at(lt, int(attempt_id))
        if self._iso_at_or_before_now(expires_at):
            self._record_signal_audit(
                lt,
                stage="maintenance_retry_expired",
                reason="maintenance_retry_expired",
                details={
                    "expires_at": expires_at,
                    "requested_usd": params.usd,
                    "requested_size": params.size,
                    "error_code": getattr(result, "error_code", None),
                },
            )
            self._finalize_signal_attempt(
                lt,
                "skipped",
                reason="maintenance_retry_expired",
                last_error_code=getattr(result, "error_code", None),
            )
            return "handled"
        ok = self.db.mark_signal_attempt_maintenance_pending(
            int(attempt_id),
            reason=reason,
            error_code=str(getattr(result, "error_code", "") or "balance_allowance"),
            expires_at=expires_at,
            retry_after=None,
        )
        if not ok:
            return ""
        self._record_signal_audit(
            lt,
            stage="maintenance_pending",
            reason=reason,
            details={
                "expires_at": expires_at,
                "requested_usd": params.usd,
                "requested_size": params.size,
                "submitted_size": getattr(result, "submitted_size", None),
                "limit_price": getattr(result, "limit_price", None),
                "error_code": getattr(result, "error_code", None),
            },
        )
        return "pending"

    def _process_maintenance_retries(
        self,
        risk: RiskManager,
        executor,
        exit_mgr: ExitManager,
        cfg: CopyTradeConfig,
        user_order_hub: Optional[UserOrderHub],
    ) -> None:
        if bool(getattr(cfg, "dry_run", False)):
            return
        if self._maintenance_retry_paused():
            return
        rows = self.db.get_due_maintenance_signal_attempts(account_name=self.account.name, limit=50)
        for row in rows:
            attempt_id = int(row.get("id") or 0)
            if attempt_id <= 0:
                continue
            if not self.db.claim_maintenance_signal_attempt_retry(attempt_id):
                continue
            lt = self._leader_trade_from_attempt_row(row)
            if lt is None:
                try:
                    self.db.update_signal_attempt_status(
                        attempt_id,
                        "failed_internal",
                        reason="maintenance_retry_rebuild_failed",
                    )
                except Exception:
                    pass
                continue
            self._process_buy_signal(
                lt,
                risk,
                executor,
                exit_mgr,
                user_order_hub,
                retrying_maintenance=True,
            )

    def _expire_maintenance_retries(self) -> None:
        rows = self.db.expire_maintenance_signal_attempts(account_name=self.account.name)
        for row in rows:
            lt = self._leader_trade_from_attempt_row(row)
            if lt is None:
                continue
            self._record_signal_audit(
                lt,
                stage="maintenance_retry_expired",
                reason="maintenance_retry_expired",
                details={
                    "expires_at": row.get("expires_at"),
                    "retry_count": row.get("retry_count"),
                    "last_error_code": row.get("last_error_code"),
                },
            )

    def _leader_trade_from_attempt_row(self, row: Dict[str, Any]) -> Optional[LeaderTrade]:
        leader_address = str(row.get("leader_address") or "").strip().lower()
        fill_key = str(row.get("leader_fill_key") or "").strip()
        token_id = str(row.get("token_id") or "").strip()
        side = str(row.get("leader_side") or "").strip().upper()
        if not leader_address or not fill_key or not token_id or side not in {"BUY", "SELL"}:
            return None
        timestamp = str(row.get("created_at") or row.get("updated_at") or "")
        ts_int = self._timestamp_to_int(timestamp)
        return LeaderTrade(
            leader_address=leader_address,
            tx_hash=str(row.get("leader_tx_hash") or ""),
            fill_key=fill_key,
            timestamp=timestamp,
            side=side,
            token_id=token_id,
            condition_id=str(row.get("condition_id") or ""),
            price=row.get("leader_price"),
            size=row.get("leader_size"),
            usd_amount=row.get("leader_usd"),
            outcome=row.get("outcome"),
            market_slug=row.get("market_slug"),
            ts_int=ts_int,
            signal_attempt_id=int(row["id"]) if row.get("id") is not None else None,
            source=str(row.get("source") or "attempt").strip().lower() or "attempt",
        )

    def _maintenance_retry_expires_at(self, lt: LeaderTrade, attempt_id: int) -> str:
        base = self._leader_signal_datetime(lt)
        if base is None:
            try:
                row = self.db.conn.execute(
                    "SELECT created_at FROM ct_signal_attempts WHERE id=?",
                    (int(attempt_id),),
                ).fetchone()
            except Exception:
                row = None
            base = self._parse_datetime(row["created_at"]) if row else None
        if base is None:
            base = datetime.now(timezone.utc)
        return (base + timedelta(seconds=MAINTENANCE_RETRY_WINDOW_S)).isoformat()

    @staticmethod
    def _leader_signal_datetime(lt: LeaderTrade) -> Optional[datetime]:
        ts_int = getattr(lt, "ts_int", None)
        try:
            if ts_int is not None and int(ts_int) > 0:
                return datetime.fromtimestamp(int(ts_int), tz=timezone.utc)
        except Exception:
            pass
        return AccountWorker._parse_datetime(getattr(lt, "timestamp", None))

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            if text.isdigit():
                return datetime.fromtimestamp(int(text), tz=timezone.utc)
            parsed = datetime.fromisoformat(text)
        except Exception:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_int(value: Any) -> Optional[int]:
        parsed = AccountWorker._parse_datetime(value)
        if parsed is None:
            return None
        return int(parsed.timestamp())

    @staticmethod
    def _iso_at_or_before_now(value: Any) -> bool:
        parsed = AccountWorker._parse_datetime(value)
        return bool(parsed is not None and parsed <= datetime.now(timezone.utc))

    def _maintenance_retry_paused(self) -> bool:
        proc = self._redeem_proc
        if proc is not None and proc.poll() is None:
            return True
        return time.time() < float(self._maintenance_retry_pause_until or 0.0)

    # ------------------------------------------------------------------
    def _prepare_copy_signals(self, new_trades: List[LeaderTrade], cfg: CopyTradeConfig) -> List[LeaderTrade]:
        out = prepare_copy_signals_live(new_trades, cfg, self._maker_like_state)
        emitted_keys = {
            str(getattr(trade, "fill_key", "") or "")
            for trade in out
            if getattr(trade, "fill_key", None)
        }
        for trade in new_trades:
            if str(getattr(trade, "fill_key", "") or "") in emitted_keys:
                continue
            if str(getattr(trade, "side", "") or "").upper() != "BUY":
                continue
            lcfg = cfg.get_leader_config(getattr(trade, "leader_address", ""))
            try:
                usd = float(getattr(trade, "usd_amount", 0.0) or 0.0)
                min_usd = float(getattr(lcfg, "min_trade_size_usd", 0.0) or 0.0)
            except Exception:
                continue
            if usd > 0 and usd < min_usd and bool(getattr(lcfg, "maker_like_enabled", False)):
                self._record_signal_audit(
                    trade,
                    stage="aggregation_pending",
                    reason="below_min_signal_threshold",
                    details={"leader_usd": usd, "min_trade_size_usd": min_usd},
                )
        if out:
            self._hourly_stats["maker_like_count"] += sum(
                1 for trade in out if getattr(trade, "is_maker_like_aggregated", False)
            )
        return out

    @staticmethod
    def _compute_maker_like_score(*, count, span_s, max_piece_usd, min_trade_size_usd, window_s) -> float:
        frag = min(1.0, max(0.0, (count - 1) / 4.0))
        piece_ratio = max_piece_usd / max(min_trade_size_usd, 1e-9)
        small_piece = 1.0 - min(1.0, piece_ratio)
        continuity = 1.0 - min(1.0, span_s / max(window_s, 1))
        return max(0.0, min(1.0, 0.45 * frag + 0.35 * small_piece + 0.20 * continuity))


    @staticmethod
    def _classify_risk_reason(reason: str) -> str:
        s = (reason or "").lower()
        if s.startswith("crypto_only:"):
            return "crypto_only"
        if s.startswith("leader_market_once"): return "leader_market_once"
        if s.startswith("leader usd"): return "min_trade_size"
        if s.startswith("price ") and "< min" in s: return "price_below_min"
        if s.startswith("price ") and "> max" in s: return "price_above_max"
        if s.startswith("settlement in"): return "settlement_too_far"
        if s.startswith("global_once"): return "market_global_once"
        if s.startswith("per_address_once"): return "market_per_address_once"
        if s.startswith("position "): return "position_limit"
        return "risk_other"

    def _record_reject(self, kind: str) -> None:
        self._reject_counts[kind] = self._reject_counts.get(kind, 0) + 1

    def _record_runtime_event(
        self,
        event_type: str,
        *,
        severity: str = "info",
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = RuntimeEvent(
            event_type=event_type,
            severity=severity,
            component="worker",
            account_name=self.account.name,
            message=message,
            details=details or {},
        )
        try:
            self.db.record_runtime_event(
                account_name=event.account_name,
                component=event.component,
                event_type=event.event_type,
                severity=event.severity,
                message=event.message,
                details=event.details,
            )
        except Exception:
            pass

    def _record_signal_audit(
        self,
        lt: LeaderTrade,
        *,
        stage: str,
        reason: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            signal = lt.to_leader_signal(self.account.name)
            self.db.record_signal_audit(
                account_name=signal.account_name,
                leader_address=signal.leader_address,
                leader_fill_key=signal.leader_fill_key,
                leader_side=signal.side,
                token_id=signal.token_id,
                condition_id=signal.condition_id,
                source=signal.source,
                stage=stage,
                reason=reason,
                details=details or {},
            )
        except Exception:
            pass

    def _base_trade_record(self, lt: LeaderTrade) -> Dict[str, Any]:
        return {
            "account_name": self.account.name,
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

    def _persist_trade(self, lt: LeaderTrade, status: str, **fields: Any) -> int:
        payload = self._base_trade_record(lt)
        payload.update(fields)
        payload["status"] = status

        trade_id = getattr(lt, "trade_id", None)
        if trade_id:
            payload.pop("account_name", None)
            payload.pop("status", None)
            self.db.update_trade_status(int(trade_id), status, **payload)
            return int(trade_id)
        return self.db.insert_trade(payload)

    def _finalize_signal_attempt(
        self,
        lt: LeaderTrade,
        status: str,
        *,
        reason: Optional[str] = None,
        **fields: Any,
    ) -> None:
        attempt_id = getattr(lt, "signal_attempt_id", None)
        if not attempt_id:
            return
        try:
            self.db.update_signal_attempt_status(int(attempt_id), status, reason=reason, **fields)
        except Exception:
            pass

    @staticmethod
    def _is_immediate_fill(result) -> bool:
        status = str(getattr(result, "exchange_status", "") or "").lower()
        if status == "matched":
            return True
        try:
            return float(getattr(result, "filled_size", 0) or 0) > 0
        except Exception:
            return False

    # ------------------------------------------------------------------
    # order verification
    # ------------------------------------------------------------------

    def _verify_recent_orders(self, executor, exit_mgr: Optional[ExitManager] = None) -> None:
        if exit_mgr is None or getattr(executor, "_client", None) is None:
            return
        summary = exit_mgr.verify_recent_order_state(source="rest")
        self._hourly_stats["buy_ok"] += int(summary.get("buy_fill_count") or 0)

    def _maybe_verify_recent_orders(self, executor, exit_mgr: Optional[ExitManager] = None) -> None:
        now = time.time()
        hub = self._user_order_hub
        interval_s = ORDER_VERIFY_INTERVAL_DEGRADED_S
        force_verify = False
        if hub is not None:
            force_verify = hub.consume_force_reconcile()
            if hub.is_ready():
                interval_s = ORDER_VERIFY_INTERVAL_READY_S
        if not force_verify and self._last_verify_ts and (now - self._last_verify_ts) < interval_s:
            return
        self._last_verify_ts = now
        self._verify_recent_orders(executor, exit_mgr)

    def _maybe_refresh_user_order_subscriptions(self) -> None:
        hub = self._user_order_hub
        if hub is None:
            return
        now = time.time()
        if self._last_user_order_subscription_refresh_ts and (
            now - self._last_user_order_subscription_refresh_ts
        ) < USER_ORDER_SUBSCRIPTION_REFRESH_S:
            return
        self._last_user_order_subscription_refresh_ts = now
        try:
            condition_ids = self.db.get_active_user_order_condition_ids(account_name=self.account.name)
            hub.replace_markets(condition_ids)
        except Exception as e:
            _logln(self.account.name, f"[warn] refresh user order subscriptions failed: {e}")

    # ------------------------------------------------------------------
    # hourly report
    # ------------------------------------------------------------------

    def _maybe_report_hourly(self) -> None:
        now = time.time()
        if self._hourly_stats["last_report_ts"] == 0.0:
            self._hourly_stats["last_report_ts"] = now
            return
        if now - self._hourly_stats["last_report_ts"] < HOURLY_REPORT_S:
            return

        reject_total = sum(self._reject_counts.values())
        parts = [f"{k}:{v}" for k, v in sorted(self._reject_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        detail = ", ".join(parts) if parts else "无"

        _logln(self.account.name,
            f"[hourly] 买入={self._hourly_stats['buy_ok']} 失败={self._hourly_stats['buy_fail']} "
            f"余额待维护={self._hourly_stats.get('balance_pending', 0)} "
            f"离场={self._hourly_stats['exit_count']} 聚合={self._hourly_stats['maker_like_count']} "
            f"拒绝={reject_total} | {detail}"
        )

        self._hourly_stats = {
            "buy_ok": 0, "buy_fail": 0, "balance_pending": 0, "exit_count": 0,
            "maker_like_count": 0, "last_report_ts": now,
        }
        self._reject_counts = {}

    # ------------------------------------------------------------------
    # redeem
    # ------------------------------------------------------------------

    def _maybe_run_redeem(self, cfg: CopyTradeConfig, env_suffix: str) -> None:
        if cfg.dry_run:
            return

        proc = self._redeem_proc
        if proc is not None:
            rc = proc.poll()
            if rc is None:
                # 超时保护：防止 redeem 子进程卡死
                elapsed = time.time() - self._redeem_start_ts
                timeout_s = _redeem_timeout_s()
                if elapsed > timeout_s:
                    _logln(self.account.name, f"[redeem] 子进程超时 ({elapsed:.0f}s/{timeout_s}s)，强制终止")
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    self._redeem_proc = None
                    self._maintenance_retry_pause_until = time.time() + EMERGENCY_REDEEM_COOLDOWN_S
                    self._record_runtime_event(
                        "maintenance_redeem_timeout",
                        severity="error",
                        message=f"redeem timeout after {elapsed:.0f}s (limit {timeout_s}s)",
                    )
                return
            try:
                out, err = proc.communicate(timeout=1)
            except Exception:
                out, err = "", ""
            self._redeem_proc = None
            if rc == 0:
                payload: Dict[str, Any] = {}
                try:
                    payload = json.loads((out or "").strip() or "{}")
                    redeemed = int(payload.get("redeemed_batches") or 0)
                    merged = int(payload.get("merged") or 0)
                    if redeemed > 0 or merged > 0:
                        _logln(self.account.name, f"[redeem] 完成: batches={redeemed} merged={merged}")
                except Exception:
                    pass
                released = self.db.release_maintenance_signal_attempts(account_name=self.account.name)
                if released:
                    _logln(self.account.name, f"[redeem] released maintenance retries: {released}")
                self._record_runtime_event(
                    "maintenance_redeem_completed",
                    severity="info",
                    message="redeem completed",
                    details={"released_attempts": released, "payload": payload},
                )
            else:
                self._maintenance_retry_pause_until = time.time() + EMERGENCY_REDEEM_COOLDOWN_S
                err_short = (err or out or "").strip()
                self._record_runtime_event(
                    "maintenance_redeem_failed",
                    severity="error",
                    message=err_short[:300] if err_short else f"redeem exited {rc}",
                    details={"exit_code": rc},
                )
                if err_short:
                    _logln(self.account.name, f"[redeem] 任务失败: {err_short[:300]}")
                else:
                    _logln(self.account.name, f"[redeem] 任务失败: exit={rc}")

        now = time.time()
        if now - self._redeem_last_ts < REDEEM_INTERVAL_S:
            return

        self._launch_redeem(env_suffix, wallet_type=cfg.wallet_type)
        self._redeem_last_ts = now

    def _trigger_emergency_redeem(self, env_suffix: str) -> None:
        now = time.time()
        if now < float(self._maintenance_retry_pause_until or 0.0):
            return
        if now - self._emergency_redeem_last_ts < EMERGENCY_REDEEM_COOLDOWN_S:
            return  # 冷却中，不重复触发
        proc = self._redeem_proc
        if proc is not None and proc.poll() is None:
            self._emergency_redeem_last_ts = now
            self._record_runtime_event(
                "maintenance_redeem_trigger_skipped",
                severity="info",
                message="redeem already running",
            )
            return
        _logln(self.account.name, "[redeem] 紧急触发: 余额不足")
        self._launch_redeem(env_suffix, wallet_type=self.account.config.wallet_type)
        self._emergency_redeem_last_ts = now
        # 注意：不更新 _redeem_last_ts，让定时 redeem 不受影响

    def _launch_redeem(self, env_suffix: str, *, wallet_type: str = "proxy") -> None:
        script = os.path.join(os.path.dirname(__file__), "redeem_proxy.py")
        if not os.path.exists(script):
            return
        try:
            cmd = [sys.executable, script]
            if env_suffix:
                cmd.extend(["--account", env_suffix])
            cmd.extend(["--wallet-type", wallet_type])
            p = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self._redeem_proc = p
            self._redeem_start_ts = time.time()
        except Exception as e:
            _logln(self.account.name, f"[redeem] 启动失败: {e}")
