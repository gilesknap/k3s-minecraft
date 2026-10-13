"""Minimal Source RCON client (as spoken by Minecraft servers).

Multi-packet responses are collected by sending a sentinel packet after each
command and reading until its reply arrives (Minecraft answers packets in
order, replying to the unknown type-0 request with its id).

``RconPool`` keeps one authenticated connection per world and reuses it, so a
polling dashboard does not make the server log a connect/disconnect pair on
every poll.
"""

from __future__ import annotations

import contextlib
import itertools
import select
import socket
import struct
import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Protocol

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


class RconUnsentError(RconError):
    """The command certainly did not run: it never fully left this process.

    Raised when there is no connection, when writing the command packet
    fails, or when the server rejects the session as unauthenticated. Only
    this error is safe to retry on a new connection for a command that is not
    idempotent; any other ``RconError`` from ``command`` may come after the
    server received (and ran) the command.
    """


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

    def alive(self) -> bool:
        """Cheap pre-flight check that a reused connection is still usable.

        Between commands the server never sends anything, so a readable socket
        means the server closed it (EOF), reset it, or left stray bytes that
        would desynchronise the next reply. Any of these: not alive. A peer
        that vanished without a FIN (pod gone) still looks alive here; that
        case is caught by pod-change eviction or by the 5 s timeout.
        """
        if self._sock is None:
            return False
        try:
            readable, _, _ = select.select([self._sock], [], [], 0)
        except (OSError, ValueError):
            return False
        return not readable

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def command(self, cmd: str) -> str:
        if self._sock is None:
            raise RconUnsentError("not connected")
        if len(cmd.encode()) > MAX_COMMAND:
            raise RconError("command too long")
        req = next(self._ids)
        sentinel = next(self._ids)
        try:
            self._send(req, SERVERDATA_EXECCOMMAND, cmd)
        except RconError as e:
            # sendall failed, so the kernel did not take the whole command
            # packet. The server only runs a complete packet, and the caller
            # drops this connection, so a partial one is discarded unexecuted.
            raise RconUnsentError(str(e)) from e
        # From here on the command packet is with the kernel and may run: any
        # failure below (sentinel send, timeout, EOF) is a plain RconError.
        self._send(sentinel, SERVERDATA_RESPONSE_VALUE, "")
        parts: list[str] = []
        while True:
            rid, _, body = self._recv()
            if rid == sentinel:
                return "".join(parts)
            if rid == req:
                parts.append(body)
            elif rid == -1:
                # the server refused the command without running it
                raise RconUnsentError("RCON session not authenticated")

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


class RconApi(Protocol):
    """What ``RconPool`` needs from a connected client (``RconClient``)."""

    def command(self, cmd: str) -> str: ...
    def alive(self) -> bool: ...
    def close(self) -> None: ...


IDLE_TIMEOUT = 300.0


@dataclass
class _Slot:
    lock: threading.Lock = field(default_factory=threading.Lock)
    client: RconApi | None = None
    key: Hashable = None  # what the client was opened with (host, password)
    target: Hashable = None  # identity of the server pod last seen
    last_used: float = 0.0
    evict_pending: bool = False


class RconPool:
    """One long-lived RCON connection per world.

    Every world has its own lock, held for a whole ``run``: commands on one
    connection never interleave, and a hung server (5 s socket timeout) only
    blocks callers for the same world. There is no global lock around I/O;
    ``_mu`` only guards the slot dict. Eviction and the idle sweep never wait
    for a world's lock: if a command is in flight they mark the slot and the
    next ``run`` drops the connection.

    Retry rules (see ``run``): a failed command drops the connection. It is
    retried once on a fresh connection only if the failure proves the command
    did not run (``RconUnsentError``), or the caller says it is idempotent
    (``retry_sent``, e.g. ``list``). A reused connection that fails the
    ``alive()`` pre-flight check is replaced before anything is sent. A fresh
    connect or authentication failure is raised at once, as before pooling.
    """

    def __init__(
        self,
        idle_timeout: float = IDLE_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.idle_timeout = idle_timeout
        self.clock = clock
        self._mu = threading.Lock()
        self._slots: dict[str, _Slot] = {}

    def _slot(self, world: str) -> _Slot:
        # Slots are never removed, so a slot fetched here stays the world's
        # only slot (removing one could let two threads use two sockets).
        with self._mu:
            slot = self._slots.get(world)
            if slot is None:
                slot = self._slots[world] = _Slot()
            return slot

    @staticmethod
    def _drop(slot: _Slot) -> None:
        client, slot.client, slot.key = slot.client, None, None
        slot.evict_pending = False
        if client is not None:
            with contextlib.suppress(Exception):  # closing a dead socket
                client.close()

    def connected(self, world: str) -> bool:
        with self._mu:
            slot = self._slots.get(world)
        return slot is not None and slot.client is not None

    def run(
        self,
        world: str,
        key: Hashable,
        connect: Callable[[], RconApi],
        cmds: list[str],
        *,
        retry_sent: bool = False,
    ) -> list[str]:
        """Run ``cmds`` in order on ``world``'s connection; one reply each.

        ``key`` identifies the endpoint and credentials (host, password); a
        cached connection made with a different key is replaced. ``connect``
        returns a connected, authenticated client.

        Failure handling, per command:

        * reused connection fails ``alive()`` -> replace it; nothing was sent.
        * ``connect`` fails -> raise. Nothing was sent, but this is already a
          fresh attempt (or the reconnect after a failure), so retrying again
          would only double the wait on a down server.
        * ``RconUnsentError`` on a reused connection -> drop, reconnect, retry
          once. The command did not run, so running it now is not a repeat.
        * any other error after the command packet was written (timeout, EOF,
          sentinel send failed) -> drop and raise: the server may have run it
          (console input, ``whitelist add``, ``save-off``...). Only with
          ``retry_sent=True`` (read-only commands such as ``list``) is it
          retried once.

        Commands of a batch that already ran are never re-sent; a retry
        resumes at the failed command. At most one retry per call.
        """
        slot = self._slot(world)
        with slot.lock:
            if slot.evict_pending or (slot.client is not None and slot.key != key):
                self._drop(slot)
            out: list[str] = []
            retried = False
            i = 0
            while i < len(cmds):
                client = slot.client
                reused = client is not None
                if client is not None and not client.alive():
                    self._drop(slot)
                    client, reused = None, False
                if client is None:
                    client = connect()  # raises: nothing sent, slot stays empty
                    slot.client, slot.key = client, key
                try:
                    out.append(client.command(cmds[i]))
                except Exception as e:
                    self._drop(slot)  # its reply stream may be out of step
                    unsent = isinstance(e, RconUnsentError)
                    rcon_err = isinstance(e, (RconError, OSError))
                    if reused and not retried and (unsent or (retry_sent and rcon_err)):
                        retried = True
                        continue
                    raise
                slot.last_used = self.clock()
                i += 1
            return out

    def note_target(self, world: str, target: Hashable) -> None:
        """Record the identity of ``world``'s server pod (None: no server).

        A different pod (name, container start time or restart count) means
        the old connection points at a server that is gone: evict it.
        """
        slot = self._slot(world)
        if target is None or (slot.target is not None and slot.target != target):
            self.evict(world)
        slot.target = target

    def evict(self, world: str) -> None:
        """Close ``world``'s connection now, or after its in-flight command."""
        with self._mu:
            slot = self._slots.get(world)
        if slot is None:
            return
        if slot.lock.acquire(blocking=False):
            try:
                self._drop(slot)
            finally:
                slot.lock.release()
        else:  # in use: the next run (or sweep) drops it
            slot.evict_pending = True

    def sweep_idle(self) -> list[str]:
        """Close connections unused for ``idle_timeout``; returns the worlds.

        Never blocks: a world whose lock is held is in use, so not idle.
        """
        now = self.clock()
        with self._mu:
            slots = list(self._slots.items())
        closed: list[str] = []
        for world, slot in slots:
            if not slot.lock.acquire(blocking=False):
                continue
            try:
                if slot.client is not None and (
                    slot.evict_pending or now - slot.last_used >= self.idle_timeout
                ):
                    self._drop(slot)
                    closed.append(world)
            finally:
                slot.lock.release()
        return closed

    def close_all(self) -> None:
        with self._mu:
            worlds = list(self._slots)
        for w in worlds:
            self.evict(w)


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
