"""Everything that talks to Telegram, behind a small interface the tests can replace.

Errors are sorted into "try again later" (flood control with its exact `retry_after`, network, Telegram's
5xx) and "this will not work by waiting" (the bot was removed from the channel, the chat does not exist,
the token was revoked, Telegram rejected the text).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from .formatting import Button


@dataclass(frozen=True)
class Outcome:
    retry_after: float | None  # None = permanent
    message: str


def classify(exc: Exception) -> Outcome:
    if isinstance(exc, TelegramRetryAfter):
        return Outcome(float(exc.retry_after), f"Telegram flood control: wait {exc.retry_after} s")
    if isinstance(exc, TelegramServerError | TelegramNetworkError):
        return Outcome(0.0, f"Telegram unavailable: {type(exc).__name__}")
    if isinstance(exc, TelegramUnauthorizedError):
        return Outcome(None, "the bot token is invalid or was revoked (BOT_TOKEN)")
    if isinstance(exc, TelegramForbiddenError):
        return Outcome(None, "the bot is not an administrator of this channel any more")
    if isinstance(exc, TelegramNotFound | TelegramBadRequest):
        return Outcome(None, f"Telegram refused: {exc.message}")
    if isinstance(exc, TelegramAPIError):
        return Outcome(None, f"Telegram error: {exc.message}")
    return Outcome(0.0, f"unexpected error: {type(exc).__name__}")


class ChannelProblem(Exception):
    pass


@dataclass(frozen=True)
class ChannelInfo:
    chat_id: int
    title: str
    username: str


class Sender(Protocol):
    async def send(self, chat_id: int, html: str, photo: Path | None, buttons: list[Button], silent: bool) -> int: ...

    async def pin(self, chat_id: int, message_id: int, silent: bool) -> None: ...

    async def edit(self, chat_id: int, message_id: int, html: str, has_photo: bool, buttons: list[Button]) -> None: ...

    async def delete(self, chat_id: int, message_id: int) -> None: ...

    async def channel(self, ref: str) -> ChannelInfo: ...


def keyboard(buttons: list[Button]) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.text, url=b.url)] for b in buttons])


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    @classmethod
    def from_token(cls, token: str) -> TelegramSender:
        return cls(Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML,
                                                          link_preview_is_disabled=False)))

    async def close(self) -> None:
        await self.bot.session.close()

    async def send(self, chat_id: int, html: str, photo: Path | None, buttons: list[Button], silent: bool) -> int:
        if photo is not None:
            msg = await self.bot.send_photo(chat_id, FSInputFile(photo), caption=html or None,
                                            reply_markup=keyboard(buttons), disable_notification=silent)
        else:
            msg = await self.bot.send_message(chat_id, html, reply_markup=keyboard(buttons),
                                              disable_notification=silent)
        return msg.message_id

    async def pin(self, chat_id: int, message_id: int, silent: bool) -> None:
        await self.bot.pin_chat_message(chat_id, message_id, disable_notification=silent)

    async def edit(self, chat_id: int, message_id: int, html: str, has_photo: bool, buttons: list[Button]) -> None:
        if has_photo:
            await self.bot.edit_message_caption(chat_id=chat_id, message_id=message_id, caption=html,
                                                reply_markup=keyboard(buttons))
        else:
            await self.bot.edit_message_text(html, chat_id=chat_id, message_id=message_id,
                                             reply_markup=keyboard(buttons))

    async def delete(self, chat_id: int, message_id: int) -> None:
        await self.bot.delete_message(chat_id, message_id)

    async def channel(self, ref: str) -> ChannelInfo:
        """@username or -100… id -> info; the bot must be an admin allowed to post."""
        ref = ref.strip()
        target: int | str = int(ref) if ref.lstrip("-").isdigit() else ("@" + ref.lstrip("@"))
        try:
            chat = await self.bot.get_chat(target)
            if chat.type != ChatType.CHANNEL:
                raise ChannelProblem(f"{ref} is not a channel")
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(chat.id, me.id)
        except (TelegramBadRequest, TelegramForbiddenError, TelegramNotFound):
            raise ChannelProblem(f"{ref}: channel not found or the bot is not in it — add the bot as an "
                                 "administrator first") from None
        if member.status != ChatMemberStatus.ADMINISTRATOR or not getattr(member, "can_post_messages", False):
            raise ChannelProblem(f"{chat.title}: the bot must be an administrator with the right to post messages")
        return ChannelInfo(chat.id, chat.title or str(chat.id), chat.username or "")
