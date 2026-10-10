"""Scan request queue and scan ownership.

Rules:
* Each authenticated user may have at most one active (pending/processing) request.
* Push mode (Scan to E-mail): an incoming scan goes to the OLDEST non-expired pending
  request (FIFO). If there is none, the scan is refused and never delivered to anyone.
* Pull mode (eSCL) uses multi-page sessions instead: see app/services/scan_session.py.
* Expired/cancelled requests can never receive a scan.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ScanRequest, ScanStatus, utcnow
from app.logging_setup import event
from app.printer.base import ScanResult

log = logging.getLogger(__name__)


class ScanNotifier(Protocol):
    async def send_scan(self, telegram_id: int, result: ScanResult) -> None: ...

    async def notify(self, telegram_id: int, text: str) -> None: ...


@dataclass(slots=True)
class CreateRequestResult:
    request: ScanRequest
    position: int  # 1 = next in line
    created: bool  # False if the user already had an active request


class ScanService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        notifier: ScanNotifier,
    ) -> None:
        self._sf = session_factory
        self._s = settings
        self._notifier = notifier
        self._claim_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ queries

    async def _position(self, session: AsyncSession, request: ScanRequest) -> int:
        ahead = await session.scalar(
            select(func.count())
            .select_from(ScanRequest)
            .where(
                ScanRequest.status == ScanStatus.PENDING,
                ScanRequest.expires_at > utcnow(),
                (ScanRequest.created_at < request.created_at)
                | ((ScanRequest.created_at == request.created_at) & (ScanRequest.id < request.id)),
            )
        )
        return int(ahead or 0) + 1

    async def get_active_request(self, telegram_id: int) -> ScanRequest | None:
        async with self._sf() as session:
            return await session.scalar(
                select(ScanRequest).where(
                    ScanRequest.telegram_user_id == telegram_id,
                    ScanRequest.status.in_([ScanStatus.PENDING, ScanStatus.PROCESSING]),
                    ScanRequest.expires_at > utcnow(),
                )
            )

    async def pending_count(self) -> int:
        async with self._sf() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(ScanRequest)
                    .where(ScanRequest.status == ScanStatus.PENDING, ScanRequest.expires_at > utcnow())
                )
                or 0
            )

    # ------------------------------------------------------------------ lifecycle

    async def create_request(self, telegram_id: int) -> CreateRequestResult:
        # Expire first so a stale row does not block the unique "one active request" index.
        await self.expire_stale(notify=False)
        now = utcnow()
        async with self._sf() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(ScanRequest).where(
                        ScanRequest.telegram_user_id == telegram_id,
                        ScanRequest.status.in_([ScanStatus.PENDING, ScanStatus.PROCESSING]),
                    )
                )
                if existing is not None:
                    return CreateRequestResult(existing, await self._position(session, existing), created=False)
            try:
                async with session.begin():
                    request = ScanRequest(
                        telegram_user_id=telegram_id,
                        status=ScanStatus.PENDING,
                        created_at=now,
                        expires_at=now + timedelta(seconds=self._s.scan_request_timeout),
                    )
                    session.add(request)
            except IntegrityError:
                # Concurrent double-press: return the request that won.
                async with session.begin():
                    existing = await session.scalar(
                        select(ScanRequest).where(
                            ScanRequest.telegram_user_id == telegram_id,
                            ScanRequest.status.in_([ScanStatus.PENDING, ScanStatus.PROCESSING]),
                        )
                    )
                if existing is None:
                    raise
                async with session.begin():
                    return CreateRequestResult(existing, await self._position(session, existing), created=False)
            async with session.begin():
                position = await self._position(session, request)
        event(log, "scan_request_created", request_id=request.id, telegram_id=telegram_id, position=position)
        return CreateRequestResult(request, position, created=True)

    async def cancel_request(self, telegram_id: int) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                update(ScanRequest)
                .where(ScanRequest.telegram_user_id == telegram_id, ScanRequest.status == ScanStatus.PENDING)
                .values(status=ScanStatus.CANCELLED, completed_at=utcnow())
            )
        if result.rowcount:
            event(log, "scan_request_cancelled", telegram_id=telegram_id)
        return bool(result.rowcount)

    async def expire_stale(self, notify: bool = True) -> list[int]:
        """Mark overdue pending requests as expired; returns affected Telegram user IDs."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            rows = (
                await session.execute(
                    select(ScanRequest.id, ScanRequest.telegram_user_id).where(
                        ScanRequest.status == ScanStatus.PENDING, ScanRequest.expires_at <= now
                    ).with_for_update(skip_locked=True)
                )
            ).all()
            if not rows:
                return []
            await session.execute(
                update(ScanRequest)
                .where(ScanRequest.id.in_([r.id for r in rows]), ScanRequest.status == ScanStatus.PENDING)
                .values(status=ScanStatus.EXPIRED, completed_at=now)
            )
        users = [r.telegram_user_id for r in rows]
        for request_id, user_id in rows:
            event(log, "scan_request_expired", request_id=request_id, telegram_id=user_id)
        if notify:
            for user_id in users:
                await self._safe_notify(user_id, "⌛ Your scan request expired.\n\nPress “📥 Receive Scan” again when you are ready.")
        return users

    async def recover_after_restart(self) -> None:
        """Requests stuck in 'processing' belong to a crashed run; they cannot complete."""
        async with self._sf() as session, session.begin():
            result = await session.execute(
                update(ScanRequest)
                .where(ScanRequest.status == ScanStatus.PROCESSING)
                .values(status=ScanStatus.FAILED, error="bot restarted", completed_at=utcnow())
            )
        if result.rowcount:
            event(log, "scan_requests_recovered", level=logging.WARNING, count=result.rowcount)

    async def _finish(self, request_id: int, status: ScanStatus, error: str | None = None) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                update(ScanRequest)
                .where(ScanRequest.id == request_id)
                .values(status=status, completed_at=utcnow(), error=error)
            )

    # ------------------------------------------------------------------ push mode

    async def claim_next(self) -> ScanRequest | None:
        """Atomically take the oldest non-expired pending request (FIFO)."""
        async with self._claim_lock:
            async with self._sf() as session, session.begin():
                request = await session.scalar(
                    select(ScanRequest)
                    .where(ScanRequest.status == ScanStatus.PENDING, ScanRequest.expires_at > utcnow())
                    .order_by(ScanRequest.created_at, ScanRequest.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if request is None:
                    return None
                request.status = ScanStatus.PROCESSING
            return request

    async def handle_incoming_scan(self, result: ScanResult) -> bool:
        """Called by a push scan source. Returns False when nobody is waiting."""
        request = await self.claim_next()
        if request is None:
            event(log, "scan_no_recipient", level=logging.WARNING, files=len(result.files))
            return False
        event(log, "scan_assigned", request_id=request.id, telegram_id=request.telegram_user_id)
        task = asyncio.create_task(self._deliver(request.id, request.telegram_user_id, result))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _deliver(self, request_id: int, telegram_id: int, result: ScanResult) -> None:
        try:
            await self._notifier.send_scan(telegram_id, result)
        except Exception as exc:
            log.exception("scan_delivery_failed", extra={"fields": {"request_id": request_id, "telegram_id": telegram_id}})
            await self._finish(request_id, ScanStatus.FAILED, error=f"delivery failed: {type(exc).__name__}")
            return
        await self._finish(request_id, ScanStatus.COMPLETED)
        event(log, "scan_delivered", request_id=request_id, telegram_id=telegram_id, files=len(result.files))

    # ------------------------------------------------------------------ helpers

    async def _safe_notify(self, telegram_id: int, text: str) -> None:
        try:
            await self._notifier.notify(telegram_id, text)
        except Exception:
            log.warning("notify_failed", extra={"fields": {"telegram_id": telegram_id}})

    async def run_expiry_loop(self, interval: float = 10) -> None:
        while True:
            try:
                await self.expire_stale()
            except Exception:
                log.exception("expiry_loop_error")
            await asyncio.sleep(interval)

    async def drain(self, timeout: float = 30) -> None:
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)
