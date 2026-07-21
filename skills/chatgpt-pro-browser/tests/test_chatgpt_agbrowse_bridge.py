from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = Path(
    __import__("os").environ.get(
        "CHATGPT_AGBROWSE_BRIDGE_UNDER_TEST",
        REPO_ROOT / "bin" / "chatgpt_agbrowse_bridge.py",
    )
)
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_bridge_test", MODULE_PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)
PROMPT_FILE_HANDOFF = BRIDGE.STATE.PROMPT_FILE_HANDOFF


def write_contract(path: Path):
    source = Path.home() / ".codex" / "contracts" / "agbrowse-0.1.18.json"
    path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def write_manifest(path: Path, **values):
    payload = {
        "question": "analyze this project",
        "mode_label": "Pro",
        "mode_variant": None,
        "app_policy": "forbidden",
        "browser_agent_home": str(path.parent / "browser-home"),
    }
    payload.update(values)
    if str(payload.get("app_policy") or "") == "required":
        if "mode_label" not in values:
            payload["mode_label"] = "GPT-5.6"
            payload["mode_variant"] = "High"
        if "chatgpt_app_name" not in values and "app_name" not in values:
            payload["chatgpt_app_name"] = "CodexPro-Test"
            payload["chatgpt_app_server_url"] = "https://example.test/mcp"
    if str(payload.get("mode_label") or "").casefold() == "pro":
        payload.pop("chatgpt_app_name", None)
        payload.pop("app_name", None)
        payload.pop("chatgpt_app_server_url", None)
    prompt_body = str(payload.pop("question"))
    prompt_file = path.with_name(f"{path.stem}-prompt.txt")
    prompt_file.write_text(prompt_body, encoding="utf-8")
    files = payload.get("files") or []
    if isinstance(files, str):
        files = [files]
    payload.update(
        {
            "question": PROMPT_FILE_HANDOFF,
            "prompt_transport": "file",
            "prompt_file": str(prompt_file),
            "prompt_file_sha256": hashlib.sha256(prompt_file.read_bytes()).hexdigest(),
            "files": [str(prompt_file), *[str(item) for item in files]],
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def healthy_identity(public_url, expected_root, expected_port, timeout):
    return {
        "ok": True,
        "public_url": public_url,
        "observed_root": expected_root,
        "observed_port": expected_port,
        "timeout": timeout,
    }


class FakeTabLifecycle:
    def list_tabs(self):
        return [{"targetId": "blank", "url": "about:blank", "type": "page"}]

    def record_owned(self, run_dir, *, target_id, url, stage):
        return {"ok": True, "run_dir": run_dir, "target_id": target_id, "url": url, "stage": stage}

    def close_pre_submit(self, run_dir, *, target_id, reason):
        return {"ok": True, "run_dir": run_dir, "target_id": target_id, "reason": reason, "verified_absent": True}

    def record_protected(self, run_dir, *, target_id, conversation_url, stage):
        return {
            "ok": True,
            "run_dir": run_dir,
            "target_id": target_id,
            "conversation_url": conversation_url,
            "stage": stage,
        }

    def close_completed(self, run_dir, *, explicit_user_request):
        run_path = Path(run_dir)
        record = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        evidence_path = run_path / "tab-lifecycle.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "schema": "test.tab-lifecycle/v1",
                    "events": [{"kind": "explicit-provider-failed-cleanup"}],
                }
            ),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "state": "closed-and-absent",
            "target_id": record["current_target_id"],
            "conversation_url": record["conversation_url"],
            "evidence": {
                "path": str(evidence_path),
                "sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
            },
        }


def prepared_bridge(
    tmp_path: Path,
    runner,
    app_connector_factory=None,
    *,
    headed_runtime_preflight=False,
    **manifest_values,
):
    project = tmp_path / "project"
    project.mkdir()
    manifest = write_manifest(tmp_path / "manifest.json", **manifest_values)
    contract = write_contract(tmp_path / "contract.json")
    if app_connector_factory is None:
        app_connector_factory = lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": "CodexPro-Test",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            }
        )
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        app_connector_factory=app_connector_factory,
        app_identity_probe=healthy_identity,
        tab_lifecycle_factory=lambda _executable, _manifest: FakeTabLifecycle(),
        headed_runtime_preflight=headed_runtime_preflight,
    )
    record = bridge.prepare(project_root=str(project), manifest_path=str(manifest), contract_path=str(contract))
    assert record["run_dir"].endswith(record["run_id"])
    return bridge, record


def completed(payload: dict, code: int = 0):
    return subprocess.CompletedProcess(["agbrowse"], code, json.dumps(payload), "")


def raw_completed(stdout: str = "", code: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(["agbrowse"], code, stdout, stderr)


class FakeAppConnector:
    def __init__(self, inspection=None, reconcile_result=None, composer_result=None, error=None, registration=None):
        self.inspection = inspection
        self.reconcile_result = reconcile_result
        self.composer_result = composer_result
        self.error = error
        self.registration = registration
        self.reconcile_calls = []
        self.inspect_calls = []

    def inspect(self, app_name, *, expected_url=None):
        self.inspect_calls.append({"app_name": app_name, "expected_url": expected_url})
        if self.error:
            raise self.error
        if isinstance(self.inspection, list):
            return dict(self.inspection.pop(0))
        return dict(self.inspection or {})

    def reconcile(self, decision):
        if self.error:
            raise self.error
        self.reconcile_calls.append(dict(decision))
        return dict(self.reconcile_result or {"ok": True, "phase": "COMPLETE"})

    def expected_registration_for_scope(self, app_name, project_root):
        return dict(self.registration) if self.registration else None

    def prepare_composer_app(self, app_name, *, composer_url="https://chatgpt.com/"):
        if self.error:
            raise self.error
        return dict(self.composer_result or {
            "ok": True,
            "state": "composer-app-mention-tab-confirmed",
            "app_name": app_name,
            "target_id": "target-composer",
            "url": composer_url,
            "selection_method": "exact-at-mention-then-tab",
            "mention_text_sha256": hashlib.sha256(f"@{app_name}".encode("utf-8")).hexdigest(),
        })

    def activate_composer_target(self, target_id):
        if self.error:
            raise self.error
        return {"ok": True, "target_id": target_id}


def test_same_parent_wave_reuses_one_hashed_app_attestation(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = tmp_path / "parent.json"
    parent_manifest.write_text(
        json.dumps({"schema": "codex.chatgpt.web-multi/v1", "workflow_id": "wf-attest", "project_root": str(project), "question": "parent"}),
        encoding="utf-8",
    )
    child_manifests = []
    for lane in range(2):
        child_manifests.append(
            write_manifest(
                tmp_path / f"child-{lane}.json",
                app_policy="required",
                chatgpt_app_name="CodexPro-Test",
                chatgpt_app_server_url="https://example.test/mcp",
                app_attestation_scope="solver-wave",
                workflow_correlation={"workflow_id": "wf-attest", "stage": f"solver-{lane}"},
            )
        )
    contract = BRIDGE.read_contract(write_contract(tmp_path / "contract.json"))
    store = BRIDGE.STATE.RunStore(tmp_path / "state")
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-attest", agbrowse_contract=contract,
    )
    children = []
    for lane, manifest in enumerate(child_manifests):
        child = store.create_child_run(
            parent_run_dir=parent["run_dir"], manifest_path=manifest, agbrowse_contract=contract,
            role="Solver", lane=lane, iteration=0, stage_id=f"solver-{lane}",
        )
        store.transition(child["run_dir"], "PREFLIGHTED")
        children.append(child)
    connector = FakeAppConnector(
        inspection={
            "state": "detail", "app_name": "CodexPro-Test", "url": "https://example.test/mcp",
            "connected": True, "full_access": True,
        },
        registration={
            "root": str(project), "app_name": "CodexPro-Test",
            "public_url": "https://example.test/mcp", "port": 8765,
        },
    )
    identity_calls = []

    def identity(public_url, expected_root, expected_port, timeout):
        identity_calls.append(public_url)
        return healthy_identity(public_url, expected_root, expected_port, timeout)

    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=identity,
    )

    first = bridge.ensure_app(children[0]["run_dir"])
    second = bridge.ensure_app(children[1]["run_dir"])

    assert first["phase"] == second["phase"] == "PREFLIGHTED"
    assert len(connector.inspect_calls) == 1
    assert identity_calls == ["https://example.test/mcp"]
    second_evidence = json.loads((Path(children[1]["run_dir"]) / "app-evidence.json").read_text(encoding="utf-8"))
    assert second_evidence["result"]["action"] == "parent-wave-attestation-reuse"
    attestation_path = Path(second_evidence["result"]["attestation_path"])
    assert attestation_path.is_file()
    assert hashlib.sha256(attestation_path.read_bytes()).hexdigest() == second_evidence["result"]["attestation_sha256"]


def test_parent_wave_attestation_is_not_reused_after_registry_url_drift(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = tmp_path / "parent.json"
    parent_manifest.write_text(
        json.dumps({"schema": "codex.chatgpt.web-multi/v1", "workflow_id": "wf-drift", "project_root": str(project), "question": "parent"}),
        encoding="utf-8",
    )
    manifests = [
        write_manifest(
            tmp_path / f"drift-{lane}.json",
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
            chatgpt_app_server_url="https://example.test/mcp",
            app_attestation_scope="solver-wave",
            workflow_correlation={"workflow_id": "wf-drift", "stage": f"solver-{lane}"},
        )
        for lane in range(2)
    ]
    contract = BRIDGE.read_contract(write_contract(tmp_path / "contract.json"))
    store = BRIDGE.STATE.RunStore(tmp_path / "state")
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-drift", agbrowse_contract=contract,
    )
    children = []
    for lane, manifest in enumerate(manifests):
        child = store.create_child_run(
            parent_run_dir=parent["run_dir"], manifest_path=manifest, agbrowse_contract=contract,
            role="Solver", lane=lane, iteration=0, stage_id=f"solver-{lane}",
        )
        store.transition(child["run_dir"], "PREFLIGHTED")
        children.append(child)
    connector = FakeAppConnector(
        inspection={
            "state": "detail", "app_name": "CodexPro-Test", "url": "https://example.test/mcp",
            "connected": True, "full_access": True,
        },
        registration={
            "root": str(project), "app_name": "CodexPro-Test",
            "public_url": "https://example.test/mcp", "port": 8765,
        },
    )
    identity_calls = []
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=lambda public_url, root, port, timeout: (
            identity_calls.append(public_url) or healthy_identity(public_url, root, port, timeout)
        ),
    )

    bridge.ensure_app(children[0]["run_dir"])
    connector.registration["public_url"] = "https://drifted.example.test/mcp"
    connector.registration["port"] = 9999
    bridge.ensure_app(children[1]["run_dir"])

    assert len(connector.inspect_calls) == 2
    assert identity_calls == ["https://example.test/mcp", "https://example.test/mcp"]
    second_evidence = json.loads((Path(children[1]["run_dir"]) / "app-evidence.json").read_text(encoding="utf-8"))
    assert second_evidence["result"]["action"] == "inspect-match"


def test_concurrent_parent_wave_has_one_app_verification_leader(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = tmp_path / "parent.json"
    parent_manifest.write_text(
        json.dumps({"schema": "codex.chatgpt.web-multi/v1", "workflow_id": "wf-stampede", "project_root": str(project), "question": "parent"}),
        encoding="utf-8",
    )
    manifests = [
        write_manifest(
            tmp_path / f"stampede-{lane}.json",
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
            chatgpt_app_server_url="https://example.test/mcp",
            app_attestation_scope="solver-wave",
            workflow_correlation={"workflow_id": "wf-stampede", "stage": f"solver-{lane}"},
        )
        for lane in range(2)
    ]
    contract = BRIDGE.read_contract(write_contract(tmp_path / "contract.json"))
    store = BRIDGE.STATE.RunStore(tmp_path / "state")
    parent = store.create_parent_workflow(
        project_root=project, manifest_path=parent_manifest, workflow_id="wf-stampede", agbrowse_contract=contract,
    )
    children = []
    for lane, manifest in enumerate(manifests):
        child = store.create_child_run(
            parent_run_dir=parent["run_dir"], manifest_path=manifest, agbrowse_contract=contract,
            role="Solver", lane=lane, iteration=0, stage_id=f"solver-{lane}",
        )
        store.transition(child["run_dir"], "PREFLIGHTED")
        children.append(child)
    connector = FakeAppConnector(
        inspection={
            "state": "detail", "app_name": "CodexPro-Test", "url": "https://example.test/mcp",
            "connected": True, "full_access": True,
        },
        registration={
            "root": str(project), "app_name": "CodexPro-Test",
            "public_url": "https://example.test/mcp", "port": 8765,
        },
    )
    identity_calls = []
    identity_lock = threading.Lock()

    def identity(public_url, root, port, timeout):
        with identity_lock:
            identity_calls.append(public_url)
        time.sleep(0.15)
        return healthy_identity(public_url, root, port, timeout)

    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=identity,
    )
    start = threading.Barrier(2)

    def ensure(child):
        start.wait(timeout=5)
        return bridge.ensure_app(child["run_dir"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(ensure, children))

    assert [item["phase"] for item in results] == ["PREFLIGHTED", "PREFLIGHTED"]
    assert len(identity_calls) == 1
    assert len(connector.inspect_calls) == 1
    actions = {
        json.loads((Path(child["run_dir"]) / "app-evidence.json").read_text(encoding="utf-8"))["result"]["action"]
        for child in children
    }
    assert actions == {"inspect-match", "parent-wave-attestation-reuse"}


def test_global_app_contract_reuses_exact_registration_without_settings_inspection(tmp_path: Path):
    projects = [tmp_path / "project-a", tmp_path / "project-b"]
    for project in projects:
        project.mkdir()
    manifests = [
        write_manifest(
            tmp_path / f"global-{index}.json",
            app_policy="required",
            chatgpt_app_name="CodexPro-CDrive-v14",
        )
        for index in range(2)
    ]
    registration = {
        "root": str(tmp_path),
        "app_name": "CodexPro-CDrive-v14",
        "public_url": "https://example.test/mcp?codexpro_token=secret",
        "port": 8790,
    }
    connector = FakeAppConnector(
        inspection={
            "state": "detail",
            "app_name": "CodexPro-CDrive-v14",
            "url": registration["public_url"],
            "connected": True,
            "full_access": True,
        },
        registration=registration,
    )
    identity_calls = []
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=lambda url, root, port, timeout: (
            identity_calls.append(url) or healthy_identity(url, root, port, timeout)
        ),
    )
    contract = write_contract(tmp_path / "contract.json")

    first = bridge.prepare(project_root=str(projects[0]), manifest_path=str(manifests[0]), contract_path=str(contract))
    bridge.ensure_app(first["run_dir"])
    second = bridge.prepare(project_root=str(projects[1]), manifest_path=str(manifests[1]), contract_path=str(contract))
    bridge.ensure_app(second["run_dir"])

    assert len(connector.inspect_calls) == 1
    assert identity_calls == [registration["public_url"], registration["public_url"]]
    evidence = json.loads((Path(second["run_dir"]) / "app-evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["action"] == "global-app-contract-reuse"
    contract_state = tmp_path / "state" / "app-contract-state.json"
    state = json.loads(contract_state.read_text(encoding="utf-8"))
    assert state["schema"] == "codex.chatgpt.global-app-contract-state/v1"
    assert len(state["entries"]) == 1
    assert state["events"][-1]["result"] == "reused"
    assert "codexpro_token=secret" not in contract_state.read_text(encoding="utf-8")


def test_global_app_contract_url_drift_falls_back_to_full_inspection(tmp_path: Path):
    projects = [tmp_path / "drift-a", tmp_path / "drift-b"]
    for project in projects:
        project.mkdir()
    manifests = [
        write_manifest(
            tmp_path / f"global-drift-{index}.json",
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
        )
        for index in range(2)
    ]
    connector = FakeAppConnector(
        inspection={
            "state": "detail",
            "app_name": "CodexPro-Test",
            "url": "https://first.example.test/mcp",
            "connected": True,
            "full_access": True,
        },
        registration={
            "root": str(tmp_path),
            "app_name": "CodexPro-Test",
            "public_url": "https://first.example.test/mcp",
            "port": 8790,
        },
    )
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=healthy_identity,
    )
    contract = write_contract(tmp_path / "contract.json")
    first = bridge.prepare(project_root=str(projects[0]), manifest_path=str(manifests[0]), contract_path=str(contract))
    bridge.ensure_app(first["run_dir"])

    connector.registration["public_url"] = "https://second.example.test/mcp"
    connector.inspection = {
        "state": "detail",
        "app_name": "CodexPro-Test",
        "url": "https://second.example.test/mcp",
        "connected": True,
        "full_access": True,
    }
    second = bridge.prepare(project_root=str(projects[1]), manifest_path=str(manifests[1]), contract_path=str(contract))
    bridge.ensure_app(second["run_dir"])

    assert len(connector.inspect_calls) == 2
    evidence = json.loads((Path(second["run_dir"]) / "app-evidence.json").read_text(encoding="utf-8"))
    assert evidence["result"]["action"] == "inspect-match"


def test_global_app_contract_never_bypasses_unhealthy_current_endpoint(tmp_path: Path):
    projects = [tmp_path / "health-a", tmp_path / "health-b"]
    for project in projects:
        project.mkdir()
    manifests = [
        write_manifest(
            tmp_path / f"global-health-{index}.json",
            app_policy="required",
            chatgpt_app_name="CodexPro-Test",
        )
        for index in range(2)
    ]
    registration = {
        "root": str(tmp_path),
        "app_name": "CodexPro-Test",
        "public_url": "https://example.test/mcp",
        "port": 8790,
    }
    connector = FakeAppConnector(
        inspection={
            "state": "detail",
            "app_name": "CodexPro-Test",
            "url": registration["public_url"],
            "connected": True,
            "full_access": True,
        },
        registration=registration,
    )
    identity_results = [
        healthy_identity(registration["public_url"], str(tmp_path), 8790, 15),
        {"ok": False, "reason": "unreachable"},
    ]
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=lambda *_: identity_results.pop(0),
    )
    contract = write_contract(tmp_path / "contract.json")
    first = bridge.prepare(project_root=str(projects[0]), manifest_path=str(manifests[0]), contract_path=str(contract))
    bridge.ensure_app(first["run_dir"])
    second = bridge.prepare(project_root=str(projects[1]), manifest_path=str(manifests[1]), contract_path=str(contract))

    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.ensure_app(second["run_dir"])

    assert error.value.code == "APP_ENDPOINT_UNHEALTHY"
    assert len(connector.inspect_calls) == 1
    _, blocked = bridge.store.load(second["run_dir"])
    assert blocked["phase"] == "PREFLIGHT_BLOCKED"


def test_global_app_contract_log_is_bounded(tmp_path: Path):
    registration = {
        "root": str(tmp_path),
        "app_name": "CodexPro-Test",
        "public_url": "https://example.test/mcp",
        "port": 8790,
    }
    connector = FakeAppConnector(
        inspection={
            "state": "detail",
            "app_name": "CodexPro-Test",
            "url": registration["public_url"],
            "connected": True,
            "full_access": True,
        },
        registration=registration,
    )
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=healthy_identity,
    )
    contract = write_contract(tmp_path / "contract.json")
    project = tmp_path / "bounded"
    project.mkdir()
    manifest_path = write_manifest(
        tmp_path / "bounded.json",
        app_policy="required",
        chatgpt_app_name="CodexPro-Test",
    )
    record = bridge.prepare(project_root=str(project), manifest_path=str(manifest_path), contract_path=str(contract))
    bridge.ensure_app(record["run_dir"])
    state_file, current = bridge.store.load(record["run_dir"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for _ in range(24):
        candidate = bridge._global_app_contract_candidate(
            state_file=state_file,
            record=current,
            manifest=manifest,
            app_name="CodexPro-Test",
            connector=connector,
        )
        assert candidate is not None
        bridge._touch_global_app_contract_reuse(candidate)

    state = json.loads((tmp_path / "state" / "app-contract-state.json").read_text(encoding="utf-8"))
    assert len(state["events"]) == BRIDGE.GLOBAL_APP_CONTRACT_MAX_EVENTS
    assert next(iter(state["entries"].values()))["reuse_count"] == 24
    assert len(connector.inspect_calls) == 1


def test_parent_owned_child_rejects_second_send_before_browser_mutation(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    parent_manifest = tmp_path / "parent.json"
    parent_manifest.write_text(
        json.dumps(
            {
                "schema": "codex.chatgpt.web-multi/v1",
                "workflow_id": "wf-child-send",
                "project_root": str(project),
                "question": "parent",
            }
        ),
        encoding="utf-8",
    )
    child_manifest = write_manifest(
        tmp_path / "child.json",
        app_policy="required",
        chatgpt_app_name="CodexPro-Test",
        chatgpt_app_server_url="https://example.test/mcp",
        provider_url="https://chatgpt.com/",
        workflow_correlation={"workflow_id": "wf-child-send", "stage": "solver-0"},
    )
    contract_path = write_contract(tmp_path / "contract.json")
    contract = BRIDGE.read_contract(contract_path)
    store = BRIDGE.STATE.RunStore(tmp_path / "state")
    parent = store.create_parent_workflow(
        project_root=project,
        manifest_path=parent_manifest,
        workflow_id="wf-child-send",
        agbrowse_contract=contract,
    )
    child = store.create_child_run(
        parent_run_dir=parent["run_dir"],
        manifest_path=child_manifest,
        agbrowse_contract=contract,
        role="Solver",
        lane=0,
        iteration=0,
        stage_id="solver-0",
    )
    store.transition(child["run_dir"], "PREFLIGHTED")
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:3] == ["web-ai", "sessions"]:
            return completed({"session": {"targetId": "target-composer", "url": "https://chatgpt.com/c/child-send"}})
        return completed(
            {
                "ok": True,
                "status": "sent",
                "sessionId": "session-child",
                "targetId": "target-composer",
                "conversationUrl": "https://chatgpt.com/c/child-send",
            }
        )

    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": "CodexPro-Test",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            }
        ),
        app_identity_probe=healthy_identity,
        tab_lifecycle_factory=lambda _executable, _manifest: FakeTabLifecycle(),
    )

    first = bridge.send(child["run_dir"])
    with pytest.raises(BRIDGE.STATE.StateError) as duplicate:
        bridge.send(child["run_dir"])

    send_commands = [command for command in commands if command[1:3] == ["web-ai", "send"]]
    assert first["send_attempt_count"] == 1
    assert duplicate.value.code == "SEND_ALREADY_ATTEMPTED"
    assert len(send_commands) == 1
    assert send_commands[0][0] == contract["agbrowse"]["executablePath"]


def test_send_process_not_created_is_exact_safe_rejection(tmp_path: Path):
    def runner(command, env, timeout):
        raise FileNotFoundError(2, "file not found", command[0])

    bridge, record = prepared_bridge(tmp_path, runner)

    result = bridge.send(record["run_dir"])

    assert result["phase"] == "SEND_REJECTED"
    events = result["recovery_events"]
    process_event = next(item for item in events if item["kind"] == "send-runner-process-not-created")
    assert process_event["mutation_allowed"] is False
    assert process_event["exception_type"] == "FileNotFoundError"
    evidence_path = Path(process_event["evidence_path"])
    assert evidence_path.is_file()
    assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == process_event["evidence_sha256"]
    assert events[-1]["kind"] == "verified-pre-submit-tab-cleanup"


def test_required_app_rejects_legacy_plugin_transport(tmp_path: Path):
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return completed(
            {
                "ok": True,
                "status": "sent",
                "sessionId": "session-1",
                "targetId": "target-1",
                "conversationUrl": "https://chatgpt.com/c/conversation-1",
            }
        )

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": "CodexPro-r-v01",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            }
        ),
        app_policy="required",
        app_name="CodexPro-r-v01",
        chatgpt_app_server_url="https://example.test/mcp",
        provider_url="https://chatgpt.com/en-US/",
        app_selection_transport="legacy-plugin-parallel",
    )
    run_dir = str(tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"])
    with pytest.raises(BRIDGE.BridgeError, match="unsupported app_selection_transport"):
        bridge.send(run_dir)
    assert commands == []


def test_explicit_inline_pill_transport_uses_prepared_target_and_reuse_tab(tmp_path: Path):
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return completed({
            "ok": True,
            "status": "sent",
            "sessionId": "session-inline",
            "conversationUrl": "https://chatgpt.com/c/conversation-inline",
        })

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": "CodexPro-r-v01",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            }
        ),
        app_policy="required",
        app_name="CodexPro-r-v01",
        chatgpt_app_server_url="https://example.test/mcp",
    )
    sent = bridge.send(record["run_dir"])
    command = commands[0]
    assert "--reuse-tab" in command
    assert "--parallel" not in command
    assert "--plugin" not in command
    assert sent["current_target_id"] == "target-composer"
    assert len(sent["app_evidence_refs"]) == 2


def test_required_app_rejects_mismatched_mention_hash_before_send(tmp_path: Path):
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return completed({"ok": True})

    app_name = "CodexPro-r-v01"
    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": app_name,
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            },
            composer_result={
                "ok": True,
                "state": "composer-app-mention-tab-confirmed",
                "app_name": app_name,
                "target_id": "target-composer",
                "url": "https://chatgpt.com/",
                "selection_method": "exact-at-mention-then-tab",
                "mention_text_sha256": "0" * 64,
            },
        ),
        app_policy="required",
        app_name=app_name,
        chatgpt_app_server_url="https://example.test/mcp",
    )
    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.send(record["run_dir"])
    assert error.value.code == "APP_SELECTION_EVIDENCE_MISSING"
    assert commands == []


def test_required_app_mismatch_blocks_before_send_and_never_calls_runner(tmp_path: Path):
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        return completed({"ok": True})

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={"state": "missing", "app_name": "CodexPro-r-v01"}
        ),
        app_policy="required",
        app_name="CodexPro-r-v01",
        chatgpt_app_server_url="https://example.test/mcp",
    )
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.send(str(run_dir))
    assert error.value.code == "APP_RECONCILE_DECISION_REQUIRED"
    assert commands == []
    _, blocked = bridge.store.load(run_dir)
    assert blocked["phase"] == "PREFLIGHT_BLOCKED"


def test_exact_active_registry_can_repair_connection_or_permission_without_new_app(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    manifest = write_manifest(
        tmp_path / "manifest.json",
        app_policy="required",
        chatgpt_app_name="CodexPro-r-v01",
    )
    contract = write_contract(tmp_path / "contract.json")
    connector = FakeAppConnector(
        inspection=[
            {
                "state": "detail",
                "app_name": "CodexPro-r-v01",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": False,
            },
            {
                "state": "detail",
                "app_name": "CodexPro-r-v01",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            },
        ],
        registration={
            "root": str(project),
            "app_name": "CodexPro-r-v01",
            "public_url": "https://example.test/mcp",
            "port": 8790,
        },
    )
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=healthy_identity,
    )
    record = bridge.prepare(project_root=str(project), manifest_path=str(manifest), contract_path=str(contract))

    repaired = bridge.ensure_app(record["run_dir"])

    assert repaired["phase"] == "PREFLIGHTED"
    assert connector.reconcile_calls == [
        {
            "root": str(project),
            "app_name": "CodexPro-r-v01",
            "public_url": "https://example.test/mcp",
            "port": 8790,
            "action": "repair-active-exact-registration",
        }
    ]


def test_missing_active_registry_app_never_autocreates_from_bridge(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    manifest = write_manifest(
        tmp_path / "manifest.json",
        app_policy="required",
        chatgpt_app_name="CodexPro-r-v01",
    )
    contract = write_contract(tmp_path / "contract.json")
    connector = FakeAppConnector(
        inspection={"state": "missing", "app_name": "CodexPro-r-v01"},
        registration={
            "root": str(project),
            "app_name": "CodexPro-r-v01",
            "public_url": "https://example.test/mcp",
            "port": 8790,
        },
    )
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: connector,
        app_identity_probe=healthy_identity,
    )
    record = bridge.prepare(project_root=str(project), manifest_path=str(manifest), contract_path=str(contract))

    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.ensure_app(record["run_dir"])

    assert error.value.code == "APP_RECONCILE_DECISION_REQUIRED"
    assert connector.reconcile_calls == []


def test_required_app_selection_warning_never_becomes_submitted(tmp_path: Path):
    def runner(command, env, timeout):
        return completed({
            "ok": True,
            "status": "sent",
            "sessionId": "session-uncertain",
            "targetId": None,
            "conversationUrl": None,
            "warnings": ["composer plugin not selected: codexpro-cdrive-v11"],
        })

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        app_connector_factory=lambda _executable: FakeAppConnector(
            inspection={
                "state": "detail",
                "app_name": "CodexPro-CDrive-v11",
                "url": "https://example.test/mcp",
                "connected": True,
                "full_access": True,
            }
        ),
        app_policy="required",
        app_name="CodexPro-CDrive-v11",
        chatgpt_app_server_url="https://example.test/mcp",
    )
    result = bridge.send(record["run_dir"])
    assert result["phase"] == "RECOVERY_REQUIRED"
    assert result.get("session_id") == "session-uncertain"
    assert not result.get("conversation_url")
    assert result.get("terminal_block_code") is None
    assert result["recovery_events"][-1]["kind"] == "prepared-target-send-target-mismatch"


def test_app_transaction_exception_blocks_project_before_submission(tmp_path: Path):
    decision = tmp_path / "decision.json"
    project = tmp_path / "project"
    project.mkdir()
    decision.write_text(
        json.dumps({
            "root": str(project),
            "app_name": "CodexPro-r-v02",
            "public_url": "https://example.test/mcp",
            "transaction_id": "tx-1",
        }),
        encoding="utf-8",
    )
    manifest = write_manifest(
        tmp_path / "manifest.json",
        app_policy="required",
        app_name="CodexPro-r-v02",
        app_decision_path=str(decision),
    )
    contract = write_contract(tmp_path / "contract.json")
    bridge = BRIDGE.Bridge(
        state_root=tmp_path / "state",
        runner=lambda *_: completed({"ok": True}),
        app_connector_factory=lambda _executable: FakeAppConnector(error=RuntimeError("ui drift")),
        app_identity_probe=healthy_identity,
    )
    record = bridge.prepare(project_root=str(project), manifest_path=str(manifest), contract_path=str(contract))
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.send(str(run_dir))
    assert error.value.code == "APP_TRANSACTION_FAILED"
    _, blocked = bridge.store.load(run_dir)
    assert blocked["phase"] == "BLOCKED_APP_TRANSACTION"


def test_pro_send_is_attachment_only_and_never_selects_app(tmp_path: Path):
    attachment = tmp_path / "context.zip"
    attachment.write_bytes(b"zip")
    commands = []
    environments = []

    def runner(command, env, timeout):
        commands.append(command)
        environments.append(env)
        return completed({"ok": True, "status": "sent", "sessionId": "s", "targetId": "t"})

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        mode_label="Pro",
        mode_variant=None,
        app_policy="forbidden",
        files=[str(attachment)],
    )
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    bridge.send(str(run_dir))
    command = commands[0]
    assert command[command.index("--model") + 1] == "pro"
    assert "--file" in command
    assert "--plugin" not in command
    assert command[1:3] == ["web-ai", "send"]
    assert command[command.index("--url") + 1] == "https://chatgpt.com/"
    assert "--parallel" in command
    assert "sessions" not in command
    assert environments[0]["AGBROWSE_WEB_AI_AUTO_START"] == "0"


def test_pro_cold_start_proves_headed_runtime_before_single_send(tmp_path: Path):
    attachment = tmp_path / "context.zip"
    attachment.write_bytes(b"zip")
    commands = []
    environments = []

    def runner(command, env, timeout):
        commands.append(command)
        environments.append(dict(env))
        if command[1] == "start":
            return raw_completed("Chrome started headed")
        if command[1:3] == ["web-ai", "send"]:
            return completed({"ok": True, "status": "sent", "sessionId": "s", "targetId": "t"})
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        headed_runtime_preflight=True,
        mode_label="Pro",
        mode_variant=None,
        app_policy="forbidden",
        files=[str(attachment)],
    )

    sent = bridge.send(record["run_dir"])

    assert sent["phase"] == "SUBMITTED"
    assert commands[0][1:] == ["start", "--headed", "--port", "9222"]
    assert commands[1][1:3] == ["web-ai", "send"]
    assert environments[1]["AGBROWSE_WEB_AI_AUTO_START"] == "0"


def test_pro_headless_runtime_is_safely_restarted_once_before_send(tmp_path: Path):
    attachment = tmp_path / "context.zip"
    attachment.write_bytes(b"zip")
    commands = []
    start_count = 0

    def runner(command, env, timeout):
        nonlocal start_count
        commands.append(command)
        if command[1] == "start":
            start_count += 1
            if start_count == 1:
                return raw_completed(
                    code=1,
                    stderr="CDP port 9222 is already backed by a headless agbrowse Chrome. Run agbrowse stop first.",
                )
            return raw_completed("Chrome started headed")
        if command[1] == "stop":
            return raw_completed("Chrome stopped")
        if command[1:3] == ["web-ai", "send"]:
            return completed({"ok": True, "status": "sent", "sessionId": "s", "targetId": "t"})
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        headed_runtime_preflight=True,
        mode_label="Pro",
        mode_variant=None,
        app_policy="forbidden",
        files=[str(attachment)],
    )

    sent = bridge.send(record["run_dir"])

    assert sent["phase"] == "SUBMITTED"
    assert [command[1] for command in commands] == ["start", "stop", "start", "web-ai"]
    assert any(
        event.get("kind") == "headless-runtime-safely-restarted-headed"
        for event in sent["recovery_events"]
    )


def test_headed_start_records_owned_blank_target_for_app_connector(tmp_path: Path):
    attachment = tmp_path / "context.zip"
    attachment.write_bytes(b"zip")

    def runner(command, env, timeout):
        if command[1] == "start":
            return raw_completed("Chrome started (CDP: http://127.0.0.1:9222)")
        if command[1:3] == ["web-ai", "send"]:
            return completed({"ok": True, "status": "sent", "sessionId": "s", "targetId": "t"})
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        headed_runtime_preflight=True,
        mode_label="Pro",
        mode_variant=None,
        app_policy="forbidden",
        files=[str(attachment)],
    )

    class Lifecycle:
        @staticmethod
        def list_tabs():
            return [
                {"targetId": "T-START", "url": "about:blank"},
                {"targetId": "T-FOREIGN", "url": "https://example.com/"},
            ]

        @staticmethod
        def record_owned(*args, **kwargs):
            return None

    bridge._tab_lifecycle = lambda executable, manifest: Lifecycle()
    sent = bridge.send(record["run_dir"])

    assert sent["phase"] in {"SUBMITTED", "RECOVERY_REQUIRED"}
    assert bridge._owned_startup_targets(sent) == {"T-START": "about:blank"}


def test_pro_headless_runtime_never_restarts_over_other_active_work(tmp_path: Path):
    attachment = tmp_path / "context.zip"
    attachment.write_bytes(b"zip")
    commands = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1] == "start":
            return raw_completed(
                code=1,
                stderr="CDP port 9222 is already backed by a headless agbrowse Chrome. Run agbrowse stop first.",
            )
        raise AssertionError(f"unsafe preflight must not continue: {command}")

    bridge, record = prepared_bridge(
        tmp_path,
        runner,
        headed_runtime_preflight=True,
        mode_label="Pro",
        mode_variant=None,
        app_policy="forbidden",
        files=[str(attachment)],
    )
    foreign_project = tmp_path / "foreign-project"
    foreign_project.mkdir()
    foreign_manifest = write_manifest(tmp_path / "foreign-manifest.json", question="foreign active work")
    foreign = bridge.store.create_run(
        project_root=str(foreign_project),
        manifest_path=str(foreign_manifest),
        agbrowse_contract={"executable": "agbrowse"},
    )
    bridge.store.transition(foreign["run_dir"], "PREFLIGHTED")

    blocked = bridge.send(record["run_dir"])

    assert blocked["phase"] == "PREFLIGHT_BLOCKED"
    assert [command[1] for command in commands] == ["start"]
    assert blocked["recovery_events"][-1]["kind"] == "headed-runtime-restart-deferred"
    assert blocked["recovery_events"][-1]["active_or_uncertain_runs"][0]["run_id"] == foreign["run_id"]


def test_work_surface_preflight_rejection_is_safe_send_rejected():
    envelope = {
        "error_code": "capability.unsupported",
        "error_stage": "provider-surface-preflight",
        "mutation_allowed": False,
        "message": "Work surface active",
    }
    assert BRIDGE.classify_pre_submit_failure(envelope) == "SEND_REJECTED"


def test_cdp_unreachable_with_no_mutation_is_safe_send_rejected():
    assert BRIDGE.classify_pre_submit_failure({
        "error_code": "cdp.unreachable",
        "error_stage": "connect",
        "mutation_allowed": False,
        "message": "start headed browser first",
    }) == "SEND_REJECTED"


def test_cdp_headless_with_no_mutation_is_safe_send_rejected():
    assert BRIDGE.classify_pre_submit_failure({
        "error_code": "cdp.headless",
        "error_stage": "connect",
        "mutation_allowed": False,
        "message": "restart headed",
    }) == "SEND_REJECTED"


def test_provider_active_capacity_with_no_mutation_is_safe_send_rejected():
    assert BRIDGE.classify_pre_submit_failure({
        "error_code": "provider.active-capacity",
        "error_stage": "provider-capacity",
        "mutation_allowed": False,
        "message": "provider active tab capacity exceeded: active-max-per-key 5/5",
    }) == "SEND_REJECTED"


def test_global_composer_lock_default_covers_full_send_window():
    assert BRIDGE.composer_lock_timeout_seconds({"send_timeout_seconds": 600}) == 1200
    assert BRIDGE.composer_lock_timeout_seconds({"send_timeout_seconds": 30}) == 630
    assert BRIDGE.composer_lock_timeout_seconds({"composer_lock_timeout_seconds": 45}) == 45


def test_read_contract_rejects_tampered_pinned_integrity(tmp_path: Path):
    contract = write_contract(tmp_path / "contract.json")
    payload = json.loads(contract.read_text(encoding="utf-8"))
    payload["agbrowse"]["npmIntegrity"] = "sha512-tampered"
    contract.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BRIDGE.BridgeError) as error:
        BRIDGE.read_contract(contract)
    assert error.value.code == "AGBROWSE_CONTRACT_INVALID"


def test_non_json_send_becomes_terminal_uncertain_block(tmp_path: Path):
    def runner(command, env, timeout):
        return subprocess.CompletedProcess(command, 1, "not json", "boom")

    bridge, record = prepared_bridge(tmp_path, runner)
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge.send(str(run_dir))
    assert error.value.code == "AGBROWSE_JSON_INVALID"
    _, blocked = bridge.store.load(run_dir)
    assert blocked["phase"] == "SUBMISSION_UNCERTAIN_IDENTITY_MISSING"


def test_poll_completion_writes_answer_and_releases_project(tmp_path: Path):
    responses = [
        completed({"ok": True, "status": "sent", "sessionId": "s", "targetId": "t"}),
        completed(
            {
                "ok": True,
                "status": "complete",
                "sessionId": "s",
                "targetId": "t",
                "conversationUrl": "https://chatgpt.com/c/c1",
                "answerText": "final answer",
            }
        ),
    ]

    def runner(command, env, timeout):
        return responses.pop(0)

    bridge, record = prepared_bridge(tmp_path, runner)
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    bridge.send(str(run_dir))
    done = bridge.poll(str(run_dir), timeout_seconds=10)
    assert done["phase"] == "COMPLETE"
    assert (run_dir / "answer.md").read_text(encoding="utf-8").strip() == "final answer"
    assert not (run_dir.parent.parent / "active.lock").exists()


def test_poll_terminal_stream_error_is_not_accepted_as_complete(tmp_path: Path):
    poisoned = (
        "partial assistant result\n\n"
        "메시지 스트림에 오류 발생\n\n"
        "다시 시도"
    )
    responses = [
        completed({"ok": True, "status": "sent", "sessionId": "s-error", "targetId": "t-error"}),
        completed(
            {
                "ok": True,
                "status": "complete",
                "sessionId": "s-error",
                "targetId": "t-error",
                "conversationUrl": "https://chatgpt.com/c/stream-error",
                "answerText": poisoned,
            }
        ),
    ]
    bridge, record = prepared_bridge(tmp_path, lambda command, env, timeout: responses.pop(0))
    run_dir = Path(record["run_dir"])

    bridge.send(str(run_dir))
    failed = bridge.poll(str(run_dir), timeout_seconds=10)

    assert failed["phase"] == "PROVIDER_FAILED_TERMINAL"
    assert failed["terminal_block_code"] == "PROVIDER_TERMINAL_ERROR_UI"
    assert failed["result"] is None
    assert not (run_dir / "answer.md").exists()
    failure_path = run_dir / "provider-terminal-failure.md"
    assert failure_path.read_text(encoding="utf-8").strip() == poisoned
    event = failed["recovery_events"][-1]
    assert event["kind"] == "provider-terminal-error-ui"
    assert event["signature"] == "chatgpt-stream-error-retry-v1"
    assert event["answer_sha256"] == hashlib.sha256(failure_path.read_bytes()).hexdigest()
    assert not (run_dir.parent.parent / "active.lock").exists()


def test_provider_error_words_in_ordinary_prose_do_not_false_match():
    assert BRIDGE.provider_terminal_error_ui(
        "The UI may say 메시지 스트림에 오류 발생 and offer 다시 시도, but this is the complete analysis."
    ) is None


def test_cleaned_standalone_provider_failure_allows_bounded_fresh_prepare(tmp_path: Path):
    poisoned = "partial\n\n메시지 스트림에 오류 발생\n\n다시 시도"
    responses = [
        completed({"ok": True, "status": "sent", "sessionId": "s-first", "targetId": "t-first"}),
        completed(
            {
                "ok": True,
                "status": "complete",
                "sessionId": "s-first",
                "targetId": "t-first",
                "conversationUrl": "https://chatgpt.com/c/first-failed",
                "answerText": poisoned,
            }
        ),
    ]
    bridge, first = prepared_bridge(tmp_path, lambda command, env, timeout: responses.pop(0))
    bridge.send(first["run_dir"])
    failed = bridge.poll(first["run_dir"], timeout_seconds=10)
    assert failed["phase"] == "PROVIDER_FAILED_TERMINAL"

    cleanup = bridge.cleanup_completed(first["run_dir"], explicit_user_request=True)
    assert cleanup["state"] == "closed-and-absent"
    _, settled = bridge.store.load(first["run_dir"])
    assert settled["cleanup_pending"] is False
    assert settled["owned_tab_state"] == "closed-and-absent"

    second = bridge.prepare(
        project_root=str(tmp_path / "project"),
        manifest_path=str(tmp_path / "manifest.json"),
        contract_path=str(tmp_path / "contract.json"),
    )

    assert second["run_id"] != first["run_id"]
    assert second["phase"] == "PREFLIGHTED"


def test_completed_cleanup_rebinds_one_exact_url_target_after_browser_restart(tmp_path: Path):
    bridge, record = prepared_bridge(tmp_path, lambda *_: completed({"ok": True}))
    run_dir = Path(record["run_dir"])
    bridge.store.transition(run_dir, "LEASED")
    bridge.store.transition(run_dir, "SEND_STARTED")
    bridge.store.transition(
        run_dir,
        "SUBMITTED",
        session_id="session-restarted",
        target_id="target-old",
        submission_receipt={"test": True},
    )
    bridge.store.transition(
        run_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/restarted",
    )
    answer_path = run_dir / "answer.md"
    answer_path.write_text("complete answer\n", encoding="utf-8")
    descriptor = {
        "path": str(answer_path),
        "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
    }
    bridge.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
    bridge.store.transition(run_dir, "VERIFIED")
    bridge.store.transition(run_dir, "COMPLETE")

    class TargetMismatch(Exception):
        code = "TAB_COMPLETED_TARGET_MISMATCH"

    class RestartLifecycle:
        def __init__(self):
            self.close_calls = 0

        def _evidence(self):
            path = run_dir / "tab-lifecycle.json"
            path.write_text(json.dumps({"events": [{"kind": "terminal-exact-url-rebind-candidate"}]}), encoding="utf-8")
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        def close_completed(self, _run_dir, *, explicit_user_request):
            assert explicit_user_request is True
            self.close_calls += 1
            if self.close_calls == 1:
                raise TargetMismatch("old target is absent")
            return {
                "ok": True,
                "state": "closed-and-absent",
                "target_id": "target-new",
                "conversation_url": "https://chatgpt.com/c/restarted",
                "evidence": self._evidence(),
            }

        def terminal_rebind_candidate(self, _run_dir):
            return {
                "ok": True,
                "phase": "COMPLETE",
                "conversation_url": "https://chatgpt.com/c/restarted",
                "old_target_id": "target-old",
                "new_target_id": "target-new",
                "old_target_absent": True,
                "url_match_count": 1,
                "foreign_owner_absent": True,
                "tabs_sha256": "b" * 64,
                "evidence": self._evidence(),
            }

    lifecycle = RestartLifecycle()
    bridge.tab_lifecycle_factory = lambda _executable, _manifest: lifecycle

    cleanup = bridge.cleanup_completed(str(run_dir), explicit_user_request=True)
    _, rebound = bridge.store.load(run_dir)

    assert cleanup["state"] == "closed-and-absent"
    assert lifecycle.close_calls == 2
    assert rebound["current_target_id"] == "target-new"
    assert rebound["owned_tab_state"] == "closed-and-absent"
    assert rebound["target_rebind_events"][-1]["reason"] == "terminal-exact-url-after-browser-restart"


def test_completed_cleanup_removes_exact_recovery_utility_before_terminal_rebind(tmp_path: Path):
    bridge, record = prepared_bridge(tmp_path, lambda *_: completed({"ok": True}))
    run_dir = Path(record["run_dir"])
    bridge.store.transition(run_dir, "LEASED")
    bridge.store.transition(run_dir, "SEND_STARTED")
    bridge.store.transition(
        run_dir,
        "SUBMITTED",
        session_id="session-duplicate",
        target_id="target-utility",
        submission_receipt={"test": True},
    )
    bridge.store.transition(run_dir, "URL_BOUND", conversation_url="https://chatgpt.com/c/duplicate")
    answer_path = run_dir / "answer.md"
    answer_path.write_text("complete answer\n", encoding="utf-8")
    descriptor = {
        "path": str(answer_path),
        "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
    }
    bridge.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
    bridge.store.transition(run_dir, "VERIFIED")
    bridge.store.transition(run_dir, "COMPLETE")

    class UrlAmbiguous(Exception):
        code = "TAB_COMPLETED_URL_AMBIGUOUS"

    class TargetMismatch(Exception):
        code = "TAB_COMPLETED_TARGET_MISMATCH"

    class DuplicateLifecycle:
        def __init__(self):
            self.events = []
            self.close_calls = 0

        def _evidence(self):
            path = run_dir / "tab-lifecycle.json"
            path.write_text(json.dumps({"events": self.events}), encoding="utf-8")
            return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        def close_completed(self, _run_dir, *, explicit_user_request):
            assert explicit_user_request is True
            self.close_calls += 1
            if self.close_calls == 1:
                raise UrlAmbiguous("original and utility share one URL")
            if self.close_calls == 2:
                raise TargetMismatch("utility target is now absent")
            self.events.append({"kind": "owned-complete-auto-cleanup", "target_id": "target-original"})
            return {
                "ok": True,
                "state": "closed-and-absent",
                "target_id": "target-original",
                "conversation_url": "https://chatgpt.com/c/duplicate",
                "evidence": self._evidence(),
            }

        def close_terminal_recovery_utilities(self, _run_dir, *, explicit_user_request):
            assert explicit_user_request is True
            self.events.append({"kind": "terminal-recovery-utility-cleanup", "target_id": "target-utility"})
            return {"ok": True, "closed_target_ids": ["target-utility"], "evidence": self._evidence()}

        def terminal_rebind_candidate(self, _run_dir):
            self.events.append({"kind": "terminal-exact-url-rebind-candidate"})
            return {
                "ok": True,
                "phase": "COMPLETE",
                "conversation_url": "https://chatgpt.com/c/duplicate",
                "old_target_id": "target-utility",
                "new_target_id": "target-original",
                "old_target_absent": True,
                "url_match_count": 1,
                "foreign_owner_absent": True,
                "tabs_sha256": "b" * 64,
                "evidence": self._evidence(),
            }

    lifecycle = DuplicateLifecycle()
    bridge.tab_lifecycle_factory = lambda _executable, _manifest: lifecycle

    cleanup = bridge.cleanup_completed(str(run_dir), explicit_user_request=True)
    _, rebound = bridge.store.load(run_dir)

    assert cleanup["target_id"] == "target-original"
    assert lifecycle.close_calls == 3
    assert [event["kind"] for event in lifecycle.events] == [
        "terminal-recovery-utility-cleanup",
        "terminal-exact-url-rebind-candidate",
        "owned-complete-auto-cleanup",
    ]
    assert rebound["current_target_id"] == "target-original"
    assert rebound["owned_tab_state"] == "closed-and-absent"


def test_unresolved_send_click_trace_is_not_treated_as_proof_of_no_submission(tmp_path: Path):
    responses = [
        completed({"ok": True, "status": "sent", "sessionId": "s-trace"}),
        completed({
            "ok": True,
            "session": {
                "sessionId": "s-trace",
                "status": "sent",
                "targetId": "t-trace",
                "conversationUrl": "https://chatgpt.com/",
                "trace": [{
                    "stage": "send.click",
                    "status": "unresolved",
                    "reason": "not-enabled",
                }],
            },
        }),
        completed({
            "ok": True,
            "status": "complete",
            "sessionId": "s-trace",
            "targetId": "t-trace",
            "conversationUrl": "https://chatgpt.com/c/trace-success",
            "answerText": "trace was not proof of rejection",
        }),
    ]

    bridge, record = prepared_bridge(tmp_path, lambda command, env, timeout: responses.pop(0))
    sent = bridge.send(record["run_dir"])
    done = bridge.poll(record["run_dir"], timeout_seconds=10)

    assert sent["phase"] == "SUBMITTED"
    assert sent["conversation_url"] is None
    assert done["phase"] == "COMPLETE"
    assert done["conversation_url"] == "https://chatgpt.com/c/trace-success"


def test_recovery_doctor_rebinds_only_saved_canonical_url(tmp_path: Path):
    responses = [
        completed(
            {
                "ok": True,
                "status": "sent",
                "sessionId": "s",
                "targetId": "old",
                "conversationUrl": "https://chatgpt.com/c/c1",
            }
        ),
        raw_completed("[]"),
        completed(
            {
                "ok": True,
                "status": "reattached",
                "sessionId": "s",
                "targetId": "new",
                "conversationUrl": "https://chatgpt.com/c/c1",
            }
        ),
        raw_completed(
            json.dumps(
                [
                    {
                        "targetId": "new",
                        "url": "https://chatgpt.com/c/c1",
                        "type": "page",
                    }
                ]
            )
        ),
    ]

    def runner(command, env, timeout):
        return responses.pop(0)

    bridge, record = prepared_bridge(tmp_path, runner)
    run_dir = tmp_path / "state" / "projects" / record["project_key"] / "runs" / record["run_id"]
    bridge.send(str(run_dir))
    bridge.store.transition(str(run_dir), "RECOVERY_REQUIRED", recovery_event={"reason": "lost"})
    recovered = bridge.recover(str(run_dir))
    assert recovered["phase"] == "URL_BOUND"
    assert recovered["current_target_id"] == "new"
    assert recovered["target_rebind_events"][0]["old_target_id"] == "old"
    assert recovered["conversation_url"] == "https://chatgpt.com/c/c1"


def test_recovery_doctor_captures_completed_exact_url_without_second_poll(tmp_path: Path):
    commands: list[list[str]] = []
    mode = {"recovery": False}

    def runner(command, env, timeout):
        commands.append(command)
        if not mode["recovery"]:
            return completed(
                {
                    "ok": True,
                    "status": "sent",
                    "sessionId": "session-stale-poll",
                    "targetId": "target-exact",
                    "conversationUrl": "https://chatgpt.com/c/exact-complete",
                }
            )
        if command[1:] == ["tabs", "--json"]:
            return raw_completed(
                json.dumps(
                    [
                        {
                            "targetId": "target-exact",
                            "url": "https://chatgpt.com/c/exact-complete",
                            "type": "page",
                        }
                    ]
                )
            )
        if command[1:3] == ["web-ai", "sessions"]:
            return completed(
                {
                    "ok": True,
                    "status": "reattached",
                    "sessionId": "session-stale-poll",
                    "targetId": "stale-root-target",
                    "conversationUrl": "https://chatgpt.com/",
                }
            )
        if command[1] == "tab-switch":
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed(
                {
                    "ok": True,
                    "targetId": "target-exact",
                    "url": "https://chatgpt.com/c/exact-complete",
                }
            )
        if command[1:3] == ["web-ai", "status"]:
            return completed(
                {
                    "ok": True,
                    "status": "ready",
                    "url": "https://chatgpt.com/c/exact-complete",
                    "capabilities": [
                        {
                            "capabilityId": "chatgpt-response-streaming",
                            "state": "ok",
                            "evidence": {"streaming": False},
                        }
                    ],
                }
            )
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({"snapshotId": "snapshot-exact", "text": "final exact answer"})
        if command[1] == "text":
            return raw_completed(
                "prompt-run-owned.txt\n"
                "더 보기\n"
                "87m 33s 동안 처리함\n"
                "CODEX_EXECUTION_SUMMARY\n\n"
                "final exact answer\n\n"
                "출처\n"
                "ChatGPT는 실수를 할 수 있습니다. 중요한 정보는 재차 확인하세요."
            )
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "stale-poll"})
    mode["recovery"] = True

    recovered = bridge.recover(run_dir)

    assert recovered["phase"] == "COMPLETE"
    assert recovered["result"]["provider_status"] == "exact-url-adjudicated-terminal"
    assert Path(recovered["result"]["path"]).read_text(encoding="utf-8").strip() == (
        "CODEX_EXECUTION_SUMMARY\n\nfinal exact answer"
    )
    assert Path(run_dir, "exact-url-adjudication.json").is_file()
    assert not any(command[1:3] == ["web-ai", "poll"] for command in commands)
    assert not any(command[1:3] == ["web-ai", "sessions"] for command in commands)


def test_poll_captures_completed_exact_url_before_long_session_poll(tmp_path: Path):
    commands: list[list[str]] = []

    def runner(command, env, timeout):
        commands.append(command)
        if command[1:3] == ["web-ai", "send"]:
            return completed(
                {
                    "ok": True,
                    "status": "sent",
                    "sessionId": "stale-session",
                    "targetId": "exact-target",
                    "conversationUrl": "https://chatgpt.com/c/already-finished",
                }
            )
        if command[1:] == ["tabs", "--json"]:
            return raw_completed(json.dumps([{"targetId": "exact-target", "url": "https://chatgpt.com/c/already-finished"}]))
        if command[1] == "tab-switch":
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed({"targetId": "exact-target", "url": "https://chatgpt.com/c/already-finished"})
        if command[1:3] == ["web-ai", "status"]:
            return completed(
                {
                    "ok": True,
                    "capabilities": [
                        {
                            "capabilityId": "chatgpt-response-streaming",
                            "evidence": {"streaming": False},
                        }
                    ],
                }
            )
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({"text": "3m 10s 동안 처리함\nFINAL_RESULT\nfinished"})
        if command[1] == "text":
            return raw_completed("prompt-owned.txt\n더 보기\n3m 10s 동안 처리함\nFINAL_RESULT\nfinished\n출처\nChatGPT는 실수를 할 수 있습니다.")
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(
        run_dir,
        "RECOVERY_REQUIRED",
        target_id="exact-target",
        recovery_event={"kind": "test-exact-terminal"},
    )
    bridge.store.transition(run_dir, "RECOVERING")
    bridge.store.transition(
        run_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/already-finished",
    )
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "stale-poll"})

    done = bridge.poll(run_dir, timeout_seconds=14400)

    assert done["phase"] == "COMPLETE"
    assert not any(command[1:3] == ["web-ai", "poll"] for command in commands)


def test_terminal_visible_answer_without_chatgpt_said_label():
    page_text = (
        "prompt-run-owned.txt\n"
        "더 보기\n"
        "87m 33s 동안 처리함\n"
        "CODEX_EXECUTION_SUMMARY\n\n"
        "status: COMPLETE\n"
        "files: 3\n\n"
        "출처\n"
        "ChatGPT는 실수를 할 수 있습니다. 중요한 정보는 재차 확인하세요.\n"
        "매우 높음\n"
    )

    assert BRIDGE._terminal_visible_assistant_answer(page_text) == (
        "CODEX_EXECUTION_SUMMARY\n\nstatus: COMPLETE\nfiles: 3"
    )


def test_run_owned_prompt_alias_is_unique_hashed_and_used_for_send(tmp_path: Path):
    bridge, record = prepared_bridge(tmp_path, lambda *_: completed({"ok": True}))
    manifest = BRIDGE.STATE.load_manifest(Path(record["manifest_path"]))
    identity = record["recovery_identity"]
    alias = Path(identity["attachment_path"])

    assert identity["attachment_name"] == f"prompt-{record['run_id']}.txt"
    assert alias.is_file()
    assert hashlib.sha256(alias.read_bytes()).hexdigest() == record["prompt_sha256"]

    command = BRIDGE.build_send_command(record, manifest, "agbrowse")
    attached = [command[index + 1] for index, item in enumerate(command) if item == "--file"]
    assert str(alias) in attached
    assert str(Path(manifest["prompt_file"]).resolve()) not in attached


def test_core_snapshot_recent_chat_refs_start_after_chat_section():
    snapshot = """e1   link       \"콘텐츠로 건너뛰기\"
e12  link       \"홈\"
e52  button     \"채팅\"
e55  link       \"새 채팅\"
e58  link       \"Task Instructions Compliance\"
e63  link       \"Instruction Compliance\"
e70  button     \"사용자 프로필 메뉴 열기\"
"""
    assert BRIDGE._recent_chat_refs({"text": snapshot}, limit=10) == [
        {"ref": "e58", "name": "Task Instructions Compliance"},
        {"ref": "e63", "name": "Instruction Compliance"},
    ]


def test_legacy_recovery_contract_requires_nonce_and_two_corroborators(tmp_path: Path):
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(
        'Return {"workflow_id":"wf-1","stage":"review","nonce":"0123456789abcdef0123456789abcdef",'
        '"input_plan_sha256":"' + ("a" * 64) + '"}.',
        encoding="utf-8",
    )
    manifest = {
        "question": PROMPT_FILE_HANDOFF,
        "prompt_transport": "file",
        "prompt_file": str(prompt),
        "prompt_file_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        "files": [str(prompt)],
    }
    contract = BRIDGE._recovery_marker_contract({"run_id": "legacy", "recovery_identity": None}, manifest)
    matching = json.dumps({
        "workflow_id": "wf-1",
        "stage": "review",
        "nonce": "0123456789abcdef0123456789abcdef",
        "input_plan_sha256": "a" * 64,
    })
    assert BRIDGE._candidate_matches_recovery_contract(matching, contract) is True
    assert BRIDGE._candidate_matches_recovery_contract('0123456789abcdef0123456789abcdef wf-1', contract) is False


def test_blocked_run_history_adjudication_rebinds_captures_and_cleans(tmp_path: Path):
    commands: list[list[str]] = []
    mode = {"recovery": False}
    alias_name = {"value": ""}
    tab_live = {"value": True}

    def runner(command, env, timeout):
        commands.append(command)
        if not mode["recovery"]:
            return completed({
                "ok": True,
                "status": "sent",
                "sessionId": "session-lost",
                "targetId": "target-lost",
            })
        if command[1:3] == ["web-ai", "sessions"]:
            return completed({
                "ok": True,
                "status": "session-doctor",
                "sessionId": "session-lost",
                "targetId": "target-lost",
                "conversationUrl": "https://chatgpt.com/",
            })
        if command[1:] == ["tabs", "--json"]:
            tabs = [{
                "targetId": "target-recovery",
                "url": "about:blank",
                "title": "about:blank",
                "lastActiveAt": None,
            }] if tab_live["value"] else []
            return raw_completed(json.dumps(tabs))
        if command[1] == "new-tab":
            raise AssertionError("the single untracked startup blank must be reused as owned")
        if command[1] == "tab-close":
            tab_live["value"] = False
            return raw_completed("ok")
        if command[1] in {"tab-switch", "navigate", "click"}:
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed({
                "ok": True,
                "targetId": "target-recovery",
                "url": "https://chatgpt.com/c/recovered-1",
            })
        if command[1:3] == ["web-ai", "status"]:
            return completed({
                "ok": True,
                "status": "ready",
                "url": "https://chatgpt.com/c/recovered-1",
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "state": "ok",
                    "evidence": {"streaming": False},
                }],
            })
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({
                "snapshotId": "snapshot-1",
                "text": f'- main:\n  - group "{alias_name["value"]}"\n  - text: "final answer body"',
                "refs": {},
            })
        if command[1] == "snapshot":
            return raw_completed(
                'e52  button  "채팅"\n'
                'e55  link    "새 채팅"\n'
                'e58  link    "Recovered conversation"\n'
            )
        if command[1] == "text":
            return raw_completed("ChatGPT said:\nfinal answer body\nChatGPT can make mistakes")
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    alias_name["value"] = record["recovery_identity"]["attachment_name"]
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "lost-target"})
    bridge.store.transition(run_dir, "RECOVERING")
    bridge.store.transition(run_dir, "BLOCKED_RECOVERY_EXHAUSTED")
    mode["recovery"] = True

    recovered = bridge.recover(run_dir)

    assert recovered["phase"] == "COMPLETE"
    assert recovered["conversation_url"] == "https://chatgpt.com/c/recovered-1"
    assert recovered["result"]["provider_status"] == "history-adjudicated-terminal"
    assert Path(recovered["result"]["path"]).read_text(encoding="utf-8").strip() == "final answer body"
    assert not (Path(run_dir).parent.parent / "active.lock").exists()
    assert any(command[1:3] == ["web-ai", "sessions"] for command in commands)
    assert any(command[1] == "snapshot" for command in commands)
    assert not any(command[1] == "new-tab" for command in commands)
    assert any(command[1] == "tab-close" and command[2] == "target-recovery" for command in commands)


def test_prepare_settlement_auto_recovers_exact_stale_run(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    bridge = BRIDGE.Bridge(state_root=tmp_path / "state", runner=lambda *_: completed({"ok": True}))
    calls = []
    monkeypatch.setattr(
        bridge.store,
        "reconcile_project_lock",
        lambda root, apply_safe_pre_submission=False: {
            "ok": False,
            "state": "STALE_OWNER_UNRESOLVED_SUBMISSION",
            "run_id": "old-run",
        },
    )
    monkeypatch.setattr(
        bridge.store,
        "paths",
        lambda root, run_id: SimpleNamespace(runs_dir=tmp_path / "runs"),
    )

    def recovered(run_dir):
        calls.append(run_dir)
        return {
            "phase": "COMPLETE",
            "conversation_url": "https://chatgpt.com/c/old-run",
            "target_rebind_events": [{"reason": "history-fingerprint-adjudication"}],
        }

    monkeypatch.setattr(bridge, "recover", recovered)
    result = bridge._settle_stale_project_before_prepare(str(project))

    assert result["state"] == "STALE_SUBMISSION_ADJUDICATED_COMPLETE"
    assert calls == [str(tmp_path / "runs" / "old-run")]


def test_prepare_settlement_never_replaces_unresolved_original(monkeypatch, tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    bridge = BRIDGE.Bridge(state_root=tmp_path / "state", runner=lambda *_: completed({"ok": True}))
    monkeypatch.setattr(
        bridge.store,
        "reconcile_project_lock",
        lambda root, apply_safe_pre_submission=False: {
            "ok": False,
            "state": "STALE_OWNER_UNRESOLVED_SUBMISSION",
            "run_id": "old-run",
        },
    )
    monkeypatch.setattr(
        bridge.store,
        "paths",
        lambda root, run_id: SimpleNamespace(runs_dir=tmp_path / "runs"),
    )
    monkeypatch.setattr(
        bridge,
        "recover",
        lambda run_dir: {
            "phase": "RESPONSE_IN_PROGRESS",
            "conversation_url": "https://chatgpt.com/c/old-run",
            "target_rebind_events": [{"reason": "history-fingerprint-adjudication"}],
        },
    )

    with pytest.raises(BRIDGE.BridgeError) as error:
        bridge._settle_stale_project_before_prepare(str(project))

    assert error.value.code == "STALE_PROJECT_RECOVERY_PENDING"
    assert error.value.evidence["phase"] == "RESPONSE_IN_PROGRESS"


def test_blocked_run_history_adjudication_preserves_owned_target_captures_and_cleans(tmp_path: Path):
    commands: list[list[str]] = []
    mode = {"recovery": False}
    alias_name = {"value": ""}
    tab_live = {"value": True}

    def runner(command, env, timeout):
        commands.append(command)
        if not mode["recovery"]:
            return completed({
                "ok": True,
                "status": "sent",
                "sessionId": "session-lost",
                "targetId": "target-lost",
            })
        if command[1:3] == ["web-ai", "sessions"]:
            return completed({
                "ok": True,
                "status": "session-doctor",
                "sessionId": "session-lost",
                "targetId": "target-lost",
                "conversationUrl": "https://chatgpt.com/",
            })
        if command[1:] == ["tabs", "--json"]:
            tabs = [{
                "targetId": "target-recovery",
                "url": "about:blank",
                "title": "about:blank",
                "lastActiveAt": None,
            }] if tab_live["value"] else []
            return raw_completed(json.dumps(tabs))
        if command[1] == "new-tab":
            raise AssertionError("the single untracked startup blank must be reused as owned")
        if command[1] == "tab-close":
            tab_live["value"] = False
            return raw_completed("ok")
        if command[1] in {"tab-switch", "navigate", "click"}:
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed({
                "ok": True,
                "targetId": "target-recovery",
                "url": "https://chatgpt.com/c/recovered-1",
            })
        if command[1:3] == ["web-ai", "status"]:
            return completed({
                "ok": True,
                "status": "ready",
                "url": "https://chatgpt.com/c/recovered-1",
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "state": "ok",
                    "evidence": {"streaming": False},
                }],
            })
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({
                "snapshotId": "snapshot-1",
                "text": f'- main:\n  - group "{alias_name["value"]}"\n  - text: "final answer body"',
                "refs": {},
            })
        if command[1] == "snapshot":
            return raw_completed(
                'e52  button  "채팅"\n'
                'e55  link    "새 채팅"\n'
                'e58  link    "Recovered conversation"\n'
            )
        if command[1] == "text":
            return raw_completed("ChatGPT said:\nfinal answer body\nChatGPT can make mistakes")
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    alias_name["value"] = record["recovery_identity"]["attachment_name"]
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "lost-target"})
    bridge.store.transition(run_dir, "RECOVERING")
    bridge.store.transition(run_dir, "BLOCKED_RECOVERY_EXHAUSTED")
    mode["recovery"] = True

    recovered = bridge.recover(run_dir)

    assert recovered["phase"] == "COMPLETE"
    assert recovered["conversation_url"] == "https://chatgpt.com/c/recovered-1"
    assert recovered["current_target_id"] == "target-lost"
    assert recovered["target_rebind_events"] == []
    assert recovered["result"]["provider_status"] == "history-adjudicated-terminal"
    assert Path(recovered["result"]["path"]).read_text(encoding="utf-8").strip() == "final answer body"
    assert not (Path(run_dir).parent.parent / "active.lock").exists()
    assert any(command[1:3] == ["web-ai", "sessions"] for command in commands)
    assert any(command[1] == "snapshot" for command in commands)
    assert not any(command[1] == "new-tab" for command in commands)
    assert any(command[1] == "tab-close" and command[2] == "target-recovery" for command in commands)

def test_completed_cleanup_closes_history_utility_without_adopting_it(tmp_path: Path):
    bridge, record = prepared_bridge(tmp_path, lambda *_: completed({"ok": True}))
    run_dir = Path(record["run_dir"])
    bridge.store.transition(run_dir, "LEASED")
    bridge.store.transition(run_dir, "SEND_STARTED")
    bridge.store.transition(
        run_dir,
        "SUBMITTED",
        session_id="session-history",
        target_id="target-owned",
        submission_receipt={"test": True},
    )
    bridge.store.transition(
        run_dir,
        "URL_BOUND",
        conversation_url="https://chatgpt.com/c/history-observer",
    )
    answer_path = run_dir / "answer.md"
    answer_path.write_text("complete answer\n", encoding="utf-8")
    descriptor = {
        "path": str(answer_path),
        "sha256": hashlib.sha256(answer_path.read_bytes()).hexdigest(),
        "bytes": answer_path.stat().st_size,
        "provider_status": "complete",
    }
    bridge.store.transition(run_dir, "RESULT_CAPTURED", result=descriptor)
    bridge.store.transition(run_dir, "VERIFIED")
    bridge.store.transition(run_dir, "COMPLETE")

    class TargetMismatch(Exception):
        code = "TAB_COMPLETED_TARGET_MISMATCH"

    class UtilityLifecycle:
        def __init__(self):
            self.close_calls = 0
            self.utility_cleanup_calls = 0

        def close_completed(self, _run_dir, *, explicit_user_request):
            assert explicit_user_request is True
            self.close_calls += 1
            if self.close_calls == 1:
                raise TargetMismatch("canonical URL is on the observer target")
            return {
                "ok": True,
                "state": "already-absent",
                "conversation_url": "https://chatgpt.com/c/history-observer",
            }

        def close_terminal_recovery_utilities(self, _run_dir, *, explicit_user_request):
            assert explicit_user_request is True
            self.utility_cleanup_calls += 1
            return {
                "ok": True,
                "state": "closed-and-absent",
                "closed_target_ids": ["target-history-utility"],
                "conversation_url": "https://chatgpt.com/c/history-observer",
            }

        def terminal_rebind_candidate(self, _run_dir):
            raise AssertionError("a history utility target must never become the child target")

    lifecycle = UtilityLifecycle()
    bridge.tab_lifecycle_factory = lambda _executable, _manifest: lifecycle

    cleanup = bridge.cleanup_completed(str(run_dir), explicit_user_request=True)
    _, settled = bridge.store.load(run_dir)

    assert cleanup["state"] == "already-absent"
    assert lifecycle.close_calls == 2
    assert lifecycle.utility_cleanup_calls == 1
    assert settled["current_target_id"] == "target-owned"
    assert settled["target_rebind_events"] == []
    assert settled["owned_open_tabs"] == 0

def test_history_adjudication_blocks_two_exact_fingerprint_matches(tmp_path: Path):
    mode = {"recovery": False}
    alias_name = {"value": ""}
    tab_live = {"value": True}
    current_ref = {"value": ""}

    def candidate_url() -> str:
        return {
            "e58": "https://chatgpt.com/c/recovered-a",
            "e59": "https://chatgpt.com/c/recovered-b",
        }[current_ref["value"]]

    def runner(command, env, timeout):
        if not mode["recovery"]:
            return completed({
                "ok": True,
                "status": "sent",
                "sessionId": "session-lost",
                "targetId": "target-lost",
            })
        if command[1:3] == ["web-ai", "sessions"]:
            return completed({
                "ok": True,
                "status": "session-doctor",
                "sessionId": "session-lost",
                "targetId": "target-lost",
                "conversationUrl": "https://chatgpt.com/",
            })
        if command[1:] == ["tabs", "--json"]:
            tabs = [{
                "targetId": "target-recovery",
                "url": "about:blank",
                "title": "about:blank",
                "lastActiveAt": None,
            }] if tab_live["value"] else []
            return raw_completed(json.dumps(tabs))
        if command[1] == "tab-close":
            tab_live["value"] = False
            return raw_completed("ok")
        if command[1] == "tab-switch":
            return raw_completed("ok")
        if command[1] == "navigate":
            current_ref["value"] = ""
            return raw_completed("ok")
        if command[1] == "click":
            current_ref["value"] = command[2]
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed({
                "ok": True,
                "targetId": "target-recovery",
                "url": candidate_url(),
            })
        if command[1:3] == ["web-ai", "status"]:
            return completed({
                "ok": True,
                "status": "ready",
                "url": candidate_url(),
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "state": "ok",
                    "evidence": {"streaming": False},
                }],
            })
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({
                "snapshotId": f"snapshot-{current_ref['value']}",
                "text": f'- main:\n  - group "{alias_name["value"]}"\n  - text: "final answer body"',
                "refs": {},
            })
        if command[1] == "snapshot":
            return raw_completed(
                'e52  button  "채팅"\n'
                'e55  link    "새 채팅"\n'
                'e58  link    "Recovered conversation A"\n'
                'e59  link    "Recovered conversation B"\n'
            )
        if command[1] == "text":
            return raw_completed("ChatGPT said:\nfinal answer body\nChatGPT can make mistakes")
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    alias_name["value"] = record["recovery_identity"]["attachment_name"]
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "lost-target"})
    bridge.store.transition(run_dir, "RECOVERING")
    bridge.store.transition(run_dir, "BLOCKED_RECOVERY_EXHAUSTED")
    mode["recovery"] = True

    recovered = bridge.recover(run_dir)

    assert recovered["phase"] == "BLOCKED_TARGET_AMBIGUOUS"
    assert recovered["terminal_block_code"] == "HISTORY_FINGERPRINT_AMBIGUOUS"
    assert recovered["conversation_url"] is None
    assert recovered["current_target_id"] == "target-lost"
    assert recovered["target_rebind_events"] == []
    adjudication = json.loads(Path(run_dir, "history-adjudication.json").read_text(encoding="utf-8"))
    assert adjudication["outcome"] == "ambiguous-exact-matches"
    assert set(adjudication["exact_match_urls"]) == {
        "https://chatgpt.com/c/recovered-a",
        "https://chatgpt.com/c/recovered-b",
    }


@pytest.mark.parametrize("failure_kind", ["click", "read"])
def test_history_adjudication_blocks_one_match_when_another_candidate_is_unread(
    tmp_path: Path,
    failure_kind: str,
):
    mode = {"recovery": False}
    alias_name = {"value": ""}
    tab_live = {"value": True}
    current_ref = {"value": ""}

    def candidate_url() -> str:
        return {
            "e58": "https://chatgpt.com/c/recovered-exact",
            "e59": "https://chatgpt.com/c/recovered-unread",
        }[current_ref["value"]]

    def runner(command, env, timeout):
        if not mode["recovery"]:
            return completed({
                "ok": True,
                "status": "sent",
                "sessionId": "session-lost",
                "targetId": "target-lost",
            })
        if command[1:3] == ["web-ai", "sessions"]:
            return completed({
                "ok": True,
                "status": "session-doctor",
                "sessionId": "session-lost",
                "targetId": "target-lost",
                "conversationUrl": "https://chatgpt.com/",
            })
        if command[1:] == ["tabs", "--json"]:
            tabs = [{
                "targetId": "target-recovery",
                "url": "about:blank",
                "title": "about:blank",
                "lastActiveAt": None,
            }] if tab_live["value"] else []
            return raw_completed(json.dumps(tabs))
        if command[1] == "tab-close":
            tab_live["value"] = False
            return raw_completed("ok")
        if command[1] == "tab-switch":
            return raw_completed("ok")
        if command[1] == "navigate":
            current_ref["value"] = ""
            return raw_completed("ok")
        if command[1] == "click":
            current_ref["value"] = command[2]
            if failure_kind == "click" and command[2] == "e59":
                return raw_completed(code=1, stderr="transient click failure")
            return raw_completed("ok")
        if command[1:] == ["active-tab", "--json"]:
            return completed({
                "ok": True,
                "targetId": "target-recovery",
                "url": candidate_url(),
            })
        if command[1:3] == ["web-ai", "status"]:
            if failure_kind == "read" and current_ref["value"] == "e59":
                return raw_completed(code=1, stderr="transient read failure")
            return completed({
                "ok": True,
                "status": "ready",
                "url": candidate_url(),
                "capabilities": [{
                    "capabilityId": "chatgpt-response-streaming",
                    "state": "ok",
                    "evidence": {"streaming": False},
                }],
            })
        if command[1:3] == ["web-ai", "snapshot"]:
            return completed({
                "snapshotId": f"snapshot-{current_ref['value']}",
                "text": f'- main:\n  - group "{alias_name["value"]}"\n  - text: "final answer body"',
                "refs": {},
            })
        if command[1] == "snapshot":
            return raw_completed(
                'e52  button  "채팅"\n'
                'e55  link    "새 채팅"\n'
                'e58  link    "Recovered exact conversation"\n'
                'e59  link    "Recovered unread conversation"\n'
            )
        if command[1] == "text":
            return raw_completed("ChatGPT said:\nfinal answer body\nChatGPT can make mistakes")
        raise AssertionError(f"unexpected command: {command}")

    bridge, record = prepared_bridge(tmp_path, runner)
    alias_name["value"] = record["recovery_identity"]["attachment_name"]
    run_dir = record["run_dir"]
    bridge.send(run_dir)
    bridge.store.transition(run_dir, "RECOVERY_REQUIRED", recovery_event={"kind": "lost-target"})
    bridge.store.transition(run_dir, "RECOVERING")
    bridge.store.transition(run_dir, "BLOCKED_RECOVERY_EXHAUSTED")
    mode["recovery"] = True

    recovered = bridge.recover(run_dir)

    # A run-owned prompt alias is a high-entropy immutable identity. One exact
    # alias match remains authoritative even if another recent chat cannot be
    # read; the unread chat cannot contain the same run-owned filename.
    assert recovered["phase"] == "COMPLETE"
    assert recovered["terminal_block_code"] is None
    assert recovered["conversation_url"] == "https://chatgpt.com/c/recovered-exact"
    assert recovered["current_target_id"] == "target-lost"
    assert recovered["target_rebind_events"] == []
    adjudication = json.loads(Path(run_dir, "history-adjudication.json").read_text(encoding="utf-8"))
    assert adjudication["outcome"] == "matched-complete"
    assert adjudication["conversation_url"] == "https://chatgpt.com/c/recovered-exact"
    assert len([item for item in adjudication["checked"] if item.get("state")]) == 1
