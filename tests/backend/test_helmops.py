import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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
    startup_rcon_commands,
)
from mcdash.models import (
    MCDashError,
    NotFound,
    ServerType,
    WorldSettings,
    normalize_gamemode,
)


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
    s = WorldSettings(
        server_type=ServerType.FORGE, whitelist=["a"], ops=["a"], pvp=True
    )
    assert settings_from_json(json.loads(json.dumps(settings_to_json(s)))) == s
    assert settings_from_json({"bogus": 1, "server_type": "PAPER"}) == WorldSettings()
    # records saved before ops existed load with none
    assert settings_from_json({"whitelist": ["a"]}).ops == []


def test_render_values() -> None:
    cfg = Config(world_node="nuc2", uid=1000, gid=1000)
    s = WorldSettings(
        version="1.16.5",
        server_type=ServerType.FORGE,
        whitelist=["noah", "giles"],
        ops=["giles"],
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
    assert ms["ops"] == "giles"
    assert v["extraEnv"]["EXISTING_OPS_FILE"] == "SYNCHRONIZE"
    assert v["mcdash"]["port"] == 30566
    assert v["mcdash"]["settings"]["whitelist"] == ["noah", "giles"]

    plain = render_values("w", WorldSettings(), 30565, Config())
    assert "image" not in plain and "nodeSelector" not in plain
    assert "whitelist" not in plain["minecraftServer"]
    assert plain["extraEnv"]["EXISTING_WHITELIST_FILE"] == "SKIP"
    # no admins chosen: leave ops.json (e.g. the backup's) alone
    assert "ops" not in plain["minecraftServer"]
    assert plain["extraEnv"]["EXISTING_OPS_FILE"] == "SKIP"


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
        whitelist=["noah"],
        ops=["noah"],
        motd="hi & bye",
        seed="12345",
        level_type="flat",
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
    assert 'name: OPS\n          value: "noah"' in out
    assert 'name: EXISTING_OPS_FILE\n          value: "SYNCHRONIZE"' in out
    assert "startupProbe:" in out
    assert 'name: SEED\n          value: "12345"' in out
    assert 'name: LEVEL_TYPE\n          value: "minecraft:flat"' in out
    assert "RCON_CMDS_STARTUP" not in out
    # without an RCON port, RCON stays cluster-internal
    assert "type: ClusterIP" in out
    s = WorldSettings(gamemode="adventure")
    f.write_text(json.dumps(render_values("doom", s, 30566, Config())))
    out = subprocess.run(
        ["helm", "template", "doom", CHART, "-n", "minecraft", "-f", str(f)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert 'name: RCON_CMDS_STARTUP\n          value: "forceload add ~ ~"' in out


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
    # ops do not depend on the whitelist
    v = render_values("w", WorldSettings(open_to_all=True, ops=["steve"]), 1, Config())
    assert v["minecraftServer"]["ops"] == "steve"
    assert v["extraEnv"]["EXISTING_OPS_FILE"] == "SYNCHRONIZE"
    v = render_values("w", s, 30565, Config())
    # the names are kept for when the world is closed again
    assert v["mcdash"]["settings"]["whitelist"] == ["noah", "giles"]
    assert v["mcdash"]["settings"]["open_to_all"] is True
    # a hand-edited string in the stored record is not truthy
    assert settings_from_json({"open_to_all": "false"}).open_to_all is False


@pytest.mark.parametrize(
    "gamemode,version,want",
    [
        ("adventure", "LATEST", ["forceload add ~ ~"]),
        ("adventure", "26.3", ["forceload add ~ ~"]),
        ("adventure", "26.4-snapshot-1", ["forceload add ~ ~"]),
        ("adventure", "1.21.4", ["forceload add ~ ~"]),
        ("adventure", "1.14", ["forceload add ~ ~"]),
        ("Adventure", "LATEST", ["forceload add ~ ~"]),
        # no forceload command before 1.14 (e.g. old Forge worlds)
        ("adventure", "1.13.2", []),
        ("adventure", "1.12.2", []),
        # the respawn bug keys on the world's default game type only
        ("survival", "LATEST", []),
        ("creative", "26.3", []),
        ("spectator", "LATEST", []),
    ],
)
def test_startup_rcon_commands(gamemode: str, version: str, want: list[str]) -> None:
    s = WorldSettings(gamemode=gamemode, version=version)
    assert startup_rcon_commands(s) == want


@pytest.mark.parametrize("gamemode", ["adventure", "Adventure ", "2", "a", "A"])
def test_startup_rcon_commands_gamemode_aliases(gamemode: str) -> None:
    # itzg/server.properties spellings of adventure all trigger the forceload
    assert startup_rcon_commands(WorldSettings(gamemode=gamemode)) == [
        "forceload add ~ ~"
    ]


def test_normalize_gamemode() -> None:
    assert normalize_gamemode(" 2 ") == "adventure"
    assert normalize_gamemode("sp") == "spectator"
    assert normalize_gamemode("creative") == "creative"
    assert normalize_gamemode("weird") == "weird"


def test_render_values_forceload_spawn() -> None:
    s = WorldSettings(gamemode="adventure")
    env = render_values("doom", s, 30565, Config())["extraEnv"]
    assert env["RCON_CMDS_STARTUP"] == "forceload add ~ ~"

    plain = render_values("w", WorldSettings(), 30565, Config())["extraEnv"]
    assert "RCON_CMDS_STARTUP" not in plain

    forge = WorldSettings(
        gamemode="adventure", version="1.12.2", server_type=ServerType.FORGE
    )
    v = render_values("old", forge, 30565, Config(), forge_version="14.23.5")
    assert "RCON_CMDS_STARTUP" not in v["extraEnv"]

    forge = WorldSettings(
        gamemode="adventure", version="1.16.5", server_type=ServerType.FORGE
    )
    v = render_values("fire-ice2", forge, 30565, Config(), forge_version="36.1.0")
    assert v["extraEnv"]["RCON_CMDS_STARTUP"] == "forceload add ~ ~"


def test_render_values_rcon_nodeport() -> None:
    v = render_values("w", WorldSettings(), 30565, Config(), rcon_port=30600)
    rc = v["minecraftServer"]["rcon"]
    assert (rc["serviceType"], rc["nodePort"], rc["externalTrafficPolicy"]) == (
        "NodePort",
        30600,
        "Local",
    )
    assert rc["existingSecret"] == "w-rcon" and rc["port"] == 25575
    assert v["mcdash"]["rconPort"] == 30600
    # the game service is unchanged
    assert v["minecraftServer"]["nodePort"] == 30565

    plain = render_values("w", WorldSettings(), 30565, Config())
    assert plain["minecraftServer"]["rcon"]["serviceType"] == "ClusterIP"
    assert "nodePort" not in plain["minecraftServer"]["rcon"]
    assert plain["mcdash"]["rconPort"] == 0


@pytest.mark.skipif(
    not (CHART and Path(CHART).is_dir() and shutil.which("helm")),
    reason="set MCD_TEST_ITZG_CHART to an unpacked itzg chart to run",
)
def test_render_rcon_nodeport_against_real_chart(tmp_path: Path) -> None:
    v = render_values("king", WorldSettings(), 30565, Config(), rcon_port=30600)
    f = tmp_path / "v.json"
    f.write_text(json.dumps(v))
    out = subprocess.run(
        ["helm", "template", "king", CHART, "-n", "minecraft", "-f", str(f)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    svcs = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    rcon = svcs["king-rcon"]["spec"]
    assert rcon["type"] == "NodePort"
    assert rcon["externalTrafficPolicy"] == "Local"
    assert rcon["ports"] == [
        {
            "name": "rcon",
            "port": 25575,
            "nodePort": 30600,
            "targetPort": "rcon",
            "protocol": "TCP",
        }
    ]
    game = svcs["king"]["spec"]
    assert game["type"] == "NodePort" and game["ports"][0]["nodePort"] == 30565
    # the labels the dashboard chart's NetworkPolicy selects on
    (dep,) = [d for d in docs if d["kind"] == "Deployment"]
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "minecraft"
    assert labels["mcdash/world"] == "king"
    ports = {p["name"]: p["containerPort"] for p in _container(dep)["ports"]}
    assert ports == {"minecraft": 25565, "rcon": 25575}


def _container(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]
