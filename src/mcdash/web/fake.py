"""In-memory ``DashboardService`` for tests and the dev server.

Not used in production. Records every call in ``calls`` so tests can assert
which service method was invoked with which actor.
"""

from __future__ import annotations

import functools
import itertools
import json
import random
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcdash.completion import CommandTree
from mcdash.models import (
    Backup,
    Completion,
    Conflict,
    Forbidden,
    MCDashError,
    NotFound,
    Player,
    ServerType,
    Tier,
    World,
    WorldSettings,
    WorldState,
)

PUBLIC_HOST = "mc.gkcluster.org"
# An extract of the 26.3 data generator's commands.json and registries.json.
SAMPLE_COMMANDS = Path(__file__).with_name("sample_commands.json")


@functools.cache
def sample_tree() -> CommandTree:
    raw = json.loads(SAMPLE_COMMANDS.read_text())
    return CommandTree(raw["commands"], raw["registries"])


PORT_MIN, PORT_MAX = 30565, 30584
RCON_PORT_MIN, RCON_PORT_MAX = 30600, 30619
RCON_HOST = "192.168.1.92"


class FakeDashboardService:
    """Implements ``mcdash.models.DashboardService`` in memory.

    ``log_lines``: lines yielded by ``stream_logs``. ``live_logs=True`` makes the
    stream follow forever with a delay (dev server); otherwise it is finite.
    ``simulate=True`` advances transitional states over time (dev server).
    """

    def __init__(
        self,
        *,
        log_lines: list[str] | None = None,
        live_logs: bool = False,
        simulate: bool = False,
    ) -> None:
        self.backups: dict[str, Backup] = {}
        self.worlds: dict[str, World] = {}
        self.players: list[Player] = []
        self.calls: list[tuple] = []
        self.log_lines = log_lines or [
            "[Server thread/INFO]: Starting minecraft server version 1.21.4",
            '[Server thread/INFO]: Done (3.2s)! For help, type "help"',
        ]
        self.live_logs = live_logs
        self.simulate = simulate
        self._pending: dict[str, tuple[float, WorldState, str]] = {}

    # -- helpers -----------------------------------------------------------
    def _record(self, *call: object) -> None:
        self.calls.append(tuple(call))

    def _tick(self) -> None:
        if not self.simulate:
            return
        now = time.monotonic()
        for name, (when, state, msg) in list(self._pending.items()):
            if now >= when and name in self.worlds:
                w = self.worlds[name]
                w.state, w.message = state, msg
                del self._pending[name]
                if state is WorldState.STARTING:
                    self._later(name, 4, WorldState.RUNNING)
                if state is WorldState.RUNNING:
                    w.players_online, w.uptime_seconds = [], 1
                    w.cpu_millicores, w.memory_bytes = 900, 1_800_000_000

    def _later(self, name: str, secs: float, state: WorldState, msg: str = "") -> None:
        self._pending[name] = (time.monotonic() + secs, state, msg)

    def _world(self, name: str) -> World:
        self._tick()
        try:
            return self.worlds[name]
        except KeyError:
            raise NotFound(f"No world called {name!r}") from None

    def _take_snapshot(self, name: str) -> Backup:
        now = datetime.now(UTC)
        bid = f"snapshot/{name}/{now:%Y%m%dT%H%M%S}{len(self.backups):03d}Z.zip"
        src = self.worlds[name].settings
        b = Backup(
            id=bid,
            tier=Tier.SNAPSHOT,
            world_name=name,
            timestamp=now,
            size_bytes=random.randint(40, 400) * 1_000_000,
            server_type=src.server_type,
            mc_version=src.version,
        )
        self.backups[bid] = b
        self.worlds[name].last_snapshot = now
        return b

    # -- backups -----------------------------------------------------------
    def list_backups(self) -> list[Backup]:
        return list(self.backups.values())

    def get_backup(self, backup_id: str) -> Backup:
        try:
            return self.backups[backup_id]
        except KeyError:
            raise NotFound(f"No backup {backup_id!r}") from None

    def set_backup_label(self, backup_id: str, label: str, description: str) -> None:
        self._record("set_backup_label", backup_id, label, description)
        b = self.get_backup(backup_id)
        if b.tier is Tier.SNAPSHOT:
            raise Forbidden("Snapshots cannot be labelled; promote them instead")
        self.backups[backup_id] = replace(b, label=label, description=description)

    def promote(
        self, snapshot_id: str, label: str, description: str, actor: str
    ) -> Backup:
        self._record("promote", snapshot_id, label, description, actor)
        b = self.get_backup(snapshot_id)
        if b.tier is not Tier.SNAPSHOT:
            raise Conflict("Only snapshots can be promoted")
        if not label.strip():
            raise MCDashError("A name is required to promote a snapshot")
        bid = f"backup/{label}.zip"
        if bid in self.backups:
            raise Conflict(f"A backup called {label!r} already exists")
        nb = replace(b, id=bid, tier=Tier.BACKUP, label=label, description=description)
        self.backups[bid] = nb
        return nb

    def delete_backup(self, backup_id: str, actor: str) -> None:
        self._record("delete_backup", backup_id, actor)
        b = self.get_backup(backup_id)
        if b.tier is Tier.ARCHIVE:
            raise Forbidden("Archive backups are read-only and cannot be deleted")
        del self.backups[backup_id]

    # -- worlds ------------------------------------------------------------
    def launch_defaults(self, backup_id: str) -> tuple[str, WorldSettings]:
        b = self.get_backup(backup_id)
        p = b.properties
        name = b.world_name
        n = 2
        while name in self.worlds:
            name = f"{b.world_name}-{n}"
            n += 1
        forge = b.server_type is ServerType.FORGE
        return name, WorldSettings(
            version=(b.mc_version or "LATEST") if forge else "LATEST",
            server_type=b.server_type,
            memory="6Gi" if forge else "4Gi",
            gamemode=p.get("gamemode", "survival"),
            force_gamemode=p.get("force-gamemode") == "true",
            difficulty=p.get("difficulty", "easy"),
            pvp=p.get("pvp") == "true",
            enable_command_block=p.get("enable-command-block") == "true",
            motd=p.get("motd", ""),
            max_world_size=int(p.get("max-world-size", 29999984)),
            whitelist=[],
        )

    def launch(
        self, backup_id: str, name: str, settings: WorldSettings, actor: str
    ) -> World:
        self._record("launch", backup_id, name, settings, actor)
        self.get_backup(backup_id)
        if name in self.worlds:
            raise Conflict(f"A world called {name!r} already exists")
        port = self._free_port()
        w = World(
            name=name,
            port=port,
            rcon_port=self._free_rcon_port(),
            node="nuc2",
            state=WorldState.SEEDING,
            settings=settings,
            source_backup_id=backup_id,
            created=datetime.now(UTC),
            connect_address=f"{PUBLIC_HOST}:{port}",
            message="Seed job running",
        )
        self.worlds[name] = w
        self._later(name, 5, WorldState.STARTING)
        return w

    def _free_port(self) -> int:
        used = {w.port for w in self.worlds.values()}
        port = next((p for p in range(PORT_MIN, PORT_MAX + 1) if p not in used), None)
        if port is None:
            raise Conflict("No free ports left in the range")
        return port

    def _free_rcon_port(self) -> int:
        used = {w.rcon_port for w in self.worlds.values()}
        port = next(
            (p for p in range(RCON_PORT_MIN, RCON_PORT_MAX + 1) if p not in used), None
        )
        if port is None:
            raise Conflict("No free RCON ports left in the range")
        return port

    def new_world_defaults(self) -> tuple[str, WorldSettings]:
        name, n = "new-world", 2
        while name in self.worlds:
            name = f"new-world-{n}"
            n += 1
        return name, WorldSettings()

    def create_world(self, name: str, settings: WorldSettings, actor: str) -> World:
        self._record("create_world", name, settings, actor)
        if name in self.worlds:
            raise Conflict(f"A world called {name!r} already exists")
        port = self._free_port()
        w = World(
            name=name,
            port=port,
            rcon_port=self._free_rcon_port(),
            rcon_host=RCON_HOST,
            node="nuc2",
            state=WorldState.STARTING,
            settings=settings,
            source_backup_id="",
            created=datetime.now(UTC),
            connect_address=f"{PUBLIC_HOST}:{port}",
            message="Generating a new world",
        )
        self.worlds[name] = w
        self._later(name, 6, WorldState.RUNNING)
        return w

    def list_worlds(self) -> list[World]:
        self._tick()
        return list(self.worlds.values())

    def get_world(self, name: str) -> World:
        return self._world(name)

    def rcon_password(self, name: str, actor: str) -> str:
        self._record("rcon_password", name, actor)
        self._world(name)
        return f"pw-{name}-s3cret"

    def start(self, name: str, actor: str) -> None:
        self._record("start", name, actor)
        w = self._world(name)
        if w.state is not WorldState.STOPPED and w.state is not WorldState.FAILED:
            raise Conflict(f"{name} is {w.state}, not stopped")
        w.state, w.message = WorldState.STARTING, ""
        self._later(name, 4, WorldState.RUNNING)

    def stop(self, name: str, actor: str) -> None:
        self._record("stop", name, actor)
        w = self._world(name)
        if w.state is WorldState.STOPPED:
            raise Conflict(f"{name} is already stopped")
        self._take_snapshot(name)
        w.state, w.message = WorldState.SNAPSHOTTING, "Snapshot before stop"
        w.players_online = w.uptime_seconds = None
        w.cpu_millicores = w.memory_bytes = None
        self._later(name, 4, WorldState.STOPPED)

    def snapshot(self, name: str, actor: str) -> None:
        self._record("snapshot", name, actor)
        w = self._world(name)
        if w.state is WorldState.SEEDING:
            raise Conflict(f"{name} is still seeding")
        prev = w.state
        self._take_snapshot(name)
        w.state, w.message = WorldState.SNAPSHOTTING, "Snapshot job running"
        self._later(name, 3, prev)

    def restore(self, name: str, backup_id: str, actor: str) -> None:
        self._record("restore", name, backup_id, actor)
        w = self._world(name)
        self.get_backup(backup_id)
        self._take_snapshot(name)
        w.source_backup_id = backup_id
        w.state, w.message = WorldState.SEEDING, f"Restoring from {backup_id}"
        self._later(name, 5, WorldState.STARTING)

    def delete(self, name: str, actor: str) -> None:
        self._record("delete", name, actor)
        self._world(name)
        self._take_snapshot(name)
        del self.worlds[name]
        self._pending.pop(name, None)

    def list_snapshots(self, name: str) -> list[Backup]:
        return [
            b
            for b in self.backups.values()
            if b.tier is Tier.SNAPSHOT and b.world_name == name
        ]

    def update_settings(self, name: str, settings: WorldSettings, actor: str) -> None:
        self._record("update_settings", name, settings, actor)
        self._world(name).settings = settings

    def set_whitelist(
        self,
        name: str,
        players: list[str],
        actor: str,
        *,
        open_to_all: bool,
        ops: list[str] | None = None,
    ) -> None:
        self._record("set_whitelist", name, players, actor, open_to_all, ops)
        settings = self._world(name).settings
        settings.whitelist = list(players)
        settings.open_to_all = open_to_all
        if ops is not None:
            settings.ops = list(ops)

    # -- live --------------------------------------------------------------
    def rcon(self, name: str, command: str, actor: str) -> str:
        self._record("rcon", name, command, actor)
        w = self._world(name)
        if w.state is not WorldState.RUNNING:
            raise Conflict(f"{name} is not running")
        cmd = command.strip()
        if cmd == "list":
            ps = w.players_online or []
            return f"There are {len(ps)} of a max of 20 players online: {', '.join(ps)}"
        if cmd.startswith("say "):
            return ""
        if cmd == "time query daytime":
            return "The time is 6000"
        return f"Unknown or incomplete command, see below for error\n{cmd}<--[HERE]"

    def complete(self, name: str, line: str) -> Completion:
        w = self._world(name)
        if w.settings.server_type is not ServerType.VANILLA:
            return Completion(len(line), [], "none", "modded server")
        start, items = sample_tree().suggest(line, w.players_online or [])
        return Completion(start, items)

    def stream_logs(self, name: str) -> Iterator[str]:
        self._world(name)
        if not self.live_logs:
            yield from self.log_lines
            return
        yield from self.log_lines
        chatter = itertools.cycle(
            [
                "[Server thread/INFO]: Noah joined the game",
                "[Server thread/INFO]: <Noah> dad come to the nether portal",
                "[Server thread/INFO]: Saving the game (this may take a moment!)",
                "[Server thread/INFO]: Saved the game",
                "[Server thread/INFO]: GilesDad has made the advancement [Diamonds!]",
                "[Server thread/WARN]: Can't keep up! Is the server overloaded?",
            ]
        )
        for line in chatter:
            time.sleep(2)
            yield f"[{datetime.now():%H:%M:%S}] {line}"

    # -- roster ------------------------------------------------------------
    def roster(self) -> list[Player]:
        return list(self.players)

    def set_roster(self, players: list[Player]) -> None:
        self._record("set_roster", players)
        self.players = list(players)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def seeded(**kwargs: object) -> FakeDashboardService:
    """A fake populated with realistic worlds, backups and players."""
    svc = FakeDashboardService(**kwargs)  # type: ignore[arg-type]
    V, F = ServerType.VANILLA, ServerType.FORGE
    archive = [
        ("2023-02-25_16_53_58-noah-james.zip", "noah-james", V, "1.19.3", 812, {}),
        ("2022-08-15_22_56_49-noah-james.zip", "noah-james", V, "1.19.2", 640, {}),
        (
            "2022-02-04+21.14.00-the-lockdown-krew.zip",
            "the-lockdown-krew",
            V,
            "1.18.1",
            1530,
            {"pvp": "true", "difficulty": "normal"},
        ),
        (
            "2021-05-15-21_56_15-transformers-again.zip",
            "transformers-again",
            V,
            "1.16.5",
            420,
            {"gamemode": "creative"},
        ),
        (
            "22-01-21.21.50.00-science-lab.zip",
            "science-lab",
            V,
            "1.18.1",
            95,
            {"gamemode": "creative", "enable-command-block": "true"},
        ),
        (
            "2022-03-12_10_01_44-king-ghidorah.zip",
            "king-ghidorah",
            V,
            "1.18.2",
            210,
            {},
        ),
        (
            "2021-11-20_19_30_02-fire-ice2.zip",
            "fire-ice2",
            F,
            "1.16.5",
            2350,
            {"difficulty": "hard", "motd": "Fire and Ice (modded)"},
        ),
        (
            "2021-11-20_19_30_02-fire-ice2(1).zip",
            "fire-ice2",
            F,
            "1.16.5",
            2349,
            {"difficulty": "hard"},
        ),
        (
            "2022-06-01_08_00_00-python-noah-dad.zip",
            "python-noah-dad",
            V,
            "1.18.2",
            77,
            {},
        ),
        (
            "2021-08-09_14_22_10-adventure-of-doom.zip",
            "adventure-of-doom",
            V,
            "1.17.1",
            160,
            {
                "gamemode": "adventure",
                "force-gamemode": "true",
                "enable-command-block": "true",
                "motd": "Adventure of DOOM",
            },
        ),
        ("2022-12-30_20_10_10-skorponok.zip", "skorponok", V, "1.19.3", 330, {}),
    ]
    labels = {
        "2023-02-25_16_53_58-noah-james.zip": (
            "Final survival",
            "Last save before the 1.20 move; includes the sky castle.",
        ),
        "2021-08-09_14_22_10-adventure-of-doom.zip": (
            "Original map",
            "Needs command blocks enabled.",
        ),
    }
    for fname, world, typ, ver, mb, props in archive:
        stamp = fname.split("-" + world)[0]
        try:
            ts = datetime.strptime(stamp, "%Y-%m-%d_%H_%M_%S").replace(tzinfo=UTC)
        except ValueError:
            ts = None
        label, desc = labels.get(fname, ("", ""))
        bid = f"archive/{fname}"
        svc.backups[bid] = Backup(
            id=bid,
            tier=Tier.ARCHIVE,
            world_name=world,
            timestamp=ts or _dt("2022-01-21T21:50:00"),
            size_bytes=mb * 1_000_000,
            server_type=typ,
            mc_version=ver,
            properties=props,
            label=label,
            description=desc,
        )
    svc.backups["backup/noah-james-end-portal.zip"] = Backup(
        id="backup/noah-james-end-portal.zip",
        tier=Tier.BACKUP,
        world_name="noah-james",
        timestamp=_dt("2026-10-04T18:12:00"),
        size_bytes=845_000_000,
        server_type=V,
        mc_version="1.21.4",
        label="End portal found",
        description="Just after we found the stronghold.",
    )
    for world, stamps in {
        "noah-james": ["2026-10-11T09:00:00", "2026-10-10T21:30:00"],
        "fire-ice2": ["2026-10-09T17:45:00"],
        "king-ghidorah": ["2026-10-08T12:00:00"],
    }.items():
        for s in stamps:
            ts = _dt(s)
            bid = f"snapshot/{world}/{ts:%Y%m%dT%H%M%SZ}.zip"
            svc.backups[bid] = Backup(
                id=bid,
                tier=Tier.SNAPSHOT,
                world_name=world,
                timestamp=ts,
                size_bytes=random.Random(s).randint(200, 900) * 1_000_000,
                server_type=F if world == "fire-ice2" else V,
                mc_version="1.16.5" if world == "fire-ice2" else "1.21.4",
            )
    now = datetime.now(UTC)
    svc.worlds = {
        "noah-james": World(
            name="noah-james",
            port=30565,
            rcon_port=30600,
            rcon_host=RCON_HOST,
            node="nuc2",
            state=WorldState.RUNNING,
            settings=WorldSettings(
                motd="Noah & James survival",
                difficulty="normal",
                whitelist=["NoahK", "JamesK", "GilesDad"],
                ops=["GilesDad"],
            ),
            source_backup_id="archive/2023-02-25_16_53_58-noah-james.zip",
            created=now - timedelta(days=6),
            connect_address=f"{PUBLIC_HOST}:30565",
            players_online=["NoahK", "GilesDad"],
            uptime_seconds=3 * 3600 + 17 * 60,
            cpu_millicores=1240,
            memory_bytes=3_100_000_000,
            last_snapshot=_dt("2026-10-11T09:00:00"),
        ),
        "fire-ice2": World(
            name="fire-ice2",
            port=30566,
            rcon_port=30601,
            node="nuc2",
            state=WorldState.STOPPED,
            settings=WorldSettings(
                version="1.16.5",
                server_type=F,
                memory="6Gi",
                difficulty="hard",
                motd="Fire and Ice (modded)",
                whitelist=["NoahK", "GilesDad"],
            ),
            source_backup_id="archive/2021-11-20_19_30_02-fire-ice2.zip",
            created=now - timedelta(days=3),
            connect_address=f"{PUBLIC_HOST}:30566",
            last_snapshot=_dt("2026-10-09T17:45:00"),
        ),
        "king-ghidorah": World(
            name="king-ghidorah",
            port=30567,
            state=WorldState.FAILED,
            settings=WorldSettings(whitelist=["NoahK"]),
            source_backup_id="archive/2022-03-12_10_01_44-king-ghidorah.zip",
            created=now - timedelta(days=4),
            connect_address=f"{PUBLIC_HOST}:30567",
            last_snapshot=_dt("2026-10-08T12:00:00"),
            message="Pod crash-looping: java.lang.OutOfMemoryError: Java heap space",
        ),
        "adventure-of-doom": World(
            name="adventure-of-doom",
            port=30568,
            rcon_port=30602,
            node="nuc2",
            state=WorldState.SEEDING,
            settings=WorldSettings(
                gamemode="adventure",
                force_gamemode=True,
                enable_command_block=True,
                motd="Adventure of DOOM",
            ),
            source_backup_id="archive/2021-08-09_14_22_10-adventure-of-doom.zip",
            created=now - timedelta(minutes=1),
            connect_address=f"{PUBLIC_HOST}:30568",
            message="Seed job running (extracting world/)",
        ),
    }
    if svc.simulate:
        svc._later("adventure-of-doom", 20, WorldState.STARTING)
    svc.players = [
        Player("NoahK", "Noah"),
        Player("JamesK", "James (cousin)"),
        Player("GilesDad", "Dad"),
        Player("MillieM", "Millie"),
    ]
    return svc
