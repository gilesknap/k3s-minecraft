"""Error mapping and the SSE log stream."""

import asyncio
import time
from collections.abc import MutableMapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mcdash.models import Conflict, Forbidden, MCDashError, NotFound
from mcdash.web.app import create_app

from .conftest import ACTOR, HX
from .fakes import FakeDashboardService, seeded


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (NotFound("gone"), 404),
        (Conflict("busy"), 409),
        (Forbidden("nope"), 403),
        (MCDashError("bad"), 400),
    ],
)
@pytest.mark.parametrize("htmx", [False, True])
def test_error_mapping(exc: MCDashError, code: int, htmx: bool) -> None:
    svc = seeded()

    def boom(name: str, actor: str) -> None:
        raise exc

    svc.start = boom  # type: ignore[method-assign]
    c = TestClient(create_app(svc), headers={"X-Auth-Request-Email": ACTOR})
    r = c.post("/worlds/fire-ice2/start", headers=HX if htmx else {})
    assert r.status_code == code
    assert str(exc) in r.text
    assert "alert-error" in r.text
    if htmx:
        assert r.headers["HX-Retarget"] == "#flash"
        assert r.headers["HX-Reswap"] == "innerHTML"
        assert "<html" not in r.text  # just the fragment
    else:
        assert "HX-Retarget" not in r.headers
        assert "<html" in r.text  # a full friendly page


def test_html_escapes_error_text(client: TestClient) -> None:
    r = client.get("/worlds/<script>")
    assert r.status_code == 404
    assert "<script>" not in r.text


def test_sse_streams_data_lines(svc: FakeDashboardService) -> None:
    svc.log_lines = ["first line", "second\nmultiline", "third"]
    c = TestClient(create_app(svc))
    with c.stream("GET", "/worlds/noah-james/logs") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "data: first line\n\n" in body
    assert "data: second\ndata: multiline\n\n" in body
    assert "data: third\n\n" in body
    assert body.rstrip().endswith("event: end\ndata:")


def test_sse_unknown_world_404(client: TestClient) -> None:
    assert client.get("/worlds/nope/logs").status_code == 404


def test_sse_stream_error_reported(svc: FakeDashboardService) -> None:
    def broken(name: str):
        yield "one"
        raise MCDashError("pod gone")

    svc.stream_logs = broken  # type: ignore[method-assign]
    c = TestClient(create_app(svc))
    body = c.get("/worlds/noah-james/logs").text
    assert "data: one" in body
    assert "event: error\ndata: pod gone" in body


def test_sse_stops_consuming_when_client_disconnects(
    svc: FakeDashboardService,
) -> None:
    """A client disconnect cancels the response and stops the pump thread."""
    pulled: list[int] = []

    def endless(name: str):
        for i in range(100_000):
            pulled.append(i)
            time.sleep(0.001)
            yield f"line {i}"

    svc.stream_logs = endless  # type: ignore[method-assign]
    app = create_app(svc)
    sent: list[bytes] = []
    disconnected = asyncio.Event()

    requested = False

    async def receive() -> dict:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.body":
            sent.append(message.get("body", b""))
            if b"line 5" in b"".join(sent):
                disconnected.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/worlds/noah-james/logs",
        "raw_path": b"/worlds/noah-james/logs",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }

    async def main() -> tuple[int, int]:
        await asyncio.wait_for(app(scope, receive, send), timeout=10)
        # keep the loop alive (as uvicorn would) and watch the pump thread
        await asyncio.sleep(0.2)
        n = len(pulled)
        await asyncio.sleep(0.3)
        return n, len(pulled)

    before, after = asyncio.run(main())
    assert b"data: line 0" in b"".join(sent)
    assert after <= before + 1
    assert before < 100_000
