"""Inspect a world backup zip without extracting it.

Only the zip central directory plus two small members (``level.dat`` and
``server.properties``) are read, so this is fast even on multi-GB zips.
"""

from __future__ import annotations

import gzip
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcdash.models import ServerType

MAX_SMALL_MEMBER = 32 * 1024 * 1024  # refuse to read bigger level.dat/properties

_FORGE_JAR = re.compile(
    r"^forge-(?P<mc>\d+(?:\.\d+)+)-(?P<forge>[\w.\-]+?)(?:-universal|-server)?\.jar$"
)
_FORGE_LIB = re.compile(
    r"^libraries/net/minecraftforge/forge/(?P<mc>\d+(?:\.\d+)+)-(?P<forge>[^/]+)/"
)
_VERSION_DIR = re.compile(r"^versions/(?P<v>[^/]+)/")


@dataclass
class Detection:
    """What we learned about a backup zip."""

    prefix: str | None  # root of the /data layout inside the zip ("" or "a/b/")
    world_dir: str | None  # zip dir holding level.dat ("<prefix>world/")
    server_type: ServerType = ServerType.VANILLA
    mc_version: str | None = None
    forge_version: str | None = None
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.world_dir is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "prefix": self.prefix,
            "world_dir": self.world_dir,
            "server_type": self.server_type.value,
            "mc_version": self.mc_version,
            "forge_version": self.forge_version,
            "properties": self.properties,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Detection:
        return cls(
            prefix=d.get("prefix"),
            world_dir=d.get("world_dir"),
            server_type=ServerType(d.get("server_type", "VANILLA")),
            mc_version=d.get("mc_version"),
            forge_version=d.get("forge_version"),
            properties=dict(d.get("properties") or {}),
        )


def version_key(v: str) -> tuple[int, ...]:
    """Sort key for ``1.16.5`` style versions (non-numeric parts sort low)."""
    parts: list[int] = []
    for p in re.split(r"[.\-]", v):
        parts.append(int(p) if p.isdigit() else -1)
    return tuple(parts)


def find_world(names: list[str]) -> tuple[str, str] | None:
    """Return ``(prefix, world_dir)`` for the shallowest ``level.dat``.

    Prefers a ``world/level.dat`` (server layout); otherwise any
    ``<dir>/level.dat`` (single-player save layout), whose dir is remapped to
    ``world/`` by the seeder.
    """
    server: list[str] = []
    other: list[str] = []
    for n in names:
        if n.endswith("/") or "__MACOSX/" in n:
            continue
        if n == "world/level.dat" or n.endswith("/world/level.dat"):
            server.append(n[: -len("level.dat")])
        elif n == "level.dat" or n.endswith("/level.dat"):
            other.append(n[: -len("level.dat")])
    if server:
        wd = min(server, key=lambda s: (s.count("/"), s))
        return wd[: -len("world/")], wd
    if other:
        wd = min(other, key=lambda s: (s.count("/"), s))
        parent = wd.rstrip("/").rpartition("/")[0]
        prefix = parent + "/" if parent else ""
        return prefix, wd
    return None


def parse_properties(text: str) -> dict[str, str]:
    """Parse a Java ``server.properties`` file (simple key=value subset)."""
    props: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"((?:\\.|[^=:\s\\])+)\s*[=:\s]\s*(.*)$", line)
        if m:
            key = m.group(1).replace("\\", "")
            val = re.sub(r"\\(.)", r"\1", m.group(2))
            props[key] = val
        else:
            props[line] = ""
    return props


def _read_small(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_SMALL_MEMBER:
        return None
    with zf.open(info) as f:
        return f.read(MAX_SMALL_MEMBER + 1)[:MAX_SMALL_MEMBER]


def level_dat_version(data: bytes) -> str | None:
    """``Data.Version.Name`` from a (gzipped) level.dat, if present."""
    import nbtlib

    try:
        if data[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
                data = g.read(MAX_SMALL_MEMBER)
        nbt = nbtlib.File.parse(io.BytesIO(data))
        root: Any = nbt
        if "Data" not in root and len(root) == 1:  # tolerate a named wrapper
            root = next(iter(root.values()))
        name = root["Data"]["Version"]["Name"]
        return str(name)
    except Exception:  # untrusted data, best effort
        return None


def detect_zip(path: str | Path) -> Detection:
    """Detect layout, server type and version of a backup zip."""
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        found = find_world(names)
        if found is None:
            return Detection(prefix=None, world_dir=None)
        prefix, world_dir = found
        rel = [n[len(prefix) :] for n in names if n.startswith(prefix)]

        det = Detection(prefix=prefix, world_dir=world_dir)

        forge = False
        forge_mc: str | None = None
        versions: set[str] = set()
        for r in rel:
            if r.startswith("mods/") and r.lower().endswith(".jar"):
                forge = True
            elif "/" not in r and (m := _FORGE_JAR.match(r)):
                forge = True
                forge_mc, det.forge_version = m["mc"], m["forge"]
            elif m := _FORGE_LIB.match(r):
                forge = True
                if det.forge_version is None:
                    forge_mc, det.forge_version = m["mc"], m["forge"]
            elif r.startswith("libraries/net/minecraftforge/"):
                forge = True
            if m := _VERSION_DIR.match(r):
                versions.add(m["v"])
        if forge:
            det.server_type = ServerType.FORGE

        lvl = _read_small(zf, world_dir + "level.dat")
        if lvl is not None:
            det.mc_version = level_dat_version(lvl)
        if det.mc_version is None:
            numeric = [v for v in versions if re.fullmatch(r"\d+(?:\.\d+)+", v)]
            if numeric:
                det.mc_version = max(numeric, key=version_key)
            elif forge_mc:
                det.mc_version = forge_mc

        props = _read_small(zf, prefix + "server.properties")
        if props is not None:
            det.properties = parse_properties(props.decode("utf-8", "replace"))
        return det
