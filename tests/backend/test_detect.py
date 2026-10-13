from pathlib import Path

from mcdash.detect import detect_zip, find_world, parse_properties
from mcdash.models import ServerType

from .helpers import make_world_zip


def test_vanilla_with_tmp_prefix(tmp_path: Path) -> None:
    z = make_world_zip(tmp_path / "a.zip", version="1.19.3")
    d = detect_zip(z)
    assert d.prefix == "tmp/tmp.w92y4CF76G/"
    assert d.world_dir == "tmp/tmp.w92y4CF76G/world/"
    assert d.server_type is ServerType.VANILLA
    assert d.mc_version == "1.19.3"
    assert d.properties["gamemode"] == "creative"
    assert d.forge_version is None


def test_forge_detection(tmp_path: Path) -> None:
    d = detect_zip(make_world_zip(tmp_path / "f.zip", forge=True, prefix=""))
    assert d.prefix == ""
    assert d.server_type is ServerType.FORGE
    assert d.forge_version == "36.1.0"
    assert d.mc_version == "1.16.5"


def test_forge_via_libraries_only(tmp_path: Path) -> None:
    z = make_world_zip(
        tmp_path / "f.zip",
        extra={
            "tmp/tmp.w92y4CF76G/libraries/net/minecraftforge/forge/"
            "1.12.2-14.23.5/x.jar": b""
        },
    )
    d = detect_zip(z)
    assert d.server_type is ServerType.FORGE
    assert d.forge_version == "14.23.5"


def test_forge_picks_newest_installer_jar(tmp_path: Path) -> None:
    # A server upgraded in place keeps the old installer jar (real fire-ice2).
    z = make_world_zip(
        tmp_path / "f.zip",
        forge=True,
        extra={
            "tmp/tmp.w92y4CF76G/forge-1.16.5-36.2.0.jar": b"jar",
            "tmp/tmp.w92y4CF76G/forge-1.16.5-36.10.0.jar": b"jar",
        },
    )
    assert detect_zip(z).forge_version == "36.10.0"


def test_forge_installer_jar_beats_libraries(tmp_path: Path) -> None:
    z = make_world_zip(
        tmp_path / "f.zip",
        forge=True,
        extra={
            "tmp/tmp.w92y4CF76G/libraries/net/minecraftforge/forge/"
            "1.16.5-36.9.0/x.jar": b""
        },
    )
    assert detect_zip(z).forge_version == "36.1.0"


def test_version_fallback_to_versions_dir(tmp_path: Path) -> None:
    z = make_world_zip(
        tmp_path / "v.zip", version=None, versions_dirs=("1.9.4", "1.16.5", "1.10")
    )
    assert detect_zip(z).mc_version == "1.16.5"


def test_corrupt_level_dat(tmp_path: Path) -> None:
    z = make_world_zip(
        tmp_path / "c.zip", prefix="p/", versions_dirs=("1.12.2",), version=None
    )
    d = detect_zip(z)
    assert d.mc_version == "1.12.2"


def test_no_world(tmp_path: Path) -> None:
    import zipfile

    p = tmp_path / "x.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("readme.txt", "hi")
    d = detect_zip(p)
    assert not d.valid


def test_find_world_prefers_server_layout_and_shallowest() -> None:
    names = [
        "a/b/world/level.dat",
        "a/world/level.dat",
        "a/world_nether/level.dat",
    ]
    assert find_world(names) == ("a/", "a/world/")


def test_find_world_single_player_layout() -> None:
    assert find_world(["saves/mansion-world/level.dat"]) == (
        "saves/",
        "saves/mansion-world/",
    )
    assert find_world(["level.dat", "region/r.0.0.mca"]) == ("", "")


def test_parse_properties() -> None:
    p = parse_properties(
        "#Minecraft server properties\n"
        "motd=A Minecraft Server\\: hi\n"
        "pvp=true\n"
        "level-seed=\n"
        "  difficulty = 2\n"
    )
    assert p == {
        "motd": "A Minecraft Server: hi",
        "pvp": "true",
        "level-seed": "",
        "difficulty": "2",
    }
