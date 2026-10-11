"""Zip a world's ``/data`` into the snapshots tier (runs inside a Job pod).

The zip root is the ``/data`` layout (no prefix), which the seeder accepts.
Written to a temp name in the destination dir, then renamed.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

from mcdash.jobs.seed import excluded_top


class SnapshotError(Exception):
    pass


def snapshot(src: Path, dest: Path) -> int:
    """Write ``dest`` (a .zip) from ``src``. Returns number of files."""
    if not (src / "world" / "level.dat").is_file():
        raise SnapshotError(f"{src} has no world/level.dat")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.tmp"
    count = 0
    try:
        with zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            for dirpath, dirnames, filenames in os.walk(src):
                rel_dir = Path(dirpath).relative_to(src)
                if rel_dir == Path("."):
                    dirnames[:] = [d for d in dirnames if not excluded_top(d)]
                dirnames.sort()
                if rel_dir != Path("."):
                    zf.write(dirpath, rel_dir.as_posix() + "/")
                for fn in sorted(filenames):
                    if rel_dir == Path(".") and excluded_top(fn):
                        continue
                    full = Path(dirpath) / fn
                    if full.is_symlink() or not full.is_file():
                        continue
                    arc = (rel_dir / fn).as_posix()
                    try:
                        zf.write(full, arc)
                    except FileNotFoundError:
                        continue  # vanished while saving (e.g. temp files)
                    count += 1
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"snapshot: wrote {count} files to {dest}")
    return count
