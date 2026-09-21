from pathlib import Path

from telethon import Button, TelegramClient, events

from tunegram import stats
from tunegram.config import Config
from tunegram.db import Database

# Jaise jaise platforms add karenge, yahan update karte jayenge
PLATFORMS = "YouTube"

HELP_TEXT = (
    "**Commands**\n\n"
    "/start - Open the main menu\n"
    "/help - Show this help\n"
    "/ping - Check bot latency\n"
    "/play <song or link> - Play in the group voice chat\n"
    "/vplay <video or link> - Play a video in the voice chat\n"
    "/pause - Pause playback\n"
    "/resume - Resume playback\n"
    "/skip - Skip the current track\n"
    "/queue - Show the queue\n"
    "/stop - Stop, clear the queue and leave the voice chat"
)


def start_buttons(cfg: Config, bot_username: str) -> list[list[Button]]:
    add_url = (
        f"https://t.me/{bot_username}?startgroup=true"
        "&admin=delete_messages+invite_users+pin_messages+manage_video_chats"
    )
    rows: list[list[Button]] = [[Button.url("➕ Add me to your group", add_url)]]
    links = []
    if cfg.owner_url:
        links.append(Button.url(cfg.owner_name, cfg.owner_url))
    if cfg.support_url:
        links.append(Button.url("Support", cfg.support_url))
    if links:
        rows.append(links)
    rows.append([Button.inline("Help & Commands ❓", b"help")])
    return rows


def start_text(cfg: Config, user_name: str, users: int, chats: int) -> str:
    s = stats.snapshot()
    return (
        f"Hello **{user_name}**! 🎧\n\n"
        f"It's me **{cfg.bot_name}**!\n\n"
        f"◆ **Supported platforms:** {PLATFORMS}\n\n"
        f"➥ **Uptime:** `{s.uptime}`\n"
        f"➥ **Server storage:** `{s.disk}%`\n"
        f"➥ **CPU load:** `{s.cpu}%`\n"
        f"➥ **RAM consumption:** `{s.ram}%`\n"
        f"➥ **Users:** `{users}`\n"
        f"➥ **Chats:** `{chats}`\n\n"
        f"👨‍💻 **Developer:** {cfg.owner_name}"
    )


def register_handlers(
    bot: TelegramClient, cfg: Config, db: Database, bot_username: str
) -> None:
    async def track(event) -> None:
        if event.is_private:
            await db.add_user(event.sender_id)
        elif event.is_group or event.is_channel:
            chat = await event.get_chat()
            await db.add_chat(event.chat_id, getattr(chat, "title", None))

    @bot.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
    async def start(event):
        await track(event)
        sender = await event.get_sender()
        users, chats = await db.counts()
        text = start_text(cfg, sender.first_name or "there", users, chats)
        buttons = start_buttons(cfg, bot_username)
        banner = Path(cfg.banner_path)
        if banner.is_file():
            await bot.send_file(event.chat_id, banner, caption=text, buttons=buttons)
        else:
            await event.respond(text, buttons=buttons)

    @bot.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
    async def help_cmd(event):
        await track(event)
        await event.respond(HELP_TEXT)

    @bot.on(events.NewMessage(pattern=r"^/ping(?:@\w+)?$"))
    async def ping(event):
        await track(event)
        import time

        t0 = time.perf_counter()
        msg = await event.respond("Pong!")
        ms = (time.perf_counter() - t0) * 1000
        await msg.edit(f"Pong! `{ms:.0f} ms`")

    async def send_home(chat_id: int, first_name: str) -> None:
        users, chats = await db.counts()
        text = start_text(cfg, first_name, users, chats)
        buttons = start_buttons(cfg, bot_username)
        banner = Path(cfg.banner_path)
        if banner.is_file():
            await bot.send_file(chat_id, banner, caption=text, buttons=buttons)
        else:
            await bot.send_message(chat_id, text, buttons=buttons)

    @bot.on(events.CallbackQuery(data=b"help"))
    async def help_cb(event):
        await event.answer()
        await event.delete()
        await bot.send_message(
            event.chat_id, HELP_TEXT, buttons=[[Button.inline("⬅ Back", b"home")]]
        )

    @bot.on(events.CallbackQuery(data=b"home"))
    async def home_cb(event):
        sender = await event.get_sender()
        await event.answer()
        await event.delete()
        await send_home(event.chat_id, sender.first_name or "there")
