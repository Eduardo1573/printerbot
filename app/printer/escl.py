"""eSCL (AirPrint scanning) pull scanner.

Used for network models WITHOUT Scan to E-mail (M130nw/M132nw/M132snw/M130fn/M132fn/
M132fp/M133fn/M134fn). HP does not document eSCL for this series in its user guide, so it
must be verified on the actual device with `python -m app.tools.probe` before use.
"""

from __future__ import annotations

import asyncio
import io
import logging
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import urljoin

import aiohttp

from app.config import Settings
from app.printer.base import (
    PrinterBusyError,
    PrinterError,
    PrinterProtocolError,
    PrinterTimeoutError,
    PrinterUnavailableError,
    PullScanSource,
    ScannedFile,
    ScanResult,
)

log = logging.getLogger(__name__)

NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}

SCAN_TIMEOUT = 180


def build_scan_settings(
    *,
    input_source: str,
    resolution: int,
    color_mode: str,
    document_format: str,
    width: int,
    height: int,
) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{NS["scan"]}" xmlns:pwg="{NS["pwg"]}">
  <pwg:Version>2.0</pwg:Version>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
      <pwg:Width>{int(width)}</pwg:Width>
      <pwg:Height>{int(height)}</pwg:Height>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{input_source}</pwg:InputSource>
  <scan:ColorMode>{color_mode}</scan:ColorMode>
  <scan:XResolution>{int(resolution)}</scan:XResolution>
  <scan:YResolution>{int(resolution)}</scan:YResolution>
  <pwg:DocumentFormat>{document_format}</pwg:DocumentFormat>
  <scan:DocumentFormatExt>{document_format}</scan:DocumentFormatExt>
</scan:ScanSettings>"""


def parse_capabilities(xml_text: str, input_source: str) -> dict:
    root = ET.fromstring(xml_text)
    section = "scan:Platen" if input_source == "Platen" else "scan:Adf"
    caps = root.find(section, NS)
    if caps is None:
        raise PrinterProtocolError(f"scanner has no {input_source} input")
    width = caps.findtext(".//scan:MaxWidth", default="2550", namespaces=NS)
    height = caps.findtext(".//scan:MaxHeight", default="3508", namespaces=NS)
    formats = [e.text for e in caps.iterfind(".//pwg:DocumentFormat", NS)] + [
        e.text for e in caps.iterfind(".//scan:DocumentFormatExt", NS)
    ]
    resolutions = sorted(
        {int(e.text) for e in caps.iterfind(".//scan:DiscreteResolution/scan:XResolution", NS) if e.text}
    )
    color_modes = [e.text for e in caps.iterfind(".//scan:ColorMode", NS)]
    return {
        "max_width": int(width),
        "max_height": int(height),
        "formats": [f for f in formats if f],
        "resolutions": resolutions,
        "color_modes": [c for c in color_modes if c],
    }


def parse_state(xml_text: str) -> tuple[str | None, str | None]:
    root = ET.fromstring(xml_text)
    return root.findtext("pwg:State", namespaces=NS), root.findtext("scan:AdfState", namespaces=NS)


class EsclScanner(PullScanSource):
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._base = settings.escl_base_url.rstrip("/") + "/"
        self._ssl: ssl.SSLContext | None = None
        if settings.escl_use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl = ctx
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def get_scan(self) -> ScanResult:
        if self._lock.locked():
            raise PrinterBusyError("scan in progress", user_message="Another scan is in progress. Try again shortly.")
        async with self._lock:
            try:
                return await asyncio.wait_for(self._scan(), timeout=SCAN_TIMEOUT)
            except TimeoutError as exc:
                raise PrinterTimeoutError("eSCL scan timed out", user_message="The scanner did not respond in time.") from exc
            except (aiohttp.ClientConnectionError, OSError) as exc:
                raise PrinterUnavailableError(f"{type(exc).__name__}: {exc}") from exc
            except ET.ParseError as exc:
                raise PrinterProtocolError(f"invalid eSCL XML: {exc}") from exc
            except aiohttp.ClientError as exc:
                raise PrinterProtocolError(f"{type(exc).__name__}: {exc}") from exc

    async def _scan(self) -> ScanResult:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=self._s.printer_connect_timeout, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status_xml = await self._get_text(session, "ScannerStatus")
            state, adf_state = parse_state(status_xml)
            if state and state != "Idle":
                raise PrinterBusyError(f"scanner state {state}", user_message="The scanner is busy. Try again shortly.")
            if self._s.escl_input_source == "Feeder" and adf_state and adf_state != "ScannerAdfLoaded":
                raise PrinterError(
                    f"adf state {adf_state}", user_message="No paper in the document feeder. Load the pages and try again."
                )

            caps = parse_capabilities(await self._get_text(session, "ScannerCapabilities"), self._s.escl_input_source)
            body = build_scan_settings(
                input_source=self._s.escl_input_source,
                resolution=self._s.escl_resolution,
                color_mode=self._s.escl_color_mode,
                document_format=self._s.escl_document_format,
                width=caps["max_width"],
                height=caps["max_height"],
            )
            async with session.post(
                urljoin(self._base, "ScanJobs"), data=body, headers={"Content-Type": "text/xml"}, ssl=self._ssl
            ) as r:
                if r.status == 503:
                    raise PrinterBusyError("scanner returned 503")
                if r.status != 201 or "Location" not in r.headers:
                    raise PrinterProtocolError(f"ScanJobs returned HTTP {r.status}")
                job_url = urljoin(self._base, r.headers["Location"])

            files = await self._collect_documents(session, job_url)
        if not files:
            raise PrinterProtocolError("scanner returned no documents")
        return ScanResult(files=files, source="escl")

    async def _collect_documents(self, session: aiohttp.ClientSession, job_url: str) -> list[ScannedFile]:
        files: list[ScannedFile] = []
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        next_url = job_url.rstrip("/") + "/NextDocument"
        started = time.monotonic()
        total = 0
        while True:
            async with session.get(next_url, ssl=self._ssl) as r:
                if r.status == 404:
                    break  # no more documents in this job
                if r.status == 503:
                    if time.monotonic() - started > SCAN_TIMEOUT:
                        raise PrinterTimeoutError("scanner never produced a document")
                    await asyncio.sleep(1)
                    continue
                if r.status != 200:
                    raise PrinterProtocolError(f"NextDocument returned HTTP {r.status}")
                buf = io.BytesIO()
                async for chunk in r.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > self._s.max_scan_size_bytes:
                        raise PrinterError("scan too large", user_message="The scan is larger than the allowed size.")
                    buf.write(chunk)
                mime = (r.content_type or self._s.escl_document_format).split(";")[0]
            ext = "pdf" if mime == "application/pdf" else "jpg"
            files.append(ScannedFile(buf.getvalue(), mime, f"scan_{stamp}_{len(files) + 1}.{ext}"))
            if self._s.escl_input_source == "Platen":
                # A flatbed job yields exactly one document; release the job explicitly.
                try:
                    async with session.delete(job_url, ssl=self._ssl):
                        pass
                except aiohttp.ClientError:
                    pass
                break
        return files

    async def _get_text(self, session: aiohttp.ClientSession, path: str) -> str:
        async with session.get(urljoin(self._base, path), ssl=self._ssl) as r:
            if r.status != 200:
                raise PrinterProtocolError(f"GET {path} returned HTTP {r.status}")
            return await r.text()
