# k3s-minecraft

A web dashboard for running Minecraft Java servers on a home Kubernetes
cluster from a library of old world backups. You pick a backup, launch a
server from it, watch it run, stop and start it while keeping its data, and
snapshot its state back into the backup list.

Each world is a release of the [itzg minecraft chart](https://github.com/itzg/minecraft-server-charts)
(running [itzg/docker-minecraft-server](https://github.com/itzg/docker-minecraft-server)).
Players connect with an ordinary Minecraft client at `<public-host>:<port>`.

The worlds this was built for, and the story behind them, are in
[MinecraftHistoryNoahAndDad.pdf](MinecraftHistoryNoahAndDad.pdf).

<!-- Screenshots: worlds list, launch form, world page (TODO) -->

## Features

- **Launch** any archived, promoted or snapshot zip as a new world. The
  dashboard detects vanilla or Forge and the original Minecraft version, and
  pre-fills the launch form from the backup's `server.properties`.
- **Stop/Start** a world while keeping its data volume and port. Stopping a
  world takes a snapshot first.
- **Snapshots**: take one at any time; one is also taken on stop, on delete
  and before a restore. The last 10 per world are kept. **Promote** a snapshot
  to a named backup that is never pruned.
- **Restore from** any backup into an existing world, keeping its port and
  settings.
- **Monitoring**: state, players online, uptime, CPU and memory, a streamed
  log tail and an RCON console.
- **Whitelist** editing from a player roster, enforced by default. A world can
  instead be marked **open to everyone** (no whitelist), which applies live.
  `online-mode` is always on.
- **Adventure worlds** keep their spawn chunk force-loaded, working around a
  Minecraft 26.x bug that respawns players at the bottom of the world when the
  spawn chunk is not loaded.

## Architecture

The full design is in [docs/design.md](docs/design.md) and the interfaces
between components in [docs/contracts.md](docs/contracts.md). In brief:

- The Python app (FastAPI with HTMX, rendered on the server) runs as one pod.
  It drives `helm` against a copy of the itzg chart baked into the image. The
  Helm releases are the source of truth for which worlds exist. There is no
  database: other state is JSON on the store volume.
- **Storage tiers** on an NFS server:
  - **archive**: the precious original backups. The NAS directory is
    `a-w`, the PersistentVolume is mounted with `ro` mount options, and the
    pod mounts it `readOnly`. Nothing in the cluster can write to it.
  - **store**: promoted backups, rolling snapshots and the dashboard's own
    state.
  - **worlds**: one dynamic RWX PVC per world.
- **Seeding and snapshots** run as Kubernetes Jobs that use the dashboard's
  own image (`python -m mcdash.jobs seed|snapshot ...`).
- **Networking**: each world gets a NodePort from a fixed range (default
  30565-30584) that it keeps for its whole life. The dashboard has a
  ClusterIP Service only and does no authentication itself. Put it behind an
  authenticating proxy; it logs `X-Auth-Request-Email` against each action.

## Deploying

The image and chart are published to GHCR:

- image: `ghcr.io/gilesknap/k3s-minecraft:<version>` (amd64 + arm64)
- chart: `oci://ghcr.io/gilesknap/charts/minecraft-dashboard`

[tpi-k3s-ansible](https://github.com/gilesknap/tpi-k3s-ansible) deploys the
chart through ArgoCD, together with the namespace, the oauth2-proxy Ingress,
the Cloudflare tunnel route and the DDNS CronJob for the game hostname. To
install it by hand:

```bash
helm install minecraft-dashboard oci://ghcr.io/gilesknap/charts/minecraft-dashboard \
  --namespace minecraft --create-namespace -f my-values.yaml
```

```yaml
# my-values.yaml
storage:
  archive: {nfs: {server: 192.168.1.3, path: /bigdisk/minecraft-archive}}
  store:   {nfs: {server: 192.168.1.3, path: /bigdisk/k8s-cluster/minecraft}}
worlds:
  publicHost: mc.example.org   # shown in connect addresses
  node: nuc2                   # pin worlds to the node your router forwards to
  storageClass: nfs-rwx        # RWX class for per-world PVCs
  portMin: 30565
  portMax: 30584
  defaultMemory: 4Gi
  snapshotKeep: 10
```

The chart creates a Deployment, a Service (port 80), a ServiceAccount with a
Role limited to its namespace, and two static NFS PV/PVC pairs. The PVs have
`Retain` reclaim and their names are prefixed with the namespace. It does
**not** create an Ingress. For each storage tier, `storage.<tier>.existingClaim`
uses a PVC you created yourself. `storage.<tier>.hostPath` exists only for
testing (kind). See
[charts/minecraft-dashboard/values.yaml](charts/minecraft-dashboard/values.yaml)
for every value.

Your router must forward TCP `portMin`-`portMax` to the pinned node.

## NAS setup and ingesting the archive

1. On the NAS, create `minecraft-archive/` **outside** any directory the
   cluster's dynamic provisioner uses. Also create the store directory with
   `backups/`, `snapshots/` and `dashboard/` inside it, writable by uid 1000.
2. Download the Google Drive backup folder. Drive splits it into wrapper zips
   (`minecraft-k8s-backup-*-NNN.zip`), each holding per-world zips.
3. Unpack them flat into the archive and lock it:

   ```bash
   chmod u+w /share/bigdisk/minecraft-archive           # only if already locked
   scripts/ingest-archive.sh /share/bigdisk/minecraft-archive \
       ~/Downloads/minecraft-k8s-backup-*.zip
   chmod -R a-w /share/bigdisk/minecraft-archive
   ```

`ingest-archive.sh <dest-dir> <drive-zip>...` extracts every
`minecraft-k8s-backup/*.zip` member into `<dest-dir>` with its original
filename and timestamp. It checks each one with `unzip -t` and requires a
`world/level.dat` entry before moving it into place. The script is
idempotent: files already present with the same size are skipped, so you can
re-run it when more parts turn up. It never overwrites an existing file: a
size mismatch is reported as a conflict. It needs only `bash` and `unzip`
(the BusyBox build is fine, as on QNAP), plus standard coreutils or BusyBox
tools. Inner zips over 4 GiB need Info-ZIP `unzip` for Zip64.

## Developing

```bash
uv sync                               # create .venv with dev dependencies
uv run ruff check
uv run pytest                         # unit + web tests (e2e excluded)
uv run python -m mcdash.web.devserver # UI on http://localhost:8080 with fake data
```

`mcdash index <dir>` prints what the dashboard detects in a directory of
backup zips.

The end-to-end test (`tests/e2e/`, pytest mark `e2e`) needs a cluster. CI
runs it on kind: it builds the image, installs the chart with hostPath
storage, writes a synthetic world zip (`tests/e2e/make_world_zip.py`) and
goes through launch, snapshot, stop, start and delete. It also checks that
the archive mount rejects writes. See the `e2e` job in
[.github/workflows/ci.yml](.github/workflows/ci.yml).

## Releasing

- **Tag `X.Y.Z`** (pure semver, no `v` prefix) publishes image `X.Y.Z`, `X.Y` and
  `latest` (not for prereleases such as `1.0.0-rc.1`), and chart
  `X.Y.Z` with appVersion `X.Y.Z`.
- **Push to `main`** publishes image `main` and `sha-<sha7>`, and the
  prerelease chart `0.0.0-main.<sha7>`. Because it is a prerelease, nothing
  picks it up unless you pin that exact version (useful for testing `main`
  from tpi-k3s-ansible).

The Dockerfile pins the `helm` binary (v3), the itzg chart version, uv and
the Python base image. Renovate keeps them current (`renovate.json`).

## Legacy

The original version of this repo, with hand-written per-world Helm values,
the `k8s-mc.sh` helpers and Raspberry Pi cluster notes, is in
[legacy/](legacy/README.md).

## License

Apache 2.0. See [LICENSE](LICENSE).
