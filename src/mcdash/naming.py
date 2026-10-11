"""Backup filename parsing and DNS-safe world names.

Archive filenames are never changed, so parsing is lenient. Formats seen::

    2021-05-15-21_56_15-transformers-best.zip
    2021-05-15-21_56_20-king-ghidorah(1).zip
    2022-02-04+21.14.00-transformers-again(1).zip
    2022-08-15_22_56_49-noah-james.zip
    22-01-21.21.50.00-noah-james.zip
    2021-05-15-21:58:22-devils-deep.zip
    2021-05-09-world-of-animals-old1.zip
    noah_single_player_best_2021_05/mansion-world.zip
    snapshot dir:  <world>/20261011T120000Z.zip
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import PurePosixPath

MAX_WORLD_NAME = 40  # leaves room for "-seed-YYYYmmddtHHMMSS" in 63-char names

_DUP_SUFFIX = re.compile(r"(?:\s*\(\d+\))+$")
_DATED = re.compile(
    r"""^(?P<y>\d{4}|\d{2})-(?P<mo>\d{2})-(?P<d>\d{2})
        (?:[-_+.T\s](?P<h>\d{2})[-_.:](?P<mi>\d{2})[-_.:](?P<s>\d{2}))?
        (?:[-_+.\s]+(?P<name>.+))?$""",
    re.VERBOSE,
)
_COMPACT = re.compile(r"^(?P<date>\d{8})T?(?P<time>\d{6})Z?$", re.IGNORECASE)
_DNS_BAD = re.compile(r"[^a-z0-9-]+")
_DNS_OK = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


def strip_dup_suffix(stem: str) -> str:
    """``king-ghidorah(1)`` -> ``king-ghidorah``."""
    return _DUP_SUFFIX.sub("", stem).strip()


def _mk(y: str, mo: str, d: str, h: str | None, mi: str | None, s: str | None):
    year = int(y)
    if len(y) == 2:
        year += 2000
    try:
        return datetime(
            year, int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0), tzinfo=UTC
        )
    except ValueError:
        return None


def parse_compact_timestamp(stem: str) -> datetime | None:
    """``20261011T120000Z`` -> datetime (UTC)."""
    m = _COMPACT.match(stem)
    if not m:
        return None
    try:
        return datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None


def format_compact_timestamp(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_backup_name(
    relpath: str, mtime: float | None = None
) -> tuple[str, datetime | None]:
    """Parse ``(world_name, timestamp)`` from a path relative to a tier root.

    ``timestamp`` falls back to ``mtime`` (UTC) when the name carries no date,
    and is ``None`` only if neither is available. ``world_name`` is the raw
    world part (not DNS-sanitised) with ``(1)`` suffixes removed.
    """
    p = PurePosixPath(relpath)
    stem = p.name
    if stem.lower().endswith(".zip"):
        stem = stem[:-4]
    stem = strip_dup_suffix(stem)
    fallback = datetime.fromtimestamp(mtime, tz=UTC) if mtime is not None else None

    # snapshot layout: <world>/<compact timestamp>.zip
    ts = parse_compact_timestamp(stem)
    if ts is not None:
        parent = p.parent.name
        return (strip_dup_suffix(parent) or stem, ts)

    m = _DATED.match(stem)
    if m and m["name"]:
        ts = _mk(m["y"], m["mo"], m["d"], m["h"], m["mi"], m["s"])
        if ts is not None:
            return (strip_dup_suffix(m["name"]), ts)
    return (stem, fallback)


def sanitize_world_name(raw: str) -> str:
    """Make a DNS-1035 label usable as helm release/Service/PVC prefix."""
    s = _DNS_BAD.sub("-", raw.strip().lower().replace("_", "-"))
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if not s:
        s = "world"
    if not s[0].isalpha():
        s = "w-" + s
    s = s[:MAX_WORLD_NAME].rstrip("-")
    return s


def is_valid_world_name(name: str) -> bool:
    return len(name) <= MAX_WORLD_NAME and bool(_DNS_OK.match(name))


def unique_name(base: str, taken: Iterable[str]) -> str:
    """``base`` if free, else ``base-2``, ``base-3``, ..."""
    taken_set = set(taken)
    base = sanitize_world_name(base)
    if base not in taken_set:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        cand = base[: MAX_WORLD_NAME - len(suffix)].rstrip("-") + suffix
        if cand not in taken_set:
            return cand
        n += 1
