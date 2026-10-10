from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer

from app.bot.handlers.status import cmd_status, format_status
from app.printer.base import PrinterStatus, PrinterUnavailableError, Supply
from app.printer.hp import HPIppPrinter, parse_supplies
from app.printer.ipp import Tag
from tests.conftest import FakePrinter, make_message, make_settings
from tests.test_ipp import FakeIppPrinter

DUMP = Path(__file__).resolve().parent.parent / "docs/printer-dumps/m130fn-ipp-attributes.json"


def test_parse_supplies_from_real_m130fn():
    printer = json.loads(DUMP.read_text())
    supplies = parse_supplies(printer)
    assert supplies == [
        Supply("Black Cartridge HP CF217A", "toner", 0, 3),
        Supply("Imaging Drum HP CF219A", "opc", 70, 1),
    ]
    assert supplies[0].is_low and not supplies[1].is_low


def test_parse_supplies_unknown_and_scaled_levels():
    supplies = parse_supplies(
        {
            "marker-names": ["A", "B"],
            "marker-types": ["toner", "toner"],
            "marker-levels": [-3, 50],
            "marker-high-levels": [100, 200],
            "marker-low-levels": [-1, 10],
        }
    )
    assert supplies == [Supply("A", "toner", None, None), Supply("B", "toner", 25, 10)]


def test_format_status_real_values():
    text = format_status(
        PrinterStatus(
            online=True,
            state="idle",
            reasons=["toner-low-warning"],
            supplies=[Supply("Black Cartridge HP CF217A", "toner", 0, 3), Supply("Imaging Drum HP CF219A", "opc", 70, 1)],
        )
    )
    assert "✅ Ready" in text
    assert "⚠️ Toner is low" in text
    assert "▱▱▱▱▱▱▱▱▱▱ 0% ⚠️ low" in text
    assert "▰▰▰▰▰▰▰▱▱▱ 70%" in text
    assert "CF217A" in text and "not an original HP" in text


def test_format_status_escapes_html():
    text = format_status(PrinterStatus(online=True, state="idle", supplies=[Supply("<b>x</b>", "toner", 80, 5)]))
    assert "&lt;b&gt;x&lt;/b&gt;" in text and "not an original" not in text


async def test_status_end_to_end_over_ipp(tmp_path):
    fake = FakeIppPrinter()
    original = fake._response

    def with_markers(request_id, status, groups):
        groups = [
            (tag, attrs + [
                (Tag.NAME, "marker-names", "Black Cartridge HP CF217A"),
                (Tag.NAME, "marker-names", "Imaging Drum HP CF219A"),
                (Tag.KEYWORD, "marker-types", "toner"),
                (Tag.KEYWORD, "marker-types", "opc"),
                (Tag.INTEGER, "marker-levels", 0),
                (Tag.INTEGER, "marker-levels", 70),
            ]) if tag == Tag.PRINTER else (tag, attrs)
            for tag, attrs in groups
        ]
        return original(request_id, status, groups)

    fake._response = with_markers
    app = web.Application()
    app.router.add_post("/ipp/print", fake.handle)
    server = TestServer(app)
    await server.start_server()
    try:
        printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=server.port))
        status = await printer.get_status()
    finally:
        await server.close()
    assert [(s.kind, s.level) for s in status.supplies] == [("toner", 0), ("opc", 70)]
    assert status.state == "idle"


async def test_status_command_printer_unreachable(session_factory, settings):
    from app.services.print_service import PrintService

    class Offline(FakePrinter):
        async def get_status(self):
            raise PrinterUnavailableError("no route")

    message = make_message("/status")
    await cmd_status(message, PrintService(session_factory, Offline(), settings))
    text = message.answer.call_args.args[0]
    assert text.startswith("❌ Can't get the printer status.")
    assert PrinterUnavailableError.user_message in text
