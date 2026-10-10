from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BTN_PRINT = "🖨 Print"
BTN_SCAN = "📥 Receive Scan"  # email mode: the printer pushes the next scan
BTN_SCAN_NOW = "📷 Scan"  # eSCL mode: the bot scans the page on the glass
MENU_BUTTONS = {BTN_PRINT, BTN_SCAN, BTN_SCAN_NOW}

CB_SCAN_CANCEL = "scan:cancel"
CB_SCAN_MORE = "scan:more"
CB_SCAN_RESCAN = "scan:rescan"
CB_SCAN_DONE = "scan:done"

_scan_label = BTN_SCAN


def configure_menu(scan_button: str) -> None:
    """Called once at startup with the button matching SCAN_MODE."""
    global _scan_label
    _scan_label = scan_button


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_PRINT), KeyboardButton(text=_scan_label)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Send a PDF to print",
    )


def remove_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def scan_request_actions() -> InlineKeyboardMarkup:
    """Email mode: a pending request can only be cancelled."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖️ Cancel", callback_data=CB_SCAN_CANCEL)]]
    )


def scan_session_actions() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Scan more", callback_data=CB_SCAN_MORE),
                InlineKeyboardButton(text="📄 Make document", callback_data=CB_SCAN_DONE),
            ],
            [
                InlineKeyboardButton(text="🔄 Rescan last page", callback_data=CB_SCAN_RESCAN),
                InlineKeyboardButton(text="✖️ Cancel", callback_data=CB_SCAN_CANCEL),
            ],
        ]
    )
