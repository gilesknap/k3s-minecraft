"""World page: direct RCON addresses and the reveal-password endpoint."""

from fastapi.testclient import TestClient

from .conftest import ACTOR, HX
from .fakes import FakeDashboardService


def test_world_page_shows_rcon_addresses(client: TestClient) -> None:
    html = client.get("/worlds/noah-james").text
    assert 'id="rcon-direct"' in html
    for addr in ("192.168.1.92:30600", "127.0.0.1:30600"):
        assert f"<code>{addr}</code>" in html
        assert f'data-copy="{addr}"' in html  # copy button
    assert "On nuc2" in html
    assert 'Iwb(server="192.168.1.92", port=30600' in html
    assert 'hx-get="/worlds/noah-james/rcon-password"' in html


def test_world_page_without_node_address(client: TestClient) -> None:
    """Stopped: no pod, so no node IP yet; the node-local address still shows."""
    html = client.get("/worlds/fire-ice2").text
    assert "node address shown while the world runs" in html
    assert "<code>127.0.0.1:30601</code>" in html


def test_world_page_without_rcon_port(client: TestClient) -> None:
    html = client.get("/worlds/king-ghidorah").text
    assert "gets an RCON port at its next start" in html
    assert "/worlds/king-ghidorah/rcon-password" not in html


def test_password_not_in_page(client: TestClient, svc: FakeDashboardService) -> None:
    pw = svc.rcon_password("noah-james", "test")
    for url in ("/worlds/noah-james", "/worlds/noah-james/status", "/"):
        assert pw not in client.get(url).text, url


def test_reveal_password(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.get("/worlds/noah-james/rcon-password", headers=HX)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    pw = "pw-noah-james-s3cret"
    assert f"<code>{pw}</code>" in r.text
    assert f'data-copy="{pw}"' in r.text and 'title="Copy password"' in r.text
    assert svc.calls[-1] == ("rcon_password", "noah-james", ACTOR)


def test_reveal_password_unknown_world(client: TestClient) -> None:
    r = client.get("/worlds/nope/rcon-password", headers=HX)
    assert r.status_code == 404
