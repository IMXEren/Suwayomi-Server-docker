"""Scheduling tests; no egress probes or network calls."""
from __future__ import annotations

import importlib.util
import pathlib
import unittest
from unittest.mock import patch

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "egress-prober-loop.py"
spec = importlib.util.spec_from_file_location("egress_prober_loop", SCRIPT)
assert spec is not None and spec.loader is not None
loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop)


class ProberScheduleTests(unittest.TestCase):
    def test_uniform_range_and_elapsed_runtime(self) -> None:
        with patch.object(loop.secrets, "randbelow", side_effect=[0, 900, 1800]) as draw:
            self.assertEqual(loop.next_sleep(3600, 60), 2640)
            self.assertEqual(loop.next_sleep(3600, 60), 3540)
            self.assertEqual(loop.next_sleep(3600, 60), 4440)
        self.assertEqual(draw.call_count, 3)
        draw.assert_called_with(1801)

    def test_overlong_probe_runs_again_without_negative_sleep(self) -> None:
        with patch.object(loop.secrets, "randbelow", return_value=0):
            self.assertEqual(loop.next_sleep(3600, 3000), 0)
