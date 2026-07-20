from __future__ import annotations

import os
import runpy
from pathlib import Path


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
RUNNER = CODEX_HOME / "bin" / "chatgpt_agbrowse_run.py"
if not RUNNER.is_file():
    RUNNER = Path(__file__).resolve().parents[3] / "bin" / "chatgpt_agbrowse_run.py"


if __name__ == "__main__":
    runpy.run_path(str(RUNNER), run_name="__main__")
