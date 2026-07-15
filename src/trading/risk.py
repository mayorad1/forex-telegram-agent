"""Position sizing and risk guards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


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
    max_lot: Optional[float] = None,
    fixed_lot: Optional[float] = None,
) -> float:
    """Lot size: fixed_lot if set, else % risk model."""
    if fixed_lot is not None and fixed_lot > 0:
        lots = round(float(fixed_lot), 2)
        return max(min_lot, lots)

    if equity <= 0 or entry <= 0 or stop_loss <= 0:
        return 0.0
    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return 0.0

    units = risk_amount / stop_distance
    lots = units / 100_000.0
    lots = max(min_lot, round(lots, 2))
    if max_lot is not None and max_lot > 0:
        lots = min(max_lot, lots)
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
    # 0 or negative = unlimited open positions
    max_open = int(risk_cfg.get("max_open_positions", 0))
    max_daily = float(risk_cfg.get("max_daily_loss_pct", 0) or 0)
    risk_pct = float(risk_cfg.get("risk_per_trade_pct", 1.0))
    min_lot = float(risk_cfg.get("min_lot_size", 0.01))
    raw_max = risk_cfg.get("max_lot_size", None)
    max_lot = float(raw_max) if raw_max not in (None, "", 0, "0") else None
    fixed = risk_cfg.get("fixed_lot_size", None)
    fixed_lot = float(fixed) if fixed not in (None, "", 0, "0") else None

    if max_open > 0 and open_positions >= max_open:
        return RiskDecision(False, 0.0, f"Max open positions ({max_open}) reached")
    if max_daily > 0 and daily_pnl_pct <= -abs(max_daily):
        return RiskDecision(False, 0.0, f"Daily loss limit ({max_daily}%) hit")

    lots = position_size(
        equity,
        entry,
        stop_loss,
        pair,
        risk_pct=risk_pct,
        min_lot=min_lot,
        max_lot=max_lot,
        fixed_lot=fixed_lot,
    )
    if lots <= 0:
        return RiskDecision(False, 0.0, "Could not size position")
    return RiskDecision(True, lots, "OK")
