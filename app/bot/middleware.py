from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.exc import SQLAlchemyError

from app.bot import texts
from app.bot.keyboards import remove_menu
from app.services.auth_service import AuthService

log = logging.getLogger(__name__)

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


async def reply(event: TelegramObject, text: str, **kwargs: Any) -> None:
    if isinstance(event, Message):
        await event.answer(text, **kwargs)
    elif isinstance(event, CallbackQuery):
        await event.answer(text[:200], show_alert=True)


class UserContextMiddleware(BaseMiddleware):
    """Outer middleware: validates the sender and loads its DB row into `data["user"]`."""

    def __init__(self, auth_service: AuthService) -> None:
        self._auth = auth_service

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        chat = data.get("event_chat")
        if user is None or user.is_bot or not isinstance(user.id, int) or user.id <= 0:
            return None
        # Private chats only: never print/scan on behalf of a group.
        if chat is not None and chat.type != ChatType.PRIVATE:
            return None
        try:
            data["user"] = await self._auth.touch_user(user.id)
        except (SQLAlchemyError, OSError):
            log.exception("database_unavailable")
            await reply(event, texts.SERVICE_UNAVAILABLE)
            return None
        return await handler(event, data)


class AuthRequiredMiddleware(BaseMiddleware):
    """Inner middleware for protected routers: blocks unauthenticated users."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("user")
        if user is None or not user.is_authenticated:
            if isinstance(event, Message):
                await event.answer(texts.AUTH_REQUIRED, reply_markup=remove_menu())
            else:
                await reply(event, texts.AUTH_REQUIRED)
            return None
        return await handler(event, data)
