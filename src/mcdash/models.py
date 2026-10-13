"""Shared data model and service contract.

READ-ONLY CONTRACT: backend (``mcdash.service``) implements ``DashboardService``;
the web layer (``mcdash.web``) consumes it. Change only by agreement — see
docs/contracts.md.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class Tier(StrEnum):
    ARCHIVE = "archive"  # precious, read-only mount
    BACKUP = "backup"  # promoted, never pruned
    SNAPSHOT = "snapshot"  # rolling, last N per world


class ServerType(StrEnum):
    VANILLA = "VANILLA"
    FORGE = "FORGE"


class WorldState(StrEnum):
    SEEDING = "seeding"  # seed Job running
    STARTING = "starting"  # replicas=1, pod not Ready
    RUNNING = "running"
    SNAPSHOTTING = "snapshotting"  # snapshot Job running
    STOPPED = "stopped"  # replicas=0
    FAILED = "failed"  # seed/snapshot Job failed or pod crash-looping


@dataclass(frozen=True)
class Backup:
    """A launchable zip in any tier.

    ``id`` is ``"<tier>/<path relative to the tier root>"`` and is stable,
    e.g. ``archive/2023-02-25_16_53_58-noah-james.zip`` or
    ``snapshot/noah-james/20261011T120000Z.zip``.
    """

    id: str
    tier: Tier
    world_name: str  # parsed from the filename (or the snapshot dir)
    timestamp: datetime | None  # parsed from the filename, else mtime (UTC)
    size_bytes: int
    server_type: ServerType
    mc_version: str | None  # original version detected from level.dat
    properties: dict[str, str] = field(default_factory=dict)  # server.properties
    label: str = ""  # display label (dashboard metadata / promote sidecar)
    description: str = ""


LEVEL_TYPES = ("normal", "flat", "amplified", "large_biomes")

# Inclusive (min, max) for the integer server.properties settings. Upper bounds
# keep typed chart values below 1e6, which helm would render as floats.
SETTING_RANGES: dict[str, tuple[int, int]] = {
    "view_distance": (2, 32),
    "simulation_distance": (5, 32),
    "spawn_protection": (0, 100000),
    "pause_when_empty_seconds": (0, 86400),
    "max_players": (1, 10000),
}


# server.properties / itzg MODE spellings -> the dashboard's names
GAMEMODE_ALIASES = {
    "0": "survival",
    "s": "survival",
    "1": "creative",
    "c": "creative",
    "2": "adventure",
    "a": "adventure",
    "3": "spectator",
    "sp": "spectator",
}


def normalize_gamemode(gamemode: str) -> str:
    """``"2"``, ``"a"`` or ``" Adventure "`` -> ``"adventure"``.

    Unknown values pass through unchanged.
    """
    gm = gamemode.strip().lower()
    return GAMEMODE_ALIASES.get(gm, gm)


@dataclass
class WorldSettings:
    """Everything the launch form edits. Passed to itzg as chart values/env.

    ``seed`` and ``level_type`` (one of ``LEVEL_TYPES``) only matter when the
    server generates a world, i.e. for ``create_world``; on a volume that
    already holds a world (launched or restored from a backup) they are inert.
    """

    version: str = "LATEST"
    server_type: ServerType = ServerType.VANILLA
    memory: str = "4Gi"
    gamemode: str = "survival"
    force_gamemode: bool = False
    difficulty: str = "easy"
    pvp: bool = False
    enable_command_block: bool = False
    motd: str = ""
    max_world_size: int = 29999984
    whitelist: list[str] = field(default_factory=list)
    # No whitelist: any genuine (online-mode) account may join. The names above
    # are kept, so closing the world again restores them.
    open_to_all: bool = False
    # Operators (admins). Must be on the whitelist unless open_to_all. Empty =
    # leave the world's ops.json (e.g. the backup's) alone.
    ops: list[str] = field(default_factory=list)
    seed: str = ""  # blank = random
    level_type: str = "normal"
    # Like seed/level_type, only applies when the server generates the world.
    hardcore: bool = False
    # server.properties tunables. ``None`` = not managed: the world's
    # server.properties keeps whatever it has (the backup's value, else the
    # vanilla default). Records saved before these existed load as None;
    # launch_defaults / new_world_defaults fill them in.
    view_distance: int | None = None  # chunks, 2-32
    simulation_distance: int | None = None  # chunks, 5-32; 1.18+
    spawn_protection: int | None = None  # blocks; 0 = off
    pause_when_empty_seconds: int | None = None  # 0 = never pause; 1.21.2+
    sync_chunk_writes: bool | None = None  # 1.16+
    allow_flight: bool | None = None
    max_players: int | None = None
    broadcast_rcon_to_ops: bool | None = None
    broadcast_console_to_ops: bool | None = None
    # JVM: itzg USE_AIKAR_FLAGS (G1 GC tuning). On by default, also for records
    # saved before the field existed.
    use_aikar_flags: bool = True


@dataclass
class World:
    name: str  # helm release name, DNS-safe
    port: int  # NodePort in the configured range, kept for the world's life
    state: WorldState
    settings: WorldSettings
    source_backup_id: str  # "" for a world generated by create_world
    created: datetime
    connect_address: str  # "<public host>:<port>"
    # RCON NodePort (rcon range, kept for the world's life); 0 until the
    # world's next install assigns one (worlds launched before it existed).
    rcon_port: int = 0
    # LAN address of the node serving RCON ("" = unknown, e.g. stopped).
    rcon_host: str = ""
    node: str = ""  # node the server pod runs on ("" = unknown)
    players_online: list[str] | None = None  # None when not running / unknown
    uptime_seconds: int | None = None
    cpu_millicores: int | None = None
    memory_bytes: int | None = None
    last_snapshot: datetime | None = None
    message: str = ""  # human-readable detail for FAILED / in-progress states


@dataclass(frozen=True)
class Suggestion:
    """One RCON console completion candidate.

    ``kind``: ``literal`` (a command or sub-command name), ``value`` (an
    argument value such as a player or item) or ``hint`` (describes an argument,
    e.g. ``<targets>``; shown but not inserted).
    """

    text: str
    kind: str = "literal"
    detail: str = ""


@dataclass
class Completion:
    """Suggestions for the token being typed, which starts at ``start``.

    ``status``: ``ready`` (a command list is known), ``pending`` (it is being
    read from the server; ask again shortly) or ``none`` (no command list for
    this world: history only). ``message`` explains ``pending``/``none``.
    """

    start: int
    items: list[Suggestion] = field(default_factory=list)
    status: str = "ready"
    message: str = ""


@dataclass
class Player:
    name: str
    note: str = ""


class MCDashError(Exception):
    """Base error; ``str(err)`` is shown to the user."""


class NotFound(MCDashError):
    pass


class Conflict(MCDashError):
    """Name taken, port range exhausted, wrong state for the operation."""


class Forbidden(MCDashError):
    """E.g. deleting an ARCHIVE backup."""


class DashboardService(Protocol):
    """All methods are synchronous and return quickly.

    Long operations (seed, snapshot, restore, delete) start
    Kubernetes Jobs and return; progress is visible via ``World.state``.
    ``actor`` is the X-Auth-Request-Email of the caller, for the audit log.
    """

    # backups
    def list_backups(self) -> list[Backup]: ...  # ARCHIVE + BACKUP + SNAPSHOT
    def get_backup(self, backup_id: str) -> Backup: ...
    def set_backup_label(
        self, backup_id: str, label: str, description: str
    ) -> None: ...
    def promote(
        self, snapshot_id: str, label: str, description: str, actor: str
    ) -> Backup: ...
    def delete_backup(
        self, backup_id: str, actor: str
    ) -> None: ...  # ARCHIVE -> Forbidden

    # worlds
    def launch_defaults(self, backup_id: str) -> tuple[str, WorldSettings]: ...
    def launch(
        self, backup_id: str, name: str, settings: WorldSettings, actor: str
    ) -> World: ...
    def new_world_defaults(self) -> tuple[str, WorldSettings]: ...
    def create_world(
        self, name: str, settings: WorldSettings, actor: str
    ) -> World: ...  # no backup: the server generates a fresh world
    def list_worlds(self) -> list[World]: ...
    def get_world(self, name: str) -> World: ...
    def start(self, name: str, actor: str) -> None: ...
    def stop(self, name: str, actor: str) -> None: ...  # scale to 0, no snapshot
    def snapshot(self, name: str, actor: str) -> None: ...
    def restore(self, name: str, backup_id: str, actor: str) -> None: ...
    def delete(self, name: str, actor: str) -> None: ...  # snapshots first
    def list_snapshots(self, name: str) -> list[Backup]: ...
    def update_settings(
        self, name: str, settings: WorldSettings, actor: str
    ) -> None: ...
    def set_whitelist(
        self,
        name: str,
        players: list[str],
        actor: str,
        *,
        open_to_all: bool,
        ops: list[str] | None = None,  # None = keep (less anyone removed)
    ) -> None: ...

    # live
    def rcon(self, name: str, command: str, actor: str) -> str: ...
    def rcon_password(self, name: str, actor: str) -> str: ...  # audited
    def stream_logs(self, name: str) -> Iterator[str]: ...  # follows; blocks
    def complete(self, name: str, line: str) -> Completion: ...  # RCON console

    # roster
    def roster(self) -> list[Player]: ...
    def set_roster(self, players: list[Player]) -> None: ...
