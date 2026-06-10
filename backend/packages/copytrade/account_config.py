"""多账号 TOML 配置加载 — 扫描 accounts/ 目录，合并 _defaults.toml."""

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional

from copytrade.config import CopyTradeConfig, validate_copytrade_config
from copytrade.paths import ACCOUNTS_DIR as DEFAULT_ACCOUNTS_DIR

ACCOUNTS_DIR = str(DEFAULT_ACCOUNTS_DIR)

_LEGACY_PRICE_CHASE_ALIASES = {
    "super_follow_price_chase_cap_abs": "aggressive_price_chase_cap_abs",
    "super_follow_price_chase_cap_bps": "aggressive_price_chase_cap_bps",
}


@dataclass
class AccountInfo:
    name: str               # 文件名（不含 .toml）
    env_suffix: str         # .env 后缀，如 "MAIN" → PRIVATE_KEY_MAIN
    config: CopyTradeConfig


def _load_toml(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_legacy_price_chase_aliases(data: dict) -> dict:
    if not isinstance(data, dict):
        return {}
    out = dict(data)
    for old_key, new_key in _LEGACY_PRICE_CHASE_ALIASES.items():
        if old_key in out and new_key not in out:
            out[new_key] = out[old_key]
    return out


def _merge_to_config(defaults: dict, overrides: dict) -> CopyTradeConfig:
    """合并 defaults + overrides 到 CopyTradeConfig."""
    defaults = _apply_legacy_price_chase_aliases(defaults)
    overrides = _apply_legacy_price_chase_aliases(overrides)
    merged = {**defaults, **overrides}
    # leader_overrides 是嵌套 dict，需要特殊处理（TOML 中是 [leader_overrides."0x..."]）
    defaults_leader_overrides = defaults.get("leader_overrides", {}) if isinstance(defaults, dict) else {}
    overrides_leader_overrides = overrides.get("leader_overrides", {}) if isinstance(overrides, dict) else {}
    leader_overrides = {
        str(k).lower(): _apply_legacy_price_chase_aliases(v)
        for k, v in defaults_leader_overrides.items()
        if isinstance(v, dict)
    }
    for k, v in overrides_leader_overrides.items():
        key = str(k).lower()
        if key in leader_overrides and isinstance(v, dict):
            leader_overrides[key] = {
                **leader_overrides[key],
                **_apply_legacy_price_chase_aliases(v),
            }
        elif isinstance(v, dict):
            leader_overrides[key] = _apply_legacy_price_chase_aliases(v)
    cfg = CopyTradeConfig()
    valid_fields = {f.name for f in fields(CopyTradeConfig)}
    for k, v in merged.items():
        if k in valid_fields:
            setattr(cfg, k, v)
    # 合并 leader_overrides：defaults 的作为基础，overrides 的覆盖
    if leader_overrides:
        # 确保 key 全部小写
        cfg.leader_overrides = {k.lower(): v for k, v in leader_overrides.items()}
    return cfg


def merge_to_config(defaults: dict, overrides: dict) -> CopyTradeConfig:
    return _merge_to_config(defaults, overrides)


def load_defaults(accounts_dir: str = ACCOUNTS_DIR) -> dict:
    path = os.path.join(accounts_dir, "_defaults.toml")
    if os.path.exists(path):
        defaults = _load_toml(path)
        validate_copytrade_config(_merge_to_config({}, defaults), context=path)
        return defaults
    return {}


def load_single_account(accounts_dir: str, name: str) -> AccountInfo:
    defaults = load_defaults(accounts_dir)
    path = os.path.join(accounts_dir, f"{name}.toml")
    if not os.path.exists(path):
        raise FileNotFoundError(f"账号配置不存在: {path}")
    data = _load_toml(path)
    env_suffix = data.pop("env_suffix", name.upper())
    cfg = validate_copytrade_config(_merge_to_config(defaults, data), context=path)
    return AccountInfo(name=name, env_suffix=env_suffix, config=cfg)


def load_all_accounts(accounts_dir: str = ACCOUNTS_DIR) -> List[AccountInfo]:
    if not os.path.isdir(accounts_dir):
        return []
    defaults = load_defaults(accounts_dir)
    accounts = []
    for fname in sorted(os.listdir(accounts_dir)):
        if not fname.endswith(".toml") or fname.startswith("_"):
            continue
        name = fname[:-5]  # strip .toml
        path = os.path.join(accounts_dir, fname)
        data = _load_toml(path)
        env_suffix = data.pop("env_suffix", name.upper())
        cfg = validate_copytrade_config(_merge_to_config(defaults, data), context=path)
        accounts.append(AccountInfo(name=name, env_suffix=env_suffix, config=cfg))
    return accounts


def has_accounts(accounts_dir: str = ACCOUNTS_DIR) -> bool:
    """检查是否存在任何账号配置文件."""
    if not os.path.isdir(accounts_dir):
        return False
    return any(
        f.endswith(".toml") and not f.startswith("_")
        for f in os.listdir(accounts_dir)
    )
