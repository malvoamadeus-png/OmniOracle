"""Entry point for the copytrade watchdog."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
PACKAGES_DIR = BACKEND_DIR / "packages"

for path in (PROJECT_ROOT, PACKAGES_DIR):
    text = str(path)
    if text in sys.path:
        sys.path.remove(text)
    sys.path.insert(0, text)

from copytrade.watchdog import main


if __name__ == "__main__":
    raise SystemExit(main())
