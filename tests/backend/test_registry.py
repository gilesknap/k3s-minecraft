"""The world registry on the store, and restoring worlds after a rebuild."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from mcdash.config import Config
from mcdash.models import WorldState
from mcdash.service import KubeDashboardService

from .conftest import Clock
from .helpers import FakeHelm, FakeKube, FakeRcon
from .test_service import FIRE, KING, ME, complete, launch_running, parts


def registry(svc: KubeDashboardService) -> dict[str, dict]:
    d = svc.store.registry_dir
    return {p.stem: json.loads(p.read_text()) for p in d.glob("*.json")}


def rebuilt(svc: KubeDashboardService, restore: bool = True) -> KubeDashboardService:
    """A new dashboard on an empty cluster, with the same store."""
    kube = FakeKube()
    helm = FakeHelm(kube)
    rcon = FakeRcon()
    cfg = replace(svc.cfg, restore_worlds=restore)
    new = KubeDashboardService(
        cfg, kube, helm, rcon_factory=rcon.factory, clock=Clock(), cache_ttl=0
    )
    new.fake_kube = kube  # type: ignore[attr-defined]
    new.fake_helm = helm  # type: ignore[attr-defined]
    new.fake_rcon = rcon  # type: ignore[attr-defined]
    return new


def test_registry_follows_the_record(svc: KubeDashboardService) -> None:
    name = launch_running(svc)
    svc.reconcile_once()
    rec = registry(svc)[name]
    assert rec["spec"]["port"] == 30565 and rec["spec"]["rconPort"] == 30600
    assert rec["spec"]["sourceBackup"] == KING
    assert (rec["op"], rec["replicas"]) == (None, 1)
    svc.stop(name, ME)
    svc.reconcile_once()
    assert registry(svc)[name]["replicas"] == 0
    # unchanged: not rewritten every tick
    path = svc.store.registry_dir / f"{name}.json"
    mtime = path.stat().st_mtime_ns
    svc.reconcile_once()
    assert path.stat().st_mtime_ns == mtime


def test_registry_entry_goes_with_a_deleted_world(svc: KubeDashboardService) -> None:
    name = launch_running(svc)
    svc.reconcile_once()
    svc.delete(name, ME)
    svc.reconcile_once()
    assert name in registry(svc)  # still snapshotting
    complete(svc, "snapshot")
    assert name not in registry(svc)
    assert rebuilt(svc).list_worlds() == []


def test_no_restore_unless_enabled(svc: KubeDashboardService) -> None:
    name = launch_running(svc)
    svc.reconcile_once()
    new = rebuilt(svc, restore=False)
    new.reconcile_once()
    assert new.list_worlds() == []
    assert name in registry(new)  # kept for a later restore


def test_restore_after_rebuild(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    king = launch_running(svc)
    fire = launch_running(svc, FIRE)
    svc.stop(fire, ME)
    svc.update_settings(
        king, replace(svc.get_world(king).settings, motd="kept", whitelist=["noah"]), ME
    )
    svc.reconcile_once()
    old_pw = kube.secrets[f"{king}-rcon"]["rcon-password"]

    new = rebuilt(svc)
    nkube, nhelm, _ = parts(new)
    new.reconcile_once()
    assert nkube.pvcs == {f"{king}-data", f"{fire}-data"}
    assert nkube.secrets[f"{king}-rcon"]["rcon-password"] != old_pw
    k, f = new.get_world(king), new.get_world(fire)
    assert (k.state, f.state) == (WorldState.RUNNING, WorldState.STOPPED)
    assert (k.port, k.rcon_port, f.port, f.rcon_port) == (30565, 30600, 30566, 30601)
    assert k.settings.motd == "kept" and k.settings.whitelist == ["noah"]
    assert f.settings.server_type.value == "FORGE"
    assert k.source_backup_id == KING
    assert "restored" in f.message
    values = nhelm.releases[king]
    assert (
        values["replicaCount"] == 1 and values["minecraftServer"]["nodePort"] == 30565
    )
    assert nhelm.releases[fire]["replicaCount"] == 0
    assert new.store.read_audit()[-1]["action"] == "restore_world"
    # once restored it is an ordinary world again: nothing more happens
    calls = len(nhelm.calls)
    new.reconcile_once()
    assert len(nhelm.calls) == calls
    new.start(fire, ME)
    assert new.get_world(fire).message == ""


def test_restore_resumes_a_seed(svc: KubeDashboardService) -> None:
    name, settings = svc.launch_defaults(KING)
    svc.launch(KING, name, settings, ME)
    svc.reconcile_once()
    new = rebuilt(svc)
    nkube, nhelm, _ = parts(new)
    new.reconcile_once()  # restores the record; the next tick re-runs the seed
    assert new.get_world(name).state is WorldState.SEEDING
    new.reconcile_once()
    assert nkube.active_jobs("seed")
    complete(new, "seed")
    assert new.get_world(name).state is WorldState.RUNNING
    assert nhelm.releases[name]["replicaCount"] == 1


def test_restore_drops_a_plain_snapshot(svc: KubeDashboardService) -> None:
    name = launch_running(svc)
    svc.snapshot(name, ME)
    svc.reconcile_once()
    assert registry(svc)[name]["op"]["kind"] == "snapshot"
    new = rebuilt(svc)
    new.reconcile_once()
    w = new.get_world(name)
    assert w.state is WorldState.RUNNING
    assert not parts(new)[0].active_jobs("snapshot")


def lost(port: int, rcon_port: int, replicas: int = 1) -> dict:
    """A registry record of a world the cluster no longer has."""
    return {
        "spec": {"port": port, "rconPort": rcon_port, "settings": {}},
        "op": None,
        "replicas": replicas,
    }


def skipped(svc: KubeDashboardService) -> set[str]:
    return {p.stem for p in svc.store.skipped_dir.glob("*.json")}


def test_restore_skips_a_port_clash(svc: KubeDashboardService) -> None:
    name = launch_running(svc)  # holds 30565 and 30600
    svc.reconcile_once()
    # after the rebuild the cluster somehow has `name` again, and a lost
    # world that wants one of its ports
    svc.store.save_registry("lost-world", lost(30566, 30600))
    new = rebuilt(svc)
    new._records = svc._records  # type: ignore[method-assign]
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == [name]
    assert "lost-world-data" not in parts(new)[0].pvcs
    assert "lost-world" not in registry(new) and skipped(new) == {"lost-world"}
    (entry,) = [a for a in new.store.read_audit() if a["action"] == "skip_restore"]
    assert entry["target"] == "lost-world" and "30600" in entry["reason"]


def test_a_skipped_world_never_comes_back(svc: KubeDashboardService) -> None:
    """Restore runs once per start: freeing the port later restores nothing."""
    holder = launch_running(svc)
    svc.reconcile_once()
    svc.store.save_registry("lost-world", lost(30566, 30600))
    new = rebuilt(svc)
    new._records = svc._records  # type: ignore[method-assign]
    new.reconcile_once()
    assert skipped(new) == {"lost-world"}
    del new._records  # back to its own (empty) cluster: the port is free
    for _ in range(3):
        new.reconcile_once()
    assert new.list_worlds() == []
    # nor after the next start
    again = rebuilt(new)
    again.reconcile_once()
    assert [w.name for w in again.list_worlds()] == [holder]


def test_two_records_clash_with_each_other(svc: KubeDashboardService) -> None:
    svc.store.save_registry("alpha", lost(30565, 30600))
    svc.store.save_registry("beta", lost(30566, 30600))
    new = rebuilt(svc)
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == ["alpha"]  # sorted: first wins
    assert skipped(new) == {"beta"}


def test_restore_runs_once_per_start(svc: KubeDashboardService) -> None:
    """A world lost while the dashboard runs comes back only at the next start."""
    name = launch_running(svc)
    svc.reconcile_once()
    new = rebuilt(svc)
    new.reconcile_once()
    assert new.get_world(name).state is WorldState.RUNNING
    nkube, nhelm, _ = parts(new)
    nkube.configmaps.clear()  # lost by hand: record and release
    nhelm.releases.clear()
    new.reconcile_once()
    assert new.list_worlds() == []
    assert rebuilt(new).list_worlds() == []  # nothing until it reconciles
    later = rebuilt(new)
    later.reconcile_once()
    assert [w.name for w in later.list_worlds()] == [name]


def test_deleting_the_record_file_prevents_a_restore(
    svc: KubeDashboardService,
) -> None:
    """The documented remedy: delete registry/<w>.json, then (re)start."""
    name = launch_running(svc)
    svc.reconcile_once()
    new = rebuilt(svc)  # not yet started: the cluster has lost the world
    (svc.store.registry_dir / f"{name}.json").unlink()
    new.reconcile_once()
    assert new.list_worlds() == []
    assert name not in registry(new)


def test_a_bad_record_does_not_stall_the_others(
    svc: KubeDashboardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = svc.store.registry_dir
    d.mkdir(parents=True)
    (d / "broken.json").write_text("{not json")
    (d / "typed.json").write_text(json.dumps({"spec": {}, "op": "seed"}))
    svc.store.save_registry("strange", {**lost(30565, 30600), "spec": {"port": "x"}})
    svc.store.save_registry("cursed", lost(30566, 30601))
    svc.store.save_registry("good", lost(30567, 30602))
    assert set(svc.store.registry()) == {"strange", "cursed", "good"}
    new = rebuilt(svc)
    real = new._create_volume_and_secret

    def boom(name: str) -> None:
        if name == "cursed":
            raise RuntimeError("unexpected")
        real(name)

    monkeypatch.setattr(new, "_create_volume_and_secret", boom)
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == ["good"]
    assert skipped(new) == {"strange"}  # not a world: set aside
    assert "cursed" in new.store.registry_names()  # kept: retried next start


def test_an_unreadable_registry_is_not_taken_for_an_empty_one(
    svc: KubeDashboardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = launch_running(svc)
    svc.reconcile_once()
    new = rebuilt(svc)
    real = new.store.registry

    def down() -> dict:
        raise OSError("stale file handle")

    monkeypatch.setattr(new.store, "registry", down)
    new.reconcile_once()
    assert new.list_worlds() == []
    monkeypatch.setattr(new.store, "registry", real)
    new.reconcile_once()  # the store is back: restored now
    assert [w.name for w in new.list_worlds()] == [name]


def test_store_registry_raises_on_os_errors(svc: KubeDashboardService) -> None:
    assert svc.store.registry() == {}  # no registry yet
    svc.store.registry_dir.parent.rmdir()
    svc.store.registry_dir.parent.write_text("not a directory")
    with pytest.raises(OSError):
        svc.store.registry()
    with pytest.raises(OSError):
        svc.store.registry_names()


def test_delete_while_the_store_is_down(
    svc: KubeDashboardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    kube, _, _ = parts(svc)
    name, settings = svc.new_world_defaults()
    svc.create_world(name, settings, ME)
    svc.stop(name, ME)
    svc.reconcile_once()

    def down(world: str) -> None:
        raise OSError("store gone")

    monkeypatch.setattr(svc.store, "drop_registry", down)
    svc.fake_helm.releases.pop(name)  # type: ignore[attr-defined]
    svc.delete(name, ME)  # no release: straight to teardown, which fails
    w = svc.get_world(name)
    assert w.state is WorldState.FAILED and "registry" in w.message
    assert f"{name}-data" in kube.pvcs  # nothing torn down
    svc.reconcile_once()  # a failed op is left alone: no traceback per tick
    monkeypatch.undo()
    svc.delete(name, ME)  # retried once the store is back
    assert svc.list_worlds() == [] and name not in registry(svc)


def test_restore_install_failure_is_retried_by_start(
    svc: KubeDashboardService,
) -> None:
    name = launch_running(svc)
    svc.reconcile_once()
    new = rebuilt(svc)
    _, nhelm, _ = parts(new)
    nhelm.fail_next = "chart exploded"
    new.reconcile_once()
    w = new.get_world(name)
    assert w.state is WorldState.FAILED and "chart exploded" in w.message
    new.start(name, ME)
    assert new.get_world(name).state is WorldState.RUNNING


def test_store_errors_do_not_stop_the_reconciler(
    svc: KubeDashboardService, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, settings = svc.launch_defaults(KING)
    svc.launch(KING, name, settings, ME)

    def boom(*a: object) -> None:
        raise OSError("store gone")

    monkeypatch.setattr(svc.store, "save_registry", boom)
    svc.kube.finish_jobs("seed")  # type: ignore[attr-defined]
    svc.reconcile_once()  # the seed still finishes
    assert svc.get_world(name).state is WorldState.RUNNING
    monkeypatch.undo()
    svc.reconcile_once()
    assert name in registry(svc)


def test_restore_worlds_env() -> None:
    assert not Config.from_env({"MCD_NAMESPACE": "mc"}).restore_worlds
    assert Config.from_env({"MCD_RESTORE_WORLDS": "true"}).restore_worlds


def test_registry_records_the_storage_class(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    svc.reconcile_once()
    assert registry(svc)[name]["storageClass"] == "nfs-rwx"
    kube.pvc_class[f"{name}-data"] = "nfs-fast"  # e.g. migrated by hand
    svc.reconcile_once()
    assert registry(svc)[name]["storageClass"] == "nfs-fast"
    kube.pvcs.discard(f"{name}-data")  # no PVC: the class last seen is kept
    svc.reconcile_once()
    assert registry(svc)[name]["storageClass"] == "nfs-fast"


def test_restore_skips_another_storage_class(svc: KubeDashboardService) -> None:
    king = launch_running(svc)
    svc.reconcile_once()
    svc.store.save_registry("older", lost(30566, 30601))  # no class recorded
    new = rebuilt(svc)
    new.cfg = replace(new.cfg, storage_class="nfs-new")
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == ["older"]  # unknown: restored
    assert skipped(new) == {king}
    (entry,) = [a for a in new.store.read_audit() if a["action"] == "skip_restore"]
    assert entry["reason"] == (
        "storage class changed (nfs-rwx → nfs-new): migrate its data first"
    )


def test_lost_worlds_keep_their_ports(svc: KubeDashboardService) -> None:
    """Not restored (yet, or restore off): a new world must not take its ports."""
    svc.store.save_registry("gone", lost(30565, 30600))
    svc.reconcile_once()  # restore is off here
    name, settings = svc.new_world_defaults()
    w = svc.create_world(name, settings, ME)
    assert (w.port, w.rcon_port) == (30566, 30601)
    # and straight after a start, before the first sync has read the registry
    fresh = rebuilt(svc, restore=False)
    w = fresh.create_world("other", settings, ME)
    assert (w.port, w.rcon_port) == (30566, 30601)


def test_one_unreadable_registry_file_is_skipped(
    svc: KubeDashboardService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc.store.save_registry("locked", lost(30565, 30600))
    svc.store.save_registry("good", lost(30566, 30601))
    real = Path.read_text

    def read_text(self: Path, *a: object, **kw: object) -> str:
        if self.name == "locked.json":
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", read_text)
    for _ in range(3):
        assert set(svc.store.registry()) == {"good"}
    warned = [r for r in caplog.records if "locked.json" in r.getMessage()]
    assert len(warned) == 1
    new = rebuilt(svc)
    new.reconcile_once()
    assert [w.name for w in new.list_worlds()] == ["good"]
    assert "locked" in new.store.registry_names()  # its name stays taken


def test_one_unwritable_registry_file_is_skipped(
    svc: KubeDashboardService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    king = launch_running(svc)
    fire = launch_running(svc, FIRE)
    svc.store.drop_registry(king)
    svc._registry_seen = None  # as after a restart
    real = svc.store.save_registry

    def save(world: str, rec: dict) -> None:
        if world == king:
            raise PermissionError(13, "Permission denied")
        real(world, rec)

    monkeypatch.setattr(svc.store, "save_registry", save)
    for _ in range(3):
        svc.reconcile_once()
    assert set(registry(svc)) == {fire}  # the others are still copied
    warned = [r for r in caplog.records if f"{king}.json" in r.getMessage()]
    assert len(warned) == 1
    monkeypatch.undo()
    svc.reconcile_once()
    assert set(registry(svc)) == {king, fire}


def test_an_unreadable_registry_is_logged_once(
    svc: KubeDashboardService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def down() -> dict:
        raise OSError("stale file handle")

    monkeypatch.setattr(svc.store, "registry", down)
    for _ in range(4):
        svc.reconcile_once()
    msgs = [r for r in caplog.records if "stale file handle" in r.getMessage()]
    assert len(msgs) == 1
