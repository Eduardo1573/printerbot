from __future__ import annotations

import io
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aiogram.types import Message
from pypdf import PdfWriter

from app.config import Settings
from app.database.database import create_engine, create_session_factory
from app.database.models import Base
from app.printer.base import PrintBackend, PrinterStatus, PrintResult, PullScanSource, ScanResult
from app.services.auth_service import AuthService
from app.services.print_service import PrintService
from app.services.scan_service import ScanService

AUTH_CODE = "correct-horse-battery"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        telegram_bot_token="123456:TEST-TOKEN-abcdefghijklmnopqrstuvwxyz",
        auth_code=AUTH_CODE,
        database_url="sqlite+aiosqlite://",
        printer_host="192.168.1.50",
        scan_mode="email",
        scan_smtp_password="smtp-password-123456",
        temp_dir=tmp_path / "work",
        health_file=tmp_path / "health",
        max_pdf_size_mb=1,
        max_pdf_pages=5,
        scan_request_timeout=300,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path):
    # SQLite by default; set TEST_DATABASE_URL to run the same tests on PostgreSQL.
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    engine = create_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def auth_service(session_factory, settings) -> AuthService:
    return AuthService(session_factory, settings)


async def authenticated(auth_service: AuthService, telegram_id: int):
    await auth_service.touch_user(telegram_id)
    await auth_service.authenticate(telegram_id, AUTH_CODE)
    return await auth_service.touch_user(telegram_id)


class FakePrinter(PrintBackend):
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[Path] = []

    async def print_pdf(self, file_path: Path, *, job_name: str) -> PrintResult:
        self.calls.append(file_path)
        if self.error:
            raise self.error
        return PrintResult(printer_job_id=42, pages=1, copies=1)

    async def get_status(self) -> PrinterStatus:
        return PrinterStatus(online=True, state="idle")


class FakeNotifier:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.scans: list[tuple[int, ScanResult]] = []
        self.messages: list[tuple[int, str]] = []

    async def send_scan(self, telegram_id: int, result: ScanResult) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.scans.append((telegram_id, result))

    async def notify(self, telegram_id: int, text: str) -> None:
        self.messages.append((telegram_id, text))


class FakePullSource(PullScanSource):
    def __init__(self, result: ScanResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    async def get_scan(self, source: str | None = None) -> ScanResult:
        if self.error:
            raise self.error
        assert self.result is not None
        return self.result


@pytest.fixture
def fake_printer() -> FakePrinter:
    return FakePrinter()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def print_service(session_factory, fake_printer, settings) -> PrintService:
    return PrintService(session_factory, fake_printer, settings)


@pytest.fixture
def scan_service(session_factory, settings, notifier) -> ScanService:
    return ScanService(session_factory, settings, notifier)


def pdf_bytes(pages: int = 1, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    if password:
        writer.encrypt(user_password=password, owner_password=password + "-owner")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_message(text: str | None = None, user_id: int = 1001, **extra) -> MagicMock:
    """A stand-in for aiogram.types.Message with async reply methods."""
    message = MagicMock(spec=Message)
    message.text = text
    message.from_user = SimpleNamespace(id=user_id, is_bot=False)
    message.chat = SimpleNamespace(id=user_id, type="private")
    message.answer = AsyncMock(return_value=MagicMock(edit_text=AsyncMock()))
    message.delete = AsyncMock()
    message.document = None
    message.photo = None
    for key, value in extra.items():
        setattr(message, key, value)
    return message


def answered_texts(message: MagicMock) -> list[str]:
    return [c.args[0] for c in message.answer.call_args_list]
