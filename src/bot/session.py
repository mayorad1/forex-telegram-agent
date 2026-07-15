"""Per-user interactive session state (multi-step commands)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class UserSession:
    user_id: int
    # waiting for: pair | confirm | none
    waiting: Optional[str] = None
    pending_action: Optional[str] = None  # force_buy, force_sell, trade, price, signal, close_pair
    pending_side: Optional[str] = None
    pending_pair: Optional[str] = None
    last_text: str = ""
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def clear_pending(self) -> None:
        self.waiting = None
        self.pending_action = None
        self.pending_side = None
        self.pending_pair = None
        self.touch()

    def ask_pair(self, action: str, side: Optional[str] = None) -> None:
        self.waiting = "pair"
        self.pending_action = action
        self.pending_side = side
        self.touch()

    def ask_confirm(self, action: str, pair: str, side: Optional[str] = None) -> None:
        self.waiting = "confirm"
        self.pending_action = action
        self.pending_pair = pair
        self.pending_side = side
        self.touch()


class SessionStore:
    def __init__(self) -> None:
        self._users: dict[int, UserSession] = {}

    def get(self, user_id: int) -> UserSession:
        if user_id not in self._users:
            self._users[user_id] = UserSession(user_id=user_id)
        return self._users[user_id]
