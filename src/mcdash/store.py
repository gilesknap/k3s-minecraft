"""JSON state under ``$MCD_STORE_DIR/dashboard/`` — no database.

Files: ``roster.json``, ``labels.json``, ``index.json`` (detection cache) and
``audit.jsonl`` (append-only). Every write is tmp + rename, and every write
path is checked against the archive guard: nothing may ever be written under
the archive directory.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcdash.models import Forbidden, Player


class ArchiveWriteError(Forbidden):
    """Attempt to write into the read-only archive tier."""


class WriteGuard:
    """Refuses writes to any path inside one of the protected roots."""

    def __init__(self, *protected: Path) -> None:
        self.protected = [Path(os.path.realpath(p)) for p in protected]

    def check(self, path: Path) -> None:
        real = Path(os.path.realpath(path))
        for root in self.protected:
            if real == root or root in real.parents:
                raise ArchiveWriteError(f"refusing to write into the archive: {path}")


def atomic_write_bytes(path: Path, data: bytes, guard: WriteGuard) -> None:
    guard.check(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    guard.check(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, obj: Any, guard: WriteGuard) -> None:
    data = json.dumps(obj, indent=2, sort_keys=True, default=str).encode()
    atomic_write_bytes(path, data + b"\n", guard)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


class Store:
    """Dashboard state files. Thread-safe within one process."""

    def __init__(self, dashboard_dir: Path, archive_dir: Path) -> None:
        self.dir = dashboard_dir
        self.guard = WriteGuard(archive_dir)
        self._lock = threading.RLock()

    # paths
    @property
    def roster_path(self) -> Path:
        return self.dir / "roster.json"

    @property
    def labels_path(self) -> Path:
        return self.dir / "labels.json"

    @property
    def index_path(self) -> Path:
        return self.dir / "index.json"

    @property
    def audit_path(self) -> Path:
        return self.dir / "audit.jsonl"

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock:
            yield

    def write_json(self, path: Path, obj: Any) -> None:
        with self._lock:
            atomic_write_json(path, obj, self.guard)

    # roster
    def roster(self) -> list[Player]:
        data = read_json(self.roster_path, [])
        out: list[Player] = []
        for p in data if isinstance(data, list) else []:
            if isinstance(p, dict) and p.get("name"):
                out.append(Player(name=str(p["name"]), note=str(p.get("note", ""))))
        return out

    def set_roster(self, players: list[Player]) -> None:
        seen: set[str] = set()
        rows = []
        for p in players:
            name = p.name.strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                rows.append({"name": name, "note": p.note})
        self.write_json(self.roster_path, rows)

    # labels (keyed by backup id)
    def labels(self) -> dict[str, dict[str, str]]:
        data = read_json(self.labels_path, {})
        return data if isinstance(data, dict) else {}

    def set_label(self, backup_id: str, label: str, description: str) -> None:
        with self._lock:
            data = self.labels()
            if label or description:
                data[backup_id] = {"label": label, "description": description}
            else:
                data.pop(backup_id, None)
            self.write_json(self.labels_path, data)

    def drop_label(self, backup_id: str) -> None:
        with self._lock:
            data = self.labels()
            if data.pop(backup_id, None) is not None:
                self.write_json(self.labels_path, data)

    # detection cache
    def load_index(self) -> dict[str, Any]:
        data = read_json(self.index_path, {})
        return data if isinstance(data, dict) else {}

    def save_index(self, index: dict[str, Any]) -> None:
        self.write_json(self.index_path, index)

    # audit log
    def audit(self, actor: str, action: str, target: str, **detail: Any) -> None:
        rec = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "actor": actor or "unknown",
            "action": action,
            "target": target,
            **detail,
        }
        line = json.dumps(rec, default=str, sort_keys=True) + "\n"
        with self._lock:
            self.guard.check(self.audit_path)
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(line)

    def read_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        try:
            lines = self.audit_path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out
