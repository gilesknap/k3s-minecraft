"""New world names: refused while a deleted or lost world still holds them.

A deleted world's folder on the world share (live, or archived by the
storage class), or a world the cluster lost but the registry still has.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from mcdash.config import Config
from mcdash.models import Conflict, MCDashError, WorldState
from mcdash.service import KubeDashboardService

from .test_registry import lost, rebuilt, skipped
from .test_service import KING, ME, launch_running


@pytest.fixture
def share(svc: KubeDashboardService, tmp_path: Path) -> Path:
    """The worlds' storage class share root, as mounted at /volumes."""
    d = tmp_path / "volumes"
    svc.cfg = replace(svc.cfg, volumes_dir=d)
    return d


def test_launch_refused_while_archived(svc: KubeDashboardService, share: Path) -> None:
    (share / "archived-minecraft" / "king-ghidorah-data").mkdir(parents=True)
    _, settings = svc.launch_defaults(KING)
    with pytest.raises(Conflict, match="still has archived data"):
        svc.launch(KING, "king-ghidorah", settings, ME)
    assert svc.list_worlds() == []
    # nothing reserved: no record, no PVC
    assert not svc.fake_kube.pvcs  # type: ignore[attr-defined]


def test_create_refused_while_archived(svc: KubeDashboardService, share: Path) -> None:
    (share / "archived-minecraft" / "new-world-data").mkdir(parents=True)
    _, settings = svc.new_world_defaults()
    with pytest.raises(Conflict, match="remove or rename the archive"):
        svc.create_world("new-world", settings, ME)


def test_refused_while_the_live_folder_is_left(
    svc: KubeDashboardService, share: Path
) -> None:
    """A delete not yet archived (or a second archive that failed)."""
    (share / "minecraft" / "new-world-data").mkdir(parents=True)
    _, settings = svc.new_world_defaults()
    with pytest.raises(Conflict, match="minecraft/new-world-data"):
        svc.create_world("new-world", settings, ME)
    _, settings = svc.launch_defaults(KING)
    (share / "minecraft" / "king-ghidorah-data").mkdir()
    with pytest.raises(Conflict, match="wait a minute"):
        svc.launch(KING, "king-ghidorah", settings, ME)


def test_suggested_names_skip_left_folders(
    svc: KubeDashboardService, share: Path
) -> None:
    (share / "archived-minecraft" / "new-world-data").mkdir(parents=True)
    (share / "minecraft" / "new-world-2-data").mkdir(parents=True)
    (share / "archived-minecraft" / "king-ghidorah-data").mkdir()
    (share / "archived-minecraft" / "unrelated").mkdir()
    assert svc.new_world_defaults()[0] == "new-world-3"
    name, settings = svc.launch_defaults(KING)
    assert name == "king-ghidorah-2"
    svc.launch(KING, name, settings, ME)  # the suggestion is accepted


def test_empty_share(svc: KubeDashboardService, share: Path) -> None:
    assert not share.exists()  # folders under the share may be missing
    assert launch_running(svc) == "king-ghidorah"


def test_unreadable_share_refuses(svc: KubeDashboardService, share: Path) -> None:
    share.mkdir()
    (share / "minecraft").write_text("not a directory")
    _, settings = svc.new_world_defaults()
    with pytest.raises(MCDashError, match="cannot check the world share"):
        svc.create_world("new-world", settings, ME)


def test_no_check_without_the_mount(svc: KubeDashboardService) -> None:
    assert svc.cfg.volumes_dir is None
    assert launch_running(svc) == "king-ghidorah"


def test_restore_needs_the_data_folder(svc: KubeDashboardService, share: Path) -> None:
    king = launch_running(svc)
    svc.store.save_registry("gone", lost(30566, 30601))
    svc.reconcile_once()
    (share / "minecraft" / f"{king}-data").mkdir(parents=True)
    # an archive of the same name does not matter: the world deletes nothing
    (share / "archived-minecraft" / f"{king}-data").mkdir(parents=True)
    new = rebuilt(svc)
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == [king]
    assert new.get_world(king).state is WorldState.RUNNING
    assert skipped(new) == {"gone"}  # would come back empty
    (entry,) = [a for a in new.store.read_audit() if a["action"] == "skip_restore"]
    assert "no data folder minecraft/gone-data" in entry["reason"]


def test_names_held_by_the_registry(svc: KubeDashboardService) -> None:
    """A lost world's name is not free, restored or set aside."""
    svc.store.save_registry("new-world", lost(30565, 30600))
    svc.store.save_registry("king-ghidorah", lost(30566, 30601))
    svc.store.skipped_dir.mkdir(parents=True)
    (svc.store.skipped_dir / "new-world-2.json").write_text("{}")
    assert svc.new_world_defaults()[0] == "new-world-3"
    assert svc.launch_defaults(KING)[0] == "king-ghidorah-2"
    _, settings = svc.new_world_defaults()
    with pytest.raises(Conflict, match=r"registry/new-world\.json"):
        svc.create_world("new-world", settings, ME)
    with pytest.raises(Conflict, match=r"registry/skipped/new-world-2\.json"):
        svc.create_world("new-world-2", settings, ME)
    _, settings = svc.launch_defaults(KING)
    with pytest.raises(Conflict, match="no longer has"):
        svc.launch(KING, "king-ghidorah", settings, ME)
    assert svc.list_worlds() == []


def test_unreadable_registry_refuses_a_name(
    svc: KubeDashboardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down() -> set[str]:
        raise OSError("store gone")

    monkeypatch.setattr(svc.store, "registry_names", down)
    name, settings = svc.new_world_defaults()  # suggestions still work
    with pytest.raises(MCDashError, match="cannot read the world registry"):
        svc.create_world(name, settings, ME)


def test_volumes_dir_env() -> None:
    assert Config.from_env({}).volumes_dir is None
    cfg = Config.from_env({"MCD_VOLUMES_DIR": "/volumes"})
    assert cfg.volumes_dir == Path("/volumes")


def test_a_wrong_mount_postpones_the_restore(
    svc: KubeDashboardService, share: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No <ns>/ on the share: every world would look lost, so nothing is
    skipped; the pass waits (one error logged) and runs once it is there."""
    _, settings = svc.new_world_defaults()
    king = launch_running(svc)
    svc.reconcile_once()
    share.mkdir()  # mounted, but empty (wrong path, or the NAS not ready)
    new = rebuilt(svc)
    for _ in range(3):
        new.reconcile_once()
    assert new.list_worlds() == [] and skipped(new) == set()
    assert king in new.store.registry_names()
    errors = [r for r in caplog.records if "restore postponed" in r.getMessage()]
    assert len(errors) == 1 and "has no minecraft/ folder" in errors[0].getMessage()
    # meanwhile a new world cannot guess at the share
    with pytest.raises(MCDashError, match="has no minecraft/ folder"):
        new.create_world("other", settings, ME)
    (share / "minecraft" / f"{king}-data").mkdir(parents=True)
    new.reconcile_once()  # the mount is right now: restored without a restart
    assert new.get_world(king).state is WorldState.RUNNING


def test_new_names_refused_while_the_share_looks_wrong(
    svc: KubeDashboardService, share: Path
) -> None:
    _, settings = svc.new_world_defaults()
    assert launch_running(svc) == "king-ghidorah"  # first world: no <ns>/ yet
    with pytest.raises(MCDashError, match="storage.volumes is the share root"):
        svc.create_world("other", settings, ME)
    with pytest.raises(MCDashError, match="has no minecraft/ folder"):
        svc.new_world_defaults()  # suggestions cannot be trusted either
    (share / "minecraft").mkdir(parents=True)
    svc.create_world("other", settings, ME)


def test_a_lost_world_counts_for_the_share_check(
    svc: KubeDashboardService, share: Path
) -> None:
    _, settings = svc.new_world_defaults()
    svc.store.save_registry("gone", lost(30565, 30600))
    with pytest.raises(MCDashError, match="has no minecraft/ folder"):
        svc.create_world("other", settings, ME)


def test_restore_looks_up_folders_without_the_lock(
    svc: KubeDashboardService, share: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    king = launch_running(svc)
    svc.reconcile_once()
    (share / "minecraft" / f"{king}-data").mkdir(parents=True)
    new = rebuilt(svc)
    held: list[bool] = []
    real = new._exists

    def exists(path: Path) -> bool:
        held.append(new._lock._is_owned())  # type: ignore[attr-defined]
        return real(path)

    monkeypatch.setattr(new, "_exists", exists)
    new.reconcile_once()
    assert new.get_world(king).state is WorldState.RUNNING
    assert held and not any(held)


def test_a_class_change_is_not_looked_up(
    svc: KubeDashboardService, share: Path
) -> None:
    """Every record on another class: skipped, even with no <ns>/ (a new share)."""
    king = launch_running(svc)
    svc.reconcile_once()
    share.mkdir()
    new = rebuilt(svc)
    new.cfg = replace(new.cfg, storage_class="nfs-new")
    new.reconcile_once()
    assert new.list_worlds() == [] and skipped(new) == {king}


def test_taken_beats_the_share_message(svc: KubeDashboardService, share: Path) -> None:
    """A live world has a folder too: say "taken", not "deleted world"."""
    king = launch_running(svc)
    (share / "minecraft" / f"{king}-data").mkdir(parents=True)
    _, settings = svc.launch_defaults(KING)
    with pytest.raises(Conflict, match="is taken$"):
        svc.launch(KING, king, settings, ME)
    with pytest.raises(Conflict, match="is taken$"):
        svc.create_world(king, settings, ME)
    # a lost world's folder: the registry's message, not "wait a minute"
    svc.store.save_registry("gone", lost(30566, 30601))
    (share / "minecraft" / "gone-data").mkdir()
    with pytest.raises(Conflict, match="no longer has"):
        svc.create_world("gone", settings, ME)


def test_an_orphan_pvc_says_how_to_free_the_name(svc: KubeDashboardService) -> None:
    svc.fake_kube.pvcs.add("half-data")  # type: ignore[attr-defined]
    _, settings = svc.new_world_defaults()
    with pytest.raises(Conflict, match="PVC half-data exists without a world record"):
        svc.create_world("half", settings, ME)
