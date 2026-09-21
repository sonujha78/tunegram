"""Voice chat player: assistant (userbot) account + PyTgCalls, with per-chat queues."""
import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from pytgcalls import PyTgCalls, filters
from pytgcalls.exceptions import NoActiveGroupCall, NotInCallError
from pytgcalls.types import AudioQuality, MediaStream, StreamEnded
from telethon import TelegramClient
from telethon.errors import (
    ChatAdminInviteRequiredError,
    ChatAdminRequiredError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    UserAlreadyParticipantError,
    UserBannedInChannelError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ExportChatInviteRequest, ImportChatInviteRequest

from tunegram.sources.youtube import Track, download

log = logging.getLogger("tunegram.player")

MAX_QUEUE = 20
IDLE_LEAVE_SECONDS = 15  # assistant leaves the group this long after playback stops


class PlayerError(Exception):
    """Error message that is safe to show to users."""


class Player:
    def __init__(self, api_id: int, api_hash: str, session_string: str) -> None:
        self.userbot = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.calls = PyTgCalls(self.userbot)
        self.now_playing: dict[int, Track] = {}
        self.queues: dict[int, deque[Track]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._members: set[int] = set()  # groups the assistant is currently in
        self._leave_tasks: dict[int, asyncio.Task] = {}
        # callbacks set by the command layer
        self.on_track_start: Callable[[int, Track], Awaitable[None]] | None = None
        self.on_queue_end: Callable[[int], Awaitable[None]] | None = None

    def _lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(chat_id, asyncio.Lock())

    async def start(self) -> str:
        await self.userbot.connect()
        if not await self.userbot.is_user_authorized():
            raise RuntimeError(
                "SESSION_STRING is invalid or expired. Run scripts/gen_session.py again."
            )
        dialogs = await self.userbot.get_dialogs()  # also fills the peer cache
        self._members = {d.id for d in dialogs if d.is_group}
        await self.calls.start()

        @self.calls.on_update(filters.stream_end())
        async def _stream_ended(_, update: StreamEnded) -> None:
            log.info("Stream ended in chat %s", update.chat_id)
            try:
                await self._advance(update.chat_id)
            except Exception:
                log.exception("advance after stream end failed")

        me = await self.userbot.get_me()
        return me.first_name or "assistant"

    async def close(self) -> None:
        for task in list(self._leave_tasks.values()):
            task.cancel()
        await self.userbot.disconnect()

    # ---- assistant membership ----

    async def ensure_assistant(self, bot: TelegramClient, chat_id: int) -> None:
        """Make sure the assistant is in the group; join it via invite link if not."""
        self.cancel_idle_leave(chat_id)
        async with self._lock(chat_id):  # waits if an idle-leave is in progress
            if chat_id in self._members:
                return

            try:
                invite = await bot(
                    ExportChatInviteRequest(chat_id, usage_limit=1, title="assistant")
                )
            except (ChatAdminRequiredError, ChatAdminInviteRequiredError):
                raise PlayerError(
                    "Make me an admin with the 'Invite users via link' permission "
                    "so my assistant can join this group, then try again."
                ) from None

            invite_hash = invite.link.rsplit("/", 1)[-1].lstrip("+")
            try:
                await self.userbot(ImportChatInviteRequest(invite_hash))
            except UserAlreadyParticipantError:
                pass
            except UserBannedInChannelError:
                raise PlayerError(
                    "The assistant account is banned in this group. Unban it and try again."
                ) from None
            except (InviteHashExpiredError, InviteHashInvalidError):
                raise PlayerError(
                    "Could not join with the invite link. Please try again."
                ) from None
            await self.userbot.get_dialogs(limit=50)
            self._members.add(chat_id)
            log.info("Assistant joined chat %s", chat_id)

    def schedule_idle_leave(self, chat_id: int) -> None:
        self.cancel_idle_leave(chat_id)
        self._leave_tasks[chat_id] = asyncio.create_task(self._idle_leave(chat_id))

    def cancel_idle_leave(self, chat_id: int) -> None:
        task = self._leave_tasks.pop(chat_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()

    def leave_if_idle(self, chat_id: int) -> None:
        """Call after a failed /play: if nothing is playing, let the assistant leave."""
        if chat_id not in self.now_playing:
            self.schedule_idle_leave(chat_id)

    async def _idle_leave(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(IDLE_LEAVE_SECONDS)
            async with self._lock(chat_id):
                if chat_id in self.now_playing:
                    return  # something started playing again
                self._members.discard(chat_id)
                try:
                    await self.userbot.delete_dialog(chat_id)  # leaves the group
                    log.info("Assistant left chat %s (idle)", chat_id)
                except Exception:
                    log.warning("Assistant could not leave chat %s", chat_id, exc_info=True)
        except asyncio.CancelledError:
            pass
        finally:
            if self._leave_tasks.get(chat_id) is asyncio.current_task():
                self._leave_tasks.pop(chat_id, None)

    # ---- internals ----

    async def _start(self, chat_id: int, track: Track) -> None:
        path = await download(track)  # cache hit if already downloaded
        stream = MediaStream(
            str(path),
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,  # audio only
        )
        try:
            # If the assistant is already in the call, this just switches the stream (gapless)
            await self.calls.play(chat_id, stream)
        except NoActiveGroupCall:
            raise PlayerError(
                "No active voice chat in this group. Start a voice chat first."
            ) from None
        except Exception as exc:
            log.exception("calls.play failed")
            raise PlayerError(
                "Could not join the voice chat. Start a voice chat in the group "
                "(or make the assistant an admin with 'Manage video chats' so it can "
                "start one) and try again."
            ) from exc
        self.now_playing[chat_id] = track

    async def _leave_call(self, chat_id: int) -> None:
        try:
            await self.calls.leave_call(chat_id)
        except Exception:
            log.debug("leave_call failed", exc_info=True)

    async def _advance(self, chat_id: int) -> Track | None:
        """Play the next queued track, or leave the call if the queue is empty."""
        async with self._lock(chat_id):
            self.now_playing.pop(chat_id, None)
            queue = self.queues.get(chat_id)
            while queue:
                track = queue.popleft()
                try:
                    await self._start(chat_id, track)
                except Exception as exc:
                    log.warning("Skipping queued track %s: %s", track.id, exc)
                    continue
                if self.on_track_start:
                    await self.on_track_start(chat_id, track)
                return track
            await self._leave_call(chat_id)
        self.schedule_idle_leave(chat_id)
        if self.on_queue_end:
            await self.on_queue_end(chat_id)
        return None

    async def _control(self, fn, chat_id: int):
        try:
            return await fn(chat_id)
        except (NotInCallError, NoActiveGroupCall):
            raise PlayerError("Nothing is playing in this group right now.") from None

    # ---- public API ----

    async def enqueue(self, chat_id: int, track: Track) -> int:
        """Add a track. Returns 0 if it started playing now, else its 1-based queue position."""
        self.cancel_idle_leave(chat_id)
        async with self._lock(chat_id):
            if chat_id not in self.now_playing:
                await self._start(chat_id, track)
                return 0
            queue = self.queues.setdefault(chat_id, deque())
            if len(queue) >= MAX_QUEUE:
                raise PlayerError(f"The queue is full ({MAX_QUEUE} tracks).")
            queue.append(track)
            return len(queue)

    async def skip(self, chat_id: int) -> None:
        if chat_id not in self.now_playing:
            raise PlayerError("Nothing is playing in this group right now.")
        await self._advance(chat_id)

    def snapshot(self, chat_id: int) -> tuple[Track | None, list[Track]]:
        return self.now_playing.get(chat_id), list(self.queues.get(chat_id, []))

    async def position(self, chat_id: int) -> int | None:
        """Seconds played of the current track, or None if unknown."""
        try:
            return int(await self.calls.time(chat_id))
        except Exception:
            return None

    async def pause(self, chat_id: int) -> None:
        await self._control(self.calls.pause, chat_id)

    async def resume(self, chat_id: int) -> None:
        await self._control(self.calls.resume, chat_id)

    async def stop(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            self.queues.pop(chat_id, None)
            await self._control(self.calls.leave_call, chat_id)
            self.now_playing.pop(chat_id, None)
        self.schedule_idle_leave(chat_id)
