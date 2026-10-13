# Contracts between components

Read with `docs/design.md`. These are the interfaces that the parallel work
streams share. `src/mcdash/models.py` is the Python contract and is
**read-only** for implementers.

## Python package layout (`src/mcdash/`)

| Module | Owner | Purpose |
|--------|-------|---------|
| `models.py` | contract (read-only) | dataclasses, errors, `DashboardService` Protocol |
| `config.py` | backend | `Config.from_env()` — env vars below |
| `backups.py`, `detect.py`, `naming.py` | backend | backup indexing, filename parsing, vanilla/Forge + version detection, server.properties parse |
| `store.py` | backend | JSON state in `$MCD_STORE_DIR/dashboard/` (roster, labels, index cache, audit log) |
| `k8s.py`, `helmops.py`, `rcon.py` | backend | Kubernetes client, `helm` CLI wrapper, RCON protocol client |
| `service.py` | backend | `KubeDashboardService` implementing `DashboardService` + reconciler thread |
| `completion.py` | backend | console completion: Brigadier tree walker over the generator reports, and the commands Job script |
| `jobs/seed.py`, `jobs/snapshot.py`, `jobs/__main__.py` | backend | code run inside Job pods (`python -m mcdash.jobs ...`) |
| `cli.py` | backend | `mcdash serve` (uvicorn on `:8080`, `create_app`), `mcdash index <dir>` (print detected backups — local debugging) |
| `web/` (`app.py` with `create_app(service)`, templates, static) | web | FastAPI + HTMX + Jinja2 UI |

Only the backend owner edits `pyproject.toml`; other streams list needed deps
in their report.

## Environment (dashboard container)

| Var | Default | Meaning |
|-----|---------|---------|
| `MCD_NAMESPACE` | downward API | namespace for worlds (same as the dashboard's) |
| `MCD_ARCHIVE_DIR` | `/archive` | archive tier mount (read-only) |
| `MCD_STORE_DIR` | `/store` | writable tree: `backups/`, `snapshots/<world>/`, `dashboard/` |
| `MCD_ARCHIVE_PVC` | — | PVC name of the archive, mounted read-only by seed Jobs at `/archive` |
| `MCD_STORE_PVC` | — | PVC name of the store, mounted by Jobs at `/store` |
| `MCD_JOB_IMAGE` | — | image for seed/snapshot Jobs (the dashboard image itself) |
| `MCD_PORT_MIN` / `MCD_PORT_MAX` | `30565` / `30584` | NodePort range, inclusive |
| `MCD_PUBLIC_HOST` | `localhost` | host shown in connect addresses |
| `MCD_WORLD_NODE` | "" | if set, `nodeSelector kubernetes.io/hostname` for worlds and Jobs |
| `MCD_STORAGE_CLASS` | `nfs-rwx` | storage class for world PVCs |
| `MCD_WORLD_SIZE` | `10Gi` | world PVC size |
| `MCD_DEFAULT_MEMORY` | `4Gi` | launch form default |
| `MCD_ITZG_CHART` | `/opt/charts/minecraft` | vendored itzg chart directory baked into the image |
| `MCD_SNAPSHOT_KEEP` | `10` | snapshots kept per world |
| `MCD_UID` / `MCD_GID` | `1000` / `1000` | itzg runtime uid/gid; Jobs run as this and chown to it |

## Kubernetes objects per world `<w>`

- PVC `<w>-data` — created by the dashboard (not the chart), storage class
  `$MCD_STORAGE_CLASS`, RWX. Labels `app.kubernetes.io/managed-by: mcdash`,
  `mcdash/world: <w>`.
- ConfigMap `<w>-mcdash` (label `mcdash/kind: world`) — the world record
  (`spec`: port, source, settings; `op`: pending multi-step operation). Exists
  from launch, before any Helm release, so it reserves the name and port while
  seeding; rebuilt from the release's `mcdash:` values if lost. `create_world`
  records op `{kind: create, step: install}` until the release is installed,
  so an interrupted create is finished by the reconciler.
- Secret `<w>-rcon` — key `rcon-password`, random.
- Helm release `<w>` of the vendored itzg chart: `persistence.dataDir.existingClaim=<w>-data`,
  NodePort game service on the allocated port, RCON ClusterIP, nodeSelector,
  `ENFORCE_WHITELIST` (false, with no `WHITELIST`, for an `open_to_all` world),
  `online-mode` true, `EULA=TRUE`. Dashboard metadata lives in
  the release values under the top-level key `mcdash:` (`port`, `sourceBackup`,
  `created`, `settings`) — read back with `helm get values`. Helm releases are
  the source of truth for which worlds exist. `settings.seed` →
  `minecraftServer.levelSeed` (env `SEED`) when non-empty; `settings.level_type`
  → `minecraftServer.levelType` (env `LEVEL_TYPE`, written verbatim to
  `level-type`): `flat|amplified|large_biomes` become
  `minecraft:flat|minecraft:amplified|minecraft:large_biomes` for 1.19+,
  `LATEST` and unparseable versions, and `FLAT|AMPLIFIED|LARGEBIOMES` for
  older versions; `normal` sets nothing (server default). Both only take effect
  when the server generates a world; on a seeded volume they are inert.
  `settings.gamemode == adventure` → `extraEnv.RCON_CMDS_STARTUP` =
  `forceload add ~ ~` (keeps the spawn chunk loaded; 26.3 adventure respawn
  bug, see design.md), except for versions that parse as older than 1.14.
  Other game modes do not set `RCON_CMDS_STARTUP`.
- Jobs `<w>-seed-<ts>` / `<w>-snap-<ts>` (no seed Job for `create_world`) — image `$MCD_JOB_IMAGE`, run as
  `$MCD_UID`, mount `<w>-data` at `/data`, archive PVC read-only at `/archive`,
  store PVC at `/store`, labels `mcdash/world`, `mcdash/job: seed|snapshot`,
  `ttlSecondsAfterFinished: 3600`.

- Job `<w>-cmds-<ts>` (label `mcdash/job: commands`, `backoffLimit: 0`) — the
  console's command list. Image: the world pod's server image (fallback
  `itzg/minecraft-server`), `sh -c mcdash.completion.REPORT_SCRIPT`, env
  `MCD_WORLD`, `MCD_POD`. Mounts `<w>-data` **read-only** at `/data` and the
  store PVC with `subPath: dashboard/commands` at `/out`; no archive. Writes
  `/out/<version>.commands.json`, `/out/<version>.registries.json` (only if
  missing) and `/out/worlds/<w>.json` = `{"pod", "version", "error"}`. Started
  by `complete()` at most once per server pod.

Job CLI (run in the Job container):

```
python -m mcdash.jobs seed --src /archive/<file>.zip|/store/<...>.zip --dest /data --mode vanilla|full
python -m mcdash.jobs snapshot --src /data --dest /store/snapshots/<w>/<YYYYmmddTHHMMSSZ>.zip
```

Both subcommands refuse a `--dest` inside the archive (`/archive` or
`$MCD_ARCHIVE_DIR`, symlinks resolved) before touching the disk; the read-only
mount is the second line of defence.

`seed` locates `world/level.dat` (any prefix, e.g. `tmp/tmp.XXXX/`), strips the
prefix, extracts `world/` + `ops.json`, `whitelist.json`, `banned-*.json`
(vanilla) or everything except `libraries/ versions/ logs/ .cache ?/` (full),
wipes `/data` first. `snapshot` writes a zip whose root is the `/data` layout
(no prefix) — the seeder accepts both. Writes go to a temp name then rename.

## RBAC needed by the dashboard ServiceAccount (namespace-scoped Role)

- `""`: secrets, services, persistentvolumeclaims, configmaps, pods, pods/log, events, serviceaccounts — full CRUD where helm needs it
- `apps`: deployments, statefulsets — full
- `batch`: jobs — full
- `metrics.k8s.io`: pods — get, list
- `policy`: poddisruptionbudgets; `networking.k8s.io`: ingresses — as the itzg chart may render them

## Dashboard Helm chart (`charts/minecraft-dashboard`, published to `oci://ghcr.io/gilesknap/charts/minecraft-dashboard`)

Key values consumed by tpi-k3s-ansible:

```yaml
image: {repository: ghcr.io/gilesknap/k3s-minecraft, tag: ""}  # tag defaults to appVersion
storage:
  archive: {nfs: {server: "", path: ""}}   # chart creates RO PV (mountOptions ro) + PVC
  store:   {nfs: {server: "", path: ""}}   # chart creates RW PV + PVC
worlds:
  portMin: 30565
  portMax: 30584
  publicHost: ""
  node: ""            # hostname to pin worlds to
  storageClass: nfs-rwx
  size: 10Gi
  defaultMemory: 4Gi
  snapshotKeep: 10
securityContext: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
```

The chart creates: Deployment (1 replica, port 8080, `/healthz`), Service
(ClusterIP 80→8080), ServiceAccount + Role + RoleBinding, the two PV/PVC
pairs. It does **not** create an Ingress (tpi-k3s-ansible owns ingress/auth).

## New worlds (`create_world`)

`new_world_defaults()` suggests a free name (`new-world`, `new-world-2`, …)
and `WorldSettings(memory=$MCD_DEFAULT_MEMORY)` (LATEST, VANILLA, survival,
normal world type, random seed). `create_world(name, settings, actor)` is
`launch` without a backup: same name/port checks, ConfigMap, Secret and PVC,
then the release is installed at once with `replicaCount: 1` — no seed Job,
so the world goes straight to `starting` → `running` while itzg generates it
on the empty volume. `World.source_backup_id` is `""` (UI shows "new world");
a later restore sets it to the restored backup. A failed install leaves the
world `failed` and "Start" retries it. Snapshot, stop/start, restore and
delete are unchanged.

## Console completion (`complete`)

`complete(name, line) -> Completion(start, items, status, message)`: suggestions
for the token of `line` that starts at `start`; each `Suggestion(text, kind,
detail)` has kind `literal`, `value` or `hint` (`<targets>`: shown, not
inserted). `status` is `ready`, `pending` (commands Job running; ask again) or
`none` (no command list: the console still has history). Served as JSON by
`GET /worlds/<w>/complete?line=…`. It sends nothing over RCON beyond the cached
`list` used for player names, and writes no audit entry.

## Runtime notes

- `DashboardService.stream_logs` may yield `""` as an idle heartbeat (at least
  every 30 s); consumers skip it and use it to notice an abandoned viewer.
- Without a reachable cluster `mcdash serve` still starts: `/healthz` and the
  backup pages work, cluster-backed pages show the error.
- The itzg release gets `startupProbe` (up to 15 min) so a first start that
  upgrades an old world is not killed by the liveness probe.
