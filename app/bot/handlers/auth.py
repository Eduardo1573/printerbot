from __future__ import annotations

import contextlib
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Filter
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import MENU_BUTTONS, main_menu, remove_menu
from app.database.models import User
from app.services.auth_service import AuthOutcome, AuthService

router = Router(name="auth")


class Unauthenticated(Filter):
    async def __call__(self, message: Message, user: User | None = None) -> bool:
        return user is not None and not user.is_authenticated


@router.message(
    Unauthenticated(),
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(MENU_BUTTONS),
)
async def access_code(message: Message, user: User, auth_service: AuthService) -> None:
    result = await auth_service.authenticate(user.telegram_id, message.text or "")
    # Don't leave the code sitting in the chat history.
    with contextlib.suppress(TelegramAPIError):
        await message.delete()

    if result.outcome in (AuthOutcome.SUCCESS, AuthOutcome.ALREADY):
        text = texts.AUTH_SUCCESS if result.outcome is AuthOutcome.SUCCESS else texts.AUTH_ALREADY
        await message.answer(text, reply_markup=main_menu())
    elif result.outcome is AuthOutcome.LOCKED:
        minutes = max(1, math.ceil((result.retry_after or 60) / 60))
        await message.answer(texts.AUTH_LOCKED.format(minutes=minutes), reply_markup=remove_menu())
    else:
        await message.answer(texts.AUTH_INVALID, reply_markup=remove_menu())
