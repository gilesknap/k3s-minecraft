"""``mcdash serve`` and ``mcdash index <dir>``."""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

from mcdash.config import Config


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    try:
        from mcdash.web.app import create_app
    except ImportError as e:
        print(f"mcdash serve: web UI unavailable: {e}", file=sys.stderr)
        return 1
    from mcdash.service import KubeDashboardService

    cfg = Config.from_env()
    service = KubeDashboardService.from_config(cfg)
    service.start_reconciler()
    app = create_app(service)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        service.stop_reconciler()
    return 0


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _index(args: argparse.Namespace) -> int:
    from mcdash.backups import BackupIndex
    from mcdash.store import Store

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    # Treat <dir> as an archive tier; keep the cache out of it (read-only use).
    with tempfile.TemporaryDirectory(prefix="mcdash-index-") as tmp:
        cfg = Config(archive_dir=root, store_dir=Path(tmp))
        idx = BackupIndex(cfg, Store(cfg.dashboard_dir, cfg.archive_dir))
        backups = idx.scan()
    rows = [
        (
            b.world_name,
            b.timestamp.strftime("%Y-%m-%d %H:%M") if b.timestamp else "?",
            b.server_type.value,
            b.mc_version or "?",
            _fmt_size(b.size_bytes),
            ",".join(
                f"{k}={b.properties[k]}"
                for k in ("gamemode", "difficulty", "enable-command-block")
                if k in b.properties
            ),
            b.id,
        )
        for b in backups
    ]
    hdr = ("WORLD", "DATE", "TYPE", "VERSION", "SIZE", "PROPERTIES", "ID")
    widths = [max(len(str(r[i])) for r in [hdr, *rows]) for i in range(len(hdr))]
    for r in [hdr, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)))
    skipped = len(list(root.rglob("*.zip"))) - len(rows)
    if skipped:
        print(f"\n{skipped} zip(s) without world/level.dat skipped", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="mcdash")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the dashboard web server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=_serve)
    i = sub.add_parser("index", help="print detected backups in a directory")
    i.add_argument("dir")
    i.set_defaults(func=_index)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
