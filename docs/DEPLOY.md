# Deployment runbook

This document describes the Compose-first deployment of the IMXEren Suwayomi fork and
the conversion of the existing `/opt/suwayomi` host from host rclone to a Compose
rclone sidecar. The systemd wrapper still starts the two Compose files at boot.

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

The old host rclone service is retired; `suwayomi-stack.service` still starts the
Compose project at boot.

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

2. Create the secrets directory and the secret files (all are gitignored):

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

   The egress hop needs two more generated files, and Compose refuses to start the hop service
   when its configuration is absent. Author the upstreams from the example, then generate the hop
   configuration *before* the first `up`:

   ```sh
   cp secrets/egress-upstreams.json.example secrets/egress-upstreams.json
   # edit secrets/egress-upstreams.json: the named SOCKS5 upstreams, `fallback` and `preferred`
   chmod 600 secrets/egress-upstreams.json
   sudo python3 scripts/egress-hop-config.py
   ```

   The upstreams are arbitrary named SOCKS5 endpoints (any provider, with or without credentials);
   the optional WARP bridge is just another upstream at `host.docker.internal:40001`.
   `secrets/README.md` gives the exact shape. That command writes `secrets/egress-hop.json`
   (mounted read-only by the hop) and seeds the `egress-rules/` files; `egress-prober` reads the
   authored `secrets/egress-upstreams.json` directly. Changing the upstreams later is not a plain
   re-run: follow "Changing the upstreams config" below.

3. Build Prowl from its source at `v1.2.0-dev.1` or later (the first release with the
   interactive browser API) as `prowl:local` before starting Compose. This template does not
   supply an image; `v1.1.0` lacks the `browser.open`/`cookies.list` commands used here.
   Follow Prowl's Dockerfile for any optional private-font build context. An image already
   built from compatible source can be reused; it is not part of this repository.

   Copy `.env.example` to `.env` and set at least `RCLONE_REMOTE`,
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
   ExecStart=/usr/bin/docker compose -f /opt/suwayomi/docker-compose.yml -f /opt/suwayomi/docker-compose.override.yml up -d --remove-orphans
   ExecStop=/usr/bin/docker compose -f /opt/suwayomi/docker-compose.yml -f /opt/suwayomi/docker-compose.override.yml down
   TimeoutStartSec=600s
   TimeoutStopSec=180s

   [Install]
   WantedBy=multi-user.target
   ```

   ```sh
   sudo systemctl daemon-reload
   sudo systemctl enable suwayomi-stack.service
   ```

   Both files are named because the deployed host keeps its host-specific settings in
   `docker-compose.override.yml` (the locally built jar mount, proxy tweaks). A unit that names
   only the base file drops them on boot, so the override must be present and named too.

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
`server.flareSolverrEnabled = true` and `server.kcefEnabled = true`. The setting stays on, but the
embedded browser is not initialized while `WEB_VIEW_PROVIDER=PROWL_VNC`: the startup script maps
that onto `server.webViewProvider`, and the external provider serves the WebView instead.

A permissions mistake here is easy to make and fails at runtime only: the app runs as
uid 1000, so a `root:root 0400` secret is readable by the sidecar but **not** by the app,
and `rclone lsjson` then fails with `permission denied` even though the mount works.

### Per-host egress (the hop)

The app, the embedded WebView and Prowl all egress through an `egress-hop` gost service, which
picks an upstream per destination host from the named SOCKS5 upstreams in
`secrets/egress-upstreams.json`: the upstream whose rule file in `egress-rules/` lists the host,
and the configured fallback (the optional WARP bridge when it is the fallback) for everything
else. A host in `egress-rules/direct-hosts.txt` leaves from this host itself and traverses no
upstream; that file is policy, edited by hand, and the generator only seeds it when it is missing.

The point is parity. A Cloudflare clearance is bound to the IP that solved it, so the app and the
solver must leave from the same address for a given site. Both are configured from the same two
variables, `SOCKS_PROXY_HOST` and `SOCKS_PROXY_PORT`, so they cannot drift apart:

```sh
# app
SOCKS_PROXY_ENABLED=true SOCKS_PROXY_HOST=egress-hop SOCKS_PROXY_PORT=1080
# prowl
PROWL_PROXY_URL=socks5://egress-hop:1080
```

Credentials live in `secrets/egress-upstreams.json` and the generated hop configuration in
`secrets/egress-hop.json`; neither belongs in this repository or in `.env`.

Assign a host to an upstream by listing it, one per line, in `egress-rules/<upstream>.txt`
(wildcards like `*.example.com` are supported), then run `sudo python3 scripts/egress-hop-config.py`
so the fallback excludes it. That single edit needs only the generator; changing the upstreams
themselves is the staged cutover below. Two details are easy to get wrong and both were measured:

- gost spreads requests across every node whose bypass passes, so the fallback carries a blacklist
  of the union of the other egresses' hosts in `egress-rules/<fallback>-exclude.txt`. Without it,
  a host with its own egress alternates between that egress and the fallback.
- a whitelist with no matchers passes everything, so an egress with no hosts must keep the
  never-matching sentinel the generator writes into its rule file.

Verify a change from inside the app container, since only there do the service names resolve:

```sh
docker compose exec suwayomi sh -lc \
  'curl -s --socks5-hostname egress-hop:1080 https://api.ipify.org; echo'
```

Run it repeatedly: a host that should be pinned must return the same address every time.

The `egress-prober` service keeps those assignments honest. It probes the configured preferred
upstream (the one set as `preferred`, defaulting to the fallback) first for every host, so a host
that can move back onto the free egress does even while a paid upstream still serves it; that move
invalidates the host's IP-bound Cloudflare clearance once. When the preferred upstream cannot serve
a host, the current assignment stays (sticky), and only then are the other upstreams tried. It
classifies a Cloudflare block page as a refusal and a challenge as usable, so an assignment and its
clearance stay put otherwise. Repairs land in the rule files, which the hop hot-reloads.

List the hosts to watch in `egress-rules/egress-watch.txt`. Hosts already assigned in a rule file
are watched whether or not they appear there, so an assignment is never left unverified. That file
is also the place for operator notes: the per-egress rule files are regenerated, so a comment
written into one of them does not survive.

```sh
# what it would do, changing nothing
docker compose exec egress-prober python /app/egress-prober.py --dry-run
# one host only
docker compose exec egress-prober python /app/egress-prober.py --host example.com
```

#### Changing the upstreams config

Changing which upstreams exist, their addresses, or `fallback`/`preferred` is the migration below
without its migrate command. The migration carries the exact commands; the steps that are easy to
get wrong are:

- stop `egress-prober` *before* editing `secrets/egress-upstreams.json`. It reads that file on its
  next hourly run and can rewrite the live `egress-rules/` from a half-edited config. The app and
  Prowl keep running.
- stage the rule files with `egress-hop-config.py --rules-local "$STAGE"`, then compare them and
  `secrets/egress-hop.json` against the backups with the migration's `cmp -s` / `diff -rq` pair.
  Those report only whether files differ, never their contents, so the credentials in
  `egress-hop.json` stay off the terminal.
- copy the accepted rules in, then force-recreate both services so they rebind the new files:
  `docker compose up -d --force-recreate --no-deps egress-hop egress-prober`. A `restart` reuses the
  existing container and keeps it bound to the old file, for the bind-mount reason below.

#### Migrating from the old Decodo env file

An older deployment read `secrets/decodo.env`. The new `secrets/egress-upstreams.json` is read by
`egress-prober` on its next hourly run, so stop that service before the migration writes the file:
otherwise a run mid-migration rewrites the live `egress-rules/` from the half-migrated config.
Only `egress-prober` is stopped; the app and Prowl keep running. The migration itself is optional:
a fresh deployment just authors `secrets/egress-upstreams.json` as above.

```sh
cd /opt/suwayomi
# Stop the prober: it reads secrets/egress-upstreams.json hourly and could rewrite the live
# egress-rules mid-check.
sudo docker compose stop egress-prober
sudo cp secrets/egress-hop.json secrets/egress-hop.json.bak
sudo python3 scripts/migrate-egress-upstreams.py    # writes secrets/egress-upstreams.json
STAGE="$(mktemp -d)" && sudo cp -a egress-rules/. "$STAGE"
sudo python3 scripts/egress-hop-config.py --rules-local "$STAGE"   # writes secrets/egress-hop.json
sudo cmp -s secrets/egress-hop.json.bak secrets/egress-hop.json \
  && echo "egress-hop.json: unchanged" \
  || echo "egress-hop.json: CHANGED - inspect locally before restarting, do not paste it"
sudo diff -rq egress-rules "$STAGE" \
  && echo "egress-rules: unchanged" \
  || echo "egress-rules: CHANGED - inspect before copying them in"
```

`--rules-local` writes the regenerated rule files into the staged copy instead of the live
`egress-rules/`, which the hop hot-reloads within seconds, so the hop never reads the staged files.
`egress-prober` is the other reader of `egress-upstreams.json`, which is why it stays stopped until
the migration is accepted. The migration keeps `decodo-1`, `decodo-2`, ... and `warp` as the
fallback and leaves `decodo.env` in place. `cmp -s` and `diff -rq` print no file contents, because
`egress-hop.json` carries upstream credentials. A `diff -rq` difference can be a header comment
only, which is not a routing change; read it before treating it as one.

When both comparisons are acceptable, put the accepted files in place and recreate the two
services so they rebind the new files:

```sh
cd /opt/suwayomi
sudo cp -a "$STAGE"/. egress-rules/    # skip when egress-rules reported unchanged
sudo docker compose up -d --force-recreate --no-deps egress-hop egress-prober
sudo rm -rf -- "$STAGE"
```

`--force-recreate` is required, not optional. `secrets/egress-hop.json`,
`secrets/egress-upstreams.json` and `scripts/egress-prober.py` are bind-mounted as *files*, and
Docker resolves each to a host inode when the container is created. The migration and the generator
replace those files atomically (`os.replace`), so the new contents are a new inode the existing
container never sees: `restart` and `start` reuse that container and keep it bound to the old file.
Recreating the two services re-resolves every bind mount, and starts the still-stopped prober on the
migrated config in the same step.

### WebView surface over VNC

The headed browser that solves challenges renders to the `display` service. The `vnc` service
attaches x11vnc to that X server over a shared socket and serves the noVNC client with websockify,
which is what will let the app present a real browser instead of the embedded canvas.

Nothing is published to the host. The web client is on the compose network only, at
`http://vnc:6080/vnc.html`, and the raw VNC port stays on loopback inside the container.

```sh
docker compose ps vnc
docker compose logs vnc | tail
# a frame straight off the VNC port, to prove the surface carries the real framebuffer.
# vncsnapshot writes JPEG whatever the file is called, and reads the frame that is on screen
# now, so it only shows something while the browser has a page up.
docker compose exec vnc vncsnapshot -quiet 127.0.0.1:0 /tmp/frame.jpg
docker compose cp vnc:/tmp/frame.jpg ./frame.jpg
```

`xwininfo` on the same display says what is actually on it, which is what makes a frame
meaningful rather than merely non-empty:

```sh
docker compose exec vnc xwininfo -root -tree -display :0
```

### How much a frame costs over the network

The client picks the encoding, not the server. This x11vnc build rejects `-zlib`, `-quality` and
`-compresslevel`, so the app's client page sets the two parameters that matter in its own query
string: `quality` (6 keeps text in the lossless palette mode and allows JPEG for photographic
areas) and `compression` (9 is the strongest zlib level the protocol offers). Both are built in
`getVncClientUrl`, and the defaults they replace are quality 6 and compression 2.

The byte effect of compression 9 through this route was not measured. Doing so needs a client that
negotiates the same Tight encoding the page does, and `vncsnapshot` negotiates its own, so it
cannot stand in for the page here. What is known is the mechanism: the server follows the client,
so these two settings are the only levers on frame size, and a smaller framebuffer is the other one.

To look at the client yourself before the app brokers it, join the network with a temporary
forwarder, because the service publishes nothing on the host:

```sh
docker run --rm -it --network suwayomi_default -p 127.0.0.1:6080:6080 \
  alpine/socat TCP-LISTEN:6080,fork,reuseaddr TCP:vnc:6080
ssh -N -L 6080:127.0.0.1:6080 <vps>     # then open http://localhost:6080/vnc.html
```

The surface is only useful while something is on the display. Prowl closes its tab group when a
fetch finishes, so between fetches the display shows an empty browser window, and a frame taken
then carries the window but not any page. Keeping a tab open for interactive use is the next
step, not something this surface does by itself.

### Challenge solving (Prowl + WARP)

The app talks to a FlareSolverr-compatible endpoint (`FLARESOLVERR_URL=http://prowl:8191`). Prowl
is used instead of FlareSolverr/Byparr because it drives a real fingerprint-patched Chromium
that owns a persistent trust profile and solves a Cloudflare challenge by itself. Its egress is
fixed by `PROWL_PROXY_URL`, so it must match the app's own proxy for a clearance to be valid.

That egress matters: at least one Cloudflare zone answers this host with a hard block page
(`Attention Required!`) while the same URL through Cloudflare WARP only returns a solvable
challenge. The host therefore runs WARP in **proxy** mode, never in full-tunnel mode, so host
traffic (SSH, image pulls, the Cloudflare Tunnel) keeps its normal route:

```sh
sudo warp-cli --accept-tos registration new
sudo warp-cli --accept-tos mode proxy
sudo warp-cli --accept-tos connect
sudo warp-cli --accept-tos status        # Connected
ss -ltnp | grep 40000                    # warp-svc listening on 127.0.0.1:40000
```

WARP's proxy listens on loopback only, so the `warp-bridge` service forwards it onto the host
network where the compose network can reach it:

```sh
docker compose exec prowl sh -lc 'env | grep PROXY_URL'
docker compose exec prowl sh -lc \
  'curl -s --socks5-hostname host.docker.internal:40001 https://api.ipify.org; echo'
```

The second command must print the WARP address, not this host's own address. To check the solver
end to end:

```sh
docker compose exec prowl sh -lc \
  'curl -s -X POST -H "Content-Type: application/json" \
     -d "{\"cmd\":\"request.get\",\"url\":\"https://example.com\",\"maxTimeout\":60000}" \
     http://127.0.0.1:8191/v1 | head -c 200'
```

Rollback: set `FLARESOLVERR_URL=http://flaresolverr:8191` and re-add the previous Byparr service.

### WebView egress

The default `PROWL_VNC` provider opens the browser in Prowl. Both the app's SOCKS setting and
`PROWL_PROXY_URL` point to `egress-hop:1080`; gost chooses the upstream by destination host.
Keep both on the same hop: challenge clearances are bound to the exit address. VNC only carries
the rendered desktop and is not the browser's network proxy.

If `WEB_VIEW_PROVIDER=CEF` is selected instead, embedded Chromium gets its proxy from the
server's SOCKS setting (`--proxy-server`), not from container `HTTP_PROXY` variables. The host
override clears those variables; changing `SUWAYOMI_HTTP_PROXY` alone does not route CEF or the
app's Java HTTP client.

### Bundled WebUI

The image syncs WebUI.zip from the jar into the persistent data directory when the bundled
revision changes. The app copies that directory into `/tmp/Tachidesk/webUI-serve` at startup
and serves the `/tmp` snapshot. After changing the persistent copy, restart the app to serve
it; an uncommitted build with the same commit-count revision will not trigger the jar sync.
A revision change is logged:

```sh
docker compose logs suwayomi | grep "Updating bundled WebUI"
```

This is what makes an image upgrade actually change the served WebUI, and it is also the
revision the app reports as its WebUI version.

### KCEF / WebView

The image still bundles KCEF and Xvfb, but `WEB_VIEW_PROVIDER=PROWL_VNC` skips CEF
initialization even when `server.kcefEnabled = true`. To use CEF, select `CEF` and keep
`KCEF_ENABLED=true`. It needs a raised `shm_size` (Docker's 64 MiB default can crash the
renderer) and roughly 0.5 GiB of spare RAM. The external Prowl browser runs in its own
container instead.

```sh
docker compose exec suwayomi sh -lc \
  'grep -E "webViewProvider|kcefEnabled" /home/suwayomi/.local/share/Tachidesk/server.conf'
docker stats --no-stream suwayomi prowl
```

### Challenge solver health

The solver container is `prowl`, which answers the app's FlareSolverr-compatible client. Probe its
own health endpoint rather than the root:

```sh
docker compose ps prowl
docker compose exec suwayomi sh -lc 'curl -s -o /dev/null -w "%{http_code}\n" http://prowl:8191/healthz'
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
- The rclone sidecar healthcheck checks the mount itself (`grep ' /data ' /proc/mounts`), not the
  remote: a remote probe would spend a Backblaze Class C transaction on every interval and trip the
  account's transaction cap. Suwayomi therefore waits only for the sidecar to be *started*
  (`condition: service_started`), so a remote-side problem does not stop the app from starting.
- `--allow-other` is safe because the sidecar runs as root.
- `WEB_UI_FLAVOR=Custom` with `WEB_UI_CHANNEL=BUNDLED` and `WEB_UI_UPDATE_INTERVAL=0` is
  required: the archival dashboard only exists in the WebUI bundled into this image, and
  any other combination lets the server replace it with an upstream release.
- The published image name is `ghcr.io/imxeren/suwayomi-server` (`:stable` from `main`,
  `:dev` from `dev`). The package is public, so the host pulls it anonymously.
