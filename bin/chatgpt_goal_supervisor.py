from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from chatgpt_goal_contract import (
    GOAL_CYCLE_SCHEMA,
    GOAL_HOST_GATES_SCHEMA,
    GOAL_MANIFEST_SCHEMA,
    GOAL_STATE_SCHEMA,
    GOAL_USER_ACTION_SCHEMA,
    GoalContractError,
    append_event,
    artifact_ref,
    atomic_write_json,
    canonical_sha256,
    file_sha256,
    load_json,
    progress_fingerprint,
    project_key,
    strict_utf8_bytes,
    validate_artifact_ref,
    validate_check_ids,
    validate_check_registry,
    validate_goal_cycle_result,
    validate_goal_manifest,
    validate_goal_state,
    validate_transition,
    write_immutable_bytes,
    write_immutable_json,
)

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
DEFAULT_STATE_ROOT = CODEX_HOME / "state" / "chatgpt-goals" / "projects"
REPO_ROOT = Path(__file__).resolve().parents[1]
REPAIR_RESULT_SCHEMA_PATHS = (
    REPO_ROOT / "skills" / "chatgpt-pro-plan-handoff" / "schemas" / "goal-repair-result-v1.schema.json",
    CODEX_HOME / "skills" / "chatgpt-pro-plan-handoff" / "schemas" / "goal-repair-result-v1.schema.json",
)
ADAPTER_PATHS = (
    REPO_ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "goal_cycle_adapter.py",
    CODEX_HOME / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "goal_cycle_adapter.py",
)
AUTOMATION_FAULT_CODES = {
    "RUNTIME_IMPORT_FAILED",
    "RUNTIME_RUN_NOT_FOUND",
    "STAGE_RUNTIME_CAPTURE_INVALID",
    "RUNTIME_DID_NOT_RETURN_RUN_ID",
    "RUNTIME_ARTIFACT_MISSING",
    "CANONICAL_CONVERSATION_URL_MISSING",
    "STAGE_TAB_CLEANUP_PENDING",
    "WEB_MULTI_RUNTIME_IMPORT_FAILED",
}
USER_BOUNDARY_CODES = {
    "SUBMISSION_UNCERTAIN_IDENTITY_MISSING",
    "MULTIPLE_RUNTIME_RUNS_FOR_STAGE",
    "WORKFLOW_ALREADY_ACTIVE",
    "REVIEW_BLOCKED",
    "PLAN_REVIEW_DID_NOT_PASS",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class GoalSupervisorError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


def _load_adapter() -> Callable[[Path, Path], dict[str, Any]]:
    selected = next((path for path in ADAPTER_PATHS if path.is_file()), None)
    if selected is None:
        raise GoalSupervisorError("GOAL_CYCLE_ADAPTER_MISSING")
    script_dir = str(selected.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("chatgpt_goal_cycle_adapter_runtime", selected)
    if spec is None or spec.loader is None:
        raise GoalSupervisorError("GOAL_CYCLE_ADAPTER_IMPORT_FAILED")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.run_cycle


class GoalSupervisor:
    def __init__(
        self,
        manifest_path: Path,
        *,
        state_root: Path | None = None,
        cycle_runner: Callable[[Path, Path], Mapping[str, Any]] | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        repair_runner: Callable[[Path, Path, Path], Mapping[str, Any]] | None = None,
    ):
        self.source_manifest_path = manifest_path.expanduser().resolve(strict=True)
        self.manifest = validate_goal_manifest(load_json(self.source_manifest_path), manifest_path=self.source_manifest_path)
        self.project_root = Path(str(self.manifest["project"]["root"])).resolve(strict=True)
        output_override = self.manifest.get("output_dir")
        if output_override:
            self.goal_dir = Path(str(output_override)).expanduser().resolve() / str(self.manifest["goal_id"])
        else:
            root = (state_root or DEFAULT_STATE_ROOT).expanduser().resolve()
            self.goal_dir = root / project_key(self.project_root) / "goals" / str(self.manifest["goal_id"])
        self.manifest_path = self.goal_dir / "goal-manifest.json"
        self.state_path = self.goal_dir / "goal-state.json"
        self.events_dir = self.goal_dir / "events"
        self.cycles_dir = self.goal_dir / "cycles"
        self.boundaries_dir = self.goal_dir / "boundaries"
        self.final_path = self.goal_dir / "final.json"
        self.lock_path = self.goal_dir / "supervisor.lock"
        self.cycle_runner = cycle_runner or _load_adapter()
        self.command_runner = command_runner or subprocess.run
        self.repair_runner = repair_runner or self._run_codex_repair

    def _manifest_hash(self) -> str:
        return file_sha256(self.manifest_path if self.manifest_path.is_file() else self.source_manifest_path)

    def _load_state(self) -> dict[str, Any]:
        state = validate_goal_state(load_json(self.state_path))
        if state["goal_id"] != self.manifest["goal_id"] or state["manifest_sha256"] != self._manifest_hash():
            raise GoalSupervisorError("GOAL_STATE_IDENTITY_CONFLICT")
        return state

    def _write_state(self, old: Mapping[str, Any], new_phase: str, **changes: Any) -> dict[str, Any]:
        validate_transition(str(old["phase"]), new_phase)
        state = {**dict(old), **changes}
        state["phase"] = new_phase
        state["revision"] = int(old["revision"]) + 1
        state["updated_at"] = utc_now()
        validate_goal_state(state)
        atomic_write_json(self.state_path, state)
        append_event(self.events_dir, {
            "goal_id": state["goal_id"],
            "revision": state["revision"],
            "phase": new_phase,
            "cycle_index": state["cycle_index"],
            "at": state["updated_at"],
        })
        return state

    def _mutate_state(self, old: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
        state = {**dict(old), **changes}
        state["revision"] = int(old["revision"]) + 1
        state["updated_at"] = utc_now()
        validate_goal_state(state)
        atomic_write_json(self.state_path, state)
        return state

    def _acquire_lock(self) -> int:
        self.goal_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise GoalSupervisorError("GOAL_SUPERVISOR_ALREADY_ACTIVE", str(self.lock_path)) from exc
        os.write(fd, json.dumps({"pid": os.getpid(), "created_at": utc_now()}).encode("utf-8"))
        return fd

    def _release_lock(self, fd: int) -> None:
        try:
            os.close(fd)
        finally:
            try:
                self.lock_path.unlink()
            except OSError:
                pass

    def prepare(self) -> dict[str, Any]:
        if self.state_path.is_file():
            return self._load_state()
        self.goal_dir.mkdir(parents=True, exist_ok=True)
        write_immutable_json(self.manifest_path, self.manifest)
        original_path = self.goal_dir / "original-goal.txt"
        write_immutable_bytes(original_path, strict_utf8_bytes(str(self.manifest["goal"]), field="goal"))
        now = utc_now()
        policy = dict(self.manifest["policy"])
        state = {
            "schema": GOAL_STATE_SCHEMA,
            "goal_id": self.manifest["goal_id"],
            "manifest_sha256": file_sha256(self.manifest_path),
            "revision": 1,
            "phase": "CREATED",
            "cycle_index": 0,
            "max_cycles": int(policy.get("max_cycles", 20)),
            "active_action": None,
            "active_workflow": None,
            "last_cycle_result": None,
            "last_host_gates": None,
            "repair_families": {},
            "progress_fingerprints": [],
            "boundary": None,
            "final": None,
            "created_at": now,
            "updated_at": now,
        }
        validate_goal_state(state)
        atomic_write_json(self.state_path, state)
        append_event(self.events_dir, {
            "goal_id": state["goal_id"], "revision": 1, "phase": "CREATED", "cycle_index": 0, "at": now,
        })
        return self._open_cycle(state, str(self.manifest["goal"]), source="original-goal")

    def _open_cycle(self, state: Mapping[str, Any], mission: str, *, source: str) -> dict[str, Any]:
        cycle_index = int(state["cycle_index"]) + 1
        if cycle_index > int(state["max_cycles"]):
            return self._stop_for_user(state, "CYCLE_BUDGET_EXHAUSTED", "The configured goal-cycle budget is exhausted.", ["Review the preserved cycle evidence and explicitly resume with a revised goal policy."])
        cycle_dir = self.cycles_dir / f"{cycle_index:04d}"
        mission_path = cycle_dir / "mission.txt"
        write_immutable_bytes(mission_path, strict_utf8_bytes(mission, field="mission"))
        original_path = self.goal_dir / "original-goal.txt"
        allowed_checks = list(self.manifest["acceptance"]["required_check_ids"])
        registry = self._registry()
        if registry:
            allowed_checks = list(registry["checks"])
        cycle_manifest = {
            "schema": GOAL_CYCLE_SCHEMA,
            "goal_id": self.manifest["goal_id"],
            "cycle_index": cycle_index,
            "cycle_nonce": secrets.token_hex(16),
            "source": source,
            "original_goal": artifact_ref(original_path),
            "mission": artifact_ref(mission_path),
            "allowed_host_check_ids": allowed_checks,
            "prior_cycle_result": state.get("last_cycle_result"),
            "prior_host_gates": state.get("last_host_gates"),
            "created_at": utc_now(),
        }
        cycle_manifest_path = cycle_dir / "cycle-manifest.json"
        write_immutable_json(cycle_manifest_path, cycle_manifest)
        next_state = self._write_state(
            state,
            "CYCLE_READY",
            cycle_index=cycle_index,
            active_action=None,
            active_workflow=None,
            boundary=None,
        )
        append_event(self.events_dir, {
            "goal_id": next_state["goal_id"], "revision": next_state["revision"], "phase": "CYCLE_READY",
            "cycle_index": cycle_index, "action": "cycle-opened", "cycle_manifest": artifact_ref(cycle_manifest_path), "at": utc_now(),
        })
        return next_state

    def _registry(self) -> dict[str, Any] | None:
        ref = self.manifest.get("check_registry")
        if ref is None:
            return None
        path = validate_artifact_ref(ref)
        return validate_check_registry(load_json(path), project_root=self.project_root)

    def _cycle_manifest_path(self, cycle_index: int) -> Path:
        return self.cycles_dir / f"{cycle_index:04d}" / "cycle-manifest.json"

    def run(self) -> dict[str, Any]:
        fd = self._acquire_lock()
        try:
            state = self.prepare()
            while state["phase"] not in {"GOAL_COMPLETE", "WAITING_USER", "FAILED_CLOSED"}:
                if state["phase"] == "CYCLE_READY":
                    state = self._run_current_cycle(state)
                elif state["phase"] == "WEB_ACTIVE":
                    state = self._resume_web_active(state)
                elif state["phase"] == "HOST_VERIFYING":
                    state = self._verify_current_cycle(state)
                elif state["phase"] == "REPAIR_ACTIVE":
                    state = self._run_repair_transaction(state)
                else:
                    raise GoalSupervisorError("GOAL_PHASE_UNHANDLED", str(state["phase"]))
            return state
        finally:
            self._release_lock(fd)

    def resume(self) -> dict[str, Any]:
        state = self.prepare()
        if state["phase"] != "WAITING_USER":
            return self.run()
        boundary = state.get("boundary")
        if not isinstance(boundary, Mapping):
            raise GoalSupervisorError("BOUNDARY_REFERENCE_MISSING")
        validate_artifact_ref(boundary, parent=self.goal_dir)
        resume_phase = load_json(Path(str(boundary["path"]))).get("resume_phase")
        if resume_phase not in {"CYCLE_READY", "WEB_ACTIVE", "HOST_VERIFYING"}:
            raise GoalSupervisorError("BOUNDARY_RESUME_PHASE_INVALID")
        state = self._write_state(state, str(resume_phase), boundary=None)
        return self.run()

    def _run_current_cycle(self, state: Mapping[str, Any]) -> dict[str, Any]:
        cycle_path = self._cycle_manifest_path(int(state["cycle_index"]))
        intent = {
            "schema": "codex.chatgpt.goal-action-intent/v1",
            "goal_id": state["goal_id"],
            "cycle_index": state["cycle_index"],
            "action": "run-inner-v4-cycle",
            "cycle_manifest": artifact_ref(cycle_path),
            "created_at": utc_now(),
        }
        intent_path = cycle_path.parent / "run-intent.json"
        write_immutable_json(intent_path, intent)
        state = self._write_state(state, "WEB_ACTIVE", active_action=artifact_ref(intent_path))
        return self._resume_web_active(state)

    def _resume_web_active(self, state: Mapping[str, Any]) -> dict[str, Any]:
        cycle_path = self._cycle_manifest_path(int(state["cycle_index"]))
        result_path = cycle_path.parent / "cycle-result.json"
        try:
            if not result_path.is_file():
                outcome = dict(self.cycle_runner(cycle_path, self.manifest_path))
                result = outcome.get("result")
                if not isinstance(result, Mapping):
                    raise GoalSupervisorError("GOAL_CYCLE_RUNNER_RESULT_INVALID")
                write_immutable_json(result_path, result)
            result_ref = artifact_ref(result_path)
            next_state = self._write_state(
                state,
                "HOST_VERIFYING",
                last_cycle_result=result_ref,
                active_action=None,
            )
            return self._verify_current_cycle(next_state)
        except Exception as exc:
            return self._handle_cycle_failure(state, exc)

    def _expected_cycle_binding(self, cycle_manifest: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "workflow_id": f"{cycle_manifest['goal_id']}-cycle-{int(cycle_manifest['cycle_index']):04d}",
            "goal_id": cycle_manifest["goal_id"],
            "cycle_index": cycle_manifest["cycle_index"],
            "stage": "gpt-orchestrator",
            "attempt_index": 1,
            "nonce": result.get("nonce"),
            "question_sha256": result.get("question_sha256"),
            "source_snapshot_sha256": result.get("source_snapshot_sha256"),
            "original_goal_sha256": cycle_manifest["original_goal"]["sha256"],
            "mission_sha256": cycle_manifest["mission"]["sha256"],
            "input_plan_sha256": result.get("input_plan_sha256"),
            "input_research_descriptor_sha256": result.get("input_research_descriptor_sha256"),
            "input_advisory_descriptor_sha256": result.get("input_advisory_descriptor_sha256"),
            "input_review_sha256": result.get("input_review_sha256"),
            "criteria": list(self.manifest["acceptance"]["criteria"]),
        }

    def _verify_current_cycle(self, state: Mapping[str, Any]) -> dict[str, Any]:
        result_ref = state.get("last_cycle_result")
        if not isinstance(result_ref, Mapping):
            raise GoalSupervisorError("CYCLE_RESULT_REFERENCE_MISSING")
        result_path = validate_artifact_ref(result_ref, parent=self.goal_dir)
        result = load_json(result_path)
        cycle_manifest = load_json(self._cycle_manifest_path(int(state["cycle_index"])))
        expected = self._expected_cycle_binding(cycle_manifest, result)
        validated = validate_goal_cycle_result(result, expected)
        if validated["decision"] == "USER_ACTION_REQUIRED":
            action = dict(validated["user_action"])
            return self._stop_for_user(
                state,
                action["code"],
                action["message"],
                action["resume_conditions"],
                resume_phase="HOST_VERIFYING",
            )
        host_gates = self._run_host_gates(validated, cycle_manifest)
        gates_path = result_path.parent / "host-gates.json"
        write_immutable_json(gates_path, host_gates)
        gates_ref = artifact_ref(gates_path)
        state = self._mutate_state(state, last_host_gates=gates_ref)
        all_passed = bool(host_gates["accepted"])
        if validated["decision"] == "GOAL_COMPLETE" and all_passed:
            final = {
                "schema": "codex.chatgpt.goal-final/v1",
                "goal_id": state["goal_id"],
                "cycle_index": state["cycle_index"],
                "cycle_result": result_ref,
                "host_gates": gates_ref,
                "target_commit_authorized": bool(self.manifest["policy"].get("target_commit", False)),
                "target_push_authorized": bool(self.manifest["policy"].get("target_push", False)),
                "target_commit_performed": False,
                "target_push_performed": False,
                "completed_at": utc_now(),
            }
            write_immutable_json(self.final_path, final)
            return self._write_state(state, "GOAL_COMPLETE", final=artifact_ref(self.final_path))
        mission = validated["next_mission_body"] if validated["decision"] == "CONTINUE" else validated["next_mission_on_gate_failure"]
        mission_bytes = strict_utf8_bytes(str(mission), field="next-cycle-mission")
        fingerprint = progress_fingerprint(validated, host_gates, canonical_sha256({"mission": mission_bytes.decode("utf-8")}))
        fingerprints = [*state["progress_fingerprints"], fingerprint][-10:]
        stagnation_limit = int(self.manifest["policy"].get("stagnation_limit", 3))
        state = self._mutate_state(state, progress_fingerprints=fingerprints)
        if len(fingerprints) >= stagnation_limit and len(set(fingerprints[-stagnation_limit:])) == 1:
            return self._stop_for_user(
                state,
                "NO_PROGRESS",
                "The same deterministic progress fingerprint repeated without material progress.",
                ["Review the preserved cycle evidence and provide an explicit new direction before resuming."],
                resume_phase="HOST_VERIFYING",
            )
        if int(state["cycle_index"]) >= int(state["max_cycles"]):
            return self._stop_for_user(
                state,
                "CYCLE_BUDGET_EXHAUSTED",
                "The maximum number of goal cycles completed without accepted completion.",
                ["Review the preserved cycle evidence and explicitly revise the goal or policy."],
                resume_phase="HOST_VERIFYING",
            )
        return self._open_cycle(state, mission_bytes.decode("utf-8"), source="web-cycle-result")

    def _run_host_gates(self, cycle_result: Mapping[str, Any], cycle_manifest: Mapping[str, Any]) -> dict[str, Any]:
        required = list(self.manifest["acceptance"]["required_check_ids"])
        requested = validate_check_ids(cycle_result["requested_host_check_ids"], cycle_manifest["allowed_host_check_ids"])
        check_ids = list(dict.fromkeys([*required, *requested]))
        registry = self._registry()
        checks: list[dict[str, Any]] = []
        checks_dir = self.cycles_dir / f"{int(cycle_manifest['cycle_index']):04d}" / "checks"
        for check_id in check_ids:
            if registry is None or check_id not in registry["checks"]:
                raise GoalSupervisorError("HOST_CHECK_DEFINITION_MISSING", check_id)
            definition = registry["checks"][check_id]
            started = time.monotonic()
            try:
                completed = self.command_runner(
                    list(definition["argv"]),
                    cwd=definition["cwd"],
                    text=True,
                    capture_output=True,
                    timeout=definition["timeout_seconds"],
                    shell=False,
                    check=False,
                    **self._windows_hidden_kwargs(),
                )
                returncode = int(completed.returncode)
                stdout = (completed.stdout or "")[:65536]
                stderr = (completed.stderr or "")[:65536]
                timed_out = False
            except subprocess.TimeoutExpired as exc:
                returncode = None
                stdout = str(exc.stdout or "")[:65536]
                stderr = str(exc.stderr or "")[:65536]
                timed_out = True
            output_path = checks_dir / f"{check_id}.output.json"
            output = {"stdout": stdout, "stderr": stderr, "truncated": len(stdout) >= 65536 or len(stderr) >= 65536}
            write_immutable_json(output_path, output)
            passed = not timed_out and returncode in definition["expected_exit_codes"]
            checks.append({
                "check_id": check_id,
                "passed": passed,
                "returncode": returncode,
                "timed_out": timed_out,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "output": artifact_ref(output_path),
            })
        criteria = {item["criterion"]: item["status"] for item in cycle_result["criterion_claims"]}
        semantic_criteria_passed = all(criteria.get(item) == "satisfied" for item in self.manifest["acceptance"]["criteria"])
        return {
            "schema": GOAL_HOST_GATES_SCHEMA,
            "goal_id": self.manifest["goal_id"],
            "cycle_index": cycle_manifest["cycle_index"],
            "decision": cycle_result["decision"],
            "contract_valid": True,
            "semantic_criteria_passed": semantic_criteria_passed,
            "implementation_complete": cycle_result["implementation_status"] == "complete",
            "blockers_absent": not cycle_result["blockers"],
            "checks": checks,
            "accepted": (
                cycle_result["decision"] == "GOAL_COMPLETE"
                and semantic_criteria_passed
                and cycle_result["implementation_status"] == "complete"
                and not cycle_result["blockers"]
                and all(item["passed"] for item in checks)
            ),
            "verified_at": utc_now(),
        }

    def _handle_cycle_failure(self, state: Mapping[str, Any], exc: Exception) -> dict[str, Any]:
        code = str(getattr(exc, "code", type(exc).__name__))
        if code in USER_BOUNDARY_CODES or "UNCERTAIN" in code or "AUTH" in code or "PERMISSION" in code:
            return self._stop_for_user(
                state,
                code,
                "The current exact web run reached a boundary that cannot be safely retried automatically.",
                ["Resolve the recorded boundary without replacing the exact run, then explicitly resume."],
                resume_phase="WEB_ACTIVE",
            )
        if code not in AUTOMATION_FAULT_CODES:
            return self._stop_for_user(
                state,
                "UNCLASSIFIED_FAILURE",
                f"The failure was not proven to be an eligible automation fault: {code}",
                ["Inspect the preserved exception and exact-run evidence before resuming."],
                resume_phase="WEB_ACTIVE",
            )
        fingerprint = canonical_sha256({"family": code, "detail": str(exc)[:1000], "phase": state["phase"]})
        families = {key: dict(value) for key, value in state["repair_families"].items()}
        family = families.setdefault(fingerprint, {"code": code, "occurrences": 0, "attempts": 0})
        family["occurrences"] += 1
        state = self._mutate_state(state, repair_families=families)
        if family["occurrences"] < 2:
            return self._stop_for_user(
                state,
                "AUTOMATION_FAULT_FIRST_OCCURRENCE",
                f"A deterministic automation fault occurred once: {code}. Existing exact-run recovery remains authoritative.",
                ["Resume to retry only the same persisted cycle and run identity."],
                resume_phase="WEB_ACTIVE",
            )
        enabled = bool(self.manifest["policy"].get("automatic_repair", False)) and os.environ.get("CODEX_CHATGPT_AUTOMATIC_REPAIR") == "1"
        budget = int(self.manifest["policy"].get("repair_attempts_per_family", 2))
        if not enabled or family["attempts"] >= budget:
            return self._stop_for_user(
                state,
                "AUTOMATION_REPAIR_DISABLED_OR_EXHAUSTED",
                f"The confirmed automation fault recurred but automatic repair is disabled or exhausted: {code}",
                ["Review the incident packet and explicitly authorize a bounded repository repair."],
                resume_phase="WEB_ACTIVE",
            )
        family["attempts"] += 1
        families[fingerprint] = family
        incident_dir = self.goal_dir / "incidents" / fingerprint / f"attempt-{family['attempts']}"
        packet = {
            "schema": "codex.chatgpt.goal-repair-message/v1",
            "goal_id": state["goal_id"],
            "cycle_index": state["cycle_index"],
            "incident_family": code,
            "fingerprint": fingerprint,
            "attempt": family["attempts"],
            "resume_phase": "WEB_ACTIVE",
            "automation_repo_root": self.manifest["project"].get("automation_repo_root"),
            "allowed_patch_globs": ["bin/*.py", "skills/chatgpt-pro-plan-handoff/**", "tests/**"],
            "diagnostic": str(exc)[:4000],
            "created_at": utc_now(),
        }
        packet_path = incident_dir / "incident.json"
        write_immutable_json(packet_path, packet)
        return self._write_state(
            state,
            "REPAIR_ACTIVE",
            repair_families=families,
            active_action=artifact_ref(packet_path),
        )

    @staticmethod
    def _windows_hidden_kwargs() -> dict[str, Any]:
        if os.name != "nt":
            return {}
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startupinfo}

    def _run_codex_repair(self, packet_path: Path, repo_root: Path, result_path: Path) -> Mapping[str, Any]:
        schema_path = next((path for path in REPAIR_RESULT_SCHEMA_PATHS if path.is_file()), None)
        if schema_path is None:
            raise GoalSupervisorError("AUTOMATIC_REPAIR_SCHEMA_MISSING")
        prompt_path = packet_path.parent / "repair-prompt.txt"
        prompt = (
            "Repair the GPT automation fault described by the immutable incident JSON at:\n"
            f"{packet_path}\n\n"
            f"Work only in the automation repository {repo_root}. Do not modify the target project, "
            "do not submit or stop any ChatGPT run, and preserve the exact existing run identity. "
            "Make the smallest authoritative-source fix, add focused regression coverage, run it, "
            "synchronize the installed deployment, commit and push public-safe changes, and verify CI. "
            "Return only the required structured result. If any condition cannot be proved, return BLOCKED."
        )
        write_immutable_bytes(prompt_path, strict_utf8_bytes(prompt, field="repair-prompt", max_bytes=32000))
        argv = [
            "codex", "exec", "--ephemeral", "--color", "never",
            "--output-schema", str(schema_path), "-o", str(result_path),
            "-C", str(repo_root), "-s", "danger-full-access",
            "-c", 'approval_policy="never"', "-",
        ]
        completed = self.command_runner(
            argv,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=3600,
            shell=False,
            check=False,
            **self._windows_hidden_kwargs(),
        )
        log_path = packet_path.parent / "repair-command.json"
        write_immutable_json(log_path, {
            "argv": argv[:-1] + ["<stdin>"],
            "returncode": int(completed.returncode),
            "stdout": (completed.stdout or "")[-65536:],
            "stderr": (completed.stderr or "")[-65536:],
            "completed_at": utc_now(),
        })
        if completed.returncode != 0 or not result_path.is_file():
            raise GoalSupervisorError("AUTOMATIC_REPAIR_COMMAND_FAILED", str(completed.returncode))
        return load_json(result_path)

    def _run_repair_transaction(self, state: Mapping[str, Any]) -> dict[str, Any]:
        action = state.get("active_action")
        if not isinstance(action, Mapping):
            return self._stop_for_user(
                state, "AUTOMATIC_REPAIR_INCIDENT_MISSING",
                "The automatic-repair incident reference is missing.",
                ["Inspect the durable goal state before resuming."],
                resume_phase="WEB_ACTIVE",
            )
        packet_path = validate_artifact_ref(action, parent=self.goal_dir)
        packet = load_json(packet_path)
        repo_text = packet.get("automation_repo_root")
        if not isinstance(repo_text, str) or not repo_text.strip():
            return self._stop_for_user(
                state, "AUTOMATION_REPO_ROOT_MISSING",
                "Automatic repair requires an explicit automation repository root.",
                ["Set project.automation_repo_root to the authoritative repository and explicitly resume."],
                resume_phase="WEB_ACTIVE",
            )
        repo_root = Path(repo_text).expanduser().resolve(strict=True)
        result_path = packet_path.parent / "repair-result.json"
        try:
            result = dict(self.repair_runner(packet_path, repo_root, result_path))
            required = {
                "schema", "status", "summary", "changed_files", "focused_tests",
                "exact_run_preserved", "new_submission_created", "installation_synced",
                "source_committed", "source_pushed", "ci_passed",
            }
            if set(result) != required or result.get("schema") != "codex.chatgpt.goal-repair-result/v1":
                raise GoalSupervisorError("AUTOMATIC_REPAIR_RESULT_INVALID")
            for field in ("changed_files", "focused_tests"):
                if not isinstance(result.get(field), list) or any(not isinstance(item, str) for item in result[field]):
                    raise GoalSupervisorError("AUTOMATIC_REPAIR_RESULT_INVALID", field)
            accepted = (
                result.get("status") == "COMPLETE"
                and bool(result.get("exact_run_preserved"))
                and not bool(result.get("new_submission_created"))
                and bool(result.get("installation_synced"))
                and bool(result.get("source_committed"))
                and bool(result.get("source_pushed"))
                and bool(result.get("ci_passed"))
                and bool(result.get("focused_tests"))
            )
            if not result_path.is_file():
                write_immutable_json(result_path, result)
            receipt_path = packet_path.parent / "repair-receipt.json"
            write_immutable_json(receipt_path, {
                "schema": "codex.chatgpt.goal-repair-receipt/v1",
                "goal_id": state["goal_id"],
                "cycle_index": state["cycle_index"],
                "incident": artifact_ref(packet_path),
                "result": artifact_ref(result_path),
                "accepted": accepted,
                "recorded_at": utc_now(),
            })
            if not accepted:
                return self._stop_for_user(
                    state, "AUTOMATIC_REPAIR_NOT_ACCEPTED",
                    str(result.get("summary") or "The repair transaction did not prove every required invariant."),
                    ["Review the repair result and explicitly resume only after the failed invariant is resolved."],
                    resume_phase="WEB_ACTIVE",
                )
            return self._write_state(
                state, "WEB_ACTIVE",
                active_action=None,
            )
        except Exception as exc:
            return self._stop_for_user(
                state, "AUTOMATIC_REPAIR_TRANSACTION_FAILED",
                str(exc)[:2000],
                ["Review the repair command evidence and explicitly resume after fixing the transaction failure."],
                resume_phase="WEB_ACTIVE",
            )

    def _stop_for_user(
        self,
        state: Mapping[str, Any],
        code: str,
        message: str,
        resume_conditions: Sequence[str],
        *,
        resume_phase: str | None = None,
    ) -> dict[str, Any]:
        if state["phase"] == "WAITING_USER":
            return dict(state)
        boundary = {
            "schema": GOAL_USER_ACTION_SCHEMA,
            "goal_id": state["goal_id"],
            "cycle_index": state["cycle_index"],
            "code": code,
            "message": message,
            "resume_conditions": list(resume_conditions),
            "resume_phase": resume_phase or ("CYCLE_READY" if state["phase"] == "CREATED" else state["phase"]),
            "created_at": utc_now(),
        }
        path = self.boundaries_dir / f"{int(state['revision']) + 1:06d}.json"
        write_immutable_json(path, boundary)
        return self._write_state(state, "WAITING_USER", boundary=artifact_ref(path), active_action=None)

    def status(self) -> dict[str, Any]:
        state = self.prepare()
        boundary_code = None
        if isinstance(state.get("boundary"), Mapping):
            try:
                boundary_code = load_json(validate_artifact_ref(state["boundary"], parent=self.goal_dir)).get("code")
            except Exception:
                boundary_code = "BOUNDARY_REFERENCE_INVALID"
        return {
            "schema": "codex.chatgpt.goal-status/v1",
            "goal_id": state["goal_id"],
            "project_root": str(self.project_root),
            "phase": state["phase"],
            "cycle_index": state["cycle_index"],
            "max_cycles": state["max_cycles"],
            "revision": state["revision"],
            "active_action": state["active_action"],
            "last_cycle_result": state["last_cycle_result"],
            "last_host_gates": state["last_host_gates"],
            "repair_family_count": len(state["repair_families"]),
            "boundary_code": boundary_code,
            "final": state["final"],
            "updated_at": state["updated_at"],
            "mechanical_only": True,
            "browser_observation_performed": False,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable non-LLM supervisor for repeated comprehensive-v4 goal cycles.")
    parser.add_argument("command", choices=["prepare", "run", "resume", "status"])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--state-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        supervisor = GoalSupervisor(args.manifest, state_root=args.state_root)
        if args.command == "prepare":
            result = supervisor.prepare()
        elif args.command == "run":
            result = supervisor.run()
        elif args.command == "resume":
            result = supervisor.resume()
        else:
            result = supervisor.status()
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(getattr(exc, "code", type(exc).__name__))
        print(json.dumps({"ok": False, "error": {"code": code, "message": str(exc)}}, ensure_ascii=False, indent=2, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
