from __future__ import annotations

import contextlib

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import main_menu, remove_menu
from app.config import ScanMode, Settings
from app.database.models import User
from app.services.auth_service import AuthService
from app.services.scan_session import ScanSessionError, ScanSessionService

router = Router(name="common")
fallback_router = Router(name="fallback")


def help_text(settings: Settings) -> str:
    scan_help = {
        ScanMode.EMAIL: texts.SCAN_HELP_EMAIL,
        ScanMode.ESCL: texts.SCAN_HELP_ESCL,
        ScanMode.DISABLED: texts.SCAN_HELP_DISABLED,
    }[settings.scan_mode]
    return texts.HELP.format(max_mb=settings.max_pdf_size_mb, max_pages=settings.max_pdf_pages, scan_help=scan_help)


@router.message(CommandStart())
async def cmd_start(message: Message, user: User) -> None:
    if user.is_authenticated:
        await message.answer(texts.WELCOME, reply_markup=main_menu())
    else:
        await message.answer(texts.AUTH_REQUIRED, reply_markup=remove_menu())


@router.message(Command("help"))
async def cmd_help(message: Message, user: User, settings: Settings) -> None:
    if not user.is_authenticated:
        await message.answer(texts.AUTH_REQUIRED, reply_markup=remove_menu())
        return
    await message.answer(help_text(settings), reply_markup=main_menu(), parse_mode="HTML")


@router.message(Command("logout"))
async def cmd_logout(
    message: Message, user: User, auth_service: AuthService, scan_sessions: ScanSessionService | None = None
) -> None:
    if not user.is_authenticated:
        await message.answer(texts.NOT_LOGGED_IN, reply_markup=remove_menu())
        return
    await auth_service.logout(user.telegram_id)
    if scan_sessions is not None:
        # An unfinished scan session must not outlive the user's access.
        with contextlib.suppress(ScanSessionError):
            await scan_sessions.cancel(user.telegram_id)
    await message.answer(texts.LOGGED_OUT, reply_markup=remove_menu())


# ---------------------------------------------------------------- fallback


@fallback_router.message(F.text.startswith("/"))
async def unknown_command(message: Message, user: User) -> None:
    if not user.is_authenticated:
        await message.answer(texts.AUTH_REQUIRED, reply_markup=remove_menu())
        return
    await message.answer(texts.UNKNOWN_COMMAND, reply_markup=main_menu())


@fallback_router.message()
async def unknown_message(message: Message, user: User) -> None:
    if not user.is_authenticated:
        await message.answer(texts.AUTH_REQUIRED, reply_markup=remove_menu())
        return
    await message.answer(texts.UNKNOWN_MESSAGE, reply_markup=main_menu())


@fallback_router.callback_query()
async def unknown_callback(callback: CallbackQuery, user: User) -> None:
    if not user.is_authenticated:
        await callback.answer(texts.AUTH_REQUIRED[:200], show_alert=True)
        return
    await callback.answer()
