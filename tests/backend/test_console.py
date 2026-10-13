"""RCON console completion in KubeDashboardService (commands Job + report)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from mcdash.completion import REPORT_SCRIPT
from mcdash.service import KubeDashboardService
from mcdash.web.fake import SAMPLE_COMMANDS

from .test_service import FIRE, ME, launch_running, parts


def write_report(svc: KubeDashboardService, world: str, pod: str, **extra: str) -> None:
    """What a successful commands Job leaves in the store."""
    d = svc.commands_dir
    (d / "worlds").mkdir(parents=True, exist_ok=True)
    raw = json.loads(SAMPLE_COMMANDS.read_text())
    (d / "26.3.commands.json").write_text(json.dumps(raw["commands"]))
    (d / "26.3.registries.json").write_text(json.dumps(raw["registries"]))
    ptr = {"pod": pod, "version": "26.3", "error": "", **extra}
    (d / "worlds" / f"{world}.json").write_text(json.dumps(ptr))


def test_running_world_starts_one_commands_job(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    kube.pods[name] = [replace(kube.pods[name][0], image="itzg/minecraft-server:j25")]
    c = svc.complete(name, "ti")
    assert (c.status, c.items) == ("pending", [])
    (job,) = kube.active_jobs("commands")
    s = job.spec
    assert s.name.startswith(f"{name}-cmds-")
    assert s.image == "itzg/minecraft-server:j25"
    assert s.command == ["sh", "-c", REPORT_SCRIPT]
    assert s.env == {"MCD_WORLD": name, "MCD_POD": f"{name}-abc"}
    assert s.data_read_only and not s.mount_archive and not s.store_read_only
    assert (s.store_sub_path, s.store_mount) == ("dashboard/commands", "/out")
    assert s.backoff_limit == 0
    # the directory the Job mounts exists (created as the dashboard's uid)
    assert (svc.cfg.store_dir / "dashboard/commands/worlds").is_dir()
    # asking again while it runs starts no second Job
    assert svc.complete(name, "tim").status == "pending"
    assert len(kube.active_jobs("commands")) == 1
    # sending commands is untouched: no audit entry from completion
    assert not [a for a in svc.store.read_audit() if a["action"] == "rcon"]

    kube.finish_jobs("commands")
    write_report(svc, name, f"{name}-abc")
    c = svc.complete(name, "ti")
    assert c.status == "ready" and c.start == 0
    assert [s.text for s in c.items] == ["tick", "time", "title"]
    c = svc.complete(name, "tp ")
    assert {"noah", "giles"} <= {s.text for s in c.items}  # online players


def test_failed_job_is_not_repeated_for_the_same_pod(
    svc: KubeDashboardService,
) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    assert svc.complete(name, "").status == "pending"
    kube.finish_jobs("commands", ok=False)
    c = svc.complete(name, "ti")
    assert c.status == "none" and c.items == []
    assert len(kube.jobs) == 2  # seed + one commands Job
    # the Job reported why: shown to the user
    write_report(svc, name, f"{name}-abc", error="the data generator failed")
    (svc.commands_dir / "26.3.commands.json").unlink()
    c = svc.complete(name, "ti")
    assert c.status == "none" and "data generator failed" in c.message


def test_new_pod_refreshes_the_version(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    write_report(svc, name, "old-pod")
    c = svc.complete(name, "ti")
    # the last known tree is used while the new pod's version is read
    assert c.status == "pending" and [s.text for s in c.items][:1] == ["tick"]
    assert len(kube.active_jobs("commands")) == 1


def test_stopped_world_uses_the_last_known_tree(svc: KubeDashboardService) -> None:
    kube, _, rcon = parts(svc)
    name = launch_running(svc)
    write_report(svc, name, f"{name}-abc")
    svc.stop(name, ME)
    kube.finish_jobs("snapshot")
    for j in kube.jobs.values():
        if j.spec.kind == "snapshot":
            local = svc.cfg.store_dir / Path(
                j.spec.args[j.spec.args.index("--dest") + 1]
            ).relative_to("/store")
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(b"zip")
    svc.reconcile_once()
    assert not kube.pods.get(name)
    rcon.log.clear()
    c = svc.complete(name, "tp ")
    assert c.status == "ready"
    texts = {s.text for s in c.items}
    assert "@a" in texts and "noah" not in texts  # no players: not running
    assert rcon.log == []
    assert not kube.active_jobs("commands")


def test_forge_and_old_worlds_get_no_job(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc, FIRE)  # Forge 1.16.5
    c = svc.complete(name, "ti")
    assert c.status == "none" and "modded" in c.message
    assert not kube.active_jobs("commands")
    # a vanilla tree for the same version, if known, is used
    d = svc.commands_dir
    d.mkdir(parents=True, exist_ok=True)
    raw = json.loads(SAMPLE_COMMANDS.read_text())
    (d / "1.16.5.commands.json").write_text(json.dumps(raw["commands"]))
    svc._cmd_cache.clear()
    assert svc.complete(name, "ti").status == "ready"


def test_bad_pointer_and_report_are_ignored(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    d = svc.commands_dir
    (d / "worlds").mkdir(parents=True)
    (d / "worlds" / f"{name}.json").write_text(
        json.dumps({"pod": f"{name}-abc", "version": "../../etc"})
    )
    c = svc.complete(name, "ti")
    assert c.status == "none" and c.items == []
    (d / "worlds" / f"{name}.json").write_text(
        json.dumps({"pod": f"{name}-abc", "version": "26.3"})
    )
    (d / "26.3.commands.json").write_text("{not json")
    assert svc.complete(name, "ti").status == "none"
    assert not kube.active_jobs("commands")
