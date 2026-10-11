"""Every page renders against the seeded fake."""

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from mcdash.web.app import create_app

from .conftest import ACTOR
from .fakes import FakeDashboardService

ARCHIVE = "archive/2023-02-25_16_53_58-noah-james.zip"
FORGE = "archive/2021-11-20_19_30_02-fire-ice2.zip"
SNAP = "snapshot/noah-james/20261011T090000Z.zip"


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/worlds/table",
        "/backups",
        "/roster",
        "/worlds/noah-james",
        "/worlds/fire-ice2",
        "/worlds/king-ghidorah",
        "/worlds/adventure-of-doom",
        "/worlds/noah-james/status",
        f"/launch?backup={quote(ARCHIVE)}",
    ],
)
def test_page_renders(client: TestClient, url: str) -> None:
    r = client.get(url)
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.text == "ok"


def test_static_htmx_is_vendored(client: TestClient) -> None:
    r = client.get("/static/htmx.min.js")
    assert r.status_code == 200
    assert "htmx" in r.text[:200]
    assert "unpkg.com" not in client.get("/").text


def test_actor_shown_in_header(client: TestClient) -> None:
    assert ACTOR in client.get("/").text


def test_actor_defaults_to_unknown(svc: FakeDashboardService) -> None:
    c = TestClient(create_app(svc))
    assert '<span class="actor" title="Signed in as">unknown</span>' in c.get("/").text


def test_worlds_table(client: TestClient) -> None:
    html = client.get("/").text
    assert 'hx-trigger="every 5s"' in html
    for text in [
        "noah-james",
        "mc.gkcluster.org:30565",
        "state-running",
        "state-failed",
        "state-seeding",
        "OutOfMemoryError",
        "NoahK, GilesDad",
        "3h 17m",
        "1.16.5",
        "forge",
        "data-copy=",
        "/worlds/fire-ice2/start",
        "/worlds/noah-james/stop",
        "/worlds/noah-james/snapshot",
    ]:
        assert text in html, text


def test_worlds_empty(svc: FakeDashboardService, client: TestClient) -> None:
    svc.worlds.clear()
    assert "No worlds yet" in client.get("/").text


def test_backups_grouped_newest_first(client: TestClient) -> None:
    html = client.get("/backups").text
    # heading per world, alphabetical
    order = [html.index(f'id="w-{w}"') for w in ["adventure-of-doom", "fire-ice2"]]
    assert order == sorted(order)
    section = html[html.index('id="w-noah-james"') :]
    section = section[: section.index("</section>")]
    dates = ["2026-10-11 09:00", "2026-10-10 21:30", "2026-10-04 18:12", "2023-02-25"]
    idx = [section.index(d) for d in dates]
    assert idx == sorted(idx)
    for tier in ("tier-archive", "tier-backup", "tier-snapshot"):
        assert tier in section
    assert "Final survival" in section


def _row_for(html: str, backup_id: str) -> str:
    start = html.index(f"/launch?backup={quote(backup_id)}")
    start = html.rindex("<tr>", 0, start)
    return html[start : html.index("</tr>", start)]


def test_archive_delete_not_offered(client: TestClient) -> None:
    html = client.get("/backups").text
    archive_row = _row_for(html, ARCHIVE)
    assert "/backups/delete" not in archive_row
    assert "Edit label" in archive_row
    assert "Promote" not in archive_row
    snap_row = _row_for(html, SNAP)
    assert "/backups/delete" in snap_row
    assert "/backups/promote" in snap_row
    backup_row = _row_for(html, "backup/noah-james-end-portal.zip")
    assert "/backups/delete" in backup_row
    assert "Edit label" in backup_row


def test_launch_form_prefilled(client: TestClient) -> None:
    html = client.get(f"/launch?backup={quote(FORGE)}").text
    assert 'value="fire-ice2-2"' in html  # name taken -> suffix
    assert '<option value="FORGE" selected>' in html
    assert 'name="version" value="1.16.5"' in html
    assert 'name="memory" value="6Gi"' in html
    assert "<option selected>hard</option>" in html
    assert 'value="Fire and Ice (modded)"' in html
    for player in ("NoahK", "JamesK", "GilesDad", "MillieM"):
        assert f'name="whitelist" value="{player}"' in html
    assert 'name="whitelist_extra"' in html


def test_launch_form_command_blocks(client: TestClient) -> None:
    bid = "archive/2021-08-09_14_22_10-adventure-of-doom.zip"
    html = client.get(f"/launch?backup={quote(bid)}").text
    assert 'name="enable_command_block" checked' in html
    assert 'name="force_gamemode" checked' in html
    assert "<option selected>adventure</option>" in html


def test_launch_unknown_backup_404(client: TestClient) -> None:
    r = client.get("/launch?backup=archive/nope.zip")
    assert r.status_code == 404
    assert "No backup" in r.text


def test_world_page_sections(client: TestClient) -> None:
    html = client.get("/worlds/noah-james").text
    for text in [
        'id="world-status"',
        "/worlds/noah-james/rcon",
        'data-src="/worlds/noah-james/logs"',
        "/worlds/noah-james/whitelist",
        "/worlds/noah-james/settings",
        "/worlds/noah-james/restore",
        "/worlds/noah-james/delete",
        "a snapshot of the current state is taken first",
        "port, whitelist and settings are kept",
        "final snapshot is taken first",
        "2026-10-11 09:00",  # snapshot list
        "/backups/promote",
    ]:
        assert text.lower() in html.lower(), text
    # current whitelist pre-ticked; roster member not on it unticked
    assert 'value="JamesK" checked' in html
    assert 'value="MillieM" >' in html


def test_world_page_lists_only_its_snapshots(client: TestClient) -> None:
    html = client.get("/worlds/fire-ice2").text
    snaps = html[html.index("<h2>Snapshots") : html.index("Restore from")]
    assert "2026-10-09 17:45" in snaps
    assert "2026-10-11 09:00" not in snaps


def test_unknown_world_404(client: TestClient) -> None:
    r = client.get("/worlds/nope")
    assert r.status_code == 404
    assert "No world called" in r.text


def test_roster_page(client: TestClient) -> None:
    html = client.get("/roster").text
    assert "JamesK" in html and "James (cousin)" in html
