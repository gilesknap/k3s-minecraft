"""Seed a world PVC from a backup zip (runs inside a Job pod).

Locates ``world/level.dat`` at any prefix, strips the prefix, wipes the
destination and extracts either the vanilla subset or the full ``/data``
minus regenerable dirs. Guards against zip-slip and skips symlink entries.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from mcdash.detect import find_world

EXCLUDED_TOP = {"libraries", "versions", "logs", ".cache", "?"}
# Loader install markers: if kept without libraries/, itzg would skip the
# (re)install and the server could not start.
INSTALL_MARKERS = re.compile(r"^\.(forge|fabric|neoforge|install)[-_.]")


def excluded_top(top: str) -> bool:
    """True for top-level /data entries that are regenerable."""
    return (
        top in EXCLUDED_TOP
        or top.endswith(".tmp")
        or top == MARKER
        or bool(INSTALL_MARKERS.match(top))
    )


VANILLA_FILES = re.compile(r"^(ops\.json|whitelist\.json|banned-[^/]*\.json)$")
MARKER = ".mcdash-seed.json"


class SeedError(Exception):
    pass


def safe_relpath(rel: str) -> PurePosixPath | None:
    """Normalise a zip member path; ``None`` if it could escape the dest."""
    rel = rel.replace("\\", "/")
    p = PurePosixPath(rel)
    if p.is_absolute() or rel.startswith("/"):
        return None
    parts = [x for x in p.parts if x not in ("", ".")]
    if not parts or any(x == ".." for x in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return PurePosixPath(*parts)


def map_member(name: str, prefix: str, world_dir: str, mode: str) -> str | None:
    """Destination path (relative to /data) for a zip member, or None to skip."""
    if name.startswith(world_dir):
        rest = name[len(world_dir) :]
        return "world/" + rest if rest else "world/"
    if not name.startswith(prefix):
        return None
    rel = name[len(prefix) :]
    if not rel:
        return None
    if world_dir != prefix + "world/" and rel.split("/")[0] == "world":
        return None  # single-player layout: don't merge a stray world/
    if mode == "vanilla":
        return rel if VANILLA_FILES.match(rel) else None
    top = rel.split("/")[0]
    if excluded_top(top):
        return None
    return rel


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def wipe(dest: Path) -> None:
    for child in dest.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _chown_tree(root: Path, uid: int, gid: int) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        os.chown(dirpath, uid, gid)
        for n in dirnames + filenames:
            os.lchown(os.path.join(dirpath, n), uid, gid)


def seed(src: Path, dest: Path, mode: str, uid: int | None, gid: int | None) -> int:
    """Extract ``src`` into ``dest``. Returns number of files written."""
    if mode not in ("vanilla", "full"):
        raise SeedError(f"bad mode {mode!r}")
    if not dest.is_dir():
        raise SeedError(f"destination {dest} is not a directory")
    dest_real = dest.resolve()
    count = 0
    with zipfile.ZipFile(src) as zf:
        found = find_world(zf.namelist())
        if found is None:
            raise SeedError(f"{src} has no world/level.dat")
        prefix, world_dir = found
        print(f"seed: prefix={prefix!r} world_dir={world_dir!r} mode={mode}")
        wipe(dest)
        for info in zf.infolist():
            target = map_member(info.filename, prefix, world_dir, mode)
            if target is None:
                continue
            rel = safe_relpath(target)
            if rel is None:
                print(f"seed: skipping unsafe path {info.filename!r}")
                continue
            if _is_symlink(info):
                print(f"seed: skipping symlink {info.filename!r}")
                continue
            out = dest / rel
            # belt and braces: resolved parent must stay inside dest
            parent = out.parent
            parent.mkdir(parents=True, exist_ok=True)
            if not parent.resolve().is_relative_to(dest_real):
                raise SeedError(f"path escapes destination: {info.filename!r}")
            if info.is_dir() or target.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
                continue
            with zf.open(info) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
            count += 1
    if not (dest / "world" / "level.dat").is_file():
        raise SeedError("seeded data has no world/level.dat")
    (dest / MARKER).write_text(
        json.dumps(
            {
                "source": str(src),
                "mode": mode,
                "files": count,
                "seeded": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )
    )
    if uid is not None and gid is not None and os.geteuid() == 0:
        try:
            _chown_tree(dest, uid, gid)
        except OSError as e:  # e.g. unmapped uid in a user namespace
            print(f"seed: warning: cannot chown to {uid}:{gid}: {e}")
    print(f"seed: wrote {count} files to {dest}")
    return count
