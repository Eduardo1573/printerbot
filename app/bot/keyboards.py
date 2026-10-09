from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BTN_PRINT = "🖨 Print"
BTN_SCAN = "📥 Receive Scan"
MENU_BUTTONS = {BTN_PRINT, BTN_SCAN}

CB_SCAN_CANCEL = "scan:cancel"
CB_SCAN_START = "scan:start"


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_PRINT), KeyboardButton(text=BTN_SCAN)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Send a PDF to print",
    )


def remove_menu() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def scan_request_actions(pull_mode: bool) -> InlineKeyboardMarkup:
    rows = []
    if pull_mode:
        rows.append([InlineKeyboardButton(text="▶️ Start scan", callback_data=CB_SCAN_START)])
    rows.append([InlineKeyboardButton(text="✖️ Cancel", callback_data=CB_SCAN_CANCEL)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
