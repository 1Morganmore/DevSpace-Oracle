#!/usr/bin/env python
"""Offline release-contract runner for the install WAL and package inventory."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOCUSED = ["tests/test_release_packaging.py", "tests/test_install_lifecycle.py"]


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--focused", action="store_true")
    mode.add_argument("--full", action="store_true")
    args = parser.parse_args()
    targets = FOCUSED if args.focused else ["tests"]
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", *targets], cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
