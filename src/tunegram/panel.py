"""Now-playing panel: message with a live progress bar and control buttons."""
import asyncio
import html
import logging

from telethon import Button, TelegramClient
from telethon.errors import FloodWaitError, MessageNotModifiedError

from tunegram.player import Player
from tunegram.sources.youtube import Track, format_duration

log = logging.getLogger("tunegram.panel")

REFRESH_SECONDS = 10  # how often the progress bar is refreshed
BAR_WIDTH = 10


def panel_text(track: Track) -> str:
    lines = [
        "▶️ <b>Now playing</b>",
        "",
        html.escape(track.title),
        f"⏱ {format_duration(track.duration)} • {html.escape(track.uploader)}",
    ]
    if track.requested_by:
        lines.append(f"👤 Requested by: {html.escape(track.requested_by)}")
    return "\n".join(lines)


def progress_label(position: int, total: int) -> str:
    if total > 0:
        position = max(0, min(position, total))
        filled = round(BAR_WIDTH * position / total)
    else:
        position, filled = 0, 0
    bar = "▰" * filled + "▱" * (BAR_WIDTH - filled)
    return f"{format_duration(position)} {bar} {format_duration(total)}"


def panel_buttons(position: int, total: int):
    return [
        [Button.inline(progress_label(position, total), b"np:noop")],
        [
            Button.inline("▶️", b"np:resume"),
            Button.inline("⏸", b"np:pause"),
            Button.inline("⏭", b"np:skip"),
            Button.inline("⏹", b"np:stop"),
        ],
    ]


class PanelManager:
    """One panel message per chat. Buttons-only edits do not show the 'edited' label."""

    def __init__(self, bot: TelegramClient, player: Player) -> None:
        self.bot = bot
        self.player = player
        self._messages: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task] = {}

    async def show(self, chat_id: int, track: Track) -> None:
        await self.close(chat_id)
        msg = await self.bot.send_message(
            chat_id,
            panel_text(track),
            parse_mode="html",
            buttons=panel_buttons(0, track.duration),
        )
        self._messages[chat_id] = msg.id
        self._tasks[chat_id] = asyncio.create_task(self._refresh(chat_id, msg.id, track))

    async def close(self, chat_id: int) -> None:
        task = self._tasks.pop(chat_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        msg_id = self._messages.pop(chat_id, None)
        if msg_id:
            try:
                await self.bot.delete_messages(chat_id, msg_id)
            except Exception:
                log.debug("could not delete panel message", exc_info=True)

    async def _refresh(self, chat_id: int, msg_id: int, track: Track) -> None:
        try:
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                current = self.player.now_playing.get(chat_id)
                if current is None or current.id != track.id:
                    return
                position = await self.player.position(chat_id)
                if position is None:
                    continue
                try:
                    await self.bot.edit_message(
                        chat_id, msg_id, buttons=panel_buttons(position, track.duration)
                    )
                except MessageNotModifiedError:
                    pass  # e.g. paused: the label did not change
                except FloodWaitError as exc:
                    await asyncio.sleep(exc.seconds + 1)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("panel refresh stopped")
