from datetime import UTC, datetime

import pytest

from mcdash.naming import (
    is_valid_world_name,
    parse_backup_name,
    sanitize_world_name,
    unique_name,
)


def dt(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)


@pytest.mark.parametrize(
    "name,world,ts",
    [
        (
            "2021-05-15-21_56_15-transformers-best.zip",
            "transformers-best",
            dt(2021, 5, 15, 21, 56, 15),
        ),
        (
            "2021-05-15-21_56_20-king-ghidorah(1).zip",
            "king-ghidorah",
            dt(2021, 5, 15, 21, 56, 20),
        ),
        (
            "2022-02-04+21.14.00-transformers-again(1).zip",
            "transformers-again",
            dt(2022, 2, 4, 21, 14, 0),
        ),
        (
            "2022-08-15_22_56_49-noah-james.zip",
            "noah-james",
            dt(2022, 8, 15, 22, 56, 49),
        ),
        ("22-01-21.21.50.00-noah-james.zip", "noah-james", dt(2022, 1, 21, 21, 50, 0)),
        (
            "2021-05-15-21:58:22-devils-deep.zip",
            "devils-deep",
            dt(2021, 5, 15, 21, 58, 22),
        ),
        (
            "2021-05-09-world-of-animals-old1.zip",
            "world-of-animals-old1",
            dt(2021, 5, 9),
        ),
        ("noah-james/20261011T120000Z.zip", "noah-james", dt(2026, 10, 11, 12, 0, 0)),
    ],
)
def test_parse_backup_name(name: str, world: str, ts: datetime) -> None:
    assert parse_backup_name(name, mtime=0) == (world, ts)


def test_parse_falls_back_to_mtime() -> None:
    w, ts = parse_backup_name("noah_single_player_best_2021_05/mansion-world.zip", 1e9)
    assert w == "mansion-world"
    assert ts == datetime.fromtimestamp(1e9, UTC)
    assert parse_backup_name("odd.zip") == ("odd", None)


def test_bad_date_falls_back() -> None:
    w, ts = parse_backup_name("2021-13-45-foo.zip", 0)
    assert ts == datetime.fromtimestamp(0, UTC)
    assert w == "2021-13-45-foo"


@pytest.mark.parametrize(
    "raw,out",
    [
        ("King Ghidorah", "king-ghidorah"),
        ("noah_james", "noah-james"),
        ("2021-world", "w-2021-world"),
        ("--", "world"),
        ("Fire&Ice!!", "fire-ice"),
        ("x" * 80, "x" * 40),
    ],
)
def test_sanitize(raw: str, out: str) -> None:
    s = sanitize_world_name(raw)
    assert s == out
    assert is_valid_world_name(s)


def test_unique_name() -> None:
    assert unique_name("fire-ice2", []) == "fire-ice2"
    assert unique_name("fire-ice2", ["fire-ice2"]) == "fire-ice2-2"
    assert unique_name("a", ["a", "a-2"]) == "a-3"
    long = "y" * 40
    got = unique_name(long, [long])
    assert got.endswith("-2") and len(got) <= 40


def test_invalid_names() -> None:
    for bad in ["", "Abc", "1abc", "abc-", "a_b", "a" * 41]:
        assert not is_valid_world_name(bad)
