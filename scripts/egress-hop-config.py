#!/usr/bin/env python3
"""Generate the egress hop's gost configuration and seed its rule files.

The hop is a SOCKS5 proxy that picks an upstream per destination host:

    app requests -> hop -> direct          (hosts listed in egress-rules/direct-hosts.txt)
                        -> <upstream>.     (hosts listed in egress-rules/<upstream>.txt)
                        -> <fallback>      (everything else)

The upstreams are arbitrary named SOCKS5 endpoints read from a configuration file, so a
deployment can mix providers, give each its own host, port and optional credentials, and choose
which one is the fallback. gost uses the first node whose bypass passes and a node without a
bypass is always used, so the fallback node is listed last and matches the complement of every
other egress.

Cloudflare clearance is bound to the IP that solved it, so whichever egress serves a host must
serve both the app and the challenge solver. Pointing the app's SOCKS setting and Prowl's
PROWL_PROXY_URL at this hop gives that for free.

Reads ./secrets/egress-upstreams.json and writes ./secrets/egress-hop.json plus the rule files
under the rules directory.

The direct list is policy rather than a probe result: it is seeded from the built-in defaults only
when it is missing and is never overwritten, because it is edited by hand. The per-upstream rule
files are seeded inert and are then owned by the prober, which fills them from its probes.

Usage: egress-hop-config.py [--upstreams FILE] [--config-out FILE] [--rules-dir DIR] [--rules-local DIR]
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SECRETS = REPO / "secrets"
UPSTREAMS_CONFIG = SECRETS / "egress-upstreams.json"
CONFIG_OUT = SECRETS / "egress-hop.json"

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: The characters a plain upstream host may carry. Anything else - a scheme separator, a path, a
#: query, or an embedded credential - marks the value as a URL rather than a host, and it is
#: refused without echoing it.
_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

#: The egress that leaves from this host itself, with no upstream at all.
#:
#: GitHub counts its unauthenticated API budget per source address, and a shared fallback egress
#: is not a stable one, so these hosts bypass every upstream. The address is a second, chain-less
#: SOCKS5 service inside this same container, because a chain node has to dial something: a node
#: with no connector is taken as an HTTP proxy and fails with "dial tcp: missing address".
DIRECT_NODE_NAME = "direct"
DIRECT_BYPASS_NAME = "direct-hosts"
DIRECT_SERVICE_ADDR = "127.0.0.1:1081"
DIRECT_RULE_FILE = f"{DIRECT_BYPASS_NAME}.txt"
DIRECT_HOSTS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "gist.githubusercontent.com",
    "*.githubusercontent.com",
    "ghcr.io",
    "pkg-containers.githubusercontent.com",
)

#: Sentinel an egress with no hosts carries. A whitelist with no matchers would otherwise pass
#: every request and the node would serve hosts that belong to another egress. `.invalid` is
#: reserved, so it can never resolve. Kept identical in scripts/egress-prober.py.
SENTINEL = "prowl-egress-unused.invalid"

#: Rule file header. Kept identical in scripts/egress-prober.py, which rewrites these files.
RULE_HEADER = (
    "# One destination host per line. A host listed here leaves through this egress instead of the\n"
    "# fallback. Wildcards such as *.example.com are supported.\n"
)

#: Union file header. Kept identical in scripts/egress-prober.py.
UNION_HEADER = (
    "# Generated: the union of the direct list and every egress rule file. A host listed here is\n"
    "# claimed by an egress, so it never falls back to the fallback upstream.\n"
)

#: Direct-list header. The file is seeded, not generated, so this documents its ownership.
DIRECT_HEADER = (
    "# Hosts listed here leave from this host itself and traverse no upstream. This file is policy,\n"
    "# edited by hand: scripts/egress-hop-config.py seeds it only when it is missing. The seed is\n"
    "# the GitHub hosts, whose unauthenticated API budget is counted per source address and so\n"
    "# must not share the fallback upstream's address.\n"
)

SENTINEL_NOTE = (
    "# No hosts assigned to this egress. The sentinel below can never match a real destination, so\n"
    "# this egress stays inert; without a matcher the whitelist would pass everything and the node\n"
    "# would be picked for every request.\n"
)


@dataclasses.dataclass(frozen=True, slots=True)
class Upstream:
    """One named SOCKS5 upstream the hop can route through."""

    name: str
    host: str
    port: int
    #: Credentials are never echoed, not even by an accidental repr().
    username: str = dataclasses.field(default="", repr=False)
    password: str = dataclasses.field(default="", repr=False)

    @property
    def addr(self) -> str:
        return f"{self.host}:{self.port}"


def fallback_bypass_name(fallback_name: str) -> str:
    """Return the name of the fallback's complement bypass."""
    return f"{fallback_name}-exclude"


def union_file_name(fallback_name: str) -> str:
    """Return the rule file the fallback's complement bypass reads."""
    return f"{fallback_bypass_name(fallback_name)}.txt"


def parse_addr(addr: str, name: str) -> tuple[str, int]:
    """Return the host and port of *addr*, or exit when it is not a usable ``host:port``."""
    host, separator, port_text = addr.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        # The raw address is never echoed: a malformed value can carry a credential in its URL.
        sys.exit(f"upstream {name!r} needs a plain host:port address; set a credential with username/password")
    if not _HOST_PATTERN.match(host):
        # A URL-like or credential-bearing host is refused without echoing it either.
        sys.exit(f"upstream {name!r} needs a plain host name; set a credential with username/password")
    port = int(port_text)
    if not 1 <= port <= 65535:
        sys.exit(f"upstream {name!r} has an invalid port: {port}")
    return host, port


def read_upstreams(path: pathlib.Path) -> tuple[list[Upstream], str]:
    """Read the named upstreams and the fallback name from *path*.

    Fails closed with a message that never contains a credential.

    :raises SystemExit: when the file is missing, malformed, or internally inconsistent.
    """
    if not path.exists():
        sys.exit(f"missing {path} (see secrets/egress-upstreams.json.example)")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        sys.exit(f"cannot read {path}: {error}")
    if not isinstance(raw, dict):
        sys.exit(f"{path} must be a JSON object with 'fallback' and 'upstreams'")

    fallback = raw.get("fallback")
    if not isinstance(fallback, str) or not fallback.strip():
        sys.exit(f"{path} must name the fallback upstream")
    fallback = fallback.strip()

    entries = raw.get("upstreams")
    if not isinstance(entries, list) or not entries:
        sys.exit(f"{path} must list at least one upstream")

    reserved = {DIRECT_NODE_NAME, DIRECT_BYPASS_NAME, fallback_bypass_name(fallback)}
    upstreams: list[Upstream] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            sys.exit(f"{path}: upstream #{index + 1} must be a JSON object")
        name = str(item.get("name", "")).strip()
        if not NAME_PATTERN.match(name) or name in reserved:
            sys.exit(f"{path}: upstream #{index + 1} has an unsafe or reserved name: {name!r}")
        if name in seen:
            sys.exit(f"{path}: duplicate upstream name: {name!r}")
        seen.add(name)
        host, port = parse_addr(str(item.get("addr", "")).strip(), name)
        username = str(item.get("username", ""))
        password = str(item.get("password", ""))
        if bool(username) != bool(password):
            sys.exit(f"{path}: upstream {name!r} must set both username and password, or neither")
        upstreams.append(
            Upstream(name=name, host=host, port=port, username=username, password=password)
        )

    if fallback not in seen:
        sys.exit(f"{path}: fallback {fallback!r} is not one of the upstreams")

    # The prober reads this too, to know which upstream is the low-cost candidate it should probe
    # first. Validated here so a typo fails the generator rather than the hourly prober.
    preferred = str(raw.get("preferred", "")).strip()
    if preferred and preferred not in seen:
        sys.exit(f"{path}: preferred {preferred!r} is not one of the upstreams")

    return upstreams, fallback


def connector(upstream: Upstream) -> dict:
    """Return the gost connector for *upstream*, with auth only when credentials are set."""
    connector: dict = {"type": "socks5"}
    if upstream.username:
        connector["auth"] = {"username": upstream.username, "password": upstream.password}
    return connector


def build(upstreams: list[Upstream], fallback_name: str, rules_dir: str) -> dict:
    """Return the gost configuration that routes through *upstreams*.

    The direct egress is listed first for readability and the fallback last: a node is chosen by
    its bypass rather than by its position, but the fallback's complement bypass only passes for
    hosts no other egress claims, which keeps each host on exactly one egress.
    """
    nodes = [
        {
            "name": DIRECT_NODE_NAME,
            "addr": DIRECT_SERVICE_ADDR,
            "connector": {"type": "socks5"},
            "dialer": {"type": "tcp"},
            "bypass": DIRECT_BYPASS_NAME,
        }
    ]
    bypasses = [
        {
            "name": DIRECT_BYPASS_NAME,
            "whitelist": True,
            "reload": "10s",
            "file": {"path": f"{rules_dir}/{DIRECT_RULE_FILE}"},
        }
    ]

    fallback_node: dict | None = None
    for upstream in upstreams:
        node = {
            "name": upstream.name,
            "addr": upstream.addr,
            "connector": connector(upstream),
            "dialer": {"type": "tcp"},
        }
        if upstream.name == fallback_name:
            node["bypass"] = fallback_bypass_name(fallback_name)
            fallback_node = node
            continue
        node["bypass"] = upstream.name
        nodes.append(node)
        bypasses.append(
            {
                "name": upstream.name,
                "whitelist": True,
                "reload": "10s",
                "file": {"path": f"{rules_dir}/{upstream.name}.txt"},
            }
        )

    if fallback_node is None:  # read_upstreams guarantees this cannot happen
        sys.exit(f"fallback {fallback_name!r} is not one of the upstreams")
    nodes.append(fallback_node)
    bypasses.append(
        {
            "name": fallback_bypass_name(fallback_name),
            "reload": "10s",
            "file": {"path": f"{rules_dir}/{union_file_name(fallback_name)}"},
        }
    )

    return {
        "services": [
            {
                "name": "socks-in",
                "addr": ":1080",
                "handler": {"type": "socks5", "chain": "chain-0"},
                "listener": {"type": "tcp"},
            },
            # A service with no chain is a plain direct proxy: it opens the connection from this
            # host with no upstream. The chain dials it for the hosts the direct bypass selects.
            # It listens on loopback only, so it cannot be reached from outside the container.
            {
                "name": "direct-out",
                "addr": DIRECT_SERVICE_ADDR,
                "handler": {"type": "socks5"},
                "listener": {"type": "tcp"},
            },
        ],
        "chains": [{"name": "chain-0", "hops": [{"name": "hop-0", "nodes": nodes}]}],
        "bypasses": bypasses,
    }


def read_entries(path: pathlib.Path) -> list[str]:
    """Return the non-comment entries of a rule file."""
    if not path.exists():
        return []
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entries.append(stripped)
    return entries


def host_matches(pattern: str, host: str) -> bool:
    """Return whether the rule *pattern* covers *host*.

    A ``*.example.com`` wildcard covers the subdomains of ``example.com`` but not the apex itself,
    which is how gost reads the same pattern. Everything else matches a host exactly, ignoring
    case. Kept identical in scripts/egress-prober.py.
    """
    pattern = pattern.strip().lower()
    host = host.strip().lower()
    if pattern.startswith("*."):
        return host.endswith(f".{pattern[2:]}")
    return pattern == host


def entries_overlap(left: str, right: str) -> bool:
    """Return whether two rule entries can match the same destination host.

    Kept identical in scripts/egress-prober.py.
    """
    return host_matches(left, right) or host_matches(right, left)


def refuse_overlaps(rule_files: list[tuple[str, list[str]]]) -> None:
    """Exit when two rule files can claim the same host.

    gost picks a node by bypass, so a host two files claim would leave through both. Comparing
    entries pairwise also catches a wildcard in one file covering a concrete host in another. The
    message names the host and the two files and carries no credential. Kept identical in
    scripts/egress-prober.py.

    :raises SystemExit: when any two files can match the same host.
    """
    for index, (left_name, left_entries) in enumerate(rule_files):
        for right_name, right_entries in rule_files[index + 1 :]:
            for left in left_entries:
                for right in right_entries:
                    if entries_overlap(left, right):
                        sys.exit(
                            f"{left!r} in {left_name} and {right!r} in {right_name} can match the "
                            "same host, so gost would spread it across two egresses; keep it in "
                            "one file"
                        )


def write_rule_files(upstreams: list[Upstream], fallback_name: str, rules_local: pathlib.Path) -> None:
    """Seed the rule files an egress needs, and write the union the fallback excludes.

    An egress with no hosts assigned must stay inert, so a seeded file carries a sentinel that can
    never match. The direct list is policy, so it is seeded only when missing and never rewritten;
    the per-upstream files are seeded only when missing and are then owned by the prober.

    A host two files can claim would be spread across two egresses by gost, so the run fails
    closed before writing anything when that is the case.
    """
    rules_local.mkdir(parents=True, exist_ok=True)

    upstream_entries: list[tuple[str, list[str]]] = []
    for upstream in upstreams:
        if upstream.name == fallback_name:
            continue
        entries = [
            entry
            for entry in read_entries(rules_local / f"{upstream.name}.txt")
            if entry != SENTINEL
        ]
        upstream_entries.append((f"{upstream.name}.txt", entries))

    direct_rule_file = rules_local / DIRECT_RULE_FILE
    if direct_rule_file.exists():
        direct_entries = [entry for entry in read_entries(direct_rule_file) if entry != SENTINEL]
    else:
        direct_entries = list(DIRECT_HOSTS)

    # Fail closed before any write, including before the config that would split an egress.
    refuse_overlaps(upstream_entries + [(DIRECT_RULE_FILE, direct_entries)])

    for upstream in upstreams:
        if upstream.name == fallback_name:
            continue
        rule_file = rules_local / f"{upstream.name}.txt"
        if not rule_file.exists():
            rule_file.write_text(RULE_HEADER + "\n" + SENTINEL_NOTE + f"{SENTINEL}\n")
    if not direct_rule_file.exists():
        direct_rule_file.write_text(DIRECT_HEADER + "\n".join(DIRECT_HOSTS) + "\n")

    # The fallback keeps every host that no egress claims, which means excluding the rest. A host
    # eligible for both the fallback and an egress would be spread across the two, so every
    # claimed host is listed once.
    claimed: set[str] = set(direct_entries)
    for _, entries in upstream_entries:
        claimed.update(entries)

    exclude = sorted(claimed)
    union_path = rules_local / union_file_name(fallback_name)
    union_path.write_text(UNION_HEADER + ("\n".join(exclude) + "\n" if exclude else ""))
    print(f"rule files in {rules_local}: {len(exclude)} host(s) excluded from {fallback_name}")


def write_private_file(path: pathlib.Path, content: str) -> None:
    """Write *content* to *path* as 0600 through an atomic replace.

    The file holds credentials, so it is written into a fresh 0600 file in the same directory and
    then renamed over the target. A post-write chmod would leave a window where the default umask
    has already exposed it, and a fixed temporary name could be pre-created permissive and then
    receive the secret. mkstemp opens its file with O_EXCL and mode 0600 under an unpredictable
    name, so an existing path is never reused. The rename replaces the old file only once the new
    one is fully written and private, so a failure keeps the old file. Kept identical in
    scripts/migrate-egress-upstreams.py.
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
    parser = argparse.ArgumentParser(description="Generate the egress hop configuration.")
    parser.add_argument("--upstreams", default=str(UPSTREAMS_CONFIG), help="path to the upstreams file")
    parser.add_argument("--config-out", default=str(CONFIG_OUT), help="path of the gost configuration to write")
    parser.add_argument("--rules-dir", default="/rules", help="rules directory inside the hop container")
    parser.add_argument(
        "--rules-local",
        default=str(REPO / "egress-rules"),
        help="host directory the rule files are written to; a temp copy stages them without a live reload",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    upstreams, fallback_name = read_upstreams(pathlib.Path(args.upstreams))
    config = build(upstreams, fallback_name, args.rules_dir)

    # Rule files first: a cross-file overlap fails closed before the config that would split. They
    # go to --rules-local, which defaults to the live egress-rules; point it at a staged copy to
    # compare against the live files before gost hot-reloads anything.
    write_rule_files(upstreams, fallback_name, pathlib.Path(args.rules_local))

    config_out = pathlib.Path(args.config_out)
    write_private_file(config_out, json.dumps(config, indent=2) + "\n")

    print(f"wrote {config_out} with {len(upstreams)} upstream(s), fallback {fallback_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
