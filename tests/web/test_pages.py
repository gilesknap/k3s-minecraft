"""Every page renders against the seeded fake."""

import re
from urllib.parse import quote, unquote

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
        "/new",
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
    start = html.rindex("<tr", 0, start)
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
    assert 'name="ops" value="NoahK"' in html  # an admin box per player


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
    # snapshot rows offer the OK/Cancel delete and come back to this page
    assert 'hx-confirm="Permanently delete this snapshot of' in html
    assert 'name="back" value="/worlds/noah-james"' in html
    # current whitelist pre-ticked; roster member not on it unticked
    assert 'value="JamesK" checked' in html
    assert 'value="MillieM" >' in html
    # admins ticked separately
    assert 'name="ops" value="GilesDad" checked' in html
    assert 'name="ops" value="JamesK" >' in html


def test_world_page_console_hooks(client: TestClient) -> None:
    """The attributes app.js binds the console history and completion to."""
    html = client.get("/worlds/noah-james").text
    assert 'id="rcon-input"' in html
    assert 'data-history-key="mcdash.rcon.noah-james"' in html
    assert 'data-complete="/worlds/noah-james/complete"' in html
    assert 'aria-controls="rcon-suggest"' in html and 'id="rcon-suggest"' in html


def test_world_complete(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.get("/worlds/noah-james/complete", params={"line": "ti"})
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["status"] == "ready" and body["start"] == 0
    assert [i["text"] for i in body["items"]] == ["tick", "time", "title"]
    body = client.get("/worlds/noah-james/complete", params={"line": "tp "}).json()
    assert {"NoahK", "GilesDad"} <= {i["text"] for i in body["items"]}
    hint = next(i for i in body["items"] if i["kind"] == "hint")
    assert hint["text"].startswith("<")
    # no command is sent and nothing is audited
    assert not [c for c in svc.calls if c[0] == "rcon"]


def test_world_complete_modded_and_missing(client: TestClient) -> None:
    body = client.get("/worlds/fire-ice2/complete", params={"line": "ti"}).json()
    assert body == {
        "start": 2,
        "items": [],
        "status": "none",
        "message": "modded server",
    }
    assert client.get("/worlds/nope/complete", params={"line": "x"}).status_code == 404


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
    assert "whitelist and admins" in html


def test_new_world_form(client: TestClient) -> None:
    html = client.get("/new").text
    assert 'action="/new"' in html and 'name="backup_id"' not in html
    assert 'value="new-world"' in html
    assert 'name="seed"' in html and 'name="level_type"' in html
    for v in ("normal", "flat", "amplified", "large_biomes"):
        assert f'value="{v}"' in html
    assert 'name="version" value="LATEST"' in html
    assert 'name="whitelist" value="NoahK"' in html  # roster offered
    # the launch-from-backup form does not offer generation settings
    launch = client.get(f"/launch?backup={quote(ARCHIVE)}").text
    assert 'name="seed"' not in launch and 'action="/launch"' in launch


@pytest.mark.parametrize("url", ["/", "/backups"])
def test_new_world_button(client: TestClient, url: str) -> None:
    assert 'href="/new"' in client.get(url).text


def _launch_order(html: str) -> list[str]:
    return re.findall(r'/launch\?backup=([^"]+)"', html)


@pytest.mark.parametrize("field", ["date", "tier", "label", "version", "type", "size"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_backups_flat_sort(client: TestClient, field: str, direction: str) -> None:
    html = client.get(f"/backups?sort={field}&dir={direction}").text
    assert 'class="bgroup"' not in html  # flat table, not grouped
    assert 'data-label="World"' in html
    assert 'aria-sort="' + ("ascending" if direction == "asc" else "descending") in html
    rows = _launch_order(html)
    assert len(rows) == len(set(rows)) > 1


def test_backups_sort_size_desc_is_largest_first(
    client: TestClient, svc: FakeDashboardService
) -> None:
    order = [
        unquote(r)
        for r in _launch_order(client.get("/backups?sort=size&dir=desc").text)
    ]
    sizes = {b.id: b.size_bytes for b in svc.list_backups()}
    assert [sizes[i] for i in order] == sorted(sizes.values(), reverse=True)


def test_version_key_is_numeric() -> None:
    from mcdash.web.app import _version_key

    assert _version_key("1.9") < _version_key("1.12.2") < _version_key("1.16.5")
    assert _version_key(None) < _version_key("1.0")


def test_backups_world_sort_reverses_groups(client: TestClient) -> None:
    asc = client.get("/backups?sort=world&dir=asc").text
    desc = client.get("/backups?sort=world&dir=desc").text
    a = re.findall(r'id="w-([^"]+)"', asc)
    d = re.findall(r'id="w-([^"]+)"', desc)
    assert a == sorted(a) and d == list(reversed(a))


def test_backups_bad_sort_falls_back_to_grouped(client: TestClient) -> None:
    html = client.get("/backups?sort=nonsense&dir=sideways").text
    assert 'class="bgroup"' in html


def test_header_link_toggles_direction(client: TestClient) -> None:
    html = client.get("/backups?sort=date&dir=desc").text
    assert "/backups?sort=date&amp;dir=asc" in html  # active column flips
    assert (
        "/backups?sort=size&amp;dir=desc" in html
    )  # first click on size: largest first
    assert "/backups?sort=label&amp;dir=asc" in html


def test_settings_form_server_properties(client: TestClient) -> None:
    html = client.get("/worlds/noah-james").text
    assert 'name="view_distance" type="number" min="2" max="32" value="12"' in html
    assert 'name="spawn_protection" type="number" min="0" max="100000" value="0"' in (
        html
    )
    assert 'name="pause_when_empty_seconds"' in html and 'value="0"' in html
    assert 'name="allow_flight" checked' in html
    assert 'name="sync_chunk_writes" >' in html  # unchecked
    assert 'name="broadcast_console_to_ops" checked' in html
    assert 'name="use_aikar_flags" checked' in html
    assert "<legend>Performance</legend>" in html
    assert 'name="hardcore"' not in html  # new worlds only


def test_settings_form_old_record(client: TestClient) -> None:
    """A world from before these settings: blank numbers, "unchanged" selects."""
    html = client.get("/worlds/fire-ice2").text
    assert re.search(r'name="view_distance"[^>]*value=""', html)
    assert 'placeholder="unchanged (default 10)"' in html
    assert re.search(
        r'<select name="allow_flight">\s*<option value="" selected>unchanged', html
    )
    assert 'name="use_aikar_flags" checked' in html


def test_new_world_form_server_properties(client: TestClient) -> None:
    html = client.get("/new").text
    assert 'name="hardcore"' in html and 'name="hardcore" checked' not in html
    assert re.search(r'name="view_distance"[^>]*value="10"', html)
    assert re.search(r'name="pause_when_empty_seconds"[^>]*value="60"', html)
    assert 'name="sync_chunk_writes" >' in html  # off by default
    assert 'name="broadcast_rcon_to_ops" checked' in html
    assert 'name="use_aikar_flags" checked' in html
    assert '<option value="" selected>unchanged' not in html


def test_launch_form_server_properties(client: TestClient) -> None:
    html = client.get(f"/launch?backup={quote(FORGE)}").text
    assert re.search(r'name="view_distance"[^>]*value="10"', html)
    # Forge 1.16.5 has no simulation-distance / pause-when-empty-seconds
    assert re.search(r'name="simulation_distance"[^>]*value=""', html)
    assert 'name="hardcore"' not in html


@pytest.mark.parametrize("env, shown", [("0.4.1", "0.4.1"), ("", "dev")])
def test_version_shown_in_header(
    svc: FakeDashboardService, monkeypatch: pytest.MonkeyPatch, env: str, shown: str
) -> None:
    monkeypatch.setenv("MCD_VERSION", env)
    c = TestClient(create_app(svc))
    for url in ("/", "/backups", "/worlds/noah-james"):
        assert f'<span class="version" title="Dashboard release">{shown}</span>' in (
            c.get(url).text
        )
