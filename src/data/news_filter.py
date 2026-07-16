"""High-impact economic news / calendar filter before trading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

# Public Forex Factory weekly calendar mirror (widely used by retail tools)
FF_WEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Map our pairs → currencies that matter
PAIR_CURRENCIES: dict[str, set[str]] = {
    "EURUSD": {"EUR", "USD"},
    "GBPUSD": {"GBP", "USD"},
    "USDJPY": {"USD", "JPY"},
    "AUDUSD": {"AUD", "USD"},
    "USDCAD": {"USD", "CAD"},
    "USDCHF": {"USD", "CHF"},
    "NZDUSD": {"NZD", "USD"},
    "EURGBP": {"EUR", "GBP"},
    "EURJPY": {"EUR", "JPY"},
    "GBPJPY": {"GBP", "JPY"},
    "AUDJPY": {"AUD", "JPY"},
    "XAUUSD": {"USD", "XAU"},  # gold reacts strongly to USD / risk news
    "XAGUSD": {"USD", "XAG"},
}

# Country field in FF feed → currency
COUNTRY_CCY: dict[str, str] = {
    "USD": "USD",
    "United States": "USD",
    "EUR": "EUR",
    "European Monetary Union": "EUR",
    "Euro Zone": "EUR",
    "GBP": "GBP",
    "United Kingdom": "GBP",
    "JPY": "JPY",
    "Japan": "JPY",
    "AUD": "AUD",
    "Australia": "AUD",
    "CAD": "CAD",
    "Canada": "CAD",
    "CHF": "CHF",
    "Switzerland": "CHF",
    "NZD": "NZD",
    "New Zealand": "NZD",
    "CNY": "CNY",
    "China": "CNY",
}


@dataclass
class NewsEvent:
    title: str
    country: str
    currency: str
    impact: str  # High / Medium / Low
    when: datetime

    def pretty(self) -> str:
        t = self.when.astimezone(timezone.utc).strftime("%H:%M UTC")
        return f"`{t}` *{self.impact}* {self.currency} — {self.title[:60]}"


@dataclass
class NewsCheck:
    allowed: bool
    reason: str
    blocking: list[NewsEvent]
    upcoming: list[NewsEvent]


_cache: dict[str, Any] = {"ts": None, "events": []}


def _parse_ff_time(date_str: str, time_str: str) -> Optional[datetime]:
    """FF feed uses date like '07-16-2026' and time like '8:30am' or 'All Day'."""
    try:
        if not time_str or time_str.lower() in {"all day", "tentative", "tbd"}:
            # treat all-day as 00:00 UTC of that day (less useful; skip block unless high)
            dt = datetime.strptime(date_str.strip(), "%m-%d-%Y").replace(tzinfo=timezone.utc)
            return dt
        # combine
        raw = f"{date_str.strip()} {time_str.strip()}"
        for fmt in ("%m-%d-%Y %I:%M%p", "%m-%d-%Y %H:%M"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        # sometimes already has timezone in dateTime field
        return None
    except Exception:  # noqa: BLE001
        return None


def fetch_week_events(timeout: float = 12.0) -> list[NewsEvent]:
    """Fetch and cache this week's calendar (5 min cache)."""
    now = datetime.now(timezone.utc)
    if _cache["ts"] and (now - _cache["ts"]).total_seconds() < 300:
        return list(_cache["events"])

    events: list[NewsEvent] = []
    try:
        r = requests.get(FF_WEEK_URL, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise ValueError("unexpected calendar format")
        for row in data:
            title = str(row.get("title") or row.get("event") or "")
            country = str(row.get("country") or "")
            impact = str(row.get("impact") or "Low").capitalize()
            if impact not in {"High", "Medium", "Low", "Holiday"}:
                impact = "Low"
            # prefer dateTime ISO if present
            when: Optional[datetime] = None
            dt_raw = row.get("date") or row.get("dateTime") or ""
            if isinstance(dt_raw, str) and "T" in dt_raw:
                try:
                    when = datetime.fromisoformat(dt_raw.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    else:
                        when = when.astimezone(timezone.utc)
                except ValueError:
                    when = None
            if when is None:
                when = _parse_ff_time(
                    str(row.get("date", "")),
                    str(row.get("time", "") or ""),
                )
            if when is None:
                continue
            ccy = COUNTRY_CCY.get(country, country[:3].upper() if len(country) >= 3 else country)
            # FF often uses currency codes already as country field (USD, EUR…)
            if country in {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "CNY"}:
                ccy = country
            events.append(
                NewsEvent(
                    title=title,
                    country=country,
                    currency=ccy,
                    impact=impact if impact != "Holiday" else "Low",
                    when=when,
                )
            )
        _cache["ts"] = now
        _cache["events"] = events
        logger.info("Loaded %s calendar events", len(events))
    except Exception as exc:  # noqa: BLE001
        logger.warning("News calendar fetch failed: %s", exc)
        # fail-open or fail-closed controlled by caller
        return list(_cache["events"]) if _cache["events"] else []

    return events


def currencies_for_pair(pair: str) -> set[str]:
    p = pair.upper().replace("/", "").replace("_", "")
    if p in PAIR_CURRENCIES:
        return set(PAIR_CURRENCIES[p])
    if len(p) >= 6:
        return {p[:3], p[3:6]}
    return {"USD"}


def check_news_for_pair(
    pair: str,
    *,
    block_minutes_before: int = 30,
    block_minutes_after: int = 15,
    min_impact: str = "High",
    fail_closed: bool = False,
) -> NewsCheck:
    """
    Return whether trading this pair is allowed given nearby news.

    min_impact: High | Medium  (blocks that level and above)
    """
    now = datetime.now(timezone.utc)
    events = fetch_week_events()
    if not events:
        if fail_closed:
            return NewsCheck(
                False,
                "News calendar unavailable — trading blocked (fail-closed)",
                [],
                [],
            )
        return NewsCheck(True, "News calendar unavailable — allowing trade", [], [])

    impact_rank = {"Low": 1, "Medium": 2, "High": 3}
    need = impact_rank.get(min_impact.capitalize(), 3)
    ccys = currencies_for_pair(pair)

    window_start = now - timedelta(minutes=block_minutes_after)
    window_end = now + timedelta(minutes=block_minutes_before)

    blocking: list[NewsEvent] = []
    upcoming: list[NewsEvent] = []

    for ev in events:
        if impact_rank.get(ev.impact, 0) < need:
            continue
        if ev.currency not in ccys:
            continue

        # upcoming list (next 6h)
        if now <= ev.when <= now + timedelta(hours=6):
            upcoming.append(ev)

        if window_start <= ev.when <= window_end:
            blocking.append(ev)

    upcoming.sort(key=lambda e: e.when)
    blocking.sort(key=lambda e: e.when)

    if blocking:
        return NewsCheck(
            False,
            f"News blackout: {len(blocking)} high-impact event(s) near now for {pair}",
            blocking,
            upcoming[:5],
        )
    if upcoming:
        nxt = upcoming[0]
        mins = int((nxt.when - now).total_seconds() // 60)
        return NewsCheck(
            True,
            f"OK — next {nxt.impact} {nxt.currency} in ~{mins}m ({nxt.title[:40]})",
            [],
            upcoming[:5],
        )
    return NewsCheck(True, "OK — no major news soon for pair currencies", [], [])


def market_session_ok(
    *,
    avoid_rollover: bool = True,
    prefer_london_ny: bool = False,
) -> tuple[bool, str]:
    """Lightweight session / rollover check (UTC)."""
    hour = datetime.now(timezone.utc).hour
    if avoid_rollover and hour in {21, 22, 23, 0}:  # around 5pm NY / daily rollover zone
        return False, f"Rollover / thin liquidity window (UTC hour {hour})"
    if prefer_london_ny:
        # London ~07-16 UTC, NY ~12-21 UTC → overlap 12-16 best
        if not (7 <= hour <= 20):
            return False, f"Outside London/NY active hours (UTC {hour})"
    return True, "Session OK"
