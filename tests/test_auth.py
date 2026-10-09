from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery
from sqlalchemy import select, text

from app.bot import texts
from app.bot.handlers import auth as auth_handlers
from app.bot.handlers import common
from app.bot.middleware import AuthRequiredMiddleware, UserContextMiddleware
from app.database.models import User
from app.services.auth_service import AuthOutcome, AuthService
from tests.conftest import AUTH_CODE, answered_texts, authenticated, make_message, make_settings


async def test_new_user_is_unauthenticated(auth_service):
    user = await auth_service.touch_user(1001)
    assert user.is_authenticated is False
    assert await auth_service.is_authenticated(1001) is False


async def test_start_unauthenticated_asks_for_code(auth_service):
    user = await auth_service.touch_user(1001)
    message = make_message("/start")
    await common.cmd_start(message, user)
    assert answered_texts(message) == [texts.AUTH_REQUIRED]


async def test_successful_authentication(auth_service):
    user = await auth_service.touch_user(1001)
    message = make_message(AUTH_CODE)
    await auth_handlers.access_code(message, user, auth_service)
    assert answered_texts(message) == [texts.AUTH_SUCCESS]
    message.delete.assert_awaited()  # the code is removed from the chat
    assert await auth_service.is_authenticated(1001)


async def test_invalid_code(auth_service):
    user = await auth_service.touch_user(1001)
    message = make_message("wrong-code")
    await auth_handlers.access_code(message, user, auth_service)
    assert answered_texts(message) == [texts.AUTH_INVALID]
    assert not await auth_service.is_authenticated(1001)


async def test_code_with_surrounding_whitespace_is_accepted(auth_service):
    await auth_service.touch_user(1001)
    result = await auth_service.authenticate(1001, f"  {AUTH_CODE}\n")
    assert result.outcome is AuthOutcome.SUCCESS


async def test_auth_code_is_never_stored(auth_service, session_factory):
    await auth_service.touch_user(1001)
    await auth_service.authenticate(1001, "wrong-code-attempt")
    await auth_service.authenticate(1001, AUTH_CODE)
    async with session_factory() as session:
        for table in ("users", "scan_requests", "print_jobs"):
            rows = (await session.execute(text(f"SELECT * FROM {table}"))).all()
            dump = repr(rows)
            assert AUTH_CODE not in dump
            assert "wrong-code-attempt" not in dump


async def test_authorization_survives_restart(session_factory, settings):
    await authenticated(AuthService(session_factory, settings), 1001)
    # A fresh service instance (as after a bot restart) sees the persisted state.
    assert await AuthService(session_factory, settings).is_authenticated(1001)


async def test_authentication_is_per_telegram_user(auth_service):
    await authenticated(auth_service, 1001)
    assert not await auth_service.is_authenticated(2002)


async def test_lockout_after_repeated_failures(session_factory, tmp_path):
    service = AuthService(session_factory, make_settings(tmp_path, auth_max_attempts=3))
    await service.touch_user(1001)
    outcomes = [(await service.authenticate(1001, "bad")).outcome for _ in range(3)]
    assert outcomes == [AuthOutcome.INVALID, AuthOutcome.INVALID, AuthOutcome.LOCKED]
    # Even the correct code is refused while locked.
    locked = await service.authenticate(1001, AUTH_CODE)
    assert locked.outcome is AuthOutcome.LOCKED
    assert locked.retry_after and locked.retry_after > 0
    assert not await service.is_authenticated(1001)


async def test_logout(auth_service):
    user = await authenticated(auth_service, 1001)
    message = make_message("/logout")
    await common.cmd_logout(message, user, auth_service)
    assert answered_texts(message) == [texts.LOGGED_OUT]
    assert not await auth_service.is_authenticated(1001)


async def test_logout_when_not_logged_in(auth_service):
    user = await auth_service.touch_user(1001)
    message = make_message("/logout")
    await common.cmd_logout(message, user, auth_service)
    assert answered_texts(message) == [texts.NOT_LOGGED_IN]


async def test_logged_in_user_start_shows_menu(auth_service):
    user = await authenticated(auth_service, 1001)
    message = make_message("/start")
    await common.cmd_start(message, user)
    assert answered_texts(message) == [texts.WELCOME]
    markup = message.answer.call_args.kwargs["reply_markup"]
    labels = [b.text for row in markup.keyboard for b in row]
    assert labels == ["🖨 Print", "📥 Receive Scan"]


# ------------------------------------------------------------- unauthorized access


async def test_auth_required_middleware_blocks_messages(auth_service):
    user = await auth_service.touch_user(1001)
    handler = AsyncMock()
    message = make_message("📥 Receive Scan")
    await AuthRequiredMiddleware()(handler, message, {"user": user})
    handler.assert_not_awaited()
    assert answered_texts(message) == [texts.AUTH_REQUIRED]


async def test_auth_required_middleware_blocks_callbacks(auth_service):
    user = await auth_service.touch_user(1001)
    handler = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.answer = AsyncMock()
    await AuthRequiredMiddleware()(handler, callback, {"user": user})
    handler.assert_not_awaited()
    callback.answer.assert_awaited()


async def test_auth_required_middleware_allows_authenticated(auth_service):
    user = await authenticated(auth_service, 1001)
    handler = AsyncMock(return_value="ok")
    assert await AuthRequiredMiddleware()(handler, make_message("x"), {"user": user}) == "ok"


async def test_logged_out_user_is_blocked_again(auth_service):
    await authenticated(auth_service, 1001)
    await auth_service.logout(1001)
    user = await auth_service.touch_user(1001)
    handler = AsyncMock()
    await AuthRequiredMiddleware()(handler, make_message("🖨 Print"), {"user": user})
    handler.assert_not_awaited()


@pytest.mark.parametrize(
    "from_user,chat_type",
    [
        (MagicMock(id=1001, is_bot=True), "private"),  # bots
        (MagicMock(id=1001, is_bot=False), "group"),  # group chats
        (MagicMock(id=-5, is_bot=False), "private"),  # invalid ids
        (None, "private"),
    ],
)
async def test_user_context_rejects_invalid_senders(auth_service, session_factory, from_user, chat_type):
    handler = AsyncMock()
    data = {"event_from_user": from_user, "event_chat": MagicMock(type=chat_type)}
    await UserContextMiddleware(auth_service)(handler, make_message("hi"), data)
    handler.assert_not_awaited()
    async with session_factory() as session:
        assert (await session.scalars(select(User))).all() == []


async def test_user_context_loads_user(auth_service):
    handler = AsyncMock()
    data = {"event_from_user": MagicMock(id=1001, is_bot=False), "event_chat": MagicMock(type="private")}
    await UserContextMiddleware(auth_service)(handler, make_message("hi"), data)
    handler.assert_awaited_once()
    assert data["user"].telegram_id == 1001


async def test_unauthenticated_unknown_message_asks_for_auth(auth_service):
    user = await auth_service.touch_user(1001)
    message = make_message(None)
    await common.unknown_message(message, user)
    assert answered_texts(message) == [texts.AUTH_REQUIRED]
