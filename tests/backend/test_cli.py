from pathlib import Path

import pytest

from mcdash.cli import main

from .helpers import make_world_zip


def test_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_world_zip(tmp_path / "2021-06-03-10_30_33-fire-ice2.zip", forge=True)
    make_world_zip(tmp_path / "22-01-21.21.50.00-noah-james.zip", version="1.18.1")
    (tmp_path / "broken.zip").write_bytes(b"nope")
    before = sorted(p.name for p in tmp_path.iterdir())
    assert main(["index", str(tmp_path)]) == 0
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert lines[0].split()[:4] == ["WORLD", "DATE", "TYPE", "VERSION"]
    assert lines[1].split()[:5] == [
        "fire-ice2",
        "2021-06-03",
        "10:30",
        "FORGE",
        "1.16.5",
    ]
    assert lines[2].split()[:5] == [
        "noah-james",
        "2022-01-21",
        "21:50",
        "VANILLA",
        "1.18.1",
    ]
    assert "1 zip(s) without world/level.dat skipped" in err
    assert sorted(p.name for p in tmp_path.iterdir()) == before  # read-only use


def test_index_missing_dir(tmp_path: Path) -> None:
    assert main(["index", str(tmp_path / "nope")]) == 1
