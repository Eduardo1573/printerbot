from __future__ import annotations

import asyncio
import io
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app.bot import texts
from app.bot.handlers import scanning
from app.bot.keyboards import CB_SCAN_CANCEL, CB_SCAN_DONE, CB_SCAN_MORE, CB_SCAN_RESCAN
from app.database.models import ScanRequest, ScanStatus
from app.printer.base import PrinterBusyError, PrinterUnavailableError, PullScanSource, ScannedFile, ScanResult
from app.services.scan_session import ScanSessionError, ScanSessionService
from tests.conftest import authenticated, make_message, make_settings


def page_pdf(width: int) -> bytes:
    """A one-page PDF whose page width identifies it (so page order can be checked)."""
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class SequenceScanner(PullScanSource):
    """Returns page widths 100, 101, 102, ... on successive scans."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: Exception | None = None
        self.delay = 0.0

    async def get_scan(self) -> ScanResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        width = 100 + self.calls
        self.calls += 1
        return ScanResult([ScannedFile(page_pdf(width), "application/pdf", "scan.pdf")], source="escl")


def widths(content: bytes) -> list[int]:
    return [int(p.mediabox.width) for p in PdfReader(io.BytesIO(content)).pages]


async def fake_preview(pdf_path, out_path) -> bytes:
    return b"JPEG-PREVIEW"


@pytest.fixture
def escl_settings(tmp_path):
    return make_settings(tmp_path, scan_mode="escl", scan_session_timeout=600, scan_max_pages=3)


@pytest.fixture
def scanner():
    return SequenceScanner()


@pytest.fixture
def notified():
    return []


@pytest.fixture
async def sessions(session_factory, escl_settings, scanner, notified, auth_service):
    for uid in (1, 2):
        await authenticated(auth_service, uid)

    async def notify(uid, text):
        notified.append((uid, text))

    return ScanSessionService(session_factory, escl_settings, scanner, notify, preview_renderer=fake_preview)


async def records(session_factory) -> list[tuple[int, str]]:
    async with session_factory() as db:
        rows = (await db.scalars(select(ScanRequest).order_by(ScanRequest.id))).all()
    return [(r.telegram_user_id, r.status) for r in rows]


# ------------------------------------------------------------------ service


async def test_scan_more_and_make_document(sessions, session_factory, escl_settings):
    first = await sessions.scan_page(1)
    assert (first.page_number, first.total_pages, first.preview_jpeg) == (1, 1, b"JPEG-PREVIEW")
    second = await sessions.scan_page(1)
    assert (second.page_number, second.total_pages) == (2, 2)
    assert await records(session_factory) == [(1, ScanStatus.PROCESSING)]

    doc = await sessions.finish(1)
    assert doc.pages == 2 and doc.filename.endswith(".pdf")
    assert widths(doc.content) == [100, 101]  # one PDF, pages in scan order
    assert await records(session_factory) == [(1, ScanStatus.COMPLETED)]
    assert sessions.active_owner() is None
    assert [p for p in escl_settings.temp_dir.iterdir() if p.name.startswith("scan-")] == []


async def test_rescan_replaces_only_the_last_page(sessions):
    await sessions.scan_page(1)  # 100
    await sessions.scan_page(1)  # 101
    redo = await sessions.scan_page(1, replace_last=True)  # 102 replaces 101
    assert (redo.page_number, redo.total_pages, redo.replaced) == (2, 2, True)
    doc = await sessions.finish(1)
    assert widths(doc.content) == [100, 102]


async def test_rescan_without_session_is_refused(sessions, scanner):
    with pytest.raises(ScanSessionError):
        await sessions.scan_page(1, replace_last=True)
    assert scanner.calls == 0


async def test_scanner_is_exclusive_to_one_user(sessions, scanner):
    await sessions.scan_page(1)
    with pytest.raises(ScanSessionError, match="someone else"):
        await sessions.scan_page(2)
    with pytest.raises(ScanSessionError):
        await sessions.finish(2)
    assert await sessions.cancel(2) is False  # cannot cancel someone else's session
    assert scanner.calls == 1


async def test_concurrent_press_while_scanning(sessions, scanner):
    scanner.delay = 0.2
    task = asyncio.create_task(sessions.scan_page(1))
    await asyncio.sleep(0.05)
    with pytest.raises(ScanSessionError, match="already being scanned"):
        await sessions.scan_page(1)
    await task
    assert scanner.calls == 1


async def test_cancel_discards_pages(sessions, session_factory, escl_settings):
    await sessions.scan_page(1)
    assert await sessions.cancel(1) is True
    assert sessions.active_owner() is None
    assert await records(session_factory) == [(1, ScanStatus.CANCELLED)]
    assert [p for p in escl_settings.temp_dir.iterdir() if p.name.startswith("scan-")] == []
    # The other user can now scan.
    assert (await sessions.scan_page(2)).page_number == 1


async def test_failed_first_page_frees_scanner(sessions, scanner, session_factory):
    scanner.error = PrinterUnavailableError("offline")
    with pytest.raises(PrinterUnavailableError):
        await sessions.scan_page(1)
    assert sessions.active_owner() is None
    assert await records(session_factory) == [(1, ScanStatus.FAILED)]


async def test_failed_later_page_keeps_earlier_pages(sessions, scanner):
    await sessions.scan_page(1)
    scanner.error = PrinterBusyError()
    with pytest.raises(PrinterBusyError):
        await sessions.scan_page(1)
    scanner.error = None
    assert sessions.page_count(1) == 1
    doc = await sessions.finish(1)
    assert doc.pages == 1


async def test_page_limit(sessions):
    for _ in range(3):
        await sessions.scan_page(1)
    with pytest.raises(ScanSessionError, match="maximum"):
        await sessions.scan_page(1)
    # Rescanning the last page is still allowed at the limit.
    assert (await sessions.scan_page(1, replace_last=True)).total_pages == 3


async def test_size_limit(session_factory, tmp_path, scanner, auth_service):
    await authenticated(auth_service, 1)
    settings = make_settings(tmp_path, scan_mode="escl", max_scan_size_mb=1)
    huge = ScannedFile(b"%PDF-" + b"0" * (2 * 1024 * 1024), "application/pdf", "x.pdf")

    class Huge(PullScanSource):
        async def get_scan(self):
            return ScanResult([huge], "escl")

    service = ScanSessionService(session_factory, settings, Huge(), AsyncMock(), preview_renderer=fake_preview)
    with pytest.raises(ScanSessionError, match="larger than"):
        await service.scan_page(1)
    assert service.active_owner() is None  # nothing kept, scanner freed


async def test_idle_session_expires_and_notifies(sessions, session_factory, notified):
    await sessions.scan_page(1)
    sessions._session.last_activity = time.monotonic() - 601
    await sessions.expire_idle()
    assert sessions.active_owner() is None
    assert await records(session_factory) == [(1, ScanStatus.EXPIRED)]
    assert notified and notified[0][0] == 1 and "1 scanned page" in notified[0][1]


async def test_expired_session_does_not_block_others(sessions):
    await sessions.scan_page(1)
    sessions._session.last_activity = time.monotonic() - 601
    # Before the sweeper runs, another user can already take over the scanner.
    assert (await sessions.scan_page(2)).page_number == 1
    assert sessions.active_owner() == 2


async def test_finish_without_session(sessions):
    with pytest.raises(ScanSessionError):
        await sessions.finish(1)


# ------------------------------------------------------------------ handlers


def make_bot():
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()
    return bot


def make_status():
    status = MagicMock(spec=Message)
    status.message_id = 50
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    return status


async def test_scan_command_sends_preview_with_buttons(sessions, escl_settings, auth_service):
    user = await authenticated(auth_service, 1)
    message = make_message("/scan", user_id=1)
    message.answer = AsyncMock(return_value=make_status())
    photo = MagicMock(spec=Message)
    photo.message_id = 51
    message.answer_photo = AsyncMock(return_value=photo)
    await scanning.scan_entry(message, make_bot(), user, escl_settings, MagicMock(), sessions)
    message.answer.assert_awaited_with(texts.SCAN_SCANNING.format(page=1))
    kwargs = message.answer_photo.call_args.kwargs
    assert kwargs["caption"].startswith("✅ Page 1 scanned.")
    buttons = [b.callback_data for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert set(buttons) == {CB_SCAN_MORE, CB_SCAN_DONE, CB_SCAN_RESCAN, CB_SCAN_CANCEL}


async def test_make_document_handler_sends_one_pdf(sessions, auth_service):
    user = await authenticated(auth_service, 1)
    await sessions.scan_page(1)
    await sessions.scan_page(1)
    callback = MagicMock(spec=CallbackQuery)
    callback.data = CB_SCAN_DONE
    callback.answer = AsyncMock()
    msg = make_message(None, user_id=1)
    msg.edit_reply_markup = AsyncMock()
    msg.answer = AsyncMock(return_value=make_status())
    msg.answer_document = AsyncMock()
    callback.message = msg
    await scanning.make_document(callback, user, sessions)
    sent = msg.answer_document.call_args
    document = sent.args[0]
    assert widths(document.data) == [100, 101]
    assert sent.kwargs["caption"] == texts.SCAN_DOCUMENT_READY.format(pages=2)


async def test_scan_busy_by_other_user_message(sessions, escl_settings, auth_service):
    await sessions.scan_page(1)
    user2 = await authenticated(auth_service, 2)
    message = make_message("📷 Scan", user_id=2)
    status = make_status()
    message.answer = AsyncMock(return_value=status)
    await scanning.scan_entry(message, make_bot(), user2, escl_settings, MagicMock(), sessions)
    assert "someone else" in status.edit_text.call_args.args[0]


@pytest.mark.skipif(__import__("shutil").which("gs") is None, reason="ghostscript not installed")
async def test_real_preview_is_jpeg(tmp_path):
    from app.services.scan_session import render_preview

    src = tmp_path / "page.pdf"
    src.write_bytes(page_pdf(595))
    jpeg = await render_preview(src, tmp_path / "preview.jpg")
    assert jpeg is not None and jpeg[:3] == b"\xff\xd8\xff"
    assert len(jpeg) < 200_000  # small enough to send instantly
