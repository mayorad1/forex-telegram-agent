"""Multi-indicator forex signal agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import pandas as pd

from src.agent.indicators import atr, ema, macd, rsi
from src.agent.pattern_learner import learn_from_candles
from src.agent.pdf_signals import (
    PdfSignalBook,
    ensure_pdf_loaded,
    load_saved_book,
)
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
        self.pattern_cfg = settings.get("patterns", {}) or {}
        watched = list(settings.get("pairs", []))
        self.pdf_book: Optional[PdfSignalBook] = ensure_pdf_loaded(watched=watched)

    def set_pdf_book(self, book: Optional[PdfSignalBook]) -> None:
        self.pdf_book = book

    def reload_pdf_book(self) -> Optional[PdfSignalBook]:
        """Reload from permanent disk storage (saved PDF / json)."""
        watched = list(self.settings.get("pairs", []))
        self.pdf_book = ensure_pdf_loaded(watched=watched)
        if self.pdf_book is None:
            self.pdf_book = load_saved_book()
        return self.pdf_book

    def analyze_pair(self, pair: str) -> Signal:
        # Longer history helps pattern learning
        hist = self.settings.get("pattern_history_period") or self.period
        if bool(self.pattern_cfg.get("enabled", True)):
            hist = self.pattern_cfg.get("history_period", hist)
        df = fetch_ohlc(pair, interval=self.timeframe, period=str(hist))
        sig = self.analyze_df(pair, df)
        sig = self.apply_pattern_learning(sig, df)
        return self.apply_pdf_bias(sig)

    def apply_pattern_learning(self, signal: Signal, df: pd.DataFrame) -> Signal:
        """Score candle patterns using historical outcomes; fill gaps when tech is weak."""
        if not bool(self.pattern_cfg.get("enabled", True)):
            return signal
        try:
            hint = learn_from_candles(df, self.pattern_cfg)
        except Exception as exc:  # noqa: BLE001
            signal.reasons.append(f"Pattern learn skip: {exc}")
            return signal

        for r in hint.reasons[:6]:
            signal.reasons.append(f"📚 {r}")

        if hint.side == "FLAT":
            return signal

        min_wr = float(self.pattern_cfg.get("min_win_rate", 0.55))
        # Boost when pattern agrees with tech
        if signal.side.value == hint.side:
            boost = int(self.pattern_cfg.get("agree_boost", 1))
            signal.score += boost
            signal.confidence = min(1.0, signal.confidence + 0.1 + hint.confidence * 0.2)
            signal.reasons.append(
                f"Pattern agrees ({hint.name} WR={hint.win_rate:.0%}) +{boost}"
            )
            return signal

        # Tech flat → allow pattern entry (PDF may still apply later)
        allow_fill = bool(self.pattern_cfg.get("fill_when_flat", True))
        if signal.side == Side.FLAT and allow_fill and hint.score >= int(
            self.pattern_cfg.get("min_pattern_score", 2)
        ):
            if hint.win_rate > 0 and hint.win_rate < min_wr and hint.samples >= int(
                self.pattern_cfg.get("min_samples", 10)
            ):
                signal.reasons.append("Pattern fill blocked — low historical WR")
                return signal
            signal.side = Side.BUY if hint.side == "BUY" else Side.SELL
            signal.score = max(hint.score, int(self.strat.get("min_score", 2)))
            signal.confidence = max(signal.confidence, hint.confidence)
            signal.reasons.append(
                f"Pattern entry: {hint.name} → {hint.side} (learned WR={hint.win_rate:.0%} n={hint.samples})"
            )
            # SL/TP from ATR if missing
            if signal.stop_loss is None and signal.atr and signal.price:
                sl_m = float(self.strat.get("sl_atr_mult", 1.5))
                tp_m = float(self.strat.get("tp_atr_mult", 2.5))
                if signal.side == Side.BUY:
                    signal.stop_loss = signal.price - signal.atr * sl_m
                    signal.take_profit = signal.price + signal.atr * tp_m
                else:
                    signal.stop_loss = signal.price + signal.atr * sl_m
                    signal.take_profit = signal.price - signal.atr * tp_m
            return signal

        # Conflict with tech — optional soft penalty
        if signal.side != Side.FLAT and hint.side != signal.side.value:
            if bool(self.pattern_cfg.get("penalize_conflict", True)):
                signal.score = max(0, signal.score - 1)
                signal.reasons.append(f"Pattern conflicts ({hint.side}) — score -1")
                if signal.score == 0:
                    signal.side = Side.FLAT
                    signal.confidence = 0.0
        return signal

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

        # RSI — only count extreme zones that align with pullback logic
        # (oversold only helps BUY if trend not strongly bearish when require_alignment)
        if rsi_v < rsi_os:
            buy_score += 1
            reasons.append(f"RSI oversold ({rsi_v:.1f})")
        elif rsi_v > rsi_ob:
            sell_score += 1
            reasons.append(f"RSI overbought ({rsi_v:.1f})")
        else:
            reasons.append(f"RSI neutral-ish ({rsi_v:.1f})")

        # MACD — require histogram on correct side (no weak "just positive")
        require_macd_rising = bool(s.get("require_macd_momentum", True))
        if require_macd_rising:
            if mh > 0 and mh >= mh_prev:
                buy_score += 1
                reasons.append("MACD hist ≥0 and not fading")
            elif mh < 0 and mh <= mh_prev:
                sell_score += 1
                reasons.append("MACD hist ≤0 and not fading")
            else:
                reasons.append("MACD momentum unclear")
        else:
            if mh > 0:
                buy_score += 1
                reasons.append("MACD hist positive")
            elif mh < 0:
                sell_score += 1
                reasons.append("MACD hist negative")

        # Optional: price vs slow EMA filter (trend quality)
        if bool(s.get("require_price_vs_ema_slow", True)):
            if price > es:
                buy_score += 0  # soft — used only as veto below
                reasons.append("Price above slow EMA")
            elif price < es:
                reasons.append("Price below slow EMA")

        if buy_score > sell_score:
            side = Side.BUY
            score = buy_score - sell_score
        elif sell_score > buy_score:
            side = Side.SELL
            score = sell_score - buy_score
        else:
            side = Side.FLAT
            score = 0
            reasons.append("No clear edge — stay flat")

        # Hard quality filters (reduce chop / counter-trend traps)
        if side != Side.FLAT and bool(s.get("require_ema_macd_agree", True)):
            ema_bull = ef > es
            macd_bull = mh > 0
            if side == Side.BUY and not (ema_bull and macd_bull):
                reasons.append("Blocked: BUY needs bullish EMA + MACD")
                side, score = Side.FLAT, 0
            elif side == Side.SELL and not ((not ema_bull) and (not macd_bull)):
                reasons.append("Blocked: SELL needs bearish EMA + MACD")
                side, score = Side.FLAT, 0

        if side == Side.BUY and bool(s.get("block_buy_if_rsi_overbought", True)) and rsi_v > rsi_ob:
            reasons.append("Blocked: RSI already overbought for BUY")
            side, score = Side.FLAT, 0
        if side == Side.SELL and bool(s.get("block_sell_if_rsi_oversold", True)) and rsi_v < rsi_os:
            reasons.append("Blocked: RSI already oversold for SELL")
            side, score = Side.FLAT, 0

        # Higher-timeframe trend filter (e.g. 1h)
        htf = s.get("htf_timeframe") or self.settings.get("htf_timeframe")
        if side != Side.FLAT and htf:
            try:
                htf_df = fetch_ohlc(pair, interval=str(htf), period=self.settings.get("htf_period", "30d"))
                htf_close = htf_df["close"]
                htf_fast = int(s.get("htf_ema_fast", 20))
                htf_slow = int(s.get("htf_ema_slow", 50))
                hf = float(ema(htf_close, htf_fast).iloc[-1])
                hs = float(ema(htf_close, htf_slow).iloc[-1])
                if side == Side.BUY and hf < hs:
                    reasons.append(f"Blocked: HTF {htf} bearish (EMA{htf_fast}<EMA{htf_slow})")
                    side, score = Side.FLAT, 0
                elif side == Side.SELL and hf > hs:
                    reasons.append(f"Blocked: HTF {htf} bullish (EMA{htf_fast}>EMA{htf_slow})")
                    side, score = Side.FLAT, 0
                else:
                    reasons.append(f"HTF {htf} aligned")
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"HTF filter skip: {exc}")

        if side == Side.BUY:
            sl = price - atr_v * sl_mult
            tp = price + atr_v * tp_mult
        elif side == Side.SELL:
            sl = price + atr_v * sl_mult
            tp = price - atr_v * tp_mult
        else:
            sl = tp = None

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
            if mode == "pdf_only" and not bool(self.pdf_cfg.get("allow_pattern_fallback", True)):
                signal.side = Side.FLAT
                signal.score = 0
                signal.confidence = 0.0
                signal.reasons.append("PDF-only mode: no idea for this pair")
            elif mode in {"pdf_only", "pdf_market"} and bool(
                self.pdf_cfg.get("allow_pattern_fallback", True)
            ):
                # Keep tech/pattern signal so we can trade without PDF
                if signal.side != Side.FLAT:
                    signal.reasons.append("No PDF idea — using market + learned candle patterns")
                else:
                    signal.reasons.append("No PDF idea and no pattern/tech edge")
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

        if mode in {"pdf_priority", "pdf_only", "pdf_market"}:
            # PDF sets direction; pdf_market also requires tech not against PDF
            tech_side = signal.side
            min_sc = int(self.strat.get("min_score", 2))
            pdf_sc = int(self.pdf_cfg.get("pdf_score", min_sc))

            if mode == "pdf_market":
                # Require market (tech) to agree with PDF
                if tech_side == Side.FLAT:
                    signal.side = Side.FLAT
                    signal.score = 0
                    signal.confidence = 0.0
                    signal.reasons.append("PDF+market: tech is FLAT — wait for market confirm")
                    return signal
                if tech_side != pdf_side:
                    signal.side = Side.FLAT
                    signal.score = 0
                    signal.confidence = 0.0
                    signal.reasons.append(
                        f"PDF+market: PDF {pdf_side.value} vs tech {tech_side.value} — blocked"
                    )
                    return signal
                signal.side = pdf_side
                signal.score = max(signal.score, pdf_sc)
                signal.confidence = min(1.0, max(signal.confidence, idea.confidence) + 0.1)
                signal.reasons.append("PDF+market: PDF and tech AGREE ✓")
            else:
                # pure PDF direction
                signal.side = pdf_side
                signal.score = max(signal.score, min_sc, pdf_sc)
                signal.confidence = max(signal.confidence, idea.confidence, 0.7)
                signal.reasons.append(f"PDF mode: following PDF {idea.side}")

            if signal.stop_loss is None and signal.atr and signal.price:
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
