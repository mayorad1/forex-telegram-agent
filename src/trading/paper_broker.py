"""Simple paper trading ledger with JSON persistence."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.agent.strategy import Side, Signal
from src.trading.risk import check_trade
from src.utils.config import RUNTIME_DIR


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Position:
    id: str
    pair: str
    side: str  # BUY / SELL
    lots: float
    entry: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    opened_at: str
    status: str = "open"  # open | closed
    exit_price: Optional[float] = None
    closed_at: Optional[str] = None
    pnl: float = 0.0
    note: str = ""


@dataclass
class PaperAccount:
    balance: float
    equity: float
    currency: str = "USD"
    positions: list[Position] = field(default_factory=list)
    closed: list[Position] = field(default_factory=list)
    daily_pnl: float = 0.0
    day_key: str = ""
    starting_balance: float = 10000.0


class PaperBroker:
    def __init__(self, starting_balance: float = 10000.0, risk_cfg: Optional[dict] = None):
        self.path = RUNTIME_DIR / "paper_account.json"
        self.risk_cfg = risk_cfg or {}
        self.account = self._load(starting_balance)

    def _load(self, starting_balance: float) -> PaperAccount:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                positions = [Position(**p) for p in raw.get("positions", [])]
                closed = [Position(**p) for p in raw.get("closed", [])]
                return PaperAccount(
                    balance=float(raw.get("balance", starting_balance)),
                    equity=float(raw.get("equity", starting_balance)),
                    currency=raw.get("currency", "USD"),
                    positions=positions,
                    closed=closed,
                    daily_pnl=float(raw.get("daily_pnl", 0.0)),
                    day_key=raw.get("day_key", ""),
                    starting_balance=float(raw.get("starting_balance", starting_balance)),
                )
            except Exception:  # noqa: BLE001
                pass
        return PaperAccount(
            balance=starting_balance,
            equity=starting_balance,
            starting_balance=starting_balance,
            day_key=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    def save(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "balance": self.account.balance,
            "equity": self.account.equity,
            "currency": self.account.currency,
            "positions": [asdict(p) for p in self.account.positions],
            "closed": [asdict(p) for p in self.account.closed[-100:]],
            "daily_pnl": self.account.daily_pnl,
            "day_key": self.account.day_key,
            "starting_balance": self.account.starting_balance,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.account.day_key != today:
            self.account.day_key = today
            self.account.daily_pnl = 0.0

    def open_from_signal(self, signal: Signal) -> tuple[bool, str, Optional[Position]]:
        self._roll_day()
        if signal.side == Side.FLAT:
            return False, "Signal is FLAT", None
        if signal.stop_loss is None:
            return False, "Signal missing stop loss", None

        # already open on pair?
        for p in self.account.positions:
            if p.pair == signal.pair and p.status == "open":
                return False, f"Already open on {signal.pair}", None

        daily_pct = (
            (self.account.daily_pnl / self.account.starting_balance) * 100.0
            if self.account.starting_balance
            else 0.0
        )
        decision = check_trade(
            equity=self.account.equity,
            open_positions=len(self.account.positions),
            daily_pnl_pct=daily_pct,
            entry=signal.price,
            stop_loss=signal.stop_loss,
            pair=signal.pair,
            risk_cfg=self.risk_cfg,
        )
        if not decision.allowed:
            return False, decision.reason, None

        pos = Position(
            id=str(uuid.uuid4())[:8],
            pair=signal.pair,
            side=signal.side.value,
            lots=decision.lots,
            entry=signal.price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            opened_at=_now(),
            note=f"score={signal.score}",
        )
        self.account.positions.append(pos)
        self.save()
        return True, f"Opened {pos.side} {pos.lots} {pos.pair} @ {pos.entry:.5f}", pos

    def _pnl(self, pos: Position, price: float) -> float:
        # simplified: PnL ≈ direction * (exit-entry) * lots * 100000
        direction = 1.0 if pos.side == "BUY" else -1.0
        move = price - pos.entry
        return direction * move * pos.lots * 100_000.0

    def mark_price(self, pair: str, price: float) -> list[str]:
        """Update equity; auto-close SL/TP. Returns event messages."""
        self._roll_day()
        events: list[str] = []
        still_open: list[Position] = []
        unrealized = 0.0

        for pos in self.account.positions:
            if pos.pair != pair:
                still_open.append(pos)
                # leave unrealized for other pairs unchanged this tick
                continue

            hit_sl = False
            hit_tp = False
            if pos.side == "BUY":
                hit_sl = pos.stop_loss is not None and price <= pos.stop_loss
                hit_tp = pos.take_profit is not None and price >= pos.take_profit
            else:
                hit_sl = pos.stop_loss is not None and price >= pos.stop_loss
                hit_tp = pos.take_profit is not None and price <= pos.take_profit

            if hit_sl or hit_tp:
                exit_px = pos.stop_loss if hit_sl else pos.take_profit
                assert exit_px is not None
                pnl = self._pnl(pos, float(exit_px))
                pos.status = "closed"
                pos.exit_price = float(exit_px)
                pos.closed_at = _now()
                pos.pnl = pnl
                self.account.balance += pnl
                self.account.daily_pnl += pnl
                self.account.closed.append(pos)
                reason = "SL" if hit_sl else "TP"
                events.append(
                    f"Closed {pos.pair} {pos.side} via {reason} @ {exit_px:.5f} PnL={pnl:+.2f}"
                )
            else:
                unrealized += self._pnl(pos, price)
                still_open.append(pos)

        # recompute unrealized for remaining positions that weren't marked this call
        # (keep simple: only marked pair updated this tick; others treated as flat mark)
        for pos in still_open:
            if pos.pair != pair:
                # approximate with entry (0 unrealized) if unknown
                pass

        self.account.positions = still_open
        self.account.equity = self.account.balance + unrealized
        self.save()
        return events

    def close_all(self, prices: dict[str, float]) -> list[str]:
        msgs: list[str] = []
        for pos in list(self.account.positions):
            px = prices.get(pos.pair)
            if px is None:
                msgs.append(f"No price for {pos.pair}, skipped")
                continue
            pnl = self._pnl(pos, px)
            pos.status = "closed"
            pos.exit_price = px
            pos.closed_at = _now()
            pos.pnl = pnl
            self.account.balance += pnl
            self.account.daily_pnl += pnl
            self.account.closed.append(pos)
            msgs.append(f"Closed {pos.pair} @ {px:.5f} PnL={pnl:+.2f}")
        self.account.positions = []
        self.account.equity = self.account.balance
        self.save()
        return msgs

    def reset(self, balance: Optional[float] = None) -> None:
        bal = balance if balance is not None else self.account.starting_balance
        self.account = PaperAccount(
            balance=bal,
            equity=bal,
            starting_balance=bal,
            day_key=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
        self.save()

    def summary(self) -> str:
        a = self.account
        lines = [
            f"*Paper account* ({a.currency})",
            f"Balance: `{a.balance:,.2f}`",
            f"Equity: `{a.equity:,.2f}`",
            f"Daily PnL: `{a.daily_pnl:+,.2f}`",
            f"Open positions: `{len(a.positions)}`",
        ]
        for p in a.positions:
            lines.append(
                f"  • `{p.id}` {p.side} {p.lots} {p.pair} @ {p.entry:.5f} "
                f"SL={p.stop_loss} TP={p.take_profit}"
            )
        if a.closed:
            recent = a.closed[-5:]
            lines.append("Recent closes:")
            for p in recent:
                lines.append(f"  • {p.pair} {p.side} PnL=`{p.pnl:+.2f}`")
        return "\n".join(lines)
