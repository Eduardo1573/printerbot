from __future__ import annotations

import asyncio
import email.policy
import smtplib
import socket
from email.message import EmailMessage

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.printer.base import PrinterBusyError, ScanResult
from app.printer.escl import EsclScanner, build_scan_settings, parse_capabilities
from app.printer.smtp_receiver import SmtpScanReceiver, extract_scan
from tests.conftest import make_settings, pdf_bytes

SMTP_PASSWORD = "smtp-password-123456"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scan_email(attachment: bytes | None = None, ctype=("application", "pdf"), name="../../evil.pdf") -> bytes:
    msg = EmailMessage()
    msg["From"] = "printer@home"
    msg["To"] = "scan@printerbot"
    msg["Subject"] = "Scanned document"
    msg.set_content("Scan attached")
    if attachment is not None:
        msg.add_attachment(attachment, maintype=ctype[0], subtype=ctype[1], filename=name)
    return msg.as_bytes(policy=email.policy.SMTP)


# ------------------------------------------------------------------ e-mail parsing


def test_extract_scan_pdf_ignores_sender_filename():
    result = extract_scan(scan_email(pdf_bytes()), max_size=10_000_000)
    assert len(result.files) == 1
    f = result.files[0]
    assert f.mime_type == "application/pdf"
    assert f.content.startswith(b"%PDF-")
    assert "/" not in f.filename and "evil" not in f.filename


def test_extract_scan_sniffs_octet_stream():
    result = extract_scan(scan_email(b"\xff\xd8\xff\xe0jpegdata", ctype=("application", "octet-stream")), 10_000)
    assert result.files[0].mime_type == "image/jpeg"


def test_extract_scan_skips_other_attachments():
    result = extract_scan(scan_email(b"MZ...", ctype=("application", "x-msdownload")), 10_000)
    assert result.files == []


def test_extract_scan_enforces_size():
    with pytest.raises(ValueError):
        extract_scan(scan_email(b"%PDF-" + b"x" * 5000), max_size=1000)


# ------------------------------------------------------------------ SMTP server


@pytest.fixture
async def smtp_server(tmp_path):
    receivers: list[SmtpScanReceiver] = []

    async def start(accept: bool = True, networks: str = "127.0.0.0/8"):
        settings = make_settings(
            tmp_path,
            scan_smtp_listen_host="127.0.0.1",
            scan_smtp_listen_port=free_port(),
            scan_smtp_allowed_networks=networks,
        )
        received: list[ScanResult] = []

        async def on_scan(result: ScanResult) -> bool:
            received.append(result)
            return accept

        receiver = SmtpScanReceiver(settings)
        await receiver.start(on_scan)
        receivers.append(receiver)
        return settings.scan_smtp_listen_port, received

    yield start
    for r in receivers:
        await r.stop()


def send(port: int, raw: bytes, password: str | None = SMTP_PASSWORD) -> None:
    with smtplib.SMTP("127.0.0.1", port, timeout=10) as client:
        client.ehlo()
        if password is not None:
            client.login("printer", password)
        client.sendmail("printer@home", ["scan@printerbot"], raw)


async def test_smtp_receives_scan(smtp_server):
    port, received = await smtp_server()
    await asyncio.to_thread(send, port, scan_email(pdf_bytes()))
    assert len(received) == 1 and received[0].files[0].mime_type == "application/pdf"


async def test_smtp_requires_authentication(smtp_server):
    port, received = await smtp_server()
    with pytest.raises(smtplib.SMTPException):
        await asyncio.to_thread(send, port, scan_email(pdf_bytes()), None)
    assert received == []


async def test_smtp_rejects_wrong_password(smtp_server):
    port, received = await smtp_server()
    with pytest.raises(smtplib.SMTPAuthenticationError):
        await asyncio.to_thread(send, port, scan_email(pdf_bytes()), "wrong-password-1234")
    assert received == []


async def test_smtp_refuses_disallowed_network(smtp_server):
    port, received = await smtp_server(networks="10.0.0.0/8")
    with pytest.raises((smtplib.SMTPException, OSError)):
        await asyncio.to_thread(send, port, scan_email(pdf_bytes()))
    assert received == []


async def test_smtp_reports_no_recipient_to_printer(smtp_server):
    port, _ = await smtp_server(accept=False)
    with pytest.raises(smtplib.SMTPDataError) as exc:
        await asyncio.to_thread(send, port, scan_email(pdf_bytes()))
    assert exc.value.smtp_code == 550


async def test_smtp_test_message_without_scan_is_discarded(smtp_server):
    # The EWS "Save and Test" button must succeed, but nothing is delivered.
    port, received = await smtp_server()
    await asyncio.to_thread(send, port, scan_email(None))
    assert received == []


# ------------------------------------------------------------------ eSCL

CAPS = """<?xml version="1.0"?>
<scan:ScannerCapabilities xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
 <scan:Platen><scan:PlatenInputCaps>
  <scan:MaxWidth>2550</scan:MaxWidth><scan:MaxHeight>3508</scan:MaxHeight>
  <scan:SettingProfiles><scan:SettingProfile>
   <scan:ColorModes><scan:ColorMode>Grayscale8</scan:ColorMode><scan:ColorMode>RGB24</scan:ColorMode></scan:ColorModes>
   <scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat></scan:DocumentFormats>
   <scan:SupportedResolutions><scan:DiscreteResolutions><scan:DiscreteResolution>
     <scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution>
   </scan:DiscreteResolution></scan:DiscreteResolutions></scan:SupportedResolutions>
  </scan:SettingProfile></scan:SettingProfiles>
 </scan:PlatenInputCaps></scan:Platen>
</scan:ScannerCapabilities>"""

STATUS = """<?xml version="1.0"?><scan:ScannerStatus xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03" xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm"><pwg:State>{state}</pwg:State></scan:ScannerStatus>"""


def test_parse_capabilities():
    caps = parse_capabilities(CAPS, "Platen")
    assert caps["max_width"] == 2550 and caps["resolutions"] == [300]
    assert "application/pdf" in caps["formats"]


def test_scan_settings_xml():
    xml = build_scan_settings(
        input_source="Platen", resolution=300, color_mode="Grayscale8", document_format="application/pdf", width=2550, height=3508
    )
    assert "<pwg:InputSource>Platen</pwg:InputSource>" in xml and "<scan:XResolution>300" in xml


@pytest.fixture
async def fake_escl():
    servers: list[TestServer] = []

    async def start(state="Idle"):
        calls = {"jobs": 0, "next": 0}

        async def status(_):
            return web.Response(text=STATUS.format(state=state), content_type="text/xml")

        async def caps(_):
            return web.Response(text=CAPS, content_type="text/xml")

        async def jobs(request):
            assert b"<pwg:InputSource>Platen" in await request.read()
            calls["jobs"] += 1
            return web.Response(status=201, headers={"Location": "/eSCL/ScanJobs/abc"})

        async def next_doc(_):
            calls["next"] += 1
            if calls["next"] == 1:
                return web.Response(status=503)  # still scanning
            return web.Response(body=b"%PDF-1.4 scanned", content_type="application/pdf")

        async def delete(_):
            return web.Response(status=200)

        app = web.Application()
        app.router.add_get("/eSCL/ScannerStatus", status)
        app.router.add_get("/eSCL/ScannerCapabilities", caps)
        app.router.add_post("/eSCL/ScanJobs", jobs)
        app.router.add_get("/eSCL/ScanJobs/abc/NextDocument", next_doc)
        app.router.add_delete("/eSCL/ScanJobs/abc", delete)
        server = TestServer(app)
        await server.start_server()
        servers.append(server)
        return server.port, calls

    yield start
    for s in servers:
        await s.close()


async def test_escl_scan(fake_escl, tmp_path):
    port, calls = await fake_escl()
    scanner = EsclScanner(make_settings(tmp_path, scan_mode="escl", printer_host="127.0.0.1", escl_port=port))
    result = await scanner.get_scan()
    assert calls["jobs"] == 1
    assert [f.mime_type for f in result.files] == ["application/pdf"]
    assert result.files[0].content == b"%PDF-1.4 scanned"


async def test_escl_busy(fake_escl, tmp_path):
    port, calls = await fake_escl(state="Processing")
    scanner = EsclScanner(make_settings(tmp_path, scan_mode="escl", printer_host="127.0.0.1", escl_port=port))
    with pytest.raises(PrinterBusyError):
        await scanner.get_scan()
    assert calls["jobs"] == 0
