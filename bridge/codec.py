"""Small versioned framing protocol shared by the core and ROS2 adapter.

The adapter runs in the system ROS2 Python/C++ environment, so this module only
uses the standard library and remains compatible with Python 3.10.
"""

import json
import struct
from typing import Any


PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_HEADER = struct.Struct(">I")


class EnvelopeStream:
    """Incremental decoder for a byte-stream socket."""

    def __init__(self) -> None:
        self._buffer = b""

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("chunk must be bytes-like")
        self._buffer += bytes(chunk)
        messages: list[dict[str, Any]] = []
        while self._buffer:
            decoded = decode_envelope(self._buffer)
            if decoded is None:
                break
            message, self._buffer = decoded
            messages.append(message)
        return messages


def encode_envelope(kind: str, payload: dict[str, Any]) -> bytes:
    if not kind or not isinstance(payload, dict):
        raise ValueError("kind must be non-empty and payload must be an object")
    body = json.dumps(
        {"version": PROTOCOL_VERSION, "kind": kind, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds MAX_MESSAGE_BYTES")
    return _HEADER.pack(len(body)) + body


def decode_envelope(buffer: bytes) -> tuple[dict[str, Any], bytes] | None:
    """Decode one envelope, returning ``None`` until a complete message exists."""
    if len(buffer) < _HEADER.size:
        return None
    (body_size,) = _HEADER.unpack(buffer[:_HEADER.size])
    if body_size > MAX_MESSAGE_BYTES:
        raise ValueError("message exceeds MAX_MESSAGE_BYTES")
    end = _HEADER.size + body_size
    if len(buffer) < end:
        return None
    try:
        message = json.loads(buffer[_HEADER.size:end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid envelope JSON") from exc
    if not isinstance(message, dict):
        raise ValueError("envelope must be an object")
    if message.get("version") != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    if not isinstance(message.get("kind"), str) or not message["kind"]:
        raise ValueError("envelope kind is required")
    if not isinstance(message.get("payload"), dict):
        raise ValueError("envelope payload must be an object")
    return message, buffer[end:]
