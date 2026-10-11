"""``python -m mcdash.jobs seed|snapshot ...`` — entry point for Job pods."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mcdash.jobs.seed import SeedError, seed
from mcdash.jobs.snapshot import SnapshotError, snapshot


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
        if args.cmd == "seed":
            uid = int(os.environ.get("MCD_UID", "1000"))
            gid = int(os.environ.get("MCD_GID", "1000"))
            seed(args.src, args.dest, args.mode, uid, gid)
        else:
            snapshot(args.src, args.dest)
    except (SeedError, SnapshotError, OSError, ValueError) as e:
        print(f"{args.cmd}: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
