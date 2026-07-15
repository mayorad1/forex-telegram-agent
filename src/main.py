"""Entry point: Forex Telegram agent — MT5 / Exness only (no paper)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.strategy import ForexAgent
from src.bot.telegram_bot import ForexTelegramBot
from src.trading.mt5_broker import MT5Broker
from src.utils.config import env_str, get_settings, load_env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
logger = logging.getLogger("forex-agent")


def main() -> None:
    load_env()
    token = env_str("TELEGRAM_BOT_TOKEN")
    if not token or token.startswith("123456"):
        logger.error("Set TELEGRAM_BOT_TOKEN in .env")
        sys.exit(1)

    if not env_str("MT5_ACCOUNT") or not env_str("MT5_PASSWORD") or not env_str("MT5_SERVER"):
        logger.error("Set MT5_ACCOUNT, MT5_PASSWORD, MT5_SERVER in .env (Exness)")
        sys.exit(1)

    settings = get_settings()
    agent = ForexAgent(settings)
    broker = MT5Broker(risk_cfg=settings.get("risk", {}))

    logger.info("Connecting to MT5 / Exness (no paper mode)…")
    ok, msg = broker.connect(retries=2)
    if ok:
        logger.info(msg)
    else:
        # Still start Telegram so /mt5 and 15m auto-retry can recover — never uses paper
        logger.error("MT5 not connected yet: %s", msg)
        logger.error(
            "Bot will keep trying every auto-cycle. Fix: MT5 open + logged into Exness + "
            "Algo Trading ON. Send /mt5 in Telegram after fixing."
        )

    bot = ForexTelegramBot(
        token=token,
        settings=settings,
        agent=agent,
        broker=broker,
    )
    logger.info(
        "Agent online | broker=MT5 | auto_trade=%sm | pairs=%s | mt5_connected=%s",
        settings.get("scan_interval_minutes", 15),
        settings.get("pairs"),
        broker.connected,
    )
    bot.run()


if __name__ == "__main__":
    main()
