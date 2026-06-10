"""交互式参数配置 + JSON 持久化."""

import copy
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

from copytrade.domain import (
    ACCOUNT_LEVEL_ONLY_CONFIG_FIELDS,
    ALLOWED_COPY_MODES,
    ALLOWED_EXIT_STRATEGIES,
    ALLOWED_PRICING_MODES,
    DEPRECATED_CONFIG_FIELDS,
)

# 市场跟单限制模式常量
MARKET_LIMIT_GLOBAL_ONCE = "global_once"      # 全局一次
MARKET_LIMIT_PER_ADDRESS = "per_address_once" # 每地址一次
MARKET_LIMIT_UNLIMITED = "unlimited"          # 无限制
CRYPTO_ALLOWED_TIMEFRAMES = ("5m", "15m", "1h", "4h", "1d", "1w")
CRYPTO_ALLOWED_TIMEFRAME_SET = set(CRYPTO_ALLOWED_TIMEFRAMES)
AGGREGATION_MODE_STRICT_PRICE = "strict_price"
AGGREGATION_MODE_EXECUTION_EPISODE = "execution_episode"
AGGREGATION_MODE_SET = {
    AGGREGATION_MODE_STRICT_PRICE,
    AGGREGATION_MODE_EXECUTION_EPISODE,
}
@dataclass
class CopyTradeConfig:
    leader_addresses: List[str] = field(default_factory=list)
    copy_mode: str = "fixed_usd"            # fixed_usd | proportional
    fixed_usd_amount: float = 100.0
    proportional_pct: float = 0.10
    proportional_max_cap: float = 500.0
    min_price: float = 0.05
    max_price: float = 0.90
    min_trade_size_usd: float = 500.0
    settlement_days_max: int = 30            # 0 disables settlement-date filtering
    exit_strategy: str = "mirror_sell"       # mirror_sell | hold_to_resolution
    auto_tp_enabled: bool = False
    market_limit_mode: str = "global_once"   # token-scoped: global_once | per_address_once | unlimited
    max_entries_per_market: int = 0          # token-scoped when market_limit_mode=unlimited
    poll_interval_s: float = 15.0
    signal_source: str = "stream_hybrid"     # activity | subgraph | hybrid | stream | stream_hybrid
    signal_reconcile_interval_s: int = 60
    signal_fetch_workers: int = 4
    dry_run: bool = False
    maker_like_enabled: bool = True
    aggregation_mode: str = AGGREGATION_MODE_STRICT_PRICE
    maker_like_window_minutes: int = 360
    maker_like_max_gap_minutes: int = 30
    maker_like_score_threshold: float = 0.60
    execution_episode_window_minutes: int = 20
    execution_episode_max_gap_minutes: int = 5
    execution_episode_price_band_abs: float = 0.03
    execution_episode_price_band_bps: float = 500.0
    execution_episode_min_fill_count: int = 2
    fee_rate: float = 0.0
    pricing_mode: str = "aggressive"         # aggressive | original | passive
    aggressive_price_chase_cap_abs: float = 0.01
    aggressive_price_chase_cap_bps: float = 300.0
    crypto_only_enabled: bool = False
    crypto_allowed_timeframes: List[str] = field(default_factory=list)
    wallet_type: str = "proxy"               # proxy | eoa
    leader_overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "CopyTradeConfig":
        cfg = cls()
        # 向后兼容：转换旧的 once_per_market 布尔值
        if "once_per_market" in data and "market_limit_mode" not in data:
            cfg.market_limit_mode = MARKET_LIMIT_GLOBAL_ONCE if data["once_per_market"] else MARKET_LIMIT_UNLIMITED
        valid = {f.name for f in fields(cls)}
        for k, v in data.items():
            if k in valid:
                setattr(cfg, k, v)
        return cfg

    def get_leader_config(self, leader_address: str) -> "CopyTradeConfig":
        """返回合并了 per-leader override 的 config 副本."""
        overrides = self.leader_overrides.get(leader_address.lower(), {})
        if not overrides:
            return self
        merged = copy.copy(self)
        for k, v in overrides.items():
            if k in DEPRECATED_CONFIG_FIELDS or k in ACCOUNT_LEVEL_ONLY_CONFIG_FIELDS:
                continue
            if hasattr(merged, k):
                setattr(merged, k, v)
        return merged


def normalize_crypto_allowed_timeframes(value: Any, *, field_name: str) -> List[str]:
    if value is None:
        return []

    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = []
        for item in value:
            text = str(item or "").strip()
            if text:
                items.append(text)
    else:
        raise ValueError(f"{field_name} must be a list of timeframes")

    normalized: List[str] = []
    seen = set()
    for item in items:
        if item not in CRYPTO_ALLOWED_TIMEFRAME_SET:
            allowed = ", ".join(CRYPTO_ALLOWED_TIMEFRAMES)
            raise ValueError(f"{field_name} has invalid value '{item}' (allowed: {allowed})")
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return normalized


def normalize_aggregation_mode(value: Any, *, field_name: str) -> str:
    mode = str(value or AGGREGATION_MODE_STRICT_PRICE).strip().lower()
    if mode not in AGGREGATION_MODE_SET:
        allowed = ", ".join(sorted(AGGREGATION_MODE_SET))
        raise ValueError(f"{field_name} has invalid value '{mode}' (allowed: {allowed})")
    return mode


def validate_execution_episode_config(
    *,
    aggregation_mode: str,
    window_minutes: Any,
    max_gap_minutes: Any,
    price_band_abs: Any,
    price_band_bps: Any,
    min_fill_count: Any,
    field_prefix: str,
) -> None:
    try:
        window_value = int(window_minutes)
    except Exception:
        raise ValueError(f"{field_prefix}.execution_episode_window_minutes must be an integer")
    if window_value <= 0:
        raise ValueError(f"{field_prefix}.execution_episode_window_minutes must be > 0")

    try:
        max_gap_value = int(max_gap_minutes)
    except Exception:
        raise ValueError(f"{field_prefix}.execution_episode_max_gap_minutes must be an integer")
    if max_gap_value <= 0:
        raise ValueError(f"{field_prefix}.execution_episode_max_gap_minutes must be > 0")

    try:
        abs_band_value = float(price_band_abs)
    except Exception:
        raise ValueError(f"{field_prefix}.execution_episode_price_band_abs must be a number")

    try:
        bps_band_value = float(price_band_bps)
    except Exception:
        raise ValueError(f"{field_prefix}.execution_episode_price_band_bps must be a number")

    try:
        min_fill_count_value = int(min_fill_count)
    except Exception:
        raise ValueError(f"{field_prefix}.execution_episode_min_fill_count must be an integer")
    if min_fill_count_value <= 0:
        raise ValueError(f"{field_prefix}.execution_episode_min_fill_count must be > 0")

    if (
        aggregation_mode == AGGREGATION_MODE_EXECUTION_EPISODE
        and abs_band_value <= 0
        and bps_band_value <= 0
    ):
        raise ValueError(
            f"{field_prefix} must set execution_episode_price_band_abs > 0 or execution_episode_price_band_bps > 0 "
            f"when aggregation_mode={AGGREGATION_MODE_EXECUTION_EPISODE}"
        )


def validate_aggressive_price_chase_config(
    *,
    cap_abs: Any,
    cap_bps: Any,
    field_prefix: str,
) -> None:
    try:
        abs_value = float(cap_abs)
    except Exception:
        raise ValueError(f"{field_prefix}.aggressive_price_chase_cap_abs must be a number")
    if abs_value < 0:
        raise ValueError(f"{field_prefix}.aggressive_price_chase_cap_abs must be >= 0")

    try:
        bps_value = float(cap_bps)
    except Exception:
        raise ValueError(f"{field_prefix}.aggressive_price_chase_cap_bps must be a number")
    if bps_value < 0:
        raise ValueError(f"{field_prefix}.aggressive_price_chase_cap_bps must be >= 0")


def validate_copytrade_config(
    cfg: CopyTradeConfig,
    *,
    context: str = "config",
) -> CopyTradeConfig:
    cfg.copy_mode = str(getattr(cfg, "copy_mode", "fixed_usd") or "fixed_usd").strip().lower()
    if cfg.copy_mode not in ALLOWED_COPY_MODES:
        allowed = ", ".join(sorted(ALLOWED_COPY_MODES))
        raise ValueError(f"{context}.copy_mode has invalid value '{cfg.copy_mode}' (allowed: {allowed})")

    cfg.pricing_mode = str(getattr(cfg, "pricing_mode", "aggressive") or "aggressive").strip().lower()
    if cfg.pricing_mode not in ALLOWED_PRICING_MODES:
        allowed = ", ".join(sorted(ALLOWED_PRICING_MODES))
        raise ValueError(f"{context}.pricing_mode has invalid value '{cfg.pricing_mode}' (allowed: {allowed})")

    cfg.exit_strategy = str(getattr(cfg, "exit_strategy", "mirror_sell") or "mirror_sell").strip().lower()
    if cfg.exit_strategy not in ALLOWED_EXIT_STRATEGIES:
        allowed = ", ".join(sorted(ALLOWED_EXIT_STRATEGIES))
        raise ValueError(f"{context}.exit_strategy has invalid value '{cfg.exit_strategy}' (allowed: {allowed})")

    cfg.aggregation_mode = normalize_aggregation_mode(
        getattr(cfg, "aggregation_mode", AGGREGATION_MODE_STRICT_PRICE),
        field_name=f"{context}.aggregation_mode",
    )
    validate_execution_episode_config(
        aggregation_mode=cfg.aggregation_mode,
        window_minutes=getattr(cfg, "execution_episode_window_minutes", 20),
        max_gap_minutes=getattr(cfg, "execution_episode_max_gap_minutes", 5),
        price_band_abs=getattr(cfg, "execution_episode_price_band_abs", 0.03),
        price_band_bps=getattr(cfg, "execution_episode_price_band_bps", 500.0),
        min_fill_count=getattr(cfg, "execution_episode_min_fill_count", 2),
        field_prefix=context,
    )
    validate_aggressive_price_chase_config(
        cap_abs=getattr(cfg, "aggressive_price_chase_cap_abs", 0.01),
        cap_bps=getattr(cfg, "aggressive_price_chase_cap_bps", 300.0),
        field_prefix=context,
    )
    cfg.crypto_allowed_timeframes = normalize_crypto_allowed_timeframes(
        getattr(cfg, "crypto_allowed_timeframes", []),
        field_name=f"{context}.crypto_allowed_timeframes",
    )
    if getattr(cfg, "crypto_only_enabled", False) and not cfg.crypto_allowed_timeframes:
        raise ValueError(
            f"{context}.crypto_allowed_timeframes must not be empty when crypto_only_enabled=true"
        )
    if getattr(cfg, "auto_tp_enabled", False) and str(getattr(cfg, "exit_strategy", "") or "") != "mirror_sell":
        raise ValueError(
            f"{context}.auto_tp_enabled requires exit_strategy=mirror_sell"
        )

    normalized_overrides: Dict[str, Dict[str, Any]] = {}
    for leader_address, raw_override in (cfg.leader_overrides or {}).items():
        if not isinstance(raw_override, dict):
            raise ValueError(f"{context}.leader_overrides[{leader_address}] must be an object")

        addr = str(leader_address or "").strip().lower()
        if not addr:
            raise ValueError(f"{context}.leader_overrides contains an empty leader address")

        override = dict(raw_override)
        if "copy_mode" in override:
            override["copy_mode"] = str(override.get("copy_mode") or "").strip().lower()
            if override["copy_mode"] not in ALLOWED_COPY_MODES:
                allowed = ", ".join(sorted(ALLOWED_COPY_MODES))
                raise ValueError(
                    f"{context}.leader_overrides[{addr}].copy_mode has invalid value "
                    f"'{override['copy_mode']}' (allowed: {allowed})"
                )
        if "pricing_mode" in override:
            override["pricing_mode"] = str(override.get("pricing_mode") or "").strip().lower()
            if override["pricing_mode"] not in ALLOWED_PRICING_MODES:
                allowed = ", ".join(sorted(ALLOWED_PRICING_MODES))
                raise ValueError(
                    f"{context}.leader_overrides[{addr}].pricing_mode has invalid value "
                    f"'{override['pricing_mode']}' (allowed: {allowed})"
                )
        if "exit_strategy" in override:
            override["exit_strategy"] = str(override.get("exit_strategy") or "").strip().lower()
            if override["exit_strategy"] not in ALLOWED_EXIT_STRATEGIES:
                allowed = ", ".join(sorted(ALLOWED_EXIT_STRATEGIES))
                raise ValueError(
                    f"{context}.leader_overrides[{addr}].exit_strategy has invalid value "
                    f"'{override['exit_strategy']}' (allowed: {allowed})"
                )

        for deprecated_key in DEPRECATED_CONFIG_FIELDS | ACCOUNT_LEVEL_ONLY_CONFIG_FIELDS:
            override.pop(deprecated_key, None)

        if "aggregation_mode" in override:
            override["aggregation_mode"] = normalize_aggregation_mode(
                override.get("aggregation_mode"),
                field_name=f"{context}.leader_overrides[{addr}].aggregation_mode",
            )
        if "crypto_allowed_timeframes" in override:
            override["crypto_allowed_timeframes"] = normalize_crypto_allowed_timeframes(
                override.get("crypto_allowed_timeframes"),
                field_name=f"{context}.leader_overrides[{addr}].crypto_allowed_timeframes",
            )

        effective_enabled = (
            override["crypto_only_enabled"]
            if "crypto_only_enabled" in override
            else cfg.crypto_only_enabled
        )
        effective_timeframes = (
            override["crypto_allowed_timeframes"]
            if "crypto_allowed_timeframes" in override
            else cfg.crypto_allowed_timeframes
        )
        if effective_enabled and not effective_timeframes:
            raise ValueError(
                f"{context}.leader_overrides[{addr}] enables crypto_only but has no effective allowed timeframes"
            )

        effective_aggregation_mode = (
            override["aggregation_mode"]
            if "aggregation_mode" in override
            else cfg.aggregation_mode
        )
        effective_exit_strategy = (
            override["exit_strategy"]
            if "exit_strategy" in override
            else cfg.exit_strategy
        )
        effective_auto_tp_enabled = cfg.auto_tp_enabled
        if effective_auto_tp_enabled and str(effective_exit_strategy or "") != "mirror_sell":
            raise ValueError(
                f"{context}.leader_overrides[{addr}].auto_tp_enabled requires effective exit_strategy=mirror_sell"
            )
        validate_execution_episode_config(
            aggregation_mode=effective_aggregation_mode,
            window_minutes=override.get("execution_episode_window_minutes", cfg.execution_episode_window_minutes),
            max_gap_minutes=override.get("execution_episode_max_gap_minutes", cfg.execution_episode_max_gap_minutes),
            price_band_abs=override.get("execution_episode_price_band_abs", cfg.execution_episode_price_band_abs),
            price_band_bps=override.get("execution_episode_price_band_bps", cfg.execution_episode_price_band_bps),
            min_fill_count=override.get("execution_episode_min_fill_count", cfg.execution_episode_min_fill_count),
            field_prefix=f"{context}.leader_overrides[{addr}]",
        )
        validate_aggressive_price_chase_config(
            cap_abs=override.get("aggressive_price_chase_cap_abs", cfg.aggressive_price_chase_cap_abs),
            cap_bps=override.get("aggressive_price_chase_cap_bps", cfg.aggressive_price_chase_cap_bps),
            field_prefix=f"{context}.leader_overrides[{addr}]",
        )

        normalized_overrides[addr] = override

    cfg.leader_overrides = normalized_overrides
    return cfg


def _input_prompt(prompt: str, default: str) -> str:
    sys.stdout.write(f"  {prompt} [{default}]: ")
    sys.stdout.flush()
    val = input().strip()
    return val if val else default


def _input_choice(prompt: str, options: List[str], default: str) -> str:
    sys.stdout.write(f"\n  {prompt}\n")
    for i, opt in enumerate(options, 1):
        marker = " *" if opt == default else ""
        sys.stdout.write(f"    {i}. {opt}{marker}\n")
    sys.stdout.write(f"  选择 [默认={options.index(default)+1}]: ")
    sys.stdout.flush()
    val = input().strip()
    if not val:
        return default
    try:
        idx = int(val) - 1
        if 0 <= idx < len(options):
            return options[idx]
    except ValueError:
        if val in options:
            return val
    return default


def interactive_setup(existing: Optional[CopyTradeConfig] = None) -> CopyTradeConfig:
    """逐项提示用户输入参数，回车采用默认值."""
    cfg = existing or CopyTradeConfig()

    sys.stdout.write("\n=== Polymarket 跟单系统配置 ===\n")

    # --- 钱包类型 ---
    sys.stdout.write("\n【钱包类型】\n")
    cfg.wallet_type = _input_choice(
        "钱包类型:", ["proxy", "eoa"], cfg.wallet_type
    )

    # --- 跟单地址 ---
    sys.stdout.write("\n【跟单地址】\n")
    default_addrs = ",".join(cfg.leader_addresses) if cfg.leader_addresses else ""
    addrs_str = _input_prompt("高手地址 (逗号分隔)", default_addrs or "无")
    if addrs_str and addrs_str != "无":
        cfg.leader_addresses = [a.strip().lower() for a in addrs_str.split(",") if a.strip()]

    # --- 跟单模式 ---
    sys.stdout.write("\n【跟单模式】\n")
    cfg.copy_mode = _input_choice(
        "跟单模式:", ["fixed_usd", "proportional"], cfg.copy_mode
    )
    if cfg.copy_mode == "fixed_usd":
        cfg.fixed_usd_amount = float(_input_prompt("固定金额 (USD)", str(cfg.fixed_usd_amount)))
    elif cfg.copy_mode == "proportional":
        cfg.proportional_pct = float(_input_prompt("比例 (0.10=10%)", str(cfg.proportional_pct)))
        cfg.proportional_max_cap = float(_input_prompt("单笔上限 (USD)", str(cfg.proportional_max_cap)))

    # --- 价格控制 ---
    sys.stdout.write("\n【价格控制】\n")
    cfg.min_price = float(_input_prompt("最低价限制", str(cfg.min_price)))
    cfg.max_price = float(_input_prompt("最高价限制", str(cfg.max_price)))
    cfg.min_trade_size_usd = float(_input_prompt("最小交易规模 (USD)", str(cfg.min_trade_size_usd)))
    cfg.settlement_days_max = int(_input_prompt("结算日期限制 (天, 0=不过滤)", str(cfg.settlement_days_max)))

    # --- 风控参数 ---
    sys.stdout.write("\n【风控参数】\n")

    # --- 离场策略 ---
    sys.stdout.write("\n【离场策略】\n")
    cfg.exit_strategy = _input_choice(
        "离场策略:", ["mirror_sell", "hold_to_resolution"], cfg.exit_strategy
    )
    cfg.auto_tp_enabled = _input_prompt(
        "自动止盈链条 (y/n)",
        "y" if cfg.auto_tp_enabled else "n",
    ).lower() in ("y", "yes", "1", "true")

    # --- 跟单频率 ---
    sys.stdout.write("\n【跟单频率】\n")
    cfg.market_limit_mode = _input_choice(
        "同一market跟单限制:",
        ["global_once", "per_address_once", "unlimited"],
        cfg.market_limit_mode
    )

    if cfg.market_limit_mode == MARKET_LIMIT_UNLIMITED:
        sys.stdout.write("\n【无限制模式 - token 进入次数】\n")
        cfg.max_entries_per_market = int(_input_prompt("同 token 最大进入次数 (0=不限)", str(cfg.max_entries_per_market)))

    # --- 轮询间隔 ---
    sys.stdout.write("\n【轮询设置】\n")
    cfg.poll_interval_s = float(_input_prompt("轮询间隔 (秒)", str(cfg.poll_interval_s)))
    cfg.signal_source = _input_choice(
        "leader 信号源:", ["stream_hybrid", "stream", "hybrid", "activity", "subgraph"], cfg.signal_source
    )
    cfg.signal_reconcile_interval_s = int(
        _input_prompt("stream 补漏间隔 (秒)", str(cfg.signal_reconcile_interval_s))
    )
    cfg.signal_fetch_workers = int(_input_prompt("信号抓取并发数", str(cfg.signal_fetch_workers)))

    # --- 挂单碎成交识别 ---
    sys.stdout.write("\n【碎成交聚合（maker-like）】\n")
    maker_like_on = _input_prompt("启用碎成交聚合? (y/n)", "y" if cfg.maker_like_enabled else "n").lower()
    cfg.maker_like_enabled = maker_like_on in ("y", "yes", "1", "true")
    if cfg.maker_like_enabled:
        cfg.maker_like_window_minutes = int(_input_prompt("聚合窗口 (分钟)", str(cfg.maker_like_window_minutes)))
        cfg.maker_like_max_gap_minutes = int(_input_prompt("连续成交最大间隔 (分钟)", str(cfg.maker_like_max_gap_minutes)))
        cfg.maker_like_score_threshold = float(_input_prompt("maker_like_score_threshold (0-1)", str(cfg.maker_like_score_threshold)))

    sys.stdout.write("\n配置完成!\n")
    return cfg


def save_config(cfg: CopyTradeConfig, path: str = "copytrade_config.json") -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
    sys.stderr.write(f"配置已保存到 {path}\n")


def load_config(path: str = "copytrade_config.json") -> Optional[CopyTradeConfig]:
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return validate_copytrade_config(CopyTradeConfig.from_dict(data), context=path)
