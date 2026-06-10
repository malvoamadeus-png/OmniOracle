"""下单执行 — py-clob-client-v2 SDK."""

import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from copytrade.aggregation import get_effective_signal_price
from copytrade.config import CopyTradeConfig
from copytrade.domain import COPY_MODE_FIXED_USD, COPY_MODE_PROPORTIONAL, DEFAULT_CLOB_MIN_ORDER_SIZE
from copytrade.monitor import LeaderTrade
from copytrade.paths import DOTENV_PATH

_ORDERBOOK_UNAVAILABLE = object()
_FALLBACK_CLOB_MIN_ORDER_SIZE = DEFAULT_CLOB_MIN_ORDER_SIZE


@dataclass
class OrderParams:
    token_id: str
    side: str           # BUY or SELL
    price: float
    size: float
    usd: float
    condition_id: str
    market_slug: Optional[str]
    outcome: Optional[str]
    passive_price_mode: bool = False
    pricing_mode: str = "aggressive"
    hard_max_price: float = 1.0
    aggressive_price_chase_cap_abs: float = 0.01
    aggressive_price_chase_cap_bps: float = 300.0
    order_purpose: str = "copytrade"
    tif: Optional[str] = None
    expiration_ts: Optional[int] = None
    enforce_budget: bool = True


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_price: Optional[float] = None
    filled_size: Optional[float] = None
    filled_usd: Optional[float] = None
    limit_price: Optional[float] = None      # 提交给交易所的限价
    exchange_status: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    submitted_size: Optional[float] = None
    min_order_size: Optional[float] = None
    retryable: bool = False


class OrderExecutor:
    @staticmethod
    def _find_repo_dotenv() -> Optional[Path]:
        return DOTENV_PATH if DOTENV_PATH.exists() else None

    def __init__(self, config: CopyTradeConfig, *, pk: Optional[str] = None, funder: Optional[str] = None, wallet_type: str = "proxy", env_suffix: str = ""):
        self.config = config
        self._client = None
        self._env_suffix = env_suffix
        self._builder_code: Optional[str] = None
        self._market_constraints_cache = {}
        self._market_info_cache = {}
        self._orderbook_availability_cache = {}
        self._init_client(pk=pk, funder=funder, wallet_type=wallet_type)

    def _get_env_value(self, base_name: str) -> str:
        suffix = str(getattr(self, "_env_suffix", "") or "").strip()
        if suffix:
            value = os.getenv(f"{base_name}_{suffix}", "").strip()
            if value:
                return value
        return os.getenv(base_name, "").strip()

    def _init_client(self, *, pk: Optional[str] = None, funder: Optional[str] = None, wallet_type: str = "proxy") -> None:
        """初始化 CLOB client.

        wallet_type:
          - "proxy": Google/Email 登录的 Proxy Wallet (signature_type=1, 需要 funder)
          - "eoa": 自托管钱包 (signature_type=0, 不需要 funder, 需要 update_balance_allowance)
        """
        try:
            from dotenv import load_dotenv
        except ImportError:
            load_dotenv = None  # type: ignore[assignment]

        # 加载仓库根目录 .env
        try:
            if load_dotenv:
                dotenv_path = self._find_repo_dotenv()
                if dotenv_path is not None and dotenv_path.exists():
                    load_dotenv(dotenv_path=dotenv_path, override=False)
        except Exception:
            pass

        # 新命名优先，旧命名作为 fallback
        if not pk:
            pk = (os.getenv("PK") or os.getenv("PK_AI") or os.getenv("PRIVATE_KEY") or "").strip()
        if not funder:
            funder = (os.getenv("FUNDER_ADDRESS") or os.getenv("FUNDER_ADDRESS_AI") or "").strip()

        if not pk:
            sys.stderr.write("[executor] 警告: 未找到私钥，无法下单\n")
            self._client = None
            return

        try:
            from py_clob_client_v2 import ApiCreds, BuilderConfig, ClobClient, SignatureTypeV2

            host = (os.getenv("POLYMARKET_CLOB_HOST") or "https://clob.polymarket.com").strip()
            chain_id = 137  # Polygon
            builder_code = (
                self._get_env_value("POLY_BUILDER_CODE")
                or os.getenv("BUILDER_CODE", "").strip()
            )
            self._builder_code = builder_code or None
            builder_config = BuilderConfig(builder_code=builder_code) if builder_code else None

            api_key = self._get_env_value("CLOB_API_KEY")
            api_secret = self._get_env_value("CLOB_SECRET")
            api_passphrase = self._get_env_value("CLOB_PASS_PHRASE")
            use_static_creds = (os.getenv("POLY_USE_STATIC_CLOB_CREDS") or "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            creds = (
                ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )
                if use_static_creds and api_key and api_secret and api_passphrase
                else None
            )

            if wallet_type == "eoa":
                # EOA + Polymarket Safe: signature_type=2 (POLY_GNOSIS_SAFE)
                # EOA 是 signer，funder 是 Polymarket 派生的 Safe 地址
                sig_type = SignatureTypeV2.POLY_GNOSIS_SAFE if funder else SignatureTypeV2.EOA
                client = ClobClient(
                    host=host,
                    chain_id=chain_id,
                    key=pk,
                    creds=creds,
                    signature_type=sig_type,
                    funder=funder or None,
                    builder_config=builder_config,
                )
                if not use_static_creds or creds is None:
                    client.set_api_creds(self._obtain_api_creds(client, ApiCreds))
                self._client = client
                label = "EOA+Safe" if funder else "EOA"
                sys.stderr.write(f"[executor] CLOB v2 client 初始化成功（{label} sig_type={int(sig_type)}）\n")

            else:
                # Proxy 模式（默认）: signature_type=1, 需要 funder
                if not funder:
                    sys.stderr.write("[executor] 错误: proxy wallet_type requires FUNDER_ADDRESS for CLOB v2\n")
                    self._client = None
                    return

                sig_type = SignatureTypeV2.POLY_PROXY

                client = ClobClient(
                    host=host,
                    chain_id=chain_id,
                    key=pk,
                    creds=creds,
                    signature_type=sig_type,
                    funder=funder,
                    builder_config=builder_config,
                )
                if not use_static_creds or creds is None:
                    client.set_api_creds(self._obtain_api_creds(client, ApiCreds))
                self._client = client
                sys.stderr.write("[executor] CLOB v2 client 初始化成功（Proxy 模式）\n")

        except ImportError:
            sys.stderr.write("[executor] 错误: py-clob-client-v2 未安装，请运行 pip install py-clob-client-v2==1.0.0\n")
            self._client = None
        except Exception as e:
            sys.stderr.write(f"[executor] CLOB client 初始化失败: {e}\n")
            self._client = None

    def get_api_creds(self) -> Optional[Dict[str, str]]:
        client = getattr(self, "_client", None)
        creds = getattr(client, "creds", None)
        if creds is None:
            return None
        api_key = str(getattr(creds, "api_key", "") or "").strip()
        api_secret = str(getattr(creds, "api_secret", "") or "").strip()
        api_passphrase = str(getattr(creds, "api_passphrase", "") or "").strip()
        if not (api_key and api_secret and api_passphrase):
            return None
        return {
            "api_key": api_key,
            "api_secret": api_secret,
            "api_passphrase": api_passphrase,
        }

    def compute_order_params(self, leader_trade: LeaderTrade, db=None, account_name: Optional[str] = None) -> Optional[OrderParams]:
        """??? copy_mode ??????????????per-leader config."""
        cfg = self.config.get_leader_config(leader_trade.leader_address)
        effective_pricing_mode = str(getattr(cfg, "pricing_mode", "aggressive") or "aggressive").strip().lower()
        aggregated = bool(getattr(leader_trade, "is_maker_like_aggregated", False))
        aggregation_kind = str(getattr(leader_trade, "aggregation_kind", "") or "")
        passive_price_mode = aggregated and (aggregation_kind != "execution_episode")
        signal_price = get_effective_signal_price(leader_trade)

        token_id = getattr(leader_trade, 'token_id', None) if hasattr(leader_trade, 'token_id') else leader_trade.get('token_id') if isinstance(leader_trade, dict) else None
        if not token_id or token_id == "":
            sys.stderr.write("[executor] ?????? token_id ?????n")
            return None

        copy_mode = str(getattr(cfg, "copy_mode", COPY_MODE_FIXED_USD) or COPY_MODE_FIXED_USD).strip().lower()
        if copy_mode == COPY_MODE_FIXED_USD:
            our_usd = cfg.fixed_usd_amount
        elif copy_mode == COPY_MODE_PROPORTIONAL:
            if leader_trade.usd_amount is None:
                return None
            our_usd = leader_trade.usd_amount * cfg.proportional_pct
            our_usd = min(our_usd, cfg.proportional_max_cap)
        else:
            our_usd = cfg.fixed_usd_amount

        if signal_price and signal_price > 0:
            our_size = our_usd / signal_price
        else:
            return None

        return OrderParams(
            token_id=leader_trade.token_id,
            side=leader_trade.side,
            price=signal_price,
            size=our_size,
            usd=our_usd,
            condition_id=leader_trade.condition_id,
            market_slug=leader_trade.market_slug,
            outcome=leader_trade.outcome,
            passive_price_mode=passive_price_mode,
            pricing_mode=effective_pricing_mode,
            hard_max_price=cfg.max_price,
            aggressive_price_chase_cap_abs=cfg.aggressive_price_chase_cap_abs,
            aggressive_price_chase_cap_bps=cfg.aggressive_price_chase_cap_bps,
        )

    def execute_order(self, params: OrderParams) -> OrderResult:
        """通过 CLOB API 下单."""
        if self._client is None:
            return OrderResult(
                success=False,
                error="CLOB client not initialized",
                error_code="client_unavailable",
                retryable=True,
            )

        # 验证 token_id 存在
        token_id = getattr(params, "token_id", None)
        if not token_id:
            return OrderResult(success=False, error="missing token_id", error_code="missing_token_id")

        try:
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

            purpose = str(getattr(params, "order_purpose", "") or "").strip().lower()
            needs_book_precheck = (
                purpose in {"auto_tp", "auto_rebuy"}
                or bool(getattr(params, "passive_price_mode", False))
                or str(getattr(params, "pricing_mode", "") or "").strip().lower() == "original"
            )
            tick_size, _min_order_size, neg_risk, orderbook_available = self.get_market_constraints(
                params.token_id,
                condition_id=params.condition_id,
                include_neg_risk=True,
                include_available=True,
                verify_orderbook=needs_book_precheck,
            )
            if orderbook_available is False:
                return OrderResult(
                    success=False,
                    error=(
                        "clob_orderbook_unavailable "
                        f"token_id={params.token_id} condition_id={params.condition_id}"
                    ),
                    error_code="orderbook_unavailable",
                    retryable=True,
                )
            if purpose in {"auto_tp", "auto_rebuy"}:
                limit_price = float(params.price or 0.0)
            elif params.passive_price_mode:
                # maker-like 聚合信号：按 leader 成交价挂单，不主动追价
                limit_price = params.price
            elif params.pricing_mode == "original":
                # 原价模式：直接使用 leader 成交价
                limit_price = params.price
            else:
                # 抢单模式（默认）：读取 tick + orderbook，使用更容易成交的限价
                limit_price = self._compute_limit_price(params.token_id, params.side, tick_size)
                if limit_price is _ORDERBOOK_UNAVAILABLE:
                    self._cache_orderbook_unavailable(
                        params.token_id,
                        condition_id=params.condition_id,
                        tick_size=tick_size,
                        min_order_size=_min_order_size,
                        neg_risk=neg_risk,
                    )
                    return OrderResult(
                        success=False,
                        error=(
                            "clob_orderbook_unavailable "
                            f"token_id={params.token_id} condition_id={params.condition_id}"
                        ),
                        error_code="orderbook_unavailable",
                        retryable=True,
                    )
                if limit_price is None:
                    limit_price = params.price
            if purpose not in {"auto_tp", "auto_rebuy"}:
                limit_price = self._apply_price_policy(params, float(limit_price))
            limit_price = self._normalize_order_price(
                float(limit_price),
                tick_size,
                side=params.side,
                purpose=purpose,
            )
            submitted_size = self._submitted_order_size(params, limit_price)
            if submitted_size <= 0:
                return OrderResult(
                    success=False,
                    error="order size became zero after budget cap",
                    error_code="zero_size",
                    limit_price=limit_price,
                    submitted_size=submitted_size,
                )
            min_order_size = self._effective_min_order_size(_min_order_size)
            if min_order_size > 0 and submitted_size + 1e-9 < min_order_size:
                return OrderResult(
                    success=False,
                    error=(
                        "clob_min_order_size "
                        f"side={params.side} purpose={purpose or 'copytrade'} "
                        f"size={submitted_size:.6f} min={min_order_size:.6f} "
                        f"token_id={params.token_id}"
                    ),
                    error_code="min_order_size",
                    limit_price=limit_price,
                    submitted_size=submitted_size,
                    min_order_size=min_order_size,
                )
            balance_error = self._preflight_balance_allowance(
                params,
                limit_price=limit_price,
                submitted_size=submitted_size,
            )
            if balance_error:
                return OrderResult(
                    success=False,
                    error=balance_error,
                    error_code="balance_allowance",
                    limit_price=limit_price,
                    submitted_size=submitted_size,
                    min_order_size=min_order_size,
                    retryable=True,
                )

            side = Side.BUY if params.side == "BUY" else Side.SELL
            requested_tif = str(getattr(params, "tif", "") or "").strip().upper()
            requested_expiration = getattr(params, "expiration_ts", None)
            if requested_tif:
                order_type = getattr(OrderType, requested_tif, None)
                if order_type is None:
                    return OrderResult(
                        success=False,
                        error=f"unsupported tif: {requested_tif}",
                        error_code="unsupported_tif",
                        limit_price=limit_price,
                        submitted_size=submitted_size,
                        min_order_size=min_order_size,
                    )
                if requested_tif == "GTC":
                    expiration = 0
                else:
                    expiration = int(requested_expiration) if requested_expiration is not None else int(time.time()) + 2 * 60 * 60
            elif purpose in {"auto_tp", "auto_rebuy"}:
                # Lot 链条单需要常驻，不使用默认 2 小时 GTD。
                expiration = 0
                order_type = OrderType.GTC
            else:
                expiration = int(requested_expiration) if requested_expiration is not None else int(time.time()) + 2 * 60 * 60
                order_type = OrderType.GTD

            order_kwargs = {
                "price": round(limit_price, 4),
                "size": submitted_size,
                "side": side,
                "token_id": params.token_id,
                "expiration": expiration,
            }
            builder_code = getattr(self, "_builder_code", None)
            if builder_code:
                order_kwargs["builder_code"] = builder_code
            order_args = OrderArgs(**order_kwargs)
            options = PartialCreateOrderOptions(
                tick_size=self._format_tick_size(tick_size),
                neg_risk=bool(neg_risk),
            )

            resp = self._create_and_post_order_with_auth_refresh(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )

            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")

            return OrderResult(
                success=True,
                order_id=str(order_id) if order_id else None,
                limit_price=limit_price,
                exchange_status="submitted",
                submitted_size=submitted_size,
                min_order_size=min_order_size,
            )

        except Exception as e:
            return OrderResult(success=False, error=str(e), error_code="exception", retryable=True)

    def _preflight_balance_allowance(
        self,
        params: OrderParams,
        *,
        limit_price: float,
        submitted_size: float,
    ) -> Optional[str]:
        client = getattr(self, "_client", None)
        getter = getattr(client, "get_balance_allowance", None)
        if client is None or not callable(getter):
            return None

        side = str(getattr(params, "side", "") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            return None

        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
        except Exception:
            return None

        if side == "SELL":
            token_id = str(getattr(params, "token_id", "") or "").strip()
            if not token_id:
                return None
            balance_params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
            required_raw = self._ceil_base_units(submitted_size)
            asset_label = f"conditional token_id={token_id}"
        else:
            balance_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            required_raw = self._ceil_base_units(max(0.0, float(limit_price or 0.0)) * submitted_size)
            asset_label = "pUSD collateral"

        if required_raw <= 0:
            return None

        try:
            row = getter(balance_params)
        except Exception as e:
            if side == "SELL":
                purpose = str(getattr(params, "order_purpose", "") or "").strip() or "copytrade"
                return (
                    "clob_balance_preflight_unavailable "
                    f"asset={asset_label} side={side} purpose={purpose} error={e}"
                )
            return None
        balance_raw = self._parse_raw_amount(self._mapping_get(row, "balance"))
        min_allowance_raw = self._min_allowance_raw(row)

        if balance_raw < required_raw or (
            min_allowance_raw is not None and min_allowance_raw < required_raw
        ):
            self._refresh_balance_allowance(balance_params)
            try:
                row = getter(balance_params)
            except Exception:
                row = None
            balance_raw = self._parse_raw_amount(self._mapping_get(row, "balance"))
            min_allowance_raw = self._min_allowance_raw(row)

        purpose = str(getattr(params, "order_purpose", "") or "").strip() or "copytrade"
        if balance_raw < required_raw:
            return (
                "insufficient_clob_balance "
                f"asset={asset_label} side={side} purpose={purpose} "
                f"balance={self._format_base_units(balance_raw)} "
                f"required={self._format_base_units(required_raw)}"
            )
        if min_allowance_raw is not None and min_allowance_raw < required_raw:
            return (
                "insufficient_clob_allowance "
                f"asset={asset_label} side={side} purpose={purpose} "
                f"allowance={self._format_base_units(min_allowance_raw)} "
                f"required={self._format_base_units(required_raw)}"
            )
        return None

    def _refresh_balance_allowance(self, params: Any) -> None:
        updater = getattr(getattr(self, "_client", None), "update_balance_allowance", None)
        if not callable(updater):
            return
        try:
            updater(params)
        except Exception:
            pass

    @staticmethod
    def _ceil_base_units(value: float) -> int:
        return int(math.ceil(max(0.0, float(value or 0.0)) * 1_000_000 - 1e-9))

    @staticmethod
    def _format_base_units(value: int) -> str:
        return f"{float(value or 0) / 1_000_000:.6f}"

    @staticmethod
    def _parse_raw_amount(value: Any) -> int:
        try:
            return max(0, int(str(value)))
        except Exception:
            return 0

    @staticmethod
    def _mapping_get(row: Any, key: str) -> Any:
        if isinstance(row, dict):
            return row.get(key)
        return getattr(row, key, None)

    def _min_allowance_raw(self, row: Any) -> Optional[int]:
        allowances = self._mapping_get(row, "allowances")
        if not isinstance(allowances, dict) or not allowances:
            return None
        values = [self._parse_raw_amount(value) for value in allowances.values()]
        return min(values) if values else None

    def cancel_order(self, order_id: str) -> bool:
        if self._client is None:
            return False
        oid = str(order_id or "").strip()
        if not oid:
            return False
        try:
            from py_clob_client_v2 import OrderPayload

            self._client.cancel_order(OrderPayload(orderID=oid))
            return True
        except Exception:
            return False

    def _create_and_post_order_with_auth_refresh(self, *, order_args: Any, options: Any, order_type: Any) -> Any:
        try:
            return self._client.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )
        except Exception as e:
            if not self._is_invalid_api_key_error(e):
                raise
            if not self._refresh_api_creds():
                raise
            sys.stderr.write("[executor] CLOB API key invalid; re-derived API credentials and retrying once\n")
            sys.stderr.flush()
            return self._client.create_and_post_order(
                order_args=order_args,
                options=options,
                order_type=order_type,
            )

    def _refresh_api_creds(self) -> bool:
        client = getattr(self, "_client", None)
        if client is None:
            return False
        setter = getattr(client, "set_api_creds", None)
        if not callable(setter):
            return False
        try:
            setter(self._obtain_api_creds(client))
            return True
        except Exception:
            return False

    def _obtain_api_creds(self, client: Any, api_creds_cls: Any = None) -> Any:
        if api_creds_cls is None:
            from py_clob_client_v2 import ApiCreds as api_creds_cls

        creds = self._request_api_creds_silent(
            client,
            endpoint="/auth/derive-api-key",
            method="GET",
            api_creds_cls=api_creds_cls,
        )
        if creds is not None:
            return creds

        creds = self._request_api_creds_silent(
            client,
            endpoint="/auth/api-key",
            method="POST",
            api_creds_cls=api_creds_cls,
        )
        if creds is not None:
            return creds

        fallback = getattr(client, "create_or_derive_api_key", None)
        if not callable(fallback):
            fallback = getattr(client, "create_or_derive_api_creds", None)
        if callable(fallback):
            return fallback()
        raise RuntimeError("failed to obtain CLOB API credentials")

    @staticmethod
    def _request_api_creds_silent(
        client: Any,
        *,
        endpoint: str,
        method: str,
        api_creds_cls: Any,
    ) -> Any:
        host = str(getattr(client, "host", "") or "").strip()
        l1_headers = getattr(client, "_l1_headers", None)
        if not host or not callable(l1_headers):
            return None

        try:
            headers = l1_headers()
            if str(method or "").upper() == "POST":
                resp = requests.post(f"{host.rstrip('/')}{endpoint}", headers=headers, timeout=10)
            else:
                resp = requests.get(f"{host.rstrip('/')}{endpoint}", headers=headers, timeout=10)
        except Exception:
            return None

        if resp.status_code >= 400:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None
        api_key = str(payload.get("apiKey") or payload.get("api_key") or "").strip()
        api_secret = str(payload.get("secret") or payload.get("api_secret") or "").strip()
        api_passphrase = str(payload.get("passphrase") or payload.get("api_passphrase") or "").strip()
        if not (api_key and api_secret and api_passphrase):
            return None
        return api_creds_cls(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    @staticmethod
    def _is_invalid_api_key_error(error: Exception) -> bool:
        text = str(error or "").lower()
        return (
            "401" in text
            or "unauthorized" in text
            or "invalid api key" in text
        )

    def _apply_price_policy(self, params: OrderParams, limit_price: float) -> float:
        """统一价格保护：超限受控追价 + 硬 max_price 上限。"""
        px = float(limit_price)
        px = self._apply_aggressive_chase_guard(params, px)
        px = self._apply_hard_price_guard(params, px)
        return max(0.0001, min(1.0, px))

    @staticmethod
    def _apply_aggressive_chase_guard(params: OrderParams, limit_price: float) -> float:
        if params.side != "BUY":
            return limit_price
        if str(getattr(params, "pricing_mode", "") or "").strip().lower() != "aggressive":
            return limit_price

        reference_price = float(getattr(params, "price", 0.0) or 0.0)
        if reference_price <= 0:
            return limit_price

        caps = []
        try:
            cap_abs = float(getattr(params, "aggressive_price_chase_cap_abs", 0.0) or 0.0)
        except (TypeError, ValueError):
            cap_abs = 0.0
        if cap_abs > 0:
            caps.append(reference_price + cap_abs)

        try:
            cap_bps = float(getattr(params, "aggressive_price_chase_cap_bps", 0.0) or 0.0)
        except (TypeError, ValueError):
            cap_bps = 0.0
        if cap_bps > 0:
            caps.append(reference_price * (1.0 + cap_bps / 10000.0))

        if not caps:
            return limit_price
        return min(limit_price, min(caps))

    def _normalize_order_price(
        self,
        limit_price: float,
        tick_size: float,
        *,
        side: str,
        purpose: str = "",
    ) -> float:
        tick = max(self._coerce_float(tick_size, default=0.01), 0.0001)
        lower = tick
        upper = max(lower, 1.0 - tick)
        clipped = min(max(float(limit_price), lower), upper)

        purpose_key = str(purpose or "").strip().lower()
        round_up = purpose_key == "auto_tp"
        steps = clipped / tick
        if round_up:
            units = math.ceil(steps - 1e-9)
        else:
            units = math.floor(steps + 1e-9)

        normalized = units * tick
        if normalized < lower:
            normalized = lower
        if normalized > upper:
            normalized = upper
        return round(normalized, 6)

    @staticmethod
    def _submitted_order_size(params: OrderParams, limit_price: float) -> float:
        raw_size = max(float(getattr(params, "size", 0.0) or 0.0), 0.0)
        if raw_size <= 0:
            return 0.0
        if params.side != "BUY" or not bool(getattr(params, "enforce_budget", True)):
            return round(raw_size, 2)

        budget = max(float(getattr(params, "usd", 0.0) or 0.0), 0.0)
        if budget <= 0 or limit_price <= 0:
            return round(raw_size, 2)

        capped_size = min(raw_size, budget / float(limit_price))
        normalized = math.floor(capped_size * 100.0 + 1e-9) / 100.0
        return round(max(normalized, 0.0), 2)

    @staticmethod
    def _effective_min_order_size(market_min_order_size: Any) -> float:
        try:
            value = float(market_min_order_size or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
        return _FALLBACK_CLOB_MIN_ORDER_SIZE

            # 两种上限都未设置时，视为不追价（最多按原价）
    @staticmethod
    def _apply_hard_price_guard(params: OrderParams, limit_price: float) -> float:
        if params.side != "BUY":
            return limit_price
        hard_max = float(params.hard_max_price or 0.0)
        if hard_max <= 0:
            return limit_price
        return min(limit_price, hard_max)

    def get_market_constraints(
        self,
        token_id: str,
        *,
        condition_id: Optional[str] = None,
        include_neg_risk: bool = False,
        include_available: bool = False,
        verify_orderbook: bool = False,
    ):
        cache = getattr(self, "_market_constraints_cache", None)
        if cache is None:
            cache = {}
            self._market_constraints_cache = cache

        key = f"{str(condition_id or '').strip().lower()}:{str(token_id or '')}"
        now = time.time()
        cached = cache.get(key)
        if cached and (now - float(cached.get("ts") or 0.0)) < 600:
            result = (
                float(cached.get("tick_size") or 0.01),
                float(cached.get("min_order_size") or 0.0),
            )
            if include_neg_risk:
                result = result + (bool(cached.get("neg_risk")),)
            if include_available:
                return result + (cached.get("available"),)
            return result

        tick_size = 0.01
        min_order_size = 0.0
        neg_risk = False
        available: Optional[bool] = None
        normalized_token = str(token_id or "").strip()
        normalized_condition = str(condition_id or "").strip().lower()
        market_info = self._get_clob_market_info(normalized_condition) if normalized_condition else None
        if isinstance(market_info, dict):
            tick_size = self._coerce_float(market_info.get("mts"), default=tick_size)
            min_order_size = self._coerce_float(market_info.get("mos"), default=min_order_size)
            neg_risk = bool(market_info.get("nr"))
            market_tokens = self._market_info_token_ids(market_info)
            if market_tokens is not None and normalized_token:
                available = normalized_token in market_tokens

        should_check_book = (
            verify_orderbook
            or not isinstance(market_info, dict)
            or (include_available and available is None)
        )
        if self._client is not None and normalized_token and available is not False and should_check_book:
            book = self._fetch_order_book_silent(normalized_token)
            if book is _ORDERBOOK_UNAVAILABLE:
                available = False
            elif book is not None:
                available = True
                tick_size = self._coerce_float(self._book_value(book, "tick_size"), default=tick_size)
                min_order_size = self._coerce_float(self._book_value(book, "min_order_size"), default=min_order_size)
                book_neg_risk = self._book_value(book, "neg_risk")
                if book_neg_risk is not None:
                    neg_risk = bool(book_neg_risk)

        cache[key] = {
            "ts": now,
            "tick_size": tick_size,
            "min_order_size": min_order_size,
            "neg_risk": neg_risk,
            "available": available,
        }
        result = (tick_size, min_order_size)
        if include_neg_risk:
            result = result + (neg_risk,)
        if include_available:
            return result + (available,)
        return result

    def _get_tick_size(self, token_id: str) -> float:
        tick_size, _ = self.get_market_constraints(token_id)
        return tick_size or 0.01

    def _get_clob_market_info(self, condition_id: str) -> Optional[Dict[str, Any]]:
        condition = str(condition_id or "").strip().lower()
        if not condition or self._client is None:
            return None
        cache = getattr(self, "_market_info_cache", None)
        if cache is None:
            cache = {}
            self._market_info_cache = cache
        now = time.time()
        cached = cache.get(condition)
        if cached and (now - float(cached.get("ts") or 0.0)) < 600:
            info = cached.get("info")
            return dict(info) if isinstance(info, dict) else None
        try:
            info = self._client.get_clob_market_info(condition)
        except Exception:
            info = None
        cache[condition] = {
            "ts": now,
            "info": dict(info) if isinstance(info, dict) else None,
        }
        return dict(info) if isinstance(info, dict) else None

    def _cache_orderbook_unavailable(
        self,
        token_id: str,
        *,
        condition_id: Optional[str],
        tick_size: float,
        min_order_size: float,
        neg_risk: bool,
    ) -> None:
        cache = getattr(self, "_market_constraints_cache", None)
        if cache is None:
            cache = {}
            self._market_constraints_cache = cache
        key = f"{str(condition_id or '').strip().lower()}:{str(token_id or '')}"
        cache[key] = {
            "ts": time.time(),
            "tick_size": tick_size,
            "min_order_size": min_order_size,
            "neg_risk": neg_risk,
            "available": False,
        }

    def _fetch_order_book_silent(self, token_id: str) -> Any:
        normalized_token = str(token_id or "").strip()
        if not normalized_token or self._client is None:
            return None

        host = str(getattr(self._client, "host", "") or "").strip()
        if not host:
            try:
                return self._client.get_order_book(normalized_token)
            except Exception as e:
                return _ORDERBOOK_UNAVAILABLE if self._is_missing_orderbook_error(e) else None

        try:
            resp = requests.get(
                f"{host.rstrip('/')}/book",
                params={"token_id": normalized_token},
                timeout=5,
            )
        except Exception:
            return None

        body_text = ""
        try:
            body_text = resp.text or ""
        except Exception:
            body_text = ""

        if resp.status_code in {400, 404}:
            if self._is_missing_orderbook_error(Exception(body_text)):
                return _ORDERBOOK_UNAVAILABLE
            return None
        if resp.status_code >= 400:
            return None

        try:
            payload = resp.json()
        except Exception:
            return None
        if isinstance(payload, dict) and payload.get("error"):
            if self._is_missing_orderbook_error(Exception(str(payload.get("error")))):
                return _ORDERBOOK_UNAVAILABLE
            return None
        return payload

    @staticmethod
    def _market_info_token_ids(market_info: Dict[str, Any]) -> Optional[set]:
        if "t" in market_info:
            tokens = market_info.get("t") or []
        elif "tokens" in market_info:
            tokens = market_info.get("tokens") or []
        else:
            return None
        out = set()
        if isinstance(tokens, list):
            for token in tokens:
                if isinstance(token, dict):
                    token_id = token.get("t") or token.get("token_id") or token.get("tokenId")
                else:
                    token_id = token
                normalized = str(token_id or "").strip()
                if normalized:
                    out.add(normalized)
        return out

    @staticmethod
    def _is_missing_orderbook_error(error: Exception) -> bool:
        text = str(error or "").lower()
        return (
            "orderbook" in text
            and (
                "does not exist" in text
                or "no orderbook exists" in text
                or "not found" in text
                or "404" in text
            )
        )

    @staticmethod
    def _format_tick_size(tick_size: float) -> str:
        tick = max(float(tick_size or 0.01), 0.0001)
        for candidate in ("0.1", "0.01", "0.001", "0.0001"):
            if abs(tick - float(candidate)) < 1e-12:
                return candidate
        if tick >= 0.1:
            return "0.1"
        if tick >= 0.01:
            return "0.01"
        if tick >= 0.001:
            return "0.001"
        return "0.0001"

    def _compute_limit_price(self, token_id: str, side: str, tick_size: float) -> Optional[float]:
        book = self._fetch_order_book_silent(token_id)
        if book is _ORDERBOOK_UNAVAILABLE:
            return _ORDERBOOK_UNAVAILABLE
        if book is None:
            return None

        if side == "BUY":
            asks = self._book_value(book, "asks") or []
            if asks:
                ask_prices = [
                    self._coerce_float(self._book_value(item, "price"), default=0.0)
                    for item in asks
                ]
                best_ask = min((price for price in ask_prices if price > 0), default=0.0)
                if best_ask > 0:
                    return best_ask + tick_size
        else:
            bids = self._book_value(book, "bids") or []
            if bids:
                bid_prices = [
                    self._coerce_float(self._book_value(item, "price"), default=0.0)
                    for item in bids
                ]
                best_bid = max((price for price in bid_prices if price > 0), default=0.0)
                if best_bid > 0:
                    return best_bid - tick_size
        return None

    @staticmethod
    def _book_value(obj: Any, key: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(key)
        return getattr(obj, key, None)

    @staticmethod
    def _coerce_float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


class DryRunExecutor(OrderExecutor):
    """模拟执行器，不实际下单."""

    def __init__(self, config: CopyTradeConfig):
        self.config = config
        self._client = None  # 不初始化真实 client

    def execute_order(self, params: OrderParams) -> OrderResult:
        # 验证 token_id 存在（支持 dataclass 和 dict）
        token_id = getattr(params, 'token_id', None) if hasattr(params, 'token_id') else params.get('token_id') if isinstance(params, dict) else None
        if not token_id or token_id == "":
            return OrderResult(success=False, error="missing token_id", error_code="missing_token_id")

        sys.stderr.write(
            f"[dry-run] {params.side} {params.size:.2f} shares @ ${params.price:.4f} "
            f"= ${params.usd:.2f} | token={token_id[:16]}... "
            f"market={params.market_slug or params.condition_id[:16]}\n"
        )
        purpose = str(getattr(params, "order_purpose", "") or "")
        if purpose in {"auto_tp", "auto_rebuy"}:
            return OrderResult(
                success=True,
                order_id=f"dry-run-{purpose}-{time.time_ns()}",
                limit_price=params.price,
                exchange_status="submitted",
                submitted_size=params.size,
            )
        return OrderResult(
            success=True,
            order_id=f"dry-run-{token_id[:8]}-{time.time_ns()}",
            filled_price=params.price,
            filled_size=params.size,
            filled_usd=params.usd,
            exchange_status="matched",
            submitted_size=params.size,
        )

    def cancel_order(self, order_id: str) -> bool:
        return True
