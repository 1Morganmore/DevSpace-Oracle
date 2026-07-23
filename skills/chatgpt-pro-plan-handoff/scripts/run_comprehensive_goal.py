from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


CANDIDATES = (
    Path(__file__).resolve().parents[3] / "bin" / "chatgpt_goal_supervisor.py",
    Path.home() / ".codex" / "bin" / "chatgpt_goal_supervisor.py",
)


def main() -> int:
    selected = next((path for path in CANDIDATES if path.is_file()), None)
    if selected is None:
        raise SystemExit("chatgpt_goal_supervisor.py is unavailable")
    bin_dir = str(selected.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("chatgpt_goal_supervisor_entrypoint", selected)
    if spec is None or spec.loader is None:
        raise SystemExit("chatgpt_goal_supervisor.py cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return int(module.main())


if __name__ == "__main__":
    raise SystemExit(main())
