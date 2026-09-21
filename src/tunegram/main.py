import asyncio
import logging
import time

from telethon import TelegramClient, events

from tunegram.config import Config, load_config

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tunegram")


def build_bot(cfg: Config) -> TelegramClient:
    bot = TelegramClient("tunegram-bot", cfg.api_id, cfg.api_hash)

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start(event):
        await event.respond(
            "Hey! I'm tunegram 🎵\nMusic commands are coming soon. Try /ping."
        )

    @bot.on(events.NewMessage(pattern=r"^/ping(?:@\w+)?$"))
    async def ping(event):
        t0 = time.perf_counter()
        msg = await event.respond("Pong!")
        ms = (time.perf_counter() - t0) * 1000
        await msg.edit(f"Pong! `{ms:.0f} ms`")

    return bot


async def main() -> None:
    cfg = load_config()
    bot = build_bot(cfg)
    await bot.start(bot_token=cfg.bot_token)
    me = await bot.get_me()
    log.info("Bot started as @%s", me.username)
    await bot.run_until_disconnected()


def run() -> None:
    asyncio.run(main())
