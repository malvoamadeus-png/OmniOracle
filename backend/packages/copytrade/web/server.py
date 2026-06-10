"""本地 Web 管理界面 — 管理多账号 TOML 配置和 .env 凭证.

启动: python -m copytrade.web.server
访问: http://127.0.0.1:8199
"""

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomllib
import tomli_w
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

SCRIPT_DIR = Path(__file__).resolve().parent
_PACKAGE_DIR = SCRIPT_DIR.parent
_PACKAGES_DIR = _PACKAGE_DIR.parent
if str(_PACKAGES_DIR) not in sys.path:
    sys.path.insert(0, str(_PACKAGES_DIR))

from copytrade.account_config import load_defaults, merge_to_config
from copytrade.config import validate_copytrade_config
from copytrade.db import CopyTradeDB
from copytrade.domain import DEPRECATED_CONFIG_FIELDS
from copytrade.paths import (
    ACCOUNTS_DIR as DEFAULT_ACCOUNTS_DIR,
    ADMIN_FRONTEND_DIR,
    DEFAULT_DB_PATH,
    DOTENV_PATH as ROOT_DOTENV_PATH,
    PACKAGE_DIR,
    WEB_DIR,
    ensure_import_paths,
)

ensure_import_paths()

SCRIPT_DIR = ADMIN_FRONTEND_DIR
COPYTRADE_DIR = PACKAGE_DIR
ACCOUNTS_DIR = DEFAULT_ACCOUNTS_DIR
DB_PATH = Path(os.getenv("COPYTRADE_DB_PATH") or os.getenv("COPYTRADE_DB") or DEFAULT_DB_PATH)
DOTENV_PATH = ROOT_DOTENV_PATH
ENV_BASE_KEYS = (
    "PRIVATE_KEY",
    "FUNDER_ADDRESS",
    "CLOB_API_KEY",
    "CLOB_SECRET",
    "CLOB_PASS_PHRASE",
    "RELAYER_API_KEY",
)

app = FastAPI(title="Copytrade 管理")

# ── .env 读写工具 ──────────────────────────────────────────────

RESTART_REQUIRED_KEYS = {
    "leader_addresses",
    "signal_source",
    "signal_reconcile_interval_s",
    "signal_fetch_workers",
    "env_suffix",
    "wallet_type",
}


def _sanitize_config_body(body: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(body or {})
    for key in DEPRECATED_CONFIG_FIELDS:
        data.pop(key, None)
    overrides = data.get("leader_overrides")
    if isinstance(overrides, dict):
        cleaned = {}
        for leader, override in overrides.items():
            if not isinstance(override, dict):
                cleaned[leader] = override
                continue
            next_override = dict(override)
            for key in DEPRECATED_CONFIG_FIELDS | {"auto_tp_enabled"}:
                next_override.pop(key, None)
            if next_override:
                cleaned[leader] = next_override
        data["leader_overrides"] = cleaned
    return data


def _changed_keys(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    keys = set((before or {}).keys()) | set((after or {}).keys())
    return sorted(key for key in keys if (before or {}).get(key) != (after or {}).get(key))


def _audit_config(
    *,
    action: str,
    target: str,
    account_name: Optional[str] = None,
    restart_required: bool = False,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        db = CopyTradeDB(str(DB_PATH))
        try:
            db.record_config_audit(
                action=action,
                target=target,
                account_name=account_name,
                restart_required=restart_required,
                details=details or {},
            )
        finally:
            db.close()
    except Exception:
        pass


def _read_dotenv() -> Dict[str, str]:
    if not DOTENV_PATH.exists():
        return {}
    result = {}
    for raw in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _write_dotenv_key(key: str, value: str) -> None:
    """写入或更新 .env 中的一个 key，保留其他内容."""
    lines = []
    found = False
    if DOTENV_PATH.exists():
        for raw in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.split("=", 1)[0].strip()
                if k == key:
                    lines.append(f"{key}={value}")
                    found = True
                    continue
            lines.append(raw)
    if not found:
        lines.append(f"{key}={value}")
    DOTENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _delete_dotenv_keys(prefix: str) -> None:
    """删除 .env 中所有以 prefix 开头的 key."""
    if not DOTENV_PATH.exists():
        return
    lines = []
    for raw in DOTENV_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k.startswith(prefix):
                continue
        lines.append(raw)
    DOTENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask(val: str) -> str:
    """脱敏显示: 前6位 + ... + 后4位."""
    if len(val) <= 12:
        return "***"
    return val[:6] + "..." + val[-4:]

# ── TOML 工具 ──────────────────────────────────────────────────

def _read_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _write_toml(path: Path, data: dict) -> None:
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


def _http_400(message: str) -> None:
    raise HTTPException(400, message)


def _validate_account_payload(name: str, body: Dict[str, Any]) -> None:
    body = _sanitize_config_body(body)
    defaults = load_defaults(str(ACCOUNTS_DIR))
    try:
        validate_copytrade_config(
            merge_to_config(defaults, body),
            context=f"accounts/{name}.toml",
        )
    except Exception as e:
        _http_400(str(e))


def _validate_defaults_payload(body: Dict[str, Any]) -> None:
    body = _sanitize_config_body(body)
    try:
        validate_copytrade_config(
            merge_to_config({}, body),
            context="accounts/_defaults.toml",
        )
    except Exception as e:
        _http_400(str(e))

    for path in sorted(ACCOUNTS_DIR.glob("*.toml")):
        if path.name.startswith("_"):
            continue
        account_body = _read_toml(path)
        account_body.pop("env_suffix", None)
        try:
            validate_copytrade_config(
                merge_to_config(body, account_body),
                context=str(path),
            )
        except Exception as e:
            _http_400(str(e))

# ── API 端点 ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = SCRIPT_DIR / "index.html"
    if not html_path.exists():
        html_path = WEB_DIR / "index.html"
    if not html_path.exists():
        raise HTTPException(500, f"Admin frontend not found: {SCRIPT_DIR / 'index.html'}")
    return html_path.read_text(encoding="utf-8")


@app.get("/api/accounts")
async def list_accounts():
    ACCOUNTS_DIR.mkdir(exist_ok=True)
    env = _read_dotenv()
    accounts = []
    for f in sorted(ACCOUNTS_DIR.iterdir()):
        if not f.suffix == ".toml" or f.name.startswith("_"):
            continue
        name = f.stem
        data = _sanitize_config_body(_read_toml(f))
        suffix = data.get("env_suffix", name.upper())
        wallet_type = data.get("wallet_type", "proxy")
        has_pk = bool(env.get(f"PRIVATE_KEY_{suffix}"))
        has_funder = bool(env.get(f"FUNDER_ADDRESS_{suffix}"))
        has_relayer_key = bool(env.get(f"RELAYER_API_KEY_{suffix}"))
        # EOA 需要私钥(+可选 relayer key + safe地址); Proxy 需要私钥 + funder
        has_creds = has_pk if wallet_type == "eoa" else (has_pk and has_funder)
        accounts.append({
            "name": name,
            "env_suffix": suffix,
            "config": data,
            "has_credentials": has_creds,
        })
    return accounts


@app.get("/api/accounts/{name}")
async def get_account(name: str):
    path = ACCOUNTS_DIR / f"{name}.toml"
    if not path.exists():
        raise HTTPException(404, f"账号 {name} 不存在")
    return _sanitize_config_body(_read_toml(path))


@app.put("/api/accounts/{name}")
async def save_account(name: str, body: Dict[str, Any]):
    if name.startswith("_"):
        raise HTTPException(400, "账号名不能以 _ 开头")
    ACCOUNTS_DIR.mkdir(exist_ok=True)
    body = _sanitize_config_body(body)
    _validate_account_payload(name, body)
    path = ACCOUNTS_DIR / f"{name}.toml"
    old_body = _sanitize_config_body(_read_toml(path)) if path.exists() else {}
    _write_toml(path, body)
    changed = _changed_keys(old_body, body)
    restart_required = bool(set(changed) & RESTART_REQUIRED_KEYS)
    _audit_config(
        action="save_account",
        target=f"accounts/{name}.toml",
        account_name=name,
        restart_required=restart_required,
        details={"changed_keys": changed},
    )
    return {"ok": True, "name": name, "restart_required": restart_required}


@app.delete("/api/accounts/{name}")
async def delete_account(name: str):
    path = ACCOUNTS_DIR / f"{name}.toml"
    if not path.exists():
        raise HTTPException(404, f"账号 {name} 不存在")
    data = _sanitize_config_body(_read_toml(path))
    suffix = data.get("env_suffix", name.upper())
    path.unlink()
    for base_key in ENV_BASE_KEYS:
        _delete_dotenv_keys(f"{base_key}_{suffix}")
    _audit_config(
        action="delete_account",
        target=f"accounts/{name}.toml",
        account_name=name,
        restart_required=True,
        details={"env_suffix": suffix, "deleted_env_keys": [f"{base}_{suffix}" for base in ENV_BASE_KEYS]},
    )
    return {"ok": True, "deleted": name, "env_suffix": suffix}


@app.get("/api/defaults")
async def get_defaults():
    path = ACCOUNTS_DIR / "_defaults.toml"
    if not path.exists():
        return {}
    return _sanitize_config_body(_read_toml(path))


@app.put("/api/defaults")
async def save_defaults(body: Dict[str, Any]):
    ACCOUNTS_DIR.mkdir(exist_ok=True)
    body = _sanitize_config_body(body)
    _validate_defaults_payload(body)
    path = ACCOUNTS_DIR / "_defaults.toml"
    old_body = _sanitize_config_body(_read_toml(path)) if path.exists() else {}
    _write_toml(path, body)
    changed = _changed_keys(old_body, body)
    restart_required = bool(set(changed) & RESTART_REQUIRED_KEYS)
    _audit_config(
        action="save_defaults",
        target="accounts/_defaults.toml",
        restart_required=restart_required,
        details={"changed_keys": changed},
    )
    return {"ok": True, "restart_required": restart_required}


@app.get("/api/env/{suffix}")
async def get_env(suffix: str):
    suffix = suffix.upper()
    env = _read_dotenv()
    result = {}
    for k in ENV_BASE_KEYS:
        full = f"{k}_{suffix}"
        val = env.get(full, "")
        result[k] = {"set": bool(val), "masked": _mask(val) if val else ""}
    return result


@app.put("/api/env/{suffix}")
async def save_env(suffix: str, body: Dict[str, str]):
    suffix = suffix.upper()
    updated_keys = []
    for base_key, value in body.items():
        if base_key not in ENV_BASE_KEYS:
            continue
        if not str(value or "").strip():
            continue
        full_key = f"{base_key}_{suffix}"
        _write_dotenv_key(full_key, value.strip())
        updated_keys.append(full_key)
    if updated_keys:
        _audit_config(
            action="save_env",
            target=f".env:{suffix}",
            restart_required=True,
            details={"updated_keys": updated_keys, "values": "masked"},
        )
    return {"ok": True, "suffix": suffix}


@app.get("/api/runtime-status")
async def runtime_status():
    try:
        db = CopyTradeDB(str(DB_PATH))
        try:
            return db.get_runtime_status(limit_events=100)
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(500, str(e))


def main():
    print("Copytrade 管理界面: http://127.0.0.1:8199")
    uvicorn.run(app, host="127.0.0.1", port=8199, log_level="warning")


if __name__ == "__main__":
    main()
