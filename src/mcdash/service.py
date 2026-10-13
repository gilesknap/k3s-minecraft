"""``KubeDashboardService`` — the real ``DashboardService`` + reconciler.

World records
-------------
Each world has a ConfigMap ``<w>-mcdash`` (label ``mcdash/kind=world``) with:

* ``spec`` — JSON ``{port, sourceBackup, created, settings, forgeVersion,
  message}``. It exists from the moment of launch (before any Helm release),
  so it reserves the port while seeding. The same metadata is mirrored into
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from mcdash.backups import BackupIndex
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
)
from mcdash.k8s import JobInfo, JobSpec, KubeApi, PodInfo
from mcdash.models import (
    LEVEL_TYPES,
    Backup,
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
from mcdash.rcon import RconClient, RconError, parse_list
from mcdash.store import Store

log = logging.getLogger(__name__)

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


class HelmApi(Protocol):
    def upgrade_install(self, release: str, values: dict[str, Any]) -> None: ...
    def uninstall(self, release: str) -> None: ...
    def list(self) -> list[dict[str, Any]]: ...
    def get_values(self, release: str) -> dict[str, Any]: ...


class RconApi(Protocol):
    def command(self, cmd: str) -> str: ...
    def close(self) -> None: ...


RconFactory = Callable[[str, int, str], RconApi]


def _default_rcon(host: str, port: int, password: str) -> RconApi:
    c = RconClient(host, port, password, timeout=5.0)
    c.connect()
    return c


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
        self._values_cache: dict[str, tuple[str, dict[str, Any]]] = {}
        self._secret_cache: dict[str, str] = {}
        self._players_cache: dict[str, tuple[float, list[str] | None]] = {}
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
        return specs, ops, rels

    @staticmethod
    def _spec_from_values(values: dict[str, Any]) -> dict[str, Any]:
        md = values.get("mcdash") or {}
        return {
            "port": int(md.get("port") or 0),
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

    def _allocate_port(self, specs: dict[str, dict[str, Any]]) -> int:
        used = {int(s.get("port") or 0) for s in specs.values()}
        for p in range(self.cfg.port_min, self.cfg.port_max + 1):
            if p not in used:
                return p
        raise Conflict(
            f"no free ports in {self.cfg.port_min}-{self.cfg.port_max}; "
            "delete a world first"
        )

    def _install(self, name: str, spec: dict[str, Any], replicas: int) -> None:
        values = render_values(
            name,
            settings_from_json(spec.get("settings")),
            int(spec["port"]),
            self.cfg,
            replicas=replicas,
            source_backup=spec.get("sourceBackup", ""),
            created=_parse_dt(spec.get("created")),
            forge_version=spec.get("forgeVersion") or None,
        )
        self.helm.upgrade_install(name, values)
        self._values_cache.pop(name, None)

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

    def _rcon_cmds(self, name: str, cmds: list[str]) -> list[str]:
        client = self.rcon_factory(
            self._rcon_host(name), RCON_PORT, self._rcon_password(name)
        )
        try:
            return [client.command(c) for c in cmds]
        finally:
            client.close()

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
            players: list[str] | None = parse_list(self._rcon_cmds(name, ["list"])[0])
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
        state, msg = self._derive(op, values, pods)
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
        for n in s.whitelist:
            if not n or len(n) > 16 or not all(c.isalnum() or c == "_" for c in n):
                raise MCDashError(f"bad player name {n!r}")
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
            self.store.audit(actor, "launch", name, backup=b.id, port=spec["port"])
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
                self._rcon_cmds(name, ["save-off", "save-all flush"])
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
        self, name: str, players: list[str], actor: str, *, open_to_all: bool
    ) -> None:
        players = list(dict.fromkeys(p.strip() for p in players if p.strip()))
        with self._lock:
            spec, op, values = self._load(name)
            settings = settings_from_json(spec.get("settings"))
            old, was_open = settings.whitelist, settings.open_to_all
            settings.whitelist = players
            settings.open_to_all = open_to_all
            self._check_settings(settings)
            pods = self.kube.list_server_pods(name) if values else []
            live = any(p.ready for p in pods)
            if self._replicas(values) and not live and open_to_all != was_open:
                # The pod would come up with the access mode of the last install
                # while the record says otherwise.
                raise Conflict(
                    f"{name} is starting; change who can join once it is running"
                )
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
                if cmds:
                    self._rcon_cmds(name, cmds)
            # Persisted in the world record; rendered into WHITELIST at the next
            # install/upgrade (avoids restarting a running server). Live changes
            # are already in whitelist.json on the world volume.
            spec["settings"] = settings_to_json(settings)
            self._save(name, spec, op)
            self.store.audit(
                actor, "set_whitelist", name, players=players, open_to_all=open_to_all
            )

    # ================================================================ live
    def rcon(self, name: str, command: str, actor: str) -> str:
        self._load(name)
        self.store.audit(actor, "rcon", name, command=command)
        try:
            return self._rcon_cmds(name, [command])[0]
        except (RconError, OSError) as e:
            raise MCDashError(f"RCON: {e}") from e

    def stream_logs(self, name: str) -> Iterator[str]:
        self._load(name)
        pods = [p for p in self.kube.list_server_pods(name) if not p.deleting]
        if not pods:
            yield f"[{name} is not running]"
            return
        yield from self.kube.stream_pod_log(pods[0].name)

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
                self._rcon_cmds(name, ["save-on"])
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
                self.kube.delete_configmap(self._cm(name))
                return False
        except (HelmError, MCDashError) as e:
            self._fail(name, spec, op, str(e))
            return False
        log.error("%s: unknown op step %r", name, step)
        return False

    def reconcile_once(self) -> None:
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
