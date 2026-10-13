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
| **Snapshots** (rolling) | `/bigdisk/k8s-cluster/minecraft/snapshots/<world>/` | manual / before delete / before restore | keep last 10 per world | yes |
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

- `scripts/ingest-archive.sh <dest> <drive-zips…>` extracts the inner zips
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
- **New world**: a world with no backup — name (suggested `new-world`), seed
  (blank = random), world type (normal / superflat / amplified / large
  biomes), version (default `LATEST`) and the usual settings. No seed Job: the
  release is installed straight onto the empty PVC and itzg generates the
  world (state: starting → running). Snapshot it to keep it; its snapshots are
  launchable and promotable like any other. Useful for disposable test worlds
  before any precious backup is copied in.
- **Stop/Start**: `replicas` 0↔1 via helm values. PVC and port are kept.
  Stop flushes the world (`save-all flush`) and then scales to 0; it takes
  no snapshot: the data stays on the PVC, so one would protect nothing,
  slow stop/restart, and push manual snapshots out of the rolling 10. Use
  "Snapshot now" first if wanted. The pod gets a 90s termination grace
  period (above itzg's 60s `STOP_DURATION`) to finish its own stop save.
  Start is refused until the old pod is gone (shown as "stopping"), so two
  servers never share the PVC.
- **Restore from…** (on an existing world): snapshot current state → stop →
  wipe + reseed PVC → start. Keeps port, whitelist, and settings.
- **Delete**: snapshot → uninstall release → delete PVC and Secret, freeing
  the port.
- No idle auto-stop and no auto-wake; the admin manages active worlds.

## Seeding (backup → PVC)

(Not used for a new world — see Worlds.) A seed **Job** created by the
dashboard mounts the archive/backups read-only
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

### Server tunables

The launch, new-world and world Settings forms also manage these
`server.properties` keys and one JVM option. Like the settings above they are
rendered into the release at install time and take effect at the next start.

| Setting | Property | Rendered as | Launch / new-world default |
|---|---|---|---|
| View distance (2–32) | `view-distance` | chart `minecraftServer.viewDistance` | backup's value, else 10 |
| Simulation distance (5–32, 1.18+) | `simulation-distance` | env `SIMULATION_DISTANCE` | backup's value, else 10 |
| Spawn protection (≥ 0; 0 = off) | `spawn-protection` | chart `minecraftServer.spawnProtection` | backup's value, else 16 |
| Pause when empty (s, ≥ 0; 0 = never, 1.21.2+) | `pause-when-empty-seconds` | env `PAUSE_WHEN_EMPTY_SECONDS` | backup's value, else 60 |
| Synchronous chunk writes (1.16+) | `sync-chunk-writes` | env `SYNC_CHUNK_WRITES` | **off** (backup's value ignored) |
| Allow flight | `allow-flight` | env `ALLOW_FLIGHT` | backup's value, else off |
| Max players (≥ 1) | `max-players` | chart `minecraftServer.maxPlayers` | backup's value, else 20 |
| Show RCON / console output to admins | `broadcast-rcon-to-ops`, `broadcast-console-to-ops` | env `BROADCAST_RCON_TO_OPS`, `BROADCAST_CONSOLE_TO_OPS` | backup's value, else on |
| Hardcore (new worlds only) | `hardcore` | chart `minecraftServer.hardcore`, only when on | off |
| Aikar's JVM flags | — | env `USE_AIKAR_FLAGS=true`, only when on | **on** |

- **Spawn protection**: non-admins cannot build or break within this many
  blocks of the world spawn, which surprises people in build worlds. 0 turns
  it off. Vanilla also disables it when the server has no ops.
- **Pause when empty**: since 1.21.2 the server stops ticking this many
  seconds after the last player leaves, so farms and redstone freeze. That
  saves CPU, but RCON-driven builds (e.g. mciwb) with nobody online then work
  against a paused world whose chunks are not ticking. Use 0 for such worlds:
  vanilla only pauses when the value is greater than 0.
- **Synchronous chunk writes**: worlds live on NFS (`nfs-rwx`), where
  synchronous writes make every chunk save wait for the disk. Off is much
  cheaper; the risk is losing the last few seconds of changes if the server
  crashes hard. Stop flushes the world (`save-all flush`) and then scales
  to 0, and snapshots run `save-all flush` first, so they are unaffected. The dashboard therefore
  defaults it off for every launch and new world, whatever the backup said.
- **Hardcore** only matters when the server generates the world, so it is on
  the New world form only, like seed and world type. It is not rendered when
  off, so a backup's `hardcore=true` is never overwritten.
- **Aikar's flags** are itzg's G1 GC tuning (`G1HeapRegionSize=8M`,
  `MaxGCPauseMillis=200`, `AlwaysPreTouch`, …; the >= 12 GiB variant is picked
  automatically). Every flag still exists in Java 25, and the 4–6 GiB heaps
  used here are the case the flags were written for. `AlwaysPreTouch` commits
  the whole heap at start, but itzg already sets `-Xms` = `-Xmx`, and the pod
  limit is the heap plus 1 GiB, so the steady-state footprint does not change.

Gates: `simulation-distance` (1.18), `pause-when-empty-seconds` (1.21.2) and
`sync-chunk-writes` (1.16) are not rendered for versions that parse as older,
e.g. old Forge worlds. `LATEST` and unparseable versions count as new.
Integer chart values stay below 10^6, where helm would render its float64
values in e-notation (the `MAX_WORLD_SIZE` problem).

**Older world records** (saved before these settings existed) load with every
property setting unset (`None`): nothing is rendered, and itzg leaves the
world's `server.properties` as it is, so these worlds keep today's behaviour,
including `sync-chunk-writes=true`. Their Settings form shows blank numbers
and "unchanged/on/off" selects, which stay unset unless changed. The
exception is Aikar's flags, which old records get as on: they only tune the
JVM, cannot affect world data, and take effect at the next start.

### Adventure worlds: spawn chunk force-load

On Minecraft 26.3 (and presumably later 26.x releases), when a player with no personal respawn point respawns on a
server whose **default game type** is adventure, `PlayerSpawnFinder.findSpawn`
takes a shortcut that reads the spawn height without loading the spawn chunk.
If nobody is near spawn the chunk is unloaded, the column looks empty, and the
player is placed at the world floor (Y=-63) and suffocates, often repeatedly.
Other game types load the chunk first and are unaffected; this includes
survival or creative worlds that switch individual players to adventure,
because the check is on the world's default, not the player's mode.

On a dedicated server the default game type is server.properties `gamemode`,
which the server writes over level.dat `GameType` at every start. The
dashboard sets `gamemode` from the world settings, so it knows the trigger
without reading level.dat. When the world's gamemode is `adventure` the release
gets `RCON_CMDS_STARTUP=forceload add ~ ~`: itzg runs it through RCON once
the server is listening, on every start. RCON commands run at the overworld
world spawn, so `~ ~` is the spawn chunk and the dashboard never needs the
spawn coordinates. Re-adding a forced chunk does nothing, forced chunks
persist in the world save, and one loaded chunk costs very little.

It is skipped for versions before 1.14, which have no `forceload` command
(e.g. old Forge worlds). `LATEST`, snapshots and other ids that do not parse
as `x.y[.z]` count as new. Moving the world spawn later (`setworldspawn`)
leaves the old chunk forced as well; remove it with `forceload remove` if it
matters. The command is not retried if it fails (itzg logs the failure); it
runs again on the next start, and a chunk forced once stays forced in the save.
A saved spawn that sits inside solid blocks is a separate problem and
needs a manual `setworldspawn`.

## Snapshots

RCON `save-off` + `save-all flush` → Job zips the PVC (excluding regenerable
dirs) to `snapshots/<world>/<UTC-timestamp>.zip` in the same layout the seeder
accepts → RCON `save-on`. Triggers: "Snapshot now", delete, restore (the
destructive ones). Stop does not snapshot; a `stop` op persisted by an older
dashboard (snapshot → scale 0) is still finished by the reconciler.
Retention: last 10 per world. **Promote** copies a snapshot into Backups with
a name and description.

## Networking

- Game port: NodePort Service from the fixed range **30565–30584** (20
  slots), lowest free slot allocated at world creation and kept for the
  world's life. `externalTrafficPolicy: Local` to preserve player IPs.
- Router forwards TCP 30565–30584 → nuc2 (`192.168.1.92`).
- All worlds are pinned to **nuc2** (`nodeSelector: kubernetes.io/hostname`).
- RCON: per-world random password in a Secret; see
  [Direct RCON](#direct-rcon-nodeport-and-networkpolicy).
- `mc.gkcluster.org`: DNS-only (grey-cloud) Cloudflare A record, kept current
  by an in-cluster DDNS CronJob (dynamic WAN IP) using a scoped Cloudflare API
  token stored as a SealedSecret.

### Direct RCON: NodePort and NetworkPolicy

Tools such as [mciwb](https://github.com/gilesknap/mciwb) send one RCON
command per block change, so round-trip time decides how fast a build runs.
`kubectl port-forward` costs ~44 ms per command (API-server stream plus a
delayed-ACK stall); a NodePort on the world's node is ~0.04 ms from that node
and a few tenths of a millisecond from the LAN.

- **Port**: each world gets an RCON NodePort from a second fixed range,
  **30600–30619** by default (`worlds.rconPortMin/rconPortMax`), allocated
  like the game port: lowest free at launch, kept for the world's life,
  stored as `rconPort` in the world record and the release's `mcdash:`
  values. Worlds that predate it get one at their next install with the server
  running (start, end of a restore, settings save while running); stop and
  the restore drain never allocate. The itzg
  chart's own RCON Service is switched to `NodePort` with that `nodePort` and
  `externalTrafficPolicy: Local` (supported by itzg 5.2.0's `rcon-svc.yaml`;
  no extra Service needed).
- **Never relay it.** The ranges must not overlap (the dashboard refuses to
  start and the chart refuses to render if they do) because the game range is
  what the router and the VPS relay forward to the internet. The RCON range
  must never be added to a router forward or to tpi-k3s-ansible's
  `vps_relay.forwards`.
- **Addresses**: the world page shows `<node-ip>:<rcon-port>` (the pod's
  `status.hostIP`, or `worlds.rconHost`) and `127.0.0.1:<rcon-port>` on the
  world's node. The latter works because kube-proxy serves NodePorts on
  localhost (k3s default; `route_localnet=1`). With `Local`, only the node
  running the world answers; worlds are pinned to one node anyway.
- **Password**: not in the page HTML. A **Reveal** button fetches it with
  `GET /worlds/<w>/rcon-password` (`no-store`), which is audited.
- **NetworkPolicy** (dashboard chart, `networkPolicy.enabled`, default on):
  selects world server pods (`app.kubernetes.io/name: minecraft` plus an
  `mcdash/world` label, which excludes the dashboard and its Jobs), ingress
  only. TCP 25565 from anywhere (players arrive through NodePort and the
  relay); TCP 25575 from the dashboard pod (podSelector) and
  `networkPolicy.rconAllowedCidrs` (default `192.168.1.0/24`); everything else
  is denied. Egress is not restricted, so the seed/snapshot/commands Jobs and
  the server's own downloads are unaffected.

What the world pod sees as the source address, verified on k3s (flannel,
embedded kube-router policy controller, iptables kube-proxy):

| Client | Path | Source seen by the policy |
|---|---|---|
| Dashboard pod (any node) | `<w>-rcon` ClusterIP, pod→pod over flannel (not masqueraded) | the dashboard's pod IP → `podSelector` |
| LAN host → `<node-ip>:<rcon-port>` | `externalTrafficPolicy: Local` → no SNAT | the client's LAN IP → `ipBlock` |
| Process on the world's node → `127.0.0.1:<rcon-port>` (or its own node IP) | kube-proxy marks LOCAL-source traffic for masquerade; the pod finally sees the node's `cni0` address (e.g. `10.42.5.1`) | allowed **before** any policy: kube-router's per-pod chain accepts `--src-type LOCAL` ("permit traffic to pods when source is the pod's local node"), evaluated in OUTPUT before POSTROUTING SNAT |
| Pod in another namespace → `<w>-rcon` ClusterIP or pod IP, or a pod on the world's node → `<node-ip>:<rcon-port>` | not masqueraded | its pod IP → denied |
| Pod on **another** node → `<world-node-ip>:<rcon-port>` | flannel masquerades pod → non-pod traffic to the sending node's IP | that node's LAN IP → **allowed** by a plain LAN `ipBlock` |
| Host-network process on another LAN node | — | that node's LAN IP → allowed |

So no extra rule is needed for the node-local path on k3s. The last two rows
are the gap a plain LAN CIDR leaves: list the other cluster nodes' IPs under
the entry's `except` (`rconAllowedCidrs: [{cidr: 192.168.1.0/24, except:
[192.168.1.81/32, …]}]`), leaving out any node you run RCON clients on (e.g. a
workstation that is also a node, at the cost of that node's pods). Another policy
engine (Calico, Cilium) may not exempt node-local traffic: add the node's
pod-CIDR gateway (e.g. `10.42.5.1/32`) to `rconAllowedCidrs` there. LAN hosts
on another subnet (e.g. a second NIC on the node) need their CIDR added too.
Trusting the node also means **host-network pods on the world's node** (e.g.
node-exporter, csi-nfs-node) can always reach RCON; the policy cannot narrow
that, so it is a property of the node, not of the dashboard.

## Player access and security

- `online-mode=true`, `EULA=true` (set by the dashboard; using it accepts the
  Mojang EULA).
- **Whitelist enforced** (`ENFORCE_WHITELIST=true`) by default. The dashboard
  keeps a **player roster**; each world's whitelist is chosen from the roster
  plus free-text. Edits apply live via RCON `whitelist add/remove` and persist
  into the release values (`WHITELIST` env) so they survive restart.
- **Open to everyone** (`open_to_all`, per world): no whitelist, so any genuine
  Minecraft account that knows the address can join. Applied live with RCON
  `whitelist off`/`on`. An open world's release sets `ENABLE_WHITELIST` and
  `ENFORCE_WHITELIST` false and omits `WHITELIST` (itzg turns the whitelist on
  whenever it is set). The names stay in the settings, and closing the world
  re-adds all of them via RCON before `whitelist on`, so they are restored
  even if the world started open. Changing the mode while the server is still
  starting is refused (the pod would come up with the old mode). Worlds are flagged "open to everyone" in
  the UI.
- **Admins** (`ops`, per world): ticked per player next to the whitelist.
  On a whitelisted world they must be on the whitelist, and taking someone
  off the whitelist removes their admin; an open world accepts any valid
  name. Applied live with RCON `op`/`deop` and persisted into the release
  values (`OPS` env, `EXISTING_OPS_FILE=SYNCHRONIZE`) so `ops.json` matches
  the list at each start. An empty list leaves `ops.json` alone
  (`EXISTING_OPS_FILE=SKIP`), so a world restored from a backup keeps the
  backup's ops until admins are chosen.

## Dashboard access

`minecraft.gkcluster.org` via the Cloudflare tunnel, gated by CF Access and
**oauth2-proxy admin-only** (as Headlamp). The app does no auth itself; it
logs `X-Auth-Request-Email` against each action. No public status page.

## Monitoring (v1)

- World list: state, port, version/type, online players (RCON `list`),
  uptime, CPU/memory (metrics-server), last snapshot time.
- World page: streamed log tail (SSE), RCON console, direct-RCON
  addresses with a reveal-password button, whitelist editor,
  settings, snapshots (launch / promote / delete).

### RCON console

- **History** is client-side: the last 100 successfully sent commands per
  world in `localStorage` (`mcdash.rcon.<world>`, no consecutive duplicates).
  Up/Down walk it and keep the half-typed draft; Escape closes the suggestions
  or clears the input.
- **Completion**: RCON has no completion request, so suggestions come from the
  vanilla data generator's reports (`--reports`): `commands.json` (the whole
  Brigadier tree) and `registries.json` (item, block, entity, effect ... ids).
  When the console is first used on a running vanilla world whose current pod
  has no record yet, the dashboard starts a **commands Job** in the world's own
  image (it has Java; the dashboard image does not) with the world volume
  mounted read-only and only `dashboard/commands/` of the store writable. The
  Job reads the resolved version from itzg's `/data/.install-vanilla.env`,
  runs the generator against the jar already on the volume (once per version;
  ~6 s and under 512 MiB heap for 26.3) and records
  `worlds/<world>.json = {pod, version, error}`. A new pod (restart, upgrade
  of `LATEST`) is checked again; a version already generated costs no Java.
  No exec into world pods, so no extra RBAC.
- The dashboard walks the tree server-side (`GET /worlds/<w>/complete?line=`):
  literals first, as Brigadier does; redirects (`tp` → `teleport`,
  `execute … run`) followed; arguments matched by parser (numbers, coordinates
  of 2–3 tokens, selectors and player names). Values come from online players
  (the cached `list`), fixed enums (gamemode, bool, dimension, colours ...) and
  static registries; other arguments show a `<name>` hint. Greedy or bracketed
  arguments (messages, NBT, JSON text) end completion.
- Forge worlds and versions before 1.13 get no Job: history only, or the
  vanilla tree of the same version if another world already produced it.
- Completion never sends anything over RCON except the existing cached `list`,
  and nothing is audited; sending a command is unchanged.
- Deferred: Grafana via the chart's `mc-monitor` exporter.

### RCON connections

Vanilla Minecraft logs every RCON connect and disconnect at INFO, with no
setting to silence it, and the worlds page polls `list` every few seconds. So
the dashboard keeps **one authenticated RCON connection per world**
(`RconPool` in `rcon.py`) and reuses it for everything: player polling, the
console, live whitelist edits and snapshot `save-off`/`save-on`. A world's
log shows one "RCON Client ... started" line per server start or idle period,
not one per poll.

- **Locking**: one lock per world, held for a whole batch of commands, so
  packets never interleave on a socket. There is no global lock around RCON
  I/O: a hung server (5 s socket timeout) only delays callers for that world.
- **Liveness**: before a reused connection is used, a zero-timeout `select`
  checks it. The server never sends unprompted, so a readable socket means it
  closed (EOF/RST) or the stream is out of step: the connection is replaced
  before anything is sent.
- **Retry rules**: any failed command drops the connection.
  - Connect or authentication failure: raised as before pooling (one
    attempt). An authentication failure also drops the cached password so
    the next call re-reads the Secret.
  - Failure before the command packet was fully written (send error, no
    connection, server says the session is not authenticated): the command
    did not run, so it is retried once on a new connection.
  - Failure after the command packet was written (timeout, EOF, sentinel
    send error): the server may have run it, so the error is surfaced, never
    retried. This covers console input, `whitelist add/remove` and
    `save-off`/`save-on`. Only the read-only `list` poll is retried once.
  - In a batch (`save-off` + `save-all flush`, whitelist edits) a retry
    resumes at the failed command; commands that ran are never re-sent.
- **Eviction**: on any Helm install/upgrade of the world (start, stop,
  restore, settings change), on delete teardown, when the ready server pod
  changes (name, container start time or restart count, checked whenever the
  pods are listed: status polls, console, snapshot, whitelist), and when the
  RCON password changes (it is part of the connection key). A peer that
  vanished without closing the socket looks alive to the liveness check, so
  the console lists the pods before every command. Eviction never waits: if
  a command is in flight the connection is dropped before the next one.
- **Idle timeout**: the reconciler loop (every 3 s) closes connections unused
  for 5 minutes, without waiting on a busy world.
- Server side: vanilla's RCON client thread blocks on read with no idle
  timeout, and the itzg image only sets the RCON port and password. If a
  server does close an idle connection, the liveness check replaces it.

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
