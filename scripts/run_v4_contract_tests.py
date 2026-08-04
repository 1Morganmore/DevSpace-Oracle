#!/usr/bin/env python
"""Offline release-contract runner for the install WAL and package inventory."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOCUSED = [
    "tests/test_global_gpt_browser_policy.py",
    "tests/test_release_packaging.py",
    "tests/test_install_lifecycle.py",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--focused", action="store_true")
    mode.add_argument("--full", action="store_true")
    args = parser.parse_args()
    targets = FOCUSED if args.focused else ["tests"]
    # Keep this short: several Windows concurrency tests create hash-bound
    # artifact names near MAX_PATH beneath the pytest base directory.
    with tempfile.TemporaryDirectory(prefix="co-") as basetemp:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets, "--basetemp", basetemp],
            cwd=ROOT,
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
