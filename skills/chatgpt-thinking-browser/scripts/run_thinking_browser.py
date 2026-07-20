from __future__ import annotations

import runpy
from pathlib import Path


ENTRYPOINT = Path(__file__).with_name("run_chatgpt_thinking.py")


if __name__ == "__main__":
    runpy.run_path(str(ENTRYPOINT), run_name="__main__")
