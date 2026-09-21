"""Voice chat player: assistant (userbot) account + PyTgCalls."""
import logging
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


class PlayerError(Exception):
    """Error message that is safe to show to users."""


class Player:
    def __init__(self, api_id: int, api_hash: str, session_string: str) -> None:
        self.userbot = TelegramClient(StringSession(session_string), api_id, api_hash)
        self.calls = PyTgCalls(self.userbot)
        self.now_playing: dict[int, Track] = {}
        self.on_end: Callable[[int], Awaitable[None]] | None = None

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
            chat_id = update.chat_id
            self.now_playing.pop(chat_id, None)
            log.info("Stream ended in chat %s", chat_id)
            try:
                await self.calls.leave_call(chat_id)
            except Exception:
                log.debug("leave_call after end failed", exc_info=True)
            if self.on_end:
                await self.on_end(chat_id)

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

    async def play(self, chat_id: int, track: Track) -> None:
        path = await download(track)
        stream = MediaStream(
            str(path),
            audio_parameters=AudioQuality.HIGH,
            video_flags=MediaStream.Flags.IGNORE,  # audio only
        )
        try:
            await self.calls.play(chat_id, stream)
        except NoActiveGroupCall:
            raise PlayerError(
                "No active voice chat in this group. Start a voice chat first."
            ) from None
        except Exception as exc:
            log.exception("calls.play failed")
            raise PlayerError(
                "Could not join the voice chat. Make sure a voice chat is running "
                "and try again."
            ) from exc
        self.now_playing[chat_id] = track

    async def _control(self, fn, chat_id: int):
        try:
            return await fn(chat_id)
        except (NotInCallError, NoActiveGroupCall):
            raise PlayerError("Nothing is playing in this group right now.") from None

    async def pause(self, chat_id: int) -> None:
        await self._control(self.calls.pause, chat_id)

    async def resume(self, chat_id: int) -> None:
        await self._control(self.calls.resume, chat_id)

    async def stop(self, chat_id: int) -> None:
        await self._control(self.calls.leave_call, chat_id)
        self.now_playing.pop(chat_id, None)
