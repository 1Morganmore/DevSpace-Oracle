from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codexpro_windows_process_identity.py"
SPEC = importlib.util.spec_from_file_location("codexpro_windows_process_identity_test", MODULE_PATH)
assert SPEC and SPEC.loader
IDENTITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = IDENTITY
SPEC.loader.exec_module(IDENTITY)


class FakeRunner:
    def __init__(self, rows: dict[tuple[str, int], object]):
        self.rows = rows

    def run_fixed_query(self, query_id: str, arguments: dict[str, object]) -> object:
        key = int(arguments.get("port") or arguments.get("pid") or 0)
        return self.rows.get((query_id, key), [] if query_id != "process" else {})


def process(pid: int, parent: int, executable: Path, command: str) -> dict[str, object]:
    return {
        "ProcessId": pid,
        "ParentProcessId": parent,
        "CreationDate": f"20260721010{pid % 10}00.000000+000",
        "ExecutablePath": str(executable),
        "CommandLine": command,
    }


def fixture_runner(tmp_path: Path) -> FakeRunner:
    launcher = tmp_path / "node.exe"
    server = tmp_path / "server.exe"
    tunnel = tmp_path / "cloudflared.exe"
    for path, body in ((launcher, b"node"), (server, b"server"), (tunnel, b"tunnel")):
        path.write_bytes(body)
    rows = {
        ("listeners", 8790): [{"LocalAddress": "127.0.0.1", "LocalPort": 8790, "OwningProcess": 200}],
        ("process", 100): process(100, 1, launcher, f'"{launcher}" launcher'),
        ("process", 200): process(200, 100, server, f'"{server}" --port 8790'),
        ("process", 300): process(300, 100, tunnel, f'"{tunnel}" tunnel --url http://127.0.0.1:8790'),
        ("process", 1): process(1, 0, launcher, f'"{launcher}" root'),
        ("children", 100): [process(200, 100, server, f'"{server}" --port 8790'), process(300, 100, tunnel, f'"{tunnel}" tunnel --url http://127.0.0.1:8790')],
        ("children", 200): [],
        ("children", 300): [],
    }
    return FakeRunner(rows)


def test_listener_receipt_binds_actual_listener_not_wrapper(tmp_path: Path) -> None:
    runner = fixture_runner(tmp_path)
    receipt = IDENTITY.collect_listener_identity(
        runner=runner,
        port=8790,
        endpoint_key="https://example.invalid/mcp",
        topology_receipt_sha256="a" * 64,
        launcher_pid=100,
        launcher_creation_time_utc="20260721010000.000000+000",
    )
    assert receipt["listener_pid"] == 200
    assert receipt["launcher_pid"] == 100
    assert len(receipt["listener_executable_sha256"]) == 64
    assert len(receipt["listener_identity_receipt_sha256"]) == 64


def test_wrapper_only_listener_is_rejected(tmp_path: Path) -> None:
    runner = fixture_runner(tmp_path)
    runner.rows[("listeners", 8790)] = [{"LocalAddress": "127.0.0.1", "LocalPort": 8790, "OwningProcess": 100}]
    with pytest.raises(IDENTITY.ProcessIdentityError) as exc:
        IDENTITY.collect_listener_identity(
            runner=runner,
            port=8790,
            endpoint_key="https://example.invalid/mcp",
            topology_receipt_sha256="b" * 64,
            launcher_pid=100,
            launcher_creation_time_utc="20260721010000.000000+000",
        )
    assert exc.value.code == "APP_LISTENER_WRAPPER_ONLY"


def test_tunnel_receipt_is_separate_and_bound_to_upstream(tmp_path: Path) -> None:
    runner = fixture_runner(tmp_path)
    receipt = IDENTITY.collect_tunnel_identity(
        runner=runner,
        launcher_pid=100,
        launcher_creation_time_utc="20260721010000.000000+000",
        port=8790,
        endpoint_key="https://example.invalid/mcp",
        topology_receipt_sha256="c" * 64,
        public_url_sha256="d" * 64,
    )
    assert receipt["tunnel_pid"] == 300
    assert receipt["local_upstream"] == "127.0.0.1:8790"
    assert receipt["public_url_sha256"] == "d" * 64


def test_identity_drift_is_fail_closed() -> None:
    with pytest.raises(IDENTITY.ProcessIdentityError) as exc:
        IDENTITY.assert_identity_unchanged(
            {"listener_identity_receipt_sha256": "a" * 64},
            {"listener_identity_receipt_sha256": "b" * 64},
            kind="listener",
        )
    assert exc.value.code == "APP_LISTENER_IDENTITY_DRIFT"
