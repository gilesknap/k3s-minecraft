"""Per-world RCON NodePorts: allocation, persistence, exhaustion, password."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from mcdash.config import Config
from mcdash.models import Conflict, NotFound, WorldSettings, WorldState
from mcdash.service import KubeDashboardService

from .test_service import KING, ME, complete, launch_running, parts


def spec_of(svc: KubeDashboardService, name: str) -> dict:
    kube, _, _ = parts(svc)
    return json.loads(kube.configmaps[f"{name}-mcdash"][1]["spec"])


def make_legacy(svc: KubeDashboardService, name: str) -> None:
    """Strip the RCON port, as for a world launched before they existed."""
    kube, helm, _ = parts(svc)
    world, data = kube.configmaps[f"{name}-mcdash"]
    spec = json.loads(data["spec"])
    spec.pop("rconPort")
    kube.configmaps[f"{name}-mcdash"] = (world, {**data, "spec": json.dumps(spec)})
    v = helm.releases[name]
    v["mcdash"].pop("rconPort")
    v["minecraftServer"]["rcon"] = {"enabled": True, "serviceType": "ClusterIP"}
    helm.revisions[name] += 1


def test_launch_allocates_rcon_nodeport(svc: KubeDashboardService) -> None:
    _, helm, _ = parts(svc)
    name = launch_running(svc)
    assert spec_of(svc, name)["rconPort"] == 30600
    v = helm.releases[name]
    assert v["mcdash"]["rconPort"] == 30600
    assert v["minecraftServer"]["rcon"] == {
        "enabled": True,
        "port": 25575,
        "existingSecret": f"{name}-rcon",
        "secretKey": "rcon-password",
        "serviceType": "NodePort",
        "nodePort": 30600,
        "externalTrafficPolicy": "Local",
    }
    w = svc.get_world(name)
    assert (w.rcon_port, w.rcon_host, w.node) == (30600, "192.168.1.92", "nuc2")
    launch = svc.store.read_audit()[0]
    assert launch["action"] == "launch" and launch["rcon_port"] == 30600

    _, s = svc.launch_defaults(KING)
    w2 = svc.create_world("fresh", s, ME)
    assert (w2.port, w2.rcon_port) == (30566, 30601)


def test_rcon_port_stable_across_lifecycle(svc: KubeDashboardService) -> None:
    kube, helm, _ = parts(svc)
    name = launch_running(svc)
    svc.stop(name, ME)
    w = svc.get_world(name)
    assert w.state is WorldState.STOPPED and w.rcon_port == 30600
    # no pod: the node address is unknown, the node name falls back to config
    assert (w.rcon_host, w.node) == ("", "nuc2")
    svc.start(name, ME)
    svc.update_settings(name, WorldSettings(motd="x"), ME)
    svc.restore(name, KING, ME)
    complete(svc, "snapshot")
    complete(svc, "seed")
    assert helm.releases[name]["minecraftServer"]["rcon"]["nodePort"] == 30600
    assert svc.get_world(name).rcon_port == 30600

    # delete frees it for the next world
    svc.delete(name, ME)
    complete(svc, "snapshot")
    assert f"{name}-mcdash" not in kube.configmaps
    _, s = svc.launch_defaults(KING)
    assert svc.create_world("next", s, ME).rcon_port == 30600


def test_rcon_host_override(svc: KubeDashboardService) -> None:
    svc.cfg = replace(svc.cfg, rcon_host="mc-lan.example.org")
    name = launch_running(svc)
    assert svc.get_world(name).rcon_host == "mc-lan.example.org"


def test_rcon_ports_exhausted(svc: KubeDashboardService) -> None:
    svc.cfg = replace(svc.cfg, rcon_port_min=30600, rcon_port_max=30601)
    _, s = svc.launch_defaults(KING)
    svc.launch(KING, "a", s, ME)
    svc.create_world("b", s, ME)
    with pytest.raises(Conflict, match="no free RCON ports in 30600-30601"):
        svc.launch(KING, "c", s, ME)
    with pytest.raises(Conflict, match="no free RCON ports"):
        svc.create_world("c", s, ME)
    # nothing was reserved for the refused worlds
    assert sorted(w.name for w in svc.list_worlds()) == ["a", "b"]


def test_existing_world_gets_port_at_next_install(svc: KubeDashboardService) -> None:
    _, helm, _ = parts(svc)
    old = launch_running(svc)
    svc.stop(old, ME)
    make_legacy(svc, old)
    assert svc.get_world(old).rcon_port == 0
    # a world launched meanwhile takes the first RCON port
    _, s = svc.launch_defaults(KING)
    assert svc.create_world("newer", s, ME).rcon_port == 30600

    helm.fail_next = "boom"  # recorded even when the install fails
    with pytest.raises(Exception, match="boom"):
        svc.start(old, ME)
    assert spec_of(svc, old)["rconPort"] == 30601
    svc.start(old, ME)
    assert helm.releases[old]["minecraftServer"]["rcon"]["nodePort"] == 30601
    assert helm.releases[old]["mcdash"]["rconPort"] == 30601
    assert svc.get_world(old).rcon_port == 30601


def test_existing_world_start_with_rcon_ports_exhausted(
    svc: KubeDashboardService,
) -> None:
    _, helm, _ = parts(svc)
    old = launch_running(svc)
    svc.stop(old, ME)
    make_legacy(svc, old)
    svc.cfg = replace(svc.cfg, rcon_port_min=30600, rcon_port_max=30600)
    _, s = svc.launch_defaults(KING)
    svc.create_world("newer", s, ME)  # takes the only RCON port
    upgrades = len(helm.calls)
    with pytest.raises(Conflict, match="no free RCON ports"):
        svc.start(old, ME)
    assert len(helm.calls) == upgrades  # no install attempted
    w = svc.get_world(old)
    assert w.state is WorldState.STOPPED and w.rcon_port == 0


def test_rcon_port_recovered_from_release_values(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    kube.configmaps.clear()  # lost record: rebuilt from the release
    assert svc.get_world(name).rcon_port == 30600
    assert spec_of(svc, name)["rconPort"] == 30600

    # record without the port, release with it (saved install, lost save)
    world, data = kube.configmaps[f"{name}-mcdash"]
    spec = json.loads(data["spec"])
    spec.pop("rconPort")
    kube.configmaps[f"{name}-mcdash"] = (world, {**data, "spec": json.dumps(spec)})
    assert svc.get_world(name).rcon_port == 30600
    _, s = svc.launch_defaults(KING)
    assert svc.create_world("other", s, ME).rcon_port == 30601


def test_rcon_password_is_audited(svc: KubeDashboardService) -> None:
    kube, _, _ = parts(svc)
    name = launch_running(svc)
    pw = kube.secrets[f"{name}-rcon"]["rcon-password"]
    assert svc.rcon_password(name, ME) == pw
    rec = svc.store.read_audit()[-1]
    assert (rec["action"], rec["target"], rec["actor"]) == ("rcon_password", name, ME)
    assert pw not in json.dumps(svc.store.read_audit())
    with pytest.raises(NotFound):
        svc.rcon_password("nope", ME)


def test_config_rcon_range() -> None:
    cfg = Config.from_env(
        {
            "MCD_NAMESPACE": "mc",
            "MCD_RCON_PORT_MIN": "30700",
            "MCD_RCON_PORT_MAX": "30709",
            "MCD_RCON_HOST": "192.168.1.92",
        }
    )
    assert (cfg.rcon_port_min, cfg.rcon_port_max, cfg.rcon_host) == (
        30700,
        30709,
        "192.168.1.92",
    )
    d = Config.from_env({"MCD_NAMESPACE": "mc"})
    assert (d.rcon_port_min, d.rcon_port_max, d.rcon_host) == (30600, 30619, "")
    with pytest.raises(ValueError, match="overlap"):
        Config(port_min=30565, port_max=30584, rcon_port_min=30580)
    with pytest.raises(ValueError, match="empty"):
        Config(rcon_port_min=30610, rcon_port_max=30600)


def test_stopping_a_legacy_world_never_allocates(svc: KubeDashboardService) -> None:
    """A replicas=0 install (stop) must not fail because the RCON range is full."""
    _, helm, _ = parts(svc)
    old = launch_running(svc)
    make_legacy(svc, old)
    svc.cfg = replace(svc.cfg, rcon_port_min=30600, rcon_port_max=30600)
    _, s = svc.launch_defaults(KING)
    svc.create_world("newer", s, ME)  # takes the only RCON port
    svc.stop(old, ME)
    w = svc.get_world(old)
    assert w.state is WorldState.STOPPED and w.rcon_port == 0
    assert "nodePort" not in helm.releases[old]["minecraftServer"]["rcon"]
