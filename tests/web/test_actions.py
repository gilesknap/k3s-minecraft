"""Each action calls the right service method with the actor."""

from urllib.parse import quote

from fastapi.testclient import TestClient

from mcdash.models import Player, ServerType, Tier, WorldState

from .conftest import ACTOR, HX
from .fakes import FakeDashboardService

ARCHIVE = "archive/2023-02-25_16_53_58-noah-james.zip"
SNAP = "snapshot/noah-james/20261011T090000Z.zip"
BACKUP = "backup/noah-james-end-portal.zip"


def test_start(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/worlds/fire-ice2/start")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/worlds/fire-ice2")
    assert svc.calls[-1] == ("start", "fire-ice2", ACTOR)


def test_stop_htmx_from_worlds_table(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post("/worlds/noah-james/stop", headers={**HX, "HX-Target": "worlds"})
    assert r.status_code == 200
    assert svc.calls[-1] == ("stop", "noah-james", ACTOR)
    assert "<table" in r.text  # the refreshed table partial
    assert 'hx-swap-oob="innerHTML"' in r.text
    assert "snapshot is taken first" in r.text


def test_snapshot_htmx_from_world_page(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post(
        "/worlds/noah-james/snapshot", headers={**HX, "HX-Target": "world-status"}
    )
    assert r.status_code == 200
    assert svc.calls[-1] == ("snapshot", "noah-james", ACTOR)
    assert 'class="facts"' in r.text
    assert "state-snapshotting" in r.text


def test_restore(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/worlds/noah-james/restore", data={"backup_id": ARCHIVE})
    assert r.status_code == 303
    assert svc.calls[-1] == ("restore", "noah-james", ARCHIVE, ACTOR)


def test_delete_world_needs_typed_name(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post("/worlds/noah-james/delete", data={"confirm": "noah"})
    assert r.status_code == 400
    assert "noah-james" in svc.worlds
    r = client.post("/worlds/noah-james/delete", data={"confirm": "noah-james"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?msg=")
    assert svc.calls[-1] == ("delete", "noah-james", ACTOR)


def test_htmx_success_uses_hx_redirect(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post(
        "/worlds/noah-james/delete", data={"confirm": "noah-james"}, headers=HX
    )
    assert r.status_code == 200
    assert r.headers["HX-Redirect"].startswith("/?msg=Deleted")


def test_update_settings_keeps_whitelist(
    client: TestClient, svc: FakeDashboardService
) -> None:
    form = {
        "server_type": "FORGE",
        "version": "1.16.5",
        "memory": "8Gi",
        "gamemode": "creative",
        "difficulty": "peaceful",
        "max_world_size": "5000",
        "motd": " hi ",
        "pvp": "on",
    }
    r = client.post("/worlds/noah-james/settings", data=form)
    assert r.status_code == 303, r.text
    name, world, settings, actor = svc.calls[-1]
    assert (name, world, actor) == ("update_settings", "noah-james", ACTOR)
    assert settings.server_type is ServerType.FORGE
    assert settings.memory == "8Gi"
    assert settings.gamemode == "creative"
    assert settings.pvp is True
    assert settings.force_gamemode is False
    assert settings.enable_command_block is False
    assert settings.max_world_size == 5000
    assert settings.motd == "hi"
    assert settings.whitelist == ["NoahK", "JamesK", "GilesDad"]
    assert settings.open_to_all is False


def test_settings_validation(client: TestClient, svc: FakeDashboardService) -> None:
    n = len(svc.calls)
    r = client.post("/worlds/noah-james/settings", data={"memory": "lots"})
    assert r.status_code == 400
    assert "Memory must look like" in r.text
    assert len(svc.calls) == n


def test_set_whitelist(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post(
        "/worlds/noah-james/whitelist",
        data={
            "whitelist": ["NoahK", "MillieM"],
            "whitelist_extra": "Steve, alex noahk",
        },
    )
    assert r.status_code == 303
    assert svc.calls[-1] == (
        "set_whitelist",
        "noah-james",
        ["NoahK", "MillieM", "Steve", "alex"],
        ACTOR,
        False,
    )


def test_open_to_everyone(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post(
        "/worlds/noah-james/whitelist",
        data={"whitelist": ["NoahK"], "open_to_all": "on"},
    )
    assert r.status_code == 303
    assert svc.calls[-1] == ("set_whitelist", "noah-james", ["NoahK"], ACTOR, True)
    html = client.get("/worlds/noah-james").text
    assert 'name="open_to_all" checked' in html
    assert "open to everyone</span>" in html
    assert "open to everyone</span>" in client.get("/").text
    # the settings form leaves access alone
    client.post("/worlds/noah-james/settings", data={"memory": "4Gi"})
    assert svc.calls[-1][2].open_to_all is True


def test_whitelist_rejects_bad_name(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post(
        "/worlds/noah-james/whitelist", data={"whitelist_extra": "bad-name!"}
    )
    assert r.status_code == 400
    assert "not a valid Minecraft player name" in r.text


def test_rcon(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/worlds/noah-james/rcon", data={"command": "/list"}, headers=HX)
    assert r.status_code == 200
    assert svc.calls[-1] == ("rcon", "noah-james", "list", ACTOR)
    assert "&gt; /list" in r.text
    assert "NoahK, GilesDad" in r.text


def test_rcon_output_escaped(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/worlds/noah-james/rcon", data={"command": "<b>"}, headers=HX)
    assert "<b>" not in r.text
    assert "&lt;b&gt;" in r.text


def test_rcon_on_stopped_world_is_409_alert(client: TestClient) -> None:
    r = client.post("/worlds/fire-ice2/rcon", data={"command": "list"}, headers=HX)
    assert r.status_code == 409
    assert r.headers["HX-Retarget"] == "#flash"
    assert "alert-error" in r.text


def test_launch(client: TestClient, svc: FakeDashboardService) -> None:
    form = {
        "backup_id": ARCHIVE,
        "name": "noah-james-2",
        "server_type": "VANILLA",
        "version": "LATEST",
        "memory": "4Gi",
        "gamemode": "survival",
        "difficulty": "normal",
        "max_world_size": "29999984",
        "motd": "Back again",
        "force_gamemode": "on",
        "enable_command_block": "on",
        "whitelist": ["NoahK", "JamesK"],
        "whitelist_extra": "Cousin_Ed",
    }
    r = client.post("/launch", data=form)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/worlds/noah-james-2")
    call, backup_id, name, settings, actor = svc.calls[-1]
    assert (call, backup_id, name, actor) == (
        "launch",
        ARCHIVE,
        "noah-james-2",
        ACTOR,
    )
    assert settings.whitelist == ["NoahK", "JamesK", "Cousin_Ed"]
    assert settings.open_to_all is False
    assert settings.force_gamemode and settings.enable_command_block
    r = client.post(
        "/launch", data={**form, "name": "noah-james-3", "open_to_all": "on"}
    )
    assert r.status_code == 303, r.text
    assert svc.calls[-1][3].open_to_all is True
    assert not settings.pvp
    assert settings.difficulty == "normal"
    assert client.get("/worlds/noah-james-2").status_code == 200


def test_launch_name_taken_conflict(client: TestClient) -> None:
    r = client.post(
        "/launch", data={"backup_id": ARCHIVE, "name": "noah-james"}, headers=HX
    )
    assert r.status_code == 409
    assert "already exists" in r.text
    assert r.headers["HX-Retarget"] == "#flash"


def test_backup_label(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post(
        "/backups/label",
        data={"backup_id": ARCHIVE, "label": "Sky castle", "description": "d"},
    )
    assert r.status_code == 303
    assert svc.calls[-1] == ("set_backup_label", ARCHIVE, "Sky castle", "d")
    assert "Sky castle" in client.get("/backups").text


def test_promote(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post(
        "/backups/promote",
        data={
            "backup_id": SNAP,
            "label": "castle-done",
            "description": "after the castle",
            "back": "/worlds/noah-james",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/worlds/noah-james?msg=")
    assert svc.calls[-1] == ("promote", SNAP, "castle-done", "after the castle", ACTOR)
    assert svc.backups["backup/castle-done.zip"].tier is Tier.BACKUP


def test_promote_requires_name(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/backups/promote", data={"backup_id": SNAP, "label": " "})
    assert r.status_code == 400
    assert not any(c[0] == "promote" for c in svc.calls)


def test_back_redirect_is_local_only(client: TestClient) -> None:
    r = client.post(
        "/backups/label",
        data={"backup_id": ARCHIVE, "label": "x", "back": "//evil.example"},
    )
    assert r.headers["location"].startswith("/?msg=")


def test_delete_snapshot_needs_no_typed_name(
    client: TestClient, svc: FakeDashboardService
) -> None:
    # the page asks OK/Cancel (hx-confirm); no typed name for snapshots
    html = client.get("/backups").text
    assert 'hx-confirm="Permanently delete this snapshot of' in html
    # htmx-only form: without JS it must not post a delete
    assert 'action="/backups/delete" hx-post="/backups/delete" hx-confirm' not in html
    r = client.post("/backups/delete", data={"backup_id": SNAP, "confirm": "wrong"})
    assert r.status_code == 303  # any typed value is ignored for snapshots
    assert svc.calls[-1] == ("delete_backup", SNAP, ACTOR)
    assert SNAP not in svc.backups


def test_delete_backup_tier(client: TestClient, svc: FakeDashboardService) -> None:
    # promoted backups still need the world name typed
    r = client.post("/backups/delete", data={"backup_id": BACKUP, "confirm": "nope"})
    assert r.status_code == 400
    assert BACKUP in svc.backups
    r = client.post(
        "/backups/delete", data={"backup_id": BACKUP, "confirm": "noah-james"}
    )
    assert r.status_code == 303
    assert svc.calls[-1] == ("delete_backup", BACKUP, ACTOR)


def test_delete_archive_forbidden(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post(
        "/backups/delete",
        data={"backup_id": ARCHIVE, "confirm": "noah-james"},
        headers=HX,
    )
    assert r.status_code == 403
    assert "read-only" in r.text
    assert ARCHIVE in svc.backups
    assert not any(c[0] == "delete_backup" for c in svc.calls)


def test_launch_link_roundtrip(client: TestClient) -> None:
    html = client.get("/backups").text
    assert f"/launch?backup={quote(SNAP)}" in html
    assert client.get(f"/launch?backup={quote(SNAP)}").status_code == 200


def test_roster_add_update_remove(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post("/roster/add", data={"name": "Steve", "note": "neighbour"})
    assert r.status_code == 303
    assert svc.players[-1] == Player("Steve", "neighbour")
    client.post("/roster/add", data={"name": "steve", "note": "next door"})
    assert [p for p in svc.players if p.name == "Steve"] == [
        Player("Steve", "next door")
    ]
    r = client.post("/roster/remove", data={"name": "Steve"})
    assert r.status_code == 303
    assert all(p.name != "Steve" for p in svc.players)
    assert svc.calls[-1][0] == "set_roster"


def test_roster_add_invalid(client: TestClient) -> None:
    r = client.post("/roster/add", data={"name": "no spaces"})
    assert r.status_code == 400


def test_roster_remove_unknown(client: TestClient) -> None:
    r = client.post("/roster/remove", data={"name": "Ghost"})
    assert r.status_code == 404


def test_get_on_action_not_allowed(client: TestClient) -> None:
    assert client.get("/worlds/noah-james/stop").status_code == 405


def test_world_state_after_stop(client: TestClient, svc: FakeDashboardService) -> None:
    client.post("/worlds/noah-james/stop")
    assert svc.worlds["noah-james"].state is WorldState.SNAPSHOTTING


# -- new world ------------------------------------------------------------------
NEW_FORM = {
    "name": "fresh",
    "seed": "12345",
    "level_type": "flat",
    "server_type": "VANILLA",
    "version": "LATEST",
    "memory": "2Gi",
    "gamemode": "creative",
    "difficulty": "peaceful",
    "max_world_size": "29999984",
    "motd": "",
    "whitelist": ["NoahK"],
    "whitelist_extra": "Cousin_Ed",
}


def test_new_world(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/new", data=NEW_FORM)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/worlds/fresh")
    call, name, settings, actor = svc.calls[-1]
    assert (call, name, actor) == ("create_world", "fresh", ACTOR)
    assert (settings.seed, settings.level_type) == ("12345", "flat")
    assert (settings.gamemode, settings.memory) == ("creative", "2Gi")
    assert settings.whitelist == ["NoahK", "Cousin_Ed"]
    page = client.get("/worlds/fresh")
    assert page.status_code == 200
    assert "new world" in page.text and "Superflat" in page.text
    assert "12345" in page.text


def test_new_world_blank_seed_is_random(
    client: TestClient, svc: FakeDashboardService
) -> None:
    r = client.post("/new", data={**NEW_FORM, "seed": "  ", "level_type": "normal"})
    assert r.status_code == 303
    settings = svc.calls[-1][2]
    assert (settings.seed, settings.level_type) == ("", "normal")


def test_new_world_errors(client: TestClient, svc: FakeDashboardService) -> None:
    r = client.post("/new", data={**NEW_FORM, "name": "noah-james"}, headers=HX)
    assert r.status_code == 409 and r.headers["HX-Retarget"] == "#flash"
    assert "already exists" in r.text
    r = client.post("/new", data={**NEW_FORM, "level_type": "caves"}, headers=HX)
    assert r.status_code == 400 and "world type" in r.text
    r = client.post("/new", data={**NEW_FORM, "name": ""})
    assert r.status_code == 400
    assert not [c for c in svc.calls if c[0] == "create_world" and c[1] != "noah-james"]


def test_settings_update_keeps_generation_settings(
    client: TestClient, svc: FakeDashboardService
) -> None:
    client.post("/new", data=NEW_FORM)
    form = {k: v for k, v in NEW_FORM.items() if k not in ("seed", "level_type")}
    r = client.post("/worlds/fresh/settings", data={**form, "memory": "3Gi"})
    assert r.status_code == 303
    call, name, settings, _ = svc.calls[-1]
    assert (call, name, settings.memory) == ("update_settings", "fresh", "3Gi")
    assert (settings.seed, settings.level_type) == ("12345", "flat")
