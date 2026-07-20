"""
Learn from historical candle patterns to predict next trade direction.

Two layers:
1) Classic candlestick patterns on the latest bar(s)
2) Historical win-rate of those patterns + short sequence fingerprint matching
   (what usually happened after similar candle sequences in the past)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd


@dataclass
class PatternHint:
    side: str  # BUY / SELL / FLAT
    score: int
    confidence: float
    name: str
    win_rate: float
    samples: int
    reasons: list[str]


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def _is_bull(o: float, c: float) -> bool:
    return c > o


def _is_bear(o: float, c: float) -> bool:
    return c < o


def detect_latest_patterns(df: pd.DataFrame) -> list[tuple[str, str, int]]:
    """
    Return list of (pattern_name, side BUY/SELL, weight 1-2) on the last candle(s).
    """
    if len(df) < 5:
        return []
    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    i = len(df) - 1
    i1, i2 = i - 1, i - 2
    patterns: list[tuple[str, str, int]] = []

    # --- single candle ---
    body = _body(o.iloc[i], c.iloc[i])
    rng = _range(h.iloc[i], l.iloc[i])
    upper = h.iloc[i] - max(o.iloc[i], c.iloc[i])
    lower = min(o.iloc[i], c.iloc[i]) - l.iloc[i]

    # Doji
    if body / rng < 0.1:
        patterns.append(("doji", "FLAT", 0))

    # Hammer (bullish rejection)
    if lower >= 2 * body and upper <= body * 0.5 and body / rng < 0.4:
        patterns.append(("hammer", "BUY", 2))

    # Shooting star (bearish rejection)
    if upper >= 2 * body and lower <= body * 0.5 and body / rng < 0.4:
        patterns.append(("shooting_star", "SELL", 2))

    # Marubozu-ish
    if body / rng > 0.75:
        if _is_bull(o.iloc[i], c.iloc[i]):
            patterns.append(("bull_marubozu", "BUY", 1))
        else:
            patterns.append(("bear_marubozu", "SELL", 1))

    # --- two candle ---
    # Bullish engulfing
    if (
        _is_bear(o.iloc[i1], c.iloc[i1])
        and _is_bull(o.iloc[i], c.iloc[i])
        and c.iloc[i] >= o.iloc[i1]
        and o.iloc[i] <= c.iloc[i1]
    ):
        patterns.append(("bullish_engulfing", "BUY", 2))

    # Bearish engulfing
    if (
        _is_bull(o.iloc[i1], c.iloc[i1])
        and _is_bear(o.iloc[i], c.iloc[i])
        and c.iloc[i] <= o.iloc[i1]
        and o.iloc[i] >= c.iloc[i1]
    ):
        patterns.append(("bearish_engulfing", "SELL", 2))

    # Piercing line
    mid1 = (o.iloc[i1] + c.iloc[i1]) / 2
    if (
        _is_bear(o.iloc[i1], c.iloc[i1])
        and _is_bull(o.iloc[i], c.iloc[i])
        and o.iloc[i] < c.iloc[i1]
        and c.iloc[i] > mid1
        and c.iloc[i] < o.iloc[i1]
    ):
        patterns.append(("piercing_line", "BUY", 2))

    # Dark cloud
    if (
        _is_bull(o.iloc[i1], c.iloc[i1])
        and _is_bear(o.iloc[i], c.iloc[i])
        and o.iloc[i] > c.iloc[i1]
        and c.iloc[i] < mid1
        and c.iloc[i] > o.iloc[i1]
    ):
        patterns.append(("dark_cloud", "SELL", 2))

    # --- three candle ---
    # Morning star (simplified)
    if (
        _is_bear(o.iloc[i2], c.iloc[i2])
        and _body(o.iloc[i1], c.iloc[i1]) < _body(o.iloc[i2], c.iloc[i2]) * 0.5
        and _is_bull(o.iloc[i], c.iloc[i])
        and c.iloc[i] > (o.iloc[i2] + c.iloc[i2]) / 2
    ):
        patterns.append(("morning_star", "BUY", 2))

    # Evening star
    if (
        _is_bull(o.iloc[i2], c.iloc[i2])
        and _body(o.iloc[i1], c.iloc[i1]) < _body(o.iloc[i2], c.iloc[i2]) * 0.5
        and _is_bear(o.iloc[i], c.iloc[i])
        and c.iloc[i] < (o.iloc[i2] + c.iloc[i2]) / 2
    ):
        patterns.append(("evening_star", "SELL", 2))

    # Three white soldiers / three black crows (simplified)
    if all(_is_bull(o.iloc[j], c.iloc[j]) for j in (i2, i1, i)) and c.iloc[i] > c.iloc[i1] > c.iloc[i2]:
        patterns.append(("three_white_soldiers", "BUY", 2))
    if all(_is_bear(o.iloc[j], c.iloc[j]) for j in (i2, i1, i)) and c.iloc[i] < c.iloc[i1] < c.iloc[i2]:
        patterns.append(("three_black_crows", "SELL", 2))

    return [(n, s, w) for n, s, w in patterns if s in {"BUY", "SELL"}]


def _pattern_at_index(df: pd.DataFrame, i: int) -> list[tuple[str, str]]:
    """Detect patterns ending at index i (need i>=2)."""
    if i < 2 or i >= len(df):
        return []
    sub = df.iloc[: i + 1].copy()
    # reuse detector by slicing last rows
    found = detect_latest_patterns(sub)
    return [(n, s) for n, s, _w in found]


def historical_pattern_stats(
    df: pd.DataFrame,
    horizon: int = 6,
    min_move_atr: float = 0.15,
) -> dict[str, dict[str, float]]:
    """
    For each pattern type, measure historical win rate:
    after pattern at bar i, did price move in predicted direction by horizon bars?
    """
    from src.agent.indicators import atr as atr_fn

    if len(df) < 40:
        return {}
    atr_s = atr_fn(df, 14)
    stats: dict[str, dict[str, float]] = {}

    for i in range(3, len(df) - horizon - 1):
        pats = _pattern_at_index(df, i)
        if not pats:
            continue
        entry = float(df["close"].iloc[i])
        future = float(df["close"].iloc[i + horizon])
        atr_v = float(atr_s.iloc[i]) if not np.isnan(atr_s.iloc[i]) else 0.0
        if atr_v <= 0:
            continue
        move = future - entry
        for name, side in pats:
            key = f"{name}:{side}"
            bucket = stats.setdefault(key, {"wins": 0.0, "n": 0.0, "sum_r": 0.0})
            bucket["n"] += 1
            if side == "BUY":
                r = move / atr_v
                win = move > atr_v * min_move_atr
            else:
                r = -move / atr_v
                win = move < -atr_v * min_move_atr
            if win:
                bucket["wins"] += 1
            bucket["sum_r"] += r

    out: dict[str, dict[str, float]] = {}
    for k, v in stats.items():
        n = max(v["n"], 1.0)
        out[k] = {
            "win_rate": v["wins"] / n,
            "samples": v["n"],
            "avg_r": v["sum_r"] / n,
        }
    return out


def candle_fingerprint(df: pd.DataFrame, length: int = 5) -> str:
    """Encode last N candles as U/D/F by body direction + relative size bucket."""
    if len(df) < length:
        return ""
    parts = []
    for i in range(len(df) - length, len(df)):
        o = float(df["open"].iloc[i])
        c = float(df["close"].iloc[i])
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        d = "U" if c > o else ("D" if c < o else "F")
        body = abs(c - o)
        rng = max(h - l, 1e-12)
        size = "S" if body / rng < 0.33 else ("L" if body / rng > 0.66 else "M")
        parts.append(d + size)
    return "-".join(parts)


def sequence_bias(
    df: pd.DataFrame,
    length: int = 5,
    horizon: int = 6,
    min_samples: int = 8,
) -> Optional[tuple[str, float, int]]:
    """
    Find historical occurrences of the same candle fingerprint;
    return (side, win_rate, samples) for the bias of next move.
    """
    if len(df) < length + horizon + 20:
        return None
    target = candle_fingerprint(df, length)
    if not target:
        return None

    ups = downs = 0
    for i in range(length, len(df) - horizon):
        window = df.iloc[: i + 1]
        fp = candle_fingerprint(window, length)
        if fp != target:
            continue
        entry = float(df["close"].iloc[i])
        future = float(df["close"].iloc[i + horizon])
        if future > entry:
            ups += 1
        elif future < entry:
            downs += 1
    n = ups + downs
    if n < min_samples:
        return None
    if ups > downs * 1.15:
        return "BUY", ups / n, n
    if downs > ups * 1.15:
        return "SELL", downs / n, n
    return None


def learn_from_candles(
    df: pd.DataFrame,
    cfg: Optional[dict[str, Any]] = None,
) -> PatternHint:
    """Main entry: produce a pattern-based trade hint from history."""
    cfg = cfg or {}
    if not bool(cfg.get("enabled", True)):
        return PatternHint("FLAT", 0, 0.0, "off", 0.0, 0, ["Pattern learning disabled"])

    min_wr = float(cfg.get("min_win_rate", 0.55))
    min_samples = int(cfg.get("min_samples", 10))
    horizon = int(cfg.get("horizon_bars", 6))
    seq_len = int(cfg.get("sequence_length", 5))
    min_score = int(cfg.get("min_pattern_score", 2))

    reasons: list[str] = []
    latest = detect_latest_patterns(df)
    stats = historical_pattern_stats(df, horizon=horizon)

    buy_pts = 0
    sell_pts = 0
    best_name = "none"
    best_wr = 0.0
    best_n = 0

    for name, side, weight in latest:
        key = f"{name}:{side}"
        st = stats.get(key, {})
        wr = float(st.get("win_rate", 0.5))
        n = int(st.get("samples", 0))
        reasons.append(f"Pattern `{name}` → {side} (hist WR={wr:.0%} n={n})")
        # discount patterns with poor history
        if n >= min_samples and wr < min_wr:
            reasons.append(f"  skipped `{name}` — weak history")
            continue
        if n >= min_samples and wr >= min_wr:
            pts = weight + (1 if wr >= 0.6 else 0)
        else:
            # new/rare pattern — half weight
            pts = max(1, weight - 1)
        if side == "BUY":
            buy_pts += pts
        else:
            sell_pts += pts
        if wr >= best_wr and n >= best_n * 0.5:
            best_wr, best_n, best_name = wr, n, name

    # Sequence fingerprint learning
    seq = sequence_bias(df, length=seq_len, horizon=horizon, min_samples=min_samples)
    if seq:
        side_s, wr_s, n_s = seq
        reasons.append(
            f"Sequence match last {seq_len} candles → {side_s} (WR={wr_s:.0%} n={n_s})"
        )
        if wr_s >= min_wr:
            if side_s == "BUY":
                buy_pts += 2
            else:
                sell_pts += 2
            if wr_s > best_wr:
                best_wr, best_n, best_name = wr_s, n_s, f"seq_{seq_len}"

    if buy_pts > sell_pts and buy_pts >= min_score:
        side = "BUY"
        score = buy_pts - sell_pts
    elif sell_pts > buy_pts and sell_pts >= min_score:
        side = "SELL"
        score = sell_pts - buy_pts
    else:
        side = "FLAT"
        score = 0
        reasons.append("No strong learned pattern edge")

    conf = 0.0
    if side != "FLAT":
        conf = min(0.95, 0.4 + best_wr * 0.4 + min(score, 3) * 0.1)

    return PatternHint(
        side=side,
        score=score,
        confidence=conf,
        name=best_name,
        win_rate=best_wr,
        samples=best_n,
        reasons=reasons,
    )
