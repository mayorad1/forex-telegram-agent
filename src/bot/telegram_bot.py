"""Telegram interface — MT5 Exness trading agent."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from src.agent.strategy import ForexAgent
from src.data.market_data import fetch_quote
from src.trading.mt5_broker import MT5Broker
from src.utils.config import allowed_user_ids, env_str

logger = logging.getLogger(__name__)

HELP_TEXT = """
*Forex MT5 Agent (Exness)*

/start — welcome
/help — help
/status — MT5 account + auto status
/mt5 — reconnect / diagnose MT5
/price `<PAIR>` — quote
/scan — scan pairs (signals only)
/signal `<PAIR>` — full signal
/trade `<PAIR>` — open MT5 trade from signal
/positions — open MT5 positions
/closeall — close all MT5 positions
/auto on|off — auto-trade every 15m
/pairs — watched pairs

_Real MT5 orders. Not financial advice._
""".strip()


class ForexTelegramBot:
    def __init__(
        self,
        token: str,
        settings: dict[str, Any],
        agent: ForexAgent,
        broker: MT5Broker,
    ):
        self.token = token
        self.settings = settings
        self.agent = agent
        self.broker = broker
        self.allowed = allowed_user_ids()
        self.auto_enabled = bool(settings.get("auto_trade_on_start", True))
        self.mode = "mt5"
        self.alert_chat_id: Optional[int] = None
        raw_alert = env_str("TELEGRAM_ALERT_CHAT_ID", "")
        if raw_alert.isdigit():
            self.alert_chat_id = int(raw_alert)
        # default alerts to first allowed user
        if self.alert_chat_id is None and self.allowed:
            self.alert_chat_id = next(iter(self.allowed))

        self.app = Application.builder().token(token).build()
        self._register()

    def _register(self) -> None:
        handlers = [
            ("start", self.cmd_start),
            ("help", self.cmd_help),
            ("status", self.cmd_status),
            ("mt5", self.cmd_mt5),
            ("price", self.cmd_price),
            ("scan", self.cmd_scan),
            ("signal", self.cmd_signal),
            ("trade", self.cmd_trade),
            ("positions", self.cmd_positions),
            ("closeall", self.cmd_closeall),
            ("auto", self.cmd_auto),
            ("pairs", self.cmd_pairs),
        ]
        for name, fn in handlers:
            self.app.add_handler(CommandHandler(name, fn))

        minutes = int(self.settings.get("scan_interval_minutes", 15))
        if self.app.job_queue is not None:
            self.app.job_queue.run_repeating(
                self.job_auto_trade,
                interval=max(60, minutes * 60),
                first=45,
                name="auto_trade_15m",
            )
            logger.info("Auto-trade job every %s minutes (first run in 45s)", minutes)
        else:
            logger.error("JobQueue missing — auto 15m trading will NOT run")

    async def _auth(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if not self.allowed:
            return True
        if user.id not in self.allowed:
            if update.message:
                await update.message.reply_text("⛔ Unauthorized.")
            return False
        return True

    async def _reply(self, update: Update, text: str) -> None:
        if update.message:
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
            )

    async def _notify(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        chat_id = self.alert_chat_id
        if chat_id is None:
            return
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify failed: %s", exc)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if update.effective_chat:
            self.alert_chat_id = update.effective_chat.id
        uid = update.effective_user.id if update.effective_user else "?"
        await self._reply(
            update,
            f"👋 MT5 Exness agent online.\n"
            f"Auto-trade 15m: `{'ON' if self.auto_enabled else 'OFF'}`\n"
            f"Your ID: `{uid}`\n\n{HELP_TEXT}",
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, HELP_TEXT)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        mins = self.settings.get("scan_interval_minutes", 15)
        text = (
            f"*Status* @ `{now}`\n"
            f"Mode: `MT5 Exness`\n"
            f"Auto-trade: `{'ON' if self.auto_enabled else 'OFF'}` every `{mins}m`\n"
            f"Pairs: `{', '.join(self.settings.get('pairs', []))}`\n\n"
            f"{self.broker.summary()}"
        )
        await self._reply(update, text)

    async def cmd_mt5(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, "⏳ Reconnecting to MT5…")
        try:
            self.broker.disconnect()
            ok, msg = self.broker.connect(retries=5)
            if ok:
                await self._reply(update, f"✅ {msg}\n\n{self.broker.summary()}")
            else:
                await self._reply(
                    update,
                    f"❌ {msg}\n\n"
                    "Keep MT5 open on Exness, turn *Algo Trading* ON, free RAM, then `/mt5` again.",
                )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ MT5 error: `{exc}`")

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            await self._reply(update, "Usage: `/price EURUSD`")
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            if self.broker.connected:
                sym = self.broker.resolve_symbol(pair)
                import MetaTrader5 as mt5

                tick = mt5.symbol_info_tick(sym)
                if tick:
                    await self._reply(
                        update, f"*{sym}* (MT5)\nBid: `{tick.bid}` Ask: `{tick.ask}`"
                    )
                    return
            q = fetch_quote(pair)
            ch = f" ({q.change_pct:+.3f}%)" if q.change_pct is not None else ""
            await self._reply(update, f"*{q.pair}*: `{q.price:.5f}`{ch}")
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Failed: `{exc}`")

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, "⏳ Scanning…")
        try:
            signals = self.agent.scan()
            min_score = int(self.settings.get("strategy", {}).get("min_score", 2))
            lines = ["*Market scan*"]
            for s in signals:
                mark = "✅" if s.is_actionable(min_score) else "·"
                rsi_part = f" RSI={s.rsi:.1f}" if s.rsi is not None else ""
                px_part = f" @ {s.price:.5f}" if s.price else ""
                lines.append(
                    f"{mark} `{s.pair}` {s.side.value} score={s.score}{px_part}{rsi_part}"
                )
            await self._reply(update, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Scan failed: `{exc}`")

    async def cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            await self._reply(update, "Usage: `/signal EURUSD`")
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            sig = self.agent.analyze_pair(pair)
            await self._reply(update, sig.pretty())
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Failed: `{exc}`")

    async def cmd_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            await self._reply(update, "Usage: `/trade EURUSD`")
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            sig = self.agent.analyze_pair(pair)
            min_score = int(self.settings.get("strategy", {}).get("min_score", 2))
            if not sig.is_actionable(min_score):
                await self._reply(
                    update, f"No actionable signal for {pair}.\n\n{sig.pretty()}"
                )
                return
            ok, msg, pos = self.broker.open_from_signal(sig)
            extra = ""
            if pos is not None:
                extra = f"\n{pos.side} {pos.lots} {pos.pair} @ {pos.entry}"
            await self._reply(
                update, f"{'✅' if ok else '❌'} [MT5] {msg}{extra}\n\n{sig.pretty()}"
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Trade failed: `{exc}`")

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, self.broker.summary())

    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        try:
            msgs = self.broker.close_all()
            await self._reply(update, "\n".join(msgs) if msgs else "No open positions.")
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Close failed: `{exc}`")

    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args or context.args[0].lower() not in {"on", "off"}:
            await self._reply(update, "Usage: `/auto on` or `/auto off`")
            return
        self.auto_enabled = context.args[0].lower() == "on"
        if update.effective_chat:
            self.alert_chat_id = update.effective_chat.id
        mins = self.settings.get("scan_interval_minutes", 15)
        await self._reply(
            update,
            f"Auto-trade: `{'ON' if self.auto_enabled else 'OFF'}` "
            f"(every `{mins}` minutes, real MT5 orders)",
        )

    async def cmd_pairs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        pairs = self.settings.get("pairs", [])
        await self._reply(update, "*Watched pairs:*\n" + "\n".join(f"• `{p}`" for p in pairs))

    def _quiet_hours(self) -> bool:
        qh = self.settings.get("quiet_hours") or {}
        start = qh.get("start_hour_utc")
        end = qh.get("end_hour_utc")
        if start is None or end is None:
            return False
        hour = datetime.now(timezone.utc).hour
        start, end = int(start), int(end)
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    async def job_auto_trade(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Every 15 minutes: scan signals and open MT5 trades."""
        if not self.auto_enabled:
            logger.info("Auto-trade skipped (disabled)")
            return
        if self._quiet_hours():
            logger.info("Auto-trade skipped (quiet hours)")
            return

        place_orders = bool(self.settings.get("auto_trade_enabled", True))
        max_new = int(self.settings.get("max_new_trades_per_cycle", 1))
        min_score = int(self.settings.get("strategy", {}).get("min_score", 2))

        try:
            # keep MT5 session alive
            self.broker.ensure()

            signals = self.agent.actionable()
            lines = [
                f"⏱ *15m auto cycle* @ `{datetime.now(timezone.utc).strftime('%H:%M UTC')}`",
                f"Actionable signals: `{len(signals)}`",
            ]

            if not signals:
                lines.append("No trades — no signal edge.")
                await self._notify(context, "\n".join(lines))
                return

            opened = 0
            for sig in sorted(signals, key=lambda s: s.score, reverse=True):
                lines.append(sig.pretty())
                if not place_orders:
                    continue
                if opened >= max_new:
                    lines.append(f"_(max {max_new} new trade(s) per cycle)_")
                    break
                ok, msg, pos = self.broker.open_from_signal(sig)
                lines.append(f"{'✅' if ok else '❌'} {msg}")
                if ok:
                    opened += 1
                logger.info("auto trade %s: %s", sig.pair, msg)

            lines.append(f"\nOpened this cycle: `{opened}`")
            lines.append("\n" + self.broker.summary())
            await self._notify(context, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto trade cycle failed: %s", exc)
            await self._notify(context, f"❌ Auto-trade cycle failed: `{exc}`")

    def run(self) -> None:
        logger.info(
            "Starting Telegram bot | MT5 | auto=%s",
            self.auto_enabled,
        )
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
