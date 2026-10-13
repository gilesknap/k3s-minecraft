"""Synthetic world zips and an in-memory fake cluster for backend tests."""

from __future__ import annotations

import gzip
import io
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nbtlib import Compound, File, String

from mcdash.k8s import JobInfo, JobSpec, PodInfo
from mcdash.models import Conflict, NotFound


def level_dat(version: str | None = "1.16.5") -> bytes:
    data = Compound({"LevelName": String("world")})
    if version:
        data["Version"] = Compound({"Name": String(version)})
    buf = io.BytesIO()
    File({"Data": data}).write(buf)
    return gzip.compress(buf.getvalue())


def make_world_zip(
    path: Path,
    *,
    prefix: str = "tmp/tmp.w92y4CF76G/",
    version: str | None = "1.16.5",
    forge: bool = False,
    properties: str = "gamemode=creative\ndifficulty=2\nenable-command-block=true\n",
    extra: dict[str, bytes] | None = None,
    versions_dirs: tuple[str, ...] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(prefix + "world/", b"")
        zf.writestr(prefix + "world/level.dat", level_dat(version))
        zf.writestr(prefix + "world/region/r.0.0.mca", b"\0" * 64)
        zf.writestr(prefix + "server.properties", properties)
        zf.writestr(prefix + "ops.json", b"[]")
        zf.writestr(prefix + "whitelist.json", b"[]")
        zf.writestr(prefix + "banned-players.json", b"[]")
        zf.writestr(prefix + "eula.txt", b"eula=true")
        zf.writestr(prefix + "logs/latest.log", b"log")
        zf.writestr(prefix + "libraries/com/x.jar", b"jar")
        zf.writestr(prefix + "?/.cache/junk", b"junk")
        for v in versions_dirs:
            zf.writestr(prefix + f"versions/{v}/{v}.jar", b"jar")
        if forge:
            zf.writestr(prefix + "mods/iceandfire-2.1.6.jar", b"jar")
            zf.writestr(prefix + "forge-1.16.5-36.1.0.jar", b"jar")
            zf.writestr(prefix + ".forge-installed-1.16.5-36.1.0", b"")
            zf.writestr(prefix + "config/mod.toml", b"x=1")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return path


# ------------------------------------------------------------------ fakes


@dataclass
class FakeJob:
    spec: JobSpec
    state: str = "active"  # active | succeeded | failed
    created: datetime = field(default_factory=lambda: datetime.now(UTC))


class FakeKube:
    """In-memory stand-in for ``mcdash.k8s.Kube``."""

    def __init__(self) -> None:
        self.configmaps: dict[str, tuple[str, dict[str, str]]] = {}
        self.secrets: dict[str, dict[str, str]] = {}
        self.pvcs: set[str] = set()
        self.jobs: dict[str, FakeJob] = {}
        self.pods: dict[str, list[PodInfo]] = {}
        self.metrics: dict[str, tuple[int, int]] = {}
        self.logs: dict[str, list[str]] = {}

    # configmaps
    def get_configmap(self, name: str) -> dict[str, str] | None:
        cm = self.configmaps.get(name)
        return dict(cm[1]) if cm else None

    def list_configmaps(self, kind: str) -> dict[str, dict[str, str]]:
        return {n: dict(d) for n, (k, d) in self.configmaps.items() if k == kind}

    def put_configmap(
        self, name: str, world: str, kind: str, data: dict[str, str]
    ) -> None:
        self.configmaps[name] = (kind, dict(data))

    def delete_configmap(self, name: str) -> None:
        self.configmaps.pop(name, None)

    # secrets
    def create_secret(self, name: str, world: str, data: dict[str, str]) -> None:
        if name in self.secrets:
            raise Conflict(name)
        self.secrets[name] = dict(data)

    def get_secret(self, name: str) -> dict[str, str] | None:
        return self.secrets.get(name)

    def delete_secret(self, name: str) -> None:
        self.secrets.pop(name, None)

    # pvcs
    def create_pvc(self, name: str, world: str) -> None:
        if name in self.pvcs:
            raise Conflict(name)
        self.pvcs.add(name)

    def pvc_exists(self, name: str) -> bool:
        return name in self.pvcs

    def delete_pvc(self, name: str) -> None:
        self.pvcs.discard(name)

    # jobs
    def create_job(self, spec: JobSpec) -> None:
        if spec.name in self.jobs:
            raise Conflict(spec.name)
        self.jobs[spec.name] = FakeJob(spec)

    def _info(self, j: FakeJob) -> JobInfo:
        return JobInfo(
            name=j.spec.name,
            world=j.spec.world,
            kind=j.spec.kind,
            active=int(j.state == "active"),
            succeeded=int(j.state == "succeeded"),
            failed=int(j.state == "failed"),
            message="BackoffLimitExceeded" if j.state == "failed" else "",
            created=j.created,
        )

    def get_job(self, name: str) -> JobInfo | None:
        j = self.jobs.get(name)
        return self._info(j) if j else None

    def list_jobs(self, world: str) -> list[JobInfo]:
        return [self._info(j) for j in self.jobs.values() if j.spec.world == world]

    def delete_job(self, name: str) -> None:
        self.jobs.pop(name, None)

    # pods
    def list_server_pods(self, world: str) -> list[PodInfo]:
        return list(self.pods.get(world, []))

    def pod_metrics(self, world: str) -> tuple[int, int] | None:
        return self.metrics.get(world)

    def stream_pod_log(self, pod: str, tail_lines: int = 200) -> Iterator[str]:
        yield from self.logs.get(pod, [])

    # test helpers
    def active_jobs(self, kind: str | None = None) -> list[FakeJob]:
        return [
            j
            for j in self.jobs.values()
            if j.state == "active" and (kind is None or j.spec.kind == kind)
        ]

    def finish_jobs(self, kind: str | None = None, ok: bool = True) -> list[FakeJob]:
        done = self.active_jobs(kind)
        for j in done:
            j.state = "succeeded" if ok else "failed"
        return done


class FakeHelm:
    def __init__(self, kube: FakeKube) -> None:
        self.kube = kube
        self.releases: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail_next: str | None = None

    def upgrade_install(self, release: str, values: dict[str, Any]) -> None:
        self.calls.append(("upgrade", release))
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            from mcdash.helmops import HelmError

            raise HelmError(msg)
        self.releases[release] = values
        self.revisions[release] = self.revisions.get(release, 0) + 1
        # simulate the Deployment: replicas 1 -> ready pod; 0 -> no pods
        if values.get("replicaCount", 1):
            self.kube.pods[release] = [
                PodInfo(
                    name=f"{release}-abc",
                    phase="Running",
                    ready=True,
                    started=datetime.now(UTC),
                )
            ]
        else:
            self.kube.pods.pop(release, None)

    def uninstall(self, release: str) -> None:
        self.calls.append(("uninstall", release))
        self.releases.pop(release, None)
        self.kube.pods.pop(release, None)

    def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": n,
                "revision": str(self.revisions[n]),
                "chart": "minecraft-5.2.0",
                "status": "deployed",
            }
            for n in self.releases
        ]

    def get_values(self, release: str) -> dict[str, Any]:
        if release not in self.releases:
            raise NotFound(release)
        return self.releases[release]


class FakeRcon:
    """Records commands; shared across connections via ``log``.

    Test controls: ``connects`` counts connections opened; ``drop_all()``
    simulates the server closing every open connection (``alive()`` turns
    false); ``fail_unsent`` / ``fail_sent`` make the next command on an
    existing connection fail before / after the request was sent; ``block``
    (host substring -> Event) holds commands to that host until set.
    """

    def __init__(self) -> None:
        self.log: list[tuple[str, str]] = []
        self.players = ["noah", "giles"]
        self.fail = False
        self.connects = 0
        self.conns: list[FakeRcon._Conn] = []
        self.fail_unsent = 0
        self.fail_sent = 0
        self.block: dict[str, threading.Event] = {}
        self.in_flight: dict[int, int] = {}  # id(conn) -> concurrent commands
        self.overlaps = 0
        self._mu = threading.Lock()

    def factory(self, host: str, port: int, password: str) -> FakeRcon._Conn:
        if self.fail:
            from mcdash.rcon import RconError

            raise RconError("connection refused")
        self.connects += 1
        c = FakeRcon._Conn(self, host)
        self.conns.append(c)
        return c

    def drop_all(self) -> None:
        for c in self.conns:
            c.server_closed = True

    @property
    def open_conns(self) -> list[FakeRcon._Conn]:
        return [c for c in self.conns if not c.closed]

    class _Conn:
        def __init__(self, outer: FakeRcon, host: str) -> None:
            self.outer = outer
            self.host = host
            self.closed = False
            self.server_closed = False

        def alive(self) -> bool:
            return not (self.closed or self.server_closed)

        def command(self, cmd: str) -> str:
            from mcdash.rcon import RconError, RconUnsentError

            o = self.outer
            if self.closed:
                raise RconUnsentError("not connected")
            if o.fail_unsent:
                o.fail_unsent -= 1
                raise RconUnsentError("RCON send failed: broken pipe")
            with o._mu:
                n = o.in_flight.get(id(self), 0) + 1
                o.in_flight[id(self)] = n
                if n > 1:
                    o.overlaps += 1
            try:
                o.log.append((self.host, cmd))  # the server got it
                if o.fail_sent:
                    o.fail_sent -= 1
                    raise RconError("RCON connection closed")
                for key, evt in o.block.items():
                    if key in self.host:
                        evt.wait(5)
                time.sleep(0.001)  # widen any interleaving window
                if cmd == "list":
                    p = o.players
                    return (
                        f"There are {len(p)} of a max of 20 players online: "
                        + ", ".join(p)
                    )
                return f"ok {cmd}"
            finally:
                with o._mu:
                    o.in_flight[id(self)] -= 1

        def close(self) -> None:
            self.closed = True
