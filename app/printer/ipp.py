"""Minimal IPP/2.0 client (RFC 8010 / RFC 8011) — just what this bot needs.

Operations: Print-Job, Get-Job-Attributes, Cancel-Job, Get-Printer-Attributes.
"""

from __future__ import annotations

import itertools
import ssl
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

import aiohttp


class Op(IntEnum):
    PRINT_JOB = 0x0002
    CANCEL_JOB = 0x0008
    GET_JOB_ATTRIBUTES = 0x0009
    GET_PRINTER_ATTRIBUTES = 0x000B


class Tag(IntEnum):
    OPERATION = 0x01
    JOB = 0x02
    END = 0x03
    PRINTER = 0x04
    UNSUPPORTED = 0x05

    INTEGER = 0x21
    BOOLEAN = 0x22
    ENUM = 0x23
    DATETIME = 0x31
    RESOLUTION = 0x32
    RANGE = 0x33
    BEG_COLLECTION = 0x34
    TEXT_WITH_LANG = 0x35
    NAME_WITH_LANG = 0x36
    END_COLLECTION = 0x37
    TEXT = 0x41
    NAME = 0x42
    KEYWORD = 0x44
    URI = 0x45
    CHARSET = 0x47
    LANGUAGE = 0x48
    MIME = 0x49
    MEMBER_NAME = 0x4A


class JobState(IntEnum):
    PENDING = 3
    PENDING_HELD = 4
    PROCESSING = 5
    PROCESSING_STOPPED = 6
    CANCELED = 7
    ABORTED = 8
    COMPLETED = 9


TERMINAL_JOB_STATES = {JobState.CANCELED, JobState.ABORTED, JobState.COMPLETED}

PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}

STATUS_VERSION_NOT_SUPPORTED = 0x0503
STATUS_BUSY = 0x0507  # server-error-busy
STATUS_NOT_ACCEPTING = 0x0506  # server-error-not-accepting-jobs

_INT_TAGS = {Tag.INTEGER, Tag.ENUM}
_DELIMITER_MAX = 0x0F


class IppError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------

Value = str | int | bool


@dataclass(slots=True)
class Attr:
    tag: Tag
    name: str
    values: list[Value]


def _encode_value(tag: Tag, value: Value) -> bytes:
    if tag in _INT_TAGS:
        return struct.pack(">i", int(value))
    if tag is Tag.BOOLEAN:
        return b"\x01" if value else b"\x00"
    return str(value).encode("utf-8")


def encode_request(
    operation: Op,
    request_id: int,
    groups: list[tuple[Tag, list[Attr]]],
    version: tuple[int, int] = (2, 0),
) -> bytes:
    out = bytearray(struct.pack(">BBHI", version[0], version[1], int(operation), request_id))
    for group_tag, attrs in groups:
        out.append(int(group_tag))
        for attr in attrs:
            for i, value in enumerate(attr.values):
                name = attr.name.encode("ascii") if i == 0 else b""
                data = _encode_value(attr.tag, value)
                out.append(int(attr.tag))
                out += struct.pack(">H", len(name)) + name
                out += struct.pack(">H", len(data)) + data
    out.append(int(Tag.END))
    return bytes(out)


# --------------------------------------------------------------------------
# Decoding
# --------------------------------------------------------------------------


@dataclass(slots=True)
class IppResponse:
    version: tuple[int, int]
    status_code: int
    request_id: int
    groups: list[tuple[int, dict[str, list[Any]]]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # successful-ok (0x0000) .. successful-ok-* (0x00FF)
        return self.status_code <= 0x00FF

    def group(self, tag: Tag) -> dict[str, list[Any]]:
        merged: dict[str, list[Any]] = {}
        for gtag, attrs in self.groups:
            if gtag == tag:
                merged.update(attrs)
        return merged

    def first(self, tag: Tag, name: str, default: Any = None) -> Any:
        values = self.group(tag).get(name)
        return values[0] if values else default


def _decode_value(tag: int, data: bytes) -> Any:
    if tag in (Tag.INTEGER, Tag.ENUM):
        return struct.unpack(">i", data)[0]
    if tag == Tag.BOOLEAN:
        return data != b"\x00"
    if tag == Tag.RESOLUTION:
        x, y, units = struct.unpack(">iib", data)
        return (x, y, units)
    if tag == Tag.RANGE:
        return struct.unpack(">ii", data)
    if tag in (Tag.TEXT_WITH_LANG, Tag.NAME_WITH_LANG):
        lang_len = struct.unpack(">H", data[:2])[0]
        text_len = struct.unpack(">H", data[2 + lang_len : 4 + lang_len])[0]
        return data[4 + lang_len : 4 + lang_len + text_len].decode("utf-8", "replace")
    if 0x40 <= tag <= 0x5F:
        return data.decode("utf-8", "replace")
    return data  # octetString, dateTime, unknown


def decode_response(payload: bytes) -> IppResponse:
    return decode_message(payload)[0]


def decode_message(payload: bytes) -> tuple[IppResponse, int]:
    """Decode an IPP message; returns it and the offset of any trailing document data.

    For requests, `status_code` holds the operation-id (same wire layout).
    """
    if len(payload) < 9:
        raise IppError("IPP response too short")
    major, minor, status, request_id = struct.unpack(">BBHI", payload[:8])
    resp = IppResponse(version=(major, minor), status_code=status, request_id=request_id)
    pos = 8
    current: dict[str, list[Any]] | None = None
    last_name: str | None = None
    depth = 0  # collection nesting; member values are skipped (not needed here)
    try:
        while pos < len(payload):
            tag = payload[pos]
            pos += 1
            if tag <= _DELIMITER_MAX:
                if tag == Tag.END:
                    break
                current = {}
                resp.groups.append((tag, current))
                last_name = None
                continue
            name_len = struct.unpack(">H", payload[pos : pos + 2])[0]
            pos += 2
            name = payload[pos : pos + name_len].decode("ascii", "replace")
            pos += name_len
            value_len = struct.unpack(">H", payload[pos : pos + 2])[0]
            pos += 2
            data = payload[pos : pos + value_len]
            if len(data) != value_len:
                raise IppError("Truncated IPP attribute")
            pos += value_len

            if tag == Tag.BEG_COLLECTION:
                if depth == 0 and current is not None:
                    if name:
                        last_name = name
                    if last_name:
                        current.setdefault(last_name, []).append({})
                depth += 1
                continue
            if tag == Tag.END_COLLECTION:
                depth = max(0, depth - 1)
                continue
            if depth > 0 or current is None:
                continue
            if name:
                last_name = name
            if last_name is None:
                continue
            current.setdefault(last_name, []).append(_decode_value(tag, data))
    except struct.error as exc:
        raise IppError(f"Malformed IPP response: {exc}") from exc
    return resp, pos


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def _operation_attrs(printer_uri: str, extra: list[Attr] | None = None) -> list[Attr]:
    attrs = [
        Attr(Tag.CHARSET, "attributes-charset", ["utf-8"]),
        Attr(Tag.LANGUAGE, "attributes-natural-language", ["en"]),
        Attr(Tag.URI, "printer-uri", [printer_uri]),
        Attr(Tag.NAME, "requesting-user-name", ["telegram-bot"]),
    ]
    return attrs + (extra or [])


class IppClient:
    def __init__(
        self,
        http_url: str,
        printer_uri: str,
        *,
        connect_timeout: float = 10,
        verify_tls: bool = False,
    ) -> None:
        self.http_url = http_url
        self.printer_uri = printer_uri
        self._connect_timeout = connect_timeout
        self._ssl: ssl.SSLContext | bool | None = None
        if http_url.startswith("https"):
            if verify_tls:
                self._ssl = ssl.create_default_context()
            else:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                self._ssl = ctx
        self._request_ids = itertools.count(1)
        self._version: tuple[int, int] = (2, 0)

    async def _post(self, body: bytes | Path, header: bytes, timeout: float) -> IppResponse:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout, sock_connect=self._connect_timeout)
        headers = {"Content-Type": "application/ipp"}
        data: bytes | AsyncIterator[bytes]
        if isinstance(body, Path):
            # Explicit Content-Length: some embedded IPP servers mishandle chunked uploads.
            headers["Content-Length"] = str(len(header) + body.stat().st_size)
            data = _stream_file(header, body)
        else:
            data = header + body
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(self.http_url, data=data, headers=headers, ssl=self._ssl) as r:
                if r.status != 200:
                    raise IppError(f"HTTP {r.status} from printer")
                content = await r.read()
        return decode_response(content)

    async def request(
        self,
        operation: Op,
        groups: list[tuple[Tag, list[Attr]]],
        *,
        document: Path | None = None,
        timeout: float = 30,
    ) -> IppResponse:
        for _ in range(2):
            header = encode_request(operation, next(self._request_ids), groups, self._version)
            resp = await self._post(document if document else b"", header, timeout)
            if resp.status_code == STATUS_VERSION_NOT_SUPPORTED and self._version != (1, 1):
                self._version = (1, 1)
                continue
            return resp
        return resp

    async def get_printer_attributes(self, names: list[str], timeout: float = 15) -> IppResponse:
        groups = [
            (
                Tag.OPERATION,
                _operation_attrs(
                    self.printer_uri, [Attr(Tag.KEYWORD, "requested-attributes", list(names))]
                ),
            )
        ]
        resp = await self.request(Op.GET_PRINTER_ATTRIBUTES, groups, timeout=timeout)
        if not resp.ok:
            raise IppError(f"Get-Printer-Attributes failed: 0x{resp.status_code:04x}", resp.status_code)
        return resp

    async def print_job(
        self,
        document: Path,
        *,
        document_format: str,
        job_name: str,
        job_attrs: list[Attr],
        timeout: float,
    ) -> IppResponse:
        op_attrs = _operation_attrs(
            self.printer_uri,
            [
                Attr(Tag.NAME, "job-name", [job_name]),
                Attr(Tag.MIME, "document-format", [document_format]),
            ],
        )
        groups = [(Tag.OPERATION, op_attrs), (Tag.JOB, job_attrs)]
        return await self.request(Op.PRINT_JOB, groups, document=document, timeout=timeout)

    async def get_job_attributes(self, job_id: int, timeout: float = 15) -> IppResponse:
        op_attrs = _operation_attrs(
            self.printer_uri,
            [
                Attr(Tag.INTEGER, "job-id", [job_id]),
                Attr(
                    Tag.KEYWORD,
                    "requested-attributes",
                    ["job-state", "job-state-reasons", "job-impressions-completed"],
                ),
            ],
        )
        return await self.request(Op.GET_JOB_ATTRIBUTES, [(Tag.OPERATION, op_attrs)], timeout=timeout)

    async def cancel_job(self, job_id: int, timeout: float = 15) -> IppResponse:
        op_attrs = _operation_attrs(self.printer_uri, [Attr(Tag.INTEGER, "job-id", [job_id])])
        return await self.request(Op.CANCEL_JOB, [(Tag.OPERATION, op_attrs)], timeout=timeout)


async def _stream_file(header: bytes, path: Path, chunk_size: int = 256 * 1024) -> AsyncIterator[bytes]:
    yield header
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            yield chunk
