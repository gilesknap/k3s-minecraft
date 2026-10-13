"""``helm`` CLI wrapper and itzg chart value rendering.

Values verified against itzg ``minecraft`` chart 5.2.0
(https://itzg.github.io/minecraft-server-charts).
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from mcdash.config import Config
from mcdash.models import (
    SETTING_RANGES,
    MCDashError,
    NotFound,
    ServerType,
    WorldSettings,
    normalize_gamemode,
)

VERIFIED_CHART_VERSION = "5.2.0"
RCON_PORT = 25575
RCON_SECRET_KEY = "rcon-password"


class HelmError(MCDashError):
    pass


# ---------------------------------------------------------------- rendering


def rcon_secret_name(world: str) -> str:
    return f"{world}-rcon"


def pvc_name(world: str) -> str:
    return f"{world}-data"


def version_tuple(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", v.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups(default="0"))


def java_tag_for(version: str | None) -> str | None:
    """itzg image tag matching the Java an old Minecraft version needs.

    ``None`` means "chart default" (latest Java) — used for LATEST/snapshots.
    """
    if not version:
        return None
    t = version_tuple(version)
    if t is None:
        return None
    if t < (1, 17, 0):
        return "java8"
    if t < (1, 18, 0):
        return "java16"
    if t < (1, 20, 5):
        return "java17"
    if t < (2, 0, 0):
        return "java21"
    return None  # new-style versions (e.g. 26.1): latest image


# itzg passes LEVEL_TYPE straight into server.properties ``level-type``.
# 1.19+ wants a world-preset id; older servers want the legacy names.
_LEVEL_TYPE_MODERN = {
    "flat": "minecraft:flat",
    "amplified": "minecraft:amplified",
    "large_biomes": "minecraft:large_biomes",
}
_LEVEL_TYPE_LEGACY = {
    "flat": "FLAT",
    "amplified": "AMPLIFIED",
    "large_biomes": "LARGEBIOMES",
}


def level_type_for(level_type: str, version: str | None) -> str | None:
    """itzg ``LEVEL_TYPE`` for ``level_type``; ``None`` = server default."""
    t = version_tuple(version or "")
    table = (
        _LEVEL_TYPE_LEGACY if t is not None and t < (1, 19, 0) else _LEVEL_TYPE_MODERN
    )
    return table.get(level_type)


# ``forceload`` exists since 1.14.
FORCELOAD_MIN_VERSION = (1, 14, 0)
# RCON commands run at the overworld world spawn, so ``~ ~`` is the spawn chunk.
FORCELOAD_SPAWN_CMD = "forceload add ~ ~"


def startup_rcon_commands(settings: WorldSettings) -> list[str]:
    """RCON commands itzg runs once the server is up (``RCON_CMDS_STARTUP``).

    Adventure worlds force-load the spawn chunk. On 26.3 (seen and verified in
    its code; later 26.x assumed) a dedicated server
    whose default game type (server.properties ``gamemode``, applied over
    level.dat ``GameType`` at every start) is adventure finds a respawn
    position without loading the spawn chunk; if it is unloaded the player
    lands at the bottom of the world and suffocates. Survival/creative worlds
    load the chunk before searching, so they are not affected, even for
    players switched to adventure individually. Skipped where ``forceload``
    does not exist (< 1.14, e.g. old Forge worlds); ``LATEST`` and versions
    that do not parse (snapshots, 26.x-style ids) are treated as new.
    """
    if normalize_gamemode(settings.gamemode) != "adventure":
        return []
    t = version_tuple(settings.version or "")
    if t is not None and t < FORCELOAD_MIN_VERSION:
        return []
    return [FORCELOAD_SPAWN_CMD]


# The first version with each server.properties key the dashboard sets that is
# not ancient. Older servers ignore unknown keys, but they are not rendered
# where pointless (e.g. 1.12 Forge worlds).
PROPERTY_MIN_VERSION: dict[str, tuple[int, ...]] = {
    "sync_chunk_writes": (1, 16, 0),  # 20w14a
    "simulation_distance": (1, 18, 0),  # 21w38a
    "pause_when_empty_seconds": (1, 21, 2),  # 24w33a
}


def property_supported(field: str, version: str | None) -> bool:
    """Whether the ``WorldSettings`` ``field`` exists on ``version``.

    ``LATEST`` and versions that do not parse (snapshots, 26.x-style ids are
    parsed) are treated as new.
    """
    need = PROPERTY_MIN_VERSION.get(field)
    t = version_tuple(version or "")
    return need is None or t is None or t >= need


def _env_bool(v: bool) -> str:
    return "true" if v else "false"


def jvm_memory(k8s_qty: str) -> str:
    """``4Gi`` -> ``4G`` (itzg MEMORY / -Xmx), ``1536Mi`` -> ``1536M``."""
    m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)i?\s*", k8s_qty, re.IGNORECASE)
    if not m:
        raise MCDashError(f"bad memory size {k8s_qty!r} (use e.g. 4Gi)")
    return f"{m.group(1)}{m.group(2).upper()}"


def memory_mib(k8s_qty: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)i?\s*", k8s_qty, re.IGNORECASE)
    if not m:
        raise MCDashError(f"bad memory size {k8s_qty!r} (use e.g. 4Gi)")
    n, unit = int(m.group(1)), m.group(2).upper()
    return {"K": n // 1024, "": n // (1024 * 1024), "M": n, "G": n * 1024}.get(
        unit, n * 1024 * 1024
    )


# ``bool | None`` settings: None = leave server.properties alone.
OPTIONAL_BOOLS = (
    "sync_chunk_writes",
    "allow_flight",
    "broadcast_rcon_to_ops",
    "broadcast_console_to_ops",
)
# WorldSettings field -> itzg env var, for settings passed as extraEnv.
PROPERTY_ENV = {
    "simulation_distance": "SIMULATION_DISTANCE",
    "pause_when_empty_seconds": "PAUSE_WHEN_EMPTY_SECONDS",
    "sync_chunk_writes": "SYNC_CHUNK_WRITES",
    "allow_flight": "ALLOW_FLIGHT",
    "broadcast_rcon_to_ops": "BROADCAST_RCON_TO_OPS",
    "broadcast_console_to_ops": "BROADCAST_CONSOLE_TO_OPS",
}


def settings_to_json(s: WorldSettings) -> dict[str, Any]:
    d = asdict(s)
    d["server_type"] = s.server_type.value
    return d


def settings_from_json(d: dict[str, Any] | None) -> WorldSettings:
    d = dict(d or {})
    base = WorldSettings()
    known = set(asdict(base))
    kw: dict[str, Any] = {k: v for k, v in d.items() if k in known}
    if "server_type" in kw:
        try:
            kw["server_type"] = ServerType(kw["server_type"])
        except ValueError:
            kw["server_type"] = ServerType.VANILLA
    for key in ("whitelist", "ops"):
        if key in kw:
            kw[key] = [str(x) for x in kw[key] or []]
    for key in ("open_to_all", "hardcore"):
        if key in kw:
            kw[key] = kw[key] is True
    if not isinstance(kw.get("forge_version", ""), str):
        kw["forge_version"] = ""
    if "use_aikar_flags" in kw:
        kw["use_aikar_flags"] = kw["use_aikar_flags"] is not False
    # managed server.properties: anything malformed -> None (not managed)
    for key in SETTING_RANGES:
        if key in kw:
            v = kw[key]
            ok = isinstance(v, int) and not isinstance(v, bool)
            kw[key] = v if ok else None
    for key in OPTIONAL_BOOLS:
        if key in kw and not isinstance(kw[key], bool):
            kw[key] = None
    return WorldSettings(**kw)


# Pod terminationGracePeriodSeconds: comfortably above itzg's STOP_DURATION
# (60s default), which the server gets to save and shut down after SIGTERM.
STOP_GRACE_SECONDS = 90


def render_values(
    world: str,
    settings: WorldSettings,
    port: int,
    cfg: Config,
    *,
    replicas: int = 1,
    source_backup: str = "",
    created: datetime | None = None,
    forge_version: str | None = None,
    rcon_port: int = 0,
) -> dict[str, Any]:
    """itzg chart values for one world (contract: docs/contracts.md).

    ``rcon_port``: the world's RCON NodePort; 0 keeps RCON cluster-internal.
    ``forge_version``: the build detected from the world's backup;
    ``settings.forge_version`` overrides it.
    """
    mem = settings.memory or cfg.default_memory
    mib = memory_mib(mem)
    ms: dict[str, Any] = {
        "eula": "TRUE",
        "type": settings.server_type.value,
        "version": settings.version or "LATEST",
        "difficulty": settings.difficulty,
        "gameMode": settings.gamemode,
        "forcegameMode": bool(settings.force_gamemode),
        "pvp": bool(settings.pvp),
        "enableCommandBlock": bool(settings.enable_command_block),
        "onlineMode": True,
        "memory": jvm_memory(mem),
        "serviceType": "NodePort",
        "nodePort": port,
        "externalTrafficPolicy": "Local",
        "rcon": {
            "enabled": True,
            "port": RCON_PORT,
            "existingSecret": rcon_secret_name(world),
            "secretKey": RCON_SECRET_KEY,
            "serviceType": "ClusterIP",
        },
    }
    if rcon_port:
        # The world's node serves it directly (worlds are pinned to one node);
        # Local keeps the client's IP, which the dashboard chart's
        # NetworkPolicy filters on.
        ms["rcon"].update(
            serviceType="NodePort",
            nodePort=rcon_port,
            externalTrafficPolicy="Local",
        )
    if settings.motd:
        ms["motd"] = settings.motd
    # itzg enables the whitelist whenever WHITELIST is set, so an open world
    # must not pass the names at all.
    if settings.whitelist and not settings.open_to_all:
        ms["whitelist"] = ",".join(settings.whitelist)
    if settings.ops:
        ms["ops"] = ",".join(settings.ops)
    # The admin's override, else the build detected from the backup (kept in
    # mcdash.forgeVersion); neither = itzg's recommended build.
    forge = settings.forge_version or forge_version
    if settings.server_type is ServerType.FORGE and forge:
        ms["forgeVersion"] = forge
    # Only used when the server generates a world (create_world); inert on a
    # volume that already holds one.
    if settings.seed:
        ms["levelSeed"] = settings.seed
    level_type = level_type_for(settings.level_type, settings.version)
    if level_type:
        ms["levelType"] = level_type
    # Only rendered when on: it matters when the server generates the world,
    # and "false" would overwrite a backup's hardcore=true.
    if settings.hardcore:
        ms["hardcore"] = True
    # Typed chart values where the chart has them. Small ints render fine
    # (helm's float64 only switches to e-notation from 1e6; SETTING_RANGES
    # stays below that). None = not managed: itzg leaves the property as is.
    for field, key in (
        ("view_distance", "viewDistance"),
        ("spawn_protection", "spawnProtection"),
        ("max_players", "maxPlayers"),
    ):
        v = getattr(settings, field)
        if v is not None:
            ms[key] = int(v)

    whitelisted = not settings.open_to_all
    extra_env: dict[str, Any] = {
        "ENABLE_WHITELIST": "TRUE" if whitelisted else "FALSE",
        "ENFORCE_WHITELIST": "TRUE" if whitelisted else "FALSE",
        # with a list, make whitelist.json match it; else keep the file as is
        # (the backup's, or the names an open world will need if closed again)
        "EXISTING_WHITELIST_FILE": (
            "SYNCHRONIZE" if settings.whitelist and whitelisted else "SKIP"
        ),
        # likewise ops.json: no list keeps the ops restored with the backup
        "EXISTING_OPS_FILE": "SYNCHRONIZE" if settings.ops else "SKIP",
        "OVERRIDE_SERVER_PROPERTIES": "TRUE",
        # not minecraftServer.maxWorldSize: the schema wants an int, which helm
        # renders as 2.9999984e+07 (float64) — pass the env var as a string
        "MAX_WORLD_SIZE": str(int(settings.max_world_size)),
    }
    # server.properties the chart has no typed value for
    for field, env in PROPERTY_ENV.items():
        v = getattr(settings, field)
        if v is None or not property_supported(field, settings.version):
            continue
        extra_env[env] = _env_bool(v) if isinstance(v, bool) else str(int(v))
    if settings.use_aikar_flags:
        extra_env["USE_AIKAR_FLAGS"] = "true"
    startup_cmds = startup_rcon_commands(settings)
    if startup_cmds:
        # itzg runs each line via rcon-cli once the server is listening, on
        # every start; re-adding an already forced chunk is a no-op.
        extra_env["RCON_CMDS_STARTUP"] = "\n".join(startup_cmds)

    values: dict[str, Any] = {
        "fullnameOverride": world,
        "replicaCount": replicas,
        "minecraftServer": ms,
        "extraEnv": extra_env,
        "resources": {
            "requests": {"memory": f"{mib}Mi", "cpu": "500m"},
            "limits": {"memory": f"{mib + 1024}Mi"},
        },
        "podSecurityContext": {
            "runAsUser": cfg.uid,
            "runAsGroup": cfg.gid,
            "fsGroup": cfg.gid,
        },
        "persistence": {
            "dataDir": {"enabled": True, "existingClaim": pvc_name(world)},
        },
        # First start of an old world (download, world upgrade, Forge install)
        # can take many minutes; without a startup probe the chart's liveness
        # probe (30s + 20 x 5s) kills it mid-upgrade.
        "startupProbe": {"enabled": True, "failureThreshold": 90, "periodSeconds": 10},
        # Stop sends SIGTERM; itzg's runner then sends "stop" and waits up to
        # STOP_DURATION (60s) for the save. The k8s default grace period
        # (30s) would SIGKILL it first. extraPodSpec values go through tpl,
        # so the number is a string.
        "extraPodSpec": {"terminationGracePeriodSeconds": str(STOP_GRACE_SECONDS)},
        "podLabels": {"mcdash/world": world},
        "deploymentLabels": {"mcdash/world": world},
        "mcdash": {
            "port": port,
            "rconPort": rcon_port,
            "sourceBackup": source_backup,
            "created": created.isoformat() if created else "",
            "settings": settings_to_json(settings),
            "forgeVersion": forge_version or "",
        },
    }
    tag = java_tag_for(settings.version)
    if tag:
        values["image"] = {"tag": tag}
    if cfg.world_node:
        values["nodeSelector"] = {"kubernetes.io/hostname": cfg.world_node}
    return values


# ---------------------------------------------------------------- helm CLI


class Helm:
    """Runs ``helm`` against the vendored itzg chart in one namespace."""

    def __init__(
        self, namespace: str, chart: Path | str, helm_bin: str = "helm"
    ) -> None:
        self.namespace = namespace
        self.chart = str(chart)
        self.helm_bin = helm_bin

    def _run(self, *args: str, timeout: float = 120) -> str:
        cmd = [self.helm_bin, *args, "--namespace", self.namespace]
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HelmError(f"helm {args[0]} failed: {e}") from e
        if res.returncode != 0:
            err = (res.stderr or res.stdout).strip()
            if "not found" in err:
                raise NotFound(f"helm {args[0]}: {err}")
            raise HelmError(f"helm {args[0]} failed: {err}")
        return res.stdout

    def upgrade_install(self, release: str, values: dict[str, Any]) -> None:
        """Install or upgrade ``release`` with exactly ``values``."""
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="mcdash-values-"
        ) as f:
            json.dump(values, f)
            f.flush()
            self._run(
                "upgrade",
                "--install",
                release,
                self.chart,
                "--reset-values",
                "-f",
                f.name,
            )

    def uninstall(self, release: str) -> None:
        with contextlib.suppress(NotFound):
            self._run("uninstall", release)

    def list(self) -> list[dict[str, Any]]:
        out = self._run("list", "--all", "-o", "json")
        data = json.loads(out or "[]")
        return data if isinstance(data, list) else []

    def get_values(self, release: str) -> dict[str, Any]:
        out = self._run("get", "values", release, "-o", "json")
        data = json.loads(out or "{}")
        return data if isinstance(data, dict) else {}
