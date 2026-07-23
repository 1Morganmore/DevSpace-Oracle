from __future__ import annotations

import subprocess
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-goal-contract-tests-") as basetemp:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_chatgpt_goal_supervisor.py",
            "--basetemp",
            basetemp,
        ]
        hidden = {}
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            hidden = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}
        return subprocess.run(command, cwd=ROOT, check=False, **hidden).returncode


if __name__ == "__main__":
    raise SystemExit(main())
