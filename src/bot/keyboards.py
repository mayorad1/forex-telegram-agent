"""Telegram reply keyboards for interactive trading bot."""

from __future__ import annotations

from telegram import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

# Main always-on menu
MAIN_ROWS = [
    [KeyboardButton("📡 Signals"), KeyboardButton("⭐ Best trade")],
    [KeyboardButton("📂 Positions"), KeyboardButton("💰 Status")],
    [KeyboardButton("🟢 Buy"), KeyboardButton("🔴 Sell")],
    [KeyboardButton("📋 Pairs"), KeyboardButton("💲 Price")],
    [KeyboardButton("📐 Lot"), KeyboardButton("⏱ Interval")],
    [KeyboardButton("🤖 Auto ON"), KeyboardButton("⏸ Auto OFF")],
    [KeyboardButton("🔌 MT5"), KeyboardButton("📄 PDF")],
    [KeyboardButton("📰 News"), KeyboardButton("❓ Help")],
    [KeyboardButton("🧹 Close all")],
]

LOT_ROWS = [
    [KeyboardButton("0.01"), KeyboardButton("0.02"), KeyboardButton("0.05")],
    [KeyboardButton("0.10"), KeyboardButton("0.20"), KeyboardButton("0.50")],
    [KeyboardButton("1.00"), KeyboardButton("2.00"), KeyboardButton("5.00")],
    [KeyboardButton("⬅️ Menu"), KeyboardButton("💰 Status")],
]

INTERVAL_ROWS = [
    [KeyboardButton("1 min"), KeyboardButton("5 min"), KeyboardButton("10 min")],
    [KeyboardButton("15 min"), KeyboardButton("30 min"), KeyboardButton("60 min")],
    [KeyboardButton("120 min"), KeyboardButton("240 min"), KeyboardButton("360 min")],
    [KeyboardButton("⬅️ Menu"), KeyboardButton("💰 Status")],
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


def lot_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(LOT_ROWS, resize_keyboard=True)


def interval_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(INTERVAL_ROWS, resize_keyboard=True)


def remove_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()
