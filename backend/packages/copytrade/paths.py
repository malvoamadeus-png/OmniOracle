"""Centralized filesystem paths for the copytrade package."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PACKAGES_DIR = PACKAGE_DIR.parent
BACKEND_DIR = PACKAGES_DIR.parent
PROJECT_ROOT = BACKEND_DIR.parent

ACCOUNTS_DIR = PACKAGE_DIR / "accounts"
WEB_DIR = PACKAGE_DIR / "web"
FRONTEND_DIR = PROJECT_ROOT / "frontend"
ADMIN_FRONTEND_DIR = FRONTEND_DIR / "admin"
DOTENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_CONFIG_PATH = PACKAGE_DIR / "copytrade_config.json"
DEFAULT_DB_PATH = PACKAGE_DIR / "copytrade.sqlite"
WATCHDOG_LOG_PATH = PACKAGE_DIR / "watchdog.log"
SUPABASE_SYNC_SCRIPT = PROJECT_ROOT / "supabase" / "sync_to_supabase.py"
ROOT_METRICS_DB_PATH = PROJECT_ROOT / "metrics_fresh.sqlite"


def ensure_import_paths() -> None:
    """Make both the package root and project root importable."""
    for path in (PROJECT_ROOT, PACKAGES_DIR):
        text = str(path)
        if text in sys.path:
            sys.path.remove(text)
        sys.path.insert(0, text)
