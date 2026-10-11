"""FastAPI + HTMX web UI over a ``DashboardService``.

Server-rendered Jinja2 pages; HTMX swaps partials and shows errors as an alert
fragment retargeted to ``#flash``. Every mutating action is a POST. The caller
identity comes from oauth2-proxy's ``X-Auth-Request-Email`` header.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from mcdash.models import (
    LEVEL_TYPES,
    Backup,
    Conflict,
    DashboardService,
    Forbidden,
    MCDashError,
    NotFound,
    Player,
    ServerType,
    Tier,
    World,
    WorldSettings,
    WorldState,
)

HERE = Path(__file__).parent
log = logging.getLogger(__name__)
GAMEMODES = ["survival", "creative", "adventure", "spectator"]
DIFFICULTIES = ["peaceful", "easy", "normal", "hard"]
LEVEL_TYPE_LABELS = {
    "normal": "Normal",
    "flat": "Superflat",
    "amplified": "Amplified",
    "large_biomes": "Large biomes",
}
PLAYER_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
SSE_KEEPALIVE_S = 15.0


# -- template helpers --------------------------------------------------------
def humansize(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"


def duration(secs: int | None) -> str:
    if secs is None:
        return "—"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def ago(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    secs = (datetime.now(UTC) - dt).total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60:.0f}m ago"
    if secs < 86400:
        return f"{secs // 3600:.0f}h ago"
    if secs < 86400 * 60:
        return f"{secs // 86400:.0f}d ago"
    return dt.strftime("%Y-%m-%d")


def fmtdate(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%Y-%m-%d %H:%M")


def cpu(m: int | None) -> str:
    if m is None:
        return "—"
    return f"{m / 1000:.2f} cores" if m >= 1000 else f"{m}m"


# -- request helpers ---------------------------------------------------------
def get_actor(request: Request) -> str:
    return request.headers.get("x-auth-request-email") or "unknown"


Actor = Annotated[str, Depends(get_actor)]


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def parse_players(selected: list[str], extra: str) -> list[str]:
    """Roster checkboxes + free text (comma/space separated), deduped."""
    out: list[str] = []
    for name in [*selected, *re.split(r"[\s,]+", extra or "")]:
        name = name.strip()
        if not name:
            continue
        if not PLAYER_RE.match(name):
            raise MCDashError(f"{name!r} is not a valid Minecraft player name")
        if name.lower() not in (o.lower() for o in out):
            out.append(name)
    return out


def settings_from_form(form: FormData, whitelist: list[str]) -> WorldSettings:
    def text(key: str, default: str = "") -> str:
        v = form.get(key)
        return v.strip() if isinstance(v, str) else default

    try:
        server_type = ServerType(text("server_type", "VANILLA"))
    except ValueError:
        raise MCDashError("Unknown server type") from None
    gamemode = text("gamemode", "survival")
    if gamemode not in GAMEMODES:
        raise MCDashError(f"Unknown gamemode {gamemode!r}")
    difficulty = text("difficulty", "easy")
    if difficulty not in DIFFICULTIES:
        raise MCDashError(f"Unknown difficulty {difficulty!r}")
    try:
        max_world_size = int(text("max_world_size", "29999984"))
    except ValueError:
        raise MCDashError("Max world size must be a whole number") from None
    if not 1 <= max_world_size <= 29999984:
        raise MCDashError("Max world size must be between 1 and 29999984")
    memory = text("memory", "4Gi")
    if not re.fullmatch(r"\d+(\.\d+)?(Mi|Gi)", memory):
        raise MCDashError("Memory must look like 4Gi or 3584Mi")
    level_type = text("level_type", "normal")
    if level_type not in LEVEL_TYPES:
        raise MCDashError(f"Unknown world type {level_type!r}")
    return WorldSettings(
        version=text("version") or "LATEST",
        server_type=server_type,
        memory=memory,
        gamemode=gamemode,
        force_gamemode="force_gamemode" in form,
        difficulty=difficulty,
        pvp="pvp" in form,
        enable_command_block="enable_command_block" in form,
        motd=text("motd"),
        max_world_size=max_world_size,
        whitelist=whitelist,
        seed=text("seed"),
        level_type=level_type,
    )


def whitelist_from_form(form: FormData) -> list[str]:
    selected = [v for v in form.getlist("whitelist") if isinstance(v, str)]
    extra = form.get("whitelist_extra")
    return parse_players(selected, extra if isinstance(extra, str) else "")


def check_confirm(typed: str, expected: str) -> None:
    if typed.strip() != expected:
        raise MCDashError(f"Type {expected!r} exactly to confirm")


def sort_backups(backups: list[Backup]) -> list[tuple[str, list[Backup]]]:
    """Group by world name (alphabetical), newest first within a group."""
    ordered = sorted(backups, key=lambda b: b.world_name)
    groups = []
    for world, items in groupby(ordered, key=lambda b: b.world_name):
        newest = sorted(
            items,
            key=lambda b: b.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        groups.append((world, newest))
    return groups


def _version_key(v: str | None) -> tuple[int, ...]:
    """Numeric version order (1.9 < 1.12 < 1.16.5); unknown sorts first."""
    if not v:
        return (-1,)
    parts = []
    for p in v.split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _date_key(b: Backup) -> datetime:
    return b.timestamp or datetime.min.replace(tzinfo=UTC)


BACKUP_SORTS: dict[str, Callable[[Backup], Any]] = {
    "world": lambda b: b.world_name,
    "date": _date_key,
    "tier": lambda b: b.tier.value,
    "label": lambda b: (b.label or b.id.split("/")[-1]).lower(),
    "version": lambda b: _version_key(b.mc_version),
    "type": lambda b: b.server_type.value,
    "size": lambda b: b.size_bytes,
}


def default_dir(field: str) -> str:
    """First click on a column: newest / largest first, otherwise A-Z."""
    return "desc" if field in ("date", "size", "version") else "asc"


def sort_backups_by(backups: list[Backup], field: str, direction: str) -> list[Backup]:
    """Flat sort by one field; ties go world A-Z, then newest first."""
    ordered = sorted(backups, key=_date_key, reverse=True)
    ordered.sort(key=lambda b: b.world_name)
    ordered.sort(key=BACKUP_SORTS[field], reverse=direction == "desc")
    return ordered


def sse_event(data: str, event: str | None = None) -> str:
    lines = [f"event: {event}"] if event else []
    lines += [f"data: {part}" for part in (data.splitlines() or [""])]
    return "\n".join(lines) + "\n\n"


# -- app ---------------------------------------------------------------------
def create_app(service: DashboardService) -> FastAPI:
    app = FastAPI(title="Minecraft dashboard", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(
        humansize=humansize, duration=duration, ago=ago, fmtdate=fmtdate, cpu=cpu
    )
    env.globals.update(
        Tier=Tier,
        WorldState=WorldState,
        GAMEMODES=GAMEMODES,
        DIFFICULTIES=DIFFICULTIES,
        LEVEL_TYPE_LABELS=LEVEL_TYPE_LABELS,
        ServerType=ServerType,
        default_dir=default_dir,
    )

    def render(
        request: Request, template: str, status_code: int = 200, **ctx: object
    ) -> HTMLResponse:
        ctx.setdefault("actor", get_actor(request))
        ctx.setdefault("msg", request.query_params.get("msg", ""))
        return templates.TemplateResponse(
            request, template, ctx, status_code=status_code
        )

    def done(request: Request, url: str, msg: str = "") -> Response:
        """Success → navigate (HX-Redirect for HTMX, 303 otherwise)."""
        if msg:
            url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
        if is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    def partial_or_done(request: Request, name: str, msg: str) -> Response:
        """Re-render the requesting HTMX partial (+ OOB flash), else redirect."""
        target = request.headers.get("hx-target")
        if is_htmx(request) and target == "worlds":
            return render(
                request,
                "_worlds_table.html",
                worlds=service.list_worlds(),
                oob_msg=msg,
            )
        if is_htmx(request) and target == "world-status":
            return render(
                request,
                "_world_status.html",
                world=service.get_world(name),
                oob_msg=msg,
            )
        return done(request, f"/worlds/{name}", msg)

    @app.exception_handler(MCDashError)
    async def mcdash_error(request: Request, exc: MCDashError) -> Response:
        if isinstance(exc, NotFound):
            code = 404
        elif isinstance(exc, Conflict):
            code = 409
        elif isinstance(exc, Forbidden):
            code = 403
        else:
            code = 400
        if is_htmx(request):
            resp = render(
                request, "_alert.html", status_code=code, level="error", text=str(exc)
            )
            resp.headers["HX-Retarget"] = "#flash"
            resp.headers["HX-Reswap"] = "innerHTML"
            return resp
        return render(request, "error.html", status_code=code, code=code, text=str(exc))

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> Response:
        """Anything else (e.g. cluster unreachable): show it, keep the UI up."""
        log.exception("%s %s failed", request.method, request.url.path)
        resp = await mcdash_error(
            request, MCDashError(f"Internal error: {type(exc).__name__}: {exc}")
        )
        resp.status_code = 500
        return resp

    # -- misc --------------------------------------------------------------
    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    # -- worlds list -------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def worlds_page(request: Request) -> Response:
        return render(request, "worlds.html", worlds=service.list_worlds())

    @app.get("/worlds/table", response_class=HTMLResponse)
    def worlds_table(request: Request) -> Response:
        return render(request, "_worlds_table.html", worlds=service.list_worlds())

    # -- world detail ------------------------------------------------------
    @app.get("/worlds/{name}", response_class=HTMLResponse)
    def world_page(request: Request, name: str) -> Response:
        world = service.get_world(name)
        snaps = sorted(
            service.list_snapshots(name),
            key=lambda b: b.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return render(
            request,
            "world.html",
            world=world,
            snapshots=snaps,
            backup_groups=sort_backups(service.list_backups()),
            roster=service.roster(),
        )

    @app.get("/worlds/{name}/status", response_class=HTMLResponse)
    def world_status(request: Request, name: str) -> Response:
        return render(request, "_world_status.html", world=service.get_world(name))

    @app.post("/worlds/{name}/start")
    def world_start(request: Request, name: str, actor: Actor) -> Response:
        service.start(name, actor)
        return partial_or_done(request, name, f"Starting {name}")

    @app.post("/worlds/{name}/stop")
    def world_stop(request: Request, name: str, actor: Actor) -> Response:
        service.stop(name, actor)
        return partial_or_done(
            request, name, f"Stopping {name} (a snapshot is taken first)"
        )

    @app.post("/worlds/{name}/snapshot")
    def world_snapshot(request: Request, name: str, actor: Actor) -> Response:
        service.snapshot(name, actor)
        return partial_or_done(request, name, f"Snapshot of {name} requested")

    @app.post("/worlds/{name}/restore")
    def world_restore(
        request: Request, name: str, actor: Actor, backup_id: Annotated[str, Form()]
    ) -> Response:
        service.restore(name, backup_id, actor)
        return done(
            request,
            f"/worlds/{name}",
            f"Restoring {name} from {backup_id} (snapshot taken first)",
        )

    @app.post("/worlds/{name}/delete")
    def world_delete(
        request: Request,
        name: str,
        actor: Actor,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        check_confirm(confirm, name)
        service.delete(name, actor)
        return done(request, "/", f"Deleted {name} (final snapshot kept)")

    @app.post("/worlds/{name}/settings")
    async def world_settings(request: Request, name: str, actor: Actor) -> Response:
        form = await request.form()
        world = service.get_world(name)
        settings = settings_from_form(form, list(world.settings.whitelist))
        # generation-only: not on this form, keep what the world was made with
        settings.seed = world.settings.seed
        settings.level_type = world.settings.level_type
        service.update_settings(name, settings, actor)
        return done(request, f"/worlds/{name}", "Settings saved")

    @app.post("/worlds/{name}/whitelist")
    async def world_whitelist(request: Request, name: str, actor: Actor) -> Response:
        form = await request.form()
        players = whitelist_from_form(form)
        service.set_whitelist(name, players, actor)
        return done(request, f"/worlds/{name}", f"Whitelist: {len(players)} players")

    @app.post("/worlds/{name}/rcon", response_class=HTMLResponse)
    def world_rcon(
        request: Request,
        name: str,
        actor: Actor,
        command: Annotated[str, Form()] = "",
    ) -> Response:
        command = command.strip()
        if not command:
            raise MCDashError("Enter a command")
        output = service.rcon(name, command.removeprefix("/"), actor)
        return render(request, "_rcon_entry.html", command=command, output=output)

    @app.get("/worlds/{name}/logs")
    async def world_logs(request: Request, name: str) -> StreamingResponse:
        service.get_world(name)  # 404 before we start streaming
        return StreamingResponse(
            _log_events(request, name),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _log_events(request: Request, name: str) -> AsyncIterator[str]:
        """Pump the blocking ``stream_logs`` iterator from a daemon thread."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=1000)
        stop = threading.Event()

        def put(item: tuple[str, str]) -> None:
            if not queue.full():
                queue.put_nowait(item)

        def post(item: tuple[str, str]) -> None:
            with contextlib.suppress(RuntimeError):  # event loop already closed
                loop.call_soon_threadsafe(put, item)

        def pump() -> None:
            it = service.stream_logs(name)
            try:
                for line in it:
                    if stop.is_set():
                        break
                    if not line:  # backend heartbeat: lets us notice `stop`
                        continue
                    post(("line", line))
                post(("end", ""))
            except Exception as exc:  # surfaced to the browser, not fatal
                post(("error", str(exc)))
            finally:
                close = getattr(it, "close", None)
                if close is not None:
                    with contextlib.suppress(Exception):
                        close()

        threading.Thread(target=pump, daemon=True, name=f"logs-{name}").start()
        try:
            while True:
                try:
                    kind, data = await asyncio.wait_for(
                        queue.get(), timeout=SSE_KEEPALIVE_S
                    )
                except TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"
                    continue
                if kind == "line":
                    yield sse_event(data)
                else:
                    yield sse_event(data, event=kind)
                    return
        finally:
            stop.set()

    # -- backups -----------------------------------------------------------
    @app.get("/backups", response_class=HTMLResponse)
    def backups_page(request: Request, sort: str = "world", dir: str = "") -> Response:
        if sort not in BACKUP_SORTS:
            sort = "world"
        if dir not in ("asc", "desc"):
            dir = default_dir(sort)
        backups = service.list_backups()
        if sort == "world":
            groups = sort_backups(backups)
            if dir == "desc":
                groups.reverse()
            return render(
                request, "backups.html", grouped=True, groups=groups, sort=sort, dir=dir
            )
        return render(
            request,
            "backups.html",
            grouped=False,
            items=sort_backups_by(backups, sort, dir),
            sort=sort,
            dir=dir,
        )

    @app.post("/backups/label")
    def backup_label(
        request: Request,
        backup_id: Annotated[str, Form()],
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "/backups",
    ) -> Response:
        service.set_backup_label(backup_id, label.strip(), description.strip())
        return done(request, _local(back), "Label saved")

    @app.post("/backups/promote")
    def backup_promote(
        request: Request,
        actor: Actor,
        backup_id: Annotated[str, Form()],
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "/backups",
    ) -> Response:
        if not label.strip():
            raise MCDashError("Give the promoted backup a name")
        b = service.promote(backup_id, label.strip(), description.strip(), actor)
        return done(request, _local(back), f"Promoted to backup {b.label or b.id}")

    @app.post("/backups/delete")
    def backup_delete(
        request: Request,
        actor: Actor,
        backup_id: Annotated[str, Form()],
        confirm: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "/backups",
    ) -> Response:
        b = service.get_backup(backup_id)
        if b.tier is Tier.ARCHIVE:
            raise Forbidden("Archive backups are read-only and cannot be deleted")
        check_confirm(confirm, b.world_name)
        service.delete_backup(backup_id, actor)
        return done(request, _local(back), f"Deleted {backup_id}")

    # -- launch ------------------------------------------------------------
    @app.get("/launch", response_class=HTMLResponse)
    def launch_page(request: Request, backup: str) -> Response:
        b = service.get_backup(backup)
        name, settings = service.launch_defaults(backup)
        return render(
            request,
            "launch.html",
            backup=b,
            name=name,
            settings=settings,
            roster=service.roster(),
        )

    @app.post("/launch")
    async def launch(request: Request, actor: Actor) -> Response:
        form = await request.form()
        backup_id = str(form.get("backup_id") or "")
        name = str(form.get("name") or "").strip()
        if not name:
            raise MCDashError("A world name is required")
        settings = settings_from_form(form, whitelist_from_form(form))
        world: World = service.launch(backup_id, name, settings, actor)
        return done(request, f"/worlds/{world.name}", f"Launching {world.name}")

    # -- new world (no backup) ----------------------------------------------
    @app.get("/new", response_class=HTMLResponse)
    def new_world_page(request: Request) -> Response:
        name, settings = service.new_world_defaults()
        return render(
            request,
            "launch.html",
            backup=None,
            name=name,
            settings=settings,
            roster=service.roster(),
        )

    @app.post("/new")
    async def new_world(request: Request, actor: Actor) -> Response:
        form = await request.form()
        name = str(form.get("name") or "").strip()
        if not name:
            raise MCDashError("A world name is required")
        settings = settings_from_form(form, whitelist_from_form(form))
        world: World = service.create_world(name, settings, actor)
        return done(request, f"/worlds/{world.name}", f"Creating {world.name}")

    # -- roster ------------------------------------------------------------
    @app.get("/roster", response_class=HTMLResponse)
    def roster_page(request: Request) -> Response:
        return render(request, "roster.html", players=service.roster())

    @app.post("/roster/add")
    def roster_add(
        request: Request,
        name: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
    ) -> Response:
        (pname,) = parse_players([name], "") or [""]
        if not pname:
            raise MCDashError("Enter a player name")
        players = service.roster()
        for i, p in enumerate(players):
            if p.name.lower() == pname.lower():
                players[i] = Player(p.name, note.strip())
                break
        else:
            players.append(Player(pname, note.strip()))
        service.set_roster(players)
        return done(request, "/roster", f"Saved {pname}")

    @app.post("/roster/remove")
    def roster_remove(request: Request, name: Annotated[str, Form()]) -> Response:
        players = service.roster()
        kept = [p for p in players if p.name != name]
        if len(kept) == len(players):
            raise NotFound(f"{name} is not on the roster")
        service.set_roster(kept)
        return done(request, "/roster", f"Removed {name}")

    return app


def _local(url: str) -> str:
    """Only allow same-site relative redirects."""
    return url if url.startswith("/") and not url.startswith("//") else "/"
