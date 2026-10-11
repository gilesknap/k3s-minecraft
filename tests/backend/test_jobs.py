import stat
import zipfile
from pathlib import Path

import pytest

from mcdash.jobs.__main__ import main
from mcdash.jobs.seed import SeedError, map_member, safe_relpath, seed

from .helpers import level_dat, make_world_zip


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_seed_vanilla(tmp_path: Path) -> None:
    z = make_world_zip(tmp_path / "w.zip", forge=True)
    dest = tmp_path / "data"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    (dest / "olddir").mkdir()
    seed(z, dest, "vanilla", None, None)
    assert files(dest) == {
        ".mcdash-seed.json",
        "world/level.dat",
        "world/region/r.0.0.mca",
        "ops.json",
        "whitelist.json",
        "banned-players.json",
    }


def test_seed_full_excludes_regenerable(tmp_path: Path) -> None:
    z = make_world_zip(tmp_path / "w.zip", forge=True)
    dest = tmp_path / "data"
    dest.mkdir()
    seed(z, dest, "full", None, None)
    got = files(dest)
    assert "mods/iceandfire-2.1.6.jar" in got
    assert "config/mod.toml" in got
    assert "server.properties" in got
    assert "forge-1.16.5-36.1.0.jar" in got
    assert not any(
        f.startswith(("libraries/", "logs/", "?/", "versions/", ".forge-installed"))
        for f in got
    )


def test_seed_zip_slip_and_symlinks(tmp_path: Path) -> None:
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("world/level.dat", level_dat())
        zf.writestr("world/../../escape.txt", "x")
        zf.writestr("/abs.txt", "x")
        info = zipfile.ZipInfo("world/link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    dest = tmp_path / "data"
    dest.mkdir()
    seed(z, dest, "full", None, None)
    assert not (tmp_path / "escape.txt").exists()
    assert not (dest / "world" / "link").exists()
    assert files(dest) == {".mcdash-seed.json", "world/level.dat"}


def test_safe_relpath() -> None:
    assert safe_relpath("a/./b") is not None
    assert safe_relpath("../x") is None
    assert safe_relpath("/x") is None
    assert safe_relpath("C:/x") is None
    assert safe_relpath("a\\..\\..\\x") is None


def test_map_member_single_player() -> None:
    assert map_member("saves/m/level.dat", "saves/", "saves/m/", "vanilla") == (
        "world/level.dat"
    )
    assert map_member("saves/world/x", "saves/", "saves/m/", "full") is None


def test_seed_no_world(tmp_path: Path) -> None:
    z = tmp_path / "x.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.txt", "x")
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "keep").write_text("x")
    with pytest.raises(SeedError):
        seed(z, dest, "vanilla", None, None)
    assert (dest / "keep").exists()  # not wiped when the source is unusable


def test_snapshot_roundtrip(tmp_path: Path) -> None:
    z = make_world_zip(tmp_path / "w.zip", forge=True)
    data = tmp_path / "data"
    data.mkdir()
    seed(z, data, "full", None, None)
    (data / "logs").mkdir()
    (data / "logs" / "latest.log").write_text("x")
    out = tmp_path / "snaps" / "w" / "20261011T120000Z.zip"
    assert main(["snapshot", "--src", str(data), "--dest", str(out)]) == 0
    assert out.is_file()
    assert not list(out.parent.glob(".*.tmp"))
    names = zipfile.ZipFile(out).namelist()
    assert "world/level.dat" in names
    assert not any(n.startswith(("logs/", ".mcdash")) for n in names)

    again = tmp_path / "data2"
    again.mkdir()
    assert (
        main(["seed", "--src", str(out), "--dest", str(again), "--mode", "full"]) == 0
    )
    assert files(again) - {".mcdash-seed.json"} == files(data) - {
        ".mcdash-seed.json",
        "logs/latest.log",
    }


def test_snapshot_requires_world(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    rc = main(
        ["snapshot", "--src", str(tmp_path / "data"), "--dest", str(tmp_path / "o.zip")]
    )
    assert rc == 1
    assert not (tmp_path / "o.zip").exists()


def test_seed_cli_bad_src(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    rc = main(
        ["seed", "--src", str(tmp_path / "nope.zip"), "--dest", str(tmp_path / "d")]
    )
    assert rc == 1


@pytest.mark.parametrize("cmd", ["seed", "snapshot"])
def test_jobs_refuse_archive_dest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    archive = tmp_path / "archive"
    z = make_world_zip(archive / "w.zip")
    monkeypatch.setenv("MCD_ARCHIVE_DIR", str(archive))
    data = tmp_path / "data"
    data.mkdir()
    seed(z, data, "full", None, None)
    before = sorted(p.name for p in archive.rglob("*"))
    if cmd == "seed":
        args = ["seed", "--src", str(z), "--dest", str(archive), "--mode", "full"]
    else:
        args = ["snapshot", "--src", str(data), "--dest", str(archive / "s.zip")]
    assert main(args) == 1
    # a symlink into the archive is resolved and refused too
    link = tmp_path / "link"
    link.symlink_to(archive)
    args[args.index("--dest") + 1] = str(link / "x")
    assert main(args) == 1
    assert sorted(p.name for p in archive.rglob("*")) == before
