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
| `store.py` | backend | JSON state in `$MCD_STORE_DIR/dashboard/` (roster, labels, index cache, audit log, world registry) |
| `k8s.py`, `helmops.py`, `rcon.py` | backend | Kubernetes client, `helm` CLI wrapper, RCON protocol client and per-world connection pool (`RconPool`, see design.md "RCON connections") |
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
| `MCD_PORT_MIN` / `MCD_PORT_MAX` | `30565` / `30584` | game NodePort range, inclusive |
| `MCD_RCON_PORT_MIN` / `MCD_RCON_PORT_MAX` | `30600` / `30619` | RCON NodePort range, inclusive; must not overlap the game range (startup fails if it does) |
| `MCD_RCON_HOST` | "" | LAN address shown for RCON; "" = the world pod's `status.hostIP` (unknown while stopped) |
| `MCD_PUBLIC_HOST` | `localhost` | host shown in connect addresses |
| `MCD_VERSION` | chart `image.tag` or `appVersion` | release shown in the page header; "" = `dev` |
| `MCD_WORLD_NODE` | "" | if set, `nodeSelector kubernetes.io/hostname` for worlds and Jobs |
| `MCD_STORAGE_CLASS` | `nfs-rwx` | storage class for world PVCs |
| `MCD_WORLD_SIZE` | `10Gi` | world PVC size |
| `MCD_DEFAULT_MEMORY` | `4Gi` | launch form default |
| `MCD_ITZG_CHART` | `/opt/charts/minecraft` | vendored itzg chart directory baked into the image |
| `MCD_SNAPSHOT_KEEP` | `10` | snapshots kept per world |
| `MCD_VOLUMES_DIR` | "" | read-only share root of `$MCD_STORAGE_CLASS`, which keeps a PVC in `<ns>/<pvc>` and archives a deleted one as `archived-<ns>/<pvc>` (chart: `storage.volumes`, at `/volumes`). See World share. "" = no checks |
| `MCD_RESTORE_WORLDS` | `false` | at start, re-create registered worlds the cluster has lost (see World registry); only with a storage class that maps a PVC name to a fixed folder |
| `MCD_UID` / `MCD_GID` | `1000` / `1000` | itzg runtime uid/gid; Jobs run as this and chown to it |

## Kubernetes objects per world `<w>`

- PVC `<w>-data` — created by the dashboard (not the chart), storage class
  `$MCD_STORAGE_CLASS`, RWX. Labels `app.kubernetes.io/managed-by: mcdash`,
  `mcdash/world: <w>`.
- ConfigMap `<w>-mcdash` (label `mcdash/kind: world`) — the world record
  (`spec`: port, rconPort, source, settings; `op`: pending multi-step
  operation — `snapshot`, `restore`, `delete`, `launch`, `create`). Exists
  from launch, before any Helm release, so it reserves the name and both ports
  while seeding. A record without `rconPort` (world launched before RCON
  NodePorts) gets one, saved at once, on its next install with the server
  running (start, end of a restore, settings save while running; stop and the
  restore drain never allocate). With the RCON range full that install fails
  with `Conflict`; after a restore the op stays at `step: seed`, so Start
  re-runs the seed and fails the same way until a world is deleted (or the
  range widened) to free a port. Rebuilt from the release's `mcdash:` values
  if lost. `create_world` records op `{kind: create, step: install}` until the
  release is installed, so an interrupted create is finished by the
  reconciler.
- Secret `<w>-rcon` — key `rcon-password`, random.
- Registry file `$MCD_STORE_DIR/dashboard/registry/<w>.json` — see World
  registry below. `registry/skipped/<w>.json` once a restore set it aside.
- Helm release `<w>` of the vendored itzg chart: `persistence.dataDir.existingClaim=<w>-data`,
  NodePort game service on the allocated port, RCON service
  (`minecraftServer.rcon`) `serviceType: NodePort`, `nodePort: <rconPort>`,
  `externalTrafficPolicy: Local` (ClusterIP only if rendered without a port),
  nodeSelector,
  `ENFORCE_WHITELIST` (false, with no `WHITELIST`, for an `open_to_all` world),
  `settings.ops` → `minecraftServer.ops` (env `OPS`, comma-separated) with
  `EXISTING_OPS_FILE=SYNCHRONIZE` when non-empty, else no `OPS` and
  `EXISTING_OPS_FILE=SKIP` (the world keeps its `ops.json`),
  `online-mode` true, `EULA=TRUE`. Dashboard metadata lives in
  the release values under the top-level key `mcdash:` (`port`, `rconPort`,
  `sourceBackup`, `created`, `settings`) — read back with `helm get values`. Helm releases are
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
  Server tunables (design.md, "Server tunables"): `settings.view_distance`,
  `spawn_protection`, `max_players` → `minecraftServer.viewDistance`,
  `spawnProtection`, `maxPlayers` (ints); `settings.hardcore` →
  `minecraftServer.hardcore: true` only when true; `simulation_distance`,
  `pause_when_empty_seconds`, `sync_chunk_writes`, `allow_flight`,
  `broadcast_rcon_to_ops`, `broadcast_console_to_ops` → `extraEnv`
  `SIMULATION_DISTANCE`, `PAUSE_WHEN_EMPTY_SECONDS`, `SYNC_CHUNK_WRITES`,
  `ALLOW_FLIGHT`, `BROADCAST_RCON_TO_OPS`, `BROADCAST_CONSOLE_TO_OPS`
  (strings, booleans as `true`/`false`). A `None` setting is not rendered
  (itzg leaves the property alone); records without these fields load as
  `None`. `simulation_distance` < 1.18, `pause_when_empty_seconds` < 1.21.2
  and `sync_chunk_writes` < 1.16 are not rendered. `settings.use_aikar_flags`
  (default true, also for old records) → `extraEnv.USE_AIKAR_FLAGS=true`;
  nothing when false. `settings.forge_version` (FORGE only; e.g. `36.2.34`,
  matching `models.FORGE_VERSION_RE`, else the service raises `MCDashError`)
  overrides the build detected from the backup (the record's `forgeVersion`,
  mirrored as `mcdash.forgeVersion`) in `minecraftServer.forgeVersion`; blank
  = the detected build, neither = itzg's recommended build. The service
  stores only an override (a value equal to the detected build is saved as
  `""`, non-Forge worlds save `""`), `World.settings.forge_version` shows the
  build in use and `launch_defaults` prefills the detected one. Old records
  load as `""`. A restore re-detects `forgeVersion` from the restored backup
  but keeps an override, as it keeps the Minecraft version. Ranges
  (`models.SETTING_RANGES`, checked by the service and the form): view 2–32, simulation 5–32, spawn protection 0–100000, pause
  0–86400 (0 = never), max players 1–10000.
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
  volumes: {nfs: {server: "", path: ""}}   # optional: share root of worlds.storageClass,
           # RO PV + PVC at /volumes (MCD_VOLUMES_DIR); half-set nfs fails
worlds:
  portMin: 30565
  portMax: 30584
  rconPortMin: 30600  # RCON NodePorts: LAN only; never relay/forward them
  rconPortMax: 30619
  rconHost: ""        # "" = the world pod's node IP
  publicHost: ""
  node: ""            # hostname to pin worlds to
  storageClass: nfs-rwx
  size: 10Gi
  defaultMemory: 4Gi
  snapshotKeep: 10
  restore: false      # MCD_RESTORE_WORLDS
securityContext: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
networkPolicy:
  enabled: true
  rconAllowedCidrs: [192.168.1.0/24]
```

The chart creates: Deployment (1 replica, port 8080, `/healthz`), Service
(ClusterIP 80→8080), ServiceAccount + Role + RoleBinding, the two PV/PVC
pairs and, with `networkPolicy.enabled`, NetworkPolicy `<fullname>-worlds`.
It selects pods with `app.kubernetes.io/name: minecraft` **and** an
`mcdash/world` label (world servers only: not the dashboard, whose name label
is `minecraft-dashboard`, nor the Jobs, which have no name label), Ingress
only: TCP 25565 from anywhere; TCP 25575 from this release's dashboard pod
(selector labels) and `rconAllowedCidrs` (each a CIDR string or an ipBlock
`{cidr, except}`); everything else denied. Rendering
fails if the RCON and game port ranges overlap. It does **not** create an Ingress (tpi-k3s-ansible owns ingress/auth).

## World registry

The ConfigMaps and releases are in etcd, so a cluster rebuild loses them. The
reconciler therefore copies each world to
`$MCD_STORE_DIR/dashboard/registry/<w>.json` =
`{spec, op, replicas, storageClass}` (`spec`/`op` as in the ConfigMap;
`replicas` from the release, kept while there is none; `storageClass` the
`storageClassName` of PVC `<w>-data`, kept while there is none). It writes a
file only when it changes, and deletes it as the first step of a delete's
teardown, so a deleted world never comes back. An unreadable registry folder
is logged once (until it is readable again) and retried on the next tick. A
single file that cannot be read (e.g. EACCES) is left out, or cannot be
written is skipped, with one warning per file per dashboard process; the
others are still copied, and that world is retried every tick. If the
teardown cannot delete the file, the delete fails (FAILED, Delete retries)
before anything is torn down.

With `MCD_RESTORE_WORLDS`, restore runs **once per dashboard start**: after
the first tick that lists the worlds and reads the registry successfully (an
unreadable registry is retried, never taken for an empty one). It reads
`registry/*.json` afresh at that point, and re-creates each registered world
that has neither a ConfigMap nor a release. The world share lookups (see
World share) run first, without the service lock; then, under the lock, the
worlds are listed again, and a world that is back by then is left alone. If
the world share is mounted but has no `<ns>/` folder (e.g. `storage.volumes`
is not the class's share root, or the NAS has not mounted its dataset yet),
the pass is **postponed**: one error is logged, nothing is touched or set
aside, and it is retried every tick until the folder is there (no restart
needed). For each world:

1. Secret `<w>-rcon` with a **new** password (passwords are never written to
   the store), and PVC `<w>-data`.
2. The release, with the recorded ports and settings and `replicas` (0 or 1).
   If the install fails, the record gets op `{kind: recover, step: install,
   error}` (FAILED; Start retries).
3. The ConfigMap, with `message` "restored after the cluster lost it" until
   the next start or stop.

An op that was in flight is kept and resumes (seed, drain, install, a
restore's or delete's snapshot, teardown), except a plain `snapshot` (or an
old `stop`), which is dropped; an op that had failed is dropped too, unless
it failed at `seed` or `install`, which Start retries. The PVC binds to the
old data only if `$MCD_STORAGE_CLASS` gives each PVC name a fixed folder.

A record that cannot be restored as it is goes to
`registry/skipped/<w>.json`, with one warning in the log and a
`skip_restore` entry (with the reason) in the audit log; it is never
restored again. The reasons, in the order checked: a record that is not a
valid world; "storage class changed (`<old>` → `<new>`): migrate its data
first" (its `storageClass` is not `$MCD_STORAGE_CLASS`; a record without one
is restored); a port held by another world (restored earlier in name order,
or still in the cluster); no data folder `<ns>/<w>-data` on the world share
(only checked with `MCD_VOLUMES_DIR`). To bring one back, free its ports (or
move its data to the new class and set `storageClass` in the file), move the
file back to `registry/`, and restart the dashboard. Registry files that are
not JSON or not a record (`spec` an object, `op` an object or null,
`replicas` an integer, `storageClass` a string if present) are ignored with
a warning. Any other error while restoring one world is logged, and that
world is retried at the next start; the others are still restored. Such a
world may be left with its PVC and Secret but no record; they are not
removed (deleting the PVC can archive or delete its data), and the retry
reuses them.

The ports of a registered world that the cluster does not have (restore
pending, failed, or `MCD_RESTORE_WORLDS` off) stay reserved: new worlds and
RCON port assignments skip them. Records in `registry/skipped/` hold no
ports.

A world removed outside the dashboard (ConfigMap and release deleted by hand)
comes back at the next start. Delete worlds in the dashboard, or delete
`registry/<w>.json` before the dashboard next starts.

A registered world's name stays taken while it has a file in `registry/` or
`registry/skipped/`: `launch` and `create_world` refuse it (`Conflict`
"taken by a world the cluster no longer has (…)"), and name suggestions skip
it. Delete the file (and the old world's data) to free the name. A name whose
PVC `<w>-data` exists without a world record is refused with how to free it
(delete the registry file, if any, then the PVC and Secret `<w>-rcon`).
These "taken" checks (a world, its PVC, the registry) run before the world
share checks, so a live world's name gets "is taken", not the deleted-world
message.

## World share

With `storage.volumes` set to the share root of `$MCD_STORAGE_CLASS`
(mounted read-only at `/volumes`, `MCD_VOLUMES_DIR`), the dashboard expects
the NFS CSI layout `subDir: ${pvc.metadata.namespace}/${pvc.metadata.name}`
with `onDelete: archive`, which csi-driver-nfs archives to
`archived-` + subDir:

```text
/volumes/<ns>/<w>-data             a world's live folder
/volumes/archived-<ns>/<w>-data    the folder of a deleted world
```

- `launch` and `create_world` refuse a name while either folder exists. The
  live folder of a deleted world stays until the driver archives it (the
  teardown does not wait), or for good if that archive fails because an
  archive of the same name exists: `Conflict` "… still has a volume folder
  …; wait a minute …". An archived one: `Conflict` "a deleted world named …
  still has archived data …; pick another name, or remove or rename the
  archive first". Either way the new PVC would get the old folder, or its
  delete would get stuck. The checks run before the service lock is taken.
- Name suggestions skip names with either folder (`new-world-2`).
- A restore needs the live folder: without it the world would come back
  empty, so the record is set aside (see World registry).
- A folder under the share that does not exist yet means none; one that
  cannot be read refuses the name. The share root itself must exist, or the
  pod cannot start (the storage class needs it anyway).
- `<ns>/` may be missing only while there is no world at all (the class
  makes it with the first PVC). Missing while a world exists in the cluster
  or the registry, the mount is taken to be wrong or not ready: new names
  and name suggestions are refused (`MCDashError` "the world share … has no
  `<ns>/` folder although worlds exist: …") and a restore is postponed,
  rather than every lookup reading "none".

Give that class a share of its own, used only by worlds: the dashboard can
read everything under it.

## New worlds (`create_world`)

`new_world_defaults()` suggests a free name (`new-world`, `new-world-2`, …)
and `WorldSettings(memory=$MCD_DEFAULT_MEMORY)` (LATEST, VANILLA, survival,
normal world type, random seed) with the server tunables set to
`service.PROPERTY_DEFAULTS` (vanilla defaults, but `sync_chunk_writes`
false). `launch_defaults` takes them from the backup's `server.properties`
where present (integers clamped into range), except `sync_chunk_writes`,
which is always false; version-gated ones the version lacks stay `None`. `create_world(name, settings, actor)` is
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

## Direct RCON (`World.rcon_port`, `rcon_password`)

`World.rcon_port` is the RCON NodePort (0 = assigned at the next start),
`World.rcon_host` the LAN address to show (`$MCD_RCON_HOST`, else the server
pod's `status.hostIP`, else `""`), `World.node` the pod's node (else
`$MCD_WORLD_NODE`). The world page shows `<rcon_host>:<rcon_port>` and
`127.0.0.1:<rcon_port>` (on `node`). `rcon_password(name, actor)` returns the
world's RCON password and writes audit action `rcon_password` (never the
password). Served by `GET /worlds/<w>/rcon-password` as an HTML fragment with
`Cache-Control: no-store`; the password is never part of any page until then.

## Runtime notes

- `DashboardService.stream_logs` may yield `""` as an idle heartbeat (at least
  every 30 s); consumers skip it and use it to notice an abandoned viewer.
- Without a reachable cluster `mcdash serve` still starts: `/healthz` and the
  backup pages work, cluster-backed pages show the error.
- The itzg release gets `startupProbe` (up to 15 min) so a first start that
  upgrades an old world is not killed by the liveness probe.
- The itzg release sets the pod's `terminationGracePeriodSeconds` to 90 (via
  `extraPodSpec`; the chart has no typed value), above itzg's 60 s
  `STOP_DURATION`, so a stop's SIGTERM is not followed by SIGKILL before the
  server has saved and exited.
- Stop is not an op: it runs RCON `save-all flush`, then upgrades the release
  to `replicaCount` 0 at once; it takes no snapshot. Start is not an op either
  (it upgrades to `replicaCount` 1), except that on a world whose seed failed
  it retries the `launch` op's seed Job. While a scaled-to-0 world's server
  pod still exists (shutting down; finished pods such as an Evicted one in
  phase Failed don't count), start is refused with `Conflict`, so two servers
  never share the PVC. An in-flight `{kind: stop, step: snapshot}` op written
  by an older dashboard is still finished (snapshot, then scale to 0).
