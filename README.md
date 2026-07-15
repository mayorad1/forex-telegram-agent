# Forex Telegram Trading Agent

A **Telegram-linked forex trading agent** that:

- Pulls live FX/metal prices (Yahoo Finance)
- Scores pairs with a multi-indicator strategy (EMA trend + RSI + MACD)
- Sends signals and scans to Telegram
- Runs **paper trades** with risk limits, stop-loss / take-profit
- Supports optional **auto-scan alerts** on a schedule

> **Not financial advice.** Markets are risky. Default mode is **paper trading** only. Do not risk money you cannot afford to lose.

## Features

| Feature | Description |
|--------|-------------|
| Telegram commands | `/scan`, `/signal`, `/trade`, `/price`, `/positions`, … |
| Strategy agent | EMA cross, RSI zones, MACD histogram score |
| PDF research | Upload a PDF; bot extracts pairs (BUY/SELL, SL/TP) and blends into picks |
| MT5 / Exness | Live or demo orders via MetaTrader 5 |
| Risk controls | Max positions, % risk per trade, lot clamps |
| Auto trade | Every 15 minutes — tech + PDF ranked ideas |

## Project layout

```
forex-telegram-agent/
├── config/settings.yaml    # pairs, strategy, risk
├── src/
│   ├── main.py             # entry point
│   ├── agent/              # strategy + indicators
│   ├── bot/                # Telegram handlers
│   ├── data/               # market data
│   ├── trading/            # paper broker + risk
│   └── utils/              # config helpers
├── scripts/run.ps1
├── requirements.txt
└── .env.example
```

## Setup (Windows)

### 1. Create a Telegram bot

1. Open Telegram → talk to [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow prompts
3. Copy the **bot token**
4. Get your user ID from [@userinfobot](https://t.me/userinfobot)

### 2. Install & configure

```powershell
cd C:\Users\HP\forex-telegram-agent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=your_token_from_BotFather
TELEGRAM_ALLOWED_USERS=your_numeric_telegram_id
TRADING_MODE=paper
PAPER_BALANCE=10000
```

Optional: edit `config/settings.yaml` (pairs, timeframe, risk).

### 3. Run

```powershell
.\scripts\run.ps1
```

Or:

```powershell
.\.venv\Scripts\python.exe -m src.main
```

Open Telegram, message your bot, send `/start`.

## Telegram commands

| Command | Action |
|---------|--------|
| `/start` `/help` | Welcome + help |
| `/status` | Mode, auto flag, paper account |
| `/pairs` | Watched pairs |
| `/price EURUSD` | Latest quote |
| `/scan` | Score all pairs |
| `/signal GBPUSD` | Full signal + SL/TP |
| `/trade EURUSD` | Open paper position if signal is actionable |
| `/positions` | Open paper positions |
| `/closeall` | Close all at market |
| `/auto on` / `/auto off` | Scheduled scan alerts |
| `/reset` | Reset paper balance |

## Strategy (default)

For each pair on the configured timeframe (default `15m`):

1. **EMA(9) vs EMA(21)** — trend bias  
2. **RSI(14)** — oversold / overbought  
3. **MACD histogram** — momentum direction  

If net score ≥ `min_score` (default 2), the signal is **actionable**.  
Stops use ATR multiples (`sl_atr_mult`, `tp_atr_mult` in settings).

## Risk (paper)

Configured in `config/settings.yaml` → `risk`:

- `max_open_positions`
- `risk_per_trade_pct`
- `max_daily_loss_pct`
- lot clamps

Paper state is saved to `runtime/paper_account.json`.

## Live trading note

`TRADING_MODE=live` is **not fully implemented**. The bot intentionally stays on the paper broker so you do not accidentally send real orders. To go live you would integrate a broker API (e.g. OANDA practice/live) in a new module and gate it behind explicit config + risk checks.

## Disclaimer

This software is for **education and research**. Past or simulated performance is not indicative of future results. You are solely responsible for any trading decisions.
