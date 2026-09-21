"""Group commands: /play /pause /resume /stop."""
import html
import logging
import re

from telethon import TelegramClient, events

from tunegram.player import Player, PlayerError
from tunegram.sources.youtube import (
    SourceError,
    Track,
    download,
    format_duration,
    get_track,
    is_url,
    search,
)

log = logging.getLogger("tunegram.music")

MAX_DURATION = 3600  # seconds; longer audio is rejected
MAX_TRIES = 4  # how many search results to try before giving up


def _mostly_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) >= 0.8


async def find_candidates(query: str) -> list[Track]:
    """Ordered list of tracks to try. English (Latin-script) titles come first."""
    if is_url(query):
        return [await get_track(query)]
    results = await search(query, limit=6)
    if not results:
        raise SourceError("No results found.")
    latin = [t for t in results if _mostly_latin(t.title)]
    others = [t for t in results if t not in latin]
    return latin + others


async def first_downloadable(candidates: list[Track]) -> Track:
    """Return the first candidate that is playable and downloads successfully."""
    reason = "Could not download any matching track. Try a different search."
    for track in candidates[:MAX_TRIES]:
        if not 0 < track.duration <= MAX_DURATION:
            reason = (
                "Live streams and tracks longer than "
                f"{MAX_DURATION // 60} minutes are not supported."
            )
            continue
        try:
            await download(track)
            return track
        except SourceError as exc:
            log.warning("Skipping %s (%s): %s", track.id, track.title, str(exc)[:150])
    raise SourceError(reason)


def register_music_handlers(bot: TelegramClient, player: Player, bot_username: str) -> None:
    def cmd(name: str, args: bool = False) -> events.NewMessage:
        tail = r"(?:\s+(.+))?" if args else ""
        # match /cmd and /cmd@thisbot only, never /cmd@otherbot
        pattern = re.compile(
            rf"^/{name}(?:@{re.escape(bot_username)})?{tail}$", re.IGNORECASE | re.DOTALL
        )
        return events.NewMessage(pattern=pattern)

    async def group_only(event) -> bool:
        if event.is_private:
            await event.respond(
                "This command works in groups. Add me to your group, "
                "start a voice chat and use /play there."
            )
            return False
        return True

    @bot.on(cmd("play", args=True))
    async def play(event):
        if not await group_only(event):
            return
        query = (event.pattern_match.group(1) or "").strip()
        if not query:
            await event.respond(
                "Usage: <code>/play song name or YouTube link</code>", parse_mode="html"
            )
            return

        status = await event.respond("🔎 Searching...")
        try:
            candidates = await find_candidates(query)
            await player.ensure_assistant(bot, event.chat_id)
            await status.edit("⬇️ Preparing audio...")
            track = await first_downloadable(candidates)
            await player.play(event.chat_id, track)
        except (PlayerError, SourceError) as exc:
            await status.edit(f"❌ {html.escape(str(exc)[:300])}", parse_mode="html")
            return
        except Exception:
            log.exception("play failed")
            await status.edit("❌ Something went wrong. Please try again.")
            return

        await status.edit(
            f"▶️ <b>Now playing</b>\n{html.escape(track.title)}\n"
            f"⏱ {format_duration(track.duration)} • {html.escape(track.uploader)}",
            parse_mode="html",
        )

    def control(name: str, action, ok_text: str) -> None:
        @bot.on(cmd(name))
        async def handler(event):
            if not await group_only(event):
                return
            try:
                await action(event.chat_id)
            except PlayerError as exc:
                await event.respond(f"❌ {exc}")
                return
            await event.respond(ok_text)

    control("pause", player.pause, "⏸ Paused")
    control("resume", player.resume, "▶️ Resumed")
    control("stop", player.stop, "⏹ Stopped")

    async def finished(chat_id: int) -> None:
        try:
            await bot.send_message(chat_id, "✅ Playback finished.")
        except Exception:
            log.exception("could not send finish message")

    player.on_end = finished
