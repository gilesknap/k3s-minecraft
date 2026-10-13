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


def test_build_commands_job(cfg: Config) -> None:
    """The console's commands Job: another image, read-only world, one store dir."""
    k = make_kube(cfg)
    job = k.build_job(
        JobSpec(
            "w-cmds-1",
            "w",
            "commands",
            [],
            image="itzg/minecraft-server:latest",
            command=["sh", "-c", "true"],
            env={"MCD_WORLD": "w"},
            data_read_only=True,
            mount_archive=False,
            store_read_only=False,
            store_sub_path="dashboard/commands",
            store_mount="/out",
            backoff_limit=0,
            active_deadline_seconds=600,
        )
    )
    assert job.spec.active_deadline_seconds == 600
    spec = job.spec.template.spec
    c = spec.containers[0]
    assert c.image == "itzg/minecraft-server:latest"
    assert c.command == ["sh", "-c", "true"]
    assert {e.name: e.value for e in c.env}["MCD_WORLD"] == "w"
    mounts = {m.name: m for m in c.volume_mounts}
    assert set(mounts) == {"data", "store"}
    assert mounts["data"].read_only is True
    assert (mounts["store"].mount_path, mounts["store"].sub_path) == (
        "/out",
        "dashboard/commands",
    )
    assert mounts["store"].read_only is False
    assert {v.name for v in spec.volumes} == {"data", "store"}
    assert spec.security_context.run_as_non_root is True
    assert job.spec.backoff_limit == 0


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
        spec=NS(
            containers=[
                NS(name="sidecar", image="busybox"),
                NS(name="w", image="itzg/minecraft-server:java21"),
            ]
        ),
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
    assert p.image == "itzg/minecraft-server:java21"
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


class _Resp:
    def __init__(self, chunks: list[bytes], then: Exception | None = None) -> None:
        self.chunks, self.then, self.released = chunks, then, False

    def stream(self, n: int):
        yield from self.chunks
        if self.then:
            raise self.then

    def release_conn(self) -> None:
        self.released = True


def test_stream_pod_log_heartbeat_and_reconnect(cfg: Config) -> None:
    from urllib3.exceptions import ReadTimeoutError

    k = make_kube(cfg)
    r1 = _Resp([b"a\nb", b"c\n"], ReadTimeoutError(None, "/log", "idle"))  # type: ignore[arg-type]
    r2 = _Resp([b"d\n"])
    k.core.read_namespaced_pod_log.side_effect = [r1, r2]
    assert list(k.stream_pod_log("p")) == ["a", "bc", "", "d"]
    first, second = k.core.read_namespaced_pod_log.call_args_list
    assert first.kwargs["tail_lines"] == 200
    assert first.kwargs["_request_timeout"][1] > 0  # bounded idle read
    assert second.kwargs["since_seconds"] == 2 and "tail_lines" not in second.kwargs
    assert r1.released and r2.released


def test_stream_pod_log_consumer_can_stop(cfg: Config) -> None:
    """A heartbeat hands control back, so an abandoned viewer is noticed."""
    from urllib3.exceptions import ReadTimeoutError

    k = make_kube(cfg)
    k.core.read_namespaced_pod_log.side_effect = lambda *a, **kw: _Resp(
        [],
        ReadTimeoutError(None, "/log", "idle"),  # type: ignore[arg-type]
    )
    it = k.stream_pod_log("p")
    assert next(it) == ""
    it.close()  # what the web pump does once its viewer has gone


def test_unavailable_cluster_raises_mcdash_error(cfg: Config) -> None:
    import pytest

    from mcdash.models import MCDashError

    k = make_kube(cfg)
    k.unavailable = "no Kubernetes configuration"
    with pytest.raises(MCDashError, match="Kubernetes unavailable"):
        k.list_configmaps("world")
    k.unavailable = ""
    k.core.list_namespaced_config_map.side_effect = OSError("connection refused")
    with pytest.raises(MCDashError, match="cannot reach"):
        k.list_configmaps("world")
