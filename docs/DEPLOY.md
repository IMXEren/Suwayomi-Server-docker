# Deployment runbook

This document describes the Compose-first deployment of the IMXEren Suwayomi fork and
the one-time conversion of the existing `/opt/suwayomi` host from "host rclone + a
systemd wrapper that shells out to `docker compose`" to plain Docker restart policies.

## Topology

```text
Internet
   │
   ▼
cloudflared (host systemd)      ── keeps its own tunnel config and credentials
   │
   ▼
Caddy (host systemd)            ── owns the public TLS endpoint, proxies to 127.0.0.1:4567
   │
   ▼
127.0.0.1:4567  ──►  suwayomi (docker compose, restart: unless-stopped)
                        │  /home/suwayomi/.local/share/Tachidesk  (local: H2 DB, downloads, staging)
                        │  /archive  (bind of the shared host path)
                        ▼
                   /srv/suwayomi/archive  (host path, mount --make-rshared)
                        ▲
                        │  rclone FUSE mount (docker compose, restart: unless-stopped)
                   suwayomi-rclone ──► B2 remote `suwayomi-b2:suwayomi/archive`
                        │
                        └── local VFS cache (named volume `suwayomi_rclone-cache`)
```

Caddy and cloudflared are **not** containerised here. They already work as host
systemd units and the conversion must leave them untouched.

## What runs where

| Component | Where | Restart owner |
| --- | --- | --- |
| Caddy | host systemd | systemd (unchanged) |
| cloudflared | host systemd | systemd (unchanged) |
| rclone FUSE mount | `rclone` compose service | Docker (`restart: unless-stopped`) |
| Suwayomi | `suwayomi` compose service | Docker (`restart: unless-stopped`) |

The old host rclone service and the systemd compose wrapper are retired.

## Prerequisites

- Docker Engine with Compose v2.17+ (needed for `depends_on: { condition: service_healthy, restart: true }`).
- `/dev/fuse` present on the host (the rclone sidecar needs it).
- The compose project lives in `/opt/suwayomi` and the image comes from this repository's
  release workflow (`ghcr.io/imxeren/suwayomi-server`).

## One-time host preparation

1. Create the archive mount directory:

   ```sh
   sudo mkdir -p /srv/suwayomi/archive
   ```

   The FUSE mount made inside the sidecar only propagates back to the host if that host
   path is a *shared* mount. On this host the root filesystem is already shared, so the
   directory inherits it and nothing else is required. Confirm that before deploying:

   ```sh
   findmnt -o TARGET,PROPAGATION / /srv/suwayomi/archive   # expect "shared" for both
   ```

   Only if the path is *not* shared, give the directory its own shared self-bind and
   persist it in `/etc/fstab`:

   ```sh
   sudo mount --bind /srv/suwayomi/archive /srv/suwayomi/archive
   sudo mount --make-rshared /srv/suwayomi/archive
   ```

   ```fstab
   # <src> <target> <type> <options>
   /srv/suwayomi/archive  /srv/suwayomi/archive  none  bind,rshared  0 0
   ```

   Note that the sidecar mounts onto `/data`, which is already a bind mount of this host
   path. rclone therefore needs `--allow-non-empty` (already set in the compose file),
   otherwise it crash-loops with `directory already mounted, use --allow-non-empty`.

2. Create the secrets directory and the two secret files (both are gitignored):

   ```sh
   mkdir -p /opt/suwayomi/secrets

   # Copy the already-tested rclone configuration; do not retype its credentials.
   # The remote name must match RCLONE_REMOTE in .env (default: `suwayomi-b2`).
   sudo cp /home/suwayomi/.config/rclone/rclone.conf /opt/suwayomi/secrets/rclone.conf

   # For a fresh Backblaze S3-compatible remote, generate this file with `rclone config`
   # using provider=Backblaze and the bucket endpoint (for example
   # s3.eu-central-003.backblazeb2.com). Never commit or paste the resulting file.

   # Suwayomi auth password
   printf '%s' '<your-auth-password>' > /opt/suwayomi/secrets/auth_password
   ```

   The sidecar runs as root, so `rclone.conf` may be `root:root 0400`. Suwayomi runs
   as uid 1000 (`suwayomi`), so make the files it reads readable by uid 1000:

   ```sh
   sudo chown root:1000 /opt/suwayomi/secrets/rclone.conf /opt/suwayomi/secrets/auth_password
   sudo chmod 0440 /opt/suwayomi/secrets/rclone.conf /opt/suwayomi/secrets/auth_password
   ```

3. Copy `.env.example` to `.env` and set at least `RCLONE_REMOTE`,
   `ARCHIVE_HOST_PATH=/srv/suwayomi/archive`, `DATA_HOST_PATH=/srv/suwayomi/data` and
   `SUWAYOMI_IMAGE` (for example `ghcr.io/imxeren/suwayomi-server:dev`).

4. Install the boot unit. The stack is started by `suwayomi-stack.service`, which now
   depends only on Docker — the rclone mount is created *inside* the Compose project:

   ```ini
   [Unit]
   Description=Suwayomi archival stack (Docker Compose: rclone sidecar + Suwayomi)
   Requires=docker.service
   After=docker.service network-online.target
   Wants=network-online.target

   [Service]
   Type=oneshot
   RemainAfterExit=yes
   WorkingDirectory=/opt/suwayomi
   ExecStart=/usr/bin/docker compose -f /opt/suwayomi/docker-compose.yml up -d --remove-orphans
   ExecStop=/usr/bin/docker compose -f /opt/suwayomi/docker-compose.yml down
   TimeoutStartSec=600s
   TimeoutStopSec=180s

   [Install]
   WantedBy=multi-user.target
   ```

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable suwayomi-stack.service
   ```

## Conversion from the host-rclone + systemd-wrapper deployment

Do this in a maintenance window. Suwayomi must not be mid-download.

1. Record the current state so it can be restored:

   ```sh
   systemctl status rclone-suwayomi 2>/dev/null || true
   systemctl status suwayomi-stack 2>/dev/null || true
   docker compose ps
   ```

2. Stop the old host rclone service and the systemd compose wrapper so docker becomes
   the only restart authority:

   ```sh
   sudo systemctl disable --now rclone-archive 2>/dev/null || true
   sudo systemctl disable --now suwayomi-stack 2>/dev/null || true
   ```

   Leave Caddy and cloudflared running throughout.

3. If a host rclone still holds the mount, unmount it before the container takes over:

   ```sh
   sudo fusermount -u /srv/suwayomi/archive 2>/dev/null || sudo umount /srv/suwayomi/archive 2>/dev/null || true
   findmnt -o TARGET,PROPAGATION /srv/suwayomi/archive   # no output == not mounted
   ```

4. Bring up the new stack:

   ```sh
   cd /opt/suwayomi
   docker compose pull
   docker compose up -d
   docker compose ps
   ```

5. Verify (see below). Only once the archive is served from the container is the
   conversion complete.

## Verification

### Mount propagation

The FUSE mount made inside the sidecar must be visible on the host:

```sh
# from the host
mount | grep /srv/suwayomi/archive
ls -la /srv/suwayomi/archive
findmnt -o TARGET,PROPAGATION /srv/suwayomi/archive   # expect "shared" / "rshared"
```

If the path is empty, propagation is not shared: re-run `mount --make-rshared` and
confirm the `fstab` entry, then `docker compose restart rclone`.

### Archive wiring inside Suwayomi

```sh
# the app must see the archive at /archive, not under the downloads directory
docker compose exec suwayomi sh -lc 'ls -la /archive | head'

# direct-remote verification must work with the same rclone.conf
docker compose exec suwayomi sh -lc \
  'rclone --config="$RCLONE_CONFIG" lsjson "$ARCHIVE_RCLONE_REMOTE" | head'

# the effective settings must reflect the .env overrides
docker compose exec suwayomi sh -lc \
  'grep -E "archivePath|archiveStagingPath|archiveRcloneRemote|authMode|webUIFlavor|webUIChannel|kcefEnabled|flareSolverr" \
     /home/suwayomi/.local/share/Tachidesk/server.conf'
```

Expect `server.archivePath = "/archive"`, `server.authMode = UI_LOGIN`,
`server.webUIFlavor = Custom`, `server.webUIChannel = BUNDLED`,
`server.flareSolverrEnabled = true` and `server.kcefEnabled = false`.

A permissions mistake here is easy to make and fails at runtime only: the app runs as
uid 1000, so a `root:root 0400` secret is readable by the sidecar but **not** by the app,
and `rclone lsjson` then fails with `permission denied` even though the mount works.

### Bundled WebUI

The `Custom` flavor serves the copy under the data root and never manages it, so the image
syncs that copy from the WebUI.zip inside the jar on every start. A revision change is logged:

```sh
docker compose logs suwayomi | grep "Updating bundled WebUI"
```

This is what makes an image upgrade actually change the served WebUI, and it is also the
revision the app reports as its WebUI version.

### KCEF / WebView

The image bundles KCEF and Xvfb, so the WebView only needs the setting turned on. It requires a
raised `shm_size` (Chromium allocates from `/dev/shm` and Docker's 64 MiB default makes the
renderer crash) and roughly 0.5 GiB of spare RAM.

```sh
docker compose exec suwayomi sh -lc \
  'grep kcefEnabled /home/suwayomi/.local/share/Tachidesk/server.conf'
docker compose logs suwayomi | grep -iE 'xvfb|LD_PRELOAD'
docker stats --no-stream suwayomi
```

Expect `server.kcefEnabled = true`, a `xvfb-run` launch line, and a working WebView. On a host
without swap, watch the memory headroom: Suwayomi plus a WebView and FlareSolverr's own Chromium
can exceed a 4 GiB instance.

### FlareSolverr

FlareSolverr (Byparr) runs by default and is enabled in the app. A root request answers
`301`, so probe the API instead:

```sh
docker compose ps flaresolverr
docker compose exec suwayomi sh -lc 'curl -s -o /dev/null -w "%{http_code}\n" http://flaresolverr:8191/health'
```

### Routing and auth

```sh
curl -fsS http://127.0.0.1:4567/api/v1/settings/about/ | head
# from outside, through Caddy/cloudflared only:
curl -fsS https://<your-host>/api/v1/settings/about/ | head
```

An unauthenticated REST request must answer `401`. GraphQL answers `200` with an
`errors` payload instead — note that a few queries such as `aboutServer` are public by
design, so verify with a library query rather than with `aboutServer`:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<your-host>/api/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ mangas { nodes { id } } }"}'   # expect an Unauthorized error
```

Port 4567 must not be reachable from the public interface (only `127.0.0.1`):

```sh
ss -ltnp | grep 4567
```

## Rollback

The conversion is reversible because nothing about the old deployment is deleted until
you remove it.

1. Stop the compose stack:

   ```sh
   cd /opt/suwayomi
   docker compose down
   ```

2. If a container FUSE mount is still attached, unmount it on the host:

   ```sh
   sudo fusermount -u /srv/suwayomi/archive 2>/dev/null || true
   ```

3. Re-enable the previous units:

   ```sh
   sudo systemctl enable --now rclone-archive
   sudo systemctl enable --now suwayomi-stack
   ```

   Restore the previous compose file from the copy taken during conversion
   (`/opt/suwayomi/docker-compose.local.yml.bak`) before starting the old stack.

4. Verify with the same checks as above. Caddy and cloudflared were never touched.

## Deployment assumptions

- The custom archive settings are read from `server.conf` using the `server.` prefix
  keys (`server.archivePath`, `server.archiveStagingPath`, `server.archiveRcloneRemote`,
  `server.archiveRcloneExecutable`). The image's `startup_script.sh` maps the
  `ARCHIVE_*` environment variables onto those keys. If a future server version ships a
  reference config without those keys, the mapping is a no-op and the values must be set
  through the WebUI instead.
- The rclone sidecar healthcheck contacts the remote; if B2 credentials are wrong the
  sidecar stays unhealthy and Suwayomi does not start (fail-closed on purpose).
- `--allow-other` is safe because the sidecar runs as root.
- `WEB_UI_FLAVOR=Custom` with `WEB_UI_CHANNEL=BUNDLED` and `WEB_UI_UPDATE_INTERVAL=0` is
  required: the archival dashboard only exists in the WebUI bundled into this image, and
  any other combination lets the server replace it with an upstream release.
- The published image name is `ghcr.io/imxeren/suwayomi-server` (`:stable` from `main`,
  `:dev` from `dev`). The package is public, so the host pulls it anonymously.
