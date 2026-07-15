"""Telegram reply keyboards for interactive trading bot."""

from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Main always-on menu
MAIN_ROWS = [
    [KeyboardButton("📡 Signals"), KeyboardButton("⭐ Best trade")],
    [KeyboardButton("📂 Positions"), KeyboardButton("💰 Status")],
    [KeyboardButton("🟢 Buy"), KeyboardButton("🔴 Sell")],
    [KeyboardButton("📋 Pairs"), KeyboardButton("💲 Price")],
    [KeyboardButton("🤖 Auto ON"), KeyboardButton("⏸ Auto OFF")],
    [KeyboardButton("🔌 MT5"), KeyboardButton("📄 PDF")],
    [KeyboardButton("🧹 Close all"), KeyboardButton("❓ Help")],
]

PAIR_ROWS = [
    [KeyboardButton("EURUSD"), KeyboardButton("GBPUSD"), KeyboardButton("USDJPY")],
    [KeyboardButton("AUDUSD"), KeyboardButton("USDCAD"), KeyboardButton("XAUUSD")],
    [KeyboardButton("NZDUSD"), KeyboardButton("USDCHF"), KeyboardButton("GBPJPY")],
    [KeyboardButton("⬅️ Menu"), KeyboardButton("📡 Signals")],
]

YES_NO_ROWS = [
    [KeyboardButton("✅ Yes"), KeyboardButton("❌ No")],
    [KeyboardButton("⬅️ Menu")],
]


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_ROWS, resize_keyboard=True, is_persistent=True)


def pair_keyboard(title_hint: str = "") -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(PAIR_ROWS, resize_keyboard=True, one_time_keyboard=False)


def yes_no_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(YES_NO_ROWS, resize_keyboard=True, one_time_keyboard=True)


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
