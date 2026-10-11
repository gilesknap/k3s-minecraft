"""``python -m mcdash.jobs seed|snapshot ...`` — entry point for Job pods."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcdash.jobs.seed import SeedError, seed
from mcdash.jobs.snapshot import SnapshotError, snapshot
from mcdash.store import ArchiveWriteError, WriteGuard


def archive_guard() -> WriteGuard:
    """The archive is never a write target, whatever the arguments say.

    Seeding wipes its destination first, so a mistaken ``--dest`` under the
    archive must be refused before anything touches the disk (the readOnly
    mount is the second line of defence).
    """
    roots = {Path("/archive")}
    if os.environ.get("MCD_ARCHIVE_DIR"):
        roots.add(Path(os.environ["MCD_ARCHIVE_DIR"]))
    return WriteGuard(*roots)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mcdash.jobs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("seed", help="extract a backup zip into a world volume")
    s.add_argument("--src", required=True, type=Path)
    s.add_argument("--dest", required=True, type=Path)
    s.add_argument("--mode", choices=["vanilla", "full"], default="vanilla")
    n = sub.add_parser("snapshot", help="zip a world volume into the store")
    n.add_argument("--src", required=True, type=Path)
    n.add_argument("--dest", required=True, type=Path)
    args = ap.parse_args(argv)

    try:
        archive_guard().check(args.dest)
        if args.cmd == "seed":
            uid = int(os.environ.get("MCD_UID", "1000"))
            gid = int(os.environ.get("MCD_GID", "1000"))
            seed(args.src, args.dest, args.mode, uid, gid)
        else:
            snapshot(args.src, args.dest)
    except (SeedError, SnapshotError, ArchiveWriteError, OSError, ValueError) as e:
        print(f"{args.cmd}: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
