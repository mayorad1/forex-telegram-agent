"""MetaTrader 5 broker — Exness live/demo orders only (no paper)."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.agent.strategy import Side, Signal
from src.trading.risk import check_trade
from src.utils.config import env_int, env_str

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None  # type: ignore


@dataclass
class MT5Position:
    ticket: int
    pair: str
    side: str
    lots: float
    entry: float
    stop_loss: float
    take_profit: float
    profit: float


class MT5Broker:
    """Connects to local MetaTrader 5 and places real market orders on Exness."""

    MAGIC = 260710

    def __init__(self, risk_cfg: Optional[dict[str, Any]] = None):
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package not installed. Run: pip install MetaTrader5"
            )
        self.risk_cfg = risk_cfg or {}
        self.login = env_int("MT5_ACCOUNT", 0)
        self.password = env_str("MT5_PASSWORD", "")
        self.server = env_str("MT5_SERVER", "")
        self.path = (env_str("MT5_PATH", "") or r"C:\Program Files\MetaTrader 5\terminal64.exe").strip()
        self.timeout_ms = env_int("MT5_TIMEOUT_MS", 12000)
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected and self._ping()

    def _ping(self) -> bool:
        try:
            return mt5.terminal_info() is not None and mt5.account_info() is not None
        except Exception:  # noqa: BLE001
            return False

    def _ensure_terminal_running(self) -> None:
        """Start terminal64.exe if it is not already running."""
        try:
            import psutil  # optional
            for p in psutil.process_iter(["name", "exe"]):
                name = (p.info.get("name") or "").lower()
                if name == "terminal64.exe":
                    return
        except Exception:  # noqa: BLE001
            # fallback: tasklist
            try:
                out = subprocess.check_output(
                    ["tasklist", "/FI", "IMAGENAME eq terminal64.exe"],
                    text=True,
                    errors="ignore",
                )
                if "terminal64.exe" in out.lower():
                    return
            except Exception:  # noqa: BLE001
                pass

        exe = Path(self.path)
        if exe.is_file():
            logger.info("Starting MT5 terminal: %s", exe)
            subprocess.Popen([str(exe)], cwd=str(exe.parent))  # noqa: S603
            time.sleep(12)

    def connect(self, retries: int = 3) -> tuple[bool, str]:
        if not self.login or not self.password or not self.server:
            return False, "Missing MT5_ACCOUNT / MT5_PASSWORD / MT5_SERVER in .env"

        self._ensure_terminal_running()
        last_err: Any = None

        for attempt in range(1, retries + 1):
            logger.info("MT5 connect attempt %s/%s (timeout=%sms)", attempt, retries, self.timeout_ms)
            try:
                mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass

            # Prefer attach to already-running Exness terminal
            ok = mt5.initialize(timeout=self.timeout_ms)
            logger.info("  attach-only init: %s %s", ok, mt5.last_error() if not ok else "ok")
            if not ok:
                ok = mt5.initialize(path=self.path, timeout=self.timeout_ms)
                logger.info("  path init: %s %s", ok, mt5.last_error() if not ok else "ok")
            if not ok:
                ok = mt5.initialize(
                    path=self.path,
                    login=self.login,
                    password=self.password,
                    server=self.server,
                    timeout=self.timeout_ms,
                )
                logger.info("  full init: %s %s", ok, mt5.last_error() if not ok else "ok")

            if not ok:
                last_err = mt5.last_error()
                logger.warning("MT5 init attempt %s failed: %s", attempt, last_err)
                time.sleep(2)
                continue

            # Force login to Exness account (in case terminal is on wrong account)
            authorized = mt5.login(self.login, password=self.password, server=self.server)
            if not authorized:
                last_err = mt5.last_error()
                logger.warning("MT5 login attempt %s failed: %s", attempt, last_err)
                mt5.shutdown()
                time.sleep(3)
                continue

            info = mt5.account_info()
            if info is None:
                last_err = mt5.last_error()
                mt5.shutdown()
                time.sleep(3)
                continue

            self._connected = True
            msg = (
                f"Connected MT5 {info.login} @ {info.server} | "
                f"balance={info.balance:.2f} {info.currency} | equity={info.equity:.2f} | "
                f"leverage=1:{info.leverage} | trade_allowed={bool(info.trade_allowed)} | "
                f"EA={bool(info.trade_expert)}"
            )
            logger.info(msg)
            if not info.trade_allowed:
                return (
                    True,
                    msg + " | WARNING: trade_allowed=False — enable trading on this account",
                )
            if not info.trade_expert:
                return (
                    True,
                    msg
                    + " | WARNING: Algo Trading OFF — press Algo Trading in MT5 toolbar",
                )
            return True, msg

        return (
            False,
            f"MT5 connection failed after {retries} attempts: {last_err}. "
            "Keep MetaTrader 5 open, logged into Exness, Algo Trading ON, then restart the bot.",
        )

    def ensure(self) -> None:
        if self.connected:
            return
        self._connected = False
        ok, msg = self.connect()
        if not ok:
            raise RuntimeError(msg)

    def disconnect(self) -> None:
        try:
            if mt5 is not None:
                mt5.shutdown()
        finally:
            self._connected = False

    def account_info(self) -> dict[str, Any]:
        self.ensure()
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return {
            "login": info.login,
            "server": info.server,
            "name": info.name,
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "margin_free": info.margin_free,
            "profit": info.profit,
            "currency": info.currency,
            "leverage": info.leverage,
            "trade_allowed": bool(info.trade_allowed),
            "trade_expert": bool(info.trade_expert),
        }

    def resolve_symbol(self, pair: str) -> str:
        self.ensure()
        base = pair.upper().replace("/", "").replace("_", "")
        candidates = [
            base,
            f"{base}m",
            f"{base}.m",
            f"{base}#",
            f"{base}pro",
            f"{base}.a",
            f"{base}.b",
            f"{base}c",
        ]
        if base in {"XAUUSD", "GOLD"}:
            candidates.extend(["XAUUSDm", "XAUUSD.m", "GOLD", "GOLDm", "XAUUSD#"])
        if base in {"XAGUSD", "SILVER"}:
            candidates.extend(["XAGUSDm", "XAGUSD.m", "SILVER", "SILVERm"])

        for sym in candidates:
            info = mt5.symbol_info(sym)
            if info is not None:
                if not info.visible:
                    mt5.symbol_select(sym, True)
                return sym

        all_syms = mt5.symbols_get()
        if all_syms:
            compact = base
            for s in all_syms:
                name_u = s.name.upper().replace(".", "").replace("#", "").replace(" ", "")
                if compact in name_u:
                    if not s.visible:
                        mt5.symbol_select(s.name, True)
                    return s.name

        raise ValueError(
            f"Symbol not found for {pair}. Enable it in MT5 Market Watch."
        )

    def _filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return mt5.ORDER_FILLING_IOC
        filling = info.filling_mode
        if filling & 2:
            return mt5.ORDER_FILLING_IOC
        if filling & 1:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    def open_positions(self) -> list[MT5Position]:
        self.ensure()
        positions = mt5.positions_get()
        if positions is None:
            return []
        out: list[MT5Position] = []
        for p in positions:
            side = "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL"
            out.append(
                MT5Position(
                    ticket=p.ticket,
                    pair=p.symbol,
                    side=side,
                    lots=p.volume,
                    entry=p.price_open,
                    stop_loss=p.sl,
                    take_profit=p.tp,
                    profit=p.profit,
                )
            )
        return out

    def open_from_signal(self, signal: Signal) -> tuple[bool, str, Optional[MT5Position]]:
        self.ensure()
        if signal.side == Side.FLAT:
            return False, "Signal is FLAT", None
        if signal.stop_loss is None:
            return False, "Signal missing stop loss", None

        info = self.account_info()
        if not info["trade_allowed"]:
            return False, "Trading not allowed on this account", None
        if not info["trade_expert"]:
            return (
                False,
                "Algo Trading disabled in MT5. Click the Algo Trading button (toolbar), then retry.",
            )

        symbol = self.resolve_symbol(signal.pair)
        for p in self.open_positions():
            if p.pair == symbol:
                return False, f"Already open on {symbol} (ticket {p.ticket})", None

        tick = mt5.symbol_info_tick(symbol)
        sym = mt5.symbol_info(symbol)
        if tick is None or sym is None:
            return False, f"No tick/symbol info for {symbol}: {mt5.last_error()}", None

        price = tick.ask if signal.side == Side.BUY else tick.bid
        decision = check_trade(
            equity=float(info["equity"]),
            open_positions=len(self.open_positions()),
            daily_pnl_pct=0.0,
            entry=price,
            stop_loss=float(signal.stop_loss),
            pair=signal.pair,
            risk_cfg=self.risk_cfg,
        )
        if not decision.allowed:
            return False, decision.reason, None

        volume = float(decision.lots)
        step = sym.volume_step or 0.01
        vmin = sym.volume_min or 0.01
        broker_max = float(sym.volume_max or 100.0)
        cfg_max = self.risk_cfg.get("max_lot_size", None)
        if cfg_max not in (None, "", 0, "0"):
            vmax = min(broker_max, float(cfg_max))
        else:
            vmax = broker_max  # no app-side cap
        volume = max(vmin, min(vmax, round(round(volume / step) * step, 2)))

        order_type = mt5.ORDER_TYPE_BUY if signal.side == Side.BUY else mt5.ORDER_TYPE_SELL
        digits = int(sym.digits)
        sl = round(float(signal.stop_loss), digits)
        tp = round(float(signal.take_profit), digits) if signal.take_profit else 0.0

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": 30,
            "magic": self.MAGIC,
            "comment": f"fx-agent s={signal.score}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        result = mt5.order_send(request)
        if result is None:
            return False, f"order_send returned None: {mt5.last_error()}", None
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return (
                False,
                f"Order rejected: retcode={result.retcode} comment={result.comment}",
                None,
            )

        pos = MT5Position(
            ticket=result.order,
            pair=symbol,
            side=signal.side.value,
            lots=volume,
            entry=result.price or price,
            stop_loss=sl,
            take_profit=tp,
            profit=0.0,
        )
        msg = (
            f"MT5 opened {pos.side} {pos.lots} {pos.pair} @ {pos.entry:.{digits}f} "
            f"(order {result.order})"
        )
        return True, msg, pos

    def close_all(self, prices: Optional[dict[str, float]] = None) -> list[str]:
        self.ensure()
        msgs: list[str] = []
        positions = mt5.positions_get()
        if not positions:
            return ["No open MT5 positions."]
        for p in positions:
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                msgs.append(f"No tick for {p.symbol}, skipped")
                continue
            if p.type == mt5.POSITION_TYPE_BUY:
                order_type = mt5.ORDER_TYPE_SELL
                price = tick.bid
            else:
                order_type = mt5.ORDER_TYPE_BUY
                price = tick.ask
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": order_type,
                "position": p.ticket,
                "price": price,
                "deviation": 30,
                "magic": self.MAGIC,
                "comment": "fx-agent close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._filling_mode(p.symbol),
            }
            result = mt5.order_send(request)
            if result is None:
                msgs.append(f"Close {p.ticket} failed: {mt5.last_error()}")
            elif result.retcode != mt5.TRADE_RETCODE_DONE:
                msgs.append(
                    f"Close {p.ticket} rejected: {result.retcode} {result.comment}"
                )
            else:
                msgs.append(f"Closed {p.symbol} ticket {p.ticket} @ {price}")
        return msgs

    def summary(self) -> str:
        try:
            info = self.account_info()
        except Exception as exc:  # noqa: BLE001
            return f"*MT5 account*\n❌ Not connected: `{exc}`"
        lines = [
            f"*MT5 Exness* `{info['login']}` @ `{info['server']}`",
            f"Name: `{info['name']}`",
            f"Balance: `{info['balance']:,.2f}` {info['currency']}",
            f"Equity: `{info['equity']:,.2f}`",
            f"Profit: `{info['profit']:+,.2f}`",
            f"Free margin: `{info['margin_free']:,.2f}`",
            f"Leverage: `1:{info['leverage']}`",
            f"Trade allowed: `{info['trade_allowed']}` | Algo: `{info['trade_expert']}`",
        ]
        positions = self.open_positions()
        lines.append(f"Open positions: `{len(positions)}`")
        for p in positions:
            lines.append(
                f"  • `{p.ticket}` {p.side} {p.lots} {p.pair} @ {p.entry} "
                f"PnL=`{p.profit:+.2f}` SL={p.stop_loss} TP={p.take_profit}"
            )
        return "\n".join(lines)
