"""End-to-end test against a real cluster (kind in CI).

Drives the real web routes (GET /healthz, /, /backups, /launch,
/worlds/{name}/status; POST /launch, /worlds/{name}/start|stop|snapshot|delete).
World state is scraped from the ``badge state-<state>`` class that
``_macros.html`` renders in the status partial.

Prerequisites (see .github/workflows/ci.yml, job ``e2e``):

- the dashboard chart installed with hostPath storage, the archive dir
  holding the zip from ``make_world_zip.py``;
- kubectl on PATH pointing at the cluster.

Run with ``pytest -m e2e tests/e2e``. Configuration via env vars below.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from urllib.parse import quote

import httpx
import pytest

pytestmark = pytest.mark.e2e

NAMESPACE = os.environ.get("E2E_NAMESPACE", "mcdash")
SERVICE = os.environ.get("E2E_SERVICE", "mcdash")
DEPLOYMENT = os.environ.get("E2E_DEPLOYMENT", "mcdash")
BACKUP_FILE = os.environ.get("E2E_BACKUP_FILE", "2024-01-02_03_04_05-e2e-world.zip")
WORLD = os.environ.get("E2E_WORLD", "e2e")
PORT_MIN = int(os.environ.get("E2E_PORT_MIN", "30565"))
PORT_MAX = int(os.environ.get("E2E_PORT_MAX", "30584"))
MEMORY = os.environ.get("E2E_MEMORY", "1Gi")
RUN_TIMEOUT = int(os.environ.get("E2E_RUN_TIMEOUT", "900"))
STATE_RE = re.compile(r"badge state-([a-z]+)")


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def client() -> Iterator[httpx.Client]:
    port = free_port()
    pf = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward", f"svc/{SERVICE}", f"{port}:80"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                if httpx.get(f"{base}/healthz", timeout=5).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            if pf.poll() is not None or time.monotonic() > deadline:
                err = pf.stderr.read() if pf.stderr else ""
                pytest.fail(f"port-forward to svc/{SERVICE} never became ready: {err}")
            time.sleep(1)
        with httpx.Client(
            base_url=base,
            timeout=60,
            headers={"X-Auth-Request-Email": "e2e@example.com"},
        ) as c:
            yield c
    finally:
        pf.terminate()
        pf.wait(timeout=10)


def world_state(client: httpx.Client, name: str) -> str | None:
    """Current state string, or None if the world does not exist (404)."""
    r = client.get(f"/worlds/{name}/status")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    m = STATE_RE.search(r.text)
    assert m, f"no state badge in status partial:\n{r.text[:2000]}"
    return m.group(1)


def wait_state(
    client: httpx.Client, name: str, want: str | None, timeout: int = RUN_TIMEOUT
) -> None:
    deadline = time.monotonic() + timeout
    state: str | None = "?"
    while time.monotonic() < deadline:
        state = world_state(client, name)
        if state == want:
            return
        if state == "failed" and want != "failed":
            diag = kubectl("get", "pods,jobs,pvc,svc", "-o", "wide", check=False)
            pytest.fail(f"world {name} FAILED while waiting for {want}\n{diag.stdout}")
        time.sleep(5)
    diag = kubectl("get", "pods,jobs,pvc,svc", "-o", "wide", check=False)
    pytest.fail(f"world {name} stuck in {state!r}, wanted {want!r}\n{diag.stdout}")


def post_ok(client: httpx.Client, url: str, data: dict[str, str] | None = None) -> None:
    r = client.post(url, data=data or {}, follow_redirects=False)
    assert r.status_code < 400, f"POST {url} -> {r.status_code}\n{r.text[:2000]}"


def snapshot_files(world: str) -> list[str]:
    r = kubectl(
        "exec",
        f"deploy/{DEPLOYMENT}",
        "--",
        "ls",
        "-1",
        f"/store/snapshots/{world}",
        check=False,
    )
    return [f for f in r.stdout.split() if f.endswith(".zip")]


def test_healthz(client: httpx.Client) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


def test_archive_mount_rejects_writes() -> None:
    # The archive is visible...
    ls = kubectl("exec", f"deploy/{DEPLOYMENT}", "--", "ls", "/archive")
    assert BACKUP_FILE in ls.stdout
    # ...but cannot be written, deleted or renamed.
    for cmd in (
        ["touch", "/archive/should-not-exist"],
        ["rm", "-f", f"/archive/{BACKUP_FILE}"],
        ["mv", f"/archive/{BACKUP_FILE}", "/archive/renamed.zip"],
    ):
        r = kubectl("exec", f"deploy/{DEPLOYMENT}", "--", *cmd, check=False)
        assert r.returncode != 0, f"{cmd} unexpectedly succeeded on /archive"
        assert "Read-only file system" in r.stderr, r.stderr
    ls = kubectl("exec", f"deploy/{DEPLOYMENT}", "--", "ls", "/archive")
    assert BACKUP_FILE in ls.stdout
    assert "should-not-exist" not in ls.stdout


def test_world_lifecycle(client: httpx.Client) -> None:
    backup_id = f"archive/{BACKUP_FILE}"

    # The synthetic backup is listed and launchable.
    backups = client.get("/backups")
    backups.raise_for_status()
    assert BACKUP_FILE in backups.text
    assert client.get(f"/launch?backup={quote(backup_id)}").status_code == 200

    # Launch -> seeding -> starting -> running.
    post_ok(
        client,
        "/launch",
        {
            "backup_id": backup_id,
            "name": WORLD,
            "server_type": "VANILLA",
            "version": "LATEST",
            "memory": MEMORY,
            "gamemode": "creative",
            "difficulty": "peaceful",
            "max_world_size": "1000",
            "motd": "mcdash e2e",
        },
    )
    wait_state(client, WORLD, "running")

    # The seed Job ran as designed: archive mounted read-only (volume and
    # mount), unprivileged, cleaned up by TTL.
    jobs = json.loads(
        kubectl(
            "get", "jobs", "-l", f"mcdash/world={WORLD},mcdash/job=seed", "-o", "json"
        ).stdout
    )["items"]
    assert jobs, "no seed Job found"
    job = jobs[0]
    assert job["status"].get("succeeded") == 1, job["status"]
    assert job["spec"]["ttlSecondsAfterFinished"] == 3600
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsUser"] == 1000
    assert pod["securityContext"]["runAsNonRoot"] is True
    vols = {v["name"]: v for v in pod["volumes"]}
    assert vols["archive"]["persistentVolumeClaim"]["readOnly"] is True
    mounts = {m["name"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["archive"]["readOnly"] is True
    assert mounts["store"]["readOnly"] is True  # seeding never writes the store

    # The game service is a NodePort in the configured range, matching the
    # connect address the dashboard shows.
    svcs = json.loads(kubectl("get", "svc", "-o", "json").stdout)["items"]
    node_ports = [
        p["nodePort"]
        for s in svcs
        if s["spec"]["type"] == "NodePort"
        and s["metadata"].get("labels", {}).get("app.kubernetes.io/instance") == WORLD
        for p in s["spec"]["ports"]
    ]
    assert node_ports, f"no NodePort service for release {WORLD}"
    assert all(PORT_MIN <= p <= PORT_MAX for p in node_ports), node_ports
    status = client.get(f"/worlds/{WORLD}/status").text
    assert any(f":{p}" in status for p in node_ports), "connect address port mismatch"

    # Snapshot now -> a zip appears under /store/snapshots/<world>/.
    before = set(snapshot_files(WORLD))
    post_ok(client, f"/worlds/{WORLD}/snapshot")
    deadline = time.monotonic() + 300
    while not (set(snapshot_files(WORLD)) - before):
        assert time.monotonic() < deadline, "snapshot zip never appeared"
        time.sleep(5)
    wait_state(client, WORLD, "running", timeout=300)

    # Stop (snapshots first) -> stopped; start -> running again.
    post_ok(client, f"/worlds/{WORLD}/stop")
    wait_state(client, WORLD, "stopped", timeout=600)
    post_ok(client, f"/worlds/{WORLD}/start")
    wait_state(client, WORLD, "running")

    # Delete needs the typed name; a wrong confirmation is refused.
    r = client.post(
        f"/worlds/{WORLD}/delete", data={"confirm": "nope"}, follow_redirects=False
    )
    assert r.status_code >= 400
    snaps_before_delete = set(snapshot_files(WORLD))
    post_ok(client, f"/worlds/{WORLD}/delete", {"confirm": WORLD})
    wait_state(client, WORLD, None, timeout=600)

    # Release, PVC and RCON secret are gone; a final snapshot was kept.
    deadline = time.monotonic() + 180
    while kubectl("get", "pvc", f"{WORLD}-data", check=False).returncode == 0:
        assert time.monotonic() < deadline, f"PVC {WORLD}-data not deleted"
        time.sleep(5)
    assert kubectl("get", "secret", f"{WORLD}-rcon", check=False).returncode != 0
    assert set(snapshot_files(WORLD)) - snaps_before_delete, "no final snapshot"
