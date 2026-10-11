from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import MagicMock

from kubernetes import client

from mcdash.config import Config
from mcdash.k8s import JobSpec, Kube, parse_cpu, parse_mem


def make_kube(cfg: Config) -> Any:
    k: Any = Kube.__new__(Kube)  # skip loading kubeconfig
    k.cfg = cfg
    k.ns = cfg.namespace
    k.client = client
    k.core = MagicMock()
    k.batch = MagicMock()
    k.custom = MagicMock()
    return k


def test_quantities() -> None:
    assert parse_cpu("250m") == 250
    assert parse_cpu("1") == 1000
    assert parse_cpu("123456789n") == 123
    assert parse_mem("2Gi") == 2 * 1024**3
    assert parse_mem("1500Ki") == 1500 * 1024
    assert parse_mem("1M") == 1_000_000
    assert parse_mem("42") == 42


def test_build_job_mounts(cfg: Config) -> None:
    k = make_kube(cfg)
    job = k.build_job(
        JobSpec(
            name="w-seed-1",
            world="w",
            kind="seed",
            args=["seed", "--src", "/archive/x.zip"],
            store_read_only=True,
        )
    )
    spec = job.spec.template.spec
    c = spec.containers[0]
    assert c.command[:3] == ["python", "-m", "mcdash.jobs"]
    mounts = {m.name: m for m in c.volume_mounts}
    assert mounts["archive"].read_only is True
    assert mounts["store"].read_only is True
    assert mounts["data"].mount_path == "/data"
    vols = {v.name: v for v in spec.volumes}
    assert vols["archive"].persistent_volume_claim.read_only is True
    assert vols["data"].persistent_volume_claim.claim_name == "w-data"
    assert spec.security_context.run_as_user == cfg.uid
    assert spec.node_selector == {"kubernetes.io/hostname": "nuc2"}
    assert job.spec.ttl_seconds_after_finished == 3600
    assert job.metadata.labels["mcdash/job"] == "seed"
    snap = k.build_job(JobSpec("w-snap-1", "w", "snapshot", [], store_read_only=False))
    smounts = {m.name: m for m in snap.spec.template.spec.containers[0].volume_mounts}
    assert smounts["store"].read_only is False
    assert smounts["archive"].read_only is True


def test_job_info_failed_condition() -> None:
    j = NS(
        metadata=NS(
            name="w-seed-1",
            labels={"mcdash/world": "w", "mcdash/job": "seed"},
            creation_timestamp=datetime.now(UTC),
        ),
        status=NS(
            active=None,
            succeeded=None,
            failed=None,
            conditions=[
                NS(type="Failed", status="True", message="BackoffLimitExceeded")
            ],
        ),
    )
    info = Kube._job_info(j)
    assert info.failed == 1 and info.done and info.message == "BackoffLimitExceeded"


def test_list_server_pods(cfg: Config) -> None:
    k = make_kube(cfg)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    pod = NS(
        metadata=NS(name="w-abc", deletion_timestamp=None),
        status=NS(
            phase="Running",
            start_time=started,
            conditions=[NS(type="Ready", status="True")],
            container_statuses=[
                NS(
                    restart_count=2,
                    state=NS(waiting=None, running=NS(started_at=started)),
                )
            ],
        ),
    )
    k.core.list_namespaced_pod.return_value = NS(items=[pod])
    (p,) = k.list_server_pods("w")
    assert p.ready and p.restarts == 2 and p.started == started
    sel = k.core.list_namespaced_pod.call_args.kwargs["label_selector"]
    assert sel == "app=w,app.kubernetes.io/name=minecraft"


def test_metrics_absent(cfg: Config) -> None:
    k = make_kube(cfg)
    k.custom.list_namespaced_custom_object.side_effect = RuntimeError("404")
    assert k.pod_metrics("w") is None
    k.custom.list_namespaced_custom_object.side_effect = None
    k.custom.list_namespaced_custom_object.return_value = {
        "items": [{"containers": [{"usage": {"cpu": "500m", "memory": "1Gi"}}]}]
    }
    assert k.pod_metrics("w") == (500, 1024**3)
