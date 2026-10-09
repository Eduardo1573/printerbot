from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.bot import texts
from app.bot.handlers import printing
from app.database.models import PrintJob, PrintStatus
from app.printer.base import PrinterUnavailableError
from app.services.print_service import DocumentValidationError, PrintService
from tests.conftest import FakePrinter, authenticated, make_message, pdf_bytes

# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "mime,name",
    [("image/png", "photo.png"), ("application/msword", "doc.docx"), (None, "notes.txt"), (None, None)],
)
def test_rejects_non_pdf_metadata(print_service, mime, name):
    with pytest.raises(DocumentValidationError, match="Only PDF"):
        print_service.check_metadata(mime_type=mime, file_name=name, file_size=100)


def test_rejects_too_large_file(print_service, settings):
    with pytest.raises(DocumentValidationError, match="too large"):
        print_service.check_metadata(
            mime_type="application/pdf", file_name="a.pdf", file_size=settings.max_pdf_size_bytes + 1
        )


def test_accepts_pdf_metadata(print_service):
    print_service.check_metadata(mime_type="application/pdf", file_name="x.bin", file_size=10)
    print_service.check_metadata(mime_type=None, file_name="report.PDF", file_size=10)


async def test_validate_pdf_ok(print_service, tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(pdf_bytes(pages=3))
    assert await print_service.validate_pdf(path) == 3


async def test_validate_pdf_rejects_fake_pdf(print_service, tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"MZ\x90\x00 this is an executable pretending to be a pdf")
    with pytest.raises(DocumentValidationError, match="not a valid PDF"):
        await print_service.validate_pdf(path)


async def test_validate_pdf_rejects_damaged_pdf(print_service, tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.7\n garbage garbage garbage")
    with pytest.raises(DocumentValidationError):
        await print_service.validate_pdf(path)


async def test_validate_pdf_rejects_password_protected(print_service, tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(pdf_bytes(password="secret"))
    with pytest.raises(DocumentValidationError, match="Password"):
        await print_service.validate_pdf(path)


async def test_validate_pdf_rejects_too_many_pages(print_service, tmp_path, settings):
    path = tmp_path / "long.pdf"
    path.write_bytes(pdf_bytes(pages=settings.max_pdf_pages + 1))
    with pytest.raises(DocumentValidationError, match="limit"):
        await print_service.validate_pdf(path)


async def test_validate_pdf_rejects_oversized_download(print_service, tmp_path, settings):
    # Telegram metadata can lie; the downloaded size is checked again.
    path = tmp_path / "big.pdf"
    path.write_bytes(pdf_bytes() + b"\0" * (settings.max_pdf_size_bytes + 1))
    with pytest.raises(DocumentValidationError, match="too large"):
        await print_service.validate_pdf(path)


# ------------------------------------------------------------------ print jobs


async def test_print_job_recorded(print_service, fake_printer, session_factory, auth_service, tmp_path):
    await authenticated(auth_service, 1001)
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf_bytes())
    result = await print_service.print_document(1001, path, page_count=1)
    assert result.copies == 1
    assert fake_printer.calls == [path]
    async with session_factory() as session:
        jobs = (await session.scalars(select(PrintJob))).all()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.copies == 1
    assert job.status == PrintStatus.COMPLETED
    assert job.telegram_user_id == 1001
    assert job.printer_job_id == 42
    assert job.completed_at is not None


async def test_database_refuses_more_than_one_copy(session_factory, auth_service):
    await authenticated(auth_service, 1001)
    async with session_factory() as session:
        session.add(PrintJob(telegram_user_id=1001, copies=2, file_size=1, status=PrintStatus.QUEUED))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_failed_printer_connection_marks_job_failed(session_factory, settings, auth_service, tmp_path):
    await authenticated(auth_service, 1001)
    service = PrintService(session_factory, FakePrinter(error=PrinterUnavailableError("connect refused")), settings)
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf_bytes())
    with pytest.raises(PrinterUnavailableError):
        await service.print_document(1001, path, page_count=1)
    async with session_factory() as session:
        job = (await session.scalars(select(PrintJob))).one()
    assert job.status == PrintStatus.FAILED
    assert "connect refused" in job.error


async def test_workspace_is_removed(print_service):
    async with print_service.workspace() as workdir:
        (workdir / "document.pdf").write_bytes(b"x")
        assert workdir.exists()
        assert oct(workdir.stat().st_mode & 0o777) == "0o700"
    assert not workdir.exists()


async def test_workspace_removed_on_error(print_service):
    with pytest.raises(RuntimeError):
        async with print_service.workspace() as workdir:
            raise RuntimeError("boom")
    assert not workdir.exists()


# ------------------------------------------------------------------ handler


def make_document_message(file_name="report.pdf", mime="application/pdf", size=1000):
    document = MagicMock(file_id="FILEID", file_unique_id="UNIQ", file_name=file_name, mime_type=mime, file_size=size)
    status = MagicMock(edit_text=AsyncMock())
    message = make_message(None, document=document)
    message.answer = AsyncMock(return_value=status)
    return message, status


def make_bot(content: bytes):
    bot = MagicMock()

    async def download(file_id, destination, timeout):
        assert Path(destination).name == "document.pdf"  # Telegram's filename is never used
        Path(destination).write_bytes(content)

    bot.download = AsyncMock(side_effect=download)
    return bot


async def test_handler_prints_pdf_and_cleans_up(print_service, fake_printer, auth_service, settings):
    user = await authenticated(auth_service, 1001)
    message, status = make_document_message(file_name="../../etc/passwd.pdf")
    await printing.document_received(message, make_bot(pdf_bytes()), user, print_service, settings)
    assert message.answer.call_args_list[0].args[0] == texts.PRINT_RECEIVED
    status.edit_text.assert_awaited_with(texts.PRINT_SUCCESS)
    assert len(fake_printer.calls) == 1
    # Temporary file and directory are gone; nothing was written outside temp_dir.
    assert not fake_printer.calls[0].exists()
    assert list(settings.temp_dir.iterdir()) == []


async def test_handler_rejects_non_pdf_without_download(print_service, fake_printer, auth_service, settings):
    user = await authenticated(auth_service, 1001)
    message, _ = make_document_message(file_name="photo.jpg", mime="image/jpeg")
    bot = make_bot(b"")
    await printing.document_received(message, bot, user, print_service, settings)
    bot.download.assert_not_awaited()
    assert fake_printer.calls == []
    assert "Only PDF" in message.answer.call_args.args[0]


async def test_handler_rejects_too_large_without_download(print_service, fake_printer, auth_service, settings):
    user = await authenticated(auth_service, 1001)
    message, _ = make_document_message(size=settings.max_pdf_size_bytes + 1)
    bot = make_bot(b"")
    await printing.document_received(message, bot, user, print_service, settings)
    bot.download.assert_not_awaited()
    assert "too large" in message.answer.call_args.args[0]


async def test_handler_reports_invalid_pdf(print_service, fake_printer, auth_service, settings):
    user = await authenticated(auth_service, 1001)
    message, status = make_document_message()
    await printing.document_received(message, make_bot(b"not a pdf at all"), user, print_service, settings)
    assert fake_printer.calls == []
    assert "not a valid PDF" in status.edit_text.call_args.args[0]


async def test_handler_reports_printer_failure(session_factory, auth_service, settings):
    user = await authenticated(auth_service, 1001)
    service = PrintService(session_factory, FakePrinter(error=PrinterUnavailableError("timeout")), settings)
    message, status = make_document_message()
    await printing.document_received(message, make_bot(pdf_bytes()), user, service, settings)
    text = status.edit_text.call_args.args[0]
    assert text.startswith("❌ Printing failed.")
    assert PrinterUnavailableError.user_message in text
    assert list(settings.temp_dir.iterdir()) == []
