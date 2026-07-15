"""Telegram interface — MT5 Exness trading agent + PDF + natural chat."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from telegram import Update
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
from src.bot.chat_parser import CHAT_EXAMPLES, ChatIntent, parse_chat
from src.data.market_data import fetch_quote
from src.trading.mt5_broker import MT5Broker
from src.utils.config import RUNTIME_DIR, allowed_user_ids, env_str

logger = logging.getLogger(__name__)

HELP_TEXT = """
*Forex Agent* — no slash needed. I detect your words.

""" + CHAT_EXAMPLES + """

*Slash still works:* /scan /trade /status /mt5 /auto

_Not financial advice._
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
            ("pdftrade", self.cmd_pdftrade),
            ("positions", self.cmd_positions),
            ("closeall", self.cmd_closeall),
            ("auto", self.cmd_auto),
            ("pairs", self.cmd_pairs),
            ("pdf", self.cmd_pdf_help),
            ("pdfsignals", self.cmd_pdfsignals),
            ("pdfclear", self.cmd_pdfclear),
            ("pdfmode", self.cmd_pdfmode),
        ]
        for name, fn in handlers:
            self.app.add_handler(CommandHandler(name, fn))

        # Accept PDF documents sent to the bot
        self.app.add_handler(
            MessageHandler(
                filters.Document.PDF | filters.Document.MimeType("application/pdf"),
                self.on_pdf_document,
            )
        )
        # Natural language (no slash) — after commands so /foo still works
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_chat_text)
        )

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
            # Telegram message limit ~4096
            if len(text) > 4000:
                text = text[:3900] + "\n\n_(truncated)_"
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
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
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify failed: %s", exc)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        if update.effective_chat:
            self.alert_chat_id = update.effective_chat.id
        uid = update.effective_user.id if update.effective_user else "?"
        pdf_n = len(self.agent.pdf_book.ideas) if self.agent.pdf_book else 0
        await self._reply(
            update,
            f"👋 Ready. Just type messages like *signals* or *buy eurusd*.\n"
            f"Auto-trade 15m: `{'ON' if self.auto_enabled else 'OFF'}`\n"
            f"PDF ideas: `{pdf_n}` | ID: `{uid}`\n\n{HELP_TEXT}",
        )

    async def on_chat_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle plain English / casual commands without /."""
        if not await self._auth(update):
            return
        text = (update.message.text if update.message else "") or ""
        intent = parse_chat(text)
        if intent is None:
            await self._reply(update, "Try `help` or `signals`.")
            return
        # Light feedback when we fuzzy-matched
        if intent.matched and intent.name not in {"unknown", "help"}:
            logger.info("chat intent=%s pair=%s side=%s matched=%r", intent.name, intent.pair, intent.side, intent.matched)
        await self._dispatch_intent(update, context, intent)

    async def _dispatch_intent(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        intent: ChatIntent,
    ) -> None:
        name = intent.name
        if name == "help":
            await self.cmd_help(update, context)
        elif name == "unknown":
            pair_hint = f"\nI saw pair `{intent.pair}`." if intent.pair else ""
            await self._reply(
                update,
                f"Not sure what you mean by _{intent.raw[:80]}_.\n"
                f"Did you mean: {intent.arg}?{pair_hint}\n\n"
                "Examples: `signals` · `buy eurusd` · `positions` · `help`",
            )
        elif name == "scan":
            await self.cmd_scan(update, context)
        elif name == "signal" and intent.pair:
            context.args = [intent.pair]
            await self.cmd_signal(update, context)
        elif name == "price" and intent.pair:
            context.args = [intent.pair]
            await self.cmd_price(update, context)
        elif name == "trade" and intent.pair:
            context.args = [intent.pair]
            await self.cmd_trade(update, context)
        elif name == "force_trade" and intent.pair and intent.side:
            await self._force_trade(update, intent.pair, intent.side)
        elif name == "best_trade":
            await self._best_trade(update)
        elif name == "need_pair":
            action = intent.side or intent.arg or "use"
            await self._reply(
                update,
                f"Which pair? e.g. `{action} eurusd` or `{action} gold`\n"
                "Pairs: eurusd, gbpusd, usdjpy, gold, audusd, usdcad…",
            )
        elif name == "positions":
            await self.cmd_positions(update, context)
        elif name == "status":
            await self.cmd_status(update, context)
        elif name == "pairs":
            await self.cmd_pairs(update, context)
        elif name == "closeall":
            await self.cmd_closeall(update, context)
        elif name == "close_pair" and intent.pair:
            await self._close_pair(update, intent.pair)
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
        else:
            await self._reply(update, "Try `help` for examples.")

    async def _best_trade(self, update: Update) -> None:
        """Scan and open the single best actionable signal."""
        await self._reply(update, "⏳ Finding best setup…")
        try:
            signals = self.agent.rank_for_trade(self.agent.actionable())
            if not signals:
                # show scan summary even if none actionable
                all_sigs = self.agent.scan()
                lines = ["No strong setup right now.\n*Latest scores:*"]
                for s in sorted(all_sigs, key=lambda x: x.score, reverse=True)[:6]:
                    lines.append(f"• `{s.pair}` {s.side.value} score={s.score}")
                lines.append("\nTry `signals` for full detail.")
                await self._reply(update, "\n".join(lines))
                return
            pick = signals[0]
            ok, msg, pos = self.broker.open_from_signal(pick)
            extra = f"\n{pos.side} {pos.lots} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} *Best pick* `{pick.pair}`\n{msg}{extra}\n\n{pick.pretty()}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Best trade failed: `{exc}`")

    async def _force_trade(self, update: Update, pair: str, side: str) -> None:
        """Open MT5 trade on request (buy/sell for pair), with tech SL/TP if possible."""
        await self._reply(update, f"⏳ Opening *{side}* on `{pair}`…")
        try:
            # Build signal from market analysis but force user side
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
                    reasons=[f"Manual {side} request"],
                    timeframe="manual",
                )
            # force direction
            forced = Side.BUY if side == "BUY" else Side.SELL
            if sig.side != forced or sig.stop_loss is None:
                # rebuild SL/TP for forced side using ATR if available
                price = sig.price or fetch_quote(pair).price
                atr = sig.atr or (price * 0.001)
                sl_m = float(self.settings.get("strategy", {}).get("sl_atr_mult", 1.5))
                tp_m = float(self.settings.get("strategy", {}).get("tp_atr_mult", 2.5))
                if forced == Side.BUY:
                    sl, tp = price - atr * sl_m, price + atr * tp_m
                else:
                    sl, tp = price + atr * sl_m, price - atr * tp_m
                sig = Signal(
                    pair=pair,
                    side=forced,
                    price=price,
                    score=max(sig.score, 2),
                    confidence=max(sig.confidence, 0.5),
                    reasons=list(sig.reasons) + [f"Manual chat: {side} {pair}"],
                    stop_loss=sl,
                    take_profit=tp,
                    atr=atr,
                    rsi=sig.rsi,
                    timeframe=sig.timeframe,
                )
            else:
                sig.side = forced
                sig.reasons.append(f"Manual chat: {side} {pair}")

            ok, msg, pos = self.broker.open_from_signal(sig)
            extra = f"\n{pos.side} {pos.lots} {pos.pair} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} {msg}{extra}\n\n{sig.pretty()}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Trade failed: `{exc}`")

    async def _close_pair(self, update: Update, pair: str) -> None:
        try:
            self.broker.ensure()
            import MetaTrader5 as mt5

            positions = mt5.positions_get()
            if not positions:
                await self._reply(update, "No open positions.")
                return
            target = pair.upper().replace("/", "")
            closed = []
            for p in positions:
                sym = p.symbol.upper().replace(".", "").replace("M", "")
                if target in p.symbol.upper().replace("/", "") or target in sym:
                    # close via broker close_all filtered — reuse order_send path
                    tick = mt5.symbol_info_tick(p.symbol)
                    if tick is None:
                        closed.append(f"No tick for {p.symbol}")
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
                        "comment": "chat close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": self.broker._filling_mode(p.symbol),
                    }
                    r = mt5.order_send(req)
                    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                        closed.append(f"Closed {p.symbol} #{p.ticket}")
                    else:
                        closed.append(f"Fail {p.symbol}: {getattr(r, 'comment', r)}")
            if not closed:
                await self._reply(update, f"No open position matching `{pair}`.")
            else:
                await self._reply(update, "\n".join(closed))
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"Close failed: `{exc}`")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(update, HELP_TEXT)

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
                f"({len(self.agent.pdf_book.ideas)} ideas, mode=`{pdf_cfg.get('mode', 'blend')}`)"
            )
        text = (
            f"*Status* @ `{now}`\n"
            f"Mode: `MT5 Exness`\n"
            f"Auto-trade: `{'ON' if self.auto_enabled else 'OFF'}` every `{mins}m`\n"
            f"{pdf_line}\n"
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
        await self._reply(update, "⏳ Scanning (tech + PDF)…")
        try:
            signals = self.agent.scan()
            min_score = int(self.settings.get("strategy", {}).get("min_score", 2))
            lines = ["*Market scan* (tech + PDF)"]
            for s in signals:
                mark = "✅" if s.is_actionable(min_score) else "·"
                rsi_part = f" RSI={s.rsi:.1f}" if s.rsi is not None else ""
                px_part = f" @ {s.price:.5f}" if s.price else ""
                pdf_tag = " 📄" if any("PDF" in r for r in s.reasons) else ""
                lines.append(
                    f"{mark} `{s.pair}` {s.side.value} score={s.score}{px_part}{rsi_part}{pdf_tag}"
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
            if self.agent.pdf_book and self.settings.get("pdf", {}).get("relax_min_score"):
                min_score = max(1, min_score - 1)
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

    async def cmd_pdftrade(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Pick best PDF-backed signal and trade it on MT5."""
        if not await self._auth(update):
            return
        if not self.agent.pdf_book or not self.agent.pdf_book.ideas:
            await self._reply(update, "No PDF loaded. Send a PDF first, then `/pdftrade`.")
            return
        await self._reply(update, "⏳ Picking best PDF-backed trade…")
        try:
            signals = self.agent.actionable()
            ranked = self.agent.rank_for_trade(signals)
            pdf_pairs = {i.pair for i in self.agent.pdf_book.ideas}
            # prefer PDF pairs; if none actionable, force-analyze PDF pairs
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
                    "No tradeable PDF idea right now.\n\n" + self.agent.pdf_book.summary(),
                )
                return
            ok, msg, pos = self.broker.open_from_signal(pick)
            extra = f"\n{pos.side} {pos.lots} @ {pos.entry}" if pos else ""
            await self._reply(
                update,
                f"{'✅' if ok else '❌'} [PDF+MT5] {msg}{extra}\n\n{pick.pretty()}",
            )
        except Exception as exc:  # noqa: BLE001
            await self._reply(update, f"PDF trade failed: `{exc}`")

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
            f"(every `{mins}` minutes, tech + PDF, real MT5 orders)",
        )

    async def cmd_pairs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        pairs = self.settings.get("pairs", [])
        await self._reply(update, "*Watched pairs:*\n" + "\n".join(f"• `{p}`" for p in pairs))

    async def cmd_pdf_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        await self._reply(
            update,
            "Send a *PDF file* in this chat (research / signal sheet).\n"
            "I extract pairs like `EURUSD BUY` with optional SL/TP.\n\n"
            "Then:\n"
            "• `/pdfsignals` — show ideas\n"
            "• `/pdftrade` — trade best PDF idea\n"
            "• `/scan` — tech + PDF combined\n"
            "• `/pdfmode blend` — default combine mode",
        )

    async def cmd_pdfsignals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        book = self.agent.pdf_book or load_saved_book()
        self.agent.pdf_book = book
        if not book:
            await self._reply(update, "No PDF loaded yet. Send a PDF file.")
            return
        await self._reply(update, book.summary())

    async def cmd_pdfclear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        clear_book()
        self.agent.set_pdf_book(None)
        await self._reply(update, "PDF ideas cleared. Tech-only signals until you upload again.")

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
                "Usage: `/pdfmode blend` | `/pdfmode pdf_priority` | `/pdfmode pdf_only`\n"
                "• *blend* — tech + PDF boost when they agree\n"
                "• *pdf\\_priority* — PDF direction wins\n"
                "• *pdf\\_only* — only pairs mentioned in PDF",
            )
            return
        mode = context.args[0].lower()
        self.settings.setdefault("pdf", {})["mode"] = mode
        self.agent.pdf_cfg = self.settings.get("pdf", {})
        await self._reply(update, f"PDF mode set to `{mode}` (this session).")

    async def on_pdf_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._auth(update):
            return
        doc = update.message.document if update.message else None
        if doc is None:
            return
        name = (doc.file_name or "signal.pdf").lower()
        mime = (doc.mime_type or "").lower()
        if not (name.endswith(".pdf") or "pdf" in mime):
            await self._reply(update, "Please send a *PDF* file.")
            return

        await self._reply(update, f"⏳ Reading PDF `{doc.file_name}`…")
        try:
            RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            dest = RUNTIME_DIR / "last_upload.pdf"
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(custom_path=str(dest))

            watched = list(self.settings.get("pairs", []))
            book = load_pdf_book(dest, source_name=doc.file_name or "upload.pdf", watched=watched)
            self.agent.set_pdf_book(book)

            if not book.ideas:
                await self._reply(
                    update,
                    f"PDF loaded (`{book.text_chars}` chars) but *no trade ideas* found.\n"
                    "Tip: use text like `EURUSD BUY SL 1.08 TP 1.10`.\n"
                    f"Preview: _{book.raw_preview[:200]}_",
                )
                return

            await self._reply(
                update,
                f"✅ PDF loaded — `{len(book.ideas)}` idea(s)\n\n"
                f"{book.summary()}\n\n"
                "Use `/pdftrade` to open the best idea, or wait for the 15m auto cycle.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("pdf upload failed")
            await self._reply(update, f"❌ PDF failed: `{exc}`")

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
        """Every 15 minutes: scan tech+PDF and open MT5 trades."""
        if not self.auto_enabled:
            logger.info("Auto-trade skipped (disabled)")
            return
        if self._quiet_hours():
            logger.info("Auto-trade skipped (quiet hours)")
            return

        place_orders = bool(self.settings.get("auto_trade_enabled", True))
        max_new = int(self.settings.get("max_new_trades_per_cycle", 1))

        try:
            self.agent.reload_pdf_book()
            self.broker.ensure()

            signals = self.agent.rank_for_trade(self.agent.actionable())
            pdf_n = len(self.agent.pdf_book.ideas) if self.agent.pdf_book else 0
            lines = [
                f"⏱ *15m auto cycle* @ `{datetime.now(timezone.utc).strftime('%H:%M UTC')}`",
                f"Actionable: `{len(signals)}` | PDF ideas: `{pdf_n}`",
            ]

            if not signals:
                lines.append("No trades — no signal edge.")
                await self._notify(context, "\n".join(lines))
                return

            opened = 0
            for sig in signals:
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
            "Starting Telegram bot | MT5 | auto=%s | pdf=%s",
            self.auto_enabled,
            bool(self.agent.pdf_book and self.agent.pdf_book.ideas),
        )
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
