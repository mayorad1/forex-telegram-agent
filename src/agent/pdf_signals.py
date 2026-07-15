"""Extract trade ideas from research / signal PDFs."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.utils.config import RUNTIME_DIR

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None  # type: ignore


PAIR_ALIASES: dict[str, str] = {
    "EURUSD": "EURUSD",
    "EUR/USD": "EURUSD",
    "EUR-USD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "GBP/USD": "GBPUSD",
    "USDJPY": "USDJPY",
    "USD/JPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "AUD/USD": "AUDUSD",
    "USDCAD": "USDCAD",
    "USD/CAD": "USDCAD",
    "USDCHF": "USDCHF",
    "USD/CHF": "USDCHF",
    "NZDUSD": "NZDUSD",
    "NZD/USD": "NZDUSD",
    "EURGBP": "EURGBP",
    "EUR/GBP": "EURGBP",
    "EURJPY": "EURJPY",
    "EUR/JPY": "EURJPY",
    "GBPJPY": "GBPJPY",
    "GBP/JPY": "GBPJPY",
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "GOLD": "XAUUSD",
    "XAGUSD": "XAGUSD",
    "SILVER": "XAGUSD",
}


@dataclass
class PdfTradeIdea:
    pair: str
    side: str  # BUY / SELL
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    source_snippet: str = ""
    confidence: float = 0.6


@dataclass
class PdfSignalBook:
    source_name: str = ""
    loaded_at: str = ""
    text_chars: int = 0
    ideas: list[PdfTradeIdea] = field(default_factory=list)
    raw_preview: str = ""

    def by_pair(self, pair: str) -> Optional[PdfTradeIdea]:
        key = pair.upper().replace("/", "")
        for idea in self.ideas:
            if idea.pair == key:
                return idea
        return None

    def summary(self) -> str:
        if not self.ideas:
            return "No PDF trade ideas loaded. Send a PDF or use /pdf with a file."
        lines = [
            f"*PDF signals* — `{self.source_name}`",
            f"Loaded: `{self.loaded_at}`",
            f"Ideas: `{len(self.ideas)}`",
            "",
        ]
        for i, idea in enumerate(self.ideas, 1):
            lines.append(
                f"{i}. `{idea.pair}` *{idea.side}*"
                + (f" entry=`{idea.entry}`" if idea.entry else "")
                + (f" SL=`{idea.stop_loss}`" if idea.stop_loss else "")
                + (f" TP=`{idea.take_profit}`" if idea.take_profit else "")
            )
            if idea.source_snippet:
                snip = idea.source_snippet[:120].replace("\n", " ")
                lines.append(f"   _{snip}_")
        return "\n".join(lines)


def extract_text_from_pdf(path: Path, max_pages: int = 30) -> str:
    if PdfReader is None:
        raise RuntimeError("pypdf not installed. Run: pip install pypdf")
    reader = PdfReader(str(path))
    chunks: list[str] = []
    for i, page in enumerate(reader.pages[:max_pages]):
        try:
            t = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            t = ""
        if t.strip():
            chunks.append(t)
    text = "\n".join(chunks)
    if not text.strip():
        raise ValueError(
            "Could not extract text from PDF (scanned image PDF?). "
            "Use a text-based PDF with pairs like EURUSD BUY."
        )
    return text


_CCY = {
    "USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "XAU", "XAG",
}


def _normalize_pair_token(tok: str) -> Optional[str]:
    t = tok.upper().replace(" ", "")
    if t in PAIR_ALIASES:
        return PAIR_ALIASES[t]
    t2 = t.replace("/", "").replace("-", "")
    if t2 in PAIR_ALIASES:
        return PAIR_ALIASES[t2]
    # only accept real FX metal codes (not random 6-letter words)
    if re.fullmatch(r"[A-Z]{6}", t2):
        a, b = t2[:3], t2[3:]
        if a in _CCY and b in _CCY and a != b:
            return t2
    return None


def _find_side(window: str) -> Optional[str]:
    w = window.upper()
    # order matters: longer phrases first
    if re.search(r"\b(STRONG\s+BUY|BUY\s+NOW|LONG|BUY|BULLISH)\b", w):
        if re.search(r"\b(DO\s+NOT\s+BUY|AVOID\s+BUY)\b", w):
            return None
        return "BUY"
    if re.search(r"\b(STRONG\s+SELL|SELL\s+NOW|SHORT|SELL|BEARISH)\b", w):
        if re.search(r"\b(DO\s+NOT\s+SELL|AVOID\s+SELL)\b", w):
            return None
        return "SELL"
    return None


def _find_levels(window: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return entry, sl, tp if present near the idea."""
    entry = sl = tp = None

    def first_num(patterns: list[str]) -> Optional[float]:
        for pat in patterns:
            m = re.search(pat, window, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    continue
        return None

    entry = first_num(
        [
            r"(?:entry|enter|at|@)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
            r"\b([0-9]+\.[0-9]{3,5})\b",  # last resort weak
        ]
    )
    # Only trust naked number as entry if labeled better — reset if only weak match
    if entry and not re.search(r"(entry|enter|at|@)", window, re.I):
        entry = None

    sl = first_num(
        [
            r"(?:stop\s*loss|stop-loss|\bsl\b)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
            r"(?:stop)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        ]
    )
    tp = first_num(
        [
            r"(?:take\s*profit|take-profit|\btp\b|\btarget\b)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)",
        ]
    )
    return entry, sl, tp


def parse_trade_ideas(text: str, watched: Optional[list[str]] = None) -> list[PdfTradeIdea]:
    """Heuristic parse of free-text PDF content into trade ideas."""
    _ = watched  # reserved for future filtering
    ideas: dict[str, PdfTradeIdea] = {}

    pair_pat = re.compile(
        r"\b("
        r"EUR/?USD|GBP/?USD|USD/?JPY|AUD/?USD|USD/?CAD|USD/?CHF|NZD/?USD|"
        r"EUR/?GBP|EUR/?JPY|GBP/?JPY|XAU/?USD|XAG/?USD|GOLD|SILVER"
        r")\b",
        re.IGNORECASE,
    )

    # Prefer line-by-line: side + pair + levels on same / nearby lines
    lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]

    def consider(pair: str, side: str, context: str) -> None:
        entry, sl, tp = _find_levels(context)
        conf = 0.55
        if sl or tp:
            conf = 0.75
        if entry:
            conf = min(0.9, conf + 0.1)
        idea = PdfTradeIdea(
            pair=pair,
            side=side,
            entry=entry,
            stop_loss=sl,
            take_profit=tp,
            source_snippet=context.strip()[:160],
            confidence=conf,
        )
        prev = ideas.get(pair)
        if prev is None or idea.confidence >= prev.confidence:
            ideas[pair] = idea

    # 1) Side-first: BUY EURUSD / SELL GBP/USD (same line)
    side_first = re.compile(
        r"\b(BUY|SELL|LONG|SHORT)\b\s+(EUR/?USD|GBP/?USD|USD/?JPY|AUD/?USD|USD/?CAD|"
        r"USD/?CHF|NZD/?USD|EUR/?GBP|EUR/?JPY|GBP/?JPY|XAU/?USD|XAG/?USD|GOLD|SILVER)\b",
        re.IGNORECASE,
    )
    for line in lines:
        for m in side_first.finditer(line):
            side_raw = m.group(1).upper()
            side = "BUY" if side_raw in {"BUY", "LONG"} else "SELL"
            pair = _normalize_pair_token(m.group(2))
            if pair:
                consider(pair, side, line)

    # 2) Pair-first: EURUSD BUY / Gold LONG (same line)
    pair_side = re.compile(
        r"\b(EUR/?USD|GBP/?USD|USD/?JPY|AUD/?USD|USD/?CAD|USD/?CHF|NZD/?USD|"
        r"EUR/?GBP|EUR/?JPY|GBP/?JPY|XAU/?USD|XAG/?USD|GOLD|SILVER)\b"
        r".{0,40}?\b(BUY|SELL|LONG|SHORT|BULLISH|BEARISH)\b",
        re.IGNORECASE,
    )
    for line in lines:
        for m in pair_side.finditer(line):
            pair = _normalize_pair_token(m.group(1))
            side_raw = m.group(2).upper()
            if side_raw in {"BUY", "LONG", "BULLISH"}:
                side = "BUY"
            else:
                side = "SELL"
            if pair:
                consider(pair, side, line)

    # 3) Fallback: pair on line + side keyword nearby (same line only)
    for line in lines:
        pm = pair_pat.search(line)
        if not pm:
            continue
        pair = _normalize_pair_token(pm.group(1))
        if not pair:
            continue
        side = _find_side(line)
        if side:
            consider(pair, side, line)

    return list(ideas.values())


def load_pdf_book(path: Path, source_name: str, watched: Optional[list[str]] = None) -> PdfSignalBook:
    text = extract_text_from_pdf(path)
    ideas = parse_trade_ideas(text, watched=watched)
    book = PdfSignalBook(
        source_name=source_name,
        loaded_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        text_chars=len(text),
        ideas=ideas,
        raw_preview=text[:500].replace("\n", " "),
    )
    save_book(book)
    return book


def save_book(book: PdfSignalBook) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "source_name": book.source_name,
        "loaded_at": book.loaded_at,
        "text_chars": book.text_chars,
        "raw_preview": book.raw_preview,
        "ideas": [asdict(i) for i in book.ideas],
    }
    (RUNTIME_DIR / "pdf_signals.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def load_saved_book() -> Optional[PdfSignalBook]:
    path = RUNTIME_DIR / "pdf_signals.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        ideas = [PdfTradeIdea(**i) for i in raw.get("ideas", [])]
        return PdfSignalBook(
            source_name=raw.get("source_name", ""),
            loaded_at=raw.get("loaded_at", ""),
            text_chars=int(raw.get("text_chars", 0)),
            ideas=ideas,
            raw_preview=raw.get("raw_preview", ""),
        )
    except Exception:  # noqa: BLE001
        return None


def clear_book() -> None:
    path = RUNTIME_DIR / "pdf_signals.json"
    if path.exists():
        path.unlink()
