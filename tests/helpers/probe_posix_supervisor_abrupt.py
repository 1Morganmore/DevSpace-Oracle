"""Prove abrupt supervisor-group death cannot leak PID-namespace work."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_posix_tree_child.py"
PARENT = ROOT / "tests" / "helpers" / "posix_detached_long_parent.py"


def main() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    with tempfile.TemporaryDirectory(prefix="devspace-oracle-pidns-") as raw:
        directory = Path(raw)
        marker = directory / "marker"
        ready = directory / "ready"
        result = directory / "result.json"
        cancel = directory / "cancel"
        environment = dict(os.environ, MARKER=str(marker), READY=str(ready))
        process = subprocess.Popen(
            [
                sys.executable,
                str(RUNNER),
                "--hard-timeout-seconds",
                "30",
                "--result-file",
                str(result),
                "--cancel-file",
                str(cancel),
                "--parent-pid",
                str(os.getpid()),
                "--receipt-nonce",
                secrets.token_hex(32),
                "--",
                sys.executable,
                str(PARENT),
            ],
            cwd=ROOT,
            env=environment,
            start_new_session=True,
        )
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        if not ready.exists():
            process.kill()
            raise RuntimeError("detached descendant readiness was not reached")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        time.sleep(3.5)
        evidence = {
            "supervisor_exit_code": process.returncode,
            "marker_exists": marker.exists(),
            "result_exists": result.exists(),
        }
        print(json.dumps(evidence, separators=(",", ":")))
        if marker.exists():
            raise RuntimeError("PID-namespace workload survived abrupt supervisor death")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
