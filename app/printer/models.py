"""Capabilities of the HP LaserJet Pro MFP M129–M132 / LaserJet Ultra MFP M133–M134 series.

Source: HP user guide c05208327 ("LaserJet Pro MFP M129-M132, LaserJet Ultra MFP M133-M134"),
section "Printer views" and chapter "Scan". Only what the guide states is encoded here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model: str
    networked: bool
    # "Scan to E-mail" / "Scan to Network Folder": touchscreen models only.
    scan_to_email: bool
    # Automatic document feeder: "f" models.
    adf: bool


# control panel type -> models (from the user guide)
_LED_USB_ONLY = {"M130a", "M132a", "M133a", "M134a"}
_LCD_NW = {"M130nw", "M132nw", "M132snw"}
_LCD_FN = {"M130fn", "M132fn", "M132fp", "M133fn", "M134fn"}
_TOUCH = {"M130fw", "M132fw"}

KNOWN_MODELS = _LED_USB_ONLY | _LCD_NW | _LCD_FN | _TOUCH


def normalize_model(raw: str) -> str:
    """'HP LaserJet Pro MFP M130FW' -> 'M130fw'."""
    match = re.search(r"M\s*(1[23]\d)\s*([a-zA-Z]*)", raw)
    if not match:
        return raw.strip()
    return f"M{match.group(1)}{match.group(2).lower()}"


def capabilities(raw_model: str) -> ModelCapabilities | None:
    model = normalize_model(raw_model)
    if model not in KNOWN_MODELS:
        return None
    return ModelCapabilities(
        model=model,
        networked=model not in _LED_USB_ONLY,
        scan_to_email=model in _TOUCH,
        adf=model in _LCD_FN or model in _TOUCH,
    )
