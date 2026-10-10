"""Document feeder (ADF) scanning: eSCL side and multi-page sessions."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.bot.handlers import scanning
from app.printer.base import PrinterError
from app.printer.escl import EsclScanner
from app.services.scan_session import ScanSessionService
from tests.conftest import authenticated, make_message, make_settings
from tests.test_scan_session import SequenceScanner, fake_preview, widths

REAL_CAPS = (Path(__file__).resolve().parent / "fixtures/m130fn-escl-capabilities.xml").read_text()
STATUS = (
    '<?xml version="1.0"?><scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" '
    'xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:State>Idle</pwg:State>'
    "<scan:AdfState>{adf}</scan:AdfState></scan:ScannerStatus>"
)


# ------------------------------------------------------------------ eSCL


@pytest.fixture
async def feeder_printer(tmp_path):
    servers: list[TestServer] = []

    async def start(adf: str = "ScannerAdfLoaded", documents: int = 3):
        state = {"settings": [], "served": 0}

        async def status(_):
            return web.Response(text=STATUS.format(adf=adf), content_type="text/xml")

        async def caps(_):
            return web.Response(text=REAL_CAPS, content_type="text/xml")

        async def jobs(request):
            state["settings"].append((await request.read()).decode())
            return web.Response(status=201, headers={"Location": "http://192.168.1.23:80/eSCL/ScanJobs/7"})

        async def next_doc(_):
            if state["served"] >= documents:
                return web.Response(status=404)
            state["served"] += 1
            return web.Response(body=f"%PDF-1.4 page {state['served']}".encode(), content_type="application/pdf")

        async def delete(_):
            return web.Response(status=200)

        app = web.Application()
        app.router.add_get("/eSCL/ScannerStatus", status)
        app.router.add_get("/eSCL/ScannerCapabilities", caps)
        app.router.add_post("/eSCL/ScanJobs", jobs)
        app.router.add_get("/eSCL/ScanJobs/7/NextDocument", next_doc)
        app.router.add_delete("/eSCL/ScanJobs/7", delete)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        settings = make_settings(tmp_path, scan_mode="escl", printer_host="127.0.0.1", escl_port=server.port)
        return EsclScanner(settings), state

    yield start
    for s in servers:
        await s.close()


async def test_auto_detects_loaded_feeder(feeder_printer):
    scanner, _ = await feeder_printer(adf="ScannerAdfLoaded")
    assert await scanner.detect_source() == "Feeder"


async def test_auto_uses_glass_when_feeder_empty(feeder_printer):
    scanner, _ = await feeder_printer(adf="ScannerAdfEmpty")
    assert await scanner.detect_source() == "Platen"


async def test_feeder_scan_collects_every_document_with_paper_size(feeder_printer):
    scanner, state = await feeder_printer(documents=3)
    result = await scanner.get_scan("Feeder")
    assert [f.content for f in result.files] == [b"%PDF-1.4 page 1", b"%PDF-1.4 page 2", b"%PDF-1.4 page 3"]
    assert result.source == "feeder"
    body = state["settings"][0]
    assert "<pwg:InputSource>Feeder</pwg:InputSource>" in body
    # A4 at 300 dpi, not the feeder's legal-size maximum (4500).
    assert re.search(r"<pwg:Width>2480</pwg:Width>", body) and re.search(r"<pwg:Height>3508</pwg:Height>", body)
    assert "<scan:ColorMode>RGB24</scan:ColorMode>" in body and "<scan:XResolution>300<" in body


async def test_glass_scan_takes_one_document(feeder_printer):
    scanner, state = await feeder_printer(adf="ScannerAdfEmpty", documents=3)
    result = await scanner.get_scan("Platen")
    assert len(result.files) == 1 and result.source == "glass"
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in state["settings"][0]


@pytest.mark.parametrize(
    "adf,message",
    [
        ("ScannerAdfJam", "Paper jam"),
        ("ScannerAdfHatchOpen", "cover is open"),
        ("ScannerAdfEmpty", "No paper in the document feeder"),
    ],
)
async def test_feeder_problems_are_explained(feeder_printer, adf, message):
    scanner, state = await feeder_printer(adf=adf)
    with pytest.raises(PrinterError) as exc:
        await scanner.get_scan("Feeder")
    assert message in exc.value.user_message
    assert state["settings"] == []  # no job was started


# ------------------------------------------------------------------ sessions


@pytest.fixture
async def setup(session_factory, tmp_path, auth_service):
    await authenticated(auth_service, 1)
    scanner = SequenceScanner()

    def make(max_pages: int = 50):
        settings = make_settings(tmp_path, scan_mode="escl", scan_max_pages=max_pages)
        return ScanSessionService(session_factory, settings, scanner, AsyncMock(), preview_renderer=fake_preview)

    return make, scanner


async def test_feeder_stack_then_glass_page(setup):
    make, scanner = setup
    sessions = make()
    scanner.feeder_pages = 3
    stack = await sessions.scan_page(1, source="Feeder")
    assert (stack.page_number, stack.last_page, stack.count, stack.total_pages) == (1, 3, 3, 3)
    assert len(stack.previews) == 3
    glass = await sessions.scan_page(1, source="Platen")
    assert (glass.page_number, glass.count, glass.total_pages) == (4, 1, 4)
    doc = await sessions.finish(1)
    assert widths(doc.content) == [100, 101, 102, 103]  # stack order kept, glass page appended


async def test_rescan_after_stack_replaces_only_last_page(setup):
    make, scanner = setup
    sessions = make()
    scanner.feeder_pages = 3
    await sessions.scan_page(1)  # 100, 101, 102
    redo = await sessions.scan_page(1, replace_last=True)  # 103 replaces 102
    assert redo.replaced and redo.count == 1 and redo.total_pages == 3
    assert widths((await sessions.finish(1)).content) == [100, 101, 103]


async def test_rescan_with_stack_replaces_last_page_with_stack(setup):
    make, scanner = setup
    sessions = make()
    await sessions.scan_page(1)  # 100
    await sessions.scan_page(1)  # 101
    scanner.feeder_pages = 2
    redo = await sessions.scan_page(1, replace_last=True)  # 102, 103 replace 101
    assert (redo.page_number, redo.last_page, redo.total_pages) == (2, 3, 3)
    assert widths((await sessions.finish(1)).content) == [100, 102, 103]


async def test_stack_over_page_limit_is_truncated(setup):
    make, scanner = setup
    sessions = make(max_pages=3)
    await sessions.scan_page(1)  # 100
    scanner.feeder_pages = 4  # 101..104, only 2 fit
    result = await sessions.scan_page(1)
    assert (result.count, result.dropped, result.total_pages) == (2, 2, 3)
    assert widths((await sessions.finish(1)).content) == [100, 101, 102]


async def test_previews_limited_to_album_size(setup):
    make, scanner = setup
    sessions = make()
    scanner.feeder_pages = 12
    result = await sessions.scan_page(1)
    assert result.count == 12 and len(result.previews) == 10


async def test_scan_entry_auto_detects_and_sends_album(setup, auth_service):
    make, scanner = setup
    sessions = make()
    user = await authenticated(auth_service, 1)
    scanner.feeder_pages = 3
    message = make_message("📷 Scan", user_id=1)
    status = MagicMock(spec=Message)
    status.message_id = 10
    status.edit_text = AsyncMock()
    status.delete = AsyncMock()
    controls = MagicMock(spec=Message)
    controls.message_id = 11
    message.answer = AsyncMock(side_effect=[status, controls])
    message.answer_media_group = AsyncMock()
    bot = MagicMock()
    bot.edit_message_reply_markup = AsyncMock()

    await scanning.scan_entry(message, bot, user, make_settings(Path("/tmp"), scan_mode="escl"), MagicMock(), sessions)

    assert scanner.sources == ["Feeder"]  # detected once, passed through
    assert "document feeder" in message.answer.call_args_list[0].args[0]
    assert len(message.answer_media_group.call_args.args[0]) == 3
    caption = message.answer.call_args_list[1].args[0]
    assert caption.startswith("✅ Pages 1–3 scanned from the feeder (3 pages).")
    assert message.answer.call_args_list[1].kwargs["reply_markup"] is not None
