from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

STATE_PATH = Path(__file__).resolve().with_name("chatgpt_oracle_state.py")
COMPAT_PATH = Path(__file__).resolve().with_name("chatgpt_oracle_compat.py")
DEVSPACE_COMPAT_PATH = Path(__file__).resolve().with_name("chatgpt_devspace_compat.py")


def load_state_module():
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_state_runtime", STATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Oracle state module unavailable: {STATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STATE = load_state_module()


def load_compat_module():
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_compat_runtime", COMPAT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Oracle compatibility module unavailable: {COMPAT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


COMPAT = load_compat_module()


def load_devspace_compat_module():
    spec = importlib.util.spec_from_file_location(
        "chatgpt_devspace_compat_runtime",
        DEVSPACE_COMPAT_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"DevSpace compatibility module unavailable: {DEVSPACE_COMPAT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


DEVSPACE_COMPAT = load_devspace_compat_module()


class OracleRunError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def build_oracle_argv(config, layout, prompt: str) -> list[str]:
    lifecycle_args = [] if "--browser-hide-window" in config.oracle_args else ["--browser-hide-window"]
    command = [
        *config.oracle_command,
        "--engine", "browser",
        "--model", config.model,
        "--browser-model-strategy", config.model_strategy,
        "--browser-thinking-time", config.thinking_time,
        "--browser-research", config.research,
        "--browser-archive", config.archive,
        *lifecycle_args,
        *config.oracle_args,
        "--slug", layout.slug,
        "--prompt", prompt,
        "--write-output", str(layout.output_path),
    ]
    if config.transport == "pro-attachment-only":
        attachment_args: list[str] = []
        for path in config.attachments:
            attachment_args.extend(["--file", str(path)])
        command[command.index("--slug"):command.index("--slug")] = [
            "--browser-attachments", "always", *attachment_args,
        ]
    if config.copy_profile is not None:
        command[command.index("--slug"):command.index("--slug")] = ["--copy-profile", str(config.copy_profile)]
    if config.transport != "pro-attachment-only" and any(
        item == "--file" or item.startswith("--file=") or item == "-f" for item in command
    ):
        raise OracleRunError("FILE_TRANSPORT_FORBIDDEN", "general GPT browser runs must not use --file")
    return command


def resolve_oracle_version(command: Sequence[str], *, run_factory=subprocess.run, platform_name: str | None = None) -> str:
    completed = run_factory(
        [*command, "--version"],
        cwd=None,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        **STATE.windows_subprocess_kwargs(platform_name=platform_name),
    )
    if completed.returncode != 0:
        raise OracleRunError("ORACLE_VERSION_FAILED", "Oracle version could not be resolved", {"exit_code": completed.returncode})
    lines = [line.strip() for line in f"{completed.stdout or ''}\n{completed.stderr or ''}".splitlines() if line.strip()]
    if not lines:
        raise OracleRunError("ORACLE_VERSION_EMPTY", "Oracle version command returned no version")
    return lines[0]


def dry_run_payload(config, layout, argv: Sequence[str], prompt: str) -> dict[str, Any]:
    return {
        "ok": True,
        "status": "dry-run",
        "run_id": layout.run_id,
        "run_dir": str(layout.run_dir),
        "argv": STATE.command_for_display(argv),
        "prompt_first_line": prompt.splitlines()[0],
        "mission_path": str(config.mission_path),
        "mission_sha256": config.mission_sha256,
        "transport": config.transport,
        "attachments": [
            {"path": str(path), "sha256": digest}
            for path, digest in zip(config.attachments, config.attachment_sha256s, strict=True)
        ],
        "output_path": str(layout.output_path),
        "transcript_path": str(layout.transcript_path),
        "stdout_path": str(layout.stdout_path),
        "stderr_path": str(layout.stderr_path),
        "contains_file_flag": "--file" in argv,
    }


def append_error(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((message.rstrip() + "\n").encode("utf-8", errors="replace"))


SESSION_STATE_RE = re.compile(r"(?im)^\s*State:\s*([a-z][a-z0-9_-]*)\s*$")
LIVE_SESSION_STATES = {"running", "streaming", "thinking", "active"}
TERMINAL_SESSION_STATES = {
    "complete", "completed", "done", "finished", "failed", "error", "cancelled", "canceled",
}


def exact_session_state(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = SESSION_STATE_RE.findall(text)
    return matches[-1].casefold() if matches else None


def historical_session_authority(run_dir: Path, state: dict[str, Any]) -> str:
    """Recover the strongest exact-session authority from durable observer logs."""
    current = str(state.get("session_authority") or "submitted_unknown")
    if (
        current == "terminal"
        and state.get("terminal_harvested") is True
        and STATE.output_is_nonempty(Path(str(state["artifacts"]["output"])))
    ):
        return "terminal"
    strongest = current
    for path in sorted(run_dir.glob("recovery-*-stdout.log"), key=lambda item: item.name):
        observed = exact_session_state(path)
        if observed in TERMINAL_SESSION_STATES:
            strongest = "terminal_observed"
            break
    return strongest


def execute_run(
    manifest_path: Path,
    *,
    dry_run: bool = False,
    run_factory: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
    compat_factory: Callable[[str], dict[str, Any]] = COMPAT.ensure_oracle_compatibility,
    devspace_compat_factory: Callable[[], dict[str, Any]] = (
        DEVSPACE_COMPAT.ensure_devspace_compatibility
    ),
) -> dict[str, Any]:
    config = STATE.load_manifest(manifest_path, platform_name=platform_name)
    layout = STATE.create_layout(config, run_id=config.requested_run_id)
    transport_mission_path = layout.run_dir / "mission.md"
    # The app reads the project mission. The copied bytes below are host-only
    # immutable evidence and are never exposed as the workspace handoff path.
    prompt = STATE.composer_prompt(config, config.mission_path)
    argv = build_oracle_argv(config, layout, prompt)
    if dry_run:
        return dry_run_payload(config, layout, argv, prompt)

    STATE.cleanup_prior_boot_browser_temps(config.run_root, platform_name=platform_name)
    mission_bytes = config.mission_path.read_bytes()
    actual_mission_sha256 = hashlib.sha256(mission_bytes).hexdigest()
    if actual_mission_sha256 != config.mission_sha256:
        raise OracleRunError(
            "MISSION_CHANGED_BEFORE_PREPARE",
            "mission bytes changed after manifest validation",
            {"expected": config.mission_sha256, "actual": actual_mission_sha256},
        )
    for attachment, expected in zip(config.attachments, config.attachment_sha256s, strict=True):
        actual = STATE.sha256_file(attachment)
        if actual != expected:
            raise OracleRunError(
                "ATTACHMENT_CHANGED_BEFORE_PREPARE",
                "attachment bytes changed after manifest validation",
                {"path": str(attachment), "expected": expected, "actual": actual},
            )
    layout.run_dir.mkdir(parents=True, exist_ok=False)
    transport_mission_path.write_bytes(mission_bytes)
    STATE.write_json_atomic(layout.state_path, STATE.state_payload(config, layout, status="prepared", resolved_version="unresolved"))
    layout.stdout_path.touch()
    layout.stderr_path.touch()
    oracle_env = STATE.browser_temp_environment(layout.browser_temp_path, platform_name=platform_name)
    try:
        version = resolve_oracle_version(config.oracle_command, run_factory=run_factory, platform_name=platform_name)
        compat_factory(version)
        if config.transport == "devspace":
            devspace_compat = devspace_compat_factory()
            if devspace_compat.get("service_restart_required"):
                raise OracleRunError(
                    "DEVSPACE_SERVICE_RESTART_REQUIRED",
                    "DevSpace was safely patched before submission and must be restarted once",
                    {"package_roots": devspace_compat.get("package_roots", [])},
                )
        STATE.update_state(layout.state_path, status="prepared", resolved_version=version)
    except Exception as exc:
        code = f"{exc.code}: " if isinstance(exc, OracleRunError) else ""
        append_error(layout.stderr_path, f"version resolution failed: {code}{exc}")
        STATE.write_transcript(layout)
        return {
            "ok": False,
            "run_dir": str(layout.run_dir),
            "result": STATE.update_state(layout.state_path, status="failed"),
        }

    try:
        with layout.stdout_path.open("wb") as stdout_handle, layout.stderr_path.open("wb") as stderr_handle:
            mutex_root = (
                config.project_root / ".oracle-parallel-submit" / str(config.parallel_parent_id)
                if config.parallel_parent_id
                else config.project_root
            )
            with STATE.project_submit_mutex(mutex_root, timeout_seconds=config.submit_mutex_timeout_seconds, platform_name=platform_name):
                owners = STATE.unresolved_project_sessions(
                    config.run_root,
                    config.project_root,
                    parallel_parent_id=config.parallel_parent_id,
                    exclude_run_id=layout.run_id,
                )
                if owners:
                    raise OracleRunError(
                        "PROJECT_SESSION_STILL_LIVE",
                        "an exact Oracle session still owns this project; recover it before submitting",
                        {"owners": owners},
                    )
                original_mission_sha256 = STATE.sha256_file(config.mission_path)
                current_mission_sha256 = STATE.sha256_file(transport_mission_path)
                if original_mission_sha256 != config.mission_sha256 or current_mission_sha256 != config.mission_sha256:
                    raise OracleRunError(
                        "MISSION_CHANGED_BEFORE_SUBMIT",
                        "mission bytes changed after manifest validation",
                        {
                            "expected": config.mission_sha256,
                            "original_actual": original_mission_sha256,
                            "evidence_actual": current_mission_sha256,
                        },
                    )
                for attachment, expected in zip(config.attachments, config.attachment_sha256s, strict=True):
                    actual = STATE.sha256_file(attachment)
                    if actual != expected:
                        raise OracleRunError(
                            "ATTACHMENT_CHANGED_BEFORE_SUBMIT",
                            "attachment bytes changed after manifest validation",
                            {"path": str(attachment), "expected": expected, "actual": actual},
                        )
                process = popen_factory(
                    argv,
                    cwd=str(config.project_root),
                    env=oracle_env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **STATE.windows_subprocess_kwargs(platform_name=platform_name),
                )
                STATE.update_state(
                    layout.state_path,
                    status="running",
                    resolved_version=version,
                    session_authority="submitted_unknown",
                )
                if not config.parallel_parent_id:
                    exit_code = int(process.wait())
            if config.parallel_parent_id:
                exit_code = int(process.wait())
    except Exception as exc:
        code = f"{exc.code}: " if isinstance(exc, OracleRunError) else ""
        append_error(layout.stderr_path, f"Oracle launch/run failed: {code}{exc}")
        STATE.write_transcript(layout)
        latest = STATE.load_state(layout.state_path)
        if latest.get("session_authority") == "pre_submit":
            STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
        return {"ok": False, "run_dir": str(layout.run_dir), "result": STATE.update_state(layout.state_path, status="failed")}
    STATE.write_transcript(layout)
    # Once Oracle has been launched, a nonzero local exit (including the
    # browser response timeout) does not prove that the exact web session
    # failed or stopped. Preserve same-project ownership and require exact-slug
    # recovery instead of presenting a terminal local failure.
    transport_complete = exit_code == 0 and STATE.output_is_nonempty(layout.output_path)
    task_outcome = (
        STATE.classify_task_outcome(
            layout.output_path,
            contract=config.task_outcome_contract,
            transport=config.transport,
        )
        if transport_complete
        else "pending"
    )
    semantic_complete = task_outcome in {
        "executed",
        "not_applicable",
        "legacy_unclassified",
    }
    status = "complete" if transport_complete and semantic_complete else "attention_required"
    if transport_complete:
        state = STATE.update_state(
            layout.state_path,
            status=status,
            exit_code=exit_code,
            session_authority="terminal",
            terminal_harvested=True,
            artifact_sha256=STATE.sha256_file(layout.output_path),
            transport_status="complete",
            task_outcome=task_outcome,
            task_outcome_reason=(
                "explicit-output-marker"
                if task_outcome in {"executed", "not_executed", "blocked"}
                else task_outcome
            ),
        )
        STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
    else:
        state = STATE.update_state(
            layout.state_path,
            status=status,
            exit_code=exit_code,
            session_authority="submitted_unknown",
            transport_status="failed" if exit_code else "incomplete",
            task_outcome=task_outcome,
        )
    return {"ok": status == "complete", "run_dir": str(layout.run_dir), "result": state}


def recovery_argv(command: Sequence[str], locator: str, action: str, output_path: Path) -> list[str]:
    if action not in {"harvest", "live"}:
        raise OracleRunError("RECOVERY_ACTION_INVALID", "recovery action must be harvest or live")
    # Oracle's bounded browser recovery reopens only the exact conversation URL
    # persisted under this slug.  Do not pass --no-recover here: it disables
    # that safe harvest path and leaves a dead CDP endpoint as ECONNREFUSED.
    argv = [*command, "session", locator, f"--{action}", "--write-output", str(output_path)]
    if "restart" in argv or "--prompt" in argv or "-p" in argv:
        raise OracleRunError("RECOVERY_COMMAND_UNSAFE", "recovery must not restart or submit a new prompt")
    return argv


def _recover_run_locked(
    run_dir: Path,
    *,
    action: str,
    dry_run: bool = False,
    oracle_command: Sequence[str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    state = STATE.load_state(directory / "state.json")
    historical_authority = historical_session_authority(directory, state)
    if (
        STATE.SESSION_AUTHORITY_RANK.get(historical_authority, -1)
        > STATE.SESSION_AUTHORITY_RANK.get(str(state.get("session_authority") or ""), -1)
    ):
        state = STATE.update_state(
            directory / "state.json",
            status="attention_required",
            exit_code=state.get("exit_code"),
            session_authority=historical_authority,
        )
    if (
        state.get("status") == "complete"
        and state.get("session_authority") == "terminal"
        and state.get("terminal_harvested") is True
        and STATE.output_is_nonempty(Path(str(state["artifacts"]["output"])))
    ):
        outcome = str(state.get("task_outcome") or "legacy_unclassified")
        return {
            "ok": outcome in {"executed", "not_applicable", "legacy_unclassified"},
            "status": "complete",
            "run_dir": str(directory),
            "action": "none",
            "result": state,
            "output_path": str(state["artifacts"]["output"]),
            "monotonic_noop": True,
        }
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator:
        raise OracleRunError("SESSION_LOCATOR_MISSING", "run state has no Oracle session locator")
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output_path = Path(str(artifacts.get("output") or (directory / "output.md"))).expanduser().resolve()
    if not STATE.is_within(STATE.oracle_state_root(), output_path):
        raise OracleRunError("RECOVERY_OUTPUT_OUTSIDE_HOST_STATE", "recovery output must remain inside host-only Oracle state")
    stored_command = oracle.get("command")
    command = STATE.validate_oracle_command(list(oracle_command) if oracle_command is not None else stored_command)
    argv_output = directory / f"recovery-{action}-candidate.md"
    argv = recovery_argv(command, locator, action, argv_output)
    if dry_run:
        return {"ok": True, "status": "dry-run", "run_dir": str(directory), "action": action, "argv": STATE.command_for_display(argv)}
    stdout_path = directory / f"recovery-{action}-stdout.log"
    stderr_path = directory / f"recovery-{action}-stderr.log"
    recovery_browser_temp = directory / f"recovery-{action}-browser-temp"
    recovery_env = STATE.browser_temp_environment(recovery_browser_temp, platform_name=platform_name)
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = popen_factory(
                argv,
                cwd=str(state["project_root"]),
                env=recovery_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                shell=False,
                **STATE.windows_subprocess_kwargs(platform_name=platform_name),
            )
            exit_code = int(process.wait())
    finally:
        STATE.cleanup_owned_browser_temp(recovery_browser_temp)
    observed_session_state = exact_session_state(stdout_path)
    if observed_session_state in LIVE_SESSION_STATES:
        if argv_output.exists():
            argv_output.unlink()
        prior_authority = str(state.get("session_authority") or "")
        updated = STATE.update_state(
            directory / "state.json",
            status="running",
            exit_code=exit_code,
            session_authority="live",
        )
        settle_disagreement = str(updated.get("session_authority") or "") in {
            "terminal_observed", "terminal",
        }
        return {
            "ok": False,
            "status": "terminal_settle_disagreement" if settle_disagreement else "session_live",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "exact_session_state": observed_session_state,
            "prior_session_authority": prior_authority,
            "session_authority": updated.get("session_authority"),
            "result": updated,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    if action == "live":
        if argv_output.exists():
            argv_output.unlink()
        authority = "terminal_observed" if observed_session_state in TERMINAL_SESSION_STATES else "submitted_unknown"
        updated = STATE.update_state(
            directory / "state.json",
            status="attention_required",
            exit_code=exit_code,
            session_authority=authority,
        )
        return {
            "ok": False,
            "status": "terminal_observed" if authority == "terminal_observed" else "attention_required",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "exact_session_state": observed_session_state,
            "result": updated,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    if (
        exit_code == 0
        and observed_session_state in TERMINAL_SESSION_STATES
        and STATE.output_is_nonempty(argv_output)
    ):
        os.replace(argv_output, output_path)
    layout = STATE.RunLayout(
        str(state["run_id"]),
        str(oracle.get("slug") or locator),
        directory,
        directory / "state.json",
        output_path,
        Path(str(artifacts.get("transcript") or (directory / "transcript.md"))),
        Path(str(artifacts.get("stdout") or (directory / "stdout.log"))),
        Path(str(artifacts.get("stderr") or (directory / "stderr.log"))),
        Path(str(artifacts.get("browser_temp") or (directory / "browser-temp"))).resolve(),
    )
    STATE.write_transcript(layout)
    harvested = (
        exit_code == 0
        and observed_session_state in TERMINAL_SESSION_STATES
        and STATE.output_is_nonempty(output_path)
    )
    # A failed recovery process is also not web-terminal evidence. Only an
    # exact terminal observation plus a nonempty durable output may complete.
    contract = str(state.get("task_outcome_contract") or "legacy")
    transport = str(state.get("transport") or "devspace")
    task_outcome = (
        STATE.classify_task_outcome(output_path, contract=contract, transport=transport)
        if harvested
        else "pending"
    )
    semantic_complete = task_outcome in {
        "executed",
        "not_applicable",
        "legacy_unclassified",
    }
    status = "complete" if harvested and semantic_complete else "attention_required"
    latest = STATE.load_state(layout.state_path)
    latest_output = Path(str(latest.get("artifacts", {}).get("output") or output_path))
    if latest.get("status") == "complete" and STATE.output_is_nonempty(latest_output):
        return {
            "ok": True,
            "status": "complete",
            "run_dir": str(directory),
            "action": action,
            "exit_code": exit_code,
            "result": latest,
            "output_path": str(latest_output),
            "monotonic_race_preserved": True,
        }
    updated = STATE.update_state(
        layout.state_path,
        status=status,
        exit_code=exit_code,
        session_authority="terminal" if harvested else (
            "terminal_observed" if observed_session_state in TERMINAL_SESSION_STATES else "submitted_unknown"
        ),
        terminal_harvested=harvested,
        artifact_sha256=STATE.sha256_file(output_path) if harvested else None,
        transport_status="complete" if harvested else "incomplete",
        task_outcome=task_outcome,
        task_outcome_reason=(
            "explicit-output-marker"
            if task_outcome in {"executed", "not_executed", "blocked"}
            else task_outcome
        ),
    )
    if harvested:
        STATE.cleanup_owned_browser_temp(layout.browser_temp_path)
    return {
        "ok": status == "complete",
        "status": status,
        "run_dir": str(directory),
        "action": action,
        "exit_code": exit_code,
        "result": updated,
        "output_path": str(output_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def adjudicate_task_outcome(
    run_dir: Path,
    *,
    expected_output_sha256: str,
    task_outcome: str,
    reason: str,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    state_path = directory / "state.json"
    state = STATE.load_state(state_path)
    output_path = Path(str((state.get("artifacts") or {}).get("output") or ""))
    if not output_path.is_file() or not STATE.is_within(STATE.oracle_state_root(), output_path.resolve()):
        raise OracleRunError(
            "ADJUDICATION_OUTPUT_INVALID",
            "exact run output is unavailable or outside host state",
        )
    actual = STATE.sha256_file(output_path)
    if actual != expected_output_sha256.strip().casefold():
        raise OracleRunError(
            "ADJUDICATION_OUTPUT_HASH_MISMATCH",
            "exact output changed before task outcome adjudication",
            {"expected": expected_output_sha256, "actual": actual},
        )
    normalized = task_outcome.strip().casefold()
    if normalized not in {"executed", "not_executed", "blocked", "unknown"}:
        raise OracleRunError(
            "ADJUDICATION_TASK_OUTCOME_INVALID",
            "task outcome must be executed, not_executed, blocked, or unknown",
        )
    if (
        str(state.get("session_authority") or "") != "terminal"
        or state.get("terminal_harvested") is not True
    ):
        raise OracleRunError(
            "ADJUDICATION_TERMINAL_REQUIRED",
            "only a durably harvested terminal run may be adjudicated",
        )
    updated = STATE.update_state(
        state_path,
        status=str(state.get("status") or "complete"),
        exit_code=state.get("exit_code"),
        transport_status="complete",
        task_outcome=normalized,
        task_outcome_reason=reason.strip() or "explicit-exact-output-adjudication",
    )
    return {
        "ok": normalized == "executed",
        "status": "task_outcome_adjudicated",
        "run_dir": str(directory),
        "output_path": str(output_path),
        "output_sha256": actual,
        "task_outcome": normalized,
        "safe_for_fresh_retry": normalized == "not_executed",
        "result": updated,
    }


def recover_run(
    run_dir: Path,
    *,
    action: str,
    dry_run: bool = False,
    oracle_command: Sequence[str] | None = None,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
    settle_timeout_seconds: float = 0,
    settle_interval_seconds: float = 15,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    directory = run_dir.expanduser().resolve(strict=True)
    stored = STATE.load_state(directory / "state.json")
    project_root = Path(str(stored.get("project_root") or "")).expanduser().resolve(strict=True)
    parallel_parent_id = str(stored.get("parallel_parent_id") or "").strip().casefold()
    if parallel_parent_id and STATE.PARENT_ID_RE.fullmatch(parallel_parent_id) is None:
        raise OracleRunError(
            "RECOVERY_PARALLEL_PARENT_ID_INVALID",
            "stored parallel parent id is invalid",
            {"parallel_parent_id": parallel_parent_id},
        )
    mutex_root = (
        project_root / ".oracle-parallel-submit" / parallel_parent_id
        if parallel_parent_id
        else project_root
    )
    with STATE.project_submit_mutex(
        mutex_root,
        timeout_seconds=30,
        platform_name=platform_name,
    ):
        result = _recover_run_locked(
            directory,
            action=action,
            dry_run=dry_run,
            oracle_command=oracle_command,
            popen_factory=popen_factory,
            platform_name=platform_name,
        )
        if dry_run or action != "live" or settle_timeout_seconds <= 0:
            return result
        deadline = time.monotonic() + settle_timeout_seconds
        while True:
            if result.get("ok"):
                return result
            if result.get("status") == "terminal_observed":
                return _recover_run_locked(
                    directory,
                    action="harvest",
                    dry_run=False,
                    oracle_command=oracle_command,
                    popen_factory=popen_factory,
                    platform_name=platform_name,
                )
            current = result.get("result") if isinstance(result.get("result"), dict) else {}
            authority = str(current.get("session_authority") or "")
            exact_state = str(result.get("exact_session_state") or "").casefold()
            still_live_or_unsettled = (
                result.get("status") in {"session_live", "terminal_settle_disagreement"}
                or authority == "live"
                and exact_state in {"", "active", "running", "streaming", "thinking", "stalled"}
            )
            if not still_live_or_unsettled:
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {
                    **result,
                    "ok": False,
                    "status": "live_settle_timeout",
                    "settle_timeout_seconds": settle_timeout_seconds,
                    "next_action": "resume the same exact-slug live recovery; never replace or resubmit",
                }
            sleep(min(settle_interval_seconds, remaining))
            result = _recover_run_locked(
                directory,
                action="live",
                dry_run=False,
                oracle_command=oracle_command,
                popen_factory=popen_factory,
                platform_name=platform_name,
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run additive Oracle browser missions without modifying agbrowse routing.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--dry-run", action="store_true")
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("--run-dir", type=Path, required=True)
    recover_parser.add_argument("--action", choices=("harvest", "live"), required=True)
    recover_parser.add_argument("--oracle-command", nargs="+")
    recover_parser.add_argument("--dry-run", action="store_true")
    recover_parser.add_argument(
        "--settle-timeout-seconds",
        type=float,
        default=5400,
        help="For live recovery, keep the exact slug in one process until terminal or this bounded deadline.",
    )
    recover_parser.add_argument(
        "--settle-interval-seconds",
        type=float,
        default=15,
    )
    adjudicate_parser = commands.add_parser("adjudicate")
    adjudicate_parser.add_argument("--run-dir", type=Path, required=True)
    adjudicate_parser.add_argument("--expected-output-sha256", required=True)
    adjudicate_parser.add_argument(
        "--task-outcome",
        choices=("executed", "not_executed", "blocked", "unknown"),
        required=True,
    )
    adjudicate_parser.add_argument("--reason", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            payload = execute_run(args.manifest, dry_run=args.dry_run)
        elif args.command == "recover":
            payload = recover_run(
                args.run_dir,
                action=args.action,
                dry_run=args.dry_run,
                oracle_command=args.oracle_command,
                settle_timeout_seconds=args.settle_timeout_seconds,
                settle_interval_seconds=args.settle_interval_seconds,
            )
        else:
            payload = adjudicate_task_outcome(
                args.run_dir,
                expected_output_sha256=args.expected_output_sha256,
                task_outcome=args.task_outcome,
                reason=args.reason,
            )
    except STATE.OracleStateError as exc:
        payload = exc.envelope()
    except OracleRunError as exc:
        payload = exc.envelope()
    except Exception as exc:
        payload = OracleRunError("ORACLE_RUN_FAILED", str(exc)).envelope()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
