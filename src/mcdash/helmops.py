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
from mcdash.models import MCDashError, NotFound, ServerType, WorldSettings

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


def _version_tuple(v: str) -> tuple[int, ...] | None:
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
    t = _version_tuple(version)
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
    t = _version_tuple(version or "")
    table = (
        _LEVEL_TYPE_LEGACY if t is not None and t < (1, 19, 0) else _LEVEL_TYPE_MODERN
    )
    return table.get(level_type)


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
    if "whitelist" in kw:
        kw["whitelist"] = [str(x) for x in kw["whitelist"] or []]
    if "open_to_all" in kw:
        kw["open_to_all"] = kw["open_to_all"] is True
    return WorldSettings(**kw)


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
) -> dict[str, Any]:
    """itzg chart values for one world (contract: docs/contracts.md)."""
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
    if settings.motd:
        ms["motd"] = settings.motd
    # itzg enables the whitelist whenever WHITELIST is set, so an open world
    # must not pass the names at all.
    if settings.whitelist and not settings.open_to_all:
        ms["whitelist"] = ",".join(settings.whitelist)
    if settings.server_type is ServerType.FORGE and forge_version:
        ms["forgeVersion"] = forge_version
    # Only used when the server generates a world (create_world); inert on a
    # volume that already holds one.
    if settings.seed:
        ms["levelSeed"] = settings.seed
    level_type = level_type_for(settings.level_type, settings.version)
    if level_type:
        ms["levelType"] = level_type

    whitelisted = not settings.open_to_all
    extra_env: dict[str, Any] = {
        "ENABLE_WHITELIST": "TRUE" if whitelisted else "FALSE",
        "ENFORCE_WHITELIST": "TRUE" if whitelisted else "FALSE",
        # with a list, make whitelist.json match it; else keep the file as is
        # (the backup's, or the names an open world will need if closed again)
        "EXISTING_WHITELIST_FILE": (
            "SYNCHRONIZE" if settings.whitelist and whitelisted else "SKIP"
        ),
        "OVERRIDE_SERVER_PROPERTIES": "TRUE",
        # not minecraftServer.maxWorldSize: the schema wants an int, which helm
        # renders as 2.9999984e+07 (float64) — pass the env var as a string
        "MAX_WORLD_SIZE": str(int(settings.max_world_size)),
    }

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
        "podLabels": {"mcdash/world": world},
        "deploymentLabels": {"mcdash/world": world},
        "mcdash": {
            "port": port,
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
