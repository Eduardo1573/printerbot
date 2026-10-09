from __future__ import annotations

import io
import logging

import pytest
from pydantic import ValidationError

from app.logging_setup import JsonFormatter, RedactingFilter, event
from app.printer.models import capabilities, normalize_model
from tests.conftest import AUTH_CODE, make_settings


def test_short_auth_code_rejected(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, auth_code="1234")


def test_email_mode_requires_smtp_password(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, scan_smtp_password=None)


def test_pdf_size_capped_at_telegram_limit(tmp_path):
    with pytest.raises(ValidationError):
        make_settings(tmp_path, max_pdf_size_mb=25)


def test_urls(tmp_path):
    s = make_settings(tmp_path, printer_host="10.0.0.5", printer_port=631)
    assert s.printer_uri == "ipp://10.0.0.5:631/ipp/print"
    assert s.printer_http_url == "http://10.0.0.5:631/ipp/print"
    assert make_settings(tmp_path, printer_use_tls=True).printer_uri.startswith("ipps://")


def test_postgres_url_gets_async_driver(tmp_path):
    s = make_settings(tmp_path, database_url="postgresql://u:p@db/x")
    assert s.database_url == "postgresql+asyncpg://u:p@db/x"


@pytest.mark.parametrize(
    "raw,model,scan_to_email,networked",
    [
        ("HP LaserJet Pro MFP M130fw", "M130fw", True, True),
        ("M132FW", "M132fw", True, True),
        ("M132nw", "M132nw", False, True),
        ("M134fn", "M134fn", False, True),
        ("M130a", "M130a", False, False),
    ],
)
def test_model_capabilities(raw, model, scan_to_email, networked):
    caps = capabilities(raw)
    assert caps and caps.model == model
    assert caps.scan_to_email is scan_to_email and caps.networked is networked
    assert normalize_model(raw) == model


def test_unknown_model():
    assert capabilities("LaserJet M404") is None


def test_secrets_are_redacted_from_logs(tmp_path):
    settings = make_settings(tmp_path)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactingFilter(settings.secrets()))
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("redaction-test")
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    token = settings.telegram_bot_token.get_secret_value()
    logger.warning("request to https://api.telegram.org/bot%s/getMe failed", token)
    event(logger, "auth_attempt", code=AUTH_CODE)
    try:
        raise RuntimeError(f"boom {AUTH_CODE}")
    except RuntimeError:
        logger.exception("crash")
    output = stream.getvalue()
    assert token not in output and AUTH_CODE not in output
    assert output.count("***") >= 3


def test_validation_errors_do_not_echo_secrets(tmp_path):
    with pytest.raises(ValidationError) as exc:
        make_settings(tmp_path, auth_code="short7")
    assert "short7" not in str(exc.value)
