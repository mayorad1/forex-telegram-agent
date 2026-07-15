"""Parse casual chat into bot intents (no slash required)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Common pairs users type
PAIR_WORDS = {
    "EURUSD": "EURUSD",
    "EUR/USD": "EURUSD",
    "EURO": "EURUSD",
    "EUR": "EURUSD",  # only if alone with buy/sell — handled carefully
    "GBPUSD": "GBPUSD",
    "GBP/USD": "GBPUSD",
    "CABLE": "GBPUSD",
    "POUND": "GBPUSD",
    "GBP": "GBPUSD",
    "USDJPY": "USDJPY",
    "USD/JPY": "USDJPY",
    "YEN": "USDJPY",
    "JPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "AUD/USD": "AUDUSD",
    "AUSSIE": "AUDUSD",
    "AUD": "AUDUSD",
    "USDCAD": "USDCAD",
    "USD/CAD": "USDCAD",
    "CAD": "USDCAD",
    "USDCHF": "USDCHF",
    "USD/CHF": "USDCHF",
    "CHF": "USDCHF",
    "NZDUSD": "NZDUSD",
    "NZD/USD": "NZDUSD",
    "NZD": "NZDUSD",
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "SILVER": "XAGUSD",
    "GBPJPY": "GBPJPY",
    "GBP/JPY": "GBPJPY",
    "EURJPY": "EURJPY",
    "EUR/JPY": "EURJPY",
    "EURGBP": "EURGBP",
    "EUR/GBP": "EURGBP",
}

# Ambiguous 3-letter codes only valid near buy/sell/price/signal
AMBIGUOUS = {"EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "XAU"}


@dataclass
class ChatIntent:
    name: str
    pair: Optional[str] = None
    side: Optional[str] = None  # BUY / SELL for force trade
    arg: Optional[str] = None
    raw: str = ""


def _norm(text: str) -> str:
    t = text.strip()
    t = re.sub(r"[^\w\s/.\-]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_pair(text: str) -> Optional[str]:
    u = text.upper()
    # longest match first
    keys = sorted(PAIR_WORDS.keys(), key=len, reverse=True)
    for k in keys:
        if k in AMBIGUOUS:
            continue
        # word boundary-ish
        if re.search(rf"(?<![A-Z]){re.escape(k)}(?![A-Z])", u):
            return PAIR_WORDS[k]
    # 6-letter continuous
    m = re.search(r"\b([A-Z]{6})\b", u)
    if m and m.group(1) in PAIR_WORDS.values():
        return m.group(1)
    # ambiguous only if clearly FX context
    for k in AMBIGUOUS:
        if re.search(rf"\b{k}\b", u):
            return PAIR_WORDS[k]
    return None


def parse_chat(text: str) -> Optional[ChatIntent]:
    """Return intent from free text, or None if not understood."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    # ignore pure slash commands (handled elsewhere)
    if raw.startswith("/"):
        return None

    n = _norm(raw)
    low = n.lower()
    pair = extract_pair(n)

    # --- help ---
    if re.fullmatch(r"(help|hi|hello|hey|menu|commands|what can you do)\b.*", low):
        return ChatIntent("help", raw=raw)

    # --- signals / scan ---
    if re.search(
        r"\b(signals?|signal list|give me signals?|show signals?|get signals?|"
        r"market scan|scan market|scan|opportunities|what to trade|any setups?)\b",
        low,
    ):
        if pair:
            return ChatIntent("signal", pair=pair, raw=raw)
        return ChatIntent("scan", raw=raw)

    # signal for pair: "signal eurusd", "analysis on gold"
    if re.search(r"\b(signal|analysis|analyze|look at|check)\b", low) and pair:
        return ChatIntent("signal", pair=pair, raw=raw)

    # --- price ---
    if re.search(r"\b(price|quote|how much|rate|current)\b", low) and pair:
        return ChatIntent("price", pair=pair, raw=raw)
    if re.search(r"\b(price|quote)\b", low) and not pair:
        return ChatIntent("help", raw=raw)  # ask which pair

    # --- buy / sell force ---
    buy_m = re.search(
        r"\b(buy|long|go long|open buy|buy now|purchase)\b(?:\s+(?:for|on|the)?)?\s*(.+)?",
        low,
    )
    sell_m = re.search(
        r"\b(sell|short|go short|open sell|sell now)\b(?:\s+(?:for|on|the)?)?\s*(.+)?",
        low,
    )
    if buy_m or sell_m:
        side = "BUY" if buy_m else "SELL"
        rest = (buy_m.group(2) if buy_m else sell_m.group(2)) or ""
        p = extract_pair(rest) or pair
        if p:
            return ChatIntent("force_trade", pair=p, side=side, raw=raw)
        return ChatIntent("need_pair", side=side, raw=raw)

    # "trade eurusd" / "open eurusd" = strategy trade
    if re.search(r"\b(trade|open|enter|execute)\b", low) and pair:
        return ChatIntent("trade", pair=pair, raw=raw)

    # --- positions / status ---
    if re.search(
        r"\b(positions?|open trades?|my trades?|open positions?|what.?s open)\b",
        low,
    ):
        return ChatIntent("positions", raw=raw)

    if re.search(r"\b(status|account|balance|equity|how am i)\b", low):
        return ChatIntent("status", raw=raw)

    if re.search(r"\b(pairs?|what pairs|watchlist|instruments)\b", low):
        return ChatIntent("pairs", raw=raw)

    if re.search(r"\b(close all|close everything|flatten|exit all)\b", low):
        return ChatIntent("closeall", raw=raw)

    if re.search(r"\b(close|exit)\b", low) and pair:
        return ChatIntent("close_pair", pair=pair, raw=raw)

    # --- auto ---
    if re.search(r"\b(auto\s*on|start auto|enable auto|auto trade on)\b", low):
        return ChatIntent("auto", arg="on", raw=raw)
    if re.search(r"\b(auto\s*off|stop auto|disable auto|auto trade off)\b", low):
        return ChatIntent("auto", arg="off", raw=raw)

    # --- mt5 ---
    if re.search(r"\b(mt5|reconnect|connect mt5|meta ?trader)\b", low):
        return ChatIntent("mt5", raw=raw)

    # --- pdf ---
    if re.search(r"\b(pdf signals?|show pdf|pdf ideas)\b", low):
        return ChatIntent("pdfsignals", raw=raw)
    if re.search(r"\b(pdf trade|trade pdf|best pdf)\b", low):
        return ChatIntent("pdftrade", raw=raw)
    if re.search(r"\b(clear pdf|pdf clear|remove pdf)\b", low):
        return ChatIntent("pdfclear", raw=raw)
    if re.search(r"\bpdf\b", low):
        return ChatIntent("pdf_help", raw=raw)

    # bare pair only → treat as signal request
    if pair and len(low.split()) <= 2:
        return ChatIntent("signal", pair=pair, raw=raw)

    return None
