from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.types import BufferedInputFile

from app.bot import texts
from app.bot.keyboards import main_menu
from app.printer.base import ScanResult

log = logging.getLogger(__name__)

ATTEMPTS = 4


class TelegramNotifier:
    """Delivers scans/notifications to users, retrying transient Telegram API failures."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def _retry(self, factory):
        delay = 2.0
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return await factory()
            except TelegramRetryAfter as exc:
                if attempt == ATTEMPTS:
                    raise
                await asyncio.sleep(exc.retry_after + 1)
            except (TelegramNetworkError, TelegramServerError):
                if attempt == ATTEMPTS:
                    raise
                log.warning("telegram_api_retry", extra={"fields": {"attempt": attempt}})
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def notify(self, telegram_id: int, text: str) -> None:
        await self._retry(lambda: self._bot.send_message(telegram_id, text, reply_markup=main_menu()))

    async def send_scan(self, telegram_id: int, result: ScanResult) -> None:
        await self._retry(lambda: self._bot.send_message(telegram_id, texts.SCAN_RECEIVED))
        for scanned in result.files:
            # Sent as documents (not photos) so Telegram does not recompress the scan.
            await self._retry(
                lambda f=scanned: self._bot.send_document(
                    telegram_id,
                    BufferedInputFile(f.content, filename=f.filename),
                    reply_markup=main_menu(),
                )
            )
