# Secrets

Live credentials, gitignored. Recreate these on a new host; the values come from the provider
dashboards. Nothing here is needed to read the repository, only to run the stack.

```
auth_password          the app's UI login password, one line, no trailing newline
rclone.conf            the archive remote. S3-compatible Backblaze B2, mounted with
                       --vfs-cache-mode full so reads can seek inside CBZ files
egress-upstreams.json  the named SOCKS5 upstreams the hop routes through, authored from
                       secrets/egress-upstreams.json.example. Holds provider credentials
egress-hop.json        GENERATED, not authored: run scripts/egress-hop-config.py
```

`egress-upstreams.json` is a small JSON object. It is read by `scripts/egress-hop-config.py`, which
turns it into `egress-hop.json` and the rule files, and by `scripts/egress-prober.py`, which probes
the upstreams it names:

```json
{
  "fallback": "warp",
  "preferred": "warp",
  "upstreams": [
    {"name": "isp-1", "addr": "proxy.example.net:10001", "username": "u", "password": "p"},
    {"name": "isp-2", "addr": "proxy.example.net:10005"},
    {"name": "warp", "addr": "host.docker.internal:40001"}
  ]
}
```

- `upstreams` — one entry per upstream, in probe-preference order. `name` is a safe, unique
  identifier and becomes a rule file name; `addr` is `host:port`; `username` and `password` are
  optional and must be given together. An upstream may be any SOCKS5 provider, and a host-local one
  such as the WARP bridge is reached at `host.docker.internal`. The reserved names `direct` and
  `direct-hosts` may not be used.
- `fallback` — required. The upstream that serves whatever no rule file claims. Its complement
  bypass file (`egress-rules/<fallback>-exclude.txt`) is generated from the other lists.
- `preferred` — optional, defaulting to the fallback. The low-cost candidate the prober probes
  first for every host, so a host that can move back onto it does.

`egress-rules/direct-hosts.txt` is separate policy, edited by hand; the generator only seeds it
when it is missing, so local edits survive. `egress-rules/<upstream>.txt` and the union file are
maintained by the prober, which may reassign a host after a probe.

Migrating an existing `decodo.env`: stop `egress-prober` first, because it reads
`egress-upstreams.json` hourly and can rewrite the live rule files from it. Then run
`scripts/migrate-egress-upstreams.py`, which writes `egress-upstreams.json` with `decodo-1`,
`decodo-2`, ... and `warp` as the fallback and touches nothing else; run
`scripts/egress-hop-config.py`; compare the regenerated `egress-hop.json` against a backup with
`cmp -s` (which prints no credentials); and only then copy the accepted rules in and recreate both
services with `docker compose up -d --force-recreate --no-deps egress-hop egress-prober`, so they
rebind the replaced files. See `docs/DEPLOY.md`.

Permissions: `chmod 600` on all of them, and `/opt/suwayomi/secrets` is `chmod 700`. The compose
files materialise these as Compose secrets rather than environment variables, which is why values
never appear in `docker-compose.yml` or `.env`.

Also outside this directory, and equally secret: `/etc/cloudflared/token` (the tunnel credential)
and the SSH host key. Neither belongs in the repository.
