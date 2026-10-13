"""The per-world RCON connection pool, alone and inside the service."""

from __future__ import annotations

import socket
import threading
import time
from datetime import UTC, datetime

import pytest

from mcdash.k8s import PodInfo
from mcdash.models import MCDashError
from mcdash.rcon import RconClient, RconError, RconPool
from mcdash.service import KubeDashboardService

from .helpers import FakeRcon
from .test_rcon import _recv, _send
from .test_service import FIRE, ME, complete, launch_running, parts


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


# ------------------------------------------------------------ service level


def test_connection_reused_across_calls(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    for i in range(3):
        assert svc.rcon(name, f"say {i}", ME) == f"ok say {i}"
        assert svc.get_world(name).players_online == ["noah", "giles"]
    assert rcon.connects == 1 and len(rcon.open_conns) == 1


def test_reconnect_after_server_close(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    rcon.drop_all()  # e.g. the server restarted in place
    rcon.log.clear()
    assert svc.rcon(name, "say hi", ME) == "ok say hi"
    assert [c for _, c in rcon.log] == ["say hi"]
    assert rcon.connects == 2 and len(rcon.open_conns) == 1


def test_unsent_command_is_retried_on_a_new_connection(
    svc: KubeDashboardService,
) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    rcon.log.clear()
    rcon.fail_unsent = 1
    assert svc.rcon(name, "whitelist add bob", ME) == "ok whitelist add bob"
    assert [c for _, c in rcon.log] == ["whitelist add bob"]
    assert rcon.connects == 2


def test_no_retry_after_a_completed_send(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    rcon.log.clear()
    rcon.fail_sent = 1
    with pytest.raises(MCDashError, match="RCON connection closed"):
        svc.rcon(name, "give noah diamond 64", ME)
    # sent once only, and the audit still records the attempt
    assert [c for _, c in rcon.log] == ["give noah diamond 64"]
    assert svc.store.read_audit()[-1]["command"] == "give noah diamond 64"
    assert rcon.connects == 1 and not rcon.open_conns  # dropped
    assert svc.rcon(name, "say hi", ME) == "ok say hi"  # next call reconnects
    assert rcon.connects == 2


def test_list_is_retried_after_send(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    rcon.log.clear()
    rcon.fail_sent = 1
    assert svc.get_world(name).players_online == ["noah", "giles"]
    assert [c for _, c in rcon.log] == ["list", "list"]


def test_batch_resumes_without_resending(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    host = svc._rcon_host(name)
    # second command fails before sending: only it is retried
    calls = {"n": 0}
    orig = FakeRcon._Conn.command

    def flaky(self: FakeRcon._Conn, cmd: str) -> str:
        calls["n"] += 1
        if calls["n"] == 2:
            from mcdash.rcon import RconUnsentError

            raise RconUnsentError("broken pipe")
        return orig(self, cmd)

    rcon.log.clear()
    FakeRcon._Conn.command = flaky  # type: ignore[method-assign]
    try:
        out = svc._rcon_cmds(name, ["save-off", "save-all flush"])
    finally:
        FakeRcon._Conn.command = orig  # type: ignore[method-assign]
    assert out == ["ok save-off", "ok save-all flush"]
    assert rcon.log == [(host, "save-off"), (host, "save-all flush")]


def test_connect_failure_is_not_retried(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    rcon.drop_all()
    rcon.fail = True
    n = rcon.connects
    with pytest.raises(MCDashError, match="connection refused"):
        svc.rcon(name, "say hi", ME)
    assert rcon.connects == n  # factory raised: one attempt, nothing sent
    assert svc.get_world(name).players_online is None


def test_password_change_reconnects(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    kube.secrets[f"{name}-rcon"]["rcon-password"] = "rotated"
    svc._secret_cache.clear()
    assert svc.rcon(name, "say hi", ME) == "ok say hi"
    assert rcon.connects == 2 and len(rcon.open_conns) == 1


def test_evicted_on_stop(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    svc.stop(name, ME)
    assert len(rcon.open_conns) == 1  # save-off used it
    complete(svc, "snapshot")  # save-on, then scale to 0
    assert "save-on" in [c for _, c in rcon.log]
    assert not rcon.open_conns
    assert not svc.rcon_pool.connected(name)


def test_evicted_on_delete(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    svc.delete(name, ME)
    complete(svc, "snapshot")
    assert not rcon.open_conns


def test_evicted_on_restore(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    svc.restore(name, FIRE, ME)
    complete(svc, "snapshot")
    assert not rcon.open_conns
    complete(svc, "seed")
    assert svc.get_world(name).players_online == ["noah", "giles"]
    assert len(rcon.open_conns) == 1


def test_evicted_on_pod_change(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    old = rcon.open_conns
    assert len(old) == 1
    # same pod, container restarted
    kube.pods[name] = [
        PodInfo(name=f"{name}-abc", phase="Running", ready=True, restarts=1)
    ]
    svc.get_world(name)
    assert old[0].closed and rcon.connects == 2
    # a replacement pod
    kube.pods[name] = [
        PodInfo(
            name=f"{name}-xyz",
            phase="Running",
            ready=True,
            started=datetime.now(UTC),
        )
    ]
    svc.rcon(name, "say hi", ME)  # the console checks the pod before sending
    assert rcon.connects == 3 and len(rcon.open_conns) == 1


def test_idle_sweep(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    clock = FakeClock()
    svc.rcon_pool.clock = clock
    name = launch_running(svc)
    svc.rcon(name, "say hi", ME)
    clock.t += 299
    svc.reconcile_once()
    assert len(rcon.open_conns) == 1
    clock.t += 2
    svc.reconcile_once()
    assert not rcon.open_conns
    assert svc.rcon(name, "say hi", ME) == "ok say hi"  # reconnects on demand
    assert rcon.connects == 2


def _two_worlds(svc: KubeDashboardService) -> tuple[str, str]:
    a = launch_running(svc)
    b = launch_running(svc, FIRE)
    assert a != b
    return a, b


def test_hung_world_does_not_block_others(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    a, b = _two_worlds(svc)
    gate = threading.Event()
    rcon.block[f"{a}-rcon."] = gate
    result: list[str] = []
    t = threading.Thread(target=lambda: result.append(svc.rcon(a, "say a", ME)))
    t.start()
    try:
        assert rcon.blocked.wait(2)  # world a's command holds its lock and hangs
        t0 = time.monotonic()
        assert svc.rcon(b, "say b", ME) == "ok say b"
        assert svc.get_world(b).players_online == ["noah", "giles"]
        # eviction and the sweep never wait for the hung world either
        svc.rcon_pool.evict(a)
        svc.rcon_pool.sweep_idle()
        assert time.monotonic() - t0 < 1.0
        assert t.is_alive()
    finally:
        gate.set()
        t.join(5)
    assert result == ["ok say a"]
    # the eviction requested mid-command applies before the next one
    svc.rcon(a, "say again", ME)
    assert sum(1 for c in rcon.conns if f"{a}-rcon." in c.host) == 2


def test_no_interleaving_under_concurrency(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    errors: list[str] = []

    def console(i: int) -> None:
        for j in range(15):
            cmd = f"say {i}-{j}"
            if svc.rcon(name, cmd, ME) != f"ok {cmd}":
                errors.append(cmd)

    def poll() -> None:
        for _ in range(15):
            if svc._players(name) != ["noah", "giles"]:
                errors.append("list")

    threads = [threading.Thread(target=console, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=poll) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    assert rcon.overlaps == 0
    assert rcon.connects == 1


# ------------------------------------------------------------ real sockets


class Server:
    """Minecraft-like RCON server accepting many connections.

    ``close_after`` commands on a connection, the server closes it (after
    replying); ``hangup_on`` makes it close on receiving that command, without
    replying, as a server dying mid-command would.
    """

    def __init__(self, close_after: int = 0, hangup_on: str = "") -> None:
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.close_after = close_after
        self.hangup_on = hangup_on
        self.connections = 0
        self.commands: list[str] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        n = 0
        with conn:
            while pkt := _recv(conn):
                rid, t, body = pkt
                if t == 3:
                    _send(conn, rid, 2, "")
                elif t == 2:
                    self.commands.append(body)
                    if body == self.hangup_on:
                        return
                    _send(conn, rid, 0, f"echo {body}")
                    n += 1
                else:
                    _send(conn, rid, 0, f"Unknown request {t:x}")
                    if self.close_after and n >= self.close_after:
                        return

    def close(self) -> None:
        self.srv.close()


def _pool_run(pool: RconPool, srv: Server, cmd: str, **kw: bool) -> list[str]:
    def connect() -> RconClient:
        c = RconClient("127.0.0.1", srv.port, "pw", timeout=1.0)
        c.connect()
        return c

    return pool.run("w", ("127.0.0.1", "pw"), connect, [cmd], **kw)


def _wait(pred: object, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if callable(pred) and pred():
            return
        time.sleep(0.01)


def test_alive_detects_server_close() -> None:
    srv = Server(close_after=1)
    try:
        c = RconClient("127.0.0.1", srv.port, "pw", timeout=1.0)
        c.connect()
        assert c.alive()
        assert c.command("say hi") == "echo say hi"
        _wait(lambda: not c.alive())
        assert not c.alive()
        c.close()
        assert not c.alive()
    finally:
        srv.close()


def test_pool_reuses_and_reconnects_over_tcp() -> None:
    srv = Server(close_after=2)
    pool = RconPool()
    try:
        assert _pool_run(pool, srv, "a") == ["echo a"]
        assert _pool_run(pool, srv, "b") == ["echo b"]
        assert srv.connections == 1
        time.sleep(0.1)  # server has now closed the connection
        assert _pool_run(pool, srv, "c") == ["echo c"]
        assert srv.connections == 2
        assert srv.commands == ["a", "b", "c"]
    finally:
        pool.close_all()
        srv.close()


def test_pool_does_not_resend_after_hangup_over_tcp() -> None:
    srv = Server(hangup_on="op someone")
    pool = RconPool()
    try:
        assert _pool_run(pool, srv, "list") == ["echo list"]
        with pytest.raises(RconError):
            _pool_run(pool, srv, "op someone")
        assert srv.commands == ["list", "op someone"]  # received exactly once
        assert not pool.connected("w")
    finally:
        pool.close_all()
        srv.close()


def test_pool_retries_idempotent_after_hangup_over_tcp() -> None:
    srv = Server(hangup_on="list")
    pool = RconPool()
    try:
        assert _pool_run(pool, srv, "say hi") == ["echo say hi"]
        # the retry hangs up too: raised after exactly one retry
        with pytest.raises(RconError):
            _pool_run(pool, srv, "list", retry_sent=True)
        assert srv.commands == ["say hi", "list", "list"]
    finally:
        pool.close_all()
        srv.close()


def test_too_long_command_keeps_the_connection(svc: KubeDashboardService) -> None:
    _, _, rcon = parts(svc)
    name = launch_running(svc)
    svc.rcon(name, "say hi", ME)
    opened = rcon.connects
    with pytest.raises(MCDashError, match="too long"):
        svc.rcon(name, "say " + "x" * 2000, ME)
    svc.rcon(name, "say again", ME)
    assert rcon.connects == opened  # rejected before sending; no reconnect
