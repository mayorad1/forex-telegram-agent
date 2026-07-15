"""Multi-indicator forex signal agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from src.agent.indicators import atr, ema, macd, rsi
from src.agent.pdf_signals import PdfSignalBook, PdfTradeIdea, load_saved_book
from src.data.market_data import fetch_ohlc


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"


@dataclass
class Signal:
    pair: str
    side: Side
    price: float
    score: int
    confidence: float  # 0..1
    reasons: list[str] = field(default_factory=list)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    atr: Optional[float] = None
    rsi: Optional[float] = None
    timeframe: str = "15m"

    def is_actionable(self, min_score: int = 2) -> bool:
        return self.side != Side.FLAT and self.score >= min_score

    def pretty(self) -> str:
        emoji = {"BUY": "🟢", "SELL": "🔴", "FLAT": "⚪"}[self.side.value]
        lines = [
            f"{emoji} *{self.pair}* — `{self.side.value}`",
            f"Price: `{self.price:.5f}`",
            f"Score: `{self.score}` | Confidence: `{self.confidence:.0%}`",
            f"TF: `{self.timeframe}`",
        ]
        if self.rsi is not None:
            lines.append(f"RSI: `{self.rsi:.1f}`")
        if self.stop_loss is not None:
            lines.append(f"SL: `{self.stop_loss:.5f}`")
        if self.take_profit is not None:
            lines.append(f"TP: `{self.take_profit:.5f}`")
        if self.reasons:
            lines.append("Reasons:")
            for r in self.reasons:
                lines.append(f"  • {r}")
        return "\n".join(lines)


class ForexAgent:
    """Score-based multi-indicator agent (EMA + RSI + MACD) + optional PDF bias."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.strat = settings.get("strategy", {})
        self.timeframe = settings.get("timeframe", "15m")
        self.period = settings.get("history_period", "5d")
        self.pdf_cfg = settings.get("pdf", {}) or {}
        self.pdf_book: Optional[PdfSignalBook] = load_saved_book()

    def set_pdf_book(self, book: Optional[PdfSignalBook]) -> None:
        self.pdf_book = book

    def reload_pdf_book(self) -> Optional[PdfSignalBook]:
        self.pdf_book = load_saved_book()
        return self.pdf_book

    def analyze_pair(self, pair: str) -> Signal:
        df = fetch_ohlc(pair, interval=self.timeframe, period=self.period)
        sig = self.analyze_df(pair, df)
        return self.apply_pdf_bias(sig)

    def analyze_df(self, pair: str, df: pd.DataFrame) -> Signal:
        s = self.strat
        close = df["close"]
        price = float(close.iloc[-1])

        rsi_period = int(s.get("rsi_period", 14))
        rsi_os = float(s.get("rsi_oversold", 30))
        rsi_ob = float(s.get("rsi_overbought", 70))
        ema_fast_n = int(s.get("ema_fast", 9))
        ema_slow_n = int(s.get("ema_slow", 21))
        macd_fast = int(s.get("macd_fast", 12))
        macd_slow = int(s.get("macd_slow", 26))
        macd_sig = int(s.get("macd_signal", 9))
        atr_period = int(s.get("atr_period", 14))
        sl_mult = float(s.get("sl_atr_mult", 1.5))
        tp_mult = float(s.get("tp_atr_mult", 2.5))

        rsi_s = rsi(close, rsi_period)
        ema_f = ema(close, ema_fast_n)
        ema_s = ema(close, ema_slow_n)
        macd_line, macd_signal, macd_hist = macd(close, macd_fast, macd_slow, macd_sig)
        atr_s = atr(df, atr_period)

        rsi_v = float(rsi_s.iloc[-1])
        atr_v = float(atr_s.iloc[-1])
        ef, es = float(ema_f.iloc[-1]), float(ema_s.iloc[-1])
        mh = float(macd_hist.iloc[-1])
        mh_prev = float(macd_hist.iloc[-2]) if len(macd_hist) > 1 else 0.0

        buy_score = 0
        sell_score = 0
        reasons: list[str] = []

        # EMA trend
        if ef > es:
            buy_score += 1
            reasons.append(f"EMA{ema_fast_n} > EMA{ema_slow_n} (bullish trend)")
        elif ef < es:
            sell_score += 1
            reasons.append(f"EMA{ema_fast_n} < EMA{ema_slow_n} (bearish trend)")

        # RSI
        if rsi_v < rsi_os:
            buy_score += 1
            reasons.append(f"RSI oversold ({rsi_v:.1f})")
        elif rsi_v > rsi_ob:
            sell_score += 1
            reasons.append(f"RSI overbought ({rsi_v:.1f})")
        elif 40 <= rsi_v <= 60:
            reasons.append(f"RSI neutral ({rsi_v:.1f})")
        else:
            if rsi_v > 50:
                buy_score += 0  # mild bias only via other signals
            reasons.append(f"RSI {rsi_v:.1f}")

        # MACD histogram momentum / cross
        if mh > 0 and mh > mh_prev:
            buy_score += 1
            reasons.append("MACD hist rising above 0")
        elif mh < 0 and mh < mh_prev:
            sell_score += 1
            reasons.append("MACD hist falling below 0")
        elif mh > 0:
            buy_score += 1
            reasons.append("MACD hist positive")
        elif mh < 0:
            sell_score += 1
            reasons.append("MACD hist negative")

        if buy_score > sell_score:
            side = Side.BUY
            score = buy_score - sell_score
            sl = price - atr_v * sl_mult
            tp = price + atr_v * tp_mult
        elif sell_score > buy_score:
            side = Side.SELL
            score = sell_score - buy_score
            sl = price + atr_v * sl_mult
            tp = price - atr_v * tp_mult
        else:
            side = Side.FLAT
            score = 0
            sl = tp = None
            reasons.append("No clear edge — stay flat")

        max_possible = 3
        confidence = min(1.0, score / max_possible) if side != Side.FLAT else 0.0

        return Signal(
            pair=pair.upper().replace("/", ""),
            side=side,
            price=price,
            score=score,
            confidence=confidence,
            reasons=reasons,
            stop_loss=sl,
            take_profit=tp,
            atr=atr_v,
            rsi=rsi_v,
            timeframe=self.timeframe,
        )

    def apply_pdf_bias(self, signal: Signal) -> Signal:
        """Merge PDF research ideas into technical signal."""
        if not bool(self.pdf_cfg.get("enabled", True)):
            return signal
        if self.pdf_book is None or not self.pdf_book.ideas:
            return signal

        idea = self.pdf_book.by_pair(signal.pair)
        mode = str(self.pdf_cfg.get("mode", "blend")).lower()  # blend | boost | pdf_priority
        boost = int(self.pdf_cfg.get("score_boost", 1))

        if idea is None:
            if mode == "pdf_only":
                signal.side = Side.FLAT
                signal.score = 0
                signal.confidence = 0.0
                signal.reasons.append("PDF-only mode: no idea for this pair")
            return signal

        pdf_side = Side.BUY if idea.side.upper() == "BUY" else Side.SELL
        signal.reasons.append(
            f"PDF ({self.pdf_book.source_name}): {idea.side} "
            f"— {idea.source_snippet[:80]}"
        )

        # Prefer PDF SL/TP when provided
        if bool(self.pdf_cfg.get("use_pdf_levels", True)):
            if idea.stop_loss is not None:
                signal.stop_loss = idea.stop_loss
            if idea.take_profit is not None:
                signal.take_profit = idea.take_profit

        if mode == "pdf_priority":
            # PDF direction wins; keep price/ATR from market
            signal.side = pdf_side
            signal.score = max(signal.score, int(self.strat.get("min_score", 2)))
            signal.confidence = max(signal.confidence, idea.confidence)
            if signal.stop_loss is None and signal.atr:
                mult_sl = float(self.strat.get("sl_atr_mult", 1.5))
                mult_tp = float(self.strat.get("tp_atr_mult", 2.5))
                if pdf_side == Side.BUY:
                    signal.stop_loss = signal.price - signal.atr * mult_sl
                    signal.take_profit = signal.price + signal.atr * mult_tp
                else:
                    signal.stop_loss = signal.price + signal.atr * mult_sl
                    signal.take_profit = signal.price - signal.atr * mult_tp
            return signal

        # blend / boost: reinforce agreement, weaken conflict
        if signal.side == pdf_side:
            signal.score += boost
            signal.confidence = min(1.0, signal.confidence + 0.15)
            signal.reasons.append(f"PDF agrees — score +{boost}")
        elif signal.side == Side.FLAT:
            # allow PDF to open a light technical-flat trade if allowed
            if bool(self.pdf_cfg.get("allow_pdf_when_flat", True)):
                signal.side = pdf_side
                signal.score = max(boost, int(self.strat.get("min_score", 2)) - 1)
                signal.confidence = idea.confidence
                signal.reasons.append("Tech flat — using PDF direction")
                if signal.stop_loss is None and signal.atr:
                    mult_sl = float(self.strat.get("sl_atr_mult", 1.5))
                    mult_tp = float(self.strat.get("tp_atr_mult", 2.5))
                    if pdf_side == Side.BUY:
                        signal.stop_loss = signal.price - signal.atr * mult_sl
                        signal.take_profit = signal.price + signal.atr * mult_tp
                    else:
                        signal.stop_loss = signal.price + signal.atr * mult_sl
                        signal.take_profit = signal.price - signal.atr * mult_tp
        else:
            # conflict
            if bool(self.pdf_cfg.get("block_on_conflict", False)):
                signal.side = Side.FLAT
                signal.score = 0
                signal.confidence = 0.0
                signal.reasons.append("PDF conflicts with tech — blocked")
            else:
                signal.score = max(0, signal.score - boost)
                signal.reasons.append(f"PDF conflicts — score -{boost}")
                if signal.score == 0:
                    signal.side = Side.FLAT
                    signal.confidence = 0.0

        max_possible = 3 + boost
        if signal.side != Side.FLAT:
            signal.confidence = min(1.0, signal.score / max_possible)
        return signal

    def scan(self, pairs: Optional[list[str]] = None) -> list[Signal]:
        pairs = pairs or list(self.settings.get("pairs", []))
        # include PDF-only pairs not in default list
        if self.pdf_book and bool(self.pdf_cfg.get("include_pdf_pairs", True)):
            extra = [i.pair for i in self.pdf_book.ideas if i.pair not in pairs]
            pairs = list(pairs) + extra
        results: list[Signal] = []
        for pair in pairs:
            try:
                results.append(self.analyze_pair(pair))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    Signal(
                        pair=pair,
                        side=Side.FLAT,
                        price=0.0,
                        score=0,
                        confidence=0.0,
                        reasons=[f"Data error: {exc}"],
                        timeframe=self.timeframe,
                    )
                )
        return results

    def actionable(self, pairs: Optional[list[str]] = None) -> list[Signal]:
        min_score = int(self.strat.get("min_score", 2))
        # slightly lower bar when PDF is loaded and mode is soft
        if self.pdf_book and self.pdf_book.ideas and bool(self.pdf_cfg.get("relax_min_score", False)):
            min_score = max(1, min_score - 1)
        sigs = [s for s in self.scan(pairs) if s.is_actionable(min_score)]
        # prefer pairs that appear in PDF when ranking later
        return sigs

    def rank_for_trade(self, signals: list[Signal]) -> list[Signal]:
        """Sort: PDF-backed first, then higher score."""
        pdf_pairs = set()
        if self.pdf_book:
            pdf_pairs = {i.pair for i in self.pdf_book.ideas}

        def key(s: Signal) -> tuple:
            in_pdf = 1 if s.pair in pdf_pairs else 0
            return (in_pdf, s.score, s.confidence)

        return sorted(signals, key=key, reverse=True)
