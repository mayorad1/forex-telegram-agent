"""Load environment and YAML settings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / "runtime"
CONFIG_PATH = ROOT / "config" / "settings.yaml"


def load_env() -> None:
    load_dotenv(ROOT / ".env")


def get_settings() -> dict[str, Any]:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def env_str(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def env_int(key: str, default: int = 0) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def env_float(key: str, default: float = 0.0) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def allowed_user_ids() -> set[int]:
    raw = env_str("TELEGRAM_ALLOWED_USERS", "")
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


def trading_mode() -> str:
    # Paper mode removed — always MT5 / Exness
    return "mt5"
