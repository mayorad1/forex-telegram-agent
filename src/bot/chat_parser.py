"""Parse casual chat into bot intents — fuzzy, multi-phrase, no slash needed."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Optional

PAIR_WORDS = {
    "EURUSD": "EURUSD",
    "EUR/USD": "EURUSD",
    "EUROUSD": "EURUSD",
    "EURO": "EURUSD",
    "EUR": "EURUSD",
    "GBPUSD": "GBPUSD",
    "GBP/USD": "GBPUSD",
    "CABLE": "GBPUSD",
    "POUND": "GBPUSD",
    "STERLING": "GBPUSD",
    "GBP": "GBPUSD",
    "USDJPY": "USDJPY",
    "USD/JPY": "USDJPY",
    "DOLLAR YEN": "USDJPY",
    "YEN": "USDJPY",
    "JPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "AUD/USD": "AUDUSD",
    "AUSSIE": "AUDUSD",
    "AUSTRALIAN": "AUDUSD",
    "AUD": "AUDUSD",
    "USDCAD": "USDCAD",
    "USD/CAD": "USDCAD",
    "LOONIE": "USDCAD",
    "CAD": "USDCAD",
    "USDCHF": "USDCHF",
    "USD/CHF": "USDCHF",
    "SWISSY": "USDCHF",
    "CHF": "USDCHF",
    "NZDUSD": "NZDUSD",
    "NZD/USD": "NZDUSD",
    "KIWI": "NZDUSD",
    "NZD": "NZDUSD",
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "XAG/USD": "XAGUSD",
    "SILVER": "XAGUSD",
    "XAG": "XAGUSD",
    "GBPJPY": "GBPJPY",
    "GBP/JPY": "GBPJPY",
    "EURJPY": "EURJPY",
    "EUR/JPY": "EURJPY",
    "EURGBP": "EURGBP",
    "EUR/GBP": "EURGBP",
    "AUDJPY": "AUDJPY",
    "AUD/JPY": "AUDJPY",
}

AMBIGUOUS = {"EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "XAU", "XAG"}

# Keyword groups → intent name (scored by match quality)
# Each entry: (intent, keywords/phrases, needs_pair?)
INTENT_PHRASES: list[tuple[str, list[str], bool]] = [
    (
        "scan",
        [
            "signals",
            "signal",
            "scan",
            "setups",
            "opportunities",
            "what to trade",
            "give me signals",
            "show signals",
            "get signals",
            "market scan",
            "scan market",
            "any signals",
            "any setups",
            "trade ideas",
            "ideas",
            "outlook",
            "what looks good",
            "best pairs",
            "show me the market",
            "market overview",
            "fx signals",
            "forex signals",
        ],
        False,
    ),
    (
        "signal",
        [
            "analysis",
            "analyze",
            "analyse",
            "look at",
            "check",
            "chart",
            "view",
            "detail",
            "full signal",
            "what about",
            "how is",
            "hows",
            "how's",
        ],
        True,
    ),
    (
        "price",
        [
            "price",
            "quote",
            "rate",
            "how much",
            "current price",
            "live price",
            "spot",
            "now trading",
            "what is",
            "whats the price",
            "what's the price",
        ],
        True,
    ),
    (
        "force_buy",
        [
            "buy",
            "long",
            "go long",
            "open buy",
            "buy now",
            "purchase",
            "bullish",
            "buy for",
            "buy on",
            "enter buy",
            "place buy",
            "i want to buy",
            "please buy",
            "buy me",
            "get long",
        ],
        False,  # pair optional → need_pair
    ),
    (
        "force_sell",
        [
            "sell",
            "short",
            "go short",
            "open sell",
            "sell now",
            "bearish",
            "sell for",
            "sell on",
            "enter sell",
            "place sell",
            "i want to sell",
            "please sell",
            "sell me",
            "get short",
        ],
        False,
    ),
    (
        "trade",
        [
            "trade",
            "open trade",
            "enter",
            "execute",
            "take trade",
            "follow signal",
            "open position",
            "place trade",
            "do the trade",
        ],
        True,
    ),
    (
        "best_trade",
        [
            "best trade",
            "best signal",
            "top setup",
            "strongest",
            "best opportunity",
            "trade best",
            "pick one",
            "choose for me",
            "auto pick",
            "trade something",
            "just trade",
            "open best",
        ],
        False,
    ),
    (
        "positions",
        [
            "positions",
            "position",
            "open trades",
            "my trades",
            "open positions",
            "whats open",
            "what's open",
            "show trades",
            "active trades",
            "running trades",
            "my positions",
            "book",
        ],
        False,
    ),
    (
        "status",
        [
            "status",
            "account",
            "balance",
            "equity",
            "how am i",
            "pnl",
            "p/l",
            "profit",
            "loss",
            "performance",
            "summary",
            "dashboard",
            "report",
            "how much money",
            "account info",
        ],
        False,
    ),
    (
        "pairs",
        [
            "pairs",
            "pair list",
            "watchlist",
            "instruments",
            "what pairs",
            "which pairs",
            "markets",
            "symbols",
        ],
        False,
    ),
    (
        "closeall",
        [
            "close all",
            "close everything",
            "flatten",
            "exit all",
            "close all trades",
            "kill all",
            "square all",
            "close positions",
            "get out of all",
        ],
        False,
    ),
    (
        "close_pair",
        ["close", "exit", "close trade", "get out", "cut", "close position"],
        True,
    ),
    (
        "auto_on",
        [
            "auto on",
            "start auto",
            "enable auto",
            "auto trade on",
            "turn on auto",
            "start trading",
            "bot on",
            "enable trading",
            "activate auto",
        ],
        False,
    ),
    (
        "auto_off",
        [
            "auto off",
            "stop auto",
            "disable auto",
            "auto trade off",
            "turn off auto",
            "stop trading",
            "bot off",
            "pause auto",
            "deactivate auto",
        ],
        False,
    ),
    (
        "mt5",
        [
            "mt5",
            "reconnect",
            "connect mt5",
            "metatrader",
            "meta trader",
            "broker",
            "exness",
            "connect account",
            "login mt5",
            "fix mt5",
        ],
        False,
    ),
    (
        "pdfsignals",
        ["pdf signals", "show pdf", "pdf ideas", "pdf signal", "from pdf"],
        False,
    ),
    (
        "pdftrade",
        ["pdf trade", "trade pdf", "best pdf", "trade from pdf"],
        False,
    ),
    (
        "pdfclear",
        ["clear pdf", "pdf clear", "remove pdf", "delete pdf"],
        False,
    ),
    ("pdf_help", ["pdf", "upload pdf", "research pdf"], False),
    (
        "help",
        [
            "help",
            "hi",
            "hello",
            "hey",
            "menu",
            "commands",
            "what can you do",
            "how to",
            "start",
            "options",
            "?",
        ],
        False,
    ),
    (
        "lot",
        [
            "lot",
            "lots",
            "lot size",
            "set lot",
            "change lot",
            "volume",
            "size",
            "position size",
            "how many lots",
        ],
        False,
    ),
]


@dataclass
class ChatIntent:
    name: str
    pair: Optional[str] = None
    side: Optional[str] = None
    arg: Optional[str] = None
    raw: str = ""
    confidence: float = 1.0
    matched: str = ""  # phrase that matched (for feedback)


def _norm(text: str) -> str:
    t = text.strip().lower()
    t = t.replace("€", "eur").replace("£", "gbp").replace("$", " usd ")
    t = re.sub(r"[^\w\s/.\-]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def extract_pair(text: str) -> Optional[str]:
    u = text.upper()
    u = re.sub(r"\s+", " ", u)
    # multi-word first
    for k in ("DOLLAR YEN",):
        if k in u:
            return PAIR_WORDS[k]
    keys = sorted(PAIR_WORDS.keys(), key=len, reverse=True)
    for k in keys:
        if k in AMBIGUOUS or " " in k:
            continue
        if re.search(rf"(?<![A-Z]){re.escape(k)}(?![A-Z])", u):
            return PAIR_WORDS[k]
    m = re.search(r"\b([A-Z]{6})\b", u)
    if m:
        cand = m.group(1)
        if cand in set(PAIR_WORDS.values()):
            return cand
        # fuzzy 6-letter against known pairs
        known = list(set(PAIR_WORDS.values()))
        close = get_close_matches(cand, known, n=1, cutoff=0.8)
        if close:
            return close[0]
    for k in AMBIGUOUS:
        if re.search(rf"\b{k}\b", u):
            return PAIR_WORDS[k]
    # fuzzy word against pair nicknames
    words = re.findall(r"[A-Za-z]{3,10}", u)
    nick_keys = [k for k in PAIR_WORDS if len(k) >= 3]
    for w in words:
        close = get_close_matches(w, nick_keys, n=1, cutoff=0.85)
        if close:
            return PAIR_WORDS[close[0]]
    return None


def _phrase_in(text: str, phrase: str) -> bool:
    """True if phrase appears as whole words (order preserved)."""
    if " " not in phrase:
        return re.search(rf"\b{re.escape(phrase)}\b", text) is not None
    # multi-word: allow flexible spaces
    parts = phrase.split()
    pat = r"\b" + r"\s+".join(re.escape(p) for p in parts) + r"\b"
    return re.search(pat, text) is not None


def _fuzzy_keyword(text: str, keyword: str) -> bool:
    """Single-word fuzzy: ty po buyy -> buy."""
    if " " in keyword:
        return False
    words = text.split()
    for w in words:
        if w == keyword:
            return True
        if len(w) >= 3 and len(keyword) >= 3:
            if get_close_matches(w, [keyword], n=1, cutoff=0.8):
                return True
    return False


def parse_chat(text: str) -> Optional[ChatIntent]:
    """Detect intent from free text. Always tries hard before giving up."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    if raw.startswith("/"):
        return None

    low = _norm(raw)
    pair = extract_pair(raw)

    # Explicit lot size first: "lot 0.05", "0.1 lots", "set lot to 0.2"
    lot_m = re.search(
        r"\b(?:set\s+)?(?:lot|lots|volume)\b\s*(?:size\s*)?(?:to|=|:)?\s*([0-9]+(?:\.[0-9]+)?)",
        low,
    )
    if not lot_m:
        lot_m = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:lot|lots)\b", low)
    if lot_m:
        return ChatIntent("set_lot", arg=lot_m.group(1), raw=raw, matched="lot", confidence=1.0)

    # Score all intent phrases
    best: Optional[tuple[float, str, str, bool]] = None  # score, intent, matched, needs_pair

    for intent_name, phrases, needs_pair in INTENT_PHRASES:
        for ph in phrases:
            score = 0.0
            if _phrase_in(low, ph):
                # longer phrases win
                score = 10.0 + len(ph)
            elif _fuzzy_keyword(low, ph):
                score = 6.0 + len(ph) * 0.1
            else:
                continue
            # boost if pair present when relevant
            if pair and intent_name in {
                "signal",
                "price",
                "trade",
                "force_buy",
                "force_sell",
                "close_pair",
            }:
                score += 3.0
            if needs_pair and not pair:
                score -= 2.0
            if best is None or score > best[0]:
                best = (score, intent_name, ph, needs_pair)

    if best is None or best[0] < 5.0:
        # bare pair → signal
        if pair and len(low.split()) <= 3:
            return ChatIntent("signal", pair=pair, raw=raw, confidence=0.7, matched=pair)
        # suggest from fuzzy intent words
        return _suggest_unknown(raw, low, pair)

    score, intent_name, matched, needs_pair = best

    # Map force_buy/sell
    if intent_name == "force_buy":
        if pair:
            return ChatIntent(
                "force_trade", pair=pair, side="BUY", raw=raw, confidence=0.95, matched=matched
            )
        return ChatIntent("need_pair", side="BUY", raw=raw, matched=matched)
    if intent_name == "force_sell":
        if pair:
            return ChatIntent(
                "force_trade", pair=pair, side="SELL", raw=raw, confidence=0.95, matched=matched
            )
        return ChatIntent("need_pair", side="SELL", raw=raw, matched=matched)

    if intent_name == "auto_on":
        return ChatIntent("auto", arg="on", raw=raw, matched=matched)
    if intent_name == "auto_off":
        return ChatIntent("auto", arg="off", raw=raw, matched=matched)

    if intent_name == "lot":
        return ChatIntent("lot_menu", raw=raw, matched=matched)

    if intent_name in {"signal", "price", "trade", "close_pair"}:
        if not pair:
            # scan if they said signals without pair
            if intent_name == "signal" and re.search(r"\bsignals?\b", low):
                return ChatIntent("scan", raw=raw, matched=matched)
            return ChatIntent(
                "need_pair",
                arg=intent_name,
                raw=raw,
                matched=matched,
            )
        return ChatIntent(intent_name, pair=pair, raw=raw, confidence=0.9, matched=matched)

    # scan vs signal: "signals eurusd" → signal
    if intent_name == "scan" and pair:
        return ChatIntent("signal", pair=pair, raw=raw, matched=matched)

    return ChatIntent(intent_name, pair=pair, raw=raw, confidence=min(1.0, score / 15), matched=matched)


def _suggest_unknown(raw: str, low: str, pair: Optional[str]) -> ChatIntent:
    """Build helpful suggestions when intent unclear."""
    all_kw: list[str] = []
    for _, phrases, _ in INTENT_PHRASES:
        all_kw.extend(phrases)
    words = low.split()
    suggestions: list[str] = []
    for w in words:
        if len(w) < 3:
            continue
        close = get_close_matches(w, all_kw, n=2, cutoff=0.6)
        suggestions.extend(close)
    # unique preserve order
    seen = set()
    uniq = []
    for s in suggestions:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    hint = ", ".join(f"`{s}`" for s in uniq[:5]) if uniq else "`signals`, `buy eurusd`, `positions`"
    return ChatIntent(
        "unknown",
        pair=pair,
        raw=raw,
        arg=hint,
        confidence=0.0,
        matched="",
    )


# Examples shown in help
CHAT_EXAMPLES = """
*Just type naturally:*

*Signals*
`signals` · `give me signals` · `what to trade` · `setups`

*One pair*
`check gold` · `signal eurusd` · `how is gbpusd` · `eurusd`

*Price*
`price gold` · `quote eurusd` · `how much is yen`

*Trade now*
`buy eurusd` · `sell gold` · `buy for usdjpy` · `go long gbp`

*Strategy trade* (only if signal is good)
`trade eurusd` · `open eurusd`

*Best idea*
`best trade` · `pick one` · `open best`

*Account*
`positions` · `status` · `balance` · `pnl`

*Close*
`close all` · `close eurusd` · `flatten`

*Bot*
`auto on` · `auto off` · `mt5` · `reconnect`

*PDF*
`pdf signals` · `pdf trade` · send a PDF file

*Lot size*
`lot` · `lot 0.05` · `set lot 0.1` · `0.02 lots`

Typos are OK — I'll try to detect what you meant.
""".strip()
