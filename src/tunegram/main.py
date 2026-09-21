import asyncio
import logging
from pathlib import Path

from telethon import TelegramClient
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import BotCommand, BotCommandScopeDefault

from tunegram import stats
from tunegram.config import load_config
from tunegram.db import Database
from tunegram.handlers import register_handlers

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tunegram")


async def main() -> None:
    cfg = load_config()
    Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)

    db = Database(f"{cfg.data_dir}/tunegram.db")
    await db.connect()

    bot = TelegramClient(f"{cfg.data_dir}/bot", cfg.api_id, cfg.api_hash)
    await bot.start(bot_token=cfg.bot_token)
    me = await bot.get_me()

    register_handlers(bot, cfg, db, me.username)
    await bot(
        SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="",
            commands=[
                BotCommand("start", "Open the main menu"),
                BotCommand("help", "Show commands"),
                BotCommand("ping", "Check bot latency"),
            ],
        )
    )
    stats.prime()
    log.info("Bot started as @%s", me.username)

    try:
        await bot.run_until_disconnected()
    finally:
        await db.close()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
