"""Unix-socket transport for the versioned adapter envelopes."""

import os
import socket
from typing import Any

from .codec import EnvelopeStream, encode_envelope


class UnixEnvelopeChannel:
    """Small blocking channel; ROS2 remains on the other process side."""

    def __init__(self, sock: socket.socket, recv_bytes: int = 64 * 1024):
        if recv_bytes < 4:
            raise ValueError("recv_bytes must be at least four bytes")
        self.sock = sock
        self.recv_bytes = recv_bytes
        self._stream = EnvelopeStream()

    def send(self, kind: str, payload: dict[str, Any]) -> None:
        self.sock.sendall(encode_envelope(kind, payload))

    def receive(self) -> list[dict[str, Any]]:
        chunk = self.sock.recv(self.recv_bytes)
        if not chunk:
            return []
        return self._stream.feed(chunk)

    def close(self) -> None:
        self.sock.close()


class UnixEnvelopeServer:
    """Single-client Unix socket endpoint owned by the simulation process.

    The simulation owns the filesystem path and accepts one adapter connection
    at a time.  A caller can close the returned channel and accept a new
    adapter after a restart without changing the wire protocol.
    """

    def __init__(self, path: str, backlog: int = 1):
        if not isinstance(path, str) or not path:
            raise ValueError("path must be a non-empty string")
        if backlog < 1:
            raise ValueError("backlog must be positive")
        self.path = path
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._socket.bind(path)
            self._socket.listen(backlog)
        except Exception:
            self._socket.close()
            raise
        self._closed = False

    def accept(self, recv_bytes: int = 64 * 1024) -> UnixEnvelopeChannel:
        if self._closed:
            raise RuntimeError("UnixEnvelopeServer is closed")
        connection, _ = self._socket.accept()
        return UnixEnvelopeChannel(connection, recv_bytes=recv_bytes)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._socket.close()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
