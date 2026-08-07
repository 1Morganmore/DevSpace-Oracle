from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "bin" / "mcp_resource_guard.py"


def load_guard():
    spec = importlib.util.spec_from_file_location("mcp_resource_guard_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generic_token_redaction_and_active_process_classification() -> None:
    guard = load_guard()
    value = guard.redact_command(
        "runner APP_TOKEN=secret OTHER_token=second --api-key third Authorization: Bearer fourth"
    )

    assert "secret" not in value and "second" not in value
    assert "third" not in value and "fourth" not in value
    assert value.count("REDACTED") == 4
    assert guard.classify_process(guard.Proc(1, 0, "node.exe", "node mcp_servers/multi-gpt/server.mjs", 1, 0)) == "multi-gpt-mcp"
    assert guard.classify_process(guard.Proc(2, 0, "python.exe", "python run_chatgpt_oracle.py", 1, 0)) == "chatgpt-runner"
    assert guard.classify_process(guard.Proc(3, 0, "cloudflared.exe", "cloudflared tunnel", 1, 0)) == "other"
