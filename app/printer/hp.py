"""HP LaserJet Pro MFP M129–M134 print backend: driverless IPP (AirPrint) over the VPN."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp
from pypdf import PdfReader

from app.config import Settings
from app.logging_setup import event
from app.printer.base import (
    PrintBackend,
    PrinterBusyError,
    PrinterError,
    PrinterProtocolError,
    PrinterStatus,
    PrinterTimeoutError,
    PrinterUnavailableError,
    PrintResult,
    Supply,
    describe_reasons,
)
from app.printer.ipp import (
    PRINTER_STATES,
    STATUS_BUSY,
    STATUS_NOT_ACCEPTING,
    TERMINAL_JOB_STATES,
    Attr,
    IppClient,
    IppError,
    IppResponse,
    JobState,
    Tag,
)
from app.printer.raster import (
    PAPER_SIZES,
    choose_raster_settings,
    choose_urf_settings,
    pdf_to_urf,
    pdf_to_pwg_raster,
)

log = logging.getLogger(__name__)

PRINTER_ATTRIBUTES = [
    "printer-state",
    "printer-state-reasons",
    "printer-is-accepting-jobs",
    "document-format-supported",
    "pwg-raster-document-resolution-supported",
    "pwg-raster-document-type-supported",
    "urf-supported",
    "sides-supported",
    "media-supported",
    "printer-make-and-model",
    "marker-names",
    "marker-types",
    "marker-levels",
    "marker-low-levels",
    "marker-high-levels",
]

POLL_INTERVAL = 2.0
JOB_SUBMIT_TIMEOUT = 300  # upper bound for uploading the raster data itself


def _count_pages(path: Path) -> int:
    return len(PdfReader(str(path), strict=False).pages)


def parse_supplies(printer: dict[str, list]) -> list[Supply]:
    """marker-* attributes are parallel lists, one entry per supply (RFC 3805 / PWG 5100.9).

    Levels are percentages of marker-high-levels; negative values mean unknown
    (-1 unavailable, -2 unknown, -3 "some remaining").
    """
    names = printer.get("marker-names", [])
    kinds = printer.get("marker-types", [])
    levels = printer.get("marker-levels", [])
    lows = printer.get("marker-low-levels", [])
    highs = printer.get("marker-high-levels", [])
    supplies: list[Supply] = []
    for i, name in enumerate(names):
        level = levels[i] if i < len(levels) and isinstance(levels[i], int) else None
        high = highs[i] if i < len(highs) and isinstance(highs[i], int) and highs[i] > 0 else 100
        if level is not None:
            level = None if level < 0 else min(100, round(level * 100 / high))
        low = lows[i] if i < len(lows) and isinstance(lows[i], int) and lows[i] >= 0 else None
        supplies.append(Supply(str(name), str(kinds[i]) if i < len(kinds) else "", level, low))
    return supplies


def _blocking_reasons(reasons: list[str]) -> list[str]:
    return [r for r in reasons if r.endswith("-error") or r in ("media-empty", "media-jam", "offline", "paused")]


class HPIppPrinter(PrintBackend):
    def __init__(self, settings: Settings, client: IppClient | None = None) -> None:
        self._settings = settings
        self._client = client or IppClient(
            settings.printer_http_url,
            settings.printer_uri,
            connect_timeout=settings.printer_connect_timeout,
            verify_tls=settings.printer_verify_tls,
        )

    async def _call(self, coro):
        try:
            return await coro
        except (aiohttp.ClientConnectionError, OSError, TimeoutError) as exc:
            raise PrinterUnavailableError(f"{type(exc).__name__}: {exc}") from exc
        except IppError as exc:
            if exc.status_code == STATUS_BUSY:
                raise PrinterBusyError(str(exc)) from exc
            raise PrinterProtocolError(str(exc)) from exc
        except aiohttp.ClientError as exc:
            raise PrinterProtocolError(f"{type(exc).__name__}: {exc}") from exc

    async def _attributes(self) -> IppResponse:
        return await self._call(self._client.get_printer_attributes(PRINTER_ATTRIBUTES))

    async def get_status(self) -> PrinterStatus:
        resp = await self._attributes()
        printer = resp.group(Tag.PRINTER)
        state = PRINTER_STATES.get(resp.first(Tag.PRINTER, "printer-state"), "unknown")
        return PrinterStatus(
            online=True,
            state=state,
            reasons=[r for r in printer.get("printer-state-reasons", []) if r != "none"],
            document_formats=list(printer.get("document-format-supported", [])),
            supplies=parse_supplies(printer),
            model=resp.first(Tag.PRINTER, "printer-make-and-model"),
        )

    async def print_pdf(self, file_path: Path, *, job_name: str) -> PrintResult:
        deadline = time.monotonic() + self._settings.print_timeout

        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PrinterTimeoutError("print deadline exceeded")
            return left

        attrs = await self._attributes()
        printer = attrs.group(Tag.PRINTER)
        reasons = list(printer.get("printer-state-reasons", []))
        if attrs.first(Tag.PRINTER, "printer-is-accepting-jobs", True) is False:
            raise PrinterBusyError(
                "printer not accepting jobs", user_message="The printer is not accepting jobs right now."
            )
        if attrs.first(Tag.PRINTER, "printer-state") == 5 or _blocking_reasons(reasons):
            raise PrinterError(
                f"printer stopped: {reasons}",
                user_message=f"Printer needs attention: {describe_reasons(reasons) or 'stopped'}.",
            )

        formats = list(printer.get("document-format-supported", []))
        if "application/pdf" in formats:
            document, document_format = file_path, "application/pdf"
        elif "image/pwg-raster" in formats:
            raster = choose_raster_settings(
                printer.get("pwg-raster-document-resolution-supported"),
                printer.get("pwg-raster-document-type-supported"),
                self._settings.printer_paper_size,
            )
            document = file_path.with_suffix(".pwg")
            started = time.monotonic()
            await pdf_to_pwg_raster(file_path, document, raster, timeout=remaining())
            event(
                log,
                "pdf_rasterized",
                format="pwg",
                dpi=raster.resolution,
                pwg_type=raster.pwg_type,
                raster_bytes=document.stat().st_size,
                seconds=round(time.monotonic() - started, 2),
            )
            document_format = "image/pwg-raster"
        elif "image/urf" in formats:
            urf = choose_urf_settings(printer.get("urf-supported"), self._settings.printer_paper_size)
            pages = await asyncio.to_thread(_count_pages, file_path)
            document = file_path.with_suffix(".urf")
            started = time.monotonic()
            await pdf_to_urf(file_path, document, urf, pages, timeout=remaining())
            event(
                log,
                "pdf_rasterized",
                format="urf",
                dpi=urf.resolution,
                color=urf.color,
                pages=pages,
                raster_bytes=document.stat().st_size,
                seconds=round(time.monotonic() - started, 2),
            )
            document_format = "image/urf"
        else:
            # application/PCLm is deliberately not used: it reboots M130 firmware 20170710.
            raise PrinterError(
                f"unsupported document formats: {formats}",
                user_message="The printer does not advertise a supported document format (PDF, PWG raster or URF).",
            )

        # Exactly one copy — never configurable.
        job_attrs = [Attr(Tag.INTEGER, "copies", [1])]
        if "one-sided" in printer.get("sides-supported", []):
            job_attrs.append(Attr(Tag.KEYWORD, "sides", ["one-sided"]))
        media = PAPER_SIZES[self._settings.printer_paper_size][1]
        if media in printer.get("media-supported", []):
            job_attrs.append(Attr(Tag.KEYWORD, "media", [media]))

        resp = await self._call(
            self._client.print_job(
                document,
                document_format=document_format,
                job_name=job_name,
                job_attrs=job_attrs,
                timeout=min(remaining(), JOB_SUBMIT_TIMEOUT),
            )
        )
        if resp.status_code in (STATUS_BUSY, STATUS_NOT_ACCEPTING):
            raise PrinterBusyError(f"Print-Job status 0x{resp.status_code:04x}")
        if not resp.ok:
            raise PrinterProtocolError(f"Print-Job failed with status 0x{resp.status_code:04x}")
        job_id = resp.first(Tag.JOB, "job-id")
        if not isinstance(job_id, int):
            raise PrinterProtocolError("Print-Job response has no job-id")
        event(log, "printer_accepted_job", printer_job_id=job_id, format=document_format)
        return await self._wait_for_job(job_id, deadline)

    async def _wait_for_job(self, job_id: int, deadline: float) -> PrintResult:
        state: int | None = None
        reasons: list[str] = []
        pages: int | None = None
        while time.monotonic() < deadline:
            try:
                resp = await self._call(self._client.get_job_attributes(job_id))
            except PrinterUnavailableError:
                # Transient: the printer's web server can be sluggish while printing.
                await asyncio.sleep(POLL_INTERVAL)
                continue
            if resp.ok:
                state = resp.first(Tag.JOB, "job-state")
                reasons = list(resp.group(Tag.JOB).get("job-state-reasons", []))
                pages = resp.first(Tag.JOB, "job-impressions-completed", pages)
                if state in TERMINAL_JOB_STATES:
                    break
            elif resp.status_code == 0x0406:  # client-error-not-found: job already purged => done
                state = JobState.COMPLETED
                break
            await asyncio.sleep(POLL_INTERVAL)

        if state == JobState.COMPLETED:
            return PrintResult(printer_job_id=job_id, pages=pages, copies=1)
        if state in (JobState.CANCELED, JobState.ABORTED):
            raise PrinterError(
                f"job {job_id} ended in state {state}: {reasons}",
                user_message=f"The printer aborted the job{': ' + describe_reasons(reasons) if describe_reasons(reasons) else ''}.",
            )

        if state == JobState.PROCESSING and not _blocking_reasons(reasons):
            # Pages are coming out; cancelling mid-document would waste paper.
            return PrintResult(
                printer_job_id=job_id,
                pages=pages,
                copies=1,
                warning="The printer is still printing; completion was not confirmed in time.",
            )

        # Deadline reached before printing started (or printer stuck): cancel so the
        # document doesn't print unexpectedly hours later.
        printer_reasons: list[str] = []
        try:
            await self._call(self._client.cancel_job(job_id))
            attrs = await self._attributes()
            printer_reasons = list(attrs.group(Tag.PRINTER).get("printer-state-reasons", []))
        except PrinterError:
            log.warning("cancel_after_timeout_failed", extra={"fields": {"printer_job_id": job_id}})
        detail = describe_reasons(printer_reasons + reasons)
        raise PrinterTimeoutError(
            f"job {job_id} not completed before deadline (state={state})",
            user_message="The printer did not finish in time; the job was cancelled"
            + (f" (printer reports: {detail})." if detail else "."),
        )
