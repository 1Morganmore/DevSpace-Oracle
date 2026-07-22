from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "bin" / "chatgpt_agbrowse_run.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_run_observation_test", PATH)
assert SPEC and SPEC.loader
RUN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RUN
SPEC.loader.exec_module(RUN)


class Store:
    def __init__(self, state_file: Path, record: dict):
        self.state_file = state_file
        self.record = record

    def load(self, run_dir: str):
        return self.state_file, dict(self.record)


class Bridge:
    def __init__(self, store: Store):
        self.store = store

    def settle_exact_terminal(self, run_dir: str):
        return self.store.load(run_dir)[1]


def _record(tmp_path: Path) -> tuple[Path, dict]:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"agbrowse_contract": str(tmp_path / "contract.json")}),
        encoding="utf-8",
    )
    contract = tmp_path / "contract.json"
    contract.write_text("{}", encoding="utf-8")
    record = {
        "schema": "codex.chatgpt.agbrowse-run/v1",
        "project_root": "C:\\project",
        "project_key": "project-key",
        "run_id": "RUN-1",
        "session_id": "SESSION-1",
        "current_target_id": "TARGET-1",
        "conversation_url": "https://chatgpt.com/c/conversation-1",
        "manifest_path": str(manifest),
        "phase": "RESPONSE_IN_PROGRESS",
    }
    return tmp_path / "run.json", record


def _install(monkeypatch, tmp_path: Path, record: dict, *, active_session: str) -> None:
    state_file = tmp_path / "run.json"
    fake = Bridge(Store(state_file, record))
    monkeypatch.setattr(RUN.BRIDGE, "Bridge", lambda state_root=None: fake)
    monkeypatch.setattr(RUN.BRIDGE, "read_contract", lambda path: {})
    monkeypatch.setattr(RUN.BRIDGE, "contract_executable", lambda contract: "agbrowse")
    monkeypatch.setattr(RUN.BRIDGE, "bridge_env", lambda manifest: {})

    def runner(command, env, timeout):
        if "sessions" in command:
            payload = {
                "sessionId": "SESSION-1",
                "targetId": "TARGET-1",
                "conversationUrl": "https://chatgpt.com/c/conversation-1",
                "status": "running",
            }
        else:
            payload = [
                {
                    "targetId": "TARGET-1",
                    "url": "https://chatgpt.com/c/conversation-1",
                    "activeCommand": {"sessionId": active_session},
                },
                {
                    "targetId": "FOREIGN",
                    "url": "https://chatgpt.com/c/foreign",
                    "activeCommand": {"sessionId": "FOREIGN-SESSION"},
                },
            ]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(RUN.BRIDGE, "default_runner", runner)


def test_matching_active_command_protects_exact_helper(monkeypatch, tmp_path: Path) -> None:
    _, record = _record(tmp_path)
    _install(monkeypatch, tmp_path, record, active_session="SESSION-1")
    result = RUN.observe_exact_run(str(tmp_path))
    assert result["state"] == "EXACT_ACTIVE"
    assert result["observed"]["active_command_session_id"] == "SESSION-1"
    assert result["process_termination_authorized"] is False
    assert result["tab_mutation_authorized"] is False


def test_durable_complete_outweighs_stale_sent_session(monkeypatch, tmp_path: Path) -> None:
    _, record = _record(tmp_path)
    record["phase"] = "COMPLETE"
    record["result"] = {"sha256": "a" * 64, "bytes": 12}
    record["cleanup_pending"] = False
    _install(monkeypatch, tmp_path, record, active_session="")

    result = RUN.observe_exact_run(str(tmp_path))

    assert result["state"] == "EXACT_COMPLETE"
    assert result["observed"]["provider_status"] == "running"
    assert "durably captured" in result["next_action"]


def test_snapshot_message_boundaries_extract_terminal_answer_without_elapsed_line() -> None:
    page_text = "\n".join(
        (
            "sidebar title",
            "prompt-RUN-1.txt",
            "더 보기",
            "AUDIT_VERDICT: PASS",
            "Verified final result.",
            "출처",
            "ChatGPT는 실수를 할 수 있습니다. 중요한 정보는 재차 확인하세요.",
        )
    )
    snapshot_text = "\n".join(
        (
            '    - group "내 메시지 작업" [ref=@e1]:',
            '      - button "메시지 복사" [ref=@e2]',
            "    - heading [level=4]",
            "    - paragraph:",
            '      - text: "AUDIT_VERDICT: PASS"',
            '    - paragraph: "Verified final result."',
            '    - group "응답 작업" [ref=@e3]:',
        )
    )

    answer = RUN.BRIDGE._terminal_snapshot_assistant_answer(page_text, snapshot_text)

    assert answer == "AUDIT_VERDICT: PASS\nVerified final result."


def test_active_command_mismatch_never_adopts_or_stops_foreign_session(monkeypatch, tmp_path: Path) -> None:
    _, record = _record(tmp_path)
    _install(monkeypatch, tmp_path, record, active_session="FOREIGN-SESSION")
    result = RUN.observe_exact_run(str(tmp_path))
    assert result["state"] == "IDENTITY_MISMATCH"
    assert "active_command_session" in result["identity_mismatches"]
    assert result["process_termination_authorized"] is False
    assert result["tab_mutation_authorized"] is False


def test_incomplete_persisted_identity_requires_exact_recovery(monkeypatch, tmp_path: Path) -> None:
    _, record = _record(tmp_path)
    record["conversation_url"] = None
    _install(monkeypatch, tmp_path, record, active_session="SESSION-1")
    result = RUN.observe_exact_run(str(tmp_path))
    assert result["state"] == "IDENTITY_INCOMPLETE"
    assert "canonical_url" in result["missing_identity_fields"]
    assert "do not inspect or submit through another tab" in result["next_action"]


def test_live_exact_target_outweighs_temporary_session_web_url(monkeypatch, tmp_path: Path) -> None:
    _, record = _record(tmp_path)
    state_file = tmp_path / "run.json"
    fake = Bridge(Store(state_file, record))
    monkeypatch.setattr(RUN.BRIDGE, "Bridge", lambda state_root=None: fake)
    monkeypatch.setattr(RUN.BRIDGE, "read_contract", lambda path: {})
    monkeypatch.setattr(RUN.BRIDGE, "contract_executable", lambda contract: "agbrowse")
    monkeypatch.setattr(RUN.BRIDGE, "bridge_env", lambda manifest: {})

    def runner(command, env, timeout):
        if "sessions" in command:
            payload = {
                "sessionId": "SESSION-1",
                "targetId": "TARGET-1",
                "conversationUrl": "https://chatgpt.com/c/WEB:temporary-id",
                "status": "running",
            }
        else:
            payload = [{
                "targetId": "TARGET-1",
                "url": "https://chatgpt.com/c/conversation-1",
                "activeCommand": {"sessionId": "SESSION-1"},
            }]
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr(RUN.BRIDGE, "default_runner", runner)
    result = RUN.observe_exact_run(str(tmp_path))

    assert result["state"] == "EXACT_ACTIVE"
    assert result["identity_mismatches"] == []
    assert result["observed"]["session_url_temporary"] is True
    assert result["observed"]["effective_canonical_url"] == record["conversation_url"]


def test_retry_completed_cleanup_command_retries_only_pending_exact_cleanup(monkeypatch, tmp_path: Path, capsys) -> None:
    _, record = _record(tmp_path)
    record.update({"phase": "COMPLETE", "cleanup_pending": True})

    class CleanupBridge(Bridge):
        def __init__(self):
            super().__init__(Store(tmp_path / "run.json", record))
            self.cleaned = False

        def cleanup_completed(self, run_dir: str, *, explicit_user_request: bool = False):
            assert run_dir == str(tmp_path)
            assert explicit_user_request is False
            self.cleaned = True
            self.store.record["cleanup_pending"] = False
            return {"ok": True, "state": "closed-and-absent"}

    bridge = CleanupBridge()
    monkeypatch.setattr(RUN.BRIDGE, "Bridge", lambda state_root=None: bridge)

    assert RUN.main(["--retry-completed-cleanup", str(tmp_path)]) == 0
    assert bridge.cleaned is True
    assert json.loads(capsys.readouterr().out)["result"]["cleanup_pending"] is False
