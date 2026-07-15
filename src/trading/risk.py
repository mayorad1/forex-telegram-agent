"""Position sizing and risk guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RiskDecision:
    allowed: bool
    lots: float
    reason: str


def pip_size(pair: str) -> float:
    p = pair.upper()
    if p.endswith("JPY"):
        return 0.01
    if p.startswith("XAU") or p.startswith("XAG"):
        return 0.1
    return 0.0001


def position_size(
    equity: float,
    entry: float,
    stop_loss: float,
    pair: str,
    risk_pct: float = 1.0,
    min_lot: float = 0.01,
    max_lot: float = 1.0,
) -> float:
    """Approximate lot size from % risk (simplified FX model)."""
    if equity <= 0 or entry <= 0 or stop_loss <= 0:
        return 0.0
    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0.0

    # Standard lot ≈ 100_000 units; P/L ≈ move * units
    # For metals/JPY this is approximate — paper mode only.
    units = risk_amount / stop_distance
    lots = units / 100_000.0
    lots = max(min_lot, min(max_lot, round(lots, 2)))
    return lots


def check_trade(
    *,
    equity: float,
    open_positions: int,
    daily_pnl_pct: float,
    entry: float,
    stop_loss: float,
    pair: str,
    risk_cfg: dict[str, Any],
) -> RiskDecision:
    max_open = int(risk_cfg.get("max_open_positions", 3))
    max_daily = float(risk_cfg.get("max_daily_loss_pct", 5.0))
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0))
    min_lot = float(risk_cfg.get("min_lot_size", 0.01))
    max_lot = float(risk_cfg.get("max_lot_size", 1.0))

    if open_positions >= max_open:
        return RiskDecision(False, 0.0, f"Max open positions ({max_open}) reached")
    if daily_pnl_pct <= -abs(max_daily):
        return RiskDecision(False, 0.0, f"Daily loss limit ({max_daily}%) hit")

    lots = position_size(
        equity, entry, stop_loss, pair, risk_pct=risk_pct, min_lot=min_lot, max_lot=max_lot
    )
    if lots <= 0:
        return RiskDecision(False, 0.0, "Could not size position")
    return RiskDecision(True, lots, "OK")
