"""State-machine tests for KubeDashboardService against an in-memory cluster."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcdash.k8s import PodInfo
from mcdash.models import (
    Conflict,
    Forbidden,
    MCDashError,
    NotFound,
    Player,
    ServerType,
    WorldState,
)
from mcdash.service import KubeDashboardService

from .helpers import FakeHelm, FakeKube, FakeRcon, make_world_zip

KING = "archive/2021-05-15-21_56_20-king-ghidorah(1).zip"
FIRE = "archive/2021-06-03-10_30_33-fire-ice2.zip"
ME = "admin@example.org"


def parts(svc: KubeDashboardService) -> tuple[FakeKube, FakeHelm, FakeRcon]:
    return svc.fake_kube, svc.fake_helm, svc.fake_rcon  # type: ignore[attr-defined]


def finish(svc: KubeDashboardService, kind: str, ok: bool = True) -> None:
    """Finish active Jobs of ``kind``; snapshot Jobs write their zip."""
    kube, _, _ = parts(svc)
    for j in kube.finish_jobs(kind, ok):
        if kind == "snapshot" and ok:
            dest = j.spec.args[j.spec.args.index("--dest") + 1]
            local = svc.cfg.store_dir / Path(dest).relative_to("/store")
            make_world_zip(local, prefix="")


def complete(svc: KubeDashboardService, kind: str, ok: bool = True) -> None:
    finish(svc, kind, ok)
    svc.reconcile_once()


def launch_running(svc: KubeDashboardService, backup: str = KING) -> str:
    name, settings = svc.launch_defaults(backup)
    svc.launch(backup, name, settings, ME)
    complete(svc, "seed")
    assert svc.get_world(name).state is WorldState.RUNNING
    return name


def test_launch_defaults(svc: KubeDashboardService) -> None:
    name, s = svc.launch_defaults(KING)
    assert name == "king-ghidorah"
    assert s.version == "LATEST" and s.server_type is ServerType.VANILLA
    assert (s.gamemode, s.difficulty, s.enable_command_block) == (
        "creative",
        "normal",
        True,
    )
    assert s.memory == "4Gi"
    name, s = svc.launch_defaults(FIRE)
    assert name == "fire-ice2"
    assert (s.server_type, s.version) == (ServerType.FORGE, "1.16.5")


def test_launch_seed_install(svc: KubeDashboardService) -> None:
    kube, helm, rcon = parts(svc)
    name, settings = svc.launch_defaults(KING)
    w = svc.launch(KING, name, settings, ME)
    assert w.state is WorldState.SEEDING
    assert w.port == 30565 and w.connect_address == "mc.example.org:30565"
    assert "king-ghidorah-data" in kube.pvcs
    assert kube.secrets["king-ghidorah-rcon"]["rcon-password"]
    (job,) = kube.active_jobs("seed")
    assert job.spec.args == [
        "seed",
        "--src",
        "/archive/2021-05-15-21_56_20-king-ghidorah(1).zip",
        "--dest",
        "/data",
        "--mode",
        "vanilla",
    ]
    assert job.spec.name.startswith("king-ghidorah-seed-2026")
    assert len(job.spec.name) <= 63

    svc.reconcile_once()  # job still running
    assert svc.get_world(name).state is WorldState.SEEDING
    assert helm.releases == {}

    complete(svc, "seed")
    w = svc.get_world(name)
    assert w.state is WorldState.RUNNING
    assert w.players_online == ["noah", "giles"]
    assert w.uptime_seconds is not None
    v = helm.releases[name]
    assert v["replicaCount"] == 1
    assert v["mcdash"]["sourceBackup"] == KING
    assert v["minecraftServer"]["nodePort"] == 30565
    assert any(h.startswith("king-ghidorah-rcon.minecraft.svc") for h, _ in rcon.log)

    with pytest.raises(Conflict):
        svc.launch(KING, name, settings, ME)
    assert svc.launch_defaults(KING)[0] == "king-ghidorah-2"

    audit = svc.store.read_audit()
    assert audit[0]["action"] == "launch" and audit[0]["actor"] == ME


def test_forge_launch_full_mode(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    name, s = svc.launch_defaults(FIRE)
    svc.launch(FIRE, name, s, ME)
    (job,) = kube.active_jobs("seed")
    assert job.spec.args[-1] == "full"
    complete(svc, "seed")
    v = helm.releases[name]
    assert v["minecraftServer"]["type"] == "FORGE"
    assert v["minecraftServer"]["forgeVersion"] == "36.1.0"
    assert v["image"]["tag"] == "java8"


def test_bad_names_and_ports(svc: KubeDashboardService) -> None:
    _, s = svc.launch_defaults(KING)
    with pytest.raises(MCDashError):
        svc.launch(KING, "Bad Name", s, ME)
    for n in ("a", "b", "c"):
        svc.launch(KING, n, s, ME)
    assert sorted(w.port for w in svc.list_worlds()) == [30565, 30566, 30567]
    with pytest.raises(Conflict, match="no free ports"):
        svc.launch(KING, "d", s, ME)
    with pytest.raises(NotFound):
        svc.launch("archive/missing.zip", "e", s, ME)


def test_snapshot_pauses_saving_and_prunes(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    for _ in range(4):
        rcon.log.clear()
        svc.snapshot(name, ME)
        assert svc.get_world(name).state is WorldState.SNAPSHOTTING
        with pytest.raises(Conflict):
            svc.stop(name, ME)
        (job,) = kube.active_jobs("snapshot")
        assert job.spec.store_read_only is False
        assert job.spec.args[:4] == ["snapshot", "--src", "/data", "--dest"]
        complete(svc, "snapshot")
        cmds = [c for _, c in rcon.log]
        assert cmds[:2] == ["save-off", "save-all flush"]
        assert "save-on" in cmds
    w = svc.get_world(name)
    assert w.state is WorldState.RUNNING
    assert w.last_snapshot is not None
    snaps = svc.list_snapshots(name)
    assert len(snaps) == svc.cfg.snapshot_keep == 3


def test_stop_start(svc: KubeDashboardService) -> None:
    _, helm, _ = parts(svc)
    name = launch_running(svc)
    svc.stop(name, ME)
    assert svc.get_world(name).state is WorldState.SNAPSHOTTING
    complete(svc, "snapshot")
    w = svc.get_world(name)
    assert w.state is WorldState.STOPPED and w.port == 30565
    assert helm.releases[name]["replicaCount"] == 0
    svc.stop(name, ME)  # idempotent
    svc.start(name, ME)
    assert svc.get_world(name).state is WorldState.RUNNING
    assert helm.releases[name]["minecraftServer"]["nodePort"] == 30565


def test_stop_aborts_when_snapshot_fails(svc: KubeDashboardService) -> None:
    _, helm, rcon = parts(svc)
    name = launch_running(svc)
    svc.stop(name, ME)
    complete(svc, "snapshot", ok=False)
    w = svc.get_world(name)
    assert w.state is WorldState.RUNNING
    assert "snapshot failed" in w.message
    assert helm.releases[name]["replicaCount"] == 1
    assert "save-on" in [c for _, c in rcon.log]


def test_whitelist_live_without_restart(svc: KubeDashboardService) -> None:
    _, helm, rcon = parts(svc)
    name = launch_running(svc)
    upgrades = len(helm.calls)
    svc.set_whitelist(name, ["noah", "giles"], ME, open_to_all=False)
    svc.set_whitelist(name, ["giles", "millie"], ME, open_to_all=False)
    cmds = [c for _, c in rcon.log if c.startswith("whitelist")]
    assert cmds == [
        "whitelist add noah",
        "whitelist add giles",
        "whitelist add millie",
        "whitelist remove noah",
    ]
    assert len(helm.calls) == upgrades  # no pod restart
    assert svc.get_world(name).settings.whitelist == ["giles", "millie"]
    with pytest.raises(MCDashError):
        svc.set_whitelist(name, ["bad name!"], ME, open_to_all=False)
    # persisted into release values at the next start
    svc.stop(name, ME)
    complete(svc, "snapshot")
    svc.start(name, ME)
    assert helm.releases[name]["minecraftServer"]["whitelist"] == "giles,millie"


def test_update_settings(svc: KubeDashboardService) -> None:
    _, helm, _ = parts(svc)
    name = launch_running(svc)
    w = svc.get_world(name)
    s = w.settings
    s.memory = "6Gi"
    s.version = "1.18.2"
    svc.update_settings(name, s, ME)
    v = helm.releases[name]
    assert v["minecraftServer"]["memory"] == "6G"
    assert v["image"]["tag"] == "java17"
    assert v["replicaCount"] == 1
    s.memory = "huge"
    with pytest.raises(MCDashError):
        svc.update_settings(name, s, ME)


def test_open_to_all_live_and_persisted(svc: KubeDashboardService) -> None:
    _, helm, rcon = parts(svc)
    name = launch_running(svc)
    upgrades = len(helm.calls)
    svc.set_whitelist(name, ["noah"], ME, open_to_all=False)
    svc.set_whitelist(name, ["noah"], ME, open_to_all=True)
    svc.set_whitelist(name, ["noah"], ME, open_to_all=True)  # no change, no command
    cmds = [c for _, c in rcon.log if c.startswith("whitelist")]
    assert cmds == ["whitelist add noah", "whitelist off"]
    assert len(helm.calls) == upgrades  # no pod restart
    w = svc.get_world(name)
    assert w.settings.open_to_all is True and w.settings.whitelist == ["noah"]
    svc.stop(name, ME)
    complete(svc, "snapshot")
    svc.start(name, ME)
    values = helm.releases[name]
    assert "whitelist" not in values["minecraftServer"]
    assert values["extraEnv"]["ENABLE_WHITELIST"] == "FALSE"
    # closing it again turns the whitelist back on live
    svc.set_whitelist(name, ["noah"], ME, open_to_all=False)
    assert [c for _, c in rcon.log if c.startswith("whitelist")][-1] == "whitelist on"


def test_closing_a_world_that_started_open_readds_every_name(
    svc: KubeDashboardService,
) -> None:
    _, helm, rcon = parts(svc)
    name, settings = svc.launch_defaults(KING)
    settings.whitelist = ["noah", "giles"]
    settings.open_to_all = True
    svc.launch(KING, name, settings, ME)
    complete(svc, "seed")
    assert "whitelist" not in helm.releases[name]["minecraftServer"]
    # the names were never put on the server, so closing must add all of them
    svc.set_whitelist(name, ["noah", "giles"], ME, open_to_all=False)
    cmds = [c for _, c in rcon.log if c.startswith("whitelist")]
    assert cmds == ["whitelist add noah", "whitelist add giles", "whitelist on"]


def test_access_change_refused_while_starting(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    kube.pods[name] = [PodInfo(name="p", phase="Pending", ready=False)]
    with pytest.raises(Conflict):
        svc.set_whitelist(name, [], ME, open_to_all=True)
    assert svc.get_world(name).settings.open_to_all is False
    # a names-only edit is still saved for the next start, as before
    svc.set_whitelist(name, ["noah"], ME, open_to_all=False)
    assert svc.get_world(name).settings.whitelist == ["noah"]
    assert not [c for _, c in rcon.log if c.startswith("whitelist")]


def test_restore(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    name = launch_running(svc)
    svc.set_whitelist(name, ["noah"], ME, open_to_all=False)
    snap = make_world_zip(svc.cfg.snapshots_dir / name / "20250101T000000Z.zip")
    assert snap.exists()
    sid = f"snapshot/{name}/20250101T000000Z.zip"
    svc.restore(name, sid, ME)
    assert svc.get_world(name).state is WorldState.SNAPSHOTTING
    complete(svc, "snapshot")
    # scaled to 0, pods gone -> seed job created in the same reconcile pass
    assert helm.releases[name]["replicaCount"] == 0
    (seedjob,) = kube.active_jobs("seed")
    assert seedjob.spec.args[2] == f"/store/snapshots/{name}/20250101T000000Z.zip"
    assert svc.get_world(name).state is WorldState.SEEDING
    complete(svc, "seed")
    w = svc.get_world(name)
    assert w.state is WorldState.RUNNING
    assert w.port == 30565 and w.source_backup_id == sid
    assert w.settings.whitelist == ["noah"]
    assert helm.releases[name]["minecraftServer"]["whitelist"] == "noah"


def test_restore_waits_for_pod_drain(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    svc.restore(name, FIRE, ME)
    finish(svc, "snapshot")
    # pretend the old pod is still terminating after scale-down
    lingering = PodInfo(name="x", phase="Running", ready=False, deleting=True)
    orig = kube.list_server_pods
    kube.list_server_pods = lambda w: [lingering]  # type: ignore[method-assign]
    svc.reconcile_once()
    assert kube.active_jobs("seed") == []
    kube.list_server_pods = orig  # type: ignore[method-assign]
    svc.reconcile_once()
    assert len(kube.active_jobs("seed")) == 1


def test_delete(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    name = launch_running(svc)
    svc.delete(name, ME)
    assert svc.get_world(name).state is WorldState.SNAPSHOTTING
    complete(svc, "snapshot")
    with pytest.raises(NotFound):
        svc.get_world(name)
    assert name not in helm.releases
    assert not kube.pvcs and not kube.secrets and not kube.configmaps
    assert len(svc.list_snapshots(name)) == 1  # final snapshot kept
    # port is free again
    _, s = svc.launch_defaults(KING)
    assert svc.launch(KING, "again", s, ME).port == 30565


def test_seed_failure_retry_and_delete(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    name, s = svc.launch_defaults(KING)
    svc.launch(KING, name, s, ME)
    complete(svc, "seed", ok=False)
    w = svc.get_world(name)
    assert w.state is WorldState.FAILED and "seed job failed" in w.message
    svc.start(name, ME)  # retries the seed
    assert svc.get_world(name).state is WorldState.SEEDING
    complete(svc, "seed", ok=False)
    svc.delete(name, ME)  # no release, nothing to snapshot: immediate teardown
    with pytest.raises(NotFound):
        svc.get_world(name)
    assert not kube.pvcs and not helm.releases
    assert kube.active_jobs("snapshot") == []


def test_helm_failure_marks_failed(svc: KubeDashboardService) -> None:
    _, helm, _ = parts(svc)
    name, s = svc.launch_defaults(KING)
    svc.launch(KING, name, s, ME)
    helm.fail_next = "chart exploded"
    complete(svc, "seed")
    w = svc.get_world(name)
    assert w.state is WorldState.FAILED and "chart exploded" in w.message


def test_crashloop_is_failed(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    kube.pods[name] = [
        PodInfo(
            name="p", phase="Running", ready=False, waiting_reason="CrashLoopBackOff"
        )
    ]
    w = svc.get_world(name)
    assert w.state is WorldState.FAILED and "CrashLoopBackOff" in w.message
    kube.pods[name] = [PodInfo(name="p", phase="Pending", ready=False)]
    assert svc.get_world(name).state is WorldState.STARTING


def test_restart_resumes_operation(svc: KubeDashboardService) -> None:
    """A fresh service (dashboard restart) picks up an in-flight op."""
    kube, helm, rcon = parts(svc)
    name = launch_running(svc)
    svc.stop(name, ME)
    finish(svc, "snapshot")

    fresh = KubeDashboardService(svc.cfg, kube, helm, rcon_factory=rcon.factory)
    assert fresh.get_world(name).state is WorldState.SNAPSHOTTING
    fresh.reconcile_once()
    assert fresh.get_world(name).state is WorldState.STOPPED


def test_restart_recreates_vanished_seed_job(svc: KubeDashboardService) -> None:
    kube, helm, rcon = parts(svc)
    name, s = svc.launch_defaults(KING)
    svc.launch(KING, name, s, ME)
    kube.jobs.clear()  # e.g. TTL-deleted while the dashboard was down
    fresh = KubeDashboardService(svc.cfg, kube, helm, rcon_factory=rcon.factory)
    fresh.reconcile_once()
    (job,) = kube.active_jobs("seed")
    assert job.spec.args[-1] == "vanilla"


def test_lost_configmap_rebuilt_from_release(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    kube.configmaps.clear()
    w = svc.get_world(name)
    assert w.port == 30565 and w.source_backup_id == KING
    assert f"{name}-mcdash" in kube.configmaps
    spec = json.loads(kube.configmaps[f"{name}-mcdash"][1]["spec"])
    assert spec["port"] == 30565


def test_rcon_logs_metrics(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    kube.metrics[name] = (250, 2 * 1024**3)
    w = svc.get_world(name)
    assert (w.cpu_millicores, w.memory_bytes) == (250, 2 * 1024**3)
    assert svc.rcon(name, "say hi", ME) == "ok say hi"
    assert svc.store.read_audit()[-1]["command"] == "say hi"
    rcon.fail = True
    with pytest.raises(MCDashError):
        svc.rcon(name, "list", ME)
    assert svc.get_world(name).players_online is None
    kube.logs[f"{name}-abc"] = ["line 1", "line 2"]
    assert list(svc.stream_logs(name)) == ["line 1", "line 2"]
    with pytest.raises(NotFound):
        list(svc.stream_logs("nope"))


def test_backups_via_service(svc: KubeDashboardService) -> None:
    ids = [b.id for b in svc.list_backups()]
    assert ids == [FIRE, KING]
    with pytest.raises(Forbidden):
        svc.delete_backup(KING, ME)
    svc.set_backup_label(KING, "First", "the original")
    assert svc.get_backup(KING).label == "First"
    make_world_zip(svc.cfg.snapshots_dir / "w" / "20261011T120000Z.zip")
    b = svc.promote("snapshot/w/20261011T120000Z.zip", "Keep", "", ME)
    svc.delete_backup(b.id, ME)
    actions = [r["action"] for r in svc.store.read_audit()]
    assert actions == ["promote", "delete_backup"]


def test_roster(svc: KubeDashboardService) -> None:
    svc.set_roster([Player("noah", "son"), Player("giles")])
    assert [p.name for p in svc.roster()] == ["noah", "giles"]


def test_protocol_conformance(svc: KubeDashboardService) -> None:
    from mcdash.models import DashboardService

    def accepts(s: DashboardService) -> DashboardService:
        return s

    assert accepts(svc) is svc
    for meth in [m for m in dir(DashboardService) if not m.startswith("_")]:
        assert callable(getattr(svc, meth)), meth


def test_ignores_other_helm_releases(svc: KubeDashboardService) -> None:
    """The dashboard's own release (chart minecraft-dashboard-x) is not a world."""
    helm: Any = svc.fake_helm  # type: ignore[attr-defined]
    orig = helm.list
    helm.list = lambda: [
        *orig(),
        {"name": "mcdash", "revision": "1", "chart": "minecraft-dashboard-0.1.0"},
    ]
    helm.get_values = lambda r: {"mcdash": {"port": 30570}}  # would be adopted
    assert svc.list_worlds() == []


# ------------------------------------------------------------------ new world


def test_new_world_defaults(svc: KubeDashboardService) -> None:
    name, s = svc.new_world_defaults()
    assert name == "new-world"
    assert (s.version, s.server_type, s.gamemode) == (
        "LATEST",
        ServerType.VANILLA,
        "survival",
    )
    assert s.memory == svc.cfg.default_memory
    assert (s.seed, s.level_type) == ("", "normal")
    svc.create_world(name, s, ME)
    assert svc.new_world_defaults()[0] == "new-world-2"


def test_create_world_installs_without_seed_job(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    _, s = svc.new_world_defaults()
    s.seed, s.level_type = "12345", "flat"
    # pod not ready yet: the fake Deployment comes up Ready, so hold it back
    orig = helm.upgrade_install

    def install_pending(release: str, values: dict[str, Any]) -> None:
        orig(release, values)
        kube.pods[release] = [
            PodInfo(name=f"{release}-abc", phase="Pending", ready=False)
        ]

    helm.upgrade_install = install_pending  # type: ignore[method-assign]
    w = svc.create_world("fresh", s, ME)
    assert w.state is WorldState.STARTING
    assert w.source_backup_id == "" and w.port == 30565
    assert kube.jobs == {}
    assert "fresh-data" in kube.pvcs and kube.secrets["fresh-rcon"]["rcon-password"]
    v = helm.releases["fresh"]
    assert v["replicaCount"] == 1
    ms = v["minecraftServer"]
    assert (ms["levelSeed"], ms["levelType"]) == ("12345", "minecraft:flat")
    assert v["mcdash"]["sourceBackup"] == ""
    assert v["mcdash"]["settings"]["seed"] == "12345"

    svc.reconcile_once()  # nothing to advance, and not treated as a failure
    assert svc.get_world("fresh").state is WorldState.STARTING
    kube.pods["fresh"] = [
        PodInfo(name="fresh-abc", phase="Running", ready=True, started=svc.now())
    ]
    w = svc.get_world("fresh")
    assert w.state is WorldState.RUNNING and w.message == ""
    assert (w.settings.seed, w.settings.level_type) == ("12345", "flat")
    assert kube.jobs == {}
    assert svc.store.read_audit()[-1]["action"] == "create_world"

    # generation settings survive a settings update and a lost ConfigMap
    w.settings.memory = "6Gi"
    svc.update_settings("fresh", w.settings, ME)
    assert helm.releases["fresh"]["minecraftServer"]["levelSeed"] == "12345"
    kube.configmaps.clear()
    w = svc.get_world("fresh")
    assert w.source_backup_id == "" and w.settings.level_type == "flat"


def test_create_world_conflicts_and_validation(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    _, s = svc.new_world_defaults()
    launch_running(svc)  # king-ghidorah on 30565
    w = svc.create_world("b", s, ME)
    assert w.port == 30566
    with pytest.raises(Conflict, match="taken"):
        svc.create_world("b", s, ME)
    with pytest.raises(Conflict, match="taken"):
        svc.create_world("king-ghidorah", s, ME)
    kube.pvcs.add("orphan-data")
    with pytest.raises(Conflict, match="taken"):
        svc.create_world("orphan", s, ME)
    with pytest.raises(MCDashError):
        svc.create_world("Bad Name", s, ME)
    bad = svc.new_world_defaults()[1]
    bad.level_type = "caves"
    with pytest.raises(MCDashError, match="world type"):
        svc.create_world("c", bad, ME)
    bad.level_type, bad.seed = "normal", "x" * 65
    with pytest.raises(MCDashError, match="seed"):
        svc.create_world("c", bad, ME)
    svc.create_world("c", s, ME)
    with pytest.raises(Conflict, match="no free ports"):
        svc.create_world("d", s, ME)


def test_create_world_snapshot_restore_delete(svc: KubeDashboardService) -> None:
    kube, helm, rcon = parts(svc)
    name, s = svc.new_world_defaults()
    svc.create_world(name, s, ME)
    assert svc.get_world(name).state is WorldState.RUNNING
    svc.snapshot(name, ME)
    complete(svc, "snapshot")
    (snap,) = svc.list_snapshots(name)
    assert svc.get_world(name).state is WorldState.RUNNING
    svc.restore(name, snap.id, ME)
    complete(svc, "snapshot")
    complete(svc, "seed")
    w = svc.get_world(name)
    assert w.state is WorldState.RUNNING and w.source_backup_id == snap.id
    svc.delete(name, ME)
    complete(svc, "snapshot")
    with pytest.raises(NotFound):
        svc.get_world(name)
    assert not kube.pvcs and not helm.releases and not kube.configmaps


def test_create_world_helm_failure_then_start(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    _, s = svc.new_world_defaults()
    helm.fail_next = "chart exploded"
    w = svc.create_world("boom", s, ME)
    assert w.state is WorldState.FAILED and "chart exploded" in w.message
    assert "boom-data" in kube.pvcs and kube.jobs == {}
    svc.reconcile_once()  # a failed op is left for the admin
    assert svc.get_world("boom").state is WorldState.FAILED
    svc.start("boom", ME)  # retries the install
    assert svc.get_world("boom").state is WorldState.RUNNING
    assert helm.releases["boom"]["replicaCount"] == 1


def test_create_world_interrupted_install_resumes(svc: KubeDashboardService) -> None:
    """Dashboard died after recording the world but before helm install."""
    kube, helm, rcon = parts(svc)
    _, s = svc.new_world_defaults()
    svc._save(  # type: ignore[attr-defined]
        "halfway",
        {"port": 30565, "sourceBackup": "", "settings": {}, "created": ""},
        {"kind": "create", "step": "install"},
    )
    fresh = KubeDashboardService(svc.cfg, kube, helm, rcon_factory=rcon.factory)
    assert fresh.get_world("halfway").state is WorldState.STARTING
    fresh.reconcile_once()
    assert fresh.get_world("halfway").state is WorldState.RUNNING
    assert "halfway-data" in kube.pvcs and "halfway-rcon" in kube.secrets
