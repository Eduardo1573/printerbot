"""Printer-agnostic interfaces. Telegram handlers and services only depend on these."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class PrinterError(Exception):
    """Base error; `user_message` is safe to show to Telegram users."""

    user_message = "Unexpected printer error."

    def __init__(self, detail: str = "", user_message: str | None = None) -> None:
        super().__init__(detail or self.user_message)
        if user_message:
            self.user_message = user_message


class PrinterUnavailableError(PrinterError):
    user_message = "Printer is unreachable (offline, powered off, or the home VPN tunnel is down)."


class PrinterBusyError(PrinterError):
    user_message = "Printer is busy. Try again in a moment."


class PrinterTimeoutError(PrinterError):
    user_message = "The printer did not finish in time."


class PrinterProtocolError(PrinterError):
    user_message = "The printer returned an unexpected response."


class DocumentRejectedError(PrinterError):
    user_message = "The document could not be processed."


@dataclass(slots=True)
class PrintResult:
    printer_job_id: int | None
    pages: int | None = None
    copies: int = 1
    warning: str | None = None


@dataclass(slots=True)
class Supply:
    name: str  # e.g. "Black Cartridge HP CF217A"
    kind: str  # IPP marker-type: "toner", "opc" (imaging drum), ...
    level: int | None  # percent 0-100; None = printer doesn't know
    low_level: int | None = None

    @property
    def is_low(self) -> bool:
        return self.level is not None and self.low_level is not None and self.level <= self.low_level


@dataclass(slots=True)
class PrinterStatus:
    online: bool
    state: str | None = None
    reasons: list[str] = field(default_factory=list)
    document_formats: list[str] = field(default_factory=list)
    supplies: list[Supply] = field(default_factory=list)
    model: str | None = None


@dataclass(slots=True)
class ScannedFile:
    content: bytes
    mime_type: str
    filename: str


@dataclass(slots=True)
class ScanResult:
    files: list[ScannedFile]
    source: str  # "email" | "escl"

    @property
    def total_size(self) -> int:
        return sum(len(f.content) for f in self.files)


class PrintBackend(ABC):
    @abstractmethod
    async def print_pdf(self, file_path: Path, *, job_name: str) -> PrintResult:
        """Print the PDF. Implementations MUST request exactly one copy."""

    @abstractmethod
    async def get_status(self) -> PrinterStatus: ...


# Returns True when a waiting Telegram user claimed the scan.
ScanHandler = Callable[[ScanResult], Awaitable[bool]]


class PushScanSource(ABC):
    """The printer sends scans on its own (e.g. Scan to E-mail).

    Ownership is decided by the scan queue when a scan arrives.
    """

    @abstractmethod
    async def start(self, on_scan: ScanHandler) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...


class PullScanSource(ABC):
    """The bot asks the printer to scan (e.g. eSCL). The requester owns the result."""

    @abstractmethod
    async def get_scan(self, source: str | None = None) -> ScanResult:
        """Scan from `source` ("Platen" = glass, "Feeder" = document feeder)."""

    async def detect_source(self) -> str:
        """Where the next scan should come from; scanners without a feeder use the glass."""
        return "Platen"


# IPP printer-state-reasons keywords -> plain words (shared by all IPP printers).
REASON_TEXT = {
    "media-empty": "out of paper",
    "media-needed": "out of paper",
    "media-jam": "paper jam",
    "toner-empty": "toner is empty",
    "toner-low": "toner is low",
    "marker-supply-low": "toner is low",
    "marker-supply-empty": "toner is empty",
    "door-open": "a door is open",
    "cover-open": "a cover is open",
    "input-tray-missing": "paper tray is missing",
    "offline": "printer is offline",
    "paused": "printer is paused",
    "shutdown": "printer is shutting down",
}


def describe_reasons(reasons: list[str]) -> str:
    texts: list[str] = []
    for reason in reasons:
        base = reason
        for suffix in ("-error", "-warning", "-report"):
            base = base.removesuffix(suffix)
        if base == "none":
            continue
        text = REASON_TEXT.get(base, base.replace("-", " "))
        if text not in texts:
            texts.append(text)
    return ", ".join(texts)
