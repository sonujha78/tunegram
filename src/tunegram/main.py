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
from tunegram.music import register_music_handlers
from tunegram.cachegc import cache_janitor
from tunegram.player import Player
from tunegram.sources.youtube import warm_up

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

    commands = [
        BotCommand("start", "Open the main menu"),
        BotCommand("help", "Show commands"),
        BotCommand("ping", "Check bot latency"),
    ]

    # Player: PyTgCalls ko event loop chahiye, isliye async main ke andar banao
    player: Player | None = None
    if cfg.session_string:
        player = Player(cfg.api_id, cfg.api_hash, cfg.session_string)
        name = await player.start()
        register_music_handlers(bot, player, me.username, db)
        commands += [
            BotCommand("play", "Play a song in the voice chat"),
            BotCommand("vplay", "Play a video in the voice chat"),
            BotCommand("pause", "Pause playback"),
            BotCommand("resume", "Resume playback"),
            BotCommand("skip", "Skip the current track"),
            BotCommand("queue", "Show the queue"),
            BotCommand("stop", "Stop, clear the queue and leave"),
        ]
        log.info("Userbot ready: %s", name)
    else:
        log.warning("SESSION_STRING missing: music commands disabled")

    await bot(
        SetBotCommandsRequest(
            scope=BotCommandScopeDefault(), lang_code="", commands=commands
        )
    )
    stats.prime()
    janitor = asyncio.create_task(cache_janitor(player)) if player else None
    warm_task = asyncio.create_task(warm_up())  # keep the reference alive
    log.info("Bot started as @%s", me.username)

    try:
        await bot.run_until_disconnected()
    finally:
        if janitor:
            janitor.cancel()
        if player:
            await player.close()
        await db.close()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
