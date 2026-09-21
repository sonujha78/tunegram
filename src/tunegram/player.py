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


class PlayerError(Exception):
    """Error message that is safe to show to users."""


class Player:
    def __init__(self, api_id: int, api_hash: str, session_string: str) -> None:
        self.userbot = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.calls = PyTgCalls(self.userbot)
        self.now_playing: dict[int, Track] = {}
        self.queues: dict[int, deque[Track]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
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
        await self.userbot.get_dialogs()  # fill the peer cache so groups can be resolved
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
        await self.userbot.disconnect()

    async def ensure_assistant(self, bot: TelegramClient, chat_id: int) -> None:
        """Make sure the assistant account is in the group; join it via invite link if not."""
        try:
            await self.userbot.get_input_entity(chat_id)
            return  # assistant already knows this chat, so it is a member
        except ValueError:
            pass  # not in the group yet

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
            raise PlayerError("Could not join with the invite link. Please try again.") from None
        await self.userbot.get_dialogs(limit=50)
        log.info("Assistant joined chat %s", chat_id)

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

    async def _leave(self, chat_id: int) -> None:
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
            await self._leave(chat_id)
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

    async def pause(self, chat_id: int) -> None:
        await self._control(self.calls.pause, chat_id)

    async def resume(self, chat_id: int) -> None:
        await self._control(self.calls.resume, chat_id)

    async def stop(self, chat_id: int) -> None:
        async with self._lock(chat_id):
            self.queues.pop(chat_id, None)
            await self._control(self.calls.leave_call, chat_id)
            self.now_playing.pop(chat_id, None)
