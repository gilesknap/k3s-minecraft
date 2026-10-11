import socket
import struct
import threading

import pytest

from mcdash.rcon import RconAuthError, RconClient, RconError, parse_list, rcon_command


def _recv(conn: socket.socket) -> tuple[int, int, str] | None:
    hdr = conn.recv(4)
    if len(hdr) < 4:
        return None
    (n,) = struct.unpack("<i", hdr)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    rid, t = struct.unpack("<ii", data[:8])
    return rid, t, data[8:-2].decode()


def _send(conn: socket.socket, rid: int, t: int, body: str) -> None:
    payload = struct.pack("<ii", rid, t) + body.encode() + b"\0\0"
    conn.sendall(struct.pack("<i", len(payload)) + payload)


def serve(password: str, big: str = "") -> tuple[int, threading.Thread]:
    """Minimal Minecraft-like RCON server, one connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run() -> None:
        conn, _ = srv.accept()
        with conn:
            while pkt := _recv(conn):
                rid, t, body = pkt
                if t == 3:
                    _send(conn, rid if body == password else -1, 2, "")
                elif t == 2:
                    out = big if body == "big" else f"echo {body}"
                    for i in range(0, max(len(out), 1), 4096):
                        _send(conn, rid, 0, out[i : i + 4096])
                else:
                    _send(conn, rid, 0, f"Unknown request {t:x}")
        srv.close()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return port, th


def test_command_roundtrip() -> None:
    port, _ = serve("pw")
    assert rcon_command("127.0.0.1", port, "pw", "list") == "echo list"


def test_multi_packet() -> None:
    big = "x" * 10000
    port, _ = serve("pw", big)
    with RconClient("127.0.0.1", port, "pw") as c:
        assert c.command("big") == big
        assert c.command("say hi") == "echo say hi"


def test_bad_password() -> None:
    port, _ = serve("pw")
    with pytest.raises(RconAuthError):
        rcon_command("127.0.0.1", port, "nope", "list")


def test_connect_refused() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(RconError):
        rcon_command("127.0.0.1", port, "pw", "list", timeout=1)


def test_timeout() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    with pytest.raises(RconError, match="timed out"):
        rcon_command("127.0.0.1", port, "pw", "list", timeout=0.3)
    srv.close()


def test_parse_list() -> None:
    assert parse_list("There are 2 of a max of 20 players online: noah, giles") == [
        "noah",
        "giles",
    ]
    assert parse_list("There are 0 of a max of 20 players online: ") == []
    assert parse_list("garbage") == []
