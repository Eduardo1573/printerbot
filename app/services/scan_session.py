"""Multi-page scan sessions for pull scanners (eSCL).

Flow: 📷 Scan → page 1 → [➕ Scan more | 🔄 Rescan last page | 📄 Make document | ✖️ Cancel]
→ ... → 📄 Make document → one merged PDF.

There is one physical scanner, so only one session exists at a time. A session left
idle for `scan_session_timeout` seconds is discarded and the scanner is freed. Scanned
pages live in a private temp directory and are deleted when the session ends. Sessions
are in memory: a bot restart discards an unfinished session.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ScanRequest, ScanStatus, utcnow
from app.logging_setup import event
from app.printer.base import PrinterError, PullScanSource

log = logging.getLogger(__name__)

PreviewRenderer = Callable[[Path, Path], Awaitable[bytes | None]]
Notify = Callable[[int, str], Awaitable[None]]


class ScanSessionError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(slots=True)
class PageScanned:
    page_number: int  # 1-based position of the page just scanned
    total_pages: int
    preview_jpeg: bytes | None
    replaced: bool = False


@dataclass(slots=True)
class FinishedDocument:
    content: bytes
    filename: str
    pages: int


@dataclass(slots=True)
class _Session:
    owner: int
    workdir: Path
    record_id: int
    pages: list[Path] = field(default_factory=list)
    last_activity: float = field(default_factory=time.monotonic)
    busy: bool = False
    # message id of the last message carrying session buttons (to strip stale buttons)
    controls_message_id: int | None = None


async def render_preview(pdf_path: Path, out_path: Path) -> bytes | None:
    """Small JPEG of the first page via Ghostscript; None if rendering fails."""
    if shutil.which("gs") is None:
        return None
    proc = await asyncio.create_subprocess_exec(
        "gs", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-dQUIET", "-dNOINTERACTIVE",
        "-sDEVICE=jpeg", "-r60", "-dJPEGQ=80", "-dFirstPage=1", "-dLastPage=1",
        f"-sOutputFile={out_path}", "-f", str(pdf_path),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "TMPDIR": str(out_path.parent)},
    )  # fmt: skip
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0 or not out_path.exists():
        return None
    return out_path.read_bytes()


def _merge(pages: list[Path]) -> bytes:
    writer = PdfWriter()
    for page in pages:
        writer.append(PdfReader(str(page), strict=False))
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


class ScanSessionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        scanner: PullScanSource,
        notify: Notify,
        preview_renderer: PreviewRenderer = render_preview,
    ) -> None:
        self._sf = session_factory
        self._s = settings
        self._scanner = scanner
        self._notify = notify
        self._render_preview = preview_renderer
        self._session: _Session | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers

    def _expired(self, session: _Session) -> bool:
        return not session.busy and time.monotonic() - session.last_activity > self._s.scan_session_timeout

    def active_owner(self) -> int | None:
        return self._session.owner if self._session else None

    def page_count(self, telegram_id: int) -> int:
        s = self._session
        return len(s.pages) if s and s.owner == telegram_id else 0

    def set_controls_message(self, telegram_id: int, message_id: int | None) -> int | None:
        """Remember the message with session buttons; returns the previous one."""
        s = self._session
        if s is None or s.owner != telegram_id:
            return None
        previous, s.controls_message_id = s.controls_message_id, message_id
        return previous

    async def _create_record(self, telegram_id: int) -> int:
        now = utcnow()
        async with self._sf() as db, db.begin():
            record = ScanRequest(
                telegram_user_id=telegram_id,
                status=ScanStatus.PROCESSING,
                created_at=now,
                expires_at=now + timedelta(seconds=self._s.scan_session_timeout),
            )
            db.add(record)
            await db.flush()
            return record.id

    async def _close_record(self, record_id: int, status: ScanStatus, error: str | None = None) -> None:
        async with self._sf() as db, db.begin():
            await db.execute(
                update(ScanRequest)
                .where(ScanRequest.id == record_id)
                .values(status=status, completed_at=utcnow(), error=error)
            )

    async def _discard(self, session: _Session, status: ScanStatus, error: str | None = None) -> None:
        if self._session is session:
            self._session = None
        await asyncio.to_thread(shutil.rmtree, session.workdir, True)
        await self._close_record(session.record_id, status, error)

    async def _session_for(self, telegram_id: int, *, create: bool) -> _Session:
        current = self._session
        if current is not None and self._expired(current):
            await self._expire(current)
            current = None
        if current is not None:
            if current.owner != telegram_id:
                raise ScanSessionError(
                    "📷 The scanner is being used by someone else right now. Try again in a few minutes."
                )
            return current
        if not create:
            raise ScanSessionError("This scan session has ended. Press “📷 Scan” to start a new one.")
        self._s.temp_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        workdir = Path(tempfile.mkdtemp(prefix="scan-", dir=self._s.temp_dir))
        os.chmod(workdir, 0o700)
        try:
            record_id = await self._create_record(telegram_id)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        session = _Session(owner=telegram_id, workdir=workdir, record_id=record_id)
        self._session = session
        event(log, "scan_session_started", telegram_id=telegram_id, record_id=record_id)
        return session

    # ------------------------------------------------------------------ actions

    async def scan_page(self, telegram_id: int, *, replace_last: bool = False) -> PageScanned:
        """Scan one page into the user's session (creating it if needed)."""
        async with self._lock:
            session = await self._session_for(telegram_id, create=not replace_last)
            if session.busy:
                raise ScanSessionError("⏳ A page is already being scanned. Please wait.")
            if not replace_last and len(session.pages) >= self._s.scan_max_pages:
                raise ScanSessionError(
                    f"The document already has {self._s.scan_max_pages} pages (the maximum). "
                    "Press “📄 Make document”."
                )
            session.busy = True
        try:
            started = time.monotonic()
            result = await self._scanner.get_scan()
            pdf = next((f for f in result.files if f.mime_type == "application/pdf"), None)
            if pdf is None:
                raise PrinterError("scanner returned no PDF", user_message="The scanner returned an unexpected file.")
            replacing = replace_last and bool(session.pages)
            index = len(session.pages) - 1 if replacing else len(session.pages)
            kept = session.pages[:index] + session.pages[index + 1 :]
            if sum(p.stat().st_size for p in kept) + len(pdf.content) > self._s.max_scan_size_bytes:
                raise ScanSessionError(
                    f"This page would make the document larger than {self._s.max_scan_size_mb} MB. "
                    "Press “📄 Make document” to finish."
                )
            page_path = session.workdir / f"page-{index + 1:03d}-{time.monotonic_ns()}.pdf"
            page_path.write_bytes(pdf.content)
            if replacing:
                session.pages[index].unlink(missing_ok=True)
                session.pages[index] = page_path
            else:
                session.pages.append(page_path)
            replaced = replacing
            preview = None
            try:
                preview = await self._render_preview(page_path, session.workdir / "preview.jpg")
            except Exception:
                log.warning("scan_preview_failed")
            event(
                log,
                "scan_page_scanned",
                telegram_id=telegram_id,
                page=index + 1,
                pages=len(session.pages),
                replaced=replaced,
                bytes=len(pdf.content),
                seconds=round(time.monotonic() - started, 1),
            )
            return PageScanned(index + 1, len(session.pages), preview, replaced)
        except (PrinterError, ScanSessionError) as exc:
            if isinstance(exc, PrinterError):
                event(log, "scan_failed", level=logging.WARNING, telegram_id=telegram_id, error=str(exc))
            if not session.pages:  # nothing worth keeping: free the scanner
                await self._discard(session, ScanStatus.FAILED, str(exc)[:1000])
            raise
        finally:
            session.busy = False
            session.last_activity = time.monotonic()

    async def finish(self, telegram_id: int) -> FinishedDocument:
        async with self._lock:
            session = await self._session_for(telegram_id, create=False)
            if session.busy:
                raise ScanSessionError("⏳ Please wait until the current page is scanned.")
            if not session.pages:
                raise ScanSessionError("No pages scanned yet.")
            session.busy = True
        try:
            content = await asyncio.to_thread(_merge, session.pages)
        except Exception:
            session.busy = False
            session.last_activity = time.monotonic()
            log.exception("scan_merge_failed")
            raise ScanSessionError("Could not assemble the document. Try “📄 Make document” again.") from None
        pages = len(session.pages)
        await self._discard(session, ScanStatus.COMPLETED)
        filename = f"scan_{datetime.now().strftime('%Y-%m-%d_%H-%M')}.pdf"
        event(log, "scan_document_created", telegram_id=telegram_id, pages=pages, bytes=len(content))
        return FinishedDocument(content=content, filename=filename, pages=pages)

    async def cancel(self, telegram_id: int) -> bool:
        async with self._lock:
            session = self._session
            if session is None or session.owner != telegram_id:
                return False
            if session.busy:
                raise ScanSessionError("⏳ Please wait until the current page is scanned.")
            await self._discard(session, ScanStatus.CANCELLED)
        event(log, "scan_session_cancelled", telegram_id=telegram_id)
        return True

    async def _expire(self, session: _Session) -> None:
        pages = len(session.pages)
        await self._discard(session, ScanStatus.EXPIRED)
        event(log, "scan_session_expired", telegram_id=session.owner, pages=pages)
        try:
            await self._notify(
                session.owner,
                f"⌛ Scan session expired after {self._s.scan_session_timeout // 60} min of inactivity"
                + (f"; {pages} scanned page(s) were discarded." if pages else "."),
            )
        except Exception:
            log.warning("notify_failed", extra={"fields": {"telegram_id": session.owner}})

    async def expire_idle(self) -> None:
        async with self._lock:
            session = self._session
            if session is not None and self._expired(session):
                await self._expire(session)

    async def run_expiry_loop(self, interval: float = 15) -> None:
        while True:
            try:
                await self.expire_idle()
            except Exception:
                log.exception("scan_session_expiry_error")
            await asyncio.sleep(interval)

    def cleanup_stale_workdirs(self) -> None:
        if not self._s.temp_dir.exists():
            return
        for child in self._s.temp_dir.iterdir():
            if child.is_dir() and child.name.startswith("scan-"):
                shutil.rmtree(child, ignore_errors=True)
