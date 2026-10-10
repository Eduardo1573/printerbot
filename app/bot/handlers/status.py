from __future__ import annotations

import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import main_menu
from app.bot.middleware import AuthRequiredMiddleware
from app.printer.base import PrinterError, PrinterStatus, Supply, describe_reasons
from app.services.print_service import PrintService

router = Router(name="status")
router.message.middleware(AuthRequiredMiddleware())

STATE_TEXT = {"idle": "✅ Ready", "processing": "🖨 Printing", "stopped": "⛔ Stopped"}
KIND_LABEL = {"toner": "Toner", "opc": "Imaging drum", "ink": "Ink", "waste-toner": "Waste toner"}
KIND_ICON = {"toner": "⚫", "opc": "🥁", "ink": "💧"}


def level_bar(level: int) -> str:
    filled = max(0, min(10, round(level / 10)))
    return "▰" * filled + "▱" * (10 - filled)


def format_supply(supply: Supply) -> str:
    label = KIND_LABEL.get(supply.kind, supply.kind.replace("-", " ").capitalize() or "Supply")
    icon = KIND_ICON.get(supply.kind, "▫️")
    name = html.escape(supply.name)
    if supply.level is None:
        return f"{icon} <b>{label}</b>: level unknown\n<i>{name}</i>"
    warning = " ⚠️ low" if supply.is_low else ""
    return f"{icon} <b>{label}</b>: {level_bar(supply.level)} {supply.level}%{warning}\n<i>{name}</i>"


def format_status(status: PrinterStatus) -> str:
    lines = [f"🖨 <b>Printer:</b> {STATE_TEXT.get(status.state or '', html.escape(status.state or 'unknown'))}"]
    problems = describe_reasons(status.reasons)
    if problems:
        lines.append(f"⚠️ {html.escape(problems.capitalize())}")
    if status.supplies:
        lines.append("")
        lines += [format_supply(s) for s in status.supplies]
    else:
        lines.append("\nThe printer does not report supply levels.")
    if any(s.kind == "toner" and (s.is_low or s.level == 0) for s in status.supplies):
        lines.append(
            "\nℹ️ HP shows low or 0% when the cartridge is nearly empty or is not an original HP "
            "cartridge. It usually keeps printing for a while — replace it when prints get faint."
        )
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, print_service: PrintService) -> None:
    try:
        status = await print_service.printer_status()
    except PrinterError as exc:
        await message.answer(f"❌ Can't get the printer status.\n\nReason: {exc.user_message}", reply_markup=main_menu())
        return
    await message.answer(format_status(status), reply_markup=main_menu(), parse_mode="HTML")
