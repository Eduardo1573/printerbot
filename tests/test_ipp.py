from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.printer.base import PrinterBusyError, PrinterError, PrinterTimeoutError, PrinterUnavailableError
from app.printer.hp import HPIppPrinter, describe_reasons
from app.printer.ipp import Attr, IppError, Op, Tag, decode_message, decode_response, encode_request
from app.printer.raster import (
    RasterSettings,
    UrfSettings,
    choose_raster_settings,
    choose_urf_settings,
    ghostscript_args,
    set_urf_page_count,
)
from tests.conftest import make_settings, pdf_bytes

# ------------------------------------------------------------------ codec


def test_encode_decode_roundtrip():
    payload = encode_request(
        Op.PRINT_JOB,
        7,
        [
            (Tag.OPERATION, [Attr(Tag.CHARSET, "attributes-charset", ["utf-8"])]),
            (Tag.JOB, [Attr(Tag.INTEGER, "copies", [1]), Attr(Tag.KEYWORD, "x", ["a", "b"])]),
        ],
    )
    msg, offset = decode_message(payload + b"DOCUMENT")
    assert msg.status_code == Op.PRINT_JOB and msg.request_id == 7
    assert msg.group(Tag.JOB) == {"copies": [1], "x": ["a", "b"]}
    assert payload[offset - 1] == Tag.END
    assert (payload + b"DOCUMENT")[offset:] == b"DOCUMENT"


def test_decode_resolution_range_and_collections():
    def attr(tag, name, value: bytes) -> bytes:
        return bytes([tag]) + struct.pack(">H", len(name)) + name + struct.pack(">H", len(value)) + value

    body = struct.pack(">BBHI", 2, 0, 0, 1) + bytes([Tag.PRINTER])
    body += attr(Tag.RESOLUTION, b"pwg-raster-document-resolution-supported", struct.pack(">iib", 600, 600, 3))
    body += attr(Tag.BEG_COLLECTION, b"media-col-default", b"")
    body += attr(Tag.MEMBER_NAME, b"", b"media-size")
    body += attr(Tag.END_COLLECTION, b"", b"")
    body += attr(Tag.KEYWORD, b"printer-state-reasons", b"none")
    body += bytes([Tag.END])
    resp = decode_response(body)
    printer = resp.group(Tag.PRINTER)
    assert printer["pwg-raster-document-resolution-supported"] == [(600, 600, 3)]
    assert printer["printer-state-reasons"] == ["none"]


def test_truncated_response_raises():
    with pytest.raises(IppError):
        decode_response(b"\x02\x00")


# ------------------------------------------------------------------ raster choice


def test_choose_raster_settings_prefers_600dpi_gray():
    s = choose_raster_settings([(300, 300, 3), (600, 600, 3), (1200, 1200, 3)], ["srgb_8", "sgray_8"], "a4")
    assert s == RasterSettings(resolution=600, pwg_type="sgray_8", paper_size="a4")


def test_choose_raster_settings_rejects_unknown_types():
    with pytest.raises(PrinterError):
        choose_raster_settings([(600, 600, 3)], ["cmyk_16"], "a4")


def test_choose_urf_settings_for_real_m130fn():
    keywords = ["V1.4", "CP99", "W8", "OB10", "PQ3-4-5", "IS1", "MT1-3-5", "RS600"]
    assert choose_urf_settings(keywords, "a4") == UrfSettings(resolution=600, color="W8", paper_size="a4")


def test_choose_urf_settings_resolution_range():
    assert choose_urf_settings(["W8", "RS300-600-1200"], "a4").resolution == 600
    assert choose_urf_settings(["SRGB24", "RS300"], "a4") == UrfSettings(300, "SRGB24", "a4")


def test_choose_urf_settings_rejects_unknown_color():
    with pytest.raises(PrinterError):
        choose_urf_settings(["CMYK32", "RS600"], "a4")


def test_set_urf_page_count(tmp_path):
    path = tmp_path / "x.urf"
    path.write_bytes(b"UNIRAST\0" + b"\0" * 4 + b"data")
    set_urf_page_count(path, 3)
    assert path.read_bytes() == b"UNIRAST\0\0\0\0\x03data"


async def test_pclm_is_never_used(fake_ipp, tmp_path, pdf_file, monkeypatch):
    """Ghostscript PCLm reboots M130 firmware 20170710; URF must be chosen instead."""
    fake, port = await fake_ipp(formats=("application/PCLm", "image/urf"))
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.01)
    calls = []

    async def fake_urf(src, dst, settings, pages, timeout):
        calls.append((settings, pages))
        dst.write_bytes(b"UNIRAST\0fake")

    monkeypatch.setattr("app.printer.hp.pdf_to_urf", fake_urf)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    await printer.print_pdf(pdf_file, job_name="t")
    msg, _ = fake.print_requests[0]
    assert msg.group(Tag.OPERATION)["document-format"] == ["image/urf"]
    assert calls == [(UrfSettings(600, "W8", "a4"), 2)]


async def test_printer_with_only_pclm_is_refused(fake_ipp, tmp_path, pdf_file):
    fake, port = await fake_ipp(formats=("application/PCLm",))
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    with pytest.raises(PrinterError):
        await printer.print_pdf(pdf_file, job_name="t")
    assert fake.print_requests == []


def test_ghostscript_args_are_safe_list(tmp_path):
    args = ghostscript_args(tmp_path / "a b;rm -rf.pdf", tmp_path / "out.pwg", RasterSettings())
    assert args[0] == "gs" and "-dSAFER" in args
    assert "-sDEVICE=pwgraster" in args
    assert args[-1].endswith("a b;rm -rf.pdf")  # one argv element, no shell involved


def test_describe_reasons():
    assert describe_reasons(["media-empty-error", "toner-low-warning", "none"]) == "out of paper, toner is low"


# ------------------------------------------------------------------ fake printer


class FakeIppPrinter:
    """Speaks just enough IPP to exercise HPIppPrinter end to end."""

    def __init__(self, formats=("application/pdf",), final_state=9, print_status=0x0000, printer_state=3):
        self.formats = list(formats)
        self.final_state = final_state
        self.print_status = print_status
        self.printer_state = printer_state
        self.print_requests: list[tuple[dict, bytes]] = []
        self.cancelled: list[int] = []

    def _response(self, request_id: int, status: int, groups) -> web.Response:
        out = bytearray(struct.pack(">BBHI", 2, 0, status, request_id))
        for tag, attrs in groups:
            out.append(tag)
            for t, name, value in attrs:
                data = struct.pack(">i", value) if t in (Tag.INTEGER, Tag.ENUM) else (
                    b"\x01" if value is True else b"\x00" if value is False else value.encode()
                )
                out += bytes([t]) + struct.pack(">H", len(name)) + name.encode() + struct.pack(">H", len(data)) + data
        out.append(Tag.END)
        return web.Response(body=bytes(out), content_type="application/ipp")

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        msg, offset = decode_message(body)
        op = msg.status_code
        common = (Tag.OPERATION, [(Tag.CHARSET, "attributes-charset", "utf-8"), (Tag.LANGUAGE, "attributes-natural-language", "en")])
        if op == Op.GET_PRINTER_ATTRIBUTES:
            attrs = [
                (Tag.ENUM, "printer-state", self.printer_state),
                (Tag.KEYWORD, "printer-state-reasons", "none" if self.printer_state != 5 else "media-empty-error"),
                (Tag.BOOLEAN, "printer-is-accepting-jobs", True),
                (Tag.KEYWORD, "sides-supported", "one-sided"),
                (Tag.KEYWORD, "media-supported", "iso_a4_210x297mm"),
            ] + [(Tag.MIME, "document-format-supported", f) for f in self.formats]
            if "image/urf" in self.formats:
                # Exactly what a real M130fn reports.
                attrs += [(Tag.KEYWORD, "urf-supported", k) for k in ("V1.4", "CP99", "W8", "OB10", "PQ3-4-5", "IS1", "MT1-3-5", "RS600")]
            return self._response(msg.request_id, 0, [common, (Tag.PRINTER, attrs)])
        if op == Op.PRINT_JOB:
            self.print_requests.append((msg, body[offset:]))
            if self.print_status:
                return self._response(msg.request_id, self.print_status, [common])
            return self._response(msg.request_id, 0, [common, (Tag.JOB, [(Tag.INTEGER, "job-id", 17), (Tag.ENUM, "job-state", 3)])])
        if op == Op.GET_JOB_ATTRIBUTES:
            return self._response(msg.request_id, 0, [common, (Tag.JOB, [(Tag.ENUM, "job-state", self.final_state), (Tag.KEYWORD, "job-state-reasons", "none")])])
        if op == Op.CANCEL_JOB:
            self.cancelled.append(msg.group(Tag.OPERATION)["job-id"][0])
            return self._response(msg.request_id, 0, [common])
        return self._response(msg.request_id, 0x0501, [common])


@pytest.fixture
async def fake_ipp():
    printers: list[TestServer] = []

    async def start(**kwargs):
        fake = FakeIppPrinter(**kwargs)
        app = web.Application(client_max_size=50 * 1024 * 1024)
        app.router.add_post("/ipp/print", fake.handle)
        server = TestServer(app)
        await server.start_server()
        printers.append(server)
        return fake, server.port

    yield start
    for server in printers:
        await server.close()


@pytest.fixture
def pdf_file(tmp_path) -> Path:
    path = tmp_path / "doc.pdf"
    path.write_bytes(pdf_bytes(2))
    return path


async def test_print_sends_exactly_one_copy(fake_ipp, tmp_path, pdf_file, monkeypatch):
    fake, port = await fake_ipp()
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.01)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    result = await printer.print_pdf(pdf_file, job_name="telegram-1")
    assert result.printer_job_id == 17 and result.copies == 1
    assert len(fake.print_requests) == 1
    msg, document = fake.print_requests[0]
    job = msg.group(Tag.JOB)
    assert job["copies"] == [1]
    assert job["sides"] == ["one-sided"]
    assert msg.group(Tag.OPERATION)["document-format"] == ["application/pdf"]
    assert document == pdf_file.read_bytes()


async def test_print_rasterizes_when_pdf_unsupported(fake_ipp, tmp_path, pdf_file, monkeypatch):
    fake, port = await fake_ipp(formats=("image/pwg-raster", "image/urf"))
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.01)

    async def fake_convert(src, dst, settings, timeout):
        dst.write_bytes(b"RaS2-fake")

    monkeypatch.setattr("app.printer.hp.pdf_to_pwg_raster", fake_convert)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    await printer.print_pdf(pdf_file, job_name="t")
    msg, document = fake.print_requests[0]
    assert msg.group(Tag.OPERATION)["document-format"] == ["image/pwg-raster"]
    assert msg.group(Tag.JOB)["copies"] == [1]
    assert document == b"RaS2-fake"


async def test_printer_unreachable(tmp_path, pdf_file):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed port => connection refused
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port, printer_connect_timeout=2))
    with pytest.raises(PrinterUnavailableError):
        await printer.print_pdf(pdf_file, job_name="t")


async def test_printer_out_of_paper(fake_ipp, tmp_path, pdf_file):
    fake, port = await fake_ipp(printer_state=5)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    with pytest.raises(PrinterError) as exc:
        await printer.print_pdf(pdf_file, job_name="t")
    assert "out of paper" in exc.value.user_message
    assert fake.print_requests == []


async def test_printer_busy(fake_ipp, tmp_path, pdf_file):
    _, port = await fake_ipp(print_status=0x0507)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    with pytest.raises(PrinterBusyError):
        await printer.print_pdf(pdf_file, job_name="t")


async def test_unexpected_printer_response(fake_ipp, tmp_path, pdf_file):
    _, port = await fake_ipp(print_status=0x0400)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    with pytest.raises(PrinterError):
        await printer.print_pdf(pdf_file, job_name="t")


async def test_aborted_job(fake_ipp, tmp_path, pdf_file, monkeypatch):
    _, port = await fake_ipp(final_state=8)
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.01)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    with pytest.raises(PrinterError, match="ended in state"):
        await printer.print_pdf(pdf_file, job_name="t")


async def test_print_timeout_cancels_job_that_never_started(fake_ipp, tmp_path, pdf_file, monkeypatch):
    fake, port = await fake_ipp(final_state=3)  # stays pending forever
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.05)
    settings = make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port)
    monkeypatch.setattr(settings, "print_timeout", 1)
    with pytest.raises(PrinterTimeoutError):
        await HPIppPrinter(settings).print_pdf(pdf_file, job_name="t")
    assert fake.cancelled == [17]


async def test_print_timeout_while_printing_is_not_cancelled(fake_ipp, tmp_path, pdf_file, monkeypatch):
    fake, port = await fake_ipp(final_state=5)  # processing
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.05)
    settings = make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port)
    monkeypatch.setattr(settings, "print_timeout", 1)
    result = await HPIppPrinter(settings).print_pdf(pdf_file, job_name="t")
    assert result.warning and fake.cancelled == []


# ------------------------------------------------------------------ real Ghostscript (Docker test image)


@pytest.mark.skipif(__import__("shutil").which("gs") is None, reason="ghostscript not installed")
async def test_real_ghostscript_produces_pwg_raster(tmp_path):
    from app.printer.raster import pdf_to_pwg_raster

    src = tmp_path / "in.pdf"
    src.write_bytes(pdf_bytes(pages=2))
    dst = tmp_path / "out.pwg"
    await pdf_to_pwg_raster(src, dst, RasterSettings(resolution=300, pwg_type="sgray_8", paper_size="a4"), timeout=60)
    data = dst.read_bytes()
    assert data[:4] == b"RaS2"
    header = data[4 : 4 + 1796]
    assert struct.unpack(">II", header[276:284]) == (300, 300)  # HWResolution
    assert struct.unpack(">I", header[400:404])[0] == 18  # sGray
    assert header[1732:1796].rstrip(b"\0") == b"iso_a4_210x297mm"
    assert data.count(b"RaS2") == 1 and len(data) > 1796  # one stream, two page headers follow


@pytest.mark.skipif(__import__("shutil").which("gs") is None, reason="ghostscript not installed")
async def test_real_ghostscript_rejects_garbage(tmp_path):
    from app.printer.base import DocumentRejectedError
    from app.printer.raster import pdf_to_pwg_raster

    src = tmp_path / "in.pdf"
    src.write_bytes(b"%PDF-1.4 not really")
    with pytest.raises(DocumentRejectedError):
        await pdf_to_pwg_raster(src, tmp_path / "o.pwg", RasterSettings(), timeout=60)


@pytest.mark.skipif(__import__("shutil").which("gs") is None, reason="ghostscript not installed")
async def test_end_to_end_pdf_to_raster_printer(fake_ipp, tmp_path, pdf_file, monkeypatch):
    """PDF -> real Ghostscript -> IPP Print-Job, against a raster-only printer like the M130."""
    fake, port = await fake_ipp(formats=("image/urf", "application/PCLm", "application/octet-stream"))
    monkeypatch.setattr("app.printer.hp.POLL_INTERVAL", 0.01)
    printer = HPIppPrinter(make_settings(tmp_path, printer_host="127.0.0.1", printer_port=port))
    result = await printer.print_pdf(pdf_file, job_name="e2e")
    assert result.copies == 1
    msg, document = fake.print_requests[0]
    assert msg.group(Tag.OPERATION)["document-format"] == ["image/urf"]  # never PCLm
    assert msg.group(Tag.JOB)["copies"] == [1]
    assert document[:8] == b"UNIRAST\0"
    assert struct.unpack(">I", document[8:12])[0] == 2  # page count patched in
    bpp, color_space = document[12], document[13]
    width, height, dpi = struct.unpack(">III", document[24:36])
    assert (bpp, color_space, dpi) == (8, 0, 600)  # W8 sGray, RS600
    assert (width, height) == (4958, 7017)  # A4 at 600 dpi
