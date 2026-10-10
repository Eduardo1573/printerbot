from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import BotCommand, ErrorEvent
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.bot import texts
from app.bot.handlers import auth, common, printing, scanning
from app.bot.keyboards import BTN_SCAN, BTN_SCAN_NOW, configure_menu
from app.bot.middleware import UserContextMiddleware
from app.bot.notifier import TelegramNotifier
from app.config import ScanMode, Settings, load_settings
from app.database.database import create_engine, create_session_factory, ping
from app.logging_setup import event, setup_logging
from app.printer.base import PrinterError, PushScanSource
from app.printer.escl import EsclScanner
from app.printer.hp import HPIppPrinter
from app.printer.models import capabilities
from app.printer.smtp_receiver import SmtpScanReceiver
from app.services.auth_service import AuthService
from app.services.print_service import PrintService
from app.services.scan_service import ScanService
from app.services.scan_session import ScanSessionService

log = logging.getLogger("app")

HEARTBEAT_INTERVAL = 30


def build_dispatcher(
    settings: Settings,
    auth_service: AuthService,
    print_service: PrintService,
    scan_service: ScanService,
    scan_sessions: ScanSessionService | None = None,
) -> Dispatcher:
    dp = Dispatcher()
    dp["settings"] = settings
    dp["auth_service"] = auth_service
    dp["print_service"] = print_service
    dp["scan_service"] = scan_service
    dp["scan_sessions"] = scan_sessions
    configure_menu(BTN_SCAN_NOW if settings.scan_mode is ScanMode.ESCL else BTN_SCAN)

    user_ctx = UserContextMiddleware(auth_service)
    dp.message.outer_middleware(user_ctx)
    dp.callback_query.outer_middleware(user_ctx)

    # Order matters: commands, then access-code entry, then protected features, then fallback.
    dp.include_routers(common.router, auth.router, printing.router, scanning.router, common.fallback_router)

    @dp.errors()
    async def on_error(error: ErrorEvent) -> bool:
        exc = error.exception
        log.error("handler_error", exc_info=(type(exc), exc, exc.__traceback__))
        text = texts.SERVICE_UNAVAILABLE if isinstance(exc, SQLAlchemyError | OSError) else texts.INTERNAL_ERROR
        update = error.update
        with contextlib.suppress(Exception):
            if update.message:
                await update.message.answer(text)
            elif update.callback_query:
                await update.callback_query.answer(text[:200], show_alert=True)
        return True

    return dp


def check_model(settings: Settings) -> None:
    caps = capabilities(settings.printer_model)
    if caps is None:
        log.warning("unknown_printer_model", extra={"fields": {"model": settings.printer_model}})
        return
    if not caps.networked:
        raise SystemExit(f"{caps.model} is USB-only and cannot be used without a computer at home.")
    if settings.scan_mode is ScanMode.EMAIL and not caps.scan_to_email:
        raise SystemExit(
            f"{caps.model} has no Scan to E-mail (touchscreen M130fw/M132fw only). "
            "Use SCAN_MODE=escl (verify with `python -m app.tools.probe`) or SCAN_MODE=disabled."
        )
    if settings.escl_input_source == "Feeder" and not caps.adf and settings.scan_mode is ScanMode.ESCL:
        raise SystemExit(f"{caps.model} has no document feeder; set ESCL_INPUT_SOURCE=Platen.")


async def wait_for_database(engine: AsyncEngine, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            await ping(engine)
            return
        except (SQLAlchemyError, OSError) as exc:
            if time.monotonic() > deadline:
                raise
            log.warning("database_unavailable_retrying", extra={"fields": {"error": type(exc).__name__}})
            await asyncio.sleep(3)


async def heartbeat(engine: AsyncEngine, settings: Settings) -> None:
    """Touches the health file while the database is reachable (used by the Docker healthcheck)."""
    while True:
        try:
            await ping(engine)
            settings.health_file.write_text(str(int(time.time())))
        except Exception as exc:
            log.warning("heartbeat_failed", extra={"fields": {"error": type(exc).__name__}})
        await asyncio.sleep(HEARTBEAT_INTERVAL)


async def check_printer(printer: HPIppPrinter) -> None:
    try:
        status = await printer.get_status()
        event(log, "printer_reachable", state=status.state, reasons=status.reasons, formats=status.document_formats)
    except PrinterError as exc:
        event(log, "printer_connection_failed", level=logging.WARNING, error=str(exc))


async def run() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_format, settings.secrets())
    check_model(settings)
    event(log, "starting", model=settings.printer_model, scan_mode=settings.scan_mode.value)

    engine = create_engine(settings.database_url)
    await wait_for_database(engine)
    session_factory = create_session_factory(engine)

    bot = Bot(settings.telegram_bot_token.get_secret_value())
    notifier = TelegramNotifier(bot)
    printer = HPIppPrinter(settings)

    push_source: PushScanSource | None = SmtpScanReceiver(settings) if settings.scan_mode is ScanMode.EMAIL else None

    auth_service = AuthService(session_factory, settings)
    print_service = PrintService(session_factory, printer, settings)
    scan_service = ScanService(session_factory, settings, notifier)
    scan_sessions = (
        ScanSessionService(session_factory, settings, EsclScanner(settings), notifier.notify)
        if settings.scan_mode is ScanMode.ESCL
        else None
    )

    print_service.cleanup_stale_workspaces()
    if scan_sessions is not None:
        scan_sessions.cleanup_stale_workdirs()
    await scan_service.recover_after_restart()
    if push_source is not None:
        await push_source.start(scan_service.handle_incoming_scan)

    tasks = [
        asyncio.create_task(scan_service.run_expiry_loop(), name="scan-expiry"),
        asyncio.create_task(heartbeat(engine, settings), name="heartbeat"),
        asyncio.create_task(check_printer(printer), name="printer-check"),
    ]
    if scan_sessions is not None:
        tasks.append(asyncio.create_task(scan_sessions.run_expiry_loop(), name="scan-session-expiry"))
    dp = build_dispatcher(settings, auth_service, print_service, scan_service, scan_sessions)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Show the menu"),
                BotCommand(command="scan", description="Scan the page on the glass"),
                BotCommand(command="help", description="How to print and scan"),
                BotCommand(command="logout", description="Remove your access"),
            ]
        )
        await bot.delete_webhook(drop_pending_updates=False)
        event(log, "bot_started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if push_source is not None:
            await push_source.stop()
        await scan_service.drain()
        await bot.session.close()
        await engine.dispose()
        event(log, "stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except TelegramUnauthorizedError:
        log.critical("telegram_token_invalid: check TELEGRAM_BOT_TOKEN")
        sys.exit(1)
    except Exception:
        # Logged through the redacting handler instead of a raw stderr traceback.
        log.critical("fatal_error", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
