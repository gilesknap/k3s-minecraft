"""``KubeDashboardService`` — the real ``DashboardService`` + reconciler.

World records
-------------
Each world has a ConfigMap ``<w>-mcdash`` (label ``mcdash/kind=world``) with:

* ``spec`` — JSON ``{port, rconPort, sourceBackup, created, settings,
  forgeVersion, message}``. It exists from the moment of launch (before any
  Helm release), so it reserves the ports while seeding. ``rconPort`` is
  absent for worlds launched before RCON NodePorts existed; their next
  install with the server running (start, end of a restore, a settings save
  while running) assigns one. The same metadata is mirrored into
  the Helm release values under ``mcdash:`` on every install/upgrade; if the
  ConfigMap is lost it is rebuilt from the release values.
* ``op`` — JSON pending multi-step operation (or empty). The reconciler
  thread advances it idempotently from cluster state, so a dashboard restart
  resumes where it left off.

Operations: ``launch`` (seed -> install), ``create`` (install only: a new
world has no backup, the server generates it on the empty volume), ``stop``
(snapshot -> scale 0), ``snapshot``, ``restore`` (snapshot -> scale 0 ->
drain -> seed -> scale 1), ``delete`` (snapshot -> teardown). An op with
``error`` set means FAILED.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mcdash.backups import BackupIndex
from mcdash.completion import REPORT_SCRIPT, VERSION_RE, CommandTree
from mcdash.config import Config
from mcdash.helmops import (
    RCON_PORT,
    RCON_SECRET_KEY,
    HelmError,
    jvm_memory,
    pvc_name,
    rcon_secret_name,
    render_values,
    settings_from_json,
    settings_to_json,
    version_tuple,
)
from mcdash.k8s import JobInfo, JobSpec, KubeApi, PodInfo
from mcdash.models import (
    LEVEL_TYPES,
    Backup,
    Completion,
    Conflict,
    MCDashError,
    NotFound,
    Player,
    ServerType,
    World,
    WorldSettings,
    WorldState,
    normalize_gamemode,
)
from mcdash.naming import (
    format_compact_timestamp,
    is_valid_world_name,
    parse_compact_timestamp,
    sanitize_world_name,
    unique_name,
)
from mcdash.rcon import (
    RconApi,
    RconAuthError,
    RconClient,
    RconError,
    RconPool,
    parse_list,
)
from mcdash.store import Store, read_json

log = logging.getLogger(__name__)
COMMANDS_JOB_DEADLINE = 600  # seconds

CM_SUFFIX = "-mcdash"
CM_KIND = "world"
BAD_WAIT_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "InvalidImageName",
}
DIFFICULTIES = {"0": "peaceful", "1": "easy", "2": "normal", "3": "hard"}
NEW_WORLD_NAME = "new-world"
MAX_SEED_LEN = 64
# The itzg image, for a commands Job when the pod does not say which it runs.
ITZG_IMAGE = "itzg/minecraft-server"


class HelmApi(Protocol):
    def upgrade_install(self, release: str, values: dict[str, Any]) -> None: ...
    def uninstall(self, release: str) -> None: ...
    def list(self) -> list[dict[str, Any]]: ...
    def get_values(self, release: str) -> dict[str, Any]: ...


RconFactory = Callable[[str, int, str], RconApi]


def _default_rcon(host: str, port: int, password: str) -> RconApi:
    c = RconClient(host, port, password, timeout=5.0)
    c.connect()
    return c


def _pod_target(pods: list[PodInfo]) -> tuple[str, str, int] | None:
    """Identity of the server pod RCON reaches, or None when none is ready.

    A new pod, or a container restart in the same pod, changes it.
    """
    for p in pods:
        if p.ready and not p.deleting:
            started = p.started.isoformat() if p.started else ""
            return (p.name, started, p.restarts)
    return None


def _bool(v: str | None) -> bool:
    return str(v).strip().lower() == "true"


def _parse_dt(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def check_players(s: WorldSettings) -> None:
    """Player names are valid; on a closed world every admin is whitelisted."""
    for n in [*s.whitelist, *s.ops]:
        if not n or len(n) > 16 or not all(c.isalnum() or c == "_" for c in n):
            raise MCDashError(f"bad player name {n!r}")
    if not s.open_to_all:
        listed = {n.lower() for n in s.whitelist}
        missing = [n for n in s.ops if n.lower() not in listed]
        if missing:
            raise MCDashError(f"admins must be on the whitelist: {', '.join(missing)}")


def dedupe_players(names: list[str]) -> list[str]:
    """Stripped, non-empty, de-duplicated case-insensitively (first spelling kept)."""
    out: dict[str, str] = {}
    for n in (p.strip() for p in names):
        if n:
            out.setdefault(n.lower(), n)
    return list(out.values())


def same_players(a: list[str], b: list[str]) -> bool:
    """The same set of names, ignoring case."""
    return {p.lower() for p in a} == {p.lower() for p in b}


def next_ops(
    old_whitelist: list[str], ops: list[str], players: list[str], open_to_all: bool
) -> list[str]:
    """The ops a whitelist edit leaves: de-duplicated, and on a closed world
    without anyone just taken off the whitelist (that takes their admin too)."""
    new_ops = dedupe_players(ops)
    if not open_to_all:
        kept = {p.lower() for p in players}
        gone = {p.lower() for p in old_whitelist} - kept
        new_ops = [p for p in new_ops if p.lower() not in gone]
    return new_ops


def check_ops_change(
    name: str, old_ops: list[str], requested: list[str], new_ops: list[str]
) -> None:
    """Refuse to clear the last admin of a stopped world.

    With no ops the next start leaves ops.json untouched (EXISTING_OPS_FILE=SKIP),
    so the old ops would persist; only RCON ``deop`` on a live server removes them.
    ``requested`` is the admin list asked for, before whitelist removals took
    their admins with them, so an indirect removal gets its own message.
    """
    if not old_ops or new_ops:
        return
    dropped = dedupe_players(requested)
    if dropped:
        raise Conflict(
            f"taking {', '.join(dropped)} off the whitelist would remove "
            f"{name}'s last admin; start it first"
        )
    raise Conflict(f"{name} is not running; start it to remove its last admin")


@dataclass(frozen=True)
class _CommandSource:
    tree: CommandTree | None
    status: str
    message: str
    running: bool


class KubeDashboardService:
    """Implements ``mcdash.models.DashboardService`` against Kubernetes."""

    def __init__(
        self,
        cfg: Config,
        kube: KubeApi,
        helm: HelmApi,
        *,
        store: Store | None = None,
        rcon_factory: RconFactory = _default_rcon,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        cache_ttl: float = 10.0,
    ) -> None:
        self.cfg = cfg
        self.kube = kube
        self.helm = helm
        self.store = store or Store(cfg.dashboard_dir, cfg.archive_dir)
        self.backups = BackupIndex(cfg, self.store)
        self.rcon_factory = rcon_factory
        self.now = clock
        self.cache_ttl = cache_ttl
        self._lock = threading.RLock()
        # one reused RCON connection per world (see RconPool for retry rules)
        self.rcon_pool = RconPool()
        self._values_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        self._secret_cache: dict[str, str] = {}
        self._players_cache: dict[str, tuple[float, list[str] | None]] = {}
        self._cmd_cache: dict[str, tuple[float, _CommandSource]] = {}
        self._trees: dict[str, tuple[float, CommandTree | None]] = {}
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_reconcile_error = ""

    @classmethod
    def from_config(cls, cfg: Config) -> KubeDashboardService:
        from mcdash.helmops import Helm
        from mcdash.k8s import Kube

        return cls(cfg, Kube(cfg), Helm(cfg.namespace, cfg.itzg_chart))

    # ================================================================ records
    def _cm(self, name: str) -> str:
        return f"{name}{CM_SUFFIX}"

    def _releases(self) -> dict[str, dict[str, Any]]:
        """Releases of the itzg chart that carry ``mcdash:`` values."""
        out: dict[str, dict[str, Any]] = {}
        for r in self.helm.list():
            name = str(r.get("name", ""))
            # chart is "<name>-<version>": only the itzg "minecraft" chart, not
            # e.g. this dashboard's own "minecraft-dashboard" release.
            if not name or not re.fullmatch(r"minecraft-\d.*", str(r.get("chart", ""))):
                continue
            rev = str(r.get("revision", ""))
            cached = self._values_cache.get(name)
            if cached and cached[0] == rev:
                values = cached[1]
            else:
                try:
                    values = self.helm.get_values(name)
                except (NotFound, HelmError):
                    continue
                self._values_cache[name] = (rev, values)
            if isinstance(values.get("mcdash"), dict):
                out[name] = values
        return out

    def _records(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any] | None],
        dict[str, dict[str, Any]],
    ]:
        """(specs, ops, release values) for every world."""
        cms = self.kube.list_configmaps(CM_KIND)
        rels = self._releases()
        specs: dict[str, dict[str, Any]] = {}
        ops: dict[str, dict[str, Any] | None] = {}
        for cm_name, data in cms.items():
            if not cm_name.endswith(CM_SUFFIX):
                continue
            w = cm_name[: -len(CM_SUFFIX)]
            try:
                specs[w] = json.loads(data.get("spec") or "{}")
            except ValueError:
                specs[w] = {}
            try:
                ops[w] = json.loads(data["op"]) if data.get("op") else None
            except ValueError:
                ops[w] = None
        for w, values in rels.items():
            if w not in specs or not specs[w].get("port"):
                specs[w] = self._spec_from_values(values)
                ops.setdefault(w, None)
                self._save(w, specs[w], ops[w])  # heal a lost ConfigMap
            elif not specs[w].get("rconPort"):
                # installed with a port the record never saw saved: adopt it
                rp = self._spec_from_values(values)["rconPort"]
                if rp:
                    specs[w]["rconPort"] = rp
        return specs, ops, rels

    @staticmethod
    def _spec_from_values(values: dict[str, Any]) -> dict[str, Any]:
        md = values.get("mcdash") or {}
        return {
            "port": int(md.get("port") or 0),
            "rconPort": int(md.get("rconPort") or 0),
            "sourceBackup": md.get("sourceBackup", ""),
            "created": md.get("created", ""),
            "settings": md.get("settings") or {},
            "forgeVersion": md.get("forgeVersion", ""),
            "message": "",
        }

    def _load(
        self, name: str
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        specs, ops, rels = self._records()
        if name not in specs:
            raise NotFound(f"world {name!r} not found")
        return specs[name], ops.get(name), rels.get(name)

    def _save(self, name: str, spec: dict[str, Any], op: dict[str, Any] | None) -> None:
        self.kube.put_configmap(
            self._cm(name),
            name,
            CM_KIND,
            {
                "spec": json.dumps(spec, sort_keys=True),
                "op": json.dumps(op, sort_keys=True) if op else "",
            },
        )

    # ================================================================ helpers
    def _stamp(self) -> tuple[str, str]:
        """(k8s-name-safe stamp, snapshot filename stamp)."""
        ts = format_compact_timestamp(self.now())
        return ts.lower().rstrip("z"), ts

    @staticmethod
    def _used_ports(specs: dict[str, dict[str, Any]]) -> set[int]:
        """Every game and RCON port held by a world record."""
        return {
            int(s.get(k) or 0) for s in specs.values() for k in ("port", "rconPort")
        }

    def _allocate_port(self, specs: dict[str, dict[str, Any]]) -> int:
        used = self._used_ports(specs)
        for p in range(self.cfg.port_min, self.cfg.port_max + 1):
            if p not in used:
                return p
        raise Conflict(
            f"no free ports in {self.cfg.port_min}-{self.cfg.port_max}; "
            "delete a world first"
        )

    def _allocate_rcon_port(self, specs: dict[str, dict[str, Any]]) -> int:
        used = self._used_ports(specs)
        lo, hi = self.cfg.rcon_port_min, self.cfg.rcon_port_max
        for p in range(lo, hi + 1):
            if p not in used:
                return p
        raise Conflict(
            f"no free RCON ports in {lo}-{hi}; delete a world first "
            "(or widen the RCON port range)"
        )

    def _ensure_rcon_port(self, name: str, spec: dict[str, Any]) -> None:
        """Give a world that predates RCON NodePorts one, and record it.

        Caller holds the lock. Saved straight away (with the op as stored), so
        the port is reserved even if the install that follows fails.
        """
        if spec.get("rconPort"):
            return
        specs, ops, _ = self._records()
        others = {w: s for w, s in specs.items() if w != name}
        spec["rconPort"] = self._allocate_rcon_port(others)
        self._save(name, spec, ops.get(name))
        log.info("%s: assigned RCON port %s", name, spec["rconPort"])

    def _install(self, name: str, spec: dict[str, Any], replicas: int) -> None:
        # Only a running server needs an RCON NodePort. Allocating on the
        # replicas=0 installs of stop/restore could fail (range full) and turn
        # a stop into a failure while the server keeps running.
        if replicas:
            self._ensure_rcon_port(name, spec)
        values = render_values(
            name,
            settings_from_json(spec.get("settings")),
            int(spec["port"]),
            self.cfg,
            replicas=replicas,
            source_backup=spec.get("sourceBackup", ""),
            created=_parse_dt(spec.get("created")),
            forge_version=spec.get("forgeVersion") or None,
            rcon_port=int(spec.get("rconPort") or 0),
        )
        try:
            self.helm.upgrade_install(name, values)
        finally:
            self._values_cache.pop(name, None)
            # an upgrade may replace or stop the server pod, even one that
            # fails part-way (no --atomic)
            self.rcon_pool.evict(name)

    def _rcon_password(self, name: str) -> str:
        pw = self._secret_cache.get(name)
        if pw is None:
            data = self.kube.get_secret(rcon_secret_name(name)) or {}
            pw = data.get(RCON_SECRET_KEY)
            if not pw:
                raise MCDashError(f"no RCON secret for {name}")
            self._secret_cache[name] = pw
        return pw

    def _rcon_host(self, name: str) -> str:
        return f"{name}-rcon.{self.cfg.namespace}.svc"

    def _rcon_cmds(
        self,
        name: str,
        cmds: list[str],
        *,
        pods: list[PodInfo] | None = None,
        retry_sent: bool = False,
    ) -> list[str]:
        """Run ``cmds`` on ``name``'s pooled RCON connection.

        ``pods`` (when the caller already listed them) evicts a connection
        to a server pod that has since been replaced. ``retry_sent`` is only
        for read-only commands: see ``RconPool.run``.
        """
        if pods is not None:
            self.rcon_pool.note_target(name, _pod_target(pods))
        host, password = self._rcon_host(name), self._rcon_password(name)

        def connect() -> RconApi:
            try:
                return self.rcon_factory(host, RCON_PORT, password)
            except RconAuthError:
                # the Secret may have been replaced: re-read it next time
                self._secret_cache.pop(name, None)
                raise

        # the password is part of the key: a changed Secret means a new session
        return self.rcon_pool.run(
            name, (host, password), connect, cmds, retry_sent=retry_sent
        )

    def _replicas(self, values: dict[str, Any] | None) -> int:
        if not values:
            return 0
        try:
            return int(values.get("replicaCount", 1))
        except (TypeError, ValueError):
            return 1

    def _snapshot_paths(self, name: str, file_stamp: str) -> tuple[Path, str]:
        local = self.cfg.snapshots_dir / name / f"{file_stamp}.zip"
        job = f"{self.cfg.job_store_mount}/snapshots/{name}/{file_stamp}.zip"
        return local, job

    # ================================================================ state
    def _derive(
        self,
        op: dict[str, Any] | None,
        values: dict[str, Any] | None,
        pods: list[PodInfo],
    ) -> tuple[WorldState, str]:
        if op:
            if op.get("error"):
                return WorldState.FAILED, f"{op['kind']} failed: {op['error']}"
            step = op.get("step")
            if step == "snapshot":
                return WorldState.SNAPSHOTTING, f"snapshot before {op['kind']}"
            if step == "drain":
                return WorldState.SEEDING, "stopping before restore"
            if step == "seed":
                return WorldState.SEEDING, "copying world data"
            if step == "teardown":
                return WorldState.STOPPED, "deleting"
            if step == "install":
                return WorldState.STARTING, "installing server"
        if values is None:
            return WorldState.FAILED, "no helm release"
        if self._replicas(values) == 0:
            return WorldState.STOPPED, ""
        live = [p for p in pods if not p.deleting]
        if any(p.ready for p in live):
            return WorldState.RUNNING, ""
        for p in live:
            if p.waiting_reason in BAD_WAIT_REASONS:
                return WorldState.FAILED, f"server pod: {p.waiting_reason}"
            if p.phase == "Failed":
                return WorldState.FAILED, "server pod failed"
        return WorldState.STARTING, ""

    def _last_snapshot(self, name: str) -> datetime | None:
        d = self.cfg.snapshots_dir / name
        best: datetime | None = None
        try:
            entries = list(d.iterdir())
        except OSError:
            return None
        for p in entries:
            if p.suffix == ".zip" and not p.name.startswith("."):
                ts = parse_compact_timestamp(p.stem)
                if ts and (best is None or ts > best):
                    best = ts
        return best

    def _players(self, name: str) -> list[str] | None:
        hit = self._players_cache.get(name)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        try:
            out = self._rcon_cmds(name, ["list"], retry_sent=True)[0]
            players: list[str] | None = parse_list(out)
        except (RconError, MCDashError, OSError):
            players = None
        self._players_cache[name] = (time.monotonic(), players)
        return players

    def _world(
        self,
        name: str,
        spec: dict[str, Any],
        op: dict[str, Any] | None,
        values: dict[str, Any] | None,
        live: bool = True,
    ) -> World:
        pods = self.kube.list_server_pods(name) if values else []
        self.rcon_pool.note_target(name, _pod_target(pods))
        state, msg = self._derive(op, values, pods)
        pod = next((p for p in pods if not p.deleting and p.host_ip), None)
        w = World(
            name=name,
            port=int(spec.get("port") or 0),
            state=state,
            settings=settings_from_json(spec.get("settings")),
            source_backup_id=str(spec.get("sourceBackup", "")),
            created=_parse_dt(spec.get("created")) or datetime.fromtimestamp(0, UTC),
            connect_address=f"{self.cfg.public_host}:{spec.get('port')}",
            last_snapshot=self._last_snapshot(name),
            message=msg or str(spec.get("message") or ""),
            rcon_port=int(spec.get("rconPort") or 0),
            rcon_host=self.cfg.rcon_host or (pod.host_ip if pod else ""),
            node=(pod.node if pod else "") or self.cfg.world_node,
        )
        if state is WorldState.RUNNING:
            ready = [p for p in pods if p.ready and not p.deleting]
            if ready and ready[0].started:
                w.uptime_seconds = int((self.now() - ready[0].started).total_seconds())
            if live:
                w.players_online = self._players(name)
                m = self.kube.pod_metrics(name)
                if m:
                    w.cpu_millicores, w.memory_bytes = m
        return w

    # ================================================================ backups
    def list_backups(self) -> list[Backup]:
        return self.backups.scan()

    def get_backup(self, backup_id: str) -> Backup:
        return self.backups.get(backup_id)

    def set_backup_label(self, backup_id: str, label: str, description: str) -> None:
        self.backups.set_label(backup_id, label, description)

    def promote(
        self, snapshot_id: str, label: str, description: str, actor: str
    ) -> Backup:
        b = self.backups.promote(snapshot_id, label, description)
        self.store.audit(actor, "promote", snapshot_id, backup=b.id, label=label)
        return b

    def delete_backup(self, backup_id: str, actor: str) -> None:
        self.backups.delete(backup_id)
        self.store.audit(actor, "delete_backup", backup_id)

    # ================================================================ worlds
    def _taken_names(self) -> set[str]:
        specs, _, _ = self._records()
        return set(specs)

    def launch_defaults(self, backup_id: str) -> tuple[str, WorldSettings]:
        b = self.backups.get(backup_id)
        name = unique_name(sanitize_world_name(b.world_name), self._taken_names())
        return name, self.defaults_for(b)

    def defaults_for(self, b: Backup) -> WorldSettings:
        p = b.properties
        s = WorldSettings(memory=self.cfg.default_memory)
        if b.server_type is ServerType.FORGE:
            s.server_type = ServerType.FORGE
            s.version = b.mc_version or "LATEST"
        gm = normalize_gamemode(p.get("gamemode", ""))
        if gm:
            s.gamemode = gm
        df = p.get("difficulty", "").strip().lower()
        if df:
            s.difficulty = DIFFICULTIES.get(df, df)
        if "force-gamemode" in p:
            s.force_gamemode = _bool(p["force-gamemode"])
        if "pvp" in p:
            s.pvp = _bool(p["pvp"])
        if "enable-command-block" in p:
            s.enable_command_block = _bool(p["enable-command-block"])
        if p.get("motd"):
            s.motd = p["motd"]
        try:
            if p.get("max-world-size"):
                s.max_world_size = int(p["max-world-size"])
        except ValueError:
            pass
        return s

    def _check_settings(self, s: WorldSettings) -> None:
        jvm_memory(s.memory or self.cfg.default_memory)  # raises on bad sizes
        check_players(s)
        if s.level_type not in LEVEL_TYPES:
            raise MCDashError(
                f"unknown world type {s.level_type!r} (one of {', '.join(LEVEL_TYPES)})"
            )
        if len(s.seed) > MAX_SEED_LEN or not s.seed.isprintable():
            raise MCDashError(
                f"seed must be at most {MAX_SEED_LEN} printable characters"
            )

    @staticmethod
    def _check_name(name: str) -> None:
        if not is_valid_world_name(name):
            raise MCDashError(
                f"{name!r} is not a valid world name (lowercase letters, digits, "
                "'-', start with a letter, max 40 chars)"
            )

    def _reserve(
        self, name: str, settings: WorldSettings, source: str, forge_version: str
    ) -> dict[str, Any]:
        """New world spec with a free port; caller holds the lock and saves it."""
        specs, _, _ = self._records()
        if name in specs or self.kube.pvc_exists(pvc_name(name)):
            raise Conflict(f"world name {name!r} is taken")
        return {
            "port": self._allocate_port(specs),
            "rconPort": self._allocate_rcon_port(specs),
            "sourceBackup": source,
            "created": self.now().isoformat(timespec="seconds"),
            "settings": settings_to_json(settings),
            "forgeVersion": forge_version,
            "message": "",
        }

    def _create_volume_and_secret(self, name: str) -> None:
        with contextlib.suppress(Conflict):
            self.kube.create_secret(
                rcon_secret_name(name),
                name,
                {RCON_SECRET_KEY: secrets.token_urlsafe(24)},
            )
        with contextlib.suppress(Conflict):
            self.kube.create_pvc(pvc_name(name), name)

    def _seed_job(
        self, name: str, backup_id: str, settings: WorldSettings
    ) -> tuple[str, JobSpec]:
        k8s_ts, _ = self._stamp()
        job = f"{name}-seed-{k8s_ts}"
        mode = "full" if settings.server_type is ServerType.FORGE else "vanilla"
        spec = JobSpec(
            name=job,
            world=name,
            kind="seed",
            args=[
                "seed",
                "--src",
                self.backups.job_path_of(backup_id),
                "--dest",
                "/data",
                "--mode",
                mode,
            ],
            store_read_only=True,
        )
        return job, spec

    def launch(
        self, backup_id: str, name: str, settings: WorldSettings, actor: str
    ) -> World:
        self._check_name(name)
        self._check_settings(settings)
        b = self.backups.get(backup_id)
        det = self.backups.detection(backup_id)
        with self._lock:
            spec = self._reserve(name, settings, b.id, det.forge_version or "")
            job, jspec = self._seed_job(name, b.id, settings)
            op = {"kind": "launch", "step": "seed", "job": job, "backup": b.id}
            self._save(name, spec, op)  # reserves name + port first
            self._create_volume_and_secret(name)
            self.kube.create_job(jspec)
            self.store.audit(
                actor,
                "launch",
                name,
                backup=b.id,
                port=spec["port"],
                rcon_port=spec["rconPort"],
            )
            return self._world(name, spec, op, None, live=False)

    def new_world_defaults(self) -> tuple[str, WorldSettings]:
        name = unique_name(NEW_WORLD_NAME, self._taken_names())
        return name, WorldSettings(memory=self.cfg.default_memory)

    def create_world(self, name: str, settings: WorldSettings, actor: str) -> World:
        """A fresh world: no seed Job, itzg generates it on the empty volume."""
        self._check_name(name)
        self._check_settings(settings)
        with self._lock:
            spec = self._reserve(name, settings, "", "")
            op: dict[str, Any] | None = {"kind": "create", "step": "install"}
            self._save(name, spec, op)  # reserves name + port first
            self._create_volume_and_secret(name)
            self.store.audit(
                actor,
                "create_world",
                name,
                port=spec["port"],
                rcon_port=spec["rconPort"],
                seed=settings.seed,
                level_type=settings.level_type,
            )
            try:
                self._install(name, spec, replicas=1)
                op = None
            except HelmError as e:  # "Start" retries the install
                op = {**op, "error": str(e)}
            self._save(name, spec, op)
            values = None if op else self._releases().get(name)
            return self._world(name, spec, op, values, live=False)

    def list_worlds(self) -> list[World]:
        specs, ops, rels = self._records()
        return [
            self._world(n, specs[n], ops.get(n), rels.get(n)) for n in sorted(specs)
        ]

    def get_world(self, name: str) -> World:
        spec, op, values = self._load(name)
        return self._world(name, spec, op, values)

    def rcon_password(self, name: str, actor: str) -> str:
        """The world's RCON password, for direct (NodePort) clients. Audited."""
        self._load(name)
        self.store.audit(actor, "rcon_password", name)
        return self._rcon_password(name)

    def _busy(self, name: str, op: dict[str, Any] | None) -> None:
        if op and not op.get("error"):
            raise Conflict(f"{name} is busy ({op['kind']} in progress)")

    def start(self, name: str, actor: str) -> None:
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            if op and op.get("step") == "seed":  # retry a failed seed
                job, jspec = self._seed_job(
                    name, op["backup"], settings_from_json(spec.get("settings"))
                )
                op = {**op, "job": job}
                op.pop("error", None)
                self._save(name, spec, op)
                self.kube.create_job(jspec)
            else:
                spec["message"] = ""
                self._save(name, spec, None)
                self._install(name, spec, replicas=1)
            self.store.audit(actor, "start", name)

    def _begin_snapshot(
        self,
        name: str,
        spec: dict[str, Any],
        values: dict[str, Any] | None,
        kind: str,
        **extra: Any,
    ) -> None:
        k8s_ts, file_ts = self._stamp()
        local, job_path = self._snapshot_paths(name, file_ts)
        op: dict[str, Any] = {
            "kind": kind,
            "step": "snapshot",
            "job": f"{name}-snap-{k8s_ts}",
            "dest": job_path,
            "local": str(local),
            "paused": False,
            **extra,
        }
        pods = self.kube.list_server_pods(name) if values else []
        if self._replicas(values) and any(p.ready for p in pods):
            try:
                self._rcon_cmds(name, ["save-off", "save-all flush"], pods=pods)
                op["paused"] = True
            except (RconError, MCDashError, OSError) as e:
                log.warning("%s: could not pause saving: %s", name, e)
        spec["message"] = ""
        self._save(name, spec, op)
        self._create_snapshot_job(name, op)

    def _create_snapshot_job(self, name: str, op: dict[str, Any]) -> None:
        self.store.guard.check(Path(op["local"]))
        Path(op["local"]).parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Conflict):
            self.kube.create_job(
                JobSpec(
                    name=op["job"],
                    world=name,
                    kind="snapshot",
                    args=["snapshot", "--src", "/data", "--dest", op["dest"]],
                    store_read_only=False,
                )
            )

    def stop(self, name: str, actor: str) -> None:
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            if values is None or (self._replicas(values) == 0 and not op):
                return
            if op:  # failed op: clear it
                op = None
            self._begin_snapshot(name, spec, values, "stop")
            self.store.audit(actor, "stop", name)

    def snapshot(self, name: str, actor: str) -> None:
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            self._begin_snapshot(name, spec, values, "snapshot")
            self.store.audit(actor, "snapshot", name)

    def restore(self, name: str, backup_id: str, actor: str) -> None:
        self.backups.get(backup_id)  # validates
        det = self.backups.detection(backup_id)
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            extra = {"backup": backup_id, "forgeVersion": det.forge_version or ""}
            if op and op.get("step") in ("seed", "install"):
                # data broken, or never generated: nothing worth snapshotting
                self._save(name, spec, {"kind": "restore", "step": "drain", **extra})
            else:
                self._begin_snapshot(name, spec, values, "restore", **extra)
            self.store.audit(actor, "restore", name, backup=backup_id)

    def delete(self, name: str, actor: str) -> None:
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            if values is None or (op and op.get("step") == "seed"):
                # never ran, or data known broken: nothing worth snapshotting
                self._save(name, spec, {"kind": "delete", "step": "teardown"})
            else:
                self._begin_snapshot(name, spec, values, "delete")
            self.store.audit(actor, "delete", name)
        self.reconcile_world(name)

    def list_snapshots(self, name: str) -> list[Backup]:
        return self.backups.snapshots_of(name)

    def update_settings(self, name: str, settings: WorldSettings, actor: str) -> None:
        self._check_settings(settings)
        with self._lock:
            spec, op, values = self._load(name)
            self._busy(name, op)
            spec["settings"] = settings_to_json(settings)
            self._save(name, spec, op)
            if values is not None:
                self._install(name, spec, replicas=self._replicas(values))
            self.store.audit(actor, "update_settings", name, settings=spec["settings"])

    def set_whitelist(
        self,
        name: str,
        players: list[str],
        actor: str,
        *,
        open_to_all: bool,
        ops: list[str] | None = None,
    ) -> None:
        players = list(dict.fromkeys(p.strip() for p in players if p.strip()))
        with self._lock:
            spec, op, values = self._load(name)
            settings = settings_from_json(spec.get("settings"))
            old, was_open = settings.whitelist, settings.open_to_all
            old_ops = settings.ops
            requested = old_ops if ops is None else ops
            new_ops = next_ops(old, requested, players, open_to_all)
            settings.whitelist = players
            settings.open_to_all = open_to_all
            settings.ops = new_ops
            self._check_settings(settings)
            pods = self.kube.list_server_pods(name) if values else []
            live = any(p.ready for p in pods)
            starting = bool(self._replicas(values)) and not live
            if starting and open_to_all != was_open:
                # The pod would come up with the access mode of the last install
                # while the record says otherwise.
                raise Conflict(
                    f"{name} is starting; change who can join once it is running"
                )
            if starting and not same_players(old_ops, new_ops):
                # The booting pod's OPS env and SYNCHRONIZE would re-apply the
                # old admins, and it cannot take RCON yet.
                raise Conflict(
                    f"{name} is starting; change its admins once it is running"
                )
            if values is not None and not self._replicas(values):
                # Stopped: the next start's SYNCHRONIZE applies any admin change
                # except an empty list.
                check_ops_change(name, old_ops, requested, new_ops)
            if self._replicas(values) and live:
                # While a world was open its whitelist.json was never synced, so
                # closing it must (re)add every name, not just the new ones.
                closing = was_open and not open_to_all
                cmds = [
                    f"whitelist add {p}" for p in players if closing or p not in old
                ]
                cmds += [f"whitelist remove {p}" for p in old if p not in players]
                if open_to_all != was_open:
                    cmds.append("whitelist off" if open_to_all else "whitelist on")
                was_op = {p.lower() for p in old_ops}
                is_op = {p.lower() for p in new_ops}
                cmds += [f"op {p}" for p in new_ops if p.lower() not in was_op]
                cmds += [f"deop {p}" for p in old_ops if p.lower() not in is_op]
                if cmds:
                    self._rcon_cmds(name, cmds, pods=pods)
            # Persisted in the world record; rendered into WHITELIST/OPS at the
            # next install/upgrade (avoids restarting a running server). Live
            # changes are already in whitelist.json/ops.json on the world volume.
            spec["settings"] = settings_to_json(settings)
            self._save(name, spec, op)
            self.store.audit(
                actor,
                "set_whitelist",
                name,
                players=players,
                open_to_all=open_to_all,
                ops=new_ops,
            )

    # ================================================================ live
    def rcon(self, name: str, command: str, actor: str) -> str:
        self._load(name)
        self.store.audit(actor, "rcon", name, command=command)
        try:
            # list the pods first: console input is never retried once sent, so
            # a connection to a replaced pod must be dropped before sending
            pods = self.kube.list_server_pods(name)
            return self._rcon_cmds(name, [command], pods=pods)[0]
        except (RconError, OSError) as e:
            raise MCDashError(f"RCON: {e}") from e

    def stream_logs(self, name: str) -> Iterator[str]:
        self._load(name)
        pods = [p for p in self.kube.list_server_pods(name) if not p.deleting]
        if not pods:
            yield f"[{name} is not running]"
            return
        yield from self.kube.stream_pod_log(pods[0].name)

    # ================================================================ console
    @property
    def commands_dir(self) -> Path:
        return self.cfg.dashboard_dir / "commands"

    def complete(self, name: str, line: str) -> Completion:
        src = self._command_source(name)
        if src.tree is None:
            return Completion(len(line), [], src.status, src.message)
        players = (self._players(name) or []) if src.running else []
        start, items = src.tree.suggest(line, players)
        return Completion(start, items, src.status, src.message)

    def _tree(self, version: str) -> CommandTree | None:
        """The command tree generated for ``version``, if any (cached)."""
        if not VERSION_RE.fullmatch(version):
            return None
        path = self.commands_dir / f"{version}.commands.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._trees.get(version)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            tree: CommandTree | None = CommandTree.load(
                path, self.commands_dir / f"{version}.registries.json"
            )
        except (OSError, ValueError, TypeError, AttributeError, KeyError) as e:
            log.warning("bad command report %s: %s", path, e)
            tree = None
        self._trees[version] = (mtime, tree)
        return tree

    def _command_source(self, name: str) -> _CommandSource:
        hit = self._cmd_cache.get(name)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        with self._lock:
            spec, op, values = self._load(name)
            settings = settings_from_json(spec.get("settings"))
            pods = self.kube.list_server_pods(name) if values and not op else []
            pod = next((p for p in pods if p.ready and not p.deleting), None)
            ptr = read_json(self.commands_dir / "worlds" / f"{name}.json", {})
            if not isinstance(ptr, dict):
                ptr = {}
            version = str(ptr.get("version") or "")
            status, msg = "ready", ""
            if pod is None:
                pass  # not running: use whatever was last recorded
            elif ptr.get("pod") != pod.name:
                why = self._why_no_commands(settings)
                if why:
                    msg = why
                elif self._commands_job(name, pod):
                    status = "pending"
                    msg = "reading the command list from the server"
            elif ptr.get("error"):
                msg = f"no command list: {ptr['error']}"
            if not version and VERSION_RE.fullmatch(settings.version):
                version = settings.version  # e.g. Forge: vanilla commands if known
            tree = self._tree(version) if version else None
            if tree is None and status == "ready":
                status = "none"
                msg = msg or "no command list for this world"
            src = _CommandSource(tree, status, msg, pod is not None)
        self._cmd_cache[name] = (time.monotonic(), src)
        return src

    def _why_no_commands(self, settings: WorldSettings) -> str:
        if settings.server_type is not ServerType.VANILLA:
            return "modded server: vanilla commands only, if known"
        t = version_tuple(settings.version)
        if t is not None and t < (1, 13, 0):
            return "Minecraft before 1.13 has no command tree"
        if not self.cfg.store_pvc:
            return "no store volume for the commands Job"
        return ""

    def _commands_job(self, name: str, pod: PodInfo) -> bool:
        """Start a commands Job for ``pod`` unless one ran or is running.

        Returns True while the Job is still to finish. A Job that already ran
        for this pod and left no record (e.g. it failed) is not repeated.
        """
        for j in self.kube.list_jobs(name):
            if j.kind != "commands":
                continue
            if not j.done:
                return True
            if pod.started and j.created and j.created >= pod.started:
                return False
        out = self.commands_dir / "worlds"
        self.store.guard.check(out)
        out.mkdir(parents=True, exist_ok=True)  # the Job mounts it as uid 1000
        k8s_ts, _ = self._stamp()
        spec = JobSpec(
            name=f"{name}-cmds-{k8s_ts}",
            world=name,
            kind="commands",
            args=[],
            image=pod.image or ITZG_IMAGE,
            command=["sh", "-c", REPORT_SCRIPT],
            env={"MCD_WORLD": name, "MCD_POD": pod.name},
            data_read_only=True,
            mount_archive=False,
            store_read_only=False,
            store_sub_path=self.commands_dir.relative_to(self.cfg.store_dir).as_posix(),
            store_mount="/out",
            backoff_limit=0,
            # ~6 s of work; a pod stuck pending fails the Job, and a failed
            # Job is not repeated for this pod, so the console degrades to
            # history-only instead of waiting forever.
            active_deadline_seconds=COMMANDS_JOB_DEADLINE,
        )
        with contextlib.suppress(Conflict):
            self.kube.create_job(spec)
            log.info(
                "started commands Job %s for %s on pod %s", spec.name, name, pod.name
            )
        return True

    # ================================================================ roster
    def roster(self) -> list[Player]:
        return self.store.roster()

    def set_roster(self, players: list[Player]) -> None:
        self.store.set_roster(players)

    # ================================================================ reconcile
    def _job_status(
        self, name: str, spec: dict[str, Any], op: dict[str, Any]
    ) -> JobInfo | None:
        """Finished Job status, or None while running.

        A missing Job (dashboard was down past its TTL, or crashed between
        recording the op and creating the Job) is recreated; seeding wipes
        first and snapshots write to a fresh name, so both are idempotent.
        """
        job = self.kube.get_job(op["job"])
        if job is None:
            if op["step"] == "snapshot":
                if Path(op["local"]).is_file():
                    return JobInfo(op["job"], name, "snapshot", succeeded=1)
                self._create_snapshot_job(name, op)
            else:
                _, jspec = self._seed_job(
                    name, op["backup"], settings_from_json(spec.get("settings"))
                )
                jspec.name = op["job"]
                with contextlib.suppress(Conflict):
                    self.kube.create_job(jspec)
            return None
        return job if job.done else None

    def _save_on(self, name: str, op: dict[str, Any]) -> None:
        if op.get("paused"):
            try:
                pods = self.kube.list_server_pods(name)
                self._rcon_cmds(name, ["save-on"], pods=pods)
            except (RconError, MCDashError, OSError) as e:
                log.warning("%s: save-on failed: %s", name, e)

    def reconcile_world(self, name: str) -> None:
        """Advance ``name``'s pending operation by as many steps as possible."""
        with self._lock:
            for _ in range(5):
                try:
                    spec, op, values = self._load(name)
                except NotFound:
                    return
                if not op or op.get("error"):
                    return
                if not self._advance(name, spec, op, values):
                    return

    def _fail(self, name: str, spec: dict[str, Any], op: dict[str, Any], msg: str):
        op = {**op, "error": msg}
        self._save(name, spec, op)
        log.warning("%s: %s failed: %s", name, op["kind"], msg)

    def _advance(
        self,
        name: str,
        spec: dict[str, Any],
        op: dict[str, Any],
        values: dict[str, Any] | None,
    ) -> bool:
        """One step. Returns True if progress was made."""
        kind, step = op["kind"], op["step"]
        try:
            if step == "snapshot":
                job = self._job_status(name, spec, op)
                if job is None:
                    return False
                self._save_on(name, op)
                if job.failed:
                    spec["message"] = (
                        f"{kind} aborted: snapshot failed ({job.message})"
                        if kind != "snapshot"
                        else f"snapshot failed ({job.message})"
                    )
                    self._save(name, spec, None)
                    return False
                try:
                    self.backups.prune_snapshots(name, self.cfg.snapshot_keep)
                except (OSError, MCDashError) as e:
                    log.warning("%s: prune failed: %s", name, e)
                if kind == "snapshot":
                    self._save(name, spec, None)
                elif kind == "stop":
                    self._install(name, spec, replicas=0)
                    self._save(name, spec, None)
                elif kind == "restore":
                    if values is not None:
                        self._install(name, spec, replicas=0)
                    self._save(name, spec, {**op, "step": "drain"})
                elif kind == "delete":
                    self._save(name, spec, {**op, "step": "teardown"})
                return True
            if step == "drain":
                if [p for p in self.kube.list_server_pods(name)]:
                    return False
                job, jspec = self._seed_job(
                    name, op["backup"], settings_from_json(spec.get("settings"))
                )
                new_op = {**op, "step": "seed", "job": job}
                self._save(name, spec, new_op)
                self.kube.create_job(jspec)
                return True
            if step == "seed":
                job = self._job_status(name, spec, op)
                if job is None:
                    return False
                if job.failed:
                    self._fail(name, spec, op, f"seed job failed ({job.message})")
                    return False
                spec["sourceBackup"] = op["backup"]
                if "forgeVersion" in op:
                    spec["forgeVersion"] = op["forgeVersion"]
                spec["message"] = ""
                self._install(name, spec, replicas=1)
                self._save(name, spec, None)
                return True
            if step == "install":  # create_world interrupted mid-way
                self._create_volume_and_secret(name)
                self._install(name, spec, replicas=1)
                self._save(name, spec, None)
                return True
            if step == "teardown":
                self.helm.uninstall(name)
                self._values_cache.pop(name, None)
                for j in self.kube.list_jobs(name):
                    self.kube.delete_job(j.name)
                self.kube.delete_pvc(pvc_name(name))
                self.kube.delete_secret(rcon_secret_name(name))
                self._secret_cache.pop(name, None)
                self.rcon_pool.evict(name)
                self.kube.delete_configmap(self._cm(name))
                return False
        except (HelmError, MCDashError) as e:
            self._fail(name, spec, op, str(e))
            return False
        log.error("%s: unknown op step %r", name, step)
        return False

    def reconcile_once(self) -> None:
        for name in self.rcon_pool.sweep_idle():
            log.debug("%s: closed idle RCON connection", name)
        try:
            _, ops, _ = self._records()
        except MCDashError as e:  # e.g. no cluster: say so once, not every tick
            if str(e) != self._last_reconcile_error:
                log.warning("reconcile: cannot list worlds: %s", e)
                self._last_reconcile_error = str(e)
            return
        except Exception:
            log.exception("reconcile: cannot list worlds")
            return
        self._last_reconcile_error = ""
        for name, op in ops.items():
            if op and not op.get("error"):
                try:
                    self.reconcile_world(name)
                except Exception:
                    log.exception("reconcile %s failed", name)

    def start_reconciler(self, interval: float = 3.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()

        def loop() -> None:
            while not self._stop_evt.is_set():
                self.reconcile_once()
                self._stop_evt.wait(interval)

        self._thread = threading.Thread(
            target=loop, name="mcdash-reconciler", daemon=True
        )
        self._thread.start()

    def stop_reconciler(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.rcon_pool.close_all()
