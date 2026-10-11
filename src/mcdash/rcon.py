"""Minimal Source RCON client (as spoken by Minecraft servers).

Multi-packet responses are collected by sending a sentinel packet after each
command and reading until its reply arrives (Minecraft answers packets in
order, replying to the unknown type-0 request with its id).
"""

from __future__ import annotations

import itertools
import socket
import struct

SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

MAX_PACKET = 4096 + 14  # body limit Minecraft uses for responses, plus header
MAX_COMMAND = 1446


class RconError(Exception):
    pass


class RconAuthError(RconError):
    pass


def encode_packet(req_id: int, ptype: int, body: str) -> bytes:
    payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


class RconClient:
    def __init__(
        self, host: str, port: int, password: str, timeout: float = 5.0
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._ids = itertools.count(1)

    # context manager
    def __enter__(self) -> RconClient:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
        except OSError as e:
            raise RconError(
                f"cannot connect to RCON {self.host}:{self.port}: {e}"
            ) from e
        self._sock.settimeout(self.timeout)
        req = next(self._ids)
        self._send(req, SERVERDATA_AUTH, self.password)
        while True:
            rid, ptype, _ = self._recv()
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if rid == -1:
                    raise RconAuthError("RCON authentication failed")
                if rid == req:
                    return

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def command(self, cmd: str) -> str:
        if self._sock is None:
            raise RconError("not connected")
        if len(cmd.encode()) > MAX_COMMAND:
            raise RconError("command too long")
        req = next(self._ids)
        sentinel = next(self._ids)
        self._send(req, SERVERDATA_EXECCOMMAND, cmd)
        self._send(sentinel, SERVERDATA_RESPONSE_VALUE, "")
        parts: list[str] = []
        while True:
            rid, _, body = self._recv()
            if rid == sentinel:
                return "".join(parts)
            if rid == req:
                parts.append(body)
            elif rid == -1:
                raise RconAuthError("RCON session not authenticated")

    # wire
    def _send(self, req_id: int, ptype: int, body: str) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(encode_packet(req_id, ptype, body))
        except OSError as e:
            raise RconError(f"RCON send failed: {e}") from e

    def _read_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except TimeoutError as e:
                raise RconError("RCON timed out") from e
            except OSError as e:
                raise RconError(f"RCON receive failed: {e}") from e
            if not chunk:
                raise RconError("RCON connection closed")
            buf.extend(chunk)
        return bytes(buf)

    def _recv(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._read_exact(4))
        if length < 10 or length > MAX_PACKET + 1024:
            raise RconError(f"bad RCON packet length {length}")
        data = self._read_exact(length)
        req_id, ptype = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", "replace")
        return req_id, ptype, body


def rcon_command(
    host: str, port: int, password: str, cmd: str, timeout: float = 5.0
) -> str:
    with RconClient(host, port, password, timeout) as c:
        return c.command(cmd)


def parse_list(output: str) -> list[str]:
    """Parse ``list`` output: 'There are 2 of a max of 20 players online: a, b'."""
    _, sep, rest = output.partition(":")
    if not sep:
        return []
    return [p.strip() for p in rest.split(",") if p.strip()]
