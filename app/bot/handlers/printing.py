from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message

from app.bot import texts
from app.bot.keyboards import BTN_PRINT, main_menu
from app.bot.middleware import AuthRequiredMiddleware
from app.config import Settings
from app.database.models import User
from app.logging_setup import event
from app.printer.base import PrinterError
from app.services.print_service import DocumentValidationError, PrintService

log = logging.getLogger(__name__)

router = Router(name="printing")
router.message.middleware(AuthRequiredMiddleware())
router.callback_query.middleware(AuthRequiredMiddleware())

DOWNLOAD_TIMEOUT = 120


@router.message(F.text == BTN_PRINT)
async def print_button(message: Message) -> None:
    await message.answer(texts.PRINT_PROMPT, reply_markup=main_menu())


@router.message(F.photo)
async def photo_received(message: Message) -> None:
    await message.answer(texts.PHOTO_NOT_SUPPORTED, reply_markup=main_menu())


@router.message(F.document)
async def document_received(
    message: Message, bot: Bot, user: User, print_service: PrintService, settings: Settings
) -> None:
    doc = message.document
    assert doc is not None
    try:
        print_service.check_metadata(mime_type=doc.mime_type, file_name=doc.file_name, file_size=doc.file_size)
    except DocumentValidationError as exc:
        event(log, "pdf_rejected", telegram_id=user.telegram_id, reason=exc.user_message, bytes=doc.file_size)
        await message.answer(f"❌ {exc.user_message}", reply_markup=main_menu())
        return

    event(log, "pdf_received", telegram_id=user.telegram_id, bytes=doc.file_size, file_unique_id=doc.file_unique_id)
    status = await message.answer(texts.PRINT_QUEUED if print_service.busy else texts.PRINT_RECEIVED)

    async def update_status(text: str) -> None:
        try:
            await status.edit_text(text)
        except TelegramAPIError:
            await message.answer(text, reply_markup=main_menu())

    async with print_service.workspace() as workdir:
        # Never trust the Telegram file name: use a fixed internal name.
        path = workdir / "document.pdf"
        try:
            await bot.download(doc.file_id, destination=path, timeout=DOWNLOAD_TIMEOUT)
        except TelegramBadRequest as exc:
            log.warning("telegram_download_failed", extra={"fields": {"error": str(exc)}})
            await update_status(texts.PRINT_FAILED.format(reason="Telegram refused the download (file too big?)."))
            return
        except (TelegramAPIError, OSError, TimeoutError) as exc:
            log.warning("telegram_download_failed", extra={"fields": {"error": type(exc).__name__}})
            await update_status(texts.PRINT_FAILED.format(reason="Could not download the file from Telegram."))
            return

        try:
            pages = await print_service.validate_pdf(path)
        except DocumentValidationError as exc:
            event(log, "pdf_rejected", telegram_id=user.telegram_id, reason=exc.user_message)
            await update_status(texts.PRINT_FAILED.format(reason=exc.user_message))
            return

        if print_service.busy:
            await update_status(texts.PRINT_QUEUED)
        try:
            result = await print_service.print_document(user.telegram_id, path, pages)
        except PrinterError as exc:
            await update_status(texts.PRINT_FAILED.format(reason=exc.user_message))
            return

    text = texts.PRINT_SUCCESS
    if result.warning:
        text += f"\n\nℹ️ {result.warning}"
    await update_status(text)
