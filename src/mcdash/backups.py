"""Index the three backup tiers (archive, promoted backups, snapshots).

Backup ids are ``"<tier>/<path relative to the tier root>"``. Detection results
are cached in ``index.json`` keyed by id and invalidated on (size, mtime).
"""

from __future__ import annotations

import logging
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mcdash.config import Config
from mcdash.detect import DETECT_VERSION, Detection, detect_zip
from mcdash.models import Backup, Forbidden, NotFound, Tier
from mcdash.naming import format_compact_timestamp, parse_backup_name
from mcdash.store import Store, atomic_write_json, read_json

log = logging.getLogger(__name__)


def _hidden(rel: PurePosixPath) -> bool:
    return any(part.startswith(".") for part in rel.parts)


class BackupIndex:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self._lock = threading.Lock()
        self._detections: dict[str, Detection] = {}

    # ----- paths
    def tier_root(self, tier: Tier) -> Path:
        return {
            Tier.ARCHIVE: self.cfg.archive_dir,
            Tier.BACKUP: self.cfg.backups_dir,
            Tier.SNAPSHOT: self.cfg.snapshots_dir,
        }[tier]

    def split_id(self, backup_id: str) -> tuple[Tier, str]:
        tier_s, _, rel = backup_id.partition("/")
        try:
            tier = Tier(tier_s)
        except ValueError:
            raise NotFound(f"unknown backup tier in {backup_id!r}") from None
        relp = PurePosixPath(rel)
        if (
            not rel
            or relp.is_absolute()
            or ".." in relp.parts
            or not rel.lower().endswith(".zip")
        ):
            raise NotFound(f"bad backup id {backup_id!r}")
        return tier, rel

    def path_of(self, backup_id: str) -> Path:
        tier, rel = self.split_id(backup_id)
        p = self.tier_root(tier) / rel
        if not p.is_file():
            raise NotFound(f"backup {backup_id} not found")
        return p

    def job_path_of(self, backup_id: str) -> str:
        """Where a seed Job sees this backup (archive at /archive, store at /store)."""
        tier, rel = self.split_id(backup_id)
        if tier is Tier.ARCHIVE:
            return f"{self.cfg.job_archive_mount}/{rel}"
        sub = "backups" if tier is Tier.BACKUP else "snapshots"
        return f"{self.cfg.job_store_mount}/{sub}/{rel}"

    # ----- scanning
    def _iter_files(self) -> list[tuple[Tier, str, Path]]:
        out: list[tuple[Tier, str, Path]] = []
        roots = [
            (Tier.ARCHIVE, self.cfg.archive_dir, "**/*.zip"),
            (Tier.BACKUP, self.cfg.backups_dir, "*.zip"),
            (Tier.SNAPSHOT, self.cfg.snapshots_dir, "*/*.zip"),
        ]
        for tier, root, pattern in roots:
            if not root.is_dir():
                continue
            for p in sorted(root.glob(pattern)):
                rel = PurePosixPath(p.relative_to(root).as_posix())
                if _hidden(rel) or not p.is_file():
                    continue
                out.append((tier, rel.as_posix(), p))
        return out

    def detection(self, backup_id: str) -> Detection:
        """Detection for one backup (cached)."""
        path = self.path_of(backup_id)
        with self._lock:
            index = self.store.load_index()
            det = self._detect_cached(backup_id, path, index)
            self._save_index_if_changed(index)
        return det

    def _detect_cached(self, bid: str, path: Path, index: dict[str, Any]) -> Detection:
        st = path.stat()
        entry = index.get(bid)
        if (
            isinstance(entry, dict)
            and entry.get("size") == st.st_size
            and entry.get("mtime") == st.st_mtime
            and entry.get("detect_version") == DETECT_VERSION
            and isinstance(entry.get("detection"), dict)
        ):
            return Detection.from_json(entry["detection"])
        try:
            det = detect_zip(path)
        except Exception as e:  # corrupt / unreadable zip
            log.warning("cannot inspect %s: %s", path, e)
            det = Detection(prefix=None, world_dir=None)
        index[bid] = {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "detect_version": DETECT_VERSION,
            "detection": det.to_json(),
        }
        index["__dirty__"] = True
        return det

    def _save_index_if_changed(self, index: dict[str, Any]) -> None:
        if index.pop("__dirty__", False):
            try:
                self.store.save_index(index)
            except OSError as e:
                log.warning("cannot save index cache: %s", e)

    def scan(self) -> list[Backup]:
        labels = self.store.labels()
        out: list[Backup] = []
        with self._lock:
            index = self.store.load_index()
            seen: set[str] = set()
            for tier, rel, path in self._iter_files():
                bid = f"{tier.value}/{rel}"
                seen.add(bid)
                try:
                    det = self._detect_cached(bid, path, index)
                    out_b = self._make_backup(tier, rel, path, det, labels)
                except OSError as e:
                    log.warning("skipping %s: %s", path, e)
                    continue
                if out_b is not None:
                    out.append(out_b)
            for stale in [k for k in index if k not in seen and k != "__dirty__"]:
                del index[stale]
                index["__dirty__"] = True
            self._save_index_if_changed(index)
        out.sort(key=sort_key)
        return out

    def _make_backup(
        self,
        tier: Tier,
        rel: str,
        path: Path,
        det: Detection,
        labels: dict[str, dict[str, str]],
    ) -> Backup | None:
        if not det.valid:
            return None
        st = path.stat()
        world, ts = parse_backup_name(rel, st.st_mtime)
        label = description = ""
        if tier is Tier.BACKUP:
            side = read_json(path.with_suffix(".json"), {})
            if isinstance(side, dict):
                label = str(side.get("label") or side.get("name") or "")
                description = str(side.get("description") or "")
                world = str(side.get("world") or world)
        bid = f"{tier.value}/{rel}"
        if bid in labels:
            label = labels[bid].get("label", label) or label
            description = labels[bid].get("description", description) or description
        return Backup(
            id=bid,
            tier=tier,
            world_name=world,
            timestamp=ts,
            size_bytes=st.st_size,
            server_type=det.server_type,
            mc_version=det.mc_version,
            properties=dict(det.properties),
            label=label,
            description=description,
        )

    def get(self, backup_id: str) -> Backup:
        tier, rel = self.split_id(backup_id)
        path = self.path_of(backup_id)
        det = self.detection(backup_id)
        b = self._make_backup(tier, rel, path, det, self.store.labels())
        if b is None:
            raise NotFound(f"{backup_id} does not contain a world/level.dat")
        return b

    def snapshots_of(self, world: str) -> list[Backup]:
        return [
            b
            for b in self.scan()
            if b.tier is Tier.SNAPSHOT and b.id.split("/")[1] == world
        ]

    # ----- mutation (store tiers only)
    def set_label(self, backup_id: str, label: str, description: str) -> None:
        tier, _ = self.split_id(backup_id)
        path = self.path_of(backup_id)
        if tier is Tier.BACKUP:
            side = path.with_suffix(".json")
            data = read_json(side, {})
            data = data if isinstance(data, dict) else {}
            data.update({"label": label, "name": label, "description": description})
            atomic_write_json(side, data, self.store.guard)
        self.store.set_label(backup_id, label, description)

    def promote(self, snapshot_id: str, label: str, description: str) -> Backup:
        tier, rel = self.split_id(snapshot_id)
        if tier is not Tier.SNAPSHOT:
            raise Forbidden("only snapshots can be promoted")
        src = self.path_of(snapshot_id)
        world, ts = parse_backup_name(rel, src.stat().st_mtime)
        ts = ts or datetime.now(UTC)
        stamp = ts.astimezone(UTC).strftime("%Y-%m-%d_%H_%M_%S")
        dest_dir = self.cfg.backups_dir
        self.store.guard.check(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = f"{stamp}-{world}.zip"
        n = 1
        while (dest_dir / name).exists():
            n += 1
            name = f"{stamp}-{world}({n}).zip"
        dest = dest_dir / name
        tmp = dest_dir / f".{name}.tmp"
        self.store.guard.check(dest)
        try:
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        atomic_write_json(
            dest.with_suffix(".json"),
            {
                "label": label,
                "name": label,
                "description": description,
                "world": world,
                "source": snapshot_id,
                "promoted": format_compact_timestamp(datetime.now(UTC)),
            },
            self.store.guard,
        )
        return self.get(f"{Tier.BACKUP.value}/{name}")

    def delete(self, backup_id: str) -> None:
        tier, _ = self.split_id(backup_id)
        if tier is Tier.ARCHIVE:
            raise Forbidden("archive backups can never be deleted")
        path = self.path_of(backup_id)
        self.store.guard.check(path)
        path.unlink()
        if tier is Tier.BACKUP:
            path.with_suffix(".json").unlink(missing_ok=True)
        self.store.drop_label(backup_id)

    def prune_snapshots(self, world: str, keep: int) -> list[str]:
        """Delete all but the newest ``keep`` snapshots of ``world``."""
        snaps = self.snapshots_of(world)
        snaps.sort(key=lambda b: b.timestamp or datetime.min.replace(tzinfo=UTC))
        doomed = snaps[: max(0, len(snaps) - keep)]
        for b in doomed:
            self.delete(b.id)
        return [b.id for b in doomed]


_TIER_ORDER = {Tier.ARCHIVE: 1, Tier.BACKUP: 0, Tier.SNAPSHOT: 2}


def sort_key(b: Backup) -> tuple[Any, ...]:
    """World, then newest first."""
    ts = b.timestamp.timestamp() if b.timestamp else 0.0
    return (b.world_name.lower(), -ts, _TIER_ORDER[b.tier], b.id)
