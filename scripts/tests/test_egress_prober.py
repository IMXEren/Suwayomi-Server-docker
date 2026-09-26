#!/usr/bin/env python3
"""Tests for scripts/egress-prober.py.

Every probe is injected, so nothing here touches the network. The file format assertions are
deliberately literal: the hop reads these files, and scripts/egress-hop-config.py writes them too,
so the three have to agree on the exact text.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent


def load_module(name: str, filename: str):
    path = SCRIPTS / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


prober = load_module("egress_prober", "egress-prober.py")

PASSWORD = "s3cret-not-in-any-output"
SENTINEL = prober.SENTINEL

OK = prober.ProbeOutcome(status=200, title="Example Domain")
CHALLENGE = prober.ProbeOutcome(status=403, title="Just a moment...")
BLOCKED = prober.ProbeOutcome(status=403, title="Attention Required! | Cloudflare")
BLOCKED_FRENCH = prober.ProbeOutcome(status=403, title="Acc&egrave;s restreint")
UNREACHABLE = prober.ProbeOutcome(status=None, error="ProbeError: cannot reach the proxy")
SERVER_ERROR = prober.ProbeOutcome(status=500, title="Server Error")

DEFAULT_UPSTREAMS = [
    {"name": "decodo-1", "addr": "isp.decodo.com:10001", "username": "someone", "password": PASSWORD},
    {"name": "decodo-2", "addr": "isp.decodo.com:10005", "username": "someone", "password": PASSWORD},
    {"name": "warp", "addr": "host.docker.internal:40001"},
]


def write_config(
    root: pathlib.Path,
    *,
    upstreams: list[dict] | None = None,
    fallback: str = "warp",
    preferred: str | None = None,
) -> pathlib.Path:
    """Write an upstreams configuration shaped like scripts/egress-hop-config.py reads."""
    secrets = root / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    config: dict = {"fallback": fallback, "upstreams": upstreams or DEFAULT_UPSTREAMS}
    if preferred is not None:
        config["preferred"] = preferred
    path = secrets / "egress-upstreams.json"
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def write_rules(root: pathlib.Path, name: str, entries: list[str]) -> pathlib.Path:
    rules_dir = root / "egress-rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / name
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return path


def write_rule(root: pathlib.Path, name: str, hosts: list[str]) -> pathlib.Path:
    """Write a rule file exactly as the prober would, so that unchanged detection is testable."""
    rules_dir = root / "egress-rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / name
    path.write_text(prober.render_rule_file(hosts), encoding="utf-8")
    return path


def read_rule(root: pathlib.Path, name: str) -> str:
    return (root / "egress-rules" / name).read_text(encoding="utf-8")


class FakeProbe:
    """A probe that answers from a table and records what it was asked."""

    def __init__(self, table: dict[tuple[str, str], prober.ProbeOutcome]) -> None:
        self.table = table
        self.calls: list[tuple[str, str]] = []

    def __call__(self, target, egress) -> prober.ProbeOutcome:
        self.calls.append((target.host, egress.name))
        return self.table.get((target.host, egress.name), UNREACHABLE)


class ProbeTargetTests(unittest.TestCase):
    def test_bare_host_defaults_to_https_443(self) -> None:
        target = prober.ProbeTarget.parse("theblank.net")
        self.assertEqual((target.host, target.port, target.scheme), ("theblank.net", 443, "https"))
        self.assertEqual(target.url(), "https://theblank.net/")
        self.assertEqual(target.entry(), "theblank.net")

    def test_scheme_and_port_are_honoured(self) -> None:
        target = prober.ProbeTarget.parse("http://example.com:8080")
        self.assertEqual((target.host, target.port, target.scheme), ("example.com", 8080, "http"))
        self.assertEqual(target.url(), "http://example.com:8080/")
        self.assertEqual(target.entry(), "example.com:8080")

    def test_http_scheme_uses_port_80(self) -> None:
        self.assertEqual(prober.ProbeTarget.parse("http://example.com").port, 80)

    def test_wildcards_ports_and_paths_are_rejected(self) -> None:
        for raw in ("*.example.com", "example.com/", "https://example.com/x", "ftp://example.com"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                prober.ProbeTarget.parse(raw)

    def test_bad_port_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            prober.ProbeTarget.parse("example.com:notaport")
        with self.assertRaises(ValueError):
            prober.ProbeTarget.parse("example.com:70000")


class ClassificationTests(unittest.TestCase):
    def test_ok_is_usable(self) -> None:
        self.assertEqual(prober.classify(OK), prober.Classification(usable=True, label="ok"))

    def test_challenge_is_usable(self) -> None:
        self.assertTrue(prober.classify(CHALLENGE).usable)
        self.assertEqual(prober.classify(CHALLENGE).label, "challenge")

    def test_hard_block_is_not_usable(self) -> None:
        self.assertEqual(prober.classify(BLOCKED), prober.Classification(usable=False, label="blocked"))

    def test_accented_and_entity_encoded_block_is_recognised(self) -> None:
        plain = prober.ProbeOutcome(status=403, title="Accès restreint")
        for outcome in (BLOCKED_FRENCH, plain):
            with self.subTest(title=outcome.title):
                self.assertFalse(prober.classify(outcome).usable)

    def test_marker_matching_ignores_case(self) -> None:
        self.assertFalse(prober.classify(prober.ProbeOutcome(status=403, title="ATTENTION REQUIRED")).usable)

    def test_transport_failure_is_not_usable(self) -> None:
        self.assertEqual(prober.classify(UNREACHABLE), prober.Classification(usable=False, label="error"))

    def test_server_error_is_not_usable(self) -> None:
        self.assertFalse(prober.classify(SERVER_ERROR).usable)
        self.assertEqual(prober.classify(SERVER_ERROR).label, "http-500")

    def test_unexplained_403_counts_as_a_solvable_challenge(self) -> None:
        outcome = prober.ProbeOutcome(status=403, title="")
        self.assertTrue(prober.classify(outcome).usable)

    def test_not_found_is_not_usable(self) -> None:
        self.assertFalse(prober.classify(prober.ProbeOutcome(status=404, title="Not Found")).usable)

    def test_body_is_consulted_only_when_the_response_has_no_title(self) -> None:
        block_in_body = prober.ProbeOutcome(status=403, title="", body="Attention Required! | Cloudflare")
        self.assertFalse(prober.classify(block_in_body).usable)
        challenge_with_link = prober.ProbeOutcome(
            status=403,
            title="Just a moment...",
            body="see https://example.com/attention-required for help",
        )
        self.assertTrue(prober.classify(challenge_with_link).usable)


class FileFormatTests(unittest.TestCase):
    def test_rule_file_matches_the_generator(self) -> None:
        self.assertEqual(
            prober.render_rule_file(["b.example", "a.example"]),
            "# One destination host per line. A host listed here leaves through this egress instead of the\n"
            "# fallback. Wildcards such as *.example.com are supported.\n"
            "a.example\nb.example\n",
        )

    def test_rule_file_keeps_the_sentinel_when_nothing_is_assigned(self) -> None:
        text = prober.render_rule_file([])
        self.assertTrue(text.startswith(prober.RULE_HEADER))
        self.assertIn(SENTINEL, text)

    def test_union_file_matches_the_generator_and_drops_the_sentinel(self) -> None:
        self.assertEqual(
            prober.render_union(["b.example", SENTINEL, "a.example"]),
            "# Generated: the union of the direct list and every egress rule file. A host listed here is\n"
            "# claimed by an egress, so it never falls back to the fallback upstream.\n"
            "a.example\nb.example\n",
        )

    def test_empty_union_is_header_only(self) -> None:
        self.assertEqual(prober.render_union([SENTINEL]), prober.UNION_HEADER)

    def test_union_file_name_follows_the_fallback(self) -> None:
        self.assertEqual(prober.union_file_name("warp"), "warp-exclude.txt")
        self.assertEqual(prober.union_file_name("primary"), "primary-exclude.txt")


class EgressConfigTests(unittest.TestCase):
    def test_upstreams_are_read_in_configuration_order_with_the_fallback_marked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory))
            egresses = prober.load_egresses(config)
        self.assertEqual([egress.name for egress in egresses.upstreams], ["decodo-1", "decodo-2", "warp"])
        self.assertEqual([egress.port for egress in egresses.upstreams], [10001, 10005, 40001])
        self.assertEqual([egress.name for egress in egresses.upstreams if egress.is_fallback], ["warp"])
        self.assertEqual(egresses.assignable(), ["decodo-1", "decodo-2"])

    def test_preferred_defaults_to_the_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), fallback="warp")
            egresses = prober.load_egresses(config)
        self.assertEqual(egresses.fallback, "warp")
        self.assertEqual(egresses.preferred, "warp")

    def test_mixed_providers_and_an_arbitrary_fallback_name(self) -> None:
        upstreams = [
            {"name": "alpha", "addr": "alpha.example:1080", "username": "u", "password": PASSWORD},
            {"name": "beta", "addr": "beta.example:1080"},
            {"name": "primary", "addr": "primary.example:1080"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), upstreams=upstreams, fallback="primary")
            egresses = prober.load_egresses(config)
        self.assertEqual(egresses.fallback, "primary")
        self.assertEqual(egresses.preferred, "primary")
        self.assertEqual(egresses.assignable(), ["alpha", "beta"])
        self.assertEqual(
            [(egress.name, egress.host, egress.port) for egress in egresses.upstreams],
            [("alpha", "alpha.example", 1080), ("beta", "beta.example", 1080), ("primary", "primary.example", 1080)],
        )

    def test_credentials_are_read_but_never_described(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory))
            egresses = prober.load_egresses(config)
        hosted = egresses.upstreams[0]
        self.assertEqual(hosted.username, "someone")
        self.assertEqual(hosted.password, PASSWORD)
        for egress in egresses.upstreams:
            self.assertNotIn(PASSWORD, egress.describe())
            self.assertNotIn(PASSWORD, repr(egress))

    def test_missing_configuration_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = pathlib.Path(directory) / "nope.json"
            with self.assertRaises(SystemExit):
                prober.load_egresses(missing)

    def test_configuration_without_a_fallback_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), fallback="absent")
            with self.assertRaises(SystemExit):
                prober.load_egresses(config)

    def test_preferred_naming_an_unknown_upstream_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), preferred="absent")
            with self.assertRaises(SystemExit):
                prober.load_egresses(config)

    def test_a_credential_bearing_address_is_refused_without_echoing_it(self) -> None:
        upstreams = [
            {"name": "bad", "addr": f"user:{PASSWORD}@proxy.example.net:10001"},
            {"name": "warp", "addr": "host.docker.internal:40001"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), upstreams=upstreams)
            with self.assertRaises(SystemExit) as caught:
                prober.load_egresses(config)
        # A credential in a malformed address must not reach the message that reports the failure.
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_a_url_address_is_refused_without_echoing_it(self) -> None:
        upstreams = [
            {"name": "bad", "addr": f"socks5://user:{PASSWORD}@proxy.example.net"},
            {"name": "warp", "addr": "host.docker.internal:40001"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(pathlib.Path(directory), upstreams=upstreams)
            with self.assertRaises(SystemExit) as caught:
                prober.load_egresses(config)
        self.assertNotIn(PASSWORD, str(caught.exception))

    def test_an_out_of_range_port_is_an_error(self) -> None:
        for addr in ("proxy.example.net:0", "proxy.example.net:70000", "proxy.example.net:port"):
            with self.subTest(addr=addr), tempfile.TemporaryDirectory() as directory:
                config = write_config(
                    pathlib.Path(directory),
                    upstreams=[
                        {"name": "bad", "addr": addr},
                        {"name": "warp", "addr": "host.docker.internal:40001"},
                    ],
                )
                with self.assertRaises(SystemExit):
                    prober.load_egresses(config)

    def test_a_plain_service_name_host_is_still_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = write_config(
                pathlib.Path(directory),
                upstreams=[{"name": "warp", "addr": "warp_bridge:40001"}],
            )
            egresses = prober.load_egresses(config)
        self.assertEqual((egresses.upstreams[0].host, egresses.upstreams[0].port), ("warp_bridge", 40001))


class ProberRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._temporary.name)
        self.config = write_config(self.root)
        self.rules_dir = self.root / "egress-rules"
        self.rules_dir.mkdir(parents=True, exist_ok=True)
        # The prober requires the mount, so every runnable test starts with the direct rule file.
        write_rules(self.root, prober.DIRECT_RULE_FILE, [])

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def build(
        self,
        table: dict[tuple[str, str], prober.ProbeOutcome],
        config: pathlib.Path | None = None,
    ) -> tuple[object, FakeProbe]:
        probe = FakeProbe(table)
        instance = prober.EgressProber(
            config_path=config or self.config,
            rules_dir=self.rules_dir,
            probe=probe,
        )
        return instance, probe

    def watch(self, hosts: list[str]) -> None:
        write_rules(self.root, prober.WATCH_FILE_NAME, hosts)

    def seed_rule_files(self, assigned: dict[str, list[str]] | None = None) -> None:
        """Create the rule files a deployment already has."""
        assigned = assigned or {}
        for name in ("decodo-1.txt", "decodo-2.txt"):
            write_rule(self.root, name, assigned.get(name, []))
        union_hosts = [host for hosts in assigned.values() for host in hosts]
        (self.rules_dir / prober.union_file_name("warp")).write_text(
            prober.render_union(union_hosts), encoding="utf-8"
        )

    def test_unassigned_host_prefers_the_preferred_candidate(self) -> None:
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "warp"): OK,
                ("a.example", "decodo-1"): OK,
                ("a.example", "decodo-2"): OK,
            }
        )
        report = instance.run()
        self.assertEqual(report.decisions[0].egress, "warp")
        self.assertEqual(report.decisions[0].label, "ok")
        self.assertFalse(report.decisions[0].changed)
        self.assertEqual(report.changes, 0)
        for name in ("decodo-1.txt", "decodo-2.txt"):
            self.assertNotIn("a.example", read_rule(self.root, name))

    def test_a_host_on_paid_recovers_to_the_preferred_candidate(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["a.example"])
        self.watch(["a.example"])
        instance, probe = self.build(
            {
                ("a.example", "warp"): OK,
                ("a.example", "decodo-1"): CHALLENGE,
            }
        )
        report = instance.run()
        decision = report.decisions[0]
        self.assertEqual((decision.egress, decision.changed), ("warp", True))
        self.assertIn("moved from decodo-1", decision.action)
        # The preferred candidate is probed first, before the egress the host is already on.
        self.assertEqual(probe.calls[0], ("a.example", "warp"))
        self.assertNotIn("a.example", read_rule(self.root, "decodo-1.txt"))
        self.assertIn(SENTINEL, read_rule(self.root, "decodo-1.txt"))

    def test_paid_assignment_is_sticky_while_the_preferred_candidate_is_unusable(self) -> None:
        self.seed_rule_files({"decodo-1.txt": ["a.example"]})
        self.watch(["a.example"])
        instance, probe = self.build(
            {
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-1"): CHALLENGE,
            }
        )
        report = instance.run()
        decision = report.decisions[0]
        self.assertEqual((decision.egress, decision.changed), ("decodo-1", False))
        self.assertIn("kept on decodo-1", decision.action)
        # Preferred first, then the current paid egress.
        self.assertEqual(probe.calls[:2], [("a.example", "warp"), ("a.example", "decodo-1")])
        self.assertEqual(report.written, [])

    def test_a_broken_assignment_moves_to_the_preferred_working_egress(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["a.example"])
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "decodo-1"): BLOCKED,
                ("a.example", "warp"): OK,
                ("a.example", "decodo-2"): OK,
            }
        )
        report = instance.run()
        decision = report.decisions[0]
        self.assertEqual((decision.egress, decision.label, decision.changed), ("warp", "ok", True))
        self.assertIn("moved from decodo-1", decision.action)
        self.assertNotIn("a.example", read_rule(self.root, "decodo-1.txt"))
        self.assertIn(SENTINEL, read_rule(self.root, "decodo-1.txt"))

    def test_a_broken_assignment_falls_through_to_the_next_upstream(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["a.example"])
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "decodo-1"): BLOCKED,
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-2"): CHALLENGE,
            }
        )
        report = instance.run()
        self.assertEqual(report.decisions[0].egress, "decodo-2")
        self.assertTrue(report.decisions[0].changed)
        self.assertIn("a.example", read_rule(self.root, "decodo-2.txt"))
        self.assertIn(SENTINEL, read_rule(self.root, "decodo-1.txt"))

    def test_a_transport_failure_is_reported_with_its_reason(self) -> None:
        self.watch(["a.example"])
        instance, _ = self.build({})
        report = instance.run()
        decision = report.decisions[0]
        self.assertEqual(decision.label, "error")
        self.assertIn("cannot reach the proxy", decision.detail)
        self.assertIn("cannot reach the proxy", prober.render_report(report))

    def test_a_report_detail_is_shortened_to_one_line(self) -> None:
        messy = prober.ProbeOutcome(status=None, error="first\nsecond   third")
        self.assertEqual(prober.shorten(messy.error), "first second third")
        self.assertEqual(prober.shorten("x" * 200).endswith("..."), True)
        self.assertEqual(len(prober.shorten("x" * 200)), 100)

    def test_no_usable_egress_leaves_the_assignment_alone(self) -> None:
        self.seed_rule_files({"decodo-1.txt": ["a.example"]})
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "decodo-1"): BLOCKED,
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-2"): UNREACHABLE,
            }
        )
        report = instance.run()
        decision = report.decisions[0]
        self.assertEqual(decision.egress, "decodo-1")
        self.assertEqual(decision.label, "blocked")
        self.assertFalse(decision.changed)
        self.assertIn("no usable egress", decision.action)
        self.assertIn("a.example", read_rule(self.root, "decodo-1.txt"))
        self.assertEqual(report.written, [])

    def test_union_is_written_from_the_assignments(self) -> None:
        write_rule(self.root, "decodo-1.txt", [])
        self.watch(["a.example", "b.example"])
        instance, _ = self.build(
            {
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-1"): CHALLENGE,
                ("b.example", "warp"): BLOCKED,
                ("b.example", "decodo-1"): OK,
            }
        )
        instance.run()
        union = read_rule(self.root, prober.union_file_name("warp"))
        self.assertEqual(union, prober.UNION_HEADER + "a.example\nb.example\n")
        self.assertNotIn(SENTINEL, union)

    def test_union_carries_the_direct_hosts_and_drops_the_sentinel(self) -> None:
        write_rules(self.root, prober.DIRECT_RULE_FILE, ["github.test"])
        write_rule(self.root, "decodo-1.txt", [])
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-1"): CHALLENGE,
            }
        )
        instance.run()
        union = read_rule(self.root, prober.union_file_name("warp"))
        self.assertEqual(union, prober.UNION_HEADER + "a.example\ngithub.test\n")

    def test_a_direct_host_is_never_probed_or_assigned(self) -> None:
        write_rules(self.root, prober.DIRECT_RULE_FILE, ["github.test"])
        self.watch(["github.test", "a.example"])
        instance, probe = self.build(
            {
                ("github.test", "warp"): BLOCKED,
                ("github.test", "decodo-1"): CHALLENGE,
                ("a.example", "warp"): OK,
            }
        )
        report = instance.run()
        self.assertEqual([decision.host for decision in report.decisions], ["a.example"])
        self.assertNotIn("github.test", {host for host, _ in probe.calls})
        for name in ("decodo-1.txt", "decodo-2.txt"):
            self.assertNotIn("github.test", read_rule(self.root, name))
        self.assertIn("github.test", read_rule(self.root, prober.union_file_name("warp")))

    def test_a_duplicate_entry_lands_in_one_file_only(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["dup.example"])
        write_rule(self.root, "decodo-2.txt", ["dup.example"])
        instance, _ = self.build(
            {
                ("dup.example", "warp"): BLOCKED,
                ("dup.example", "decodo-1"): CHALLENGE,
                ("dup.example", "decodo-2"): OK,
            }
        )
        report = instance.run()
        # The rule files already recorded it under decodo-1 (first file wins) and the preferred
        # upstream cannot serve it, so it stays there and decodo-2 becomes inert.
        self.assertEqual(report.decisions[0].egress, "decodo-1")
        self.assertIn("dup.example", read_rule(self.root, "decodo-1.txt"))
        self.assertNotIn("dup.example", read_rule(self.root, "decodo-2.txt"))
        self.assertIn(SENTINEL, read_rule(self.root, "decodo-2.txt"))
        self.assertNotIn(SENTINEL, read_rule(self.root, prober.union_file_name("warp")))

    def test_a_wildcard_in_one_file_refuses_a_concrete_host_from_another(self) -> None:
        # decodo-1 holds *.example.com while the watched foo.example.com would move onto the
        # preferred decodo-2. The pair can match the same host, so the write is refused before any
        # file is touched rather than published for gost to hot-reload.
        write_rule(self.root, "decodo-1.txt", ["*.example.com"])
        write_config(self.root, preferred="decodo-2")
        self.watch(["foo.example.com"])
        instance, _ = self.build({("foo.example.com", "decodo-2"): OK})
        before = {path.name: path.read_text(encoding="utf-8") for path in self.rules_dir.iterdir()}
        with self.assertRaises(SystemExit) as caught:
            instance.run()
        message = str(caught.exception)
        self.assertIn("foo.example.com", message)
        self.assertIn("*.example.com", message)
        after = {path.name: path.read_text(encoding="utf-8") for path in self.rules_dir.iterdir()}
        self.assertEqual(before, after)
        self.assertFalse((self.rules_dir / "decodo-2.txt").exists())
        self.assertFalse((self.rules_dir / prober.union_file_name("warp")).exists())

    def test_an_apex_and_its_wildcard_in_different_files_still_write(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["*.example.com"])
        write_rule(self.root, "decodo-2.txt", ["example.com"])
        instance, _ = self.build({})
        report = instance.run()
        self.assertFalse(report.dry_run)
        self.assertIn("*.example.com", read_rule(self.root, "decodo-1.txt"))
        self.assertIn("example.com", read_rule(self.root, "decodo-2.txt"))

    def test_unchanged_files_are_not_rewritten(self) -> None:
        self.seed_rule_files({"decodo-1.txt": ["a.example"]})
        self.watch(["a.example"])
        table = {("a.example", "warp"): BLOCKED, ("a.example", "decodo-1"): CHALLENGE}
        first, _ = self.build(table)
        first_report = first.run()
        self.assertEqual(first_report.written, [])
        before = {
            path.name: (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
            for path in self.rules_dir.iterdir()
        }
        second, _ = self.build(table)
        second_report = second.run()
        self.assertEqual(second_report.written, [])
        self.assertTrue(second_report.unchanged)
        after = {
            path.name: (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
            for path in self.rules_dir.iterdir()
        }
        self.assertEqual(before, after)

    def test_dry_run_writes_nothing(self) -> None:
        self.watch(["a.example"])
        instance, _ = self.build(
            {
                ("a.example", "warp"): BLOCKED,
                ("a.example", "decodo-1"): CHALLENGE,
            }
        )
        report = instance.run(dry_run=True)
        self.assertTrue(report.dry_run)
        self.assertEqual(report.written, [])
        self.assertEqual(report.decisions[0].egress, "decodo-1")
        self.assertEqual(
            sorted(path.name for path in self.rules_dir.iterdir()),
            [prober.DIRECT_RULE_FILE, prober.WATCH_FILE_NAME],
        )

    def test_a_run_report_never_contains_a_credential(self) -> None:
        self.watch(["a.example"])
        instance, _ = self.build({("a.example", "warp"): OK})
        report = instance.run()
        # Egress.describe() is what fills report.egresses, so the whole path from config to report
        # is covered here, not just the dataclass in isolation.
        text = prober.render_report(report)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(PASSWORD, " ".join(report.egresses))

    def test_dry_run_refuses_an_overlap_like_a_real_run(self) -> None:
        # A wildcard in one file and a concrete host the prober would move onto another can match
        # the same host, so a real run refuses the write. A dry run validates the same plan, so it
        # must refuse too rather than report success for a mapping a real run aborts.
        write_rule(self.root, "decodo-1.txt", ["*.example.com"])
        write_config(self.root, preferred="decodo-2")
        self.watch(["foo.example.com"])
        instance, _ = self.build({("foo.example.com", "decodo-2"): OK})
        before = {path.name: path.read_text(encoding="utf-8") for path in self.rules_dir.iterdir()}
        with self.assertRaises(SystemExit) as caught:
            instance.run(dry_run=True)
        message = str(caught.exception)
        self.assertIn("foo.example.com", message)
        self.assertIn("*.example.com", message)
        after = {path.name: path.read_text(encoding="utf-8") for path in self.rules_dir.iterdir()}
        self.assertEqual(before, after)
        self.assertFalse((self.rules_dir / "decodo-2.txt").exists())
        self.assertFalse((self.rules_dir / prober.union_file_name("warp")).exists())

    def test_only_host_limits_the_probes(self) -> None:
        self.watch(["a.example", "b.example", "*.wild.example"])
        instance, probe = self.build(
            {
                ("a.example", "warp"): OK,
                ("b.example", "warp"): OK,
            }
        )
        report = instance.run(only_host="b.example")
        self.assertEqual([decision.host for decision in report.decisions], ["b.example"])
        self.assertEqual({host for host, _ in probe.calls}, {"b.example"})
        self.assertEqual(report.watched_but_not_probed, [])

    def test_assigned_hosts_are_watched_even_when_not_listed(self) -> None:
        write_rule(self.root, "decodo-2.txt", ["assigned.example"])
        instance, probe = self.build(
            {
                ("assigned.example", "warp"): BLOCKED,
                ("assigned.example", "decodo-2"): CHALLENGE,
            }
        )
        report = instance.run()
        self.assertEqual([decision.host for decision in report.decisions], ["assigned.example"])
        self.assertEqual(report.decisions[0].egress, "decodo-2")
        self.assertIn(("assigned.example", "decodo-2"), probe.calls)

    def test_an_assigned_wildcard_is_preserved_and_not_probed(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["*.wild.example", "plain.example"])
        self.watch(["plain.example"])
        instance, probe = self.build(
            {("plain.example", "warp"): BLOCKED, ("plain.example", "decodo-1"): CHALLENGE}
        )
        report = instance.run()
        self.assertEqual({host for host, _ in probe.calls}, {"plain.example"})
        content = read_rule(self.root, "decodo-1.txt")
        self.assertIn("*.wild.example", content)
        self.assertIn("plain.example", content)
        self.assertIn("*.wild.example", read_rule(self.root, prober.union_file_name("warp")))

    def test_a_watch_file_wildcard_is_reported_rather_than_probed(self) -> None:
        self.watch(["plain.example", "*.wild.example"])
        instance, probe = self.build({("plain.example", "warp"): OK})
        report = instance.run()
        self.assertEqual(report.watched_but_not_probed, ["*.wild.example"])
        self.assertEqual({host for host, _ in probe.calls}, {"plain.example"})
        text = prober.render_report(report)
        self.assertIn("*.wild.example", text)
        self.assertIn("1 not probeable", text)

    def test_a_watch_file_typo_is_an_error(self) -> None:
        self.watch(["not a host"])
        instance, _ = self.build({})
        with self.assertRaises(SystemExit):
            instance.run()

    def test_a_narrowed_run_leaves_other_assignments_alone(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["kept.example", "looked.example"])
        instance, probe = self.build(
            {
                ("kept.example", "decodo-1"): CHALLENGE,
                ("looked.example", "warp"): BLOCKED,
                ("looked.example", "decodo-1"): BLOCKED,
                ("looked.example", "decodo-2"): OK,
            }
        )
        report = instance.run(only_host="looked.example")
        self.assertEqual([decision.host for decision in report.decisions], ["looked.example"])
        self.assertEqual({host for host, _ in probe.calls}, {"looked.example"})
        content = read_rule(self.root, "decodo-1.txt")
        self.assertIn("kept.example", content)
        self.assertNotIn("looked.example", content)

    def test_watch_file_entry_wins_over_the_rule_file_form(self) -> None:
        write_rule(self.root, "decodo-1.txt", ["a.example"])
        self.watch(["http://a.example"])
        instance, probe = self.build(
            {("a.example", "warp"): BLOCKED, ("a.example", "decodo-1"): CHALLENGE}
        )
        report = instance.run()
        self.assertEqual(len(probe.calls), 2)
        self.assertEqual(report.decisions[0].entry, "a.example")

    def test_a_wildcard_direct_rule_excludes_matching_concrete_hosts(self) -> None:
        write_rules(self.root, prober.DIRECT_RULE_FILE, ["*.githubusercontent.com"])
        self.watch(["raw.githubusercontent.com", "other.example"])
        instance, probe = self.build(
            {
                ("raw.githubusercontent.com", "warp"): CHALLENGE,
                ("other.example", "warp"): OK,
            }
        )
        report = instance.run()
        self.assertEqual([decision.host for decision in report.decisions], ["other.example"])
        self.assertNotIn("raw.githubusercontent.com", {host for host, _ in probe.calls})
        for name in ("decodo-1.txt", "decodo-2.txt"):
            self.assertNotIn("raw.githubusercontent.com", read_rule(self.root, name))
        union = read_rule(self.root, prober.union_file_name("warp"))
        self.assertIn("*.githubusercontent.com", union)
        self.assertNotIn("raw.githubusercontent.com", union)

    def test_a_missing_rules_directory_fails_closed(self) -> None:
        missing = self.root / "not-mounted"
        probe = FakeProbe({})
        instance = prober.EgressProber(config_path=self.config, rules_dir=missing, probe=probe)
        with self.assertRaises(SystemExit):
            instance.run()
        self.assertEqual(probe.calls, [])
        self.assertFalse(missing.exists())

    def test_a_missing_direct_rule_file_fails_closed(self) -> None:
        (self.rules_dir / prober.DIRECT_RULE_FILE).unlink()
        self.watch(["a.example"])
        instance, probe = self.build({("a.example", "warp"): OK})
        with self.assertRaises(SystemExit):
            instance.run()
        self.assertEqual(probe.calls, [])
        self.assertFalse((self.rules_dir / "decodo-1.txt").exists())

    def test_an_intentionally_empty_watch_list_is_still_valid(self) -> None:
        self.watch([])
        instance, probe = self.build({})
        report = instance.run()
        self.assertEqual(report.decisions, [])
        self.assertEqual(probe.calls, [])

    def test_no_watch_file_and_no_assignments_is_quiet(self) -> None:
        instance, probe = self.build({})
        report = instance.run()
        self.assertEqual(report.decisions, [])
        self.assertEqual(probe.calls, [])


class ReportTests(unittest.TestCase):
    def test_report_never_contains_a_credential(self) -> None:
        report = prober.Report(
            egresses=["warp (host.docker.internal:40001)", "decodo-1 (isp.decodo.com:10001)"],
            decisions=[prober.Decision("a.example", "decodo-1", "challenge", False, "kept on decodo-1")],
            watched_but_not_probed=["*.wild.example"],
            written=[],
            unchanged=[],
            dry_run=True,
        )
        text = prober.render_report(report)
        self.assertNotIn(PASSWORD, text)
        self.assertIn("dry run", text)
        self.assertIn("changes: 0", text)
        self.assertIn("a.example", text)
        self.assertIn("*.wild.example", text)

    def test_changes_are_counted(self) -> None:
        report = prober.Report(
            egresses=["warp"],
            decisions=[
                prober.Decision("a.example", "warp", "ok", True, "moved from decodo-1"),
                prober.Decision("b.example", "warp", "ok", False, "kept on warp"),
            ],
            watched_but_not_probed=[],
            written=["x"],
            unchanged=[],
            dry_run=False,
        )
        self.assertEqual(report.changes, 1)
        self.assertIn("changes: 1", prober.render_report(report))


if __name__ == "__main__":
    unittest.main()
