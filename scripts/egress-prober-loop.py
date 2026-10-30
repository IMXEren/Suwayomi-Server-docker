#!/usr/bin/env python3
"""Run the egress prober on an independently jittered start-to-start schedule."""
from __future__ import annotations

import secrets
import subprocess
import sys
import time

JITTER_SECONDS = 15 * 60


def next_sleep(interval: int, elapsed: float) -> float:
    jitter = secrets.randbelow(2 * JITTER_SECONDS + 1) - JITTER_SECONDS
    return max(0.0, interval + jitter - elapsed)


def main() -> None:
    interval = int(sys.argv[1])
    while True:
        started = time.monotonic()
        subprocess.run([sys.executable, "/app/egress-prober.py"], check=False)
        time.sleep(next_sleep(interval, time.monotonic() - started))


if __name__ == "__main__":
    main()
