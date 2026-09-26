#!/usr/bin/env python3
"""Keep every watched host on an egress that works.

The stack egresses through `egress-hop` (gost), which sends each request to an upstream chosen by
destination host: the named upstream whose rule file lists the host, or the configured fallback
for everything else. Which upstream works is a property of the host and of that upstream's IP
reputation, and it changes: a session that is fine today can be served a hard block page tomorrow,
and the fallback is hard blocked by at least one site an ISP session clears.

This prober keeps that mapping honest. It probes each watched host against every configured egress,
directly rather than through the hop, and rewrites the rule files so that each host sits on a
working egress. The configured low-cost upstream is probed first for every host, so a host moves
back onto it as soon as it can; only when that upstream cannot serve the host does the current
assignment get to stay, and only then are the other upstreams tried.

A destination only ever appears in one file: an entry the direct list claims is dropped from every
upstream file, and a duplicate goes to the first upstream that holds it, because gost spreads a
host across every node whose bypass passes. A wildcard and a concrete host it covers are refused
rather than split across two files, because that pair cannot be resolved by dropping a duplicate:
the direct list itself is policy, edited by hand, and is never rewritten here.

Stickiness is deliberate: a Cloudflare clearance is bound to the IP that solved it, so a host that
hops between egresses loses its clearance and has to solve the challenge again. The one egress a
host is moved onto without hesitation is the preferred (low-cost) candidate: it is probed first for
every host, so a host that can move back to it does, even while a paid upstream still serves it.
That move costs the host its clearance once, which is cheaper than keeping it on paid egress.

Usage:
    egress-prober.py                    probe, then write the rule files
    egress-prober.py --dry-run          probe and report, write nothing
    egress-prober.py --host a.example   probe one host
    egress-prober.py --timeout 30       bound each request

Run it where `host.docker.internal` resolves, since that is how a host-local upstream (such as a
WARP bridge) is reached: the same `extra_hosts` entry the hop service declares. The rules directory
and its direct-hosts.txt must be present: without them the run refuses instead of reading as empty
and writing rule files outside the mount. A missing watch file is not an error, it just means there
are no hosts beyond the ones already assigned.

The watch list is `egress-rules/egress-watch.txt`, one host per line with `#` comments allowed.
Every host already assigned in a rule file is watched as well, so no assignment goes unverified.
Keep notes in the watch list rather than in a rule file: the rule files are regenerated.
"""
from __future__ import annotations

import argparse
import dataclasses
import html
import json
import pathlib
import re
import socket
import ssl
import sys
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence

REPO = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO / "secrets" / "egress-upstreams.json"
RULES_DIR = REPO / "egress-rules"
WATCH_FILE_NAME = "egress-watch.txt"

#: Rule file holding the direct egress's hosts, matching scripts/egress-hop-config.py. Its entries
#: join the union so the fallback cannot also be eligible for them, but the file is never rewritten
#: here, because a probe has nothing to say about a hop that has no upstream.
DIRECT_RULE_FILE = "direct-hosts.txt"

#: Sentinel an egress with no hosts carries, matching scripts/egress-hop-config.py. A whitelist
#: with no matchers passes every request, so an idle egress would otherwise serve hosts that
#: belong to another egress.
SENTINEL = "prowl-egress-unused.invalid"

#: Rule file header, matching scripts/egress-hop-config.py.
RULE_HEADER = (
    "# One destination host per line. A host listed here leaves through this egress instead of the\n"
    "# fallback. Wildcards such as *.example.com are supported.\n"
)

#: Union file header, matching scripts/egress-hop-config.py.
UNION_HEADER = (
    "# Generated: the union of the direct list and every egress rule file. A host listed here is\n"
    "# claimed by an egress, so it never falls back to the fallback upstream.\n"
)

SENTINEL_NOTE = (
    "# No hosts assigned to this egress. The sentinel below can never match a real destination, so\n"
    "# this egress stays inert; without a matcher the whitelist would pass everything and the node\n"
    "# would be picked for every request.\n"
)


def union_file_name(fallback_name: str) -> str:
    """Return the rule file the fallback's complement bypass reads."""
    return f"{fallback_name}-exclude.txt"

#: Body titles that mean the request was refused outright and a browser cannot help.
BLOCK_MARKERS = ("attention required", "acces restreint")

#: Body titles that mean a challenge a real browser can solve.
CHALLENGE_MARKERS = ("just a moment",)

DEFAULT_TIMEOUT_SECONDS = 20.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_HTTPS_PORT = 443
DEFAULT_HTTP_PORT = 80
MAX_BODY_BYTES = 64 * 1024
MAX_TITLE_LENGTH = 300

_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")

#: The characters a plain upstream host may carry, matching scripts/egress-hop-config.py's
#: parse_addr. Anything else - a scheme separator, a path, a query, or an embedded credential -
#: marks the address as a URL rather than a host, so it is refused rather than echoed.
_UPSTREAM_HOST_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_STATUS_PATTERN = re.compile(r"^HTTP/\d(?:\.\d)?\s+(\d{3})")

_SOCKS5_VERSION = 5
_SOCKS5_AUTH_NONE = 0x00
_SOCKS5_AUTH_USERNAME = 0x02
_SOCKS5_COMMAND_CONNECT = 0x01
_SOCKS5_ADDRESS_DOMAIN = 0x03
_SOCKS5_SUCCEEDED = 0x00
_MAX_SOCKS5_FIELD_BYTES = 255


class ProbeError(Exception):
    """A probe could not be completed."""


@dataclasses.dataclass(frozen=True, slots=True)
class Egress:
    """One upstream the hop can use, read from the hop configuration."""

    name: str
    host: str
    port: int
    #: Credentials are read for the SOCKS5 handshake but never printed, not even by a repr().
    username: str = dataclasses.field(default="", repr=False)
    password: str = dataclasses.field(default="", repr=False)
    #: The upstream the hop uses when no other egress claims a host.
    is_fallback: bool = False

    def describe(self) -> str:
        """Return a label safe to print: the endpoint, never the credentials."""
        return f"{self.name} ({self.host}:{self.port})"


@dataclasses.dataclass(frozen=True, slots=True)
class Egresses:
    """The hop's upstreams, read from the configuration the generator and this prober share."""

    #: The candidate probed first for every host: the low-cost egress the mapping should settle on.
    #: Defaults to the fallback when the configuration does not name one.
    preferred: str
    #: The upstream the hop uses when no other egress claims a host.
    fallback: str
    upstreams: tuple[Egress, ...]

    def assignable(self) -> list[str]:
        """Return the names that own a rule file: every upstream except the fallback."""
        return [egress.name for egress in self.upstreams if not egress.is_fallback]


@dataclasses.dataclass(frozen=True, slots=True)
class ProbeTarget:
    """A destination to probe."""

    host: str
    port: int = DEFAULT_HTTPS_PORT
    scheme: str = "https"

    @classmethod
    def parse(cls, raw: str) -> ProbeTarget:
        """Build a target from a watch or rule entry.

        Accepts ``host``, ``host:port``, and either with an ``http`` or ``https`` scheme.

        :raises ValueError: when *raw* is not a single concrete host.
        """
        value = raw.strip()
        scheme = "https"
        if "://" in value:
            scheme, _, value = value.partition("://")
            scheme = scheme.lower()
            if scheme not in {"http", "https"}:
                msg = f"unsupported scheme in {raw!r}"
                raise ValueError(msg)
        if "/" in value:
            msg = f"a target must be a host, not a URL with a path: {raw!r}"
            raise ValueError(msg)
        host, separator, port_text = value.rpartition(":")
        if not separator:
            host, port_text = value, ""
        if not host or not _HOST_PATTERN.match(host):
            msg = f"a target must be a host name: {raw!r}"
            raise ValueError(msg)
        if port_text:
            if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
                msg = f"invalid port in {raw!r}"
                raise ValueError(msg)
            port = int(port_text)
        else:
            port = DEFAULT_HTTP_PORT if scheme == "http" else DEFAULT_HTTPS_PORT
        return cls(host=host, port=port, scheme=scheme)

    @property
    def authority(self) -> str:
        """Return the host, with the port only when it is not the default for the scheme."""
        default = DEFAULT_HTTP_PORT if self.scheme == "http" else DEFAULT_HTTPS_PORT
        return self.host if self.port == default else f"{self.host}:{self.port}"

    def url(self) -> str:
        """Return the URL the probe requests."""
        return f"{self.scheme}://{self.authority}/"

    def entry(self) -> str:
        """Return the form written back to a rule file."""
        default = DEFAULT_HTTP_PORT if self.scheme == "http" else DEFAULT_HTTPS_PORT
        return self.host if self.port == default else f"{self.host}:{self.port}"


@dataclasses.dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """What a probe saw. A status of ``None`` means the request never completed."""

    status: int | None
    title: str = ""
    body: str = ""
    error: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class Classification:
    """Whether an egress may serve a host, and the short label to report."""

    usable: bool
    label: str


@dataclasses.dataclass(frozen=True, slots=True)
class Decision:
    """What the prober decided for one host."""

    host: str
    egress: str
    label: str
    changed: bool
    action: str
    #: The form to write back to a rule file, which keeps a non-default port.
    entry: str = ""
    #: Why no egress was usable, when that is what happened.
    detail: str = ""


@dataclasses.dataclass(slots=True)
class Report:
    """Everything the run observed and, unless it was a dry run, changed."""

    egresses: list[str]
    decisions: list[Decision]
    watched_but_not_probed: list[str]
    written: list[str]
    unchanged: list[str]
    dry_run: bool

    @property
    def changes(self) -> int:
        return sum(1 for decision in self.decisions if decision.changed)


def normalize_text(value: str) -> str:
    """Fold *value* for marker matching: entities decoded, accents dropped, lower case."""
    unescaped = html.unescape(value)
    decomposed = unicodedata.normalize("NFKD", unescaped)
    without_marks = "".join(character for character in decomposed if not unicodedata.combining(character))
    return without_marks.casefold()


def extract_title(response_text: str) -> str:
    """Return the document title of *response_text*, or an empty string."""
    match = _TITLE_PATTERN.search(response_text)
    if match is None:
        return ""
    return " ".join(match.group(1).split())[:MAX_TITLE_LENGTH]


def extract_status(response_text: str) -> int | None:
    """Return the status code of the first response line, or ``None``."""
    match = _STATUS_PATTERN.match(response_text)
    return int(match.group(1)) if match else None


def classify(outcome: ProbeOutcome) -> Classification:
    """Decide whether *outcome* means the egress can serve the host.

    A hard block page is refused for good, because it is served by an edge that has already
    decided against this source. A challenge page is usable, because the solver passes it and
    then the app's own requests inherit the clearance. The title decides first, and the body is
    consulted only when a response carried no title, so a challenge page that happens to link to
    a blocked page is not mistaken for a block.
    """
    if outcome.status is None:
        return Classification(usable=False, label="error")
    haystack = normalize_text(outcome.title or outcome.body)
    if any(marker in haystack for marker in BLOCK_MARKERS):
        return Classification(usable=False, label="blocked")
    if any(marker in haystack for marker in CHALLENGE_MARKERS):
        return Classification(usable=True, label="challenge")
    if 200 <= outcome.status < 400:
        return Classification(usable=True, label="ok")
    if outcome.status == 403:
        # Cloudflare serves its challenge with 403, and some challenges carry no title we can
        # read, so an unexplained 403 counts as solvable rather than as a refusal.
        return Classification(usable=True, label="challenge")
    return Classification(usable=False, label=f"http-{outcome.status}")


def parse_entry(raw: str) -> ProbeTarget | None:
    """Return the target for a watch or rule entry, or ``None`` when it cannot be probed.

    A wildcard cannot be probed, so an entry like ``*.example.com`` returns ``None`` and is left
    where it is rather than being re-decided.
    """
    try:
        return ProbeTarget.parse(raw)
    except ValueError:
        return None


def entry_key(entry_text: str) -> str:
    """Return an entry's identity for duplicate resolution: its host, or the raw wildcard.

    Two entries for the same host have to collapse to one destination, because gost spreads a host
    across every node whose bypass passes.
    """
    parsed = parse_entry(entry_text)
    return parsed.host if parsed is not None else entry_text


def host_matches(pattern: str, host: str) -> bool:
    """Return whether the rule *pattern* covers *host*.

    A ``*.example.com`` wildcard covers the subdomains of ``example.com`` but not the apex itself,
    which is how gost reads the same pattern. Everything else matches a host exactly, ignoring
    case. Kept identical in scripts/egress-hop-config.py.
    """
    pattern = pattern.strip().lower()
    host = host.strip().lower()
    if pattern.startswith("*."):
        return host.endswith(f".{pattern[2:]}")
    return pattern == host


def entries_overlap(left: str, right: str) -> bool:
    """Return whether two rule entries can match the same destination host.

    Kept identical in scripts/egress-hop-config.py.
    """
    return host_matches(left, right) or host_matches(right, left)


def refuse_overlaps(rule_files: list[tuple[str, list[str]]]) -> None:
    """Exit when two rule files can claim the same host.

    gost picks a node by bypass and reloads these files live, so a host two files can match would
    leave through both. Comparing entries pairwise also catches a wildcard in one file covering a
    concrete host in another, which a duplicate-key check cannot. The write step calls this before
    touching any file, so the hop never reloads an overlap; the message names the host and the two
    files and carries no credential.

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


def claimed_by_direct(entry: str, direct_entries: Sequence[str]) -> bool:
    """Return whether the direct list claims *entry*.

    The direct list is policy: a host it claims is never assigned to an upstream and never falls
    back. A wildcard it lists covers the concrete hosts it matches, not only its literal entries.
    """
    return any(entries_overlap(entry, direct) for direct in direct_entries)


def parse_upstream_addr(addr: str, name: str, config_path: pathlib.Path) -> tuple[str, int]:
    """Return the host and port of an upstream *addr*, or exit when it is not ``host:port``.

    Mirrors scripts/egress-hop-config.py's parse_addr: an address carrying a scheme, a path, or an
    embedded credential is refused, and the raw value is never echoed, so a secret inside it can
    never reach a report, a log, or :meth:`Egress.describe`. Kept local rather than imported,
    because this script runs with only its own file and the rules mount.

    :raises SystemExit: when *addr* is not a plain ``host:port``.
    """
    host, separator, port_text = addr.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        # The raw address is never echoed: a malformed value can carry a credential in its URL.
        sys.exit(f"{config_path}: upstream {name!r} needs a plain host:port address; set a credential with username/password")
    if not _UPSTREAM_HOST_PATTERN.match(host):
        # A URL-like or credential-bearing host is refused without echoing it either.
        sys.exit(f"{config_path}: upstream {name!r} needs a plain host name; set a credential with username/password")
    port = int(port_text)
    if not 1 <= port <= 65535:
        sys.exit(f"{config_path}: upstream {name!r} has an invalid port: {port}")
    return host, port


def load_egresses(config_path: pathlib.Path) -> Egresses:
    """Return the hop's upstreams and the two roles the prober needs.

    The configuration is the file scripts/egress-hop-config.py also reads, so the prober probes
    exactly the upstreams the hop is built from. ``fallback`` names the upstream that serves
    whatever no other egress claims; ``preferred``, defaulting to the fallback, is the low-cost
    candidate probed first for every host.

    :raises SystemExit: when the configuration is missing or unusable.
    """
    if not config_path.exists():
        sys.exit(f"missing {config_path} (see secrets/egress-upstreams.json.example)")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        sys.exit(f"cannot read {config_path}: {error}")
    if not isinstance(config, dict):
        sys.exit(f"{config_path} must be a JSON object with 'fallback' and 'upstreams'")

    fallback = str(config.get("fallback", "")).strip()
    if not fallback:
        sys.exit(f"{config_path} must name the fallback upstream")
    preferred = str(config.get("preferred", fallback)).strip() or fallback

    entries = config.get("upstreams")
    if not isinstance(entries, list) or not entries:
        sys.exit(f"{config_path} must list at least one upstream")

    egresses: list[Egress] = []
    seen: set[str] = set()
    for index, item in enumerate(entries):
        if not isinstance(item, dict):
            sys.exit(f"{config_path}: upstream #{index + 1} must be a JSON object")
        name = str(item.get("name", "")).strip()
        if not name:
            sys.exit(f"{config_path}: upstream #{index + 1} needs a name")
        if name in seen:
            sys.exit(f"{config_path}: duplicate upstream name: {name!r}")
        seen.add(name)
        host, port = parse_upstream_addr(str(item.get("addr", "")).strip(), name, config_path)
        egresses.append(
            Egress(
                name=name,
                host=host,
                port=port,
                username=str(item.get("username", "")),
                password=str(item.get("password", "")),
                is_fallback=name == fallback,
            )
        )

    if fallback not in seen:
        sys.exit(f"{config_path}: fallback {fallback!r} is not one of the upstreams")
    if preferred not in seen:
        sys.exit(f"{config_path}: preferred {preferred!r} is not one of the upstreams")
    return Egresses(preferred=preferred, fallback=fallback, upstreams=tuple(egresses))


def read_entries(path: pathlib.Path) -> list[str]:
    """Return the non-comment entries of a rule file, sentinel included."""
    if not path.exists():
        return []
    entries: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            entries.append(stripped)
    return entries


def render_rule_file(hosts: Iterable[str]) -> str:
    """Render a rule file: the shared header, then the hosts or the sentinel."""
    entries = sorted(set(hosts))
    if not entries:
        return RULE_HEADER + "\n" + SENTINEL_NOTE + f"{SENTINEL}\n"
    return RULE_HEADER + "\n".join(entries) + "\n"


def render_union(hosts: Iterable[str]) -> str:
    """Render the union of every claimed host, which keeps the fallback away from them."""
    entries = sorted({host for host in hosts if host != SENTINEL})
    return UNION_HEADER + ("\n".join(entries) + "\n" if entries else "")


def write_if_changed(path: pathlib.Path, content: str) -> bool:
    """Write *content* to *path* when it differs. Return whether anything was written."""
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read exactly *count* bytes from *sock*."""
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            msg = "the connection closed during the proxy handshake"
            raise ProbeError(msg)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def socks5_open(egress: Egress, target: ProbeTarget, timeout: float) -> socket.socket:
    """Open a socket to *target* through the SOCKS5 *egress*.

    The host name is sent to the proxy rather than resolved here, so the proxy resolves it. That
    matches how the app reaches the hop, and it is what lets gost route by host.

    :raises ProbeError: when the proxy refuses, or the handshake fails.
    """
    try:
        sock = socket.create_connection((egress.host, egress.port), timeout=timeout)
    except OSError as error:
        msg = f"cannot reach the proxy: {type(error).__name__}: {error}"
        raise ProbeError(msg) from error

    try:
        sock.settimeout(timeout)
        if egress.username:
            sock.sendall(bytes([_SOCKS5_VERSION, 2, _SOCKS5_AUTH_NONE, _SOCKS5_AUTH_USERNAME]))
        else:
            sock.sendall(bytes([_SOCKS5_VERSION, 1, _SOCKS5_AUTH_NONE]))
        version, method = _recv_exactly(sock, 2)
        if version != _SOCKS5_VERSION:
            msg = "the proxy did not answer with SOCKS5"
            raise ProbeError(msg)
        if method == _SOCKS5_AUTH_USERNAME:
            _socks5_authenticate(sock, egress)
        elif method != _SOCKS5_AUTH_NONE:
            msg = "the proxy requires an authentication method this prober does not use"
            raise ProbeError(msg)

        host_bytes = target.host.encode("idna")
        if len(host_bytes) > _MAX_SOCKS5_FIELD_BYTES:
            msg = "the target host name is too long for SOCKS5"
            raise ProbeError(msg)
        request = (
            bytes([_SOCKS5_VERSION, _SOCKS5_COMMAND_CONNECT, 0, _SOCKS5_ADDRESS_DOMAIN, len(host_bytes)])
            + host_bytes
            + target.port.to_bytes(2, "big")
        )
        sock.sendall(request)
        _read_socks5_reply(sock, egress)
    except BaseException:
        sock.close()
        raise
    return sock


def _socks5_authenticate(sock: socket.socket, egress: Egress) -> None:
    """Complete RFC 1929 username and password authentication."""
    username = egress.username.encode()
    password = egress.password.encode()
    if len(username) > _MAX_SOCKS5_FIELD_BYTES or len(password) > _MAX_SOCKS5_FIELD_BYTES:
        msg = "the proxy credentials are too long for SOCKS5"
        raise ProbeError(msg)
    sock.sendall(bytes([1, len(username)]) + username + bytes([len(password)]) + password)
    _, status = _recv_exactly(sock, 2)
    if status != _SOCKS5_SUCCEEDED:
        msg = f"the proxy rejected the credentials for {egress.name}"
        raise ProbeError(msg)


def _read_socks5_reply(sock: socket.socket, egress: Egress) -> None:
    """Read and validate the SOCKS5 connect reply."""
    _, reply, _, address_type = _recv_exactly(sock, 4)
    if reply != _SOCKS5_SUCCEEDED:
        msg = f"the proxy refused the connection for {egress.name} with code {reply}"
        raise ProbeError(msg)
    if address_type == 1:
        _recv_exactly(sock, 4 + 2)
    elif address_type == _SOCKS5_ADDRESS_DOMAIN:
        _recv_exactly(sock, _recv_exactly(sock, 1)[0] + 2)
    elif address_type == 4:
        _recv_exactly(sock, 16 + 2)
    else:
        msg = f"the proxy answered with an unknown address type {address_type}"
        raise ProbeError(msg)


def _read_response(sock: socket.socket) -> str:
    """Read up to the body limit from *sock* and decode it."""
    chunks: list[bytes] = []
    received = 0
    while received < MAX_BODY_BYTES:
        try:
            chunk = sock.recv(min(8192, MAX_BODY_BYTES - received))
        except TimeoutError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def probe_over_socks(target: ProbeTarget, egress: Egress, timeout: float) -> ProbeOutcome:
    """Request *target* through *egress* and return what came back."""
    sock: socket.socket | None = None
    try:
        sock = socks5_open(egress, target, timeout)
        if target.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=target.host)
        request = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {target.authority}\r\n"
            f"User-Agent: {USER_AGENT}\r\n"
            f"Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8\r\n"
            f"Accept-Encoding: identity\r\n"
            f"Accept-Language: en-US,en;q=0.9\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode())
        response_text = _read_response(sock)
    except ProbeError as error:
        return ProbeOutcome(status=None, error=str(error))
    except OSError as error:
        # Timeouts and TLS failures are OSError subclasses, so one clause covers them all.
        return ProbeOutcome(status=None, error=f"{type(error).__name__}: {error}")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    status = extract_status(response_text)
    if status is None:
        return ProbeOutcome(status=None, error="the response had no HTTP status line")
    return ProbeOutcome(status=status, title=extract_title(response_text), body=response_text)


def shorten(text: str, limit: int = 100) -> str:
    """Return *text* collapsed onto one line and trimmed for a report line."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 3] + "..."


ProbeFn = Callable[[ProbeTarget, Egress], ProbeOutcome]


class EgressProber:
    """Probe watched hosts and keep each one on a usable egress."""

    def __init__(
        self,
        *,
        config_path: pathlib.Path = CONFIG_PATH,
        rules_dir: pathlib.Path = RULES_DIR,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        probe: ProbeFn | None = None,
    ) -> None:
        self._config_path = pathlib.Path(config_path)
        self._rules_dir = pathlib.Path(rules_dir)
        self._timeout = float(timeout)
        self._probe = probe if probe is not None else self._default_probe

    def _default_probe(self, target: ProbeTarget, egress: Egress) -> ProbeOutcome:
        return probe_over_socks(target, egress, self._timeout)

    def run(self, *, dry_run: bool = False, only_host: str | None = None) -> Report:
        """Probe the watched hosts and, unless *dry_run*, write the assignments back."""
        self._require_rules_mount()
        config = load_egresses(self._config_path)
        upstream_names = config.assignable()
        assignments = self._read_assignments(upstream_names)
        watch_entries, watch_skipped = self._read_watch_entries()

        # The watch list wins over the rule files, so an explicit entry there decides how a host
        # is probed. Every assigned host is watched as well, so nothing goes unverified.
        targets: dict[str, ProbeTarget] = {}
        for entry in watch_entries:
            targets.setdefault(entry.host, entry)
        for name in upstream_names:
            for entry_text in self._rule_entries(name):
                if entry_text == SENTINEL:
                    continue
                parsed = parse_entry(entry_text)
                if parsed is not None:
                    targets.setdefault(parsed.host, parsed)

        # A host the direct list claims is policy, not an assignment: it is left where it is and is
        # never probed, so its entry stays in the direct file alone. A wildcard the direct list
        # carries covers the concrete hosts it matches, not only its literal entries.
        direct_entries = self._direct_entries()
        targets = {
            host: entry
            for host, entry in targets.items()
            if not claimed_by_direct(host, direct_entries)
        }

        if only_host is not None:
            if parse_entry(only_host) is None:
                sys.exit(f"--host must be a host name: {only_host!r}")
            targets = {host: entry for host, entry in targets.items() if host == only_host}
            # The run is scoped to one host, so a wildcard elsewhere is not part of its report.
            watch_skipped = [entry for entry in watch_skipped if entry == only_host]

        decisions = [
            self._decide(target, config, assignments)
            for target in sorted(targets.values(), key=lambda entry: entry.host)
        ]

        written: list[str] = []
        unchanged: list[str] = []
        if dry_run:
            # A dry run writes nothing, but it still validates the plan it would publish, so it can
            # never report success for a mapping a real run would refuse.
            self._refuse_planned_overlaps(
                upstream_names,
                direct_entries,
                self._plan_assignment(upstream_names, direct_entries, decisions),
            )
        else:
            written, unchanged = self._apply(upstream_names, config.fallback, direct_entries, decisions)

        return Report(
            egresses=[egress.describe() for egress in config.upstreams],
            decisions=decisions,
            watched_but_not_probed=sorted(set(watch_skipped)),
            written=written,
            unchanged=unchanged,
            dry_run=dry_run,
        )

    def _rule_entries(self, name: str) -> list[str]:
        """Return the entries of one rule file."""
        return read_entries(self._rules_dir / f"{name}.txt")

    def _require_rules_mount(self) -> None:
        """Fail closed when the rules directory or direct rule file is not where it should be.

        A missing directory reads as "nothing watched" and would let the write step create rule
        files outside the mounted directory, so the run refuses instead.
        """
        if not self._rules_dir.is_dir():
            sys.exit(f"rules directory {self._rules_dir} is missing; is the egress-rules mount in place?")
        direct = self._rules_dir / DIRECT_RULE_FILE
        if not direct.is_file():
            sys.exit(f"missing {direct}; the direct list marks the hosts that must not use an upstream")

    def _direct_entries(self) -> list[str]:
        """Return the direct file's entries, sentinel excluded.

        The file is policy, so it is only read: a host it claims is never assigned to an upstream
        and never falls back either.
        """
        return [
            entry
            for entry in read_entries(self._rules_dir / DIRECT_RULE_FILE)
            if entry != SENTINEL
        ]

    def _read_assignments(self, upstream_names: Sequence[str]) -> dict[str, str]:
        """Return the host to egress map that the rule files currently describe.

        Only probeable hosts appear: a wildcard cannot be probed, so it is preserved in its file
        rather than being treated as an assignment to re-decide.
        """
        assignments: dict[str, str] = {}
        for name in upstream_names:
            for entry_text in self._rule_entries(name):
                if entry_text == SENTINEL:
                    continue
                parsed = parse_entry(entry_text)
                if parsed is not None:
                    assignments.setdefault(parsed.host, name)
        return assignments

    def _read_watch_entries(self) -> tuple[list[ProbeTarget], list[str]]:
        """Return the probeable watch entries and the ones that could not be probed."""
        path = self._rules_dir / WATCH_FILE_NAME
        if not path.exists():
            return [], []
        targets: list[ProbeTarget] = []
        skipped: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped == SENTINEL:
                continue
            parsed = parse_entry(stripped)
            if parsed is not None:
                targets.append(parsed)
            elif "*" in stripped:
                skipped.append(stripped)
            else:
                sys.exit(f"{path}: a target must be a host name, not {stripped!r}")
        return targets, skipped

    def _probe_order(self, current: str, config: Egresses) -> list[Egress]:
        """Return the egresses to try: the preferred candidate, the current one, then the rest.

        The preferred (low-cost) upstream is probed first for every host, so a host that can move
        back onto it does, which keeps paid egress off it. Only when the preferred upstream cannot
        serve the host does the current assignment get to stay.
        """
        order: list[Egress] = []
        seen: set[str] = set()
        for name in (config.preferred, current):
            if name in seen:
                continue
            for egress in config.upstreams:
                if egress.name == name:
                    order.append(egress)
                    seen.add(name)
                    break
        for egress in config.upstreams:
            if egress.name not in seen:
                order.append(egress)
                seen.add(egress.name)
        return order

    def _decide(
        self,
        target: ProbeTarget,
        config: Egresses,
        assignments: Mapping[str, str],
    ) -> Decision:
        """Probe *target* in preference order and decide where it should sit."""
        current = assignments.get(target.host, config.fallback)
        labels: dict[str, str] = {}
        errors: dict[str, str] = {}
        for egress in self._probe_order(current, config):
            outcome = self._probe(target, egress)
            classification = classify(outcome)
            labels[egress.name] = classification.label
            if outcome.error:
                errors[egress.name] = outcome.error
            if classification.usable:
                changed = egress.name != current
                action = f"moved from {current}" if changed else f"kept on {current}"
                return Decision(
                    host=target.host,
                    egress=egress.name,
                    label=classification.label,
                    changed=changed,
                    action=action,
                    entry=target.entry(),
                )
        label = labels.get(current, "unusable")
        detail = errors.get(current) or next(iter(errors.values()), "")
        return Decision(
            host=target.host,
            egress=current,
            label=label,
            changed=False,
            action=f"no usable egress, kept on {current}",
            entry=target.entry(),
            detail=shorten(detail),
        )

    def _plan_assignment(
        self,
        upstream_names: Sequence[str],
        direct_entries: Sequence[str],
        decisions: Sequence[Decision],
    ) -> dict[str, set[str]]:
        """Return the entries each rule file would hold, without writing anything.

        Each destination ends up in at most one file, because gost spreads across every node whose
        bypass passes and a host eligible for two egresses would leave through both. The direct
        list is policy and wins over a probe; among upstreams, the first in configuration order
        that already holds an entry keeps it.
        """
        decided = {decision.host for decision in decisions}
        claimed: set[str] = set()
        hosts_by_egress: dict[str, set[str]] = {name: set() for name in upstream_names}

        for decision in decisions:
            if decision.egress not in hosts_by_egress:
                continue
            entry = decision.entry or decision.host
            if claimed_by_direct(entry, direct_entries):
                continue
            key = entry_key(entry)
            if key in claimed:
                continue
            hosts_by_egress[decision.egress].add(entry)
            claimed.add(key)

        for name in upstream_names:
            for entry_text in self._rule_entries(name):
                if entry_text == SENTINEL:
                    continue
                if claimed_by_direct(entry_text, direct_entries):
                    continue
                key = entry_key(entry_text)
                # A host this run did not decide, either because it cannot be probed or because the
                # run was narrowed with --host, keeps the assignment it had - unless the direct
                # list or an earlier upstream already claims it, which would spread it across two.
                if key in claimed:
                    continue
                parsed = parse_entry(entry_text)
                if parsed is not None and parsed.host in decided:
                    continue
                hosts_by_egress[name].add(entry_text)
                claimed.add(key)

        return hosts_by_egress

    def _refuse_planned_overlaps(
        self,
        upstream_names: Sequence[str],
        direct_entries: Sequence[str],
        hosts_by_egress: Mapping[str, set[str]],
    ) -> None:
        """Exit when two files in *hosts_by_egress* can match the same host.

        Two files that can match the same host would spread it across two egresses when gost
        reloads, so the overlap is refused before any write. The direct list joins the check
        because it is policy and keeps winning. The message names the host and the two files and
        carries no credential.

        :raises SystemExit: when any two files can match the same host.
        """
        refuse_overlaps(
            [(f"{name}.txt", sorted(hosts_by_egress[name])) for name in upstream_names]
            + [(DIRECT_RULE_FILE, list(direct_entries))]
        )

    def _apply(
        self,
        upstream_names: Sequence[str],
        fallback_name: str,
        direct_entries: Sequence[str],
        decisions: Sequence[Decision],
    ) -> tuple[list[str], list[str]]:
        """Write the rule files and the union. Return the written and unchanged paths."""
        hosts_by_egress = self._plan_assignment(upstream_names, direct_entries, decisions)
        self._refuse_planned_overlaps(upstream_names, direct_entries, hosts_by_egress)

        written: list[str] = []
        unchanged: list[str] = []
        for name in upstream_names:
            path = self._rules_dir / f"{name}.txt"
            if write_if_changed(path, render_rule_file(hosts_by_egress[name])):
                written.append(str(path))
            else:
                unchanged.append(str(path))

        union_hosts = {entry for hosts in hosts_by_egress.values() for entry in hosts}
        union_hosts.update(direct_entries)
        union_path = self._rules_dir / union_file_name(fallback_name)
        if write_if_changed(union_path, render_union(union_hosts)):
            written.append(str(union_path))
        else:
            unchanged.append(str(union_path))
        return written, unchanged


def render_report(report: Report) -> str:
    """Return the human readable summary of *report*."""
    lines: list[str] = []
    if report.dry_run:
        lines.append("egress prober: dry run, nothing written")
    lines.append(f"egresses: {', '.join(report.egresses)}")
    watched = f"watched: {len(report.decisions)} host(s)"
    if report.watched_but_not_probed:
        watched += f", {len(report.watched_but_not_probed)} not probeable"
    lines.append(watched)
    lines.append("")
    lines.append(f"  {'host':<34} {'egress':<10} {'result':<10} action")
    for decision in report.decisions:
        lines.append(f"  {decision.host:<34} {decision.egress:<10} {decision.label:<10} {decision.action}")
        if decision.detail:
            lines.append(f"      {decision.egress}: {decision.detail}")
    for entry in report.watched_but_not_probed:
        lines.append(f"  {entry:<34} {'-':<10} {'wildcard':<10} not probed, nothing to assign")
    lines.append("")
    lines.append(f"changes: {report.changes}")
    if report.dry_run:
        lines.append("wrote: nothing (dry run)")
    else:
        lines.append(f"wrote: {len(report.written)} file(s), {len(report.unchanged)} unchanged")
        for path in report.written:
            lines.append(f"  {path}")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Keep each watched host on a working egress.")
    parser.add_argument("--dry-run", action="store_true", help="probe and report, write nothing")
    parser.add_argument("--host", default=None, help="probe only this host")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="bound each request, in seconds",
    )
    parser.add_argument("--config", default=str(CONFIG_PATH), help="path to the upstreams configuration")
    parser.add_argument("--rules-dir", default=str(RULES_DIR), help="directory holding the rule files")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        sys.exit("--timeout must be greater than zero")
    prober = EgressProber(
        config_path=pathlib.Path(args.config),
        rules_dir=pathlib.Path(args.rules_dir),
        timeout=args.timeout,
    )
    report = prober.run(dry_run=args.dry_run, only_host=args.host)
    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
