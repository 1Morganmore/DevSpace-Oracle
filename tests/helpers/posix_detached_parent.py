"""Fixture parent that leaves a session-detached descendant behind."""

from __future__ import annotations

import os
import subprocess
import sys
import time


marker = os.environ["MARKER"]
descendant = (
    "import pathlib,time;"
    "time.sleep(2);"
    f"pathlib.Path({marker!r}).write_text('survived', encoding='utf-8')"
)
subprocess.Popen(
    [sys.executable, "-c", descendant],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(0.2)
