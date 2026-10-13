"""Console completion: the command-tree walker and the report Job script."""

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from mcdash.completion import REPORT_SCRIPT, CommandTree
from mcdash.web.fake import SAMPLE_COMMANDS, sample_tree

PLAYERS = ["NoahK", "GilesDad"]


def texts(line: str, players: list[str] | None = None) -> list[str]:
    return [s.text for s in sample_tree().suggest(line, players or PLAYERS)[1]]


def picks(line: str) -> list[str]:
    _, items = sample_tree().suggest(line, PLAYERS)
    return [s.text for s in items if s.kind != "hint"]


def test_sample_is_a_real_report_extract() -> None:
    raw = json.loads(SAMPLE_COMMANDS.read_text())
    assert raw["commands"]["type"] == "root"
    assert {"time", "tp", "teleport", "execute", "give"} <= set(
        raw["commands"]["children"]
    )


def test_command_names() -> None:
    assert picks("ti") == ["tick", "time", "title"]
    start, items = sample_tree().suggest("ti", [])
    assert start == 0 and all(s.kind == "literal" for s in items)
    assert picks("")[:3] == ["deop", "difficulty", "effect"]


def test_subcommands() -> None:
    assert {"set", "add", "query"} <= set(picks("time "))
    assert picks("time s") == ["set"]
    assert sample_tree().suggest("time s", [])[0] == 5


def test_players_for_entity_arguments() -> None:
    got = picks("tp ")
    assert {"NoahK", "GilesDad", "@a", "@p", "@s"} <= set(got)
    assert picks("tp No") == ["NoahK"]
    # a players-only argument offers no entity-only selectors
    assert "@e" not in picks("gamemode creative ")
    assert "NoahK" in picks("gamemode creative ")


def test_hints_name_the_arguments() -> None:
    _, items = sample_tree().suggest("tp ", PLAYERS)
    hints = {s.text for s in items if s.kind == "hint"}
    assert {"<targets>", "<location>", "<destination>"} <= hints
    # hints come after the insertable candidates
    kinds = [s.kind for s in items]
    assert kinds.index("hint") > max(i for i, k in enumerate(kinds) if k != "hint")


def test_literal_wins_over_argument() -> None:
    # As in Brigadier, a matching literal hides the arguments beside it.
    lit = {"type": "literal", "executable": True}
    word = {"type": "argument", "parser": "brigadier:string"}
    tree = CommandTree(
        {
            "type": "root",
            "children": {
                "cmd": {
                    "type": "literal",
                    "children": {
                        "x": {"type": "literal", "children": {"a": lit}},
                        "arg": {
                            **word,
                            "properties": {"type": "word"},
                            "children": {"b": lit},
                        },
                    },
                }
            },
        }
    )
    assert [s.text for s in tree.suggest("cmd x ", [])[1]] == ["a"]
    assert [s.text for s in tree.suggest("cmd y ", [])[1]] == ["b"]
    assert picks("time query ") == ["gametime", "time"]
    assert "<timeline>" in texts("time query ")


def test_redirects_and_execute_run() -> None:
    assert picks("tell ") == picks("msg ")
    assert picks("execute as @a run ti") == ["tick", "time", "title"]
    assert "NoahK" in picks("execute as @a at @s run tp ")


def test_multi_token_coordinates() -> None:
    # tp <location> takes three coordinates
    assert texts("tp ~ ~1 ") == ["<location>"]
    assert texts("setblock 1 2 ") == ["<pos>"]
    assert picks("setblock 1 2 3 end_st")[0] == "end_stone"
    assert "<block>" in texts("setblock ~ ~ ~ ")


def test_registry_values() -> None:
    got = picks("give @p dia")
    assert got[0] == "diamond" and "diamond_sword" in got
    # a word inside the id matches too, after the prefix matches
    assert "diamond_sword" in picks("give @p sword")
    assert "speed" in picks("effect give @a spe")
    assert "pig" in picks("summon pi")


def test_greedy_and_unknown() -> None:
    assert texts("say hello wor") == ["<message>"]
    assert texts("nonsense ") == []
    assert texts("time set day extra ") == []


def test_slash_and_offsets() -> None:
    start, items = sample_tree().suggest("/time q", [])
    assert start == 6 and [s.text for s in items] == ["query"]
    start, _ = sample_tree().suggest("  ti", [])
    assert start == 2


def test_exact_match_not_repeated() -> None:
    assert "time" not in picks("time")


def test_rejects_non_report() -> None:
    with pytest.raises(ValueError):
        CommandTree({"type": "literal"})


def test_load_without_registries(tmp_path: Path) -> None:
    raw = json.loads(SAMPLE_COMMANDS.read_text())
    (tmp_path / "c.json").write_text(json.dumps(raw["commands"]))
    tree = CommandTree.load(tmp_path / "c.json", tmp_path / "missing.json")
    assert [s.text for s in tree.suggest("give @p dia", [])[1]] == ["<item>"]


# ------------------------------------------------------------ the Job script

FAKE_JAVA = """#!/bin/sh
echo "$@" >> "$JAVA_LOG"
[ -n "${JAVA_FAIL:-}" ] && exit 1
while [ $# -gt 0 ]; do
  if [ "$1" = --output ]; then out=$2; fi
  shift
done
mkdir -p "$out/reports"
echo '{"type": "root", "children": {}}' > "$out/reports/commands.json"
echo '{}' > "$out/reports/registries.json"
"""


@pytest.fixture
def job_env(tmp_path: Path) -> dict[str, str]:
    if not shutil.which("sh") or not shutil.which("unzip"):
        pytest.skip("needs sh and unzip")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    java = bin_ / "java"
    java.write_text(FAKE_JAVA)
    java.chmod(0o755)
    data = tmp_path / "data"
    data.mkdir()
    with zipfile.ZipFile(data / "minecraft_server.26.3.jar", "w") as zf:
        zf.writestr("META-INF/versions.list", "x")
    (data / ".install-vanilla.env").write_text(
        'TYPE="VANILLA"\nSERVER="/x/minecraft_server.26.3.jar"\nVERSION="26.3"\n'.replace(
            "/x", str(data)
        )
    )
    out = tmp_path / "out"
    out.mkdir()
    return {
        **os.environ,
        "PATH": f"{bin_}:{os.environ['PATH']}",
        "JAVA_LOG": str(tmp_path / "java.log"),
        "MCD_DATA": str(data),
        "MCD_OUT": str(out),
        "MCD_WORLD": "w",
        "MCD_POD": "w-abc-1",
        "TMPDIR": str(tmp_path),
    }


def run_job(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", REPORT_SCRIPT], env=env, capture_output=True, text=True
    )


def pointer(env: dict[str, str]) -> dict[str, str]:
    return json.loads((Path(env["MCD_OUT"]) / "worlds" / "w.json").read_text())


def test_job_generates_once_per_version(job_env: dict[str, str]) -> None:
    r = run_job(job_env)
    assert r.returncode == 0, r.stderr
    out = Path(job_env["MCD_OUT"])
    assert (out / "26.3.commands.json").is_file()
    assert (out / "26.3.registries.json").is_file()
    assert pointer(job_env) == {"pod": "w-abc-1", "version": "26.3", "error": ""}
    log = Path(job_env["JAVA_LOG"]).read_text()
    assert "-DbundlerMainClass=net.minecraft.data.Main -jar" in log
    assert "--reports" in log
    # another pod on the same version: no second generator run
    r = run_job({**job_env, "MCD_POD": "w-abc-2"})
    assert r.returncode == 0
    assert Path(job_env["JAVA_LOG"]).read_text() == log
    assert pointer(job_env)["pod"] == "w-abc-2"
    assert not [p for p in out.iterdir() if ".tmp." in p.name]


def test_job_old_flat_jar(job_env: dict[str, str]) -> None:
    jar = Path(job_env["MCD_DATA"]) / "minecraft_server.26.3.jar"
    with zipfile.ZipFile(jar, "w") as zf:
        zf.writestr("net/minecraft/data/Main.class", "x")
    assert run_job(job_env).returncode == 0
    assert "-cp " in Path(job_env["JAVA_LOG"]).read_text()


def test_job_not_vanilla(job_env: dict[str, str]) -> None:
    (Path(job_env["MCD_DATA"]) / ".install-vanilla.env").unlink()
    assert run_job(job_env).returncode == 0
    assert pointer(job_env)["error"] == "not a vanilla server"


def test_job_generator_failure(job_env: dict[str, str]) -> None:
    r = run_job({**job_env, "JAVA_FAIL": "1"})
    assert r.returncode == 1
    assert pointer(job_env) == {
        "pod": "w-abc-1",
        "version": "26.3",
        "error": "the data generator failed",
    }
    assert not (Path(job_env["MCD_OUT"]) / "26.3.commands.json").exists()


def test_job_rejects_odd_version(job_env: dict[str, str]) -> None:
    (Path(job_env["MCD_DATA"]) / ".install-vanilla.env").write_text(
        'VERSION="../../etc"\nSERVER="x"\n'
    )
    assert run_job(job_env).returncode == 1
    assert pointer(job_env)["error"] == "unrecognised version"
    assert not Path(job_env["JAVA_LOG"]).exists()
