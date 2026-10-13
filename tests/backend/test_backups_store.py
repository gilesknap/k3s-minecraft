import json
import os
from pathlib import Path

import pytest

from mcdash import backups
from mcdash.backups import BackupIndex
from mcdash.config import Config
from mcdash.models import Forbidden, NotFound, Player, ServerType, Tier
from mcdash.store import ArchiveWriteError, Store, WriteGuard, atomic_write_json

from .helpers import make_world_zip


@pytest.fixture
def idx(cfg: Config) -> BackupIndex:
    return BackupIndex(cfg, Store(cfg.dashboard_dir, cfg.archive_dir))


def test_scan_all_tiers(cfg: Config, idx: BackupIndex, archive_zips) -> None:
    make_world_zip(cfg.backups_dir / "2026-01-01_00_00_00-king-ghidorah.zip")
    (cfg.backups_dir / "2026-01-01_00_00_00-king-ghidorah.json").write_text(
        json.dumps({"name": "Best", "description": "the good one"})
    )
    make_world_zip(cfg.snapshots_dir / "king-ghidorah" / "20261011T120000Z.zip")
    (cfg.archive_dir / "junk.zip").write_bytes(b"not a zip")
    (cfg.archive_dir / "notes.txt").write_text("x")

    bs = idx.scan()
    ids = [b.id for b in bs]
    assert ids == [
        "archive/2021-06-03-10_30_33-fire-ice2.zip",
        "snapshot/king-ghidorah/20261011T120000Z.zip",
        "backup/2026-01-01_00_00_00-king-ghidorah.zip",
        "archive/2021-05-15-21_56_20-king-ghidorah(1).zip",
    ]
    fire = bs[0]
    assert fire.server_type is ServerType.FORGE and fire.mc_version == "1.16.5"
    promoted = bs[2]
    assert promoted.tier is Tier.BACKUP
    assert (promoted.label, promoted.description) == ("Best", "the good one")
    assert bs[1].world_name == "king-ghidorah"
    # cache written; never into the archive
    assert idx.store.index_path.is_file()
    assert set(os.listdir(cfg.archive_dir)) == {
        "2021-05-15-21_56_20-king-ghidorah(1).zip",
        "2021-06-03-10_30_33-fire-ice2.zip",
        "junk.zip",
        "notes.txt",
    }


def test_detection_cache_invalidates(cfg: Config, idx: BackupIndex) -> None:
    p = make_world_zip(cfg.archive_dir / "2021-01-01-a.zip", version="1.12.2")
    assert idx.scan()[0].mc_version == "1.12.2"
    make_world_zip(p, version="1.18.2")
    os.utime(p, (1e9, 1e9))
    assert idx.scan()[0].mc_version == "1.18.2"


def test_detection_cache_invalidates_on_detect_version(
    cfg: Config, idx: BackupIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_world_zip(cfg.archive_dir / "2021-01-01-a.zip", version="1.12.2")
    assert idx.scan()[0].mc_version == "1.12.2"
    calls: list[Path] = []
    real = backups.detect_zip
    monkeypatch.setattr(backups, "detect_zip", lambda p: calls.append(p) or real(p))
    idx.scan()
    assert calls == []  # cached
    monkeypatch.setattr(backups, "DETECT_VERSION", backups.DETECT_VERSION + 1)
    idx.scan()
    assert len(calls) == 1


def test_bad_ids(idx: BackupIndex) -> None:
    for bad in ["nope/x.zip", "archive/../x.zip", "archive/", "archive/x.txt"]:
        with pytest.raises(NotFound):
            idx.get(bad)


def test_labels_promote_delete(cfg: Config, idx: BackupIndex, archive_zips) -> None:
    aid = "archive/2021-05-15-21_56_20-king-ghidorah(1).zip"
    idx.set_label(aid, "Original", "first ever")
    assert idx.get(aid).label == "Original"
    assert not any(p.suffix == ".json" for p in cfg.archive_dir.iterdir())
    with pytest.raises(Forbidden):
        idx.delete(aid)

    make_world_zip(cfg.snapshots_dir / "king-ghidorah" / "20261011T120000Z.zip")
    sid = "snapshot/king-ghidorah/20261011T120000Z.zip"
    with pytest.raises(Forbidden):
        idx.promote(aid, "x", "y")
    b = idx.promote(sid, "Castle done", "after the castle")
    assert b.id == "backup/2026-10-11_12_00_00-king-ghidorah.zip"
    assert (b.label, b.world_name) == ("Castle done", "king-ghidorah")
    b2 = idx.promote(sid, "again", "")
    assert b2.id == "backup/2026-10-11_12_00_00-king-ghidorah(2).zip"
    assert b2.world_name == "king-ghidorah"

    idx.set_label(b.id, "Renamed", "d")
    side = json.loads(
        (cfg.backups_dir / Path(b.id).name).with_suffix(".json").read_text()
    )
    assert side["label"] == "Renamed"
    idx.delete(b.id)
    assert not (cfg.backups_dir / Path(b.id).name).exists()
    assert not (cfg.backups_dir / Path(b.id).name).with_suffix(".json").exists()


def test_prune(cfg: Config, idx: BackupIndex) -> None:
    for h in range(5):
        make_world_zip(cfg.snapshots_dir / "w" / f"20261011T1{h}0000Z.zip")
    gone = idx.prune_snapshots("w", 3)
    assert gone == [
        "snapshot/w/20261011T100000Z.zip",
        "snapshot/w/20261011T110000Z.zip",
    ]
    assert len(idx.snapshots_of("w")) == 3


def test_archive_guard(cfg: Config, tmp_path: Path) -> None:
    guard = WriteGuard(cfg.archive_dir)
    with pytest.raises(ArchiveWriteError):
        atomic_write_json(cfg.archive_dir / "x.json", {}, guard)
    with pytest.raises(ArchiveWriteError):
        atomic_write_json(cfg.archive_dir / "sub" / "x.json", {}, guard)
    # symlink into the archive is resolved and refused too
    link = tmp_path / "sneaky"
    link.symlink_to(cfg.archive_dir)
    with pytest.raises(ArchiveWriteError):
        atomic_write_json(link / "x.json", {}, guard)
    atomic_write_json(tmp_path / "ok.json", {"a": 1}, guard)
    assert list(cfg.archive_dir.iterdir()) == []

    # a store pointed at the archive cannot write anything
    bad = Store(cfg.archive_dir / "dashboard", cfg.archive_dir)
    with pytest.raises(ArchiveWriteError):
        bad.audit("me", "x", "y")
    with pytest.raises(ArchiveWriteError):
        bad.set_roster([Player("noah")])


def test_store_roster_and_audit(cfg: Config) -> None:
    s = Store(cfg.dashboard_dir, cfg.archive_dir)
    assert s.roster() == []
    s.set_roster([Player("noah", "son"), Player("Noah"), Player("giles"), Player(" ")])
    assert [p.name for p in s.roster()] == ["noah", "giles"]
    s.audit("a@b", "launch", "w", port=1)
    s.audit("", "stop", "w")
    recs = s.read_audit()
    assert [r["action"] for r in recs] == ["launch", "stop"]
    assert recs[0]["actor"] == "a@b" and recs[1]["actor"] == "unknown"
    assert not list(cfg.dashboard_dir.glob(".*.tmp"))


def test_atomic_write_is_world_readable(cfg: Config) -> None:
    f = cfg.dashboard_dir / "x.json"
    atomic_write_json(f, {"a": 1}, WriteGuard(cfg.archive_dir))
    assert f.stat().st_mode & 0o777 == 0o644
