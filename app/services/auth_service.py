from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import ScanRequest, ScanStatus, User, utcnow
from app.logging_setup import event

log = logging.getLogger(__name__)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class AuthOutcome(StrEnum):
    SUCCESS = "success"
    ALREADY = "already"
    INVALID = "invalid"
    LOCKED = "locked"


@dataclass(slots=True)
class AuthResult:
    outcome: AuthOutcome
    retry_after: int | None = None
    attempts_left: int | None = None


class AuthService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
        self._sf = session_factory
        self._s = settings
        self._code = settings.auth_code.get_secret_value().encode()

    async def touch_user(self, telegram_id: int) -> User:
        """Get or create the user row and refresh last_seen_at."""
        if telegram_id <= 0:
            raise ValueError("invalid telegram id")
        async with self._sf() as session, session.begin():
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id, is_authenticated=False)
                session.add(user)
            else:
                user.last_seen_at = utcnow()
        return user

    async def is_authenticated(self, telegram_id: int) -> bool:
        async with self._sf() as session:
            value = await session.scalar(select(User.is_authenticated).where(User.telegram_id == telegram_id))
        return bool(value)

    def _code_matches(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.strip().encode(), self._code)

    async def authenticate(self, telegram_id: int, candidate: str) -> AuthResult:
        now = utcnow()
        async with self._sf() as session, session.begin():
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
                await session.flush()
            if user.is_authenticated:
                return AuthResult(AuthOutcome.ALREADY)

            locked_until = as_utc(user.auth_locked_until)
            if locked_until and locked_until > now:
                return AuthResult(AuthOutcome.LOCKED, retry_after=int((locked_until - now).total_seconds()) + 1)

            if self._code_matches(candidate):
                user.is_authenticated = True
                user.authenticated_at = now
                user.failed_auth_attempts = 0
                user.auth_locked_until = None
                event(log, "user_authenticated", telegram_id=telegram_id)
                return AuthResult(AuthOutcome.SUCCESS)

            user.failed_auth_attempts = (user.failed_auth_attempts or 0) + 1
            if user.failed_auth_attempts >= self._s.auth_max_attempts:
                user.failed_auth_attempts = 0
                user.auth_locked_until = now + timedelta(seconds=self._s.auth_lockout_seconds)
                event(log, "user_auth_locked", level=logging.WARNING, telegram_id=telegram_id)
                return AuthResult(AuthOutcome.LOCKED, retry_after=self._s.auth_lockout_seconds)
            event(log, "user_auth_failed", level=logging.WARNING, telegram_id=telegram_id)
            return AuthResult(
                AuthOutcome.INVALID, attempts_left=self._s.auth_max_attempts - user.failed_auth_attempts
            )

    async def logout(self, telegram_id: int) -> bool:
        async with self._sf() as session, session.begin():
            result = await session.execute(
                update(User)
                .where(User.telegram_id == telegram_id, User.is_authenticated.is_(True))
                .values(is_authenticated=False, authenticated_at=None)
            )
            # A logged-out user must not receive scans.
            await session.execute(
                update(ScanRequest)
                .where(ScanRequest.telegram_user_id == telegram_id, ScanRequest.status == ScanStatus.PENDING)
                .values(status=ScanStatus.CANCELLED, completed_at=utcnow())
            )
        changed = result.rowcount > 0
        if changed:
            event(log, "user_logged_out", telegram_id=telegram_id)
        return changed
