"""Fetch forex / metal OHLC from Yahoo Finance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
import yfinance as yf

# Yahoo symbols for common FX and metals
YAHOO_SYMBOLS: dict[str, str] = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "USDCAD=X",
    "USDCHF": "USDCHF=X",
    "NZDUSD": "NZDUSD=X",
    "EURGBP": "EURGBP=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",  # gold futures proxy
    "XAGUSD": "SI=F",  # silver futures proxy
}


@dataclass
class Quote:
    pair: str
    price: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    change_pct: Optional[float] = None


def to_yahoo(pair: str) -> str:
    key = pair.upper().replace("/", "").replace("_", "")
    if key in YAHOO_SYMBOLS:
        return YAHOO_SYMBOLS[key]
    # assume already a yahoo ticker or XXXYYY form
    if key.endswith("=X") or key.endswith("=F"):
        return key
    return f"{key}=X"


def fetch_ohlc(
    pair: str,
    interval: str = "15m",
    period: str = "5d",
) -> pd.DataFrame:
    """Return OHLCV dataframe with lowercase columns."""
    symbol = to_yahoo(pair)
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval, auto_adjust=True)
    if df is None or df.empty:
        raise ValueError(f"No market data for {pair} ({symbol})")
    df = df.rename(columns=str.lower)
    # keep standard columns
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[cols].dropna()


def fetch_quote(pair: str) -> Quote:
    """Latest mid price for a pair."""
    df = fetch_ohlc(pair, interval="1m", period="1d")
    close = float(df["close"].iloc[-1])
    change_pct = None
    if len(df) >= 2:
        prev = float(df["close"].iloc[-2])
        if prev:
            change_pct = (close - prev) / prev * 100.0
    return Quote(pair=pair.upper().replace("/", ""), price=close, change_pct=change_pct)


def fetch_multi_quotes(pairs: list[str]) -> list[Quote]:
    out: list[Quote] = []
    for p in pairs:
        try:
            out.append(fetch_quote(p))
        except Exception as exc:  # noqa: BLE001
            out.append(Quote(pair=p, price=0.0, change_pct=None))
            # stash error in change_pct as None; callers can filter price==0
            _ = exc
    return out
