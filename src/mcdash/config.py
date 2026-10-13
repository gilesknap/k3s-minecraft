"""Runtime configuration from ``MCD_*`` environment variables (see contracts.md)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _read_namespace() -> str:
    sa = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    try:
        return sa.read_text().strip() or "minecraft"
    except OSError:
        return "minecraft"


@dataclass(frozen=True)
class Config:
    namespace: str = "minecraft"
    archive_dir: Path = Path("/archive")
    store_dir: Path = Path("/store")
    archive_pvc: str = ""
    store_pvc: str = ""
    job_image: str = ""
    port_min: int = 30565
    port_max: int = 30584
    # RCON NodePorts, one per world. Must not overlap the game range: the VPS
    # relay forwards the game range to the internet; RCON must never be relayed.
    rcon_port_min: int = 30600
    rcon_port_max: int = 30619
    # LAN address shown for RCON; "" = the world pod's node (status.hostIP).
    rcon_host: str = ""
    public_host: str = "localhost"
    world_node: str = ""
    storage_class: str = "nfs-rwx"
    world_size: str = "10Gi"
    default_memory: str = "4Gi"
    itzg_chart: Path = Path("/opt/charts/minecraft")
    snapshot_keep: int = 10
    uid: int = 1000
    gid: int = 1000

    # Where Jobs see the tiers (fixed mount points inside Job pods).
    job_archive_mount: str = "/archive"
    job_store_mount: str = "/store"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        e = os.environ if env is None else env

        def s(key: str, default: str) -> str:
            v = e.get(key)
            return default if v is None or v == "" else v

        ns = e.get("MCD_NAMESPACE") or _read_namespace()
        return cls(
            namespace=ns,
            archive_dir=Path(s("MCD_ARCHIVE_DIR", "/archive")),
            store_dir=Path(s("MCD_STORE_DIR", "/store")),
            archive_pvc=s("MCD_ARCHIVE_PVC", ""),
            store_pvc=s("MCD_STORE_PVC", ""),
            job_image=s("MCD_JOB_IMAGE", ""),
            port_min=int(s("MCD_PORT_MIN", "30565")),
            port_max=int(s("MCD_PORT_MAX", "30584")),
            rcon_port_min=int(s("MCD_RCON_PORT_MIN", "30600")),
            rcon_port_max=int(s("MCD_RCON_PORT_MAX", "30619")),
            rcon_host=s("MCD_RCON_HOST", ""),
            public_host=s("MCD_PUBLIC_HOST", "localhost"),
            world_node=s("MCD_WORLD_NODE", ""),
            storage_class=s("MCD_STORAGE_CLASS", "nfs-rwx"),
            world_size=s("MCD_WORLD_SIZE", "10Gi"),
            default_memory=s("MCD_DEFAULT_MEMORY", "4Gi"),
            itzg_chart=Path(s("MCD_ITZG_CHART", "/opt/charts/minecraft")),
            snapshot_keep=int(s("MCD_SNAPSHOT_KEEP", "10")),
            uid=int(s("MCD_UID", "1000")),
            gid=int(s("MCD_GID", "1000")),
        )

    def __post_init__(self) -> None:
        if self.rcon_port_min > self.rcon_port_max:
            raise ValueError("RCON port range is empty (min > max)")
        if self.rcon_port_min <= self.port_max and self.port_min <= self.rcon_port_max:
            raise ValueError(
                f"RCON ports {self.rcon_port_min}-{self.rcon_port_max} overlap the "
                f"game ports {self.port_min}-{self.port_max}"
            )

    @property
    def backups_dir(self) -> Path:
        return self.store_dir / "backups"

    @property
    def snapshots_dir(self) -> Path:
        return self.store_dir / "snapshots"

    @property
    def dashboard_dir(self) -> Path:
        return self.store_dir / "dashboard"
