"""Write a tiny synthetic vanilla world backup zip for the e2e test.

Usage::

    python tests/e2e/make_world_zip.py <out-dir> [--world e2e-world]

Produces ``<out-dir>/2024-01-02_03_04_05-<world>.zip`` laid out like the real
archive (a full ``/data`` under a ``tmp/tmp.XXXX/`` prefix) with a gzip NBT
``world/level.dat`` (1.20.1, default world-gen), ``server.properties``,
``ops.json`` and ``whitelist.json``. Minecraft generates terrain on first start.
"""

from __future__ import annotations

import argparse
import tempfile
import zipfile
from pathlib import Path

from nbtlib import Byte, Compound, File, Int, List, Long, String

PREFIX = "tmp/tmp.e2etest/"
DATA_VERSION = 3465  # 1.20.1
FILENAME_STAMP = "2024-01-02_03_04_05"


def _dimension(kind: str, settings: str, biome_source: Compound) -> Compound:
    return Compound(
        {
            "type": String(f"minecraft:{kind}"),
            "generator": Compound(
                {
                    "type": String("minecraft:noise"),
                    "settings": String(f"minecraft:{settings}"),
                    "biome_source": biome_source,
                }
            ),
        }
    )


def multi_noise(preset: str) -> Compound:
    return Compound(
        {
            "type": String("minecraft:multi_noise"),
            "preset": String(f"minecraft:{preset}"),
        }
    )


def level_dat(world: str, seed: int = 20240102) -> File:
    data = Compound(
        {
            "DataVersion": Int(DATA_VERSION),
            "version": Int(19133),  # anvil
            "Version": Compound(
                {
                    "Id": Int(DATA_VERSION),
                    "Name": String("1.20.1"),
                    "Series": String("main"),
                    "Snapshot": Byte(0),
                }
            ),
            "LevelName": String(world),
            "GameType": Int(0),
            "Difficulty": Byte(1),
            "hardcore": Byte(0),
            "allowCommands": Byte(0),
            "initialized": Byte(0),
            "SpawnX": Int(0),
            "SpawnY": Int(64),
            "SpawnZ": Int(0),
            "Time": Long(0),
            "DayTime": Long(0),
            "DataPacks": Compound(
                {
                    "Enabled": List[String]([String("vanilla")]),
                    "Disabled": List[String]([]),
                }
            ),
            "WorldGenSettings": Compound(
                {
                    "seed": Long(seed),
                    "generate_features": Byte(1),
                    "bonus_chest": Byte(0),
                    "dimensions": Compound(
                        {
                            "minecraft:overworld": _dimension(
                                "overworld", "overworld", multi_noise("overworld")
                            ),
                            "minecraft:the_nether": _dimension(
                                "the_nether", "nether", multi_noise("nether")
                            ),
                            "minecraft:the_end": _dimension(
                                "the_end",
                                "end",
                                Compound({"type": String("minecraft:the_end")}),
                            ),
                        }
                    ),
                }
            ),
        }
    )
    return File({"Data": data}, gzipped=True)


SERVER_PROPERTIES = """\
#Minecraft server properties
gamemode=creative
difficulty=peaceful
pvp=false
enable-command-block=true
motd=mcdash e2e world
level-name=world
online-mode=true
"""


def make_world_zip(out_dir: Path, world: str = "e2e-world") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{FILENAME_STAMP}-{world}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        level = Path(tmp) / "level.dat"
        level_dat(world).save(level)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(PREFIX, "")
            z.writestr(PREFIX + "server.properties", SERVER_PROPERTIES)
            z.writestr(PREFIX + "ops.json", "[]")
            z.writestr(PREFIX + "whitelist.json", "[]")
            z.writestr(PREFIX + "banned-players.json", "[]")
            z.writestr(PREFIX + "world/", "")
            z.write(level, PREFIX + "world/level.dat")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--world", default="e2e-world")
    args = parser.parse_args()
    print(make_world_zip(args.out_dir, args.world))


if __name__ == "__main__":
    main()
