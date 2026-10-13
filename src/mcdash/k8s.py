"""Thin Kubernetes API layer returning plain dataclasses.

``KubeApi`` is the interface the service uses; ``Kube`` implements it with the
official client (in-cluster config, falling back to kubeconfig). Tests use an
in-memory fake with the same methods.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from mcdash.config import Config
from mcdash.models import Conflict, MCDashError

log = logging.getLogger(__name__)

MANAGED_BY = {"app.kubernetes.io/managed-by": "mcdash"}
WORLD_LABEL = "mcdash/world"
JOB_LABEL = "mcdash/job"
KIND_LABEL = "mcdash/kind"

# (connect, read) seconds for ordinary API calls: never hang a web request.
REQUEST_TIMEOUT = (5, 30)
# A followed log with no new line for this long is re-opened, yielding a ""
# heartbeat so the consumer can notice it has been abandoned.
LOG_IDLE_TIMEOUT = 30.0


@dataclass
class JobInfo:
    name: str
    world: str
    kind: str  # seed | snapshot
    active: int = 0
    succeeded: int = 0
    failed: int = 0
    message: str = ""
    created: datetime | None = None

    @property
    def done(self) -> bool:
        return self.succeeded > 0 or self.failed > 0


@dataclass
class PodInfo:
    name: str
    phase: str
    ready: bool
    restarts: int = 0
    waiting_reason: str = ""
    started: datetime | None = None
    deleting: bool = False
    image: str = ""  # the server container's image
    host_ip: str = ""  # status.hostIP: the node's InternalIP
    node: str = ""  # spec.nodeName


@dataclass
class JobSpec:
    """A Job against world ``world``'s volume (mounted at ``/data``).

    By default it runs ``python -m mcdash.jobs <args>`` in the dashboard image
    with the archive (read-only) and the whole store mounted. ``image`` and
    ``command`` run something else; ``store_sub_path`` mounts only that
    directory of the store, at ``store_mount``.
    """

    name: str
    world: str
    kind: str
    args: list[str]
    store_read_only: bool = True
    labels: dict[str, str] = field(default_factory=dict)
    image: str = ""
    command: list[str] | None = None
    env: dict[str, str] = field(default_factory=dict)
    data_read_only: bool = False
    mount_archive: bool = True
    store_sub_path: str = ""
    store_mount: str = ""
    backoff_limit: int = 1
    # None = no limit. A Job that cannot start (image pull, no memory on the
    # node) otherwise never finishes, so callers waiting on it never give up.
    active_deadline_seconds: int | None = None


class KubeApi(Protocol):
    def get_configmap(self, name: str) -> dict[str, str] | None: ...
    def list_configmaps(self, kind: str) -> dict[str, dict[str, str]]: ...
    def put_configmap(
        self, name: str, world: str, kind: str, data: dict[str, str]
    ) -> None: ...
    def delete_configmap(self, name: str) -> None: ...
    def create_secret(self, name: str, world: str, data: dict[str, str]) -> None: ...
    def get_secret(self, name: str) -> dict[str, str] | None: ...
    def delete_secret(self, name: str) -> None: ...
    def create_pvc(self, name: str, world: str) -> None: ...
    def pvc_exists(self, name: str) -> bool: ...
    def delete_pvc(self, name: str) -> None: ...
    def create_job(self, spec: JobSpec) -> None: ...
    def get_job(self, name: str) -> JobInfo | None: ...
    def list_jobs(self, world: str) -> list[JobInfo]: ...
    def delete_job(self, name: str) -> None: ...
    def list_server_pods(self, world: str) -> list[PodInfo]: ...
    def pod_metrics(self, world: str) -> tuple[int, int] | None: ...
    def stream_pod_log(self, pod: str, tail_lines: int = 200) -> Iterator[str]: ...


def parse_cpu(q: str) -> int:
    """CPU quantity -> millicores."""
    q = str(q)
    if q.endswith("n"):
        return int(int(q[:-1]) / 1_000_000)
    if q.endswith("u"):
        return int(int(q[:-1]) / 1000)
    if q.endswith("m"):
        return int(q[:-1])
    return int(float(q) * 1000)


_MEM_UNITS = {
    "Ki": 1024,
    "Mi": 1024**2,
    "Gi": 1024**3,
    "Ti": 1024**4,
    "k": 1000,
    "K": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
}


def parse_mem(q: str) -> int:
    q = str(q)
    for suffix in sorted(_MEM_UNITS, key=len, reverse=True):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * _MEM_UNITS[suffix])
    return int(float(q))


def server_selector(world: str) -> str:
    # labels the itzg chart puts on its pods (fullnameOverride=world)
    return f"app={world},app.kubernetes.io/name=minecraft"


class Kube:
    """Real implementation using the kubernetes client."""

    def __init__(self, cfg: Config) -> None:
        from kubernetes import client, config

        # Without a cluster the dashboard still boots: the UI loads, backups
        # are browsable, and cluster actions report this error instead of the
        # pod crash-looping.
        self.unavailable = ""
        try:
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config()
            except (config.ConfigException, OSError) as e:
                self.unavailable = f"no Kubernetes configuration ({e})"
                log.error("Kubernetes unavailable: %s", e)
        self.cfg = cfg
        self.ns = cfg.namespace
        self.client = client
        self.core = client.CoreV1Api()
        self.batch = client.BatchV1Api()
        self.custom = client.CustomObjectsApi()

    # ------------------------------------------------------------ helpers
    def _meta(self, name: str, world: str, extra: dict[str, str] | None = None):
        labels = {**MANAGED_BY, WORLD_LABEL: world, **(extra or {})}
        return self.client.V1ObjectMeta(name=name, namespace=self.ns, labels=labels)

    def _call(self, fn, *args, **kw) -> Any:
        """Call the API with a timeout; network failures become MCDashError.

        ``ApiException`` is passed through for the callers below to map.
        """
        from kubernetes.client.exceptions import ApiException

        if getattr(self, "unavailable", ""):
            raise MCDashError(f"Kubernetes unavailable: {self.unavailable}")
        kw.setdefault("_request_timeout", REQUEST_TIMEOUT)
        try:
            return fn(*args, **kw)
        except ApiException:
            raise
        except Exception as e:  # urllib3 / socket errors
            raise MCDashError(f"cannot reach the Kubernetes API: {e}") from e

    def _api(self, fn, *args, **kw) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            return self._call(fn, *args, **kw)
        except ApiException as e:
            raise MCDashError(f"kubernetes: {e.reason}") from e

    def _ignore_404(self, fn, *args, **kw) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            return self._call(fn, *args, **kw)
        except ApiException as e:
            if e.status == 404:
                return None
            raise MCDashError(f"kubernetes: {e.reason}") from e

    def _create(self, fn, *args, **kw) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            return self._call(fn, *args, **kw)
        except ApiException as e:
            if e.status == 409:
                raise Conflict(f"already exists: {e.reason}") from e
            raise MCDashError(f"kubernetes: {e.reason} {e.body}") from e

    # ------------------------------------------------------------ configmaps
    def get_configmap(self, name: str) -> dict[str, str] | None:
        cm = self._ignore_404(self.core.read_namespaced_config_map, name, self.ns)
        return dict(cm.data or {}) if cm is not None else None

    def list_configmaps(self, kind: str) -> dict[str, dict[str, str]]:
        sel = f"app.kubernetes.io/managed-by=mcdash,{KIND_LABEL}={kind}"
        res = self._api(
            self.core.list_namespaced_config_map, self.ns, label_selector=sel
        )
        items: list[Any] = list(res.items)
        return {str(i.metadata.name): dict(i.data or {}) for i in items}

    def put_configmap(
        self, name: str, world: str, kind: str, data: dict[str, str]
    ) -> None:
        body = self.client.V1ConfigMap(
            metadata=self._meta(name, world, {KIND_LABEL: kind}), data=data
        )
        existing = self._ignore_404(self.core.read_namespaced_config_map, name, self.ns)
        if existing is None:
            self._create(self.core.create_namespaced_config_map, self.ns, body)
        else:
            self._api(self.core.replace_namespaced_config_map, name, self.ns, body)

    def delete_configmap(self, name: str) -> None:
        self._ignore_404(self.core.delete_namespaced_config_map, name, self.ns)

    # ------------------------------------------------------------ secrets
    def create_secret(self, name: str, world: str, data: dict[str, str]) -> None:
        body = self.client.V1Secret(
            metadata=self._meta(name, world), string_data=data, type="Opaque"
        )
        self._create(self.core.create_namespaced_secret, self.ns, body)

    def get_secret(self, name: str) -> dict[str, str] | None:
        import base64

        s = self._ignore_404(self.core.read_namespaced_secret, name, self.ns)
        if s is None:
            return None
        return {k: base64.b64decode(v).decode() for k, v in (s.data or {}).items()}

    def delete_secret(self, name: str) -> None:
        self._ignore_404(self.core.delete_namespaced_secret, name, self.ns)

    # ------------------------------------------------------------ PVCs
    def create_pvc(self, name: str, world: str) -> None:
        c = self.client
        body = c.V1PersistentVolumeClaim(
            metadata=self._meta(name, world),
            spec=c.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteMany"],
                storage_class_name=self.cfg.storage_class,
                resources=c.V1VolumeResourceRequirements(
                    requests={"storage": self.cfg.world_size}
                ),
            ),
        )
        self._create(self.core.create_namespaced_persistent_volume_claim, self.ns, body)

    def pvc_exists(self, name: str) -> bool:
        r = self._ignore_404(
            self.core.read_namespaced_persistent_volume_claim, name, self.ns
        )
        return r is not None

    def delete_pvc(self, name: str) -> None:
        self._ignore_404(
            self.core.delete_namespaced_persistent_volume_claim, name, self.ns
        )

    # ------------------------------------------------------------ jobs
    def build_job(self, spec: JobSpec) -> Any:
        c = self.client
        cfg = self.cfg
        mounts = [
            c.V1VolumeMount(
                name="data", mount_path="/data", read_only=spec.data_read_only or None
            )
        ]
        volumes = [
            c.V1Volume(
                name="data",
                persistent_volume_claim=c.V1PersistentVolumeClaimVolumeSource(
                    claim_name=f"{spec.world}-data"
                ),
            )
        ]
        if cfg.archive_pvc and spec.mount_archive:
            mounts.append(
                c.V1VolumeMount(
                    name="archive", mount_path=cfg.job_archive_mount, read_only=True
                )
            )
            volumes.append(
                c.V1Volume(
                    name="archive",
                    persistent_volume_claim=c.V1PersistentVolumeClaimVolumeSource(
                        claim_name=cfg.archive_pvc, read_only=True
                    ),
                )
            )
        if cfg.store_pvc:
            mounts.append(
                c.V1VolumeMount(
                    name="store",
                    mount_path=spec.store_mount or cfg.job_store_mount,
                    read_only=spec.store_read_only,
                    sub_path=spec.store_sub_path or None,
                )
            )
            volumes.append(
                c.V1Volume(
                    name="store",
                    persistent_volume_claim=c.V1PersistentVolumeClaimVolumeSource(
                        claim_name=cfg.store_pvc
                    ),
                )
            )
        labels = {
            **MANAGED_BY,
            WORLD_LABEL: spec.world,
            JOB_LABEL: spec.kind,
            **spec.labels,
        }
        container = c.V1Container(
            name=spec.kind,
            image=spec.image or cfg.job_image,
            command=spec.command or ["python", "-m", "mcdash.jobs", *spec.args],
            env=[
                c.V1EnvVar(name="MCD_UID", value=str(cfg.uid)),
                c.V1EnvVar(name="MCD_GID", value=str(cfg.gid)),
                c.V1EnvVar(name="MCD_ARCHIVE_DIR", value=cfg.job_archive_mount),
                c.V1EnvVar(name="PYTHONUNBUFFERED", value="1"),
                *(c.V1EnvVar(name=k, value=v) for k, v in spec.env.items()),
            ],
            volume_mounts=mounts,
            security_context=c.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=c.V1Capabilities(drop=["ALL"]),
            ),
            resources=c.V1ResourceRequirements(
                requests={"memory": "256Mi", "cpu": "250m"},
                limits={"memory": "1Gi"},
            ),
        )
        pod_spec = c.V1PodSpec(
            restart_policy="Never",
            containers=[container],
            volumes=volumes,
            security_context=c.V1PodSecurityContext(
                run_as_user=cfg.uid,
                run_as_group=cfg.gid,
                fs_group=cfg.gid,
                run_as_non_root=True,
            ),
            node_selector=(
                {"kubernetes.io/hostname": cfg.world_node} if cfg.world_node else None
            ),
        )
        return c.V1Job(
            metadata=c.V1ObjectMeta(name=spec.name, namespace=self.ns, labels=labels),
            spec=c.V1JobSpec(
                backoff_limit=spec.backoff_limit,
                active_deadline_seconds=spec.active_deadline_seconds,
                ttl_seconds_after_finished=3600,
                template=c.V1PodTemplateSpec(
                    metadata=c.V1ObjectMeta(labels=labels), spec=pod_spec
                ),
            ),
        )

    def create_job(self, spec: JobSpec) -> None:
        self._create(self.batch.create_namespaced_job, self.ns, self.build_job(spec))

    @staticmethod
    def _job_info(j: Any) -> JobInfo:
        labels = j.metadata.labels or {}
        st = j.status
        msg = ""
        for cond in st.conditions or []:
            if cond.type == "Failed" and cond.status == "True":
                msg = cond.message or cond.reason or "failed"
        return JobInfo(
            name=j.metadata.name,
            world=labels.get(WORLD_LABEL, ""),
            kind=labels.get(JOB_LABEL, ""),
            active=st.active or 0,
            succeeded=st.succeeded or 0,
            failed=max(1, st.failed or 0) if msg else (st.failed or 0),
            message=msg,
            created=j.metadata.creation_timestamp,
        )

    def get_job(self, name: str) -> JobInfo | None:
        j = self._ignore_404(self.batch.read_namespaced_job, name, self.ns)
        return self._job_info(j) if j is not None else None

    def list_jobs(self, world: str) -> list[JobInfo]:
        res = self._api(
            self.batch.list_namespaced_job,
            self.ns,
            label_selector=f"{WORLD_LABEL}={world}",
        )
        return [self._job_info(j) for j in res.items]

    def delete_job(self, name: str) -> None:
        self._ignore_404(
            self.batch.delete_namespaced_job,
            name,
            self.ns,
            propagation_policy="Background",
        )

    # ------------------------------------------------------------ pods
    def list_server_pods(self, world: str) -> list[PodInfo]:
        res = self._api(
            self.core.list_namespaced_pod,
            self.ns,
            label_selector=server_selector(world),
        )
        out = []
        items: list[Any] = list(res.items)
        for p in items:
            st = p.status
            ready = any(
                c.type == "Ready" and c.status == "True" for c in st.conditions or []
            )
            restarts = 0
            reason = ""
            started = st.start_time
            containers = list(p.spec.containers or []) if p.spec else []
            image = next(
                (c.image for c in containers if c.name == world),
                containers[0].image if containers else "",
            )
            for cs in st.container_statuses or []:
                restarts += cs.restart_count or 0
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    reason = cs.state.waiting.reason
                if cs.state and cs.state.running and cs.state.running.started_at:
                    started = cs.state.running.started_at
            out.append(
                PodInfo(
                    name=p.metadata.name,
                    phase=st.phase or "",
                    ready=ready,
                    restarts=restarts,
                    waiting_reason=reason,
                    started=started,
                    deleting=p.metadata.deletion_timestamp is not None,
                    image=image or "",
                    host_ip=st.host_ip or "",
                    node=(p.spec.node_name if p.spec else None) or "",
                )
            )
        return out

    def pod_metrics(self, world: str) -> tuple[int, int] | None:
        try:
            res = self._call(
                self.custom.list_namespaced_custom_object,
                "metrics.k8s.io",
                "v1beta1",
                self.ns,
                "pods",
                label_selector=server_selector(world),
            )
        except Exception:  # metrics-server absent or forbidden
            return None
        cpu = mem = 0
        items = res.get("items", []) if isinstance(res, dict) else []
        if not items:
            return None
        for item in items:
            for c in item.get("containers", []):
                usage = c.get("usage", {})
                cpu += parse_cpu(usage.get("cpu", "0"))
                mem += parse_mem(usage.get("memory", "0"))
        return cpu, mem

    def stream_pod_log(self, pod: str, tail_lines: int = 200) -> Iterator[str]:
        """Follow ``pod``'s log until the container exits.

        Never blocks longer than ``LOG_IDLE_TIMEOUT`` without yielding: on an
        idle read timeout it yields ``""`` (a heartbeat, not a log line) and
        re-opens the stream from just before the timeout. A consumer that has
        lost interest stops iterating at the next heartbeat, so no thread or
        connection outlives its viewer by more than that.
        """
        from kubernetes.client.exceptions import ApiException
        from urllib3.exceptions import ProtocolError, ReadTimeoutError

        kw: dict[str, Any] = {"tail_lines": tail_lines}
        while True:
            try:
                resp: Any = self._call(
                    self.core.read_namespaced_pod_log,
                    pod,
                    self.ns,
                    follow=True,
                    _preload_content=False,
                    _request_timeout=(5, LOG_IDLE_TIMEOUT),
                    **kw,
                )
            except ApiException as e:  # pod gone or not started
                yield f"[log stream ended: {e.reason}]"
                return
            buf = b""
            idle = False
            try:
                for chunk in resp.stream(4096):
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line:
                            yield line.decode("utf-8", "replace")
                if buf:
                    yield buf.decode("utf-8", "replace")
            except (TimeoutError, ReadTimeoutError, ProtocolError):
                idle = True
            finally:
                with contextlib.suppress(Exception):
                    resp.release_conn()
            if not idle:
                return  # container exited: the server closed the stream
            if buf:
                yield buf.decode("utf-8", "replace")
            yield ""  # heartbeat
            # Nothing was logged for LOG_IDLE_TIMEOUT, so a short look-back
            # catches lines racing the reconnect without duplicating any.
            kw = {"since_seconds": 2}
