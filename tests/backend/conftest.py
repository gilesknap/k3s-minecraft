from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mcdash.config import Config
from mcdash.service import KubeDashboardService

from .helpers import FakeHelm, FakeKube, FakeRcon, make_world_zip


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    archive = tmp_path / "archive"
    store = tmp_path / "store"
    archive.mkdir()
    (store / "backups").mkdir(parents=True)
    (store / "snapshots").mkdir()
    (store / "dashboard").mkdir()
    return Config(
        namespace="minecraft",
        archive_dir=archive,
        store_dir=store,
        archive_pvc="mc-archive",
        store_pvc="mc-store",
        job_image="ghcr.io/gilesknap/k3s-minecraft:test",
        port_min=30565,
        port_max=30567,
        public_host="mc.example.org",
        world_node="nuc2",
        snapshot_keep=3,
    )


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


@pytest.fixture
def archive_zips(cfg: Config) -> dict[str, Path]:
    a = cfg.archive_dir
    return {
        "king": make_world_zip(a / "2021-05-15-21_56_20-king-ghidorah(1).zip"),
        "fire": make_world_zip(
            a / "2021-06-03-10_30_33-fire-ice2.zip", forge=True, prefix="tmp/tmp.KS/"
        ),
    }


@pytest.fixture
def svc(cfg: Config, archive_zips: dict[str, Path]):
    kube = FakeKube()
    helm = FakeHelm(kube)
    rcon = FakeRcon()
    service = KubeDashboardService(
        cfg, kube, helm, rcon_factory=rcon.factory, clock=Clock(), cache_ttl=0
    )
    service.fake_kube = kube  # type: ignore[attr-defined]
    service.fake_helm = helm  # type: ignore[attr-defined]
    service.fake_rcon = rcon  # type: ignore[attr-defined]
    return service
