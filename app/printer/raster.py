"""PDF -> PWG Raster conversion with Ghostscript.

The M129–M134 series does not interpret PDF/PostScript/PCL6. Its print languages are
PCLmS, URF and PWG Raster (HP datasheet), so the VPS rasterises PDFs before sending them
over IPP — exactly what a driverless (IPP Everywhere / AirPrint) client does.
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


async def pdf_to_pwg_raster(src: Path, dst: Path, settings: RasterSettings, timeout: float) -> None:
    if shutil.which("gs") is None:
        raise PrinterError("ghostscript not installed", user_message="Server misconfiguration: Ghostscript missing.")
    # Arguments are passed as a list (no shell) — file names never reach a shell.
    proc = await asyncio.create_subprocess_exec(
        *ghostscript_args(src, dst, settings),
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
