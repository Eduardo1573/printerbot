"""Built-in SMTP receiver for the printer's "Scan to E-mail" feature.

The HP M130fw / M132fw (touchscreen models) can e-mail scans through an SMTP server
configured in the Embedded Web Server. We point that SMTP server at the bot (via the
WireGuard tunnel), so each scan arrives here as an e-mail with the scan attached. The
bot never relays mail anywhere; it only accepts authenticated messages from the printer.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import hmac
import ipaddress
import logging
import ssl
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session

from app.config import Settings
from app.logging_setup import event
from app.printer.base import PushScanSource, ScanHandler, ScannedFile, ScanResult

log = logging.getLogger(__name__)

ACCEPTED_TYPES = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tif",
}

# Returned to the printer when nobody pressed "Receive Scan": the person standing at
# the printer sees a send error instead of silently losing the scan.
NO_RECIPIENT_REPLY = "550 5.7.1 No Telegram user is waiting for a scan"


def extract_scan(raw: bytes, max_size: int) -> ScanResult:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    files: list[ScannedFile] = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    total = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        ext = ACCEPTED_TYPES.get(ctype)
        if ext is None and ctype == "application/octet-stream":
            # Some firmwares label attachments generically; sniff the content.
            payload = part.get_payload(decode=True) or b""
            if payload.startswith(b"%PDF-"):
                ctype, ext = "application/pdf", "pdf"
            elif payload.startswith(b"\xff\xd8\xff"):
                ctype, ext = "image/jpeg", "jpg"
        if ext is None:
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        total += len(payload)
        if total > max_size:
            raise ValueError("scan exceeds size limit")
        # The sender-supplied filename is ignored on purpose.
        files.append(ScannedFile(payload, ctype, f"scan_{stamp}_{len(files) + 1}.{ext}"))
    return ScanResult(files=files, source="email")


def _peer_ip(peer) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        host = peer[0] if isinstance(peer, tuple) else str(peer)
        ip = ipaddress.ip_address(host)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            return ip.ipv4_mapped
        return ip
    except (ValueError, IndexError, TypeError):
        return None


class _Handler:
    def __init__(self, settings: Settings, accept: Callable[[ScanResult], Awaitable[bool]]) -> None:
        self._settings = settings
        self._accept = accept

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        peer = str(_peer_ip(session.peer))
        raw = envelope.original_content or envelope.content or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "surrogateescape")
        try:
            result = await asyncio.to_thread(extract_scan, raw, self._settings.max_scan_size_bytes)
        except ValueError:
            event(log, "scan_rejected", level=logging.WARNING, reason="too_large", peer=peer)
            return "552 5.3.4 Scan too large"
        except Exception:
            log.exception("scan_parse_failed")
            return "554 5.6.0 Could not parse message"
        if not result.files:
            # e.g. the EWS "Save and Test" message: accept so the printer's test passes.
            event(log, "smtp_message_without_scan_discarded", peer=peer)
            return "250 2.0.0 Accepted (no scan attachment, discarded)"
        event(log, "scan_received", source="email", files=len(result.files), bytes=result.total_size, peer=peer)
        accepted = await self._accept(result)
        return "250 2.0.0 Scan accepted" if accepted else NO_RECIPIENT_REPLY


class _PrinterSMTP(SMTP):
    """SMTP session that refuses clients outside the allowed networks."""

    allowed_networks: tuple = ()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        ip = _peer_ip(transport.get_extra_info("peername"))
        if ip is None or not any(ip in net for net in self.allowed_networks):
            event(log, "smtp_connection_refused", level=logging.WARNING, peer=str(ip))
            transport.close()


class SmtpScanReceiver(PushScanSource):
    def __init__(self, settings: Settings) -> None:
        self._s = settings
        self._server: asyncio.base_events.Server | None = None

    def _authenticate(self, server, session, envelope, mechanism, auth_data) -> AuthResult:
        if not isinstance(auth_data, LoginPassword):
            return AuthResult(success=False, handled=False)
        expected_user = self._s.scan_smtp_username.encode()
        expected_pass = self._s.scan_smtp_password.get_secret_value().encode()  # type: ignore[union-attr]
        ok = hmac.compare_digest(auth_data.login, expected_user) & hmac.compare_digest(
            auth_data.password, expected_pass
        )
        if not ok:
            event(log, "smtp_auth_failed", level=logging.WARNING, peer=str(_peer_ip(session.peer)))
            # handled=False makes aiosmtpd send "535 credentials invalid" itself.
            return AuthResult(success=False, handled=False)
        return AuthResult(success=True, auth_data=auth_data)

    def _tls_context(self) -> ssl.SSLContext | None:
        if self._s.scan_smtp_tls_mode == "none":
            return None
        ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ctx.load_cert_chain(str(self._s.scan_smtp_tls_cert), str(self._s.scan_smtp_tls_key))
        return ctx

    async def start(self, on_scan: ScanHandler) -> None:
        async def accept(result: ScanResult) -> bool:
            return bool(await on_scan(result))

        handler = _Handler(self._s, accept)
        tls = self._tls_context()
        mode = self._s.scan_smtp_tls_mode

        def factory() -> SMTP:
            smtp = _PrinterSMTP(
                handler,
                hostname="printerbot",
                ident="printerbot",
                data_size_limit=int(self._s.max_scan_size_bytes * 1.4) + 65536,  # base64 overhead
                authenticator=self._authenticate,
                auth_required=True,
                auth_require_tls=mode != "none",
                tls_context=tls if mode == "starttls" else None,
                require_starttls=mode == "starttls",
                enable_SMTPUTF8=True,
                timeout=120,
            )
            smtp.allowed_networks = self._s.allowed_smtp_networks
            return smtp

        if mode == "none":
            # Plaintext AUTH is intentional here: the SMTP port is only reachable through
            # the WireGuard tunnel, which already encrypts and authenticates the peer.
            warnings.filterwarnings("ignore", message="Requiring AUTH while not requiring TLS")
        loop = asyncio.get_running_loop()
        self._server = await loop.create_server(
            factory,
            host=self._s.scan_smtp_listen_host,
            port=self._s.scan_smtp_listen_port,
            ssl=tls if mode == "implicit" else None,
        )
        event(
            log,
            "smtp_receiver_started",
            host=self._s.scan_smtp_listen_host,
            port=self._s.scan_smtp_listen_port,
            tls=mode,
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
