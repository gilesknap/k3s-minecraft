import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mcdash.config import Config
from mcdash.helmops import (
    Helm,
    HelmError,
    java_tag_for,
    jvm_memory,
    level_type_for,
    render_values,
    settings_from_json,
    settings_to_json,
)
from mcdash.models import MCDashError, NotFound, ServerType, WorldSettings


@pytest.mark.parametrize(
    "v,tag",
    [
        ("LATEST", None),
        (None, None),
        ("1.12.2", "java8"),
        ("1.16.5", "java8"),
        ("1.17.1", "java16"),
        ("1.18.2", "java17"),
        ("1.20.4", "java17"),
        ("1.20.6", "java21"),
        ("1.21.4", "java21"),
        ("26.1", None),
        ("21w03a", None),
    ],
)
def test_java_tag(v: str | None, tag: str | None) -> None:
    assert java_tag_for(v) == tag


def test_memory() -> None:
    assert jvm_memory("4Gi") == "4G"
    assert jvm_memory("1536Mi") == "1536M"
    with pytest.raises(MCDashError):
        jvm_memory("lots")


def test_settings_json_roundtrip() -> None:
    s = WorldSettings(server_type=ServerType.FORGE, whitelist=["a"], pvp=True)
    assert settings_from_json(json.loads(json.dumps(settings_to_json(s)))) == s
    assert settings_from_json({"bogus": 1, "server_type": "PAPER"}) == WorldSettings()


def test_render_values() -> None:
    cfg = Config(world_node="nuc2", uid=1000, gid=1000)
    s = WorldSettings(
        version="1.16.5",
        server_type=ServerType.FORGE,
        whitelist=["noah", "giles"],
        enable_command_block=True,
    )
    v = render_values("fire-ice2", s, 30566, cfg, replicas=0, forge_version="36.1.0")
    ms = v["minecraftServer"]
    assert v["fullnameOverride"] == "fire-ice2"
    assert v["replicaCount"] == 0
    assert v["image"] == {"tag": "java8"}
    assert v["nodeSelector"] == {"kubernetes.io/hostname": "nuc2"}
    assert v["persistence"]["dataDir"] == {
        "enabled": True,
        "existingClaim": "fire-ice2-data",
    }
    assert ms["eula"] == "TRUE" and ms["onlineMode"] is True
    assert (ms["serviceType"], ms["nodePort"], ms["externalTrafficPolicy"]) == (
        "NodePort",
        30566,
        "Local",
    )
    assert ms["rcon"]["existingSecret"] == "fire-ice2-rcon"
    assert ms["rcon"]["serviceType"] == "ClusterIP"
    assert ms["type"] == "FORGE" and ms["forgeVersion"] == "36.1.0"
    assert ms["whitelist"] == "noah,giles"
    assert ms["memory"] == "4G"
    assert v["extraEnv"]["ENFORCE_WHITELIST"] == "TRUE"
    assert v["extraEnv"]["EXISTING_WHITELIST_FILE"] == "SYNCHRONIZE"
    assert v["mcdash"]["port"] == 30566
    assert v["mcdash"]["settings"]["whitelist"] == ["noah", "giles"]

    plain = render_values("w", WorldSettings(), 30565, Config())
    assert "image" not in plain and "nodeSelector" not in plain
    assert "whitelist" not in plain["minecraftServer"]
    assert plain["extraEnv"]["EXISTING_WHITELIST_FILE"] == "SKIP"


@pytest.mark.parametrize(
    "lt,version,want",
    [
        ("normal", "LATEST", None),
        ("flat", "LATEST", "minecraft:flat"),
        ("amplified", "1.21.4", "minecraft:amplified"),
        ("large_biomes", "1.19", "minecraft:large_biomes"),
        ("large_biomes", "26.1", "minecraft:large_biomes"),
        ("flat", "24w10a", "minecraft:flat"),
        ("flat", "1.18.2", "FLAT"),
        ("large_biomes", "1.16.5", "LARGEBIOMES"),
        ("bogus", "LATEST", None),
    ],
)
def test_level_type(lt: str, version: str, want: str | None) -> None:
    assert level_type_for(lt, version) == want


def test_render_seed_and_level_type() -> None:
    s = WorldSettings(seed="-42", level_type="amplified")
    ms = render_values("w", s, 30565, Config())["minecraftServer"]
    assert (ms["levelSeed"], ms["levelType"]) == ("-42", "minecraft:amplified")
    ms = render_values("w", WorldSettings(), 30565, Config())["minecraftServer"]
    assert "levelSeed" not in ms and "levelType" not in ms


CHART = os.environ.get("MCD_TEST_ITZG_CHART", "")


@pytest.mark.skipif(
    not (CHART and Path(CHART).is_dir() and shutil.which("helm")),
    reason="set MCD_TEST_ITZG_CHART to an unpacked itzg chart to run",
)
def test_render_against_real_chart(tmp_path: Path) -> None:
    s = WorldSettings(
        whitelist=["noah"], motd="hi & bye", seed="12345", level_type="flat"
    )
    v = render_values("king", s, 30565, Config(world_node="nuc2"), replicas=0)
    f = tmp_path / "v.json"
    f.write_text(json.dumps(v))
    out = subprocess.run(
        ["helm", "template", "king", CHART, "-n", "minecraft", "-f", str(f)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "replicas: 0" in out
    assert "nodePort: 30565" in out
    assert "externalTrafficPolicy: Local" in out
    assert "claimName: king-data" in out
    assert "name: king-rcon\n" in out or "name: king-rcon" in out
    assert '"29999984"' in out
    assert "ENFORCE_WHITELIST" in out
    assert "startupProbe:" in out
    assert 'name: SEED\n          value: "12345"' in out
    assert 'name: LEVEL_TYPE\n          value: "minecraft:flat"' in out


def _fake_helm(tmp_path: Path, script: str) -> Helm:
    exe = tmp_path / "helm"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    return Helm("minecraft", tmp_path / "chart", helm_bin=str(exe))


def test_helm_cli_wrapper(tmp_path: Path) -> None:
    log = tmp_path / "args"
    h = _fake_helm(
        tmp_path,
        f'echo "$@" >> {log}\n'
        'case "$1" in list) echo \'[{"name":"w","chart":"minecraft-5.2.0"}]\';;\n'
        "get) echo '{\"replicaCount\": 0}';;\nesac\n",
    )
    assert h.list() == [{"name": "w", "chart": "minecraft-5.2.0"}]
    assert h.get_values("w") == {"replicaCount": 0}
    h.upgrade_install("w", {"a": 1})
    lines = log.read_text().splitlines()
    assert lines[0] == "list --all -o json --namespace minecraft"
    assert lines[2].startswith("upgrade --install w ")
    assert "--reset-values -f" in lines[2]


def test_helm_errors(tmp_path: Path) -> None:
    h = _fake_helm(tmp_path, 'echo "Error: release: not found" >&2; exit 1\n')
    with pytest.raises(NotFound):
        h.get_values("x")
    h.uninstall("x")  # not found is fine
    h2 = _fake_helm(tmp_path, 'echo "Error: boom" >&2; exit 1\n')
    with pytest.raises(HelmError, match="boom"):
        h2.list()


def test_render_values_open_to_all() -> None:
    s = WorldSettings(whitelist=["noah", "giles"], open_to_all=True)
    v = render_values("w", s, 30565, Config())
    # itzg turns the whitelist on whenever WHITELIST is set
    assert "whitelist" not in v["minecraftServer"]
    assert v["extraEnv"]["ENABLE_WHITELIST"] == "FALSE"
    assert v["extraEnv"]["ENFORCE_WHITELIST"] == "FALSE"
    assert v["extraEnv"]["EXISTING_WHITELIST_FILE"] == "SKIP"
    # the names are kept for when the world is closed again
    assert v["mcdash"]["settings"]["whitelist"] == ["noah", "giles"]
    assert v["mcdash"]["settings"]["open_to_all"] is True
