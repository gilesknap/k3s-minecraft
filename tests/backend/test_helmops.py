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
    property_supported,
    render_values,
    settings_from_json,
    settings_to_json,
    startup_rcon_commands,
)
from mcdash.models import (
    SETTING_RANGES,
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


# every managed server.properties setting, at non-default values
TUNED = WorldSettings(
    view_distance=16,
    simulation_distance=6,
    spawn_protection=0,
    pause_when_empty_seconds=0,
    sync_chunk_writes=False,
    allow_flight=True,
    max_players=8,
    broadcast_rcon_to_ops=False,
    broadcast_console_to_ops=False,
    hardcore=True,
)


def test_settings_json_old_record() -> None:
    """Records from before the server.properties settings leave them alone."""
    s = settings_from_json({"gamemode": "creative", "pvp": True})
    for field in [*SETTING_RANGES, "sync_chunk_writes", "allow_flight"]:
        assert getattr(s, field) is None
    assert s.broadcast_rcon_to_ops is None and s.broadcast_console_to_ops is None
    assert s.hardcore is False
    assert s.use_aikar_flags is True  # the one new default old records get
    v = render_values("old", s, 30565, Config())
    for key in ("viewDistance", "spawnProtection", "maxPlayers", "hardcore"):
        assert key not in v["minecraftServer"]
    env = v["extraEnv"]
    for key in (
        "SIMULATION_DISTANCE",
        "PAUSE_WHEN_EMPTY_SECONDS",
        "SYNC_CHUNK_WRITES",
        "ALLOW_FLIGHT",
        "BROADCAST_RCON_TO_OPS",
        "BROADCAST_CONSOLE_TO_OPS",
    ):
        assert key not in env
    assert env["USE_AIKAR_FLAGS"] == "true"


def test_settings_json_new_fields() -> None:
    assert settings_from_json(json.loads(json.dumps(settings_to_json(TUNED)))) == TUNED
    off = WorldSettings(use_aikar_flags=False)
    assert settings_from_json(settings_to_json(off)).use_aikar_flags is False
    # malformed values are dropped, not rendered
    s = settings_from_json(
        {"view_distance": "12", "max_players": True, "allow_flight": "yes"}
    )
    assert (s.view_distance, s.max_players, s.allow_flight) == (None, None, None)


def test_render_server_properties() -> None:
    v = render_values("tuned", TUNED, 30565, Config())
    ms, env = v["minecraftServer"], v["extraEnv"]
    # typed chart values, as ints
    assert ms["viewDistance"] == 16 and isinstance(ms["viewDistance"], int)
    assert ms["spawnProtection"] == 0
    assert ms["maxPlayers"] == 8
    assert ms["hardcore"] is True
    # no chart value: itzg env, as strings
    assert env["SIMULATION_DISTANCE"] == "6"
    assert env["PAUSE_WHEN_EMPTY_SECONDS"] == "0"
    assert env["SYNC_CHUNK_WRITES"] == "false"
    assert env["ALLOW_FLIGHT"] == "true"
    assert env["BROADCAST_RCON_TO_OPS"] == "false"
    assert env["BROADCAST_CONSOLE_TO_OPS"] == "false"
    assert env["USE_AIKAR_FLAGS"] == "true"
    v = render_values("x", WorldSettings(use_aikar_flags=False), 30565, Config())
    assert "USE_AIKAR_FLAGS" not in v["extraEnv"]
    assert "hardcore" not in v["minecraftServer"]


@pytest.mark.parametrize(
    "field,version,want",
    [
        ("simulation_distance", "LATEST", True),
        ("simulation_distance", "26.3", True),
        ("simulation_distance", "1.18", True),
        ("simulation_distance", "1.17.1", False),
        ("pause_when_empty_seconds", "1.21.2", True),
        ("pause_when_empty_seconds", "1.21.1", False),
        ("pause_when_empty_seconds", "1.21", False),
        ("pause_when_empty_seconds", "24w33a", True),  # unparseable = new
        ("sync_chunk_writes", "1.16", True),
        ("sync_chunk_writes", "1.15.2", False),
        ("view_distance", "1.7.10", True),
        ("allow_flight", None, True),
    ],
)
def test_property_supported(field: str, version: str | None, want: bool) -> None:
    assert property_supported(field, version) is want


def test_render_version_gates() -> None:
    s = WorldSettings(
        version="1.12.2",
        server_type=ServerType.FORGE,
        view_distance=8,
        simulation_distance=8,
        pause_when_empty_seconds=0,
        sync_chunk_writes=False,
        allow_flight=True,
    )
    v = render_values("forge", s, 30565, Config())
    env = v["extraEnv"]
    assert v["minecraftServer"]["viewDistance"] == 8
    assert env["ALLOW_FLIGHT"] == "true"
    for key in ("SIMULATION_DISTANCE", "PAUSE_WHEN_EMPTY_SECONDS", "SYNC_CHUNK_WRITES"):
        assert key not in env
    s.version = "1.20.1"  # has simulation-distance and sync-chunk-writes, no pause
    env = render_values("forge", s, 30565, Config())["extraEnv"]
    assert env["SIMULATION_DISTANCE"] == "8"
    assert env["SYNC_CHUNK_WRITES"] == "false"
    assert "PAUSE_WHEN_EMPTY_SECONDS" not in env


@pytest.mark.skipif(
    not (CHART and Path(CHART).is_dir() and shutil.which("helm")),
    reason="set MCD_TEST_ITZG_CHART to an unpacked itzg chart to run",
)
def test_render_server_properties_against_real_chart(tmp_path: Path) -> None:
    def env_of(s: WorldSettings) -> dict[str, str]:
        f = tmp_path / "v.json"
        f.write_text(json.dumps(render_values("king", s, 30565, Config())))
        out = subprocess.run(
            ["helm", "template", "king", CHART, "-n", "minecraft", "-f", str(f)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        docs = [d for d in yaml.safe_load_all(out) if d]
        (dep,) = [d for d in docs if d["kind"] == "Deployment"]
        return {e["name"]: e.get("value") for e in _container(dep)["env"]}

    env = env_of(TUNED)
    assert env["VIEW_DISTANCE"] == "16"
    assert env["SPAWN_PROTECTION"] == "0"  # zero still rendered
    assert env["MAX_PLAYERS"] == "8"
    assert env["HARDCORE"] == "true"
    assert env["SIMULATION_DISTANCE"] == "6"
    assert env["PAUSE_WHEN_EMPTY_SECONDS"] == "0"
    assert env["SYNC_CHUNK_WRITES"] == "false"
    assert env["ALLOW_FLIGHT"] == "true"
    assert env["BROADCAST_RCON_TO_OPS"] == "false"
    assert env["BROADCAST_CONSOLE_TO_OPS"] == "false"
    assert env["USE_AIKAR_FLAGS"] == "true"
    # the largest allowed values stay plain integers (no 1e+06)
    big = WorldSettings()
    for field, (_, hi) in SETTING_RANGES.items():
        setattr(big, field, hi)
    env = env_of(big)
    for field, key in (
        ("view_distance", "VIEW_DISTANCE"),
        ("spawn_protection", "SPAWN_PROTECTION"),
        ("max_players", "MAX_PLAYERS"),
        ("simulation_distance", "SIMULATION_DISTANCE"),
        ("pause_when_empty_seconds", "PAUSE_WHEN_EMPTY_SECONDS"),
    ):
        assert env[key] == str(SETTING_RANGES[field][1])
    # unmanaged (old record): the chart's "default" placeholders render nothing
    env = env_of(settings_from_json({}))
    for key in ("VIEW_DISTANCE", "SPAWN_PROTECTION", "MAX_PLAYERS", "HARDCORE"):
        assert key not in env
