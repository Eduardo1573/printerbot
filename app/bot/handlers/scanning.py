from __future__ import annotations

import contextlib
import math

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import (
    BTN_SCAN,
    BTN_SCAN_NOW,
    CB_SCAN_CANCEL,
    CB_SCAN_DONE,
    CB_SCAN_MORE,
    CB_SCAN_RESCAN,
    main_menu,
    scan_request_actions,
    scan_session_actions,
)
from app.bot.middleware import AuthRequiredMiddleware
from app.config import ScanMode, Settings
from app.database.models import User
from app.printer.base import PrinterError
from app.services.scan_service import ScanService
from app.services.scan_session import ScanSessionError, ScanSessionService

router = Router(name="scanning")
router.message.middleware(AuthRequiredMiddleware())
router.callback_query.middleware(AuthRequiredMiddleware())


# ====================================================================== entry point


@router.message(F.text.in_({BTN_SCAN, BTN_SCAN_NOW}))
@router.message(Command("scan"))
async def scan_entry(
    message: Message,
    bot: Bot,
    user: User,
    settings: Settings,
    scan_service: ScanService,
    scan_sessions: ScanSessionService | None,
) -> None:
    if settings.scan_mode is ScanMode.ESCL and scan_sessions is not None:
        await _scan_page(message, bot, user.telegram_id, scan_sessions, replace_last=False)
    elif settings.scan_mode is ScanMode.EMAIL:
        await receive_scan(message, user, scan_service, settings)
    else:
        await message.answer(texts.SCAN_DISABLED, reply_markup=main_menu())


# ====================================================================== eSCL: multi-page sessions


async def _strip_buttons(bot: Bot, chat_id: int, message_id: int | None) -> None:
    if message_id is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)


async def _scan_page(
    message: Message, bot: Bot, telegram_id: int, sessions: ScanSessionService, *, replace_last: bool
) -> None:
    current = sessions.page_count(telegram_id)
    page = current if replace_last and current else current + 1
    status = await message.answer(texts.SCAN_SCANNING.format(page=page))
    try:
        result = await sessions.scan_page(telegram_id, replace_last=replace_last)
    except (ScanSessionError, PrinterError) as exc:
        reason = exc.user_message
        text = reason if isinstance(exc, ScanSessionError) else texts.SCAN_FAILED.format(reason=reason)
        # Pages already scanned are kept: offer the session buttons again.
        markup = scan_session_actions() if sessions.page_count(telegram_id) else None
        with contextlib.suppress(TelegramAPIError):
            await status.edit_text(text, reply_markup=markup)
        if markup:
            previous = sessions.set_controls_message(telegram_id, status.message_id)
            if previous != status.message_id:
                await _strip_buttons(bot, telegram_id, previous)
        return

    template = texts.SCAN_PAGE_REPLACED if result.replaced else texts.SCAN_PAGE_DONE
    caption = template.format(page=result.page_number, total=result.total_pages) + "\n\n" + texts.SCAN_NEXT_PAGE
    controls: Message = status
    if result.preview_jpeg:
        try:
            controls = await message.answer_photo(
                BufferedInputFile(result.preview_jpeg, filename=f"page-{result.page_number}.jpg"),
                caption=caption,
                reply_markup=scan_session_actions(),
            )
            with contextlib.suppress(TelegramAPIError):
                await status.delete()
        except TelegramAPIError:
            controls = status
            await status.edit_text(caption, reply_markup=scan_session_actions())
    else:
        await status.edit_text(caption, reply_markup=scan_session_actions())
    previous = sessions.set_controls_message(telegram_id, controls.message_id)
    if previous != controls.message_id:
        await _strip_buttons(bot, telegram_id, previous)


async def _session_callback_message(callback: CallbackQuery) -> Message | None:
    await callback.answer()
    message = callback.message if isinstance(callback.message, Message) else None
    if message is not None:
        with contextlib.suppress(TelegramAPIError):
            await message.edit_reply_markup(reply_markup=None)
    return message


@router.callback_query(F.data.in_({CB_SCAN_MORE, CB_SCAN_RESCAN}))
async def scan_more(callback: CallbackQuery, bot: Bot, user: User, scan_sessions: ScanSessionService | None) -> None:
    if scan_sessions is None:
        await callback.answer()
        return
    message = await _session_callback_message(callback)
    if message is not None:
        await _scan_page(
            message, bot, user.telegram_id, scan_sessions, replace_last=callback.data == CB_SCAN_RESCAN
        )


@router.callback_query(F.data == CB_SCAN_DONE)
async def make_document(callback: CallbackQuery, user: User, scan_sessions: ScanSessionService | None) -> None:
    if scan_sessions is None:
        await callback.answer()
        return
    message = await _session_callback_message(callback)
    if message is None:
        return
    status = await message.answer(texts.SCAN_ASSEMBLING)
    try:
        document = await scan_sessions.finish(user.telegram_id)
    except ScanSessionError as exc:
        markup = scan_session_actions() if scan_sessions.page_count(user.telegram_id) else None
        await status.edit_text(exc.user_message, reply_markup=markup)
        if markup:
            scan_sessions.set_controls_message(user.telegram_id, status.message_id)
        return
    with contextlib.suppress(TelegramAPIError):
        await status.delete()
    await message.answer_document(
        BufferedInputFile(document.content, filename=document.filename),
        caption=texts.SCAN_DOCUMENT_READY.format(pages=document.pages),
        reply_markup=main_menu(),
    )


# ====================================================================== cancel (both modes)


@router.callback_query(F.data == CB_SCAN_CANCEL)
async def cancel_scan(
    callback: CallbackQuery,
    user: User,
    settings: Settings,
    scan_service: ScanService,
    scan_sessions: ScanSessionService | None,
) -> None:
    message = callback.message if isinstance(callback.message, Message) else None
    if settings.scan_mode is ScanMode.ESCL and scan_sessions is not None:
        try:
            cancelled = await scan_sessions.cancel(user.telegram_id)
        except ScanSessionError as exc:
            await callback.answer(exc.user_message[:200], show_alert=True)
            return
        text = texts.SCAN_SESSION_CANCELLED if cancelled else texts.SCAN_NOTHING_TO_CANCEL
    else:
        cancelled = await scan_service.cancel_request(user.telegram_id)
        text = texts.SCAN_CANCELLED if cancelled else texts.SCAN_NOTHING_TO_CANCEL
    await callback.answer()
    if message is not None:
        with contextlib.suppress(TelegramAPIError):
            if message.photo:
                await message.edit_caption(caption=text, reply_markup=None)
            else:
                await message.edit_text(text, reply_markup=None)


# ====================================================================== email mode (push scans)


async def receive_scan(message: Message, user: User, scan_service: ScanService, settings: Settings) -> None:
    result = await scan_service.create_request(user.telegram_id)
    if not result.created:
        await message.answer(
            texts.SCAN_ALREADY_PENDING.format(position=result.position), reply_markup=scan_request_actions()
        )
        return
    minutes = max(1, math.ceil(settings.scan_request_timeout / 60))
    if result.position > 1:
        text = texts.SCAN_READY_EMAIL_QUEUED.format(
            ahead=result.position - 1, position=result.position, minutes=minutes
        )
    else:
        text = texts.SCAN_READY_EMAIL.format(minutes=minutes)
    await message.answer(text, reply_markup=scan_request_actions())
