#!/usr/bin/env python3
"""One-time migration from secrets/decodo.env to the generic secrets/egress-upstreams.json.

The deployment used to read a Decodo-specific env file. It now reads a list of named SOCKS5
upstreams, so this converts an existing env file into that list. The names the live rule files
already refer to are preserved (`decodo-1`, `decodo-2`, ... from the ports, and `warp` as the
fallback), so the per-host assignments keep working unchanged once the hop is regenerated.

This runs nothing and edits no rule file: it only writes the new configuration, and only when the
old one is complete. If anything is missing it exits without writing, so a half-migrated config can
never silently change routing.

Safe transition, on the host. The prober reads the new config hourly, and the hop hot-reloads
egress-rules within seconds, so the prober is stopped before the new config is written and the
generator is pointed at a staged copy rather than the live files:

    cd /opt/suwayomi
    # Stop the prober first: it reads secrets/egress-upstreams.json hourly and can rewrite the live
    # egress-rules from it, which would defeat the staged comparison. The app and Prowl keep running.
    sudo docker compose stop egress-prober
    sudo cp secrets/egress-hop.json secrets/egress-hop.json.bak
    sudo python3 scripts/migrate-egress-upstreams.py
    STAGE="$(mktemp -d)" && sudo cp -a egress-rules/. "$STAGE"
    sudo python3 scripts/egress-hop-config.py --rules-local "$STAGE"
    sudo cmp -s secrets/egress-hop.json.bak secrets/egress-hop.json \
      && echo 'egress-hop.json: unchanged' \
      || echo 'egress-hop.json: CHANGED - inspect locally before restarting, do not paste it'
    sudo diff -rq egress-rules "$STAGE" \
      && echo 'egress-rules: unchanged' \
      || echo 'egress-rules: CHANGED - inspect, then copy into egress-rules'

--rules-local keeps the regenerated rule files out of the live egress-rules, so the hop never reads
the staged files. egress-prober is the other reader of egress-upstreams.json: it runs hourly and can
write the live egress-rules from the new config, so it stays stopped until the migration is
accepted. Once both comparisons are acceptable, copy the accepted files in and recreate both services
with `docker compose up -d --force-recreate --no-deps egress-hop egress-prober`: the hop config and
this script's file are bind-mounted as files, so a replaced inode needs a fresh container, which
restart/start do not provide. A mismatch means a routing decision may differ, so review it first:
diff -rq compares bytes, so a header-comment-only difference is not a routing change. The comparisons
print no part of either file, because egress-hop.json holds upstream credentials. The old
secrets/decodo.env and every live egress-rules file are left in place, so the previous setup remains
recoverable.

Usage:
    migrate-egress-upstreams.py                    read secrets/decodo.env, write the new config
    migrate-egress-upstreams.py --provider-host H  override the upstream host (default isp.decodo.com)
    migrate-egress-upstreams.py --force            replace an existing new config
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SECRETS = REPO / "secrets"
LEGACY_ENV = SECRETS / "decodo.env"
CONFIG_OUT = SECRETS / "egress-upstreams.json"

DEFAULT_PROVIDER_HOST = "isp.decodo.com"
DEFAULT_FALLBACK_NAME = "warp"
DEFAULT_FALLBACK_ADDR = "host.docker.internal:40001"


def read_env(path: pathlib.Path) -> dict[str, str]:
    """Return the ``KEY=VALUE`` pairs of an env file, comments and blanks ignored."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def parse_ports(text: str) -> list[int]:
    """Return the ports listed in *text*, or exit when one is not a usable port number."""
    ports: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= 65535:
            sys.exit(f"invalid port in DECODO_PORTS: {part!r}; nothing written")
        ports.append(int(part))
    return ports


def write_private_file(path: pathlib.Path, content: str) -> None:
    """Write *content* to *path* as 0600 through an atomic replace.

    The file holds credentials, so it is written into a fresh 0600 file in the same directory and
    then renamed over the target. A post-write chmod would leave a window where the default umask
    has already exposed it, and a fixed temporary name could be pre-created permissive and then
    receive the secret. mkstemp opens its file with O_EXCL and mode 0600 under an unpredictable
    name, so an existing path is never reused. The rename replaces the old file only once the new
    one is fully written and private, so a failure keeps the old file. Kept identical in
    scripts/egress-hop-config.py.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content.encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate secrets/decodo.env to the upstreams config.")
    parser.add_argument("--env", default=str(LEGACY_ENV), help="the legacy env file to read")
    parser.add_argument("--out", default=str(CONFIG_OUT), help="the upstreams config to write")
    parser.add_argument("--provider-host", default=DEFAULT_PROVIDER_HOST, help="host of the migrated ISP proxies")
    parser.add_argument("--fallback-name", default=DEFAULT_FALLBACK_NAME, help="name to give the fallback upstream")
    parser.add_argument("--fallback-addr", default=DEFAULT_FALLBACK_ADDR, help="host:port of the fallback upstream")
    parser.add_argument("--force", action="store_true", help="replace an existing upstreams config")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = pathlib.Path(args.env)
    out_path = pathlib.Path(args.out)

    if out_path.exists() and not args.force:
        sys.exit(f"{out_path} already exists; refusing to overwrite (pass --force to replace it)")
    if not env_path.exists():
        sys.exit(f"nothing to migrate: {env_path} does not exist")

    values = read_env(env_path)
    user = values.get("DECODO_USER", "")
    password = values.get("DECODO_PASS", "")
    ports_text = values.get("DECODO_PORTS", "")
    if not user or not password or not ports_text:
        sys.exit(f"{env_path} must set DECODO_USER, DECODO_PASS and DECODO_PORTS; nothing written")

    ports = parse_ports(ports_text)
    if not ports:
        sys.exit(f"{env_path} lists no ports; nothing written")

    upstreams: list[dict[str, str]] = [
        {
            "name": f"decodo-{index + 1}",
            "addr": f"{args.provider_host}:{port}",
            "username": user,
            "password": password,
        }
        for index, port in enumerate(ports)
    ]
    upstreams.append({"name": args.fallback_name, "addr": args.fallback_addr})
    config = {
        "fallback": args.fallback_name,
        "preferred": args.fallback_name,
        "upstreams": upstreams,
    }

    write_private_file(out_path, json.dumps(config, indent=2) + "\n")

    print(f"wrote {out_path} with {len(ports)} upstream(s) and fallback {args.fallback_name}")
    print(f"left {env_path} and every egress-rules file unchanged")
    print("next: keep egress-prober stopped, since it reads this file hourly and can rewrite the live")
    print("      egress-rules from it. Run scripts/egress-hop-config.py, compare secrets/egress-hop.json")
    print("      against the backup with 'cmp -s' (it prints no credentials), then recreate both services")
    print("      with 'docker compose up -d --force-recreate --no-deps egress-hop egress-prober' once the")
    print("      regenerated files are accepted, so they rebind the files replaced above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
