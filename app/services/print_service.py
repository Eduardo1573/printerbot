from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import PrintJob, PrintStatus, utcnow
from app.logging_setup import event
from app.printer.base import PrintBackend, PrinterError, PrintResult

log = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"


class DocumentValidationError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class PrintService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        backend: PrintBackend,
        settings: Settings,
    ) -> None:
        self._sf = session_factory
        self._backend = backend
        self._s = settings
        self._slots = asyncio.Semaphore(settings.max_concurrent_print_jobs)

    @property
    def busy(self) -> bool:
        return self._slots.locked()

    # ------------------------------------------------------------------ validation

    def check_metadata(self, *, mime_type: str | None, file_name: str | None, file_size: int | None) -> None:
        """Cheap checks on Telegram metadata before downloading anything."""
        is_pdf_mime = (mime_type or "").lower() == "application/pdf"
        is_pdf_name = (file_name or "").lower().endswith(".pdf")
        if not (is_pdf_mime or is_pdf_name):
            raise DocumentValidationError("Only PDF files can be printed.")
        if file_size is None:
            raise DocumentValidationError("Could not determine the file size.")
        if file_size > self._s.max_pdf_size_bytes:
            raise DocumentValidationError(f"The PDF is too large. Maximum size is {self._s.max_pdf_size_mb} MB.")
        if file_size == 0:
            raise DocumentValidationError("The file is empty.")

    def _inspect_pdf(self, path: Path) -> int:
        size = path.stat().st_size
        if size == 0:
            raise DocumentValidationError("The file is empty.")
        if size > self._s.max_pdf_size_bytes:
            raise DocumentValidationError(f"The PDF is too large. Maximum size is {self._s.max_pdf_size_mb} MB.")
        with path.open("rb") as fh:
            head = fh.read(1024)
        # The PDF spec allows junk before the header, but only within the first 1024 bytes.
        if PDF_MAGIC not in head:
            raise DocumentValidationError("The file is not a valid PDF.")
        try:
            reader = PdfReader(str(path), strict=False)
            if reader.is_encrypted:
                # Owner-password-only PDFs open with an empty user password.
                if not reader.decrypt(""):
                    raise DocumentValidationError("Password-protected PDFs cannot be printed.")
            pages = len(reader.pages)
        except DocumentValidationError:
            raise
        except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, RecursionError, NotImplementedError):
            raise DocumentValidationError("The PDF is damaged or unreadable.") from None
        if pages == 0:
            raise DocumentValidationError("The PDF has no pages.")
        if pages > self._s.max_pdf_pages:
            raise DocumentValidationError(f"The PDF has {pages} pages; the limit is {self._s.max_pdf_pages}.")
        return pages

    async def validate_pdf(self, path: Path) -> int:
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._inspect_pdf, path), timeout=30)
        except TimeoutError:
            raise DocumentValidationError("The PDF is too complex to process.") from None

    # ------------------------------------------------------------------ temp files

    @asynccontextmanager
    async def workspace(self) -> AsyncIterator[Path]:
        """Private per-job temp directory, always removed afterwards."""
        self._s.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = Path(tempfile.mkdtemp(prefix="job-", dir=self._s.temp_dir))
        os.chmod(path, 0o700)
        try:
            yield path
        finally:
            await asyncio.to_thread(shutil.rmtree, path, True)

    def cleanup_stale_workspaces(self) -> None:
        if not self._s.temp_dir.exists():
            return
        for child in self._s.temp_dir.iterdir():
            if child.is_dir() and child.name.startswith("job-"):
                shutil.rmtree(child, ignore_errors=True)

    # ------------------------------------------------------------------ printing

    async def create_job(self, telegram_id: int, file_size: int, page_count: int | None) -> int:
        async with self._sf() as session, session.begin():
            job = PrintJob(
                telegram_user_id=telegram_id,
                status=PrintStatus.QUEUED,
                copies=1,
                file_size=file_size,
                page_count=page_count,
            )
            session.add(job)
            await session.flush()
            return job.id

    async def _update_job(self, job_id: int, **values) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(update(PrintJob).where(PrintJob.id == job_id).values(**values))

    async def print_document(self, telegram_id: int, path: Path, page_count: int | None) -> PrintResult:
        file_size = path.stat().st_size
        job_id = await self.create_job(telegram_id, file_size, page_count)
        event(log, "print_job_queued", job_id=job_id, telegram_id=telegram_id, bytes=file_size, pages=page_count)
        async with self._slots:
            await self._update_job(job_id, status=PrintStatus.PROCESSING)
            event(log, "print_job_started", job_id=job_id)
            try:
                result = await self._backend.print_pdf(path, job_name=f"telegram-{job_id}")
            except PrinterError as exc:
                await self._update_job(job_id, status=PrintStatus.FAILED, error=str(exc)[:1000], completed_at=utcnow())
                event(log, "print_job_failed", level=logging.WARNING, job_id=job_id, error=str(exc), kind=type(exc).__name__)
                raise
            except Exception as exc:
                await self._update_job(job_id, status=PrintStatus.FAILED, error=repr(exc)[:1000], completed_at=utcnow())
                log.exception("print_job_crashed", extra={"fields": {"job_id": job_id}})
                raise PrinterError(repr(exc)) from exc
        await self._update_job(
            job_id,
            status=PrintStatus.COMPLETED,
            printer_job_id=result.printer_job_id,
            completed_at=utcnow(),
            error=result.warning,
        )
        event(log, "print_job_completed", job_id=job_id, printer_job_id=result.printer_job_id, warning=result.warning)
        return result
