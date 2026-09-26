#!/usr/bin/env python3
"""Tests for scripts/egress-hop-config.py.

Nothing here touches the network. The header assertions are deliberately literal, because the
prober rewrites the same files and the two scripts have to agree on the exact text.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import unittest.mock

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


hop = load_module("egress_hop_config", "egress-hop-config.py")
prober = load_module("egress_prober", "egress-prober.py")
migrate = load_module("migrate_egress_upstreams", "migrate-egress-upstreams.py")


def write_config(
    path: pathlib.Path,
    *,
    fallback: str = "warp",
    preferred: str | None = None,
    upstreams: list[dict] | None = None,
) -> pathlib.Path:
    if upstreams is None:
        upstreams = [
            {"name": "isp-1", "addr": "proxy.example.net:10001", "username": "u", "password": "p"},
            {"name": "isp-2", "addr": "proxy.example.net:10005"},
            {"name": "warp", "addr": "host.docker.internal:40001"},
        ]
    config: dict = {"fallback": fallback, "upstreams": upstreams}
    if preferred is not None:
        config["preferred"] = preferred
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return path


def upstream(name: str, addr: str, username: str = "", password: str = "") -> "hop.Upstream":
    host, _, port = addr.rpartition(":")
    return hop.Upstream(name=name, host=host, port=int(port), username=username, password=password)


def bypass_by_name(config: dict, name: str) -> dict:
    return next(bypass for bypass in config["bypasses"] if bypass["name"] == name)


def node_by_name(config: dict, name: str) -> dict:
    nodes = config["chains"][0]["hops"][0]["nodes"]
    return next(node for node in nodes if node["name"] == name)


class HeaderAgreementTests(unittest.TestCase):
    def test_rule_header_matches_the_prober(self) -> None:
        self.assertEqual(hop.RULE_HEADER, prober.RULE_HEADER)

    def test_union_header_matches_the_prober(self) -> None:
        self.assertEqual(hop.UNION_HEADER, prober.UNION_HEADER)

    def test_sentinel_note_matches_the_prober(self) -> None:
        self.assertEqual(hop.SENTINEL_NOTE, prober.SENTINEL_NOTE)
        self.assertEqual(hop.SENTINEL, prober.SENTINEL)

    def test_union_file_name_matches_the_prober(self) -> None:
        self.assertEqual(hop.union_file_name("warp"), prober.union_file_name("warp"))


class ReadUpstreamsTests(unittest.TestCase):
    def read(self, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(pathlib.Path(directory) / "egress-upstreams.json", **kwargs)
            return hop.read_upstreams(path)

    def test_reads_named_upstreams_and_the_fallback(self) -> None:
        upstreams, fallback = self.read()
        self.assertEqual(fallback, "warp")
        self.assertEqual([item.name for item in upstreams], ["isp-1", "isp-2", "warp"])
        self.assertEqual((upstreams[0].host, upstreams[0].port), ("proxy.example.net", 10001))
        self.assertEqual((upstreams[0].username, upstreams[0].password), ("u", "p"))
        self.assertEqual((upstreams[1].username, upstreams[1].password), ("", ""))

    def test_mixed_provider_endpoints_are_accepted(self) -> None:
        upstreams, _ = self.read(
            upstreams=[
                {"name": "alpha", "addr": "alpha.example:1080", "username": "u", "password": "p"},
                {"name": "beta", "addr": "beta.example:3128"},
                {"name": "warp", "addr": "host.docker.internal:40001"},
            ]
        )
        self.assertEqual([item.addr for item in upstreams], ["alpha.example:1080", "beta.example:3128", "host.docker.internal:40001"])

    def test_an_arbitrary_fallback_name_is_accepted(self) -> None:
        _, fallback = self.read(
            fallback="primary",
            upstreams=[
                {"name": "alpha", "addr": "alpha.example:1080"},
                {"name": "primary", "addr": "primary.example:1080"},
            ],
        )
        self.assertEqual(fallback, "primary")

    def test_missing_file_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            hop.read_upstreams(pathlib.Path(tempfile.gettempdir()) / "definitely-absent.json")

    def test_unknown_fallback_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(fallback="absent")

    def test_unknown_preferred_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(preferred="absent")

    def test_duplicate_names_are_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(
                upstreams=[
                    {"name": "dup", "addr": "a.example:1080"},
                    {"name": "dup", "addr": "b.example:1080"},
                ]
            )

    def test_reserved_names_are_an_error(self) -> None:
        for reserved in ("direct", "direct-hosts", "warp-exclude"):
            with self.subTest(reserved=reserved), self.assertRaises(SystemExit):
                self.read(upstreams=[{"name": reserved, "addr": "a.example:1080"}])

    def test_a_bad_port_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(upstreams=[{"name": "bad", "addr": "a.example:0"}])
        with self.assertRaises(SystemExit):
            self.read(upstreams=[{"name": "bad", "addr": "a.example:port"}])

    def test_half_credentials_are_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            self.read(upstreams=[{"name": "half", "addr": "a.example:1080", "username": "u"}])

    def test_a_malformed_address_never_echoes_its_content(self) -> None:
        secret = "s3cret-not-in-any-output"
        with self.assertRaises(SystemExit) as caught:
            hop.parse_addr(f"socks5://user:{secret}@host", "isp-1")
        self.assertNotIn(secret, str(caught.exception))

    def test_a_credential_bearing_host_is_rejected_without_echoing_it(self) -> None:
        secret = "s3cret-not-in-any-output"
        with self.assertRaises(SystemExit) as caught:
            hop.parse_addr(f"user:{secret}@proxy.example.net:10001", "isp-1")
        self.assertNotIn(secret, str(caught.exception))

    def test_a_plain_service_name_host_is_still_accepted(self) -> None:
        self.assertEqual(hop.parse_addr("warp_bridge:40001", "warp"), ("warp_bridge", 40001))

    def test_read_upstreams_never_echoes_a_malformed_address(self) -> None:
        secret = "s3cret-not-in-any-output"
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                pathlib.Path(directory) / "egress-upstreams.json",
                upstreams=[
                    {"name": "bad", "addr": f"socks5://user:{secret}@proxy.example.net"},
                    {"name": "warp", "addr": "host.docker.internal:40001"},
                ],
            )
            with self.assertRaises(SystemExit) as caught:
                hop.read_upstreams(path)
        self.assertNotIn(secret, str(caught.exception))

    def test_read_upstreams_rejects_a_credential_bearing_host(self) -> None:
        secret = "s3cret-not-in-any-output"
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(
                pathlib.Path(directory) / "egress-upstreams.json",
                upstreams=[
                    {"name": "bad", "addr": f"user:{secret}@proxy.example.net:10001"},
                    {"name": "warp", "addr": "host.docker.internal:40001"},
                ],
            )
            with self.assertRaises(SystemExit) as caught:
                hop.read_upstreams(path)
        self.assertNotIn(secret, str(caught.exception))


class BuildTests(unittest.TestCase):
    def test_the_direct_node_is_first_and_the_fallback_last(self) -> None:
        config = hop.build(
            [upstream("isp-1", "a.example:10001"), upstream("isp-2", "b.example:10005"), upstream("warp", "p.example:40001")],
            "warp",
            "/rules",
        )
        names = [node["name"] for node in config["chains"][0]["hops"][0]["nodes"]]
        self.assertEqual(names, ["direct", "isp-1", "isp-2", "warp"])

    def test_mixed_provider_endpoints_keep_their_addresses(self) -> None:
        config = hop.build(
            [upstream("alpha", "alpha.example:1080"), upstream("beta", "beta.example:3128"), upstream("primary", "primary.example:1080")],
            "primary",
            "/rules",
        )
        self.assertEqual(node_by_name(config, "alpha")["addr"], "alpha.example:1080")
        self.assertEqual(node_by_name(config, "beta")["addr"], "beta.example:3128")
        self.assertEqual(node_by_name(config, "primary")["addr"], "primary.example:1080")

    def test_auth_is_only_added_when_credentials_are_set(self) -> None:
        config = hop.build(
            [upstream("isp-1", "a.example:1080", "u", "p"), upstream("isp-2", "b.example:1080"), upstream("warp", "p.example:40001")],
            "warp",
            "/rules",
        )
        self.assertEqual(node_by_name(config, "isp-1")["connector"]["auth"], {"username": "u", "password": "p"})
        self.assertNotIn("auth", node_by_name(config, "isp-2")["connector"])

    def test_only_the_fallback_bypass_is_a_complement(self) -> None:
        config = hop.build(
            [upstream("isp-1", "a.example:1080"), upstream("isp-2", "b.example:1080"), upstream("warp", "p.example:40001")],
            "warp",
            "/rules",
        )
        self.assertEqual(node_by_name(config, "warp")["bypass"], "warp-exclude")
        self.assertNotIn("whitelist", bypass_by_name(config, "warp-exclude"))
        for name in ("direct-hosts", "isp-1", "isp-2"):
            self.assertTrue(bypass_by_name(config, name)["whitelist"])

    def test_an_arbitrary_fallback_names_its_own_complement_bypass(self) -> None:
        config = hop.build(
            [upstream("alpha", "a.example:1080"), upstream("primary", "p.example:1080")],
            "primary",
            "/rules",
        )
        self.assertEqual(node_by_name(config, "primary")["bypass"], "primary-exclude")
        self.assertEqual(
            bypass_by_name(config, "primary-exclude")["file"]["path"],
            "/rules/primary-exclude.txt",
        )

    def test_bypass_files_point_into_the_rules_directory(self) -> None:
        config = hop.build(
            [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
            "warp",
            "/rules",
        )
        self.assertEqual(bypass_by_name(config, "isp-1")["file"]["path"], "/rules/isp-1.txt")
        self.assertEqual(bypass_by_name(config, "direct-hosts")["file"]["path"], "/rules/direct-hosts.txt")


class MainTests(unittest.TestCase):
    def test_rules_local_defaults_to_the_live_directory(self) -> None:
        self.assertEqual(hop.parse_args([]).rules_local, str(hop.REPO / "egress-rules"))

    def test_main_writes_rule_files_to_the_rules_local_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            upstreams_path = write_config(root / "egress-upstreams.json")
            config_out = root / "egress-hop.json"
            rules_local = root / "stage"
            hop.main(
                [
                    "--upstreams", str(upstreams_path),
                    "--config-out", str(config_out),
                    "--rules-local", str(rules_local),
                ]
            )
            self.assertTrue((rules_local / hop.DIRECT_RULE_FILE).exists())
            self.assertTrue((rules_local / hop.union_file_name("warp")).exists())
            self.assertTrue(config_out.exists())


class RuleFileTests(unittest.TestCase):
    def test_an_unused_upstream_is_seeded_with_the_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            hop.write_rule_files(
                [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
                "warp",
                rules,
            )
            isp = (rules / "isp-1.txt").read_text(encoding="utf-8")
        self.assertTrue(isp.startswith(hop.RULE_HEADER))
        self.assertIn(hop.SENTINEL, isp)
        self.assertIn("No hosts assigned", isp)

    def test_the_fallback_gets_no_rule_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            hop.write_rule_files([upstream("warp", "p.example:40001")], "warp", rules)
            self.assertFalse((rules / "warp.txt").exists())

    def test_the_direct_file_is_seeded_with_the_github_defaults_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            hop.write_rule_files([upstream("warp", "p.example:40001")], "warp", rules)
            direct = (rules / "direct-hosts.txt").read_text(encoding="utf-8")
        self.assertTrue(direct.startswith(hop.DIRECT_HEADER))
        self.assertIn("github.com", direct)
        self.assertIn("*.githubusercontent.com", direct)

    def test_a_hand_edited_direct_file_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "direct-hosts.txt").write_text("# mine\nmyhost.example\n", encoding="utf-8")
            hop.write_rule_files([upstream("warp", "p.example:40001")], "warp", rules)
            direct = (rules / "direct-hosts.txt").read_text(encoding="utf-8")
        self.assertEqual(direct, "# mine\nmyhost.example\n")

    def test_the_union_is_the_direct_and_provider_hosts_without_the_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "isp-1.txt").write_text("provider.example\n", encoding="utf-8")
            (rules / "direct-hosts.txt").write_text("github.com\n", encoding="utf-8")
            hop.write_rule_files(
                [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
                "warp",
                rules,
            )
            union = (rules / "warp-exclude.txt").read_text(encoding="utf-8")
        self.assertEqual(union, hop.UNION_HEADER + "github.com\nprovider.example\n")

    def test_an_existing_provider_file_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "isp-1.txt").write_text(hop.RULE_HEADER + "kept.example\n", encoding="utf-8")
            hop.write_rule_files(
                [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
                "warp",
                rules,
            )
            isp = (rules / "isp-1.txt").read_text(encoding="utf-8")
        self.assertIn("kept.example", isp)

    def test_two_upstreams_claiming_the_same_host_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "isp-1.txt").write_text("dup.example\n", encoding="utf-8")
            (rules / "isp-2.txt").write_text("dup.example\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as caught:
                hop.write_rule_files(
                    [
                        upstream("isp-1", "a.example:1080"),
                        upstream("isp-2", "b.example:1080"),
                        upstream("warp", "p.example:40001"),
                    ],
                    "warp",
                    rules,
                )
        message = str(caught.exception)
        self.assertIn("dup.example", message)
        self.assertIn("isp-1.txt", message)
        self.assertIn("isp-2.txt", message)

    def test_a_host_claimed_by_the_direct_list_and_an_upstream_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / hop.DIRECT_RULE_FILE).write_text("github.com\n", encoding="utf-8")
            (rules / "isp-1.txt").write_text("github.com\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                hop.write_rule_files(
                    [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
                    "warp",
                    rules,
                )
            self.assertEqual((rules / hop.DIRECT_RULE_FILE).read_text(encoding="utf-8"), "github.com\n")

    def test_a_wildcard_covering_a_host_in_another_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / hop.DIRECT_RULE_FILE).write_text("*.githubusercontent.com\n", encoding="utf-8")
            (rules / "isp-1.txt").write_text("raw.githubusercontent.com\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                hop.write_rule_files(
                    [upstream("isp-1", "a.example:1080"), upstream("warp", "p.example:40001")],
                    "warp",
                    rules,
                )

    def test_an_apex_and_its_wildcard_may_live_in_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "isp-1.txt").write_text("*.example.com\n", encoding="utf-8")
            (rules / "isp-2.txt").write_text("example.com\n", encoding="utf-8")
            hop.write_rule_files(
                [
                    upstream("isp-1", "a.example:1080"),
                    upstream("isp-2", "b.example:1080"),
                    upstream("warp", "p.example:40001"),
                ],
                "warp",
                rules,
            )
            union = (rules / hop.union_file_name("warp")).read_text(encoding="utf-8")
        self.assertIn("*.example.com", union)
        self.assertIn("example.com", union)

    def test_the_union_is_not_written_when_rule_files_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rules = pathlib.Path(directory)
            (rules / "isp-1.txt").write_text("dup.example\n", encoding="utf-8")
            (rules / "isp-2.txt").write_text("dup.example\n", encoding="utf-8")
            union = rules / hop.union_file_name("warp")
            union.write_text("stale\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                hop.write_rule_files(
                    [
                        upstream("isp-1", "a.example:1080"),
                        upstream("isp-2", "b.example:1080"),
                        upstream("warp", "p.example:40001"),
                    ],
                    "warp",
                    rules,
                )
            self.assertEqual(union.read_text(encoding="utf-8"), "stale\n")


class WildcardAgreementTests(unittest.TestCase):
    CASES = [
        ("*.example.com", "foo.example.com", True),
        ("*.example.com", "a.b.example.com", True),
        ("*.example.com", "example.com", False),
        ("example.com", "example.com", True),
        ("example.com", "foo.example.com", False),
        ("*.example.com", "*.example.com", True),
        ("GITHUB.com", "github.com", True),
    ]

    def test_wildcard_matching_agrees_with_the_prober(self) -> None:
        for pattern, host, expected in self.CASES:
            with self.subTest(pattern=pattern, host=host):
                self.assertEqual(hop.host_matches(pattern, host), expected)
                self.assertEqual(prober.host_matches(pattern, host), expected)

    def test_an_apex_is_not_covered_by_its_wildcard(self) -> None:
        self.assertFalse(hop.entries_overlap("*.example.com", "example.com"))
        self.assertTrue(hop.entries_overlap("*.example.com", "foo.example.com"))
        self.assertTrue(hop.entries_overlap("dup.example", "dup.example"))
        self.assertFalse(hop.entries_overlap("a.example", "b.example"))

    def test_entries_overlap_agrees_with_the_prober(self) -> None:
        pairs = [
            ("*.example.com", "foo.example.com"),
            ("example.com", "*.example.com"),
            ("*.a.example.com", "*.example.com"),
            ("a.example", "b.example"),
            ("dup.example", "dup.example"),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertEqual(hop.entries_overlap(left, right), prober.entries_overlap(left, right))


class PrivateWriteTests(unittest.TestCase):
    def test_the_config_is_created_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "egress-hop.json"
            modes = self._capture_open_modes(lambda: hop.write_private_file(path, "{}\n"))
            self.assertEqual(modes, [0o600])
            self.assertEqual(path.read_text(encoding="utf-8"), "{}\n")

    def test_the_migrated_config_is_created_with_private_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "egress-upstreams.json"
            modes = self._capture_open_modes(lambda: migrate.write_private_file(path, "{}\n"))
            self.assertEqual(modes, [0o600])

    def test_a_failed_replace_keeps_the_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "egress-hop.json"
            path.write_text("old\n", encoding="utf-8")
            with unittest.mock.patch("os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    hop.write_private_file(path, "new\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "old\n")
            self.assertEqual([entry.name for entry in pathlib.Path(directory).iterdir()], [path.name])

    def test_a_pre_existing_permissive_tmp_cannot_receive_the_secret(self) -> None:
        secret = '{"username": "someone", "password": "s3cret-not-in-any-output"}'
        for module in (hop, migrate):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory)
                path = root / "egress-hop.json"
                decoy = path.with_name(f"{path.name}.tmp")
                decoy.write_text("attacker\n", encoding="utf-8")
                os.chmod(decoy, 0o644)
                module.write_private_file(path, secret + "\n")
                self.assertEqual(decoy.read_text(encoding="utf-8"), "attacker\n")
                self.assertNotIn("s3cret-not-in-any-output", decoy.read_text(encoding="utf-8"))
                self.assertEqual(path.read_text(encoding="utf-8"), secret + "\n")

    def test_no_temporary_file_is_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "egress-hop.json"
            hop.write_private_file(path, "{}\n")
            self.assertEqual([entry.name for entry in pathlib.Path(directory).iterdir()], [path.name])

    @staticmethod
    def _capture_open_modes(action) -> list[int]:
        """Run *action*, returning the mode each ``os.open`` call was created with."""
        modes: list[int] = []
        real_open = os.open

        def spy(target, flags, mode=0o777):
            modes.append(mode)
            return real_open(target, flags, mode)

        with unittest.mock.patch("os.open", side_effect=spy):
            action()
        return modes


if __name__ == "__main__":
    unittest.main()
