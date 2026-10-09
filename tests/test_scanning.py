from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.bot import texts
from app.bot.handlers import scanning
from app.database.models import ScanRequest, ScanStatus, utcnow
from app.printer.base import PrinterBusyError, PrinterUnavailableError, ScannedFile, ScanResult
from app.services.scan_service import ScanRequestError, ScanService
from tests.conftest import FakeNotifier, FakePullSource, answered_texts, authenticated, make_message, make_settings


def scan(name: str = "scan.pdf") -> ScanResult:
    return ScanResult(files=[ScannedFile(b"%PDF-1.4 " + name.encode(), "application/pdf", name)], source="email")


async def statuses(session_factory) -> dict[int, str]:
    async with session_factory() as session:
        rows = (await session.scalars(select(ScanRequest).order_by(ScanRequest.id))).all()
    return {r.telegram_user_id: r.status for r in rows}


async def wait_delivered(service: ScanService) -> None:
    await service.drain(timeout=5)


@pytest.fixture
async def users(auth_service):
    for uid in (1, 2, 3):
        await authenticated(auth_service, uid)


# ------------------------------------------------------------------ creation


async def test_create_scan_request(scan_service, users, session_factory, settings):
    result = await scan_service.create_request(1)
    assert result.created and result.position == 1
    req = result.request
    assert req.telegram_user_id == 1
    assert req.status == ScanStatus.PENDING
    assert req.created_at is not None
    lifetime = req.expires_at - req.created_at
    assert lifetime == timedelta(seconds=settings.scan_request_timeout)


async def test_second_press_returns_existing_request(scan_service, users):
    first = await scan_service.create_request(1)
    second = await scan_service.create_request(1)
    assert second.created is False
    assert second.request.id == first.request.id


async def test_concurrent_presses_create_single_request(scan_service, users, session_factory):
    results = await asyncio.gather(*(scan_service.create_request(1) for _ in range(5)), return_exceptions=True)
    assert not [r for r in results if isinstance(r, Exception)]
    async with session_factory() as session:
        rows = (await session.scalars(select(ScanRequest))).all()
    assert len(rows) == 1


async def test_queue_positions(scan_service, users):
    assert (await scan_service.create_request(1)).position == 1
    assert (await scan_service.create_request(2)).position == 2
    assert (await scan_service.create_request(3)).position == 3


# ------------------------------------------------------------------ ownership


async def test_scan_goes_to_the_requesting_user(scan_service, users, notifier, session_factory):
    await scan_service.create_request(2)
    assert await scan_service.handle_incoming_scan(scan("a.pdf")) is True
    await wait_delivered(scan_service)
    assert [uid for uid, _ in notifier.scans] == [2]
    assert (await statuses(session_factory))[2] == ScanStatus.COMPLETED


async def test_multiple_waiting_users_are_served_fifo(scan_service, users, notifier, session_factory):
    for uid in (1, 2, 3):
        await scan_service.create_request(uid)
    for name in ("first", "second", "third"):
        assert await scan_service.handle_incoming_scan(scan(name)) is True
    await wait_delivered(scan_service)
    delivered = {uid: result.files[0].filename for uid, result in notifier.scans}
    assert delivered == {1: "first", 2: "second", 3: "third"}
    assert set((await statuses(session_factory)).values()) == {ScanStatus.COMPLETED}


async def test_simultaneous_scans_never_share_a_recipient(scan_service, users, notifier):
    for uid in (1, 2, 3):
        await scan_service.create_request(uid)
    results = await asyncio.gather(*(scan_service.handle_incoming_scan(scan(f"s{i}")) for i in range(5)))
    await wait_delivered(scan_service)
    assert sorted(results) == [False, False, True, True, True]
    recipients = [uid for uid, _ in notifier.scans]
    assert sorted(recipients) == [1, 2, 3]  # each exactly once


async def test_no_pending_recipient_scan_is_refused(scan_service, users, notifier):
    assert await scan_service.handle_incoming_scan(scan()) is False
    await wait_delivered(scan_service)
    assert notifier.scans == []


async def test_completed_request_does_not_receive_second_scan(scan_service, users, notifier):
    await scan_service.create_request(1)
    assert await scan_service.handle_incoming_scan(scan("one"))
    await wait_delivered(scan_service)
    assert await scan_service.handle_incoming_scan(scan("two")) is False
    assert len(notifier.scans) == 1


async def test_cancelled_request_receives_nothing(scan_service, users, notifier):
    await scan_service.create_request(1)
    assert await scan_service.cancel_request(1) is True
    assert await scan_service.handle_incoming_scan(scan()) is False
    assert notifier.scans == []


async def test_logout_cancels_pending_request(scan_service, users, auth_service, notifier, session_factory):
    await scan_service.create_request(1)
    await auth_service.logout(1)
    assert await scan_service.handle_incoming_scan(scan()) is False
    assert (await statuses(session_factory))[1] == ScanStatus.CANCELLED


async def test_delivery_failure_marks_request_failed(session_factory, settings, users):
    service = ScanService(session_factory, settings, FakeNotifier(fail=True))
    await service.create_request(1)
    assert await service.handle_incoming_scan(scan()) is True
    await wait_delivered(service)
    assert (await statuses(session_factory))[1] == ScanStatus.FAILED


# ------------------------------------------------------------------ expiration


async def expire_now(session_factory, telegram_id: int) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(
            update(ScanRequest)
            .where(ScanRequest.telegram_user_id == telegram_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )


async def test_expired_request_never_receives_scan(scan_service, users, notifier, session_factory):
    await scan_service.create_request(1)
    await expire_now(session_factory, 1)
    # Even before the expiry sweeper runs, an overdue request is not eligible.
    assert await scan_service.handle_incoming_scan(scan()) is False
    assert notifier.scans == []


async def test_expired_request_is_skipped_for_next_user(scan_service, users, notifier, session_factory):
    await scan_service.create_request(1)
    await scan_service.create_request(2)
    await expire_now(session_factory, 1)
    assert await scan_service.handle_incoming_scan(scan()) is True
    await wait_delivered(scan_service)
    assert [uid for uid, _ in notifier.scans] == [2]


async def test_expiry_sweeper_marks_and_notifies(scan_service, users, notifier, session_factory):
    await scan_service.create_request(1)
    await expire_now(session_factory, 1)
    assert await scan_service.expire_stale() == [1]
    assert (await statuses(session_factory))[1] == ScanStatus.EXPIRED
    assert notifier.messages and notifier.messages[0][0] == 1
    # The user can immediately create a new request afterwards.
    assert (await scan_service.create_request(1)).created


async def test_restart_recovery_fails_processing_requests(scan_service, users, session_factory):
    await scan_service.create_request(1)
    assert await scan_service.claim_next() is not None
    await scan_service.recover_after_restart()
    assert (await statuses(session_factory))[1] == ScanStatus.FAILED


# ------------------------------------------------------------------ pull mode (eSCL)


async def test_pull_scan_delivers_to_requester(session_factory, tmp_path, users, notifier):
    settings = make_settings(tmp_path, scan_mode="escl")
    service = ScanService(session_factory, settings, notifier, pull_source=FakePullSource(result=scan("pulled")))
    await service.create_request(3)
    await service.run_pull_scan(3)
    assert [(uid, r.files[0].filename) for uid, r in notifier.scans] == [(3, "pulled")]
    assert (await statuses(session_factory))[3] == ScanStatus.COMPLETED


async def test_pull_scan_requires_pending_request(session_factory, tmp_path, users, notifier):
    settings = make_settings(tmp_path, scan_mode="escl")
    service = ScanService(session_factory, settings, notifier, pull_source=FakePullSource(result=scan()))
    with pytest.raises(ScanRequestError):
        await service.run_pull_scan(1)


async def test_pull_scan_busy_keeps_request_pending(session_factory, tmp_path, users, notifier):
    settings = make_settings(tmp_path, scan_mode="escl")
    service = ScanService(session_factory, settings, notifier, pull_source=FakePullSource(error=PrinterBusyError()))
    await service.create_request(1)
    with pytest.raises(PrinterBusyError):
        await service.run_pull_scan(1)
    assert (await statuses(session_factory))[1] == ScanStatus.PENDING


async def test_pull_scan_printer_offline_fails_request(session_factory, tmp_path, users, notifier):
    settings = make_settings(tmp_path, scan_mode="escl")
    source = FakePullSource(error=PrinterUnavailableError("no route"))
    service = ScanService(session_factory, settings, notifier, pull_source=source)
    await service.create_request(1)
    with pytest.raises(PrinterUnavailableError):
        await service.run_pull_scan(1)
    assert (await statuses(session_factory))[1] == ScanStatus.FAILED
    assert notifier.scans == []


# ------------------------------------------------------------------ handler


async def test_receive_scan_button(scan_service, auth_service, settings):
    user = await authenticated(auth_service, 1)
    message = make_message("📥 Receive Scan", user_id=1)
    await scanning.receive_scan(message, user, scan_service, settings)
    assert answered_texts(message)[0].startswith("📥 Ready to receive a scan.")


async def test_receive_scan_button_reports_queue(scan_service, auth_service, settings, users):
    await scan_service.create_request(2)
    user = await authenticated(auth_service, 1)
    message = make_message("📥 Receive Scan", user_id=1)
    await scanning.receive_scan(message, user, scan_service, settings)
    assert "1 request(s) ahead" in answered_texts(message)[0]


async def test_receive_scan_when_disabled(session_factory, tmp_path, auth_service, notifier):
    settings = make_settings(tmp_path, scan_mode="disabled")
    service = ScanService(session_factory, settings, notifier)
    user = await authenticated(auth_service, 1)
    message = make_message("📥 Receive Scan", user_id=1)
    await scanning.receive_scan(message, user, service, settings)
    assert answered_texts(message) == [texts.SCAN_DISABLED]
    assert await service.get_active_request(1) is None
