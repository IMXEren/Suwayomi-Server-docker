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

1. Create the archive mount directory and add its self-bind to `/etc/fstab`. Do not
   activate that bind while the old host rclone service is still mounted there. The
   self-bind gives the directory a mount of its own; `rshared` lets the FUSE mount made
   inside the sidecar propagate back to the host and into the Suwayomi container:

   ```sh
   sudo mkdir -p /srv/suwayomi/archive
   ```

   Persist the shared self-bind across reboots:

   ```fstab
   # <src> <target> <type> <options>
   # bind-mount the directory onto itself to obtain the required propagation
   /srv/suwayomi/archive  /srv/suwayomi/archive  none  bind,rshared  0 0
   ```

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

3. Copy `.env.example` to `.env` and set at least `RCLONE_REMOTE` and
   `ARCHIVE_HOST_PATH=/srv/suwayomi/archive`.

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
   sudo systemctl disable --now rclone-suwayomi 2>/dev/null || true
   sudo systemctl disable --now suwayomi-stack 2>/dev/null || true
   ```

   Leave Caddy and cloudflared running throughout.

3. If a host rclone still holds the mount, unmount it before the container takes over.
   Then activate the persisted shared self-bind. This ordering matters: making the old
   FUSE mount shared and then unmounting it would lose the propagation setup.

   ```sh
   sudo fusermount -u /srv/suwayomi/archive 2>/dev/null || sudo umount /srv/suwayomi/archive 2>/dev/null || true
   sudo mount --bind /srv/suwayomi/archive /srv/suwayomi/archive
   sudo mount --make-rshared /srv/suwayomi/archive
   findmnt -o TARGET,PROPAGATION /srv/suwayomi/archive
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
docker compose exec suwayomi sh -lc \
  'rclone --config="$RCLONE_CONFIG" lsd suwayomi-b2:'
```

### Routing and auth

```sh
curl -fsS http://127.0.0.1:4567/api/v1/settings/about/ | head
# from outside, through Caddy/cloudflared only:
curl -fsS https://<your-host>/api/v1/settings/about/ | head
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
   sudo systemctl enable --now rclone-suwayomi
   sudo systemctl enable --now suwayomi-stack
   ```

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
