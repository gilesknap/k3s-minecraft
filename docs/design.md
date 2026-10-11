# Minecraft Dashboard — Design

Status: agreed design, 2026-10-11. Not yet implemented.

## Goal

A web dashboard in the K3s cluster (deployed by
[tpi-k3s-ansible](https://github.com/gilesknap/tpi-k3s-ansible)) that lets an
admin pick a world backup, launch a Minecraft server from it, monitor running
servers, stop/start them while keeping their data, and save their state back
into the list of backups. Servers use the
[itzg minecraft chart](https://github.com/itzg/minecraft-server-charts).
Players connect with ordinary Minecraft clients via `mc.gkcluster.org:<port>`.

The original backups — especially the worlds described in
`MinecraftHistoryNoahAndDad.pdf` — are precious and must never be writable by
anything in the cluster.

## Repository split

| Repo | Owns |
|------|------|
| `k3s-minecraft` (this repo) | Dashboard app (Python, FastAPI + HTMX, server-rendered), Dockerfile, its Helm chart, CI publishing multi-arch image + OCI chart to `ghcr.io/gilesknap/…`, `scripts/ingest-archive.sh`. Old content (`giles-servers/`, `k8s-mc.sh`, `useful/`) moves to `legacy/`. |
| `tpi-k3s-ansible` | Cluster wiring only: `enable_minecraft` toggle + ArgoCD Application, `minecraft` namespace, dashboard ServiceAccount + namespace-scoped Role, static NFS PVs, oauth2-proxy Ingress + tunnel route, DDNS CronJob + sealed Cloudflare token, docs (how-to + NAS setup section). |

## Storage tiers (NAS `192.168.1.3`)

| Tier | NAS path (client side) | Written by | Pruned | Deletable in UI |
|------|------------------------|------------|--------|-----------------|
| **Archive** (precious) | `/bigdisk/minecraft-archive/` | nobody — `a-w` on the NAS **and** `readOnly` mount | never | no |
| **Backups** (promoted) | `/bigdisk/k8s-cluster/minecraft/backups/` | "Promote" of a snapshot | never | yes, typed-name confirm |
| **Snapshots** (rolling) | `/bigdisk/k8s-cluster/minecraft/snapshots/<world>/` | manual / on stop / before delete / before restore | keep last 10 per world | yes |
| Dashboard state | `/bigdisk/k8s-cluster/minecraft/dashboard/` | dashboard | — | — |
| Live world data | dynamic `nfs-rwx` PVC per world (`/k8s-dynamic`) | the server | — | via world delete |

- The archive sits **outside** `k8s-cluster/` so no cluster-wide tooling ever
  targets it. Graduating a promoted backup into the archive is a deliberate
  manual step on the NAS.
- Promoted backups carry a sidecar `<name>.json` with a name and description.
- Dashboard state (JSON): player roster, per-backup display labels and
  descriptions, backup index cache. No database.
- Because `nfs-rwx` has `reclaimPolicy: Delete`, deleting a world always takes
  a snapshot first.

## Archive ingestion

The Google Drive download is a set of split wrapper zips
(`minecraft-k8s-backup-*-NNN.zip`) each containing inner per-world zips. The
inner zips hold a full `/data` under a `tmp/tmp.XXXX/` prefix.

- `scripts/ingest-archive.sh <drive-zips…> <dest>` extracts the inner zips
  **flat, keeping original filenames**, verifies each (`unzip -t` + a
  `world/level.dat` entry), and is idempotent (re-run to add files later).
- Run on the NAS (or a host with `bigdisk` mounted), then lock the directory:
  `chmod -R a-w`. Documented in tpi-k3s-ansible `docs/how-to/nas-setup.md`.
- **All** versions of every world are ingested — older copies may contain
  builds lost from newer ones (e.g. Emerald City in Animal World).
- Filenames are never changed. The dashboard parses `<date>-<world>.zip`
  leniently (formats seen: `2021-05-15-21_56_15`, `2022-02-04+21.14.00`,
  `2022-08-15_22_56_49`, `22-01-21.21.50.00`, `(1)` duplicate suffixes),
  falling back to file mtime.

Known gap: Drive part `-002` is missing, and four PDF worlds have no backup
yet (`noah-dad-original-survival`, `mansion-world`, `millie`, `animal-world`;
some lived under `MinecraftBackups/noah_single_player_best_2021_05/`).
To be recovered from Drive and ingested later.

## Runtime ownership

- ArgoCD deploys only the dashboard and its wiring.
- The dashboard runs `helm` against the itzg chart in the `minecraft`
  namespace, with a ServiceAccount whose Role is limited to that namespace.
  Helm release secrets are the source of truth for which worlds exist; port and
  world metadata are stored as release labels/values.

## Worlds

- A **world** is a named instance: Helm release + PVC + port + RCON Secret.
  Name defaults to the world name parsed from the backup; must be unique and
  DNS-safe. Launching a backup whose name is taken suggests `<name>-2`, so
  side-by-side copies are allowed.
- **Launch** list: every Archive and Backup zip is a row, sorted by world then
  newest first, with a heading per world. Snapshots are listed under their
  world and are launchable too.
- **Stop/Start**: `replicas` 0↔1 via helm values. Stop snapshots first. PVC
  and port are kept.
- **Restore from…** (on an existing world): snapshot current state → stop →
  wipe + reseed PVC → start. Keeps port, whitelist, and settings.
- **Delete**: snapshot → uninstall release → delete PVC and Secret, freeing
  the port.
- No idle auto-stop and no auto-wake; the admin manages active worlds.

## Seeding (backup → PVC)

A seed **Job** created by the dashboard mounts the archive/backups read-only
and the new PVC read-write, locates `world/level.dat` to strip the
`tmp/tmp.XXXX/` prefix, extracts, and chowns to the itzg UID. The Helm release
is installed only after the Job succeeds (world state: seeding → starting →
running).

Detection, from the zip without full extraction:

- **Forge/modded**: `mods/` dir, `forge-*.jar`, or
  `libraries/net/minecraftforge`.
- **Original version**: `level.dat` → `Data.Version.Name`, falling back to
  `versions/<x>`.

| | Vanilla | Forge / modded |
|---|---|---|
| Server version | `LATEST` (players run latest client) — overridable | original detected version, `TYPE=FORGE` — overridable |
| Copied | `world/` + `ops.json`, `whitelist.json`, `banned-*.json` | full `/data` minus `libraries/`, `versions/`, `logs/`, `.cache`, `?/` |
| Java image | default | itzg image tag matching the old version's Java |

Upgrading a vanilla world to `LATEST` is one-way for that live copy only; the
source zip is untouched.

The launch form is **pre-filled from the backup's `server.properties`**:
gamemode, force-gamemode, difficulty, pvp, enable-command-block (needed by
adventure maps such as `adventure-of-doom`, `devils-deep`), motd,
max-world-size. These are passed as itzg env vars rather than copying the old
properties file blind. Memory defaults to 4 Gi, editable.

## Snapshots

RCON `save-off` + `save-all flush` → Job zips the PVC (excluding regenerable
dirs) to `snapshots/<world>/<UTC-timestamp>.zip` in the same layout the seeder
accepts → RCON `save-on`. Triggers: "Snapshot now", stop, delete, restore.
Retention: last 10 per world. **Promote** copies a snapshot into Backups with
a name and description.

## Networking

- Game port: NodePort Service from the fixed range **30565–30584** (20
  slots), lowest free slot allocated at world creation and kept for the
  world's life. `externalTrafficPolicy: Local` to preserve player IPs.
- Router forwards TCP 30565–30584 → nuc2 (`192.168.1.92`).
- All worlds are pinned to **nuc2** (`nodeSelector: kubernetes.io/hostname`).
- RCON: ClusterIP only, per-world random password in a Secret.
- `mc.gkcluster.org`: DNS-only (grey-cloud) Cloudflare A record, kept current
  by an in-cluster DDNS CronJob (dynamic WAN IP) using a scoped Cloudflare API
  token stored as a SealedSecret.

## Player access and security

- `online-mode=true`, `EULA=true` (set by the dashboard; using it accepts the
  Mojang EULA).
- **Whitelist enforced** (`ENFORCE_WHITELIST=true`). The dashboard keeps a
  **player roster**; each world's whitelist is chosen from the roster plus
  free-text. Edits apply live via RCON `whitelist add/remove` and persist into
  the release values (`WHITELIST` env) so they survive restart.
- Ops come from the backup.

## Dashboard access

`minecraft.gkcluster.org` via the Cloudflare tunnel, gated by CF Access and
**oauth2-proxy admin-only** (as Headlamp). The app does no auth itself; it
logs `X-Auth-Request-Email` against each action. No public status page.

## Monitoring (v1)

- World list: state, port, version/type, online players (RCON `list`),
  uptime, CPU/memory (metrics-server), last snapshot time.
- World page: streamed log tail (SSE), RCON console, whitelist editor,
  settings, snapshots (launch / promote / delete).
- Deferred: Grafana via the chart's `mc-monitor` exporter.

## Rollout

1. **NAS (manual)**: create `minecraft-archive/` and
   `k8s-cluster/minecraft/{backups,snapshots,dashboard}`, ingest, lock the
   archive read-only. Router forward 30565–30584 → nuc2.
2. **tpi-k3s-ansible** (worktree branch `minecraft`): wiring as above;
   `just check`; `just switch-branch minecraft` to test live before merge.
3. **k3s-minecraft**: skeleton app + CI (image + chart) first, then features
   in order: list/launch/seed → stop/start → snapshot/promote/restore →
   whitelist/roster → log tail + console.
4. **Proving**: first a disposable non-PDF world (`king-ghidorah` or
   `skorponok`) end to end — seed, connect from a real client via
   `mc.gkcluster.org`, stop, snapshot, restore, delete. Then a Forge world
   (`fire-ice2`). Only then a precious world. A test asserts the archive
   mount rejects writes.
