from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "codex.chatgpt.oracle-comprehensive/v1"
RECEIPT_SCHEMA = "codex.chatgpt.oracle-stage-result/v1"
STATE_SCHEMA = "codex.chatgpt.oracle-comprehensive-state/v1"
STAGES = {"plan", "pro", "web-multi", "review", "implementation", "final-web-gate"}
TRANSITIONS = {
    "plan": {"review", "web-multi", "pro"},
    "web-multi": {"review"},
    "pro": {"review"},
    "review": {"plan", "implementation"},
    "implementation": {"final-web-gate"},
    "final-web-gate": {"complete", "implementation"},
}
BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("oracle_comprehensive_runner", BIN / "chatgpt_oracle_run.py")
MULTI = _load("oracle_comprehensive_multi", BIN / "chatgpt_oracle_multi.py")


class WorkflowError(RuntimeError):
    pass


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError(f"JSON object required: {path}")
    return value


def _inside(root: Path, value: Any, *, exists: bool = True) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise WorkflowError("workflow paths must be absolute")
    path = path.resolve(strict=exists)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkflowError(f"path outside project: {path}") from exc
    return path


def load_manifest(path: Path) -> dict[str, Any]:
    value = _json(path.resolve(strict=True))
    if value.get("schema") != SCHEMA:
        raise WorkflowError(f"schema must be {SCHEMA}")
    root = Path(str(value.get("project_root") or "")).expanduser().resolve(strict=True)
    workflow_dir = _inside(root, value.get("workflow_dir"), exists=False)
    mission = _inside(root, value.get("initial_mission_path"))
    maximum = int(value.get("max_stages", 8))
    if not 1 <= maximum <= 12:
        raise WorkflowError("max_stages must be within 1..12")
    local_gate = value.get("local_gate_command")
    if not isinstance(local_gate, list) or not local_gate or not all(isinstance(item, str) and item for item in local_gate):
        raise WorkflowError("local_gate_command must be a nonempty string list")
    state_root = RUNNER.STATE.oracle_state_root()
    if RUNNER.STATE.is_within(root, state_root) or RUNNER.STATE.is_within(state_root, root):
        raise WorkflowError("host state must be disjoint from project")
    workflow_id = str(value.get("workflow_id") or "").strip()
    if not workflow_id or not all(character in "0123456789abcdef-" for character in workflow_id.casefold()):
        raise WorkflowError("workflow_id must be stable hex/UUID text")
    return {
        **value,
        "project_root": root,
        "workflow_dir": workflow_dir,
        "initial_mission_path": mission,
        "max_stages": maximum,
        "app_name": str(value.get("app_name") or "DevSpace"),
        "model": str(value.get("model") or "gpt-5.6"),
        "local_gate_command": list(local_gate),
        "manifest_sha256": sha(path.resolve(strict=True)),
        "workflow_id": workflow_id,
    }


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stage_mission(
    config: dict[str, Any],
    workflow_id: str,
    index: int,
    stage: str,
    source: Path,
    attempt_id: str,
) -> tuple[Path, Path, str]:
    stage_dir = config["workflow_dir"] / "stages" / f"{index:02d}-{stage}-{attempt_id[:12]}"
    receipt = stage_dir / "stage-result.json"
    target = stage_dir / "mission.md"
    stage_dir.mkdir(parents=True, exist_ok=True)
    body = source.read_text(encoding="utf-8")
    input_sha = sha(source)
    protocol = (
        "\n\n[HOST_STAGE_CONTRACT]\n"
        f"workflow_id={workflow_id}\nstage={stage}\nstage_index={index}\n"
        f"attempt_id={attempt_id}\ninput_mission_sha256={input_sha}\n"
        f"Write the small UTF-8 stage receipt to: {receipt}\n"
        "Receipt schema: codex.chatgpt.oracle-stage-result/v1. Include workflow_id, "
        "stage, attempt_id, input_mission_sha256, status, output_path, output_sha256, next_stage, next_mission_path, "
        "next_mission_sha256, ready_for_next, blocker. Write the next mission itself; "
        "the host will validate bytes and hashes but will not rewrite its meaning.\n"
    )
    target.write_text(body.rstrip() + protocol, encoding="utf-8")
    return target, receipt, input_sha


def _oracle_manifest(config: dict[str, Any], mission: Path, stage_dir: Path, run_id: str) -> Path:
    path = stage_dir / "oracle.json"
    _write(path, {
        "schema": RUNNER.STATE.SCHEMA,
        "project_root": str(config["project_root"]),
        "mission_path": str(mission),
        "app_name": config["app_name"],
        "mode": "browser",
        "model": config["model"],
        "model_strategy": "select",
        "research": "off",
        "archive": "auto",
        "parallel_parent_id": config["_parallel_parent_id"],
        "run_id": run_id,
    })
    return path


def _validate_receipt(
    config: dict[str, Any],
    receipt_path: Path,
    workflow_id: str,
    stage: str,
    attempt_id: str,
    input_sha: str,
) -> dict[str, Any]:
    value = _json(receipt_path)
    if (
        value.get("schema") != RECEIPT_SCHEMA
        or value.get("workflow_id") != workflow_id
        or value.get("stage") != stage
        or value.get("attempt_id") != attempt_id
        or value.get("input_mission_sha256") != input_sha
    ):
        raise WorkflowError("stage receipt identity mismatch")
    if value.get("status") not in {"PASS", "COMPLETE"} or value.get("ready_for_next") is not True or value.get("blocker"):
        raise WorkflowError("stage receipt did not pass")
    output = _inside(config["project_root"], value.get("output_path"))
    if not output.is_file() or not output.read_bytes().strip() or value.get("output_sha256") != sha(output):
        raise WorkflowError("stage output is missing or hash-mismatched")
    next_stage = str(value.get("next_stage") or "")
    if next_stage not in TRANSITIONS[stage]:
        raise WorkflowError(f"invalid transition {stage}->{next_stage}")
    if next_stage == "complete":
        return {**value, "_next_mission": None}
    next_mission = _inside(config["project_root"], value.get("next_mission_path"))
    if value.get("next_mission_sha256") != sha(next_mission):
        raise WorkflowError("next mission hash mismatch")
    return {**value, "_next_mission": next_mission}


def _state_path(config: dict[str, Any], workflow_id: str) -> Path:
    project_key = hashlib.sha256(str(config["project_root"]).casefold().encode("utf-8")).hexdigest()[:24]
    return RUNNER.STATE.oracle_state_root() / "workflows" / project_key / f"{workflow_id}.json"


def _run_local_gate(config: dict[str, Any], runner: Callable[..., Any]) -> dict[str, Any]:
    completed = runner(
        config["local_gate_command"],
        cwd=str(config["project_root"]),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        **RUNNER.STATE.windows_subprocess_kwargs(),
    )
    return {
        "exit_code": int(completed.returncode),
        "stdout_sha256": hashlib.sha256((completed.stdout or "").encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256((completed.stderr or "").encode("utf-8")).hexdigest(),
    }


def _recover_exact_oracle_stage(
    stored: dict[str, Any],
    *,
    oracle_recover: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Recover only the persisted Oracle run; this path never submits a prompt."""
    run_dir = Path(str(stored.get("oracle_run_dir") or "")).expanduser()
    expected_run_id = str(stored.get("oracle_run_id") or stored.get("current_attempt_id") or "")
    if not run_dir.is_absolute() or not expected_run_id:
        return {"ok": False, "error": "ORACLE_RECOVERY_IDENTITY_MISSING"}
    try:
        directory = run_dir.resolve(strict=True)
        run_state = RUNNER.STATE.load_state(directory / "state.json")
    except Exception as exc:
        return {"ok": False, "error": "ORACLE_RECOVERY_RUN_UNAVAILABLE", "detail": str(exc)}
    if str(run_state.get("run_id") or "") != expected_run_id:
        return {"ok": False, "error": "ORACLE_RECOVERY_IDENTITY_MISMATCH"}
    return oracle_recover(directory, action="harvest", dry_run=False)


def _recover_oracle_under_workflow_mutex(
    run_dir: Path,
    *,
    action: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Recover a comprehensive child through its persisted parent mutex.

    The workflow already owns the canonical project mutex, while every
    comprehensive child is launched with ``parallel_parent_id`` and therefore
    owns a distinct parent-scoped mutex.  The public recovery entry point reads
    that persisted identity and acquires the same child mutex.  A missing parent
    identity fails closed instead of falling back to the non-reentrant project
    mutex or bypassing the live child lock.
    """
    directory = run_dir.expanduser().resolve(strict=True)
    state = RUNNER.STATE.load_state(directory / "state.json")
    if not str(state.get("parallel_parent_id") or "").strip():
        return {"ok": False, "error": "ORACLE_RECOVERY_PARALLEL_PARENT_MISSING"}
    return RUNNER.recover_run(directory, action=action, dry_run=dry_run)


def _recover_exact_multi_stage(stored: dict[str, Any]) -> dict[str, Any]:
    """Read a persisted Multi result only; absent identity is never a retry signal."""
    result_path = Path(str(stored.get("multi_result_path") or "")).expanduser()
    expected_manifest_sha = str(stored.get("multi_manifest_sha256") or "")
    if not result_path.is_absolute() or not expected_manifest_sha:
        return {"ok": False, "error": "MULTI_RECOVERY_IDENTITY_MISSING"}
    try:
        result = _json(result_path.resolve(strict=True))
    except Exception as exc:
        return {"ok": False, "error": "MULTI_RESULT_UNAVAILABLE", "detail": str(exc)}
    if result.get("schema") != MULTI.RESULT_SCHEMA or not str(result.get("parent_id") or ""):
        return {"ok": False, "error": "MULTI_RESULT_IDENTITY_INVALID"}
    return {
        "ok": result.get("status") in {"complete", "partial"},
        "parent_id": str(result["parent_id"]),
        "next_stage_result_path": result.get("next_stage_result_path"),
        "status": result.get("status"),
        "result_path": str(result_path.resolve()),
        "manifest_sha256": expected_manifest_sha,
    }


def _run_workflow_locked(
    manifest_path: Path,
    *,
    dry_run: bool = False,
    oracle_execute: Callable[..., dict[str, Any]] = RUNNER.execute_run,
    oracle_recover: Callable[..., dict[str, Any]] = _recover_oracle_under_workflow_mutex,
    multi_execute: Callable[..., dict[str, Any]] = MULTI.run_multi,
    local_gate_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    config = load_manifest(manifest_path)
    workflow_id = config["workflow_id"]
    config["_parallel_parent_id"] = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    config["workflow_dir"].mkdir(parents=True, exist_ok=True)
    if dry_run:
        attempt_id = uuid.uuid4().hex
        mission, receipt_path, input_sha = _stage_mission(
            config, workflow_id, 0, "plan", config["initial_mission_path"], attempt_id
        )
        oracle_manifest = _oracle_manifest(config, mission, mission.parent, attempt_id)
        preview = oracle_execute(oracle_manifest, dry_run=True)
        return {
            "ok": bool(preview.get("ok")),
            "schema": STATE_SCHEMA,
            "status": "dry-run",
            "workflow_id": workflow_id,
            "stage": "plan",
            "attempt_id": attempt_id,
            "input_mission_sha256": input_sha,
            "receipt_path": str(receipt_path),
            "oracle_preview": preview,
        }
    state_path = _state_path(config, workflow_id)
    if state_path.is_file():
        stored = _json(state_path)
        if stored.get("manifest_sha256") != config["manifest_sha256"]:
            raise WorkflowError("workflow manifest changed after preparation")
        if stored.get("status") == "complete":
            return {"ok": True, **stored}
        if stored.get("status") == "awaiting_receipt":
            stored_receipt = Path(str(stored["receipt_path"])).resolve()
            if not stored_receipt.is_file():
                return {"ok": False, **stored}
            receipt = _validate_receipt(
                config,
                stored_receipt,
                workflow_id,
                str(stored["current_stage"]),
                str(stored["current_attempt_id"]),
                str(stored["current_input_sha256"]),
            )
            records = list(stored.get("records") or [])
            if receipt["next_stage"] == "complete":
                gate = _run_local_gate(config, local_gate_runner)
                if gate["exit_code"] != 0:
                    blocked = {**stored, "status": "attention_required", "blocker": "deterministic local gate failed", "local_gate": gate}
                    _write(state_path, blocked)
                    return {"ok": False, **blocked}
                complete = {
                    **stored, "status": "complete", "final_output_path": receipt["output_path"], "local_gate": gate
                }
                _write(state_path, complete)
                return {"ok": True, **complete}
            stage = str(receipt["next_stage"])
            source = receipt["_next_mission"]
            start_index = int(stored["next_index"]) + 1
            _write(state_path, {
                "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "next_stage": stage,
                "next_mission_path": str(source), "next_mission_sha256": receipt["next_mission_sha256"],
                "next_index": start_index, "records": records,
            })
        elif stored.get("status") == "attention_required" and stored.get("next_stage") == "pro":
            pro_receipt = Path(str(stored.get("receipt_path") or "")).resolve()
            if not pro_receipt.is_file():
                return {"ok": False, **stored}
            receipt = _validate_receipt(
                config,
                pro_receipt,
                workflow_id,
                "pro",
                str(stored["current_attempt_id"]),
                str(stored["current_input_sha256"]),
            )
            stage = str(receipt["next_stage"])
            source = receipt["_next_mission"]
            records = list(stored.get("records") or []) + [{"stage": "pro", "receipt_path": str(pro_receipt)}]
            start_index = int(stored["next_index"]) + 1
            _write(state_path, {
                "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "next_stage": stage,
                "next_mission_path": str(source), "next_mission_sha256": receipt["next_mission_sha256"],
                "next_index": start_index, "records": records,
            })
        elif stored.get("status") in {"running", "attention_required"} and stored.get("current_stage") == "web-multi":
            recovered = _recover_exact_multi_stage(stored)
            records = list(stored.get("records") or [])
            if not recovered.get("ok"):
                blocked = {
                    **stored,
                    "status": "attention_required",
                    "blocker": "web-multi exact result is not ready; no retry was submitted",
                    "recovery": recovered,
                    "records": records,
                }
                _write(state_path, blocked)
                return {"ok": False, **blocked}
            result_path = Path(str(recovered.get("next_stage_result_path") or ""))
            if not result_path.is_file():
                blocked = {
                    **stored,
                    "status": "attention_required",
                    "blocker": "web-multi result has no bound stage receipt",
                    "recovery": recovered,
                    "records": records,
                }
                _write(state_path, blocked)
                return {"ok": False, **blocked}
            attempt_id = str(recovered["parent_id"])
            receipt = _validate_receipt(config, result_path, workflow_id, "web-multi", attempt_id, sha(Path(str(stored["current_mission_path"]))))
            records.append({"stage": "web-multi", "parent_id": attempt_id, "result_path": recovered["result_path"], "recovered": True})
            prepared = {
                "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "next_stage": str(receipt["next_stage"]),
                "next_mission_path": str(receipt["_next_mission"]),
                "next_mission_sha256": receipt["next_mission_sha256"],
                "next_index": int(stored["next_index"]) + 1,
                "records": records,
            }
            _write(state_path, prepared)
            return _run_workflow_locked(
                manifest_path, oracle_execute=oracle_execute, oracle_recover=oracle_recover,
                multi_execute=multi_execute, local_gate_runner=local_gate_runner,
            )
        elif stored.get("status") in {"running", "attention_required"} and stored.get("current_stage"):
            recovered = _recover_exact_oracle_stage(stored, oracle_recover=oracle_recover)
            records = list(stored.get("records") or [])
            records.append({
                "stage": stored["current_stage"], "run_id": stored.get("oracle_run_id") or stored.get("current_attempt_id"),
                "run_dir": stored.get("oracle_run_dir"), "recovered": True, "recovery_status": recovered.get("status"),
            })
            if not recovered.get("ok"):
                blocked = {
                    **stored,
                    "status": "attention_required",
                    "blocker": "exact Oracle recovery did not produce a terminal output; no retry was submitted",
                    "recovery": recovered,
                    "records": records,
                }
                _write(state_path, blocked)
                return {"ok": False, **blocked}
            awaiting = {
                **stored, "status": "awaiting_receipt", "records": records,
                "recovery": {"status": "recovered", "run_id": stored.get("oracle_run_id") or stored.get("current_attempt_id"), "run_dir": stored.get("oracle_run_dir")},
            }
            _write(state_path, awaiting)
            return _run_workflow_locked(
                manifest_path, oracle_execute=oracle_execute, oracle_recover=oracle_recover,
                multi_execute=multi_execute, local_gate_runner=local_gate_runner,
            )
        elif stored.get("status") in {"running", "attention_required"}:
            return {"ok": False, **stored}
        else:
            stage = str(stored["next_stage"])
            source = Path(str(stored["next_mission_path"])).resolve(strict=True)
            if str(stored.get("next_mission_sha256") or "") != sha(source):
                raise WorkflowError("prepared next mission changed after receipt verification")
            records = list(stored.get("records") or [])
            start_index = int(stored.get("next_index") or 0)
    else:
        stage, source, records, start_index = "plan", config["initial_mission_path"], [], 0
        _write(state_path, {
            "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
            "manifest_sha256": config["manifest_sha256"], "next_stage": stage,
            "next_mission_path": str(source), "next_mission_sha256": sha(source),
            "next_index": 0, "records": records,
        })
    for index in range(start_index, config["max_stages"]):
        if stage == "pro":
            attempt_id = uuid.uuid4().hex
            input_sha = sha(source)
            pro_dir = config["workflow_dir"] / "pro" / attempt_id[:12]
            receipt_path = pro_dir / "stage-result.json"
            handoff_path = pro_dir / "handoff.json"
            _write(handoff_path, {
                "schema": "codex.chatgpt.oracle-pro-handoff/v1",
                "workflow_id": workflow_id,
                "stage": "pro",
                "attempt_id": attempt_id,
                "input_mission_path": str(source),
                "input_mission_sha256": input_sha,
                "receipt_path": str(receipt_path),
                "transport": "existing-attachment-only-pro",
            })
            result = {"schema": STATE_SCHEMA, "status": "attention_required", "workflow_id": workflow_id,
                      "manifest_sha256": config["manifest_sha256"],
                      "next_action": "run existing attachment-only Pro handoff, then provide a bound Pro receipt",
                      "next_stage": "pro", "next_mission_path": str(source), "next_index": index,
                      "current_attempt_id": attempt_id, "current_input_sha256": input_sha,
                      "receipt_path": str(receipt_path), "pro_handoff_path": str(handoff_path), "records": records}
            _write(state_path, result)
            return {"ok": False, **result}
        if stage == "web-multi":
            # This complete preflight is intentionally before the send-boundary
            # state write.  An invalid Multi manifest is a retryable pre-submit
            # error, not an active/uncertain provider workflow.
            multi_config = MULTI.load_manifest(source)
            multi_source = _json(source)
            binding = multi_source.get("next_stage_binding") if isinstance(multi_source.get("next_stage_binding"), dict) else {}
            if binding.get("workflow_id") != workflow_id or binding.get("stage") != "web-multi":
                raise WorkflowError("web-multi manifest is not bound to this workflow")
            multi_result_path = multi_config["output_dir"] / "result.json"
            multi_receipt_path = multi_config.get("next_stage_result_path")
            multi_execution_id = hashlib.sha256(
                f"{workflow_id}:{index}:{sha(source)}".encode("utf-8")
            ).hexdigest()
            _write(state_path, {
                "schema": STATE_SCHEMA, "status": "running", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "current_stage": stage,
                "current_mission_path": str(source), "next_index": index, "records": records,
                "multi_execution_id": multi_execution_id, "multi_manifest_sha256": sha(source),
                "multi_result_path": str(multi_result_path),
                "multi_receipt_path": str(multi_receipt_path) if multi_receipt_path else None,
            })
            multi_result = multi_execute(source, dry_run=False, parent_lock_held=True)
            records.append({"stage": stage, "result": multi_result})
            if not multi_result.get("ok"):
                break
            result_path = Path(str(multi_result.get("next_stage_result_path") or ""))
            if not result_path.is_file():
                return {"ok": False, "status": "attention_required", "workflow_id": workflow_id,
                        "error": "web-multi merger did not provide next_stage_result_path", "records": records}
            attempt_id = str(multi_result.get("parent_id") or "")
            receipt = _validate_receipt(config, result_path, workflow_id, "web-multi", attempt_id, sha(source))
            stage, source = str(receipt["next_stage"]), receipt["_next_mission"]
            _write(state_path, {
                "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "next_stage": stage,
                "next_mission_path": str(source), "next_mission_sha256": receipt["next_mission_sha256"],
                "next_index": index + 1, "records": records,
            })
            continue
        attempt_id = uuid.uuid4().hex
        mission, receipt_path, input_sha = _stage_mission(config, workflow_id, index, stage, source, attempt_id)
        stage_dir = mission.parent
        oracle_manifest = _oracle_manifest(config, mission, stage_dir, attempt_id)
        oracle_config = RUNNER.STATE.load_manifest(oracle_manifest)
        oracle_layout = RUNNER.STATE.create_layout(oracle_config, run_id=attempt_id)
        _write(state_path, {
            "schema": STATE_SCHEMA, "status": "running", "workflow_id": workflow_id,
            "manifest_sha256": config["manifest_sha256"], "current_stage": stage,
            "current_attempt_id": attempt_id, "current_input_sha256": input_sha,
            "current_mission_path": str(source), "receipt_path": str(receipt_path),
            "oracle_run_id": attempt_id, "oracle_run_dir": str(oracle_layout.run_dir), "oracle_manifest_path": str(oracle_manifest),
            "next_index": index, "records": records,
        })
        run = oracle_execute(oracle_manifest, dry_run=False)
        records.append({"stage": stage, "run_dir": run.get("run_dir"), "ok": bool(run.get("ok"))})
        if run.get("ok"):
            _write(state_path, {
                "schema": STATE_SCHEMA, "status": "awaiting_receipt", "workflow_id": workflow_id,
                "manifest_sha256": config["manifest_sha256"], "current_stage": stage,
                "current_attempt_id": attempt_id, "current_input_sha256": input_sha,
                "current_mission_path": str(source), "receipt_path": str(receipt_path),
                "oracle_run_dir": run.get("run_dir"), "next_index": index, "records": records,
            })
        if run.get("ok") and not receipt_path.is_file():
            return {"ok": False, **_json(state_path)}
        if not run.get("ok"):
            retained = {
                **_json(state_path), "status": "attention_required", "records": records,
                "blocker": "Oracle stage needs exact recovery; no replacement was submitted",
            }
            _write(state_path, retained)
            return {"ok": False, **retained}
        receipt = _validate_receipt(config, receipt_path, workflow_id, stage, attempt_id, input_sha)
        if receipt["next_stage"] == "complete":
            gate = _run_local_gate(config, local_gate_runner)
            if gate["exit_code"] != 0:
                result = {"schema": STATE_SCHEMA, "status": "attention_required", "workflow_id": workflow_id,
                          "manifest_sha256": config["manifest_sha256"], "records": records,
                          "blocker": "deterministic local gate failed", "local_gate": gate}
                _write(state_path, result)
                return {"ok": False, **result}
            result = {"schema": STATE_SCHEMA, "status": "complete", "workflow_id": workflow_id,
                      "manifest_sha256": config["manifest_sha256"], "records": records,
                      "final_output_path": receipt["output_path"], "local_gate": gate}
            _write(state_path, result)
            return {"ok": True, **result}
        stage, source = str(receipt["next_stage"]), receipt["_next_mission"]
        _write(state_path, {
            "schema": STATE_SCHEMA, "status": "prepared", "workflow_id": workflow_id,
            "manifest_sha256": config["manifest_sha256"], "next_stage": stage,
            "next_mission_path": str(source), "next_mission_sha256": receipt["next_mission_sha256"],
            "next_index": index + 1, "records": records,
        })
    result = {"schema": STATE_SCHEMA, "status": "attention_required", "workflow_id": workflow_id,
              "records": records, "next_stage": stage, "blocker": "stage failed or maximum stage count reached"}
    _write(state_path, {**result, "manifest_sha256": config["manifest_sha256"]})
    return {"ok": False, **result}


def run_workflow(
    manifest_path: Path,
    *,
    dry_run: bool = False,
    oracle_execute: Callable[..., dict[str, Any]] = RUNNER.execute_run,
    oracle_recover: Callable[..., dict[str, Any]] = _recover_oracle_under_workflow_mutex,
    multi_execute: Callable[..., dict[str, Any]] = MULTI.run_multi,
    local_gate_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    config = load_manifest(manifest_path)
    if dry_run:
        return _run_workflow_locked(
            manifest_path,
            dry_run=True,
            oracle_execute=oracle_execute,
            oracle_recover=oracle_recover,
            multi_execute=multi_execute,
            local_gate_runner=local_gate_runner,
        )
    with RUNNER.STATE.project_submit_mutex(config["project_root"], timeout_seconds=30):
        return _run_workflow_locked(
            manifest_path,
            dry_run=False,
            oracle_execute=oracle_execute,
            oracle_recover=oracle_recover,
            multi_execute=multi_execute,
            local_gate_runner=local_gate_runner,
        )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a bounded Oracle comprehensive workflow.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        value = run_workflow(args.manifest, dry_run=args.dry_run)
    except Exception as exc:
        value = {"ok": False, "error": {"code": "ORACLE_COMPREHENSIVE_FAILED", "message": str(exc)}}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
