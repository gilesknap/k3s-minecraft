"""``helm template`` of the dashboard's own chart (charts/minecraft-dashboard)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

CHART = Path(__file__).resolve().parents[2] / "charts" / "minecraft-dashboard"
STORAGE = [
    "--set",
    "storage.archive.nfs.server=192.0.2.1",
    "--set",
    "storage.archive.nfs.path=/archive",
    "--set",
    "storage.store.nfs.server=192.0.2.1",
    "--set",
    "storage.store.nfs.path=/store",
]

pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="needs helm")


def template(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", "mcd", str(CHART), "-n", "minecraft", *STORAGE, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def docs(*args: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(template(*args).stdout) if d]


def policies(*args: str) -> list[dict[str, Any]]:
    return [d for d in docs(*args) if d["kind"] == "NetworkPolicy"]


def env_of(*args: str) -> dict[str, str]:
    (dep,) = [d for d in docs(*args) if d["kind"] == "Deployment"]
    (c,) = dep["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value", "") for e in c["env"]}


def test_network_policy_default() -> None:
    (np,) = policies()
    spec = np["spec"]
    assert spec["policyTypes"] == ["Ingress"]
    sel = spec["podSelector"]
    assert sel["matchLabels"] == {"app.kubernetes.io/name": "minecraft"}
    assert sel["matchExpressions"] == [{"key": "mcdash/world", "operator": "Exists"}]
    game, rcon = spec["ingress"]
    assert game == {"ports": [{"protocol": "TCP", "port": 25565}]}  # from anyone
    assert rcon["ports"] == [{"protocol": "TCP", "port": 25575}]
    assert rcon["from"] == [
        {
            "podSelector": {
                "matchLabels": {
                    "app.kubernetes.io/name": "minecraft-dashboard",
                    "app.kubernetes.io/instance": "mcd",
                }
            }
        },
        {"ipBlock": {"cidr": "192.168.1.0/24"}},
    ]


def test_network_policy_selects_world_pods_only() -> None:
    """Not the dashboard pod, nor the Jobs (labels from k8s.Kube.build_job)."""
    (np,) = policies()
    sel = np["spec"]["podSelector"]

    def selected(labels: dict[str, str]) -> bool:
        ok = all(labels.get(k) == v for k, v in sel["matchLabels"].items())
        return ok and all(e["key"] in labels for e in sel["matchExpressions"])

    (dep,) = [d for d in docs() if d["kind"] == "Deployment"]
    assert not selected(dep["spec"]["template"]["metadata"]["labels"])
    job = {
        "app.kubernetes.io/managed-by": "mcdash",
        "mcdash/world": "w",
        "mcdash/job": "seed",
    }
    assert not selected(job)
    world = {
        "app": "w",
        "app.kubernetes.io/name": "minecraft",
        "app.kubernetes.io/instance": "w",
        "mcdash/world": "w",
    }
    assert selected(world)
    # an itzg release the dashboard does not manage
    assert not selected({"app": "x", "app.kubernetes.io/name": "minecraft"})


def test_network_policy_cidrs_and_toggle() -> None:
    (np,) = policies(
        "--set", "networkPolicy.rconAllowedCidrs={192.168.1.0/24,192.168.0.0/24}"
    )
    cidrs = [p["ipBlock"]["cidr"] for p in np["spec"]["ingress"][1]["from"][1:]]
    assert cidrs == ["192.168.1.0/24", "192.168.0.0/24"]
    # an ipBlock with exceptions (e.g. the other cluster nodes)
    (np,) = policies(
        "--set",
        "networkPolicy.rconAllowedCidrs[0].cidr=192.168.1.0/24",
        "--set",
        "networkPolicy.rconAllowedCidrs[0].except={192.168.1.81/32,192.168.1.84/32}",
    )
    assert np["spec"]["ingress"][1]["from"][1] == {
        "ipBlock": {
            "cidr": "192.168.1.0/24",
            "except": ["192.168.1.81/32", "192.168.1.84/32"],
        }
    }
    res = template("--set", "networkPolicy.rconAllowedCidrs={not-a-cidr}", check=False)
    assert res.returncode != 0
    (np,) = policies("--set", "networkPolicy.rconAllowedCidrs=null")
    assert len(np["spec"]["ingress"][1]["from"]) == 1  # dashboard only
    assert policies("--set", "networkPolicy.enabled=false") == []


def test_rcon_settings_reach_the_dashboard() -> None:
    env = env_of()
    assert (env["MCD_RCON_PORT_MIN"], env["MCD_RCON_PORT_MAX"]) == ("30600", "30619")
    assert env["MCD_RCON_HOST"] == ""
    env = env_of(
        "--set",
        "worlds.rconPortMin=30700",
        "--set",
        "worlds.rconPortMax=30709",
        "--set",
        "worlds.rconHost=192.168.1.92",
    )
    assert (env["MCD_RCON_PORT_MIN"], env["MCD_RCON_PORT_MAX"]) == ("30700", "30709")
    assert env["MCD_RCON_HOST"] == "192.168.1.92"


def test_overlapping_ranges_rejected() -> None:
    res = template("--set", "worlds.rconPortMin=30580", check=False)
    assert res.returncode != 0
    assert "must not overlap" in res.stderr
