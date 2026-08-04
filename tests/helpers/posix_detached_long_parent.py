"""Long-running fixture with a session-detached delayed side effect."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


marker = os.environ["MARKER"]
ready = os.environ["READY"]
descendant = (
    "import pathlib,time;"
    "time.sleep(3);"
    f"pathlib.Path({marker!r}).write_text('survived', encoding='utf-8')"
)
subprocess.Popen(
    [sys.executable, "-c", descendant],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
Path(ready).write_text("ready", encoding="utf-8")
time.sleep(30)
