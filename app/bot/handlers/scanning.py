from __future__ import annotations

import contextlib
import math

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from app.bot import texts
from app.bot.keyboards import BTN_SCAN, CB_SCAN_CANCEL, CB_SCAN_START, main_menu, scan_request_actions
from app.bot.middleware import AuthRequiredMiddleware
from app.config import ScanMode, Settings
from app.database.models import User
from app.printer.base import PrinterBusyError, PrinterError
from app.services.scan_service import ScanRequestError, ScanService

router = Router(name="scanning")
router.message.middleware(AuthRequiredMiddleware())
router.callback_query.middleware(AuthRequiredMiddleware())


@router.message(F.text == BTN_SCAN)
async def receive_scan(message: Message, user: User, scan_service: ScanService, settings: Settings) -> None:
    if settings.scan_mode is ScanMode.DISABLED:
        await message.answer(texts.SCAN_DISABLED, reply_markup=main_menu())
        return
    result = await scan_service.create_request(user.telegram_id)
    pull = settings.scan_mode is ScanMode.ESCL
    if not result.created:
        await message.answer(
            texts.SCAN_ALREADY_PENDING.format(position=result.position),
            reply_markup=scan_request_actions(pull_mode=pull),
        )
        return
    minutes = max(1, math.ceil(settings.scan_request_timeout / 60))
    if pull:
        feeder = " (or in the document feeder)" if settings.escl_input_source == "Feeder" else ""
        text = texts.SCAN_READY_ESCL.format(minutes=minutes, feeder=feeder)
    elif result.position > 1:
        text = texts.SCAN_READY_EMAIL_QUEUED.format(
            ahead=result.position - 1, position=result.position, minutes=minutes
        )
    else:
        text = texts.SCAN_READY_EMAIL.format(minutes=minutes)
    await message.answer(text, reply_markup=scan_request_actions(pull_mode=pull))


@router.callback_query(F.data == CB_SCAN_CANCEL)
async def cancel_scan(callback: CallbackQuery, user: User, scan_service: ScanService) -> None:
    cancelled = await scan_service.cancel_request(user.telegram_id)
    await callback.answer()
    text = texts.SCAN_CANCELLED if cancelled else texts.SCAN_NOTHING_TO_CANCEL
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_text(text)


@router.callback_query(F.data == CB_SCAN_START)
async def start_scan(callback: CallbackQuery, user: User, scan_service: ScanService, settings: Settings) -> None:
    if settings.scan_mode is not ScanMode.ESCL:
        await callback.answer()
        return
    await callback.answer(texts.SCAN_SCANNING)
    message = callback.message if isinstance(callback.message, Message) else None
    if message:
        with contextlib.suppress(TelegramAPIError):
            await message.edit_text(texts.SCAN_SCANNING)
    try:
        await scan_service.run_pull_scan(user.telegram_id)
    except ScanRequestError as exc:
        if message:
            await message.answer(f"❌ {exc.user_message}", reply_markup=main_menu())
    except PrinterBusyError as exc:
        # Nothing was scanned and the request is still pending: offer a retry.
        if message:
            await message.answer(f"⏳ {exc.user_message}", reply_markup=scan_request_actions(pull_mode=True))
    except PrinterError as exc:
        if message:
            await message.answer(texts.SCAN_FAILED.format(reason=exc.user_message), reply_markup=main_menu())
