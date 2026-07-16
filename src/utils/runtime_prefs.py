"""Persist user prefs changed from Telegram (interval, mode, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.utils.config import RUNTIME_DIR

PREFS_FILE = RUNTIME_DIR / "user_prefs.json"


def load_prefs() -> dict[str, Any]:
    if not PREFS_FILE.exists():
        return {}
    try:
        return json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save_prefs(prefs: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_prefs()
    existing.update(prefs)
    PREFS_FILE.write_text(json.dumps(existing, indent=2), encoding="utf-8")


def get_scan_interval_minutes(default: int = 15) -> int:
    prefs = load_prefs()
    try:
        m = int(prefs.get("scan_interval_minutes", default))
        return max(1, min(1440, m))  # 1 min .. 24h
    except (TypeError, ValueError):
        return default


def set_scan_interval_minutes(minutes: int) -> int:
    m = max(1, min(1440, int(minutes)))
    save_prefs({"scan_interval_minutes": m})
    return m
