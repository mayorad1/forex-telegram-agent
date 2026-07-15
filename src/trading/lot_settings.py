"""Persist user lot-size preference (Telegram-controlled)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.utils.config import RUNTIME_DIR

LOT_FILE = RUNTIME_DIR / "lot_settings.json"


def load_fixed_lot() -> Optional[float]:
    if not LOT_FILE.exists():
        return None
    try:
        data = json.loads(LOT_FILE.read_text(encoding="utf-8"))
        v = data.get("fixed_lot_size")
        if v is None:
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


def save_fixed_lot(lots: Optional[float]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"fixed_lot_size": lots}
    LOT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def apply_lot_to_risk_cfg(risk_cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge saved Telegram lot into risk config (mutates and returns)."""
    saved = load_fixed_lot()
    if saved is not None and saved > 0:
        risk_cfg["fixed_lot_size"] = saved
    return risk_cfg
