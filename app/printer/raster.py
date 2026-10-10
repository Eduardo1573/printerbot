"""PDF -> printer raster conversion with Ghostscript (Apple URF or PWG Raster).

The M129–M134 series does not interpret PDF/PostScript/PCL6, so the VPS rasterises PDFs
before sending them over IPP — exactly what a driverless (AirPrint / Mopria) client does.

Verified on a real M130fn (firmware 20170710, tests/fixtures/):
* it advertises `image/urf` and `application/PCLm` — NOT `image/pwg-raster`, despite the
  datasheet;
* Ghostscript's generic PCLm output makes this firmware reboot, so PCLm is never used;
* 8-bit grayscale URF at 600 dpi (urf-supported: W8, RS600) prints correctly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.printer.base import DocumentRejectedError, PrinterError, PrinterTimeoutError

log = logging.getLogger(__name__)

# PWG raster type keyword -> (cupsColorSpace, cupsBitsPerColor) for Ghostscript's pwgraster device.
PWG_TYPES: dict[str, tuple[int, int]] = {
    "sgray_8": (18, 8),  # CUPS_CSPACE_SW
    "black_1": (3, 1),  # CUPS_CSPACE_K
    "srgb_8": (19, 8),  # CUPS_CSPACE_SRGB
}
PWG_TYPE_PREFERENCE = ("sgray_8", "black_1", "srgb_8")

# config value -> (Ghostscript paper name, PWG media keyword)
PAPER_SIZES = {"a4": ("a4", "iso_a4_210x297mm"), "letter": ("letter", "na_letter_8.5x11in")}


@dataclass(frozen=True, slots=True)
class RasterSettings:
    resolution: int = 600
    pwg_type: str = "sgray_8"
    paper_size: str = "a4"


def choose_raster_settings(
    resolutions: list[tuple[int, int, int]] | None,
    types: list[str] | None,
    paper_size: str,
) -> RasterSettings:
    """Pick the best settings the printer advertises (pwg-raster-document-*-supported)."""
    resolution = 600
    if resolutions:
        dpis = sorted({x for x, y, _units in resolutions if x == y})
        if dpis:
            fitting = [d for d in dpis if d <= 600]
            resolution = fitting[-1] if fitting else dpis[0]
    pwg_type = "sgray_8"
    if types:
        for candidate in PWG_TYPE_PREFERENCE:
            if candidate in types:
                pwg_type = candidate
                break
        else:
            raise PrinterError(
                f"No supported PWG raster type in {types}",
                user_message="The printer does not support a compatible raster format.",
            )
    return RasterSettings(resolution=resolution, pwg_type=pwg_type, paper_size=paper_size)


def ghostscript_args(src: Path, dst: Path, settings: RasterSettings) -> list[str]:
    color_space, bits = PWG_TYPES[settings.pwg_type]
    return [
        "gs",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-dQUIET",
        "-dNOINTERACTIVE",
        "-sDEVICE=pwgraster",
        f"-r{settings.resolution}",
        f"-dcupsColorSpace={color_space}",
        f"-dcupsBitsPerColor={bits}",
        f"-sPAPERSIZE={PAPER_SIZES[settings.paper_size][0]}",
        f"-scupsPageSizeName={PAPER_SIZES[settings.paper_size][1]}",
        "-dFIXEDMEDIA",
        "-dPDFFitPage",
        f"-sOutputFile={dst}",
        "-f",
        str(src),
    ]


# URF colour keyword (from urf-supported) -> (cupsColorSpace, cupsBitsPerColor)
URF_COLORS: dict[str, tuple[int, int]] = {"W8": (18, 8), "SRGB24": (19, 8)}


@dataclass(frozen=True, slots=True)
class UrfSettings:
    resolution: int = 600
    color: str = "W8"
    paper_size: str = "a4"


def choose_urf_settings(urf_supported: list[str] | None, paper_size: str) -> UrfSettings:
    """Pick settings from urf-supported keywords, e.g. ['V1.4','W8','RS600','PQ3-4-5']."""
    keywords = [k for k in (urf_supported or []) if isinstance(k, str)]
    dpis: list[int] = []
    for k in keywords:
        if k.startswith("RS"):
            dpis += [int(x) for x in k[2:].split("-") if x.isdigit()]
    fitting = sorted(d for d in dpis if d <= 600)
    resolution = fitting[-1] if fitting else (min(dpis) if dpis else 600)
    if "W8" in keywords or not keywords:
        color = "W8"
    elif "SRGB24" in keywords:
        color = "SRGB24"
    else:
        raise PrinterError(
            f"no supported URF colour mode in {keywords}",
            user_message="The printer does not support a compatible raster format.",
        )
    return UrfSettings(resolution=resolution, color=color, paper_size=paper_size)


def urf_args(src: Path, dst: Path, settings: UrfSettings) -> list[str]:
    color_space, bits = URF_COLORS[settings.color]
    return [
        "gs",
        "-dSAFER",
        "-dBATCH",
        "-dNOPAUSE",
        "-dQUIET",
        "-dNOINTERACTIVE",
        "-sDEVICE=urf",
        f"-r{settings.resolution}",
        f"-dcupsColorSpace={color_space}",
        f"-dcupsBitsPerColor={bits}",
        f"-sPAPERSIZE={PAPER_SIZES[settings.paper_size][0]}",
        "-dFIXEDMEDIA",
        "-dPDFFitPage",
        f"-sOutputFile={dst}",
        "-f",
        str(src),
    ]


def set_urf_page_count(path: Path, pages: int) -> None:
    """Ghostscript writes 0 in the UNIRAST page-count field; Apple clients write the real count."""
    with path.open("r+b") as fh:
        if fh.read(8) != b"UNIRAST\0":
            raise DocumentRejectedError("not a URF file")
        fh.write(pages.to_bytes(4, "big"))


async def _run_ghostscript(args: list[str], dst: Path, timeout: float) -> None:
    if shutil.which("gs") is None:
        raise PrinterError("ghostscript not installed", user_message="Server misconfiguration: Ghostscript missing.")
    # Arguments are passed as a list (no shell) — file names never reach a shell.
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        # Ghostscript spills large pages to temp files; keep them in the job's private
        # workspace (disk-backed, deleted afterwards) instead of the small /tmp tmpfs.
        env={**os.environ, "TMPDIR": str(dst.parent)},
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise PrinterTimeoutError("ghostscript timed out", user_message="Converting the PDF took too long.")
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        log.warning(
            "pdf_conversion_failed",
            extra={"fields": {"returncode": proc.returncode, "stderr": stderr[-500:].decode("utf-8", "replace")}},
        )
        raise DocumentRejectedError(
            "ghostscript failed", user_message="The PDF could not be rendered for printing (damaged or unsupported)."
        )


async def pdf_to_pwg_raster(src: Path, dst: Path, settings: RasterSettings, timeout: float) -> None:
    await _run_ghostscript(ghostscript_args(src, dst, settings), dst, timeout)


async def pdf_to_urf(src: Path, dst: Path, settings: UrfSettings, pages: int, timeout: float) -> None:
    await _run_ghostscript(urf_args(src, dst, settings), dst, timeout)
    set_urf_page_count(dst, pages)
