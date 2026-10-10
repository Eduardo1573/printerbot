"""Application configuration loaded from environment variables / `.env`."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScanMode(StrEnum):
    # Printer pushes scans to the bot's built-in SMTP receiver (Scan to E-mail).
    EMAIL = "email"
    # Bot pulls scans from the printer via eSCL (AirPrint scanning) over the VPN.
    ESCL = "escl"
    DISABLED = "disabled"


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error must never echo a secret value.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # --- Telegram / auth -------------------------------------------------
    telegram_bot_token: SecretStr
    auth_code: SecretStr = Field(min_length=8)
    auth_max_attempts: int = Field(default=5, ge=1)
    auth_lockout_seconds: int = Field(default=900, ge=1)

    # --- Database --------------------------------------------------------
    database_url: str

    # --- Printer (IPP printing) -----------------------------------------
    printer_model: str = "M130fw"
    printer_host: str
    printer_port: int = 631
    printer_ipp_path: str = "/ipp/print"
    printer_use_tls: bool = False
    # HP printers ship a self-signed certificate, so verification is off by default.
    # Traffic is carried inside the WireGuard tunnel anyway.
    printer_verify_tls: bool = False
    printer_paper_size: Literal["a4", "letter"] = "a4"
    printer_connect_timeout: int = Field(default=10, ge=1)

    # --- Print limits ----------------------------------------------------
    # The public Telegram Bot API cannot download files larger than 20 MB.
    max_pdf_size_mb: int = Field(default=20, ge=1, le=20)
    max_pdf_pages: int = Field(default=100, ge=1)
    print_timeout: int = Field(default=120, ge=10)
    max_concurrent_print_jobs: int = Field(default=1, ge=1, le=10)

    # --- Scanning --------------------------------------------------------
    scan_mode: ScanMode = ScanMode.EMAIL
    scan_request_timeout: int = Field(default=300, ge=30)
    max_scan_size_mb: int = Field(default=45, ge=1, le=50)

    # Built-in SMTP receiver (scan_mode=email)
    scan_smtp_listen_host: str = "0.0.0.0"
    scan_smtp_listen_port: int = 2525
    scan_smtp_username: str = "printer"
    scan_smtp_password: SecretStr | None = None
    scan_smtp_allowed_networks: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    # none: plaintext inside the WireGuard tunnel (recommended; the tunnel encrypts).
    # starttls / implicit: when the printer reaches the SMTP port over the Internet.
    scan_smtp_tls_mode: Literal["none", "starttls", "implicit"] = "none"
    scan_smtp_tls_cert: Path | None = None
    scan_smtp_tls_key: Path | None = None

    # eSCL scanner (scan_mode=escl)
    escl_port: int = 8080
    escl_path: str = "/eSCL"
    escl_use_tls: bool = False
    # Auto: document feeder when paper is loaded there, otherwise the glass.
    escl_input_source: Literal["Auto", "Platen", "Feeder"] = "Auto"
    escl_resolution: int = 300
    escl_color_mode: Literal["RGB24", "Grayscale8", "BlackAndWhite1"] = "RGB24"
    # Multi-page sessions merge per-page PDFs, so the scanner must return PDF.
    escl_document_format: Literal["application/pdf"] = "application/pdf"
    # Idle seconds before an unfinished multi-page scan session is discarded.
    scan_session_timeout: int = Field(default=600, ge=60)
    scan_max_pages: int = Field(default=50, ge=1, le=200)

    # --- Runtime ---------------------------------------------------------
    temp_dir: Path = Path("/var/tmp/printerbot")
    health_file: Path = Path("/tmp/printerbot.health")
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"

    @field_validator("printer_ipp_path", "escl_path")
    @classmethod
    def _leading_slash(cls, v: str) -> str:
        return v if v.startswith("/") else "/" + v

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        if v.startswith("postgresql://") or v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v.split("://", 1)[1]
        return v

    @model_validator(mode="after")
    def _check_scan_settings(self) -> Settings:
        if self.scan_mode is ScanMode.EMAIL:
            if self.scan_smtp_password is None or len(self.scan_smtp_password.get_secret_value()) < 12:
                raise ValueError("SCAN_SMTP_PASSWORD (min 12 chars) is required when SCAN_MODE=email")
            if self.scan_smtp_tls_mode != "none" and not (self.scan_smtp_tls_cert and self.scan_smtp_tls_key):
                raise ValueError("SCAN_SMTP_TLS_CERT and SCAN_SMTP_TLS_KEY are required when SCAN_SMTP_TLS_MODE is not 'none'")
        self.allowed_smtp_networks  # validate eagerly
        return self

    @cached_property
    def allowed_smtp_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        return tuple(
            ipaddress.ip_network(part.strip(), strict=False)
            for part in self.scan_smtp_allowed_networks.split(",")
            if part.strip()
        )

    @property
    def max_pdf_size_bytes(self) -> int:
        return self.max_pdf_size_mb * 1024 * 1024

    @property
    def max_scan_size_bytes(self) -> int:
        return self.max_scan_size_mb * 1024 * 1024

    def _bracketed_host(self) -> str:
        return f"[{self.printer_host}]" if ":" in self.printer_host else self.printer_host

    @property
    def printer_http_url(self) -> str:
        """HTTP(S) URL the IPP request is POSTed to."""
        scheme = "https" if self.printer_use_tls else "http"
        return f"{scheme}://{self._bracketed_host()}:{self.printer_port}{self.printer_ipp_path}"

    @property
    def printer_uri(self) -> str:
        """`printer-uri` IPP operation attribute."""
        scheme = "ipps" if self.printer_use_tls else "ipp"
        return f"{scheme}://{self._bracketed_host()}:{self.printer_port}{self.printer_ipp_path}"

    @property
    def escl_base_url(self) -> str:
        scheme = "https" if self.escl_use_tls else "http"
        return f"{scheme}://{self._bracketed_host()}:{self.escl_port}{self.escl_path}"

    def secrets(self) -> list[str]:
        """Secret values that must never appear in logs."""
        values = [self.telegram_bot_token.get_secret_value(), self.auth_code.get_secret_value()]
        if self.scan_smtp_password:
            values.append(self.scan_smtp_password.get_secret_value())
        return [v for v in values if v]


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
