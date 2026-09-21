"""Group commands: /play /vplay /skip /queue /pause /resume /stop and the panel buttons."""
import asyncio
import html
import logging
import re
import time
from dataclasses import replace

from telethon import TelegramClient, events

from tunegram.db import Database
from tunegram.panel import PanelManager
from tunegram.player import Player, PlayerError
from tunegram.sources.youtube import (
    SourceError,
    Track,
    download,
    download_video,
    format_duration,
    get_track,
    is_url,
    search,
)

log = logging.getLogger("tunegram.music")

MAX_DURATION = 3600  # seconds; longer audio is rejected
MAX_VIDEO_DURATION = 1800  # seconds; videos are big, so the limit is lower
MAX_TRIES = 4  # how many search results to try before giving up
CACHED_FIELDS = ("id", "title", "uploader", "duration", "webpage_url", "thumbnail")


def _mostly_latin(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isascii() for c in letters) / len(letters) >= 0.8


_VIDEO_GOOD = re.compile(
    r"\b(official (music )?video|music video|video song|full video|official mv)\b"
)
_VIDEO_BAD = re.compile(
    r"\b(lyrics?|lyrical|audio|karaoke|instrumental|slowed|reverb|8d|"
    r"reaction|status|cover|jukebox)\b"
)


def _video_score(track: Track) -> int:
    title = track.title.lower()
    score = 0
    if _VIDEO_GOOD.search(title):
        score += 3
    if _VIDEO_BAD.search(title):
        score -= 5
    return score


async def find_candidates(query: str, video: bool = False) -> list[Track]:
    """Ordered list of tracks to try. English (Latin-script) titles come first.
    For /vplay, real music videos are ranked above lyric videos and audio uploads."""
    if is_url(query):
        return [await get_track(query)]
    results = await search(
        f"{query} official video" if video else query, limit=8 if video else 6
    )
    if not results:
        raise SourceError("No results found.")
    latin = [t for t in results if _mostly_latin(t.title)]
    others = [t for t in results if t not in latin]
    ordered = latin + others
    if video:
        ordered.sort(key=_video_score, reverse=True)  # stable sort keeps Latin-first on ties
    return ordered


async def first_downloadable(candidates: list[Track], video: bool = False) -> Track:
    """Return the first candidate that is playable and downloads successfully."""
    limit = MAX_VIDEO_DURATION if video else MAX_DURATION
    fetch = download_video if video else download
    reason = "Could not download any matching track. Try a different search."
    for track in candidates[:MAX_TRIES]:
        if not 0 < track.duration <= limit:
            reason = (
                f"Live streams and {'videos' if video else 'tracks'} longer than "
                f"{limit // 60} minutes are not supported."
            )
            continue
        try:
            await fetch(track)
            return replace(track, video=video)
        except SourceError as exc:
            log.warning("Skipping %s (%s): %s", track.id, track.title, str(exc)[:150])
    raise SourceError(reason)


def queue_text(current: Track | None, upcoming: list[Track]) -> str:
    def line(t: Track) -> str:
        icon = "🎬 " if t.video else ""
        return f"{icon}{html.escape(t.title)} ({format_duration(t.duration)})"

    if current is None:
        return "The queue is empty. Use /play or /vplay to add something."
    lines = ["🎶 <b>Now playing</b>", line(current)]
    if upcoming:
        lines += ["", "📜 <b>Up next</b>"]
        for i, t in enumerate(upcoming[:10], 1):
            lines.append(f"{i}. {line(t)}")
        if len(upcoming) > 10:
            lines.append(f"... and {len(upcoming) - 10} more")
    return "\n".join(lines)


def register_music_handlers(
    bot: TelegramClient, player: Player, bot_username: str, db: Database
) -> None:
    panels = PanelManager(bot, player)

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
                "This command works in groups. Add me to your group and use /play there."
            )
            return False
        return True

    async def start_play(event, video: bool) -> None:
        if not await group_only(event):
            return
        command = "/vplay" if video else "/play"
        query = (event.pattern_match.group(1) or "").strip()
        if not query:
            await event.respond(
                f"Usage: <code>{command} "
                f"{'video name or YouTube link' if video else 'song name or YouTube link'}</code>",
                parse_mode="html",
            )
            return

        sender = await event.get_sender()
        requester = (
            getattr(sender, "first_name", None) or getattr(sender, "title", None) or "Unknown"
        )

        status = await event.respond("🔎 Searching...")
        try:
            t0 = time.perf_counter()
            query_key = " ".join(query.lower().split())
            cached = None if is_url(query) else await db.get_cached_track(query_key, video)
            candidates = (
                [Track(**cached)] if cached else await find_candidates(query, video=video)
            )
            t1 = time.perf_counter()
            await status.edit(
                f"⬇️ Loading{' video' if video else ''}: "
                f"<b>{html.escape(candidates[0].title)}</b>",
                parse_mode="html",
            )
            # join the assistant and download at the same time
            join_result, track_result = await asyncio.gather(
                player.ensure_assistant(bot, event.chat_id),
                first_downloadable(candidates, video=video),
                return_exceptions=True,
            )
            if cached and isinstance(track_result, SourceError):
                # the remembered result stopped working (removed video, etc.): search again
                await db.delete_cached_track(query_key, video)
                candidates = await find_candidates(query, video=video)
                track_result = await first_downloadable(candidates, video=video)
            for result in (join_result, track_result):
                if isinstance(result, BaseException):
                    raise result
            t2 = time.perf_counter()
            if not is_url(query):
                data = {k: getattr(track_result, k) for k in CACHED_FIELDS}
                await db.put_cached_track(query_key, video, data)
            track = replace(track_result, requested_by=requester)
            position = await player.enqueue(event.chat_id, track)
            t3 = time.perf_counter()
            log.info(
                "%s timings: search=%.1fs join+download=%.1fs start=%.1fs total=%.1fs",
                command, t1 - t0, t2 - t1, t3 - t2, t3 - t0,
            )
        except (PlayerError, SourceError) as exc:
            await status.edit(f"❌ {html.escape(str(exc)[:300])}", parse_mode="html")
            player.leave_if_idle(event.chat_id)
            return
        except Exception:
            log.exception("%s failed", command)
            await status.edit("❌ Something went wrong. Please try again.")
            player.leave_if_idle(event.chat_id)
            return

        if position == 0:
            await status.delete()
            await panels.show(event.chat_id, track)
        else:
            icon = "🎬 " if track.video else ""
            await status.edit(
                f"➕ <b>Added to queue</b> (#{position})\n{icon}{html.escape(track.title)}\n"
                f"⏱ {format_duration(track.duration)}",
                parse_mode="html",
            )

    @bot.on(cmd("play", args=True))
    async def play(event):
        await start_play(event, video=False)

    @bot.on(cmd("vplay", args=True))
    async def vplay(event):
        await start_play(event, video=True)

    @bot.on(cmd("queue"))
    async def show_queue(event):
        if not await group_only(event):
            return
        current, upcoming = player.snapshot(event.chat_id)
        await event.respond(queue_text(current, upcoming), parse_mode="html")

    async def do_stop(chat_id: int) -> None:
        await player.stop(chat_id)
        await panels.close(chat_id)

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
    control("skip", player.skip, "⏭ Skipped")
    control("stop", do_stop, "⏹ Stopped and queue cleared")

    button_actions = {
        "resume": (player.resume, "▶️ Resumed"),
        "pause": (player.pause, "⏸ Paused"),
        "skip": (player.skip, "⏭ Skipped"),
        "stop": (do_stop, "⏹ Stopped"),
    }

    @bot.on(events.CallbackQuery(pattern=rb"^np:(noop|resume|pause|skip|stop)$"))
    async def panel_button(event):
        action = event.pattern_match.group(1).decode()
        if action == "noop":
            await event.answer()
            return
        fn, toast = button_actions[action]
        try:
            await fn(event.chat_id)
        except PlayerError as exc:
            await event.answer(str(exc), alert=True)
            return
        await event.answer(toast)

    async def track_started(chat_id: int, track: Track) -> None:
        try:
            await panels.show(chat_id, track)
        except Exception:
            log.exception("could not show now-playing panel")

    async def queue_finished(chat_id: int) -> None:
        try:
            await panels.close(chat_id)
            await bot.send_message(chat_id, "✅ Queue finished.")
        except Exception:
            log.exception("could not send finish message")

    player.on_track_start = track_started
    player.on_queue_end = queue_finished
