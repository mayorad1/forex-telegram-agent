"""Interactive Telegram trading bot — chat, buttons, multi-step flows."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from telegram import ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.agent.pdf_signals import clear_book, load_pdf_book, load_saved_book
from src.agent.strategy import ForexAgent, Side, Signal
from src.bot.chat_parser import CHAT_EXAMPLES, ChatIntent, extract_pair, parse_chat
from src.bot.keyboards import (
    interval_keyboard,
    lot_keyboard,
    main_keyboard,
    pair_keyboard,
    yes_no_keyboard,
)
from src.bot.session import SessionStore
from src.data.market_data import fetch_quote
from src.trading.lot_settings import apply_lot_to_risk_cfg, load_fixed_lot, save_fixed_lot
from src.trading.mt5_broker import MT5Broker
from src.utils.config import RUNTIME_DIR, allowed_user_ids, env_str
from src.utils.runtime_prefs import get_scan_interval_minutes, set_scan_interval_minutes

logger = logging.getLogger(__name__)

HELP_TEXT = (
    """
*🤖 Interactive Forex Agent*

Tap buttons below *or* type freely — I detect your command.

"""
    + CHAT_EXAMPLES
    + """

*Buttons* = one tap · *Chat* = type anything like `buy gold`

_Not financial advice._
"""
).strip()

# Map keyboard button labels → synthetic chat text
BUTTON_MAP = {
    "📡 signals": "signals",
    "⭐ best trade": "best trade",
    "📂 positions": "positions",
    "💰 status": "status",
    "🟢 buy": "buy",
    "🔴 sell": "sell",
    "📋 pairs": "pairs",
    "💲 price": "price",
    "🤖 auto on": "auto on",
    "⏸ auto off": "auto off",
    "🔌 mt5": "mt5",
    "📄 pdf": "pdf",
    "🧹 close all": "close all",
    "❓ help": "help",
    "⬅️ menu": "menu",
    "✅ yes": "yes",
    "❌ no": "no",
    "📐 lot": "lot",
    "📏 lot size": "lot",
    "⏱ interval": "interval",
}


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
        self.sessions = SessionStore()
        raw_alert = env_str("TELEGRAM_ALERT_CHAT_ID", "")
        if raw_alert.isdigit():
            self.alert_chat_id = int(raw_alert)
        if self.alert_chat_id is None and self.allowed:
            self.alert_chat_id = next(iter(self.allowed))

        self.app = Application.builder().token(token).build()
        self._register()

    def _register(self) -> None:
        handlers = [
            ("start", self.cmd_start),
            ("help", self.cmd_help),
            ("menu", self.cmd_menu),
            ("status", self.cmd_status),
            ("mt5", self.cmd_mt5),
            ("price", self.cmd_price),
            ("scan", self.cmd_scan),
            ("signal", self.cmd_signal),
            ("trade", self.cmd_trade),
            ("pdftrade", self.cmd_pdftrade),
            ("positions", self.cmd_positions),
            ("closeall", self.cmd_closeall),
            ("auto", self.cmd_auto),
            ("pairs", self.cmd_pairs),
            ("pdf", self.cmd_pdf_help),
            ("pdfsignals", self.cmd_pdfsignals),
            ("pdfclear", self.cmd_pdfclear),
            ("pdfmode", self.cmd_pdfmode),
            ("lot", self.cmd_lot),
            ("interval", self.cmd_interval),
        ]
        for name, fn in handlers:
            self.app.add_handler(CommandHandler(name, fn))

        self.app.add_handler(
            MessageHandler(
                filters.Document.PDF | filters.Document.MimeType("application/pdf"),
                self.on_pdf_document,
            )
        )
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_chat_text)
        )
        # Stickers / photos / voice — always reply interactively
        self.app.add_handler(
            MessageHandler(
                filters.PHOTO | filters.VOICE | filters.Sticker.ALL | filters.VIDEO,
                self.on_other_media,
            )
        )

        self._schedule_auto_job(first=45)

    # ── helpers ──────────────────────────────────────────────

    def _interval_minutes(self) -> int:
        default = int(self.settings.get("scan_interval_minutes", 15))
        return get_scan_interval_minutes(default)

    def _schedule_auto_job(self, first: int = 30) -> None:
        """(Re)schedule auto-trade job from current interval preference."""
        jq = self.app.job_queue
        if jq is None:
            logger.error("JobQueue missing — auto trading will NOT run")
            return
        # remove old
        for job in jq.get_jobs_by_name("auto_trade_cycle"):
            job.schedule_removal()
        minutes = self._interval_minutes()
        seconds = max(60, minutes * 60)
        jq.run_repeating(
            self.job_auto_trade,
            interval=seconds,
            first=first,
            name="auto_trade_cycle",
        )
        self.settings["scan_interval_minutes"] = minutes
        logger.info("Auto-trade scheduled every %s minute(s)", minutes)

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

    def _uid(self, update: Update) -> int:
        return update.effective_user.id if update.effective_user else 0

    async def _reply(
        self,
        update: Update,
        text: str,
        keyboard: Optional[ReplyKeyboardMarkup] = None,
    ) -> None:
        if not update.message:
            return
        if len(text) > 4000:
            text = text[:3900] + "\n\n_(truncated)_"
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=keyboard if keyboard is not None else main_keyboard(),
        )

    async def _notify(self, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
        chat_id = self.alert_chat_id
        if chat_id is None:
            return
        try:
            if len(text) > 4000:
                text = text[:3900] + "\n\n_(truncated)_"
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
                reply_markup=main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify failed: %s", exc)

    def _normalize_button(self, text: str) -> str:
        t = text.strip()
        key = t.lower()
        if key in BUTTON_MAP:
            return BUTTON_MAP[key]
        # strip emoji prefix for matching
        cleaned = re.sub(r"^[^\w]+", "", t).strip().lower()
        if cleaned in BUTTON_MAP:
            return BUTTON_MAP[cleaned]
        # exact pair buttons
        if re.fullmatch(r"[A-Za-z]{6}", t.replace("/", "")):
            return t.upper().replace("/", "")
        return t

    # ── entry / menu ─────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if update.effective_chat:
            self.alert_chat_id = update.effective_chat.id
        sess = self.sessions.get(self._uid(update))
        sess.clear_pending()
        pdf_n = len(self.agent.pdf_book.ideas) if self.agent.pdf_book else 0
        mins = self._interval_minutes()
        await self._reply(
            update,
            f"👋 *Hi!* PDF-only auto trader ready.\n\n"
            f"• Send a *PDF* with trade ideas\n"
            f"• Auto trades *only from that PDF*\n"
            f"• Interval: *every {mins} min* (change: `interval 5`)\n"
            f"• Trades: *unlimited* · lot: change with `lot 0.01`\n\n"
            f"Auto: `{'ON' if self.auto_enabled else 'OFF'}` · PDF ideas: `{pdf_n}`\n"
            f"MT5: `{'connected' if self.broker.connected else 'reconnect'}`\n\n"
            f"Tap a button or type a command 👇",
            main_keyboard(),
        )

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        self.sessions.get(self._uid(update)).clear_pending()
        await self._reply(update, "📋 *Main menu* — tap a button or type a command.", main_keyboard())

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, HELP_TEXT, main_keyboard())

    async def on_other_media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(
            update,
            "I work with *text* and *PDF files*.\n"
            "Try: `signals` · `buy eurusd` · or send a research PDF.\n"
            "Or tap a button below 👇",
            main_keyboard(),
        )

    # ── chat router ──────────────────────────────────────────

    async def on_chat_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        raw = (update.message.text if update.message else "") or ""
        text = self._normalize_button(raw)
        uid = self._uid(update)
        sess = self.sessions.get(uid)
        sess.last_text = raw

        # Menu cancel
        if text.lower() in {"menu", "cancel", "stop", "nevermind", "never mind", "back"}:
            sess.clear_pending()
            await self._reply(update, "OK — back to menu. What next?", main_keyboard())
            return

        # Multi-step: waiting for lot size
        if sess.waiting == "lot":
            await self._handle_lot_reply(update, sess, text)
            return

        # Multi-step: waiting for interval minutes
        if sess.waiting == "interval":
            await self._handle_interval_reply(update, sess, text)
            return

        # Multi-step: waiting for pair
        if sess.waiting == "pair":
            await self._handle_pair_reply(update, context, sess, text)
            return

        # Multi-step: waiting for yes/no confirm
        if sess.waiting == "confirm":
            await self._handle_confirm_reply(update, context, sess, text)
            return

        # Bare number while idle → treat as lot size shortcut
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text.strip()):
            await self._set_lot(update, text.strip())
            return

        # Bare pair while idle → show signal interactively
        if re.fullmatch(r"[A-Za-z]{3,6}/?[A-Za-z]{0,3}", text.replace(" ", "")):
            p = extract_pair(text)
            if p and len(text.split()) == 1:
                context.args = [p]
                await self.cmd_signal(update, context)
                return

        intent = parse_chat(text)
        if intent is None:
            intent = ChatIntent("unknown", raw=raw, arg="`signals`, `buy eurusd`, `help`")

        logger.info(
            "chat user=%s intent=%s pair=%s side=%s matched=%r",
            uid,
            intent.name,
            intent.pair,
            intent.side,
            intent.matched,
        )
        await self._dispatch_intent(update, context, intent)

    async def _handle_pair_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        sess,
        text: str,
    ) -> None:
        if text.lower() in {"menu", "cancel"}:
            sess.clear_pending()
            await self._reply(update, "Cancelled.", main_keyboard())
            return
        pair = extract_pair(text) or (
            text.upper().replace("/", "") if re.fullmatch(r"[A-Za-z]{6}", text.replace("/", "")) else None
        )
        if not pair:
            await self._reply(
                update,
                f"Still need a *pair* for `{sess.pending_action}`.\n"
                "Tap one below or type e.g. `EURUSD` / `gold`.",
                pair_keyboard(),
            )
            return

        action = sess.pending_action
        side = sess.pending_side
        sess.clear_pending()

        if action in {"force_buy", "force_sell", "force_trade"}:
            await self._force_trade(update, pair, side or "BUY")
        elif action == "trade":
            context.args = [pair]
            await self.cmd_trade(update, context)
        elif action == "price":
            context.args = [pair]
            await self.cmd_price(update, context)
        elif action == "signal":
            context.args = [pair]
            await self.cmd_signal(update, context)
        elif action == "close_pair":
            await self._close_pair(update, pair)
        else:
            context.args = [pair]
            await self.cmd_signal(update, context)

    async def _handle_confirm_reply(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        sess,
        text: str,
    ) -> None:
        low = text.lower().strip()
        yes = low in {"yes", "y", "ok", "okay", "✅ yes", "confirm", "do it", "go", "sure"}
        no = low in {"no", "n", "nope", "❌ no", "cancel", "stop"}
        if no:
            sess.clear_pending()
            await self._reply(update, "Cancelled. Nothing opened.", main_keyboard())
            return
        if not yes:
            await self._reply(
                update,
                f"Confirm *{sess.pending_side or sess.pending_action}* "
                f"`{sess.pending_pair}`? Tap Yes or No.",
                yes_no_keyboard(),
            )
            return
        pair = sess.pending_pair
        side = sess.pending_side
        action = sess.pending_action
        sess.clear_pending()
        if action == "force_trade" and pair and side:
            await self._force_trade(update, pair, side)
        elif action == "best_trade":
            await self._best_trade(update, skip_confirm=True)
        elif action == "closeall":
            await self.cmd_closeall(update, context)
        else:
            await self._reply(update, "Done.", main_keyboard())

    async def _dispatch_intent(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        intent: ChatIntent,
    ) -> None:
        sess = self.sessions.get(self._uid(update))
        name = intent.name

        if name == "help":
            await self.cmd_help(update, context)
        elif name == "unknown":
            await self._reply(
                update,
                f"🤔 I heard: _{intent.raw[:100]}_\n"
                f"Did you mean: {intent.arg}?\n\n"
                "Or tap a button 👇",
                main_keyboard(),
            )
        elif name == "scan":
            await self.cmd_scan(update, context)
        elif name == "signal":
            if intent.pair:
                context.args = [intent.pair]
                await self.cmd_signal(update, context)
            else:
                sess.ask_pair("signal")
                await self._reply(
                    update,
                    "Which pair do you want a *signal* for?",
                    pair_keyboard(),
                )
        elif name == "price":
            if intent.pair:
                context.args = [intent.pair]
                await self.cmd_price(update, context)
            else:
                sess.ask_pair("price")
                await self._reply(update, "Price for which *pair*?", pair_keyboard())
        elif name == "trade":
            if intent.pair:
                context.args = [intent.pair]
                await self.cmd_trade(update, context)
            else:
                sess.ask_pair("trade")
                await self._reply(update, "Trade which *pair* (strategy)?", pair_keyboard())
        elif name == "force_trade":
            if intent.pair and intent.side:
                # Ask confirm for safety on interactive force trade
                sess.ask_confirm("force_trade", intent.pair, intent.side)
                await self._reply(
                    update,
                    f"Confirm *{intent.side}* on `{intent.pair}` on MT5?\n"
                    "Tap *Yes* to open, *No* to cancel.",
                    yes_no_keyboard(),
                )
            else:
                sess.ask_pair("force_trade", intent.side or "BUY")
                await self._reply(
                    update,
                    f"*{intent.side or 'BUY'}* which pair?",
                    pair_keyboard(),
                )
        elif name == "need_pair":
            action = "force_trade" if intent.side else (intent.arg or "signal")
            sess.ask_pair(action, intent.side)
            await self._reply(
                update,
                f"Which pair for *{intent.side or action}*? Tap below or type it.",
                pair_keyboard(),
            )
        elif name == "best_trade":
            await self._best_trade(update)
        elif name == "positions":
            await self.cmd_positions(update, context)
        elif name == "status":
            await self.cmd_status(update, context)
        elif name == "pairs":
            await self.cmd_pairs(update, context)
        elif name == "closeall":
            sess.ask_confirm("closeall", "", None)
            await self._reply(
                update,
                "⚠️ Close *ALL* open MT5 positions?\nTap *Yes* or *No*.",
                yes_no_keyboard(),
            )
        elif name == "close_pair":
            if intent.pair:
                await self._close_pair(update, intent.pair)
            else:
                sess.ask_pair("close_pair")
                await self._reply(update, "Close which pair?", pair_keyboard())
        elif name == "auto" and intent.arg:
            context.args = [intent.arg]
            await self.cmd_auto(update, context)
        elif name == "mt5":
            await self.cmd_mt5(update, context)
        elif name == "pdfsignals":
            await self.cmd_pdfsignals(update, context)
        elif name == "pdftrade":
            await self.cmd_pdftrade(update, context)
        elif name == "pdfclear":
            await self.cmd_pdfclear(update, context)
        elif name == "pdf_help":
            await self.cmd_pdf_help(update, context)
        elif name == "set_lot" and intent.arg:
            await self._set_lot(update, intent.arg)
        elif name == "lot_menu":
            sess.waiting = "lot"
            sess.pending_action = "set_lot"
            cur = load_fixed_lot() or self.settings.get("risk", {}).get("fixed_lot_size") or "auto"
            await self._reply(
                update,
                f"Current lot: `{cur}`\n"
                "Tap a size or type e.g. `0.05` / `lot 0.1`",
                lot_keyboard(),
            )
        elif name == "set_interval" and intent.arg:
            await self._set_interval(update, intent.arg)
        elif name == "interval_menu":
            sess.waiting = "interval"
            sess.pending_action = "set_interval"
            cur = self._interval_minutes()
            await self._reply(
                update,
                f"⏱ Auto-trade every *{cur}* minute(s).\n"
                "Tap a value or type `every 5 min` / `interval 30`",
                interval_keyboard(),
            )
        else:
            await self._reply(
                update,
                "I'm here! Try `signals`, `buy eurusd`, `lot 0.05`, or tap a button.",
                main_keyboard(),
            )

    async def _handle_lot_reply(self, update: Update, sess, text: str) -> None:
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", "."))
        if not m:
            await self._reply(
                update,
                "Send a number like `0.01` or `0.10`",
                lot_keyboard(),
            )
            return
        sess.clear_pending()
        await self._set_lot(update, m.group(1))

    async def _handle_interval_reply(self, update: Update, sess, text: str) -> None:
        m = re.search(r"([0-9]{1,4})", text)
        if not m:
            await self._reply(
                update,
                "Send minutes like `5` or `30 min`",
                interval_keyboard(),
            )
            return
        sess.clear_pending()
        await self._set_interval(update, m.group(1))

    async def _set_interval(self, update: Update, raw: str) -> None:
        try:
            minutes = int(float(raw))
        except ValueError:
            await self._reply(update, "Invalid minutes. Example: `every 15 min`", main_keyboard())
            return
        minutes = set_scan_interval_minutes(minutes)
        self.settings["scan_interval_minutes"] = minutes
        self._schedule_auto_job(first=20)
        await self._reply(
            update,
            f"✅ Auto-trade interval set to *every {minutes} minute(s)*\n"
            f"Mode: *PDF-only* · trades: *unlimited*\n"
            f"Next cycle starts soon. Change anytime: `interval 10` or tap *⏱ Interval*",
            main_keyboard(),
        )

    async def _set_lot(self, update: Update, raw: str) -> None:
        try:
            lots = float(raw.replace(",", "."))
        except ValueError:
            await self._reply(update, "Invalid lot. Example: `lot 0.05`", main_keyboard())
            return
        if lots <= 0:
            await self._reply(update, "Lot must be greater than 0.", main_keyboard())
            return
        if lots > 100:
            await self._reply(
                update,
                "Lot looks huge (`>100`). Type again if you meant it, e.g. `0.10`.",
                main_keyboard(),
            )
            return
        lots = round(lots, 2)
        save_fixed_lot(lots)
        # live update broker risk
        self.broker.risk_cfg["fixed_lot_size"] = lots
        self.settings.setdefault("risk", {})["fixed_lot_size"] = lots
        await self._reply(
            update,
            f"✅ Lot size set to `{lots}` for *all new trades*\n"
            f"(unlimited open positions · unlimited per cycle)\n\n"
            f"Change anytime: `lot 0.02` or tap *Lot*",
            main_keyboard(),
        )

    # ── trading actions ──────────────────────────────────────

    async def _force_trade(self, update: Update, pair: str, side: str) -> None:
        await self._reply(update, f"⏳ Opening *{side}* `{pair}` on MT5…", main_keyboard())
        try:
            try:
                sig = self.agent.analyze_pair(pair)
            except Exception:  # noqa: BLE001
                q = fetch_quote(pair)
                sig = Signal(
                    pair=pair,
                    side=Side.BUY if side == "BUY" else Side.SELL,
                    price=q.price,
                    score=2,
                    confidence=0.5,
                    reasons=[f"Manual {side}"],
                    timeframe="manual",
                )
            forced = Side.BUY if side == "BUY" else Side.SELL
            price = sig.price or fetch_quote(pair).price
            atr = sig.atr or (price * 0.001)
            sl_m = float(self.settings.get("strategy", {}).get("sl_atr_mult", 1.5))
            tp_m = float(self.settings.get("strategy", {}).get("tp_atr_mult", 2.5))
            if forced == Side.BUY:
                sl, tp = price - atr * sl_m, price + atr * tp_m
            else:
                sl, tp = price + atr * sl_m, price - atr * tp_m
            if sig.stop_loss is None or sig.side != forced:
                sig = Signal(
                    pair=pair,
                    side=forced,
                    price=price,
                    score=max(sig.score, 2),
                    confidence=max(sig.confidence, 0.5),
                    reasons=list(sig.reasons) + [f"Interactive: {side} {pair}"],
                    stop_loss=sl,
                    take_profit=tp,
                    atr=atr,
                    rsi=sig.rsi,
                    timeframe=sig.timeframe,
                )
            else:
                sig.side = forced
            ok, msg, pos = self.broker.open_from_signal(sig)
            extra = f"\n{pos.side} {pos.lots} lots @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} {msg}{extra}\n\n{sig.pretty()}",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ Trade failed: `{exc}`", main_keyboard())

    async def _best_trade(self, update: Update, skip_confirm: bool = True) -> None:
        await self._reply(update, "⏳ Scanning for best setup…", main_keyboard())
        try:
            signals = self.agent.rank_for_trade(self.agent.actionable())
            if not signals:
                all_sigs = self.agent.scan()
                lines = ["No strong setup right now.\n*Scores:*"]
                for s in sorted(all_sigs, key=lambda x: x.score, reverse=True)[:6]:
                    lines.append(f"• `{s.pair}` {s.side.value} score={s.score}")
                await self._reply(update, "\n".join(lines), main_keyboard())
                return
            pick = signals[0]
            ok, msg, pos = self.broker.open_from_signal(pick)
            extra = f"\n{pos.side} {pos.lots} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} *Best pick* `{pick.pair}`\n{msg}{extra}\n\n{pick.pretty()}",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def _close_pair(self, update: Update, pair: str) -> None:
        try:
            self.broker.ensure()
            import MetaTrader5 as mt5

            positions = mt5.positions_get()
            if not positions:
                await self._reply(update, "No open positions.", main_keyboard())
                return
            target = pair.upper().replace("/", "")
            closed = []
            for p in positions:
                if target not in p.symbol.upper().replace("/", "").replace(".", ""):
                    # loose match
                    if target[:6] not in p.symbol.upper().replace("M", ""):
                        continue
                tick = mt5.symbol_info_tick(p.symbol)
                if tick is None:
                    closed.append(f"No tick {p.symbol}")
                    continue
                if p.type == mt5.POSITION_TYPE_BUY:
                    otype, price = mt5.ORDER_TYPE_SELL, tick.bid
                else:
                    otype, price = mt5.ORDER_TYPE_BUY, tick.ask
                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": p.symbol,
                    "volume": p.volume,
                    "type": otype,
                    "position": p.ticket,
                    "price": price,
                    "deviation": 30,
                    "magic": self.broker.MAGIC,
                    "comment": "interactive close",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self.broker._filling_mode(p.symbol),
                }
                r = mt5.order_send(req)
                if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                    closed.append(f"✅ Closed {p.symbol} #{p.ticket}")
                else:
                    closed.append(f"❌ {p.symbol}: {getattr(r, 'comment', r)}")
            await self._reply(
                update,
                "\n".join(closed) if closed else f"No position matching `{pair}`.",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    # ── slash / shared commands ──────────────────────────────

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        mins = self.settings.get("scan_interval_minutes", 15)
        pdf_cfg = self.settings.get("pdf", {}) or {}
        pdf_line = "PDF: `none`"
        if self.agent.pdf_book and self.agent.pdf_book.ideas:
            pdf_line = (
                f"PDF: `{self.agent.pdf_book.source_name}` "
                f"({len(self.agent.pdf_book.ideas)} ideas, `{pdf_cfg.get('mode', 'blend')}`)"
            )
        lot = load_fixed_lot() or self.settings.get("risk", {}).get("fixed_lot_size") or "auto %"
        max_open = self.settings.get("risk", {}).get("max_open_positions", 0)
        max_cycle = self.settings.get("max_new_trades_per_cycle", 0)
        mins = self._interval_minutes()
        pdf_mode = (self.settings.get("pdf") or {}).get("mode", "blend")
        text = (
            f"*Status* @ `{now}`\n"
            f"Mode: `MT5` · signals: `PDF-only ({pdf_mode})`\n"
            f"Auto-trade: `{'ON' if self.auto_enabled else 'OFF'}` every `{mins} min`\n"
            f"Lot: `{lot}` · open: `{'∞' if not max_open else max_open}` · "
            f"per cycle: `{'∞' if not max_cycle else max_cycle}`\n"
            f"MT5: `{'✅ connected' if self.broker.connected else '❌ disconnected'}`\n"
            f"{pdf_line}\n"
            f"_Change interval: `every 5 min` · lot: `lot 0.02`_\n\n"
            f"{self.broker.summary()}"
        )
        await self._reply(update, text, main_keyboard())

    async def cmd_mt5(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, "⏳ Reconnecting MT5…", main_keyboard())
        try:
            self.broker.disconnect()
            ok, msg = self.broker.connect(retries=5)
            await self._reply(
                update,
                (f"✅ {msg}\n\n{self.broker.summary()}" if ok else f"❌ {msg}"),
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            self.sessions.get(self._uid(update)).ask_pair("price")
            await self._reply(update, "Price for which pair?", pair_keyboard())
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            if self.broker.connected:
                try:
                    sym = self.broker.resolve_symbol(pair)
                    import MetaTrader5 as mt5

                    tick = mt5.symbol_info_tick(sym)
                    if tick:
                        await self._reply(
                            update,
                            f"*{sym}*\nBid: `{tick.bid}`\nAsk: `{tick.ask}`",
                            main_keyboard(),
                        )
                        return
                except Exception:  # noqa: BLE001
                    pass
            q = fetch_quote(pair)
            ch = f" ({q.change_pct:+.3f}%)" if q.change_pct is not None else ""
            await self._reply(update, f"*{q.pair}*: `{q.price:.5f}`{ch}", main_keyboard())
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, "⏳ Scanning markets…", main_keyboard())
        try:
            signals = self.agent.scan()
            min_score = int(self.settings.get("strategy", {}).get("min_score", 2))
            lines = ["*📡 Market signals*\n"]
            for s in signals:
                mark = "✅" if s.is_actionable(min_score) else "·"
                rsi_part = f" RSI={s.rsi:.1f}" if s.rsi is not None else ""
                px = f" @ {s.price:.5f}" if s.price else ""
                pdf = " 📄" if any("PDF" in r for r in s.reasons) else ""
                lines.append(
                    f"{mark} `{s.pair}` *{s.side.value}* score=`{s.score}`{px}{rsi_part}{pdf}"
                )
            lines.append("\n_Tap a pair name or type `buy eurusd` / `check gold`_")
            await self._reply(update, "\n".join(lines), main_keyboard())
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ Scan failed: `{exc}`", main_keyboard())

    async def cmd_signal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            self.sessions.get(self._uid(update)).ask_pair("signal")
            await self._reply(update, "Signal for which pair?", pair_keyboard())
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            sig = self.agent.analyze_pair(pair)
            tip = ""
            if sig.side.value in {"BUY", "SELL"}:
                tip = f"\n\nReply `buy {pair.lower()}` or `sell {pair.lower()}` to open."
            await self._reply(update, sig.pretty() + tip, main_keyboard())
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_trade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args:
            self.sessions.get(self._uid(update)).ask_pair("trade")
            await self._reply(update, "Trade which pair?", pair_keyboard())
            return
        pair = context.args[0].upper().replace("/", "")
        try:
            sig = self.agent.analyze_pair(pair)
            min_score = int(self.settings.get("strategy", {}).get("min_score", 2))
            if not sig.is_actionable(min_score):
                await self._reply(
                    update,
                    f"No strong signal for `{pair}` yet.\n\n{sig.pretty()}\n\n"
                    f"Force it with `buy {pair.lower()}` or `sell {pair.lower()}`.",
                    main_keyboard(),
                )
                return
            ok, msg, pos = self.broker.open_from_signal(sig)
            extra = f"\n{pos.side} {pos.lots} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} {msg}{extra}\n\n{sig.pretty()}",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_pdftrade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not self.agent.pdf_book or not self.agent.pdf_book.ideas:
            await self._reply(
                update,
                "No PDF loaded. Send a *PDF file* in chat first.",
                main_keyboard(),
            )
            return
        await self._reply(update, "⏳ Best PDF idea…", main_keyboard())
        try:
            signals = self.agent.actionable()
            ranked = self.agent.rank_for_trade(signals)
            pdf_pairs = {i.pair for i in self.agent.pdf_book.ideas}
            pick = next((s for s in ranked if s.pair in pdf_pairs), None)
            if pick is None:
                for idea in self.agent.pdf_book.ideas:
                    sig = self.agent.analyze_pair(idea.pair)
                    if sig.side.value != "FLAT":
                        pick = sig
                        break
            if pick is None:
                await self._reply(
                    update,
                    "No tradeable PDF idea now.\n\n" + self.agent.pdf_book.summary(),
                    main_keyboard(),
                )
                return
            ok, msg, pos = self.broker.open_from_signal(pick)
            extra = f"\n{pos.side} {pos.lots} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} [PDF] {msg}{extra}\n\n{pick.pretty()}",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, self.broker.summary(), main_keyboard())

    async def cmd_closeall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        # If already confirmed via session, pending cleared by caller path
        try:
            msgs = self.broker.close_all()
            await self._reply(
                update,
                "\n".join(msgs) if msgs else "No open positions.",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"❌ `{exc}`", main_keyboard())

    async def cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args or context.args[0].lower() not in {"on", "off"}:
            await self._reply(update, "Say `auto on` or `auto off`.", main_keyboard())
            return
        self.auto_enabled = context.args[0].lower() == "on"
        if update.effective_chat:
            self.alert_chat_id = update.effective_chat.id
        mins = self.settings.get("scan_interval_minutes", 15)
        await self._reply(
            update,
            f"Auto-trade: *{'ON' if self.auto_enabled else 'OFF'}* (every {mins}m)",
            main_keyboard(),
        )

    async def cmd_pairs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        pairs = self.settings.get("pairs", [])
        await self._reply(
            update,
            "*Watchlist:*\n"
            + "\n".join(f"• `{p}`" for p in pairs)
            + "\n\nTap a pair button or type its name for a signal.",
            pair_keyboard(),
        )

    async def cmd_pdf_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(
            update,
            "📄 *PDF mode*\n"
            "1. Send a PDF file here\n"
            "2. I extract BUY/SELL ideas\n"
            "3. Say `pdf signals` or `pdf trade`\n\n"
            "Best format: `EURUSD BUY SL 1.08 TP 1.10`",
            main_keyboard(),
        )

    async def cmd_pdfsignals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        book = self.agent.pdf_book or load_saved_book()
        self.agent.pdf_book = book
        if not book:
            await self._reply(update, "No PDF loaded. Send a PDF file.", main_keyboard())
            return
        await self._reply(update, book.summary(), main_keyboard())

    async def cmd_pdfclear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        clear_book()
        self.agent.set_pdf_book(None)
        await self._reply(update, "PDF ideas cleared.", main_keyboard())

    async def cmd_lot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if context.args:
            await self._set_lot(update, context.args[0])
            return
        sess = self.sessions.get(self._uid(update))
        sess.waiting = "lot"
        sess.pending_action = "set_lot"
        cur = load_fixed_lot() or self.settings.get("risk", {}).get("fixed_lot_size") or "auto"
        await self._reply(
            update,
            f"Current lot: `{cur}`\nTap a size or type `lot 0.05`",
            lot_keyboard(),
        )

    async def cmd_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if context.args:
            await self._set_interval(update, context.args[0])
            return
        sess = self.sessions.get(self._uid(update))
        sess.waiting = "interval"
        sess.pending_action = "set_interval"
        cur = self._interval_minutes()
        await self._reply(
            update,
            f"⏱ Current interval: *{cur} min*\nTap below or type `every 10 min`",
            interval_keyboard(),
        )

    async def cmd_pdfmode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if not context.args or context.args[0].lower() not in {
            "blend",
            "pdf_priority",
            "pdf_only",
        }:
            await self._reply(
                update,
                "Modes: `blend` · `pdf_priority` · `pdf_only`\n"
                "Example: type later via config; default is blend.",
                main_keyboard(),
            )
            return
        mode = context.args[0].lower()
        self.settings.setdefault("pdf", {})["mode"] = mode
        self.agent.pdf_cfg = self.settings.get("pdf", {})
        await self._reply(update, f"PDF mode: `{mode}`", main_keyboard())

    async def on_pdf_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        doc = update.message.document if update.message else None
        if doc is None:
            return
        name = (doc.file_name or "signal.pdf").lower()
        mime = (doc.mime_type or "").lower()
        if not (name.endswith(".pdf") or "pdf" in mime):
            await self._reply(update, "Please send a PDF.", main_keyboard())
            return
        await self._reply(update, f"⏳ Reading `{doc.file_name}`…", main_keyboard())
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            dest = RUNTIME_DIR / "last_upload.pdf"
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(custom_path=str(dest))
            book = load_pdf_book(
                dest,
                source_name=doc.file_name or "upload.pdf",
                watched=list(self.settings.get("pairs", [])),
            )
            self.agent.set_pdf_book(book)
            if not book.ideas:
                await self._reply(
                    update,
                    f"PDF read (`{book.text_chars}` chars) but no ideas found.\n"
                    "Use text like `EURUSD BUY SL … TP …`",
                    main_keyboard(),
                )
                return
            await self._reply(
                update,
                f"✅ Loaded *{len(book.ideas)}* idea(s)\n\n{book.summary()}\n\n"
                "Say `pdf trade` or wait for auto cycle.",
                main_keyboard(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("pdf upload failed")
            await self._reply(update, f"❌ PDF failed: `{exc}`", main_keyboard())

    def _quiet_hours(self) -> bool:
        qh = self.settings.get("quiet_hours") or {}
        start, end = qh.get("start_hour_utc"), qh.get("end_hour_utc")
        if start is None or end is None:
            return False
        hour = datetime.now(timezone.utc).hour
        start, end = int(start), int(end)
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    async def job_auto_trade(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.auto_enabled or self._quiet_hours():
            return
        place_orders = bool(self.settings.get("auto_trade_enabled", True))
        max_new = int(self.settings.get("max_new_trades_per_cycle", 0) or 0)
        unlimited = max_new <= 0
        mins = self._interval_minutes()
        try:
            apply_lot_to_risk_cfg(self.broker.risk_cfg)
            self.agent.reload_pdf_book()
            self.agent.pdf_cfg = self.settings.get("pdf", {}) or {}
            pdf_n = len(self.agent.pdf_book.ideas) if self.agent.pdf_book else 0
            lot = self.broker.risk_cfg.get("fixed_lot_size") or load_fixed_lot() or "?"
            pdf_mode = (self.settings.get("pdf") or {}).get("mode", "blend")

            if pdf_mode == "pdf_only" and pdf_n == 0:
                await self._notify(
                    context,
                    f"⏱ *{mins}m cycle* — PDF-only mode\n"
                    "❌ No PDF loaded. Send a PDF with ideas like `EURUSD BUY SL … TP …`",
                )
                return

            self.broker.ensure()
            signals = self.agent.rank_for_trade(self.agent.actionable())
            lines = [
                f"⏱ *{mins}m cycle* `{datetime.now(timezone.utc).strftime('%H:%M UTC')}`",
                f"Mode: `{pdf_mode}` · PDF ideas: `{pdf_n}` · actionable: `{len(signals)}`",
                f"Lot: `{lot}` · limit: `{'∞' if unlimited else max_new}`",
            ]
            if not signals:
                lines.append("No PDF trades this cycle.")
                await self._notify(context, "\n".join(lines))
                return
            opened = 0
            for sig in signals:
                lines.append(sig.pretty())
                if not place_orders:
                    continue
                if not unlimited and opened >= max_new:
                    lines.append(f"_(max {max_new}/cycle)_")
                    break
                ok, msg, _pos = self.broker.open_from_signal(sig)
                lines.append(f"{'✅' if ok else '❌'} {msg}")
                if ok:
                    opened += 1
            lines.append(f"\nOpened: `{opened}`\n{self.broker.summary()}")
            await self._notify(context, "\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            logger.exception("auto trade failed")
            await self._notify(context, f"❌ Auto cycle: `{exc}`")

    def run(self) -> None:
        logger.info(
            "Bot starting | auto=%s | interval=%sm | pdf_mode=%s",
            self.auto_enabled,
            self._interval_minutes(),
            (self.settings.get("pdf") or {}).get("mode"),
        )
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
