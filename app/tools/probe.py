"""Verify what the real printer supports, over the actual network path (VPN tunnel).

    docker compose run --rm bot python -m app.tools.probe
    docker compose run --rm bot python -m app.tools.probe --print /app/tests/fixtures/test.pdf
    docker compose run --rm bot python -m app.tools.probe --scan /tmp

Run this once after setting up the tunnel and before relying on the bot.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import aiohttp

from app.config import ScanMode, load_settings
from app.printer.base import PrinterError
from app.printer.escl import EsclScanner, parse_capabilities, parse_state
from app.printer.hp import PRINTER_ATTRIBUTES, HPIppPrinter
from app.printer.ipp import IppClient, Tag
from app.printer.models import capabilities


def line(label: str, value: object) -> None:
    print(f"  {label:<38} {value}")


async def probe_ipp(settings) -> bool:
    print(f"\n[IPP] {settings.printer_uri}")
    client = IppClient(
        settings.printer_http_url,
        settings.printer_uri,
        connect_timeout=settings.printer_connect_timeout,
        verify_tls=settings.printer_verify_tls,
    )
    try:
        resp = await client.get_printer_attributes(PRINTER_ATTRIBUTES)
    except Exception as exc:  # noqa: BLE001 - diagnostics tool
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        print("  -> Check the WireGuard tunnel (`wg show`), PRINTER_HOST and that IPP is enabled on the printer.")
        return False
    attrs = resp.group(Tag.PRINTER)
    for name in PRINTER_ATTRIBUTES:
        line(name, attrs.get(name, "-"))
    formats = attrs.get("document-format-supported", [])
    ok = "application/pdf" in formats or "image/pwg-raster" in formats
    print(f"  => printing {'SUPPORTED' if ok else 'NOT SUPPORTED (no PDF / PWG raster)'}")
    return ok


async def probe_escl(settings) -> bool:
    print(f"\n[eSCL] {settings.escl_base_url}")
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            base = settings.escl_base_url.rstrip("/")
            async with session.get(f"{base}/ScannerCapabilities", ssl=False) as r:
                if r.status != 200:
                    print(f"  ScannerCapabilities -> HTTP {r.status}")
                    return False
                caps_xml = await r.text()
            async with session.get(f"{base}/ScannerStatus", ssl=False) as r:
                status_xml = await r.text() if r.status == 200 else ""
    except Exception as exc:  # noqa: BLE001
        print(f"  FAILED: {type(exc).__name__}: {exc}")
        return False
    for source in ("Platen", "Feeder"):
        try:
            caps = parse_capabilities(caps_xml, source)
            line(source, caps)
        except Exception as exc:  # noqa: BLE001
            line(source, f"not available ({exc})")
    if status_xml:
        line("state / adf", parse_state(status_xml))
    print("  => eSCL endpoint responds (run --scan to confirm a full scan works)")
    return True


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", dest="print_file", type=Path, help="print this PDF (exactly 1 copy)")
    parser.add_argument("--scan", dest="scan_dir", type=Path, help="perform an eSCL scan and save it here")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    settings = load_settings()
    caps = capabilities(settings.printer_model)
    print(f"Configured model: {settings.printer_model} -> {caps or 'unknown model (not in table)'}")
    print(f"Scan mode: {settings.scan_mode.value}")

    ipp_ok = await probe_ipp(settings)
    escl_ok = await probe_escl(settings)

    if args.print_file:
        print(f"\n[TEST PRINT] {args.print_file}")
        try:
            result = await HPIppPrinter(settings).print_pdf(args.print_file, job_name="probe-test")
            print(f"  printed: job {result.printer_job_id}, copies={result.copies}, warning={result.warning}")
        except PrinterError as exc:
            print(f"  FAILED: {exc.user_message} ({exc})")
            ipp_ok = False

    if args.scan_dir:
        print("\n[TEST SCAN] place a document on the glass...")
        try:
            result = await EsclScanner(settings).get_scan()
            args.scan_dir.mkdir(parents=True, exist_ok=True)
            for f in result.files:
                (args.scan_dir / f.filename).write_bytes(f.content)
                print(f"  saved {args.scan_dir / f.filename} ({len(f.content)} bytes, {f.mime_type})")
        except PrinterError as exc:
            print(f"  FAILED: {exc.user_message} ({exc})")
            escl_ok = False

    print("\nSummary")
    line("printing (IPP)", "OK" if ipp_ok else "FAILED")
    if settings.scan_mode is ScanMode.EMAIL:
        line("scanning (Scan to E-mail)", "configured on printer; test with 📥 Receive Scan")
    else:
        line("scanning (eSCL)", "OK" if escl_ok else "FAILED")
    return 0 if ipp_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
