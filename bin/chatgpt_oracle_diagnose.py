#!/usr/bin/env python
"""Read-only Oracle failure-signature report.

This tool never launches Oracle, never touches a browser, and never mutates
run state.  It classifies every persisted run into a small bounded set of
buckets so repairs target the layer that actually fails instead of the layer
that reported the symptom.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, TextIO

BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


STATE = _load("oracle_diagnose_state", BIN / "chatgpt_oracle_state.py")

SCHEMA = "codex.chatgpt.oracle-diagnosis/v1"
TRIAGE_SCHEMA = "codex.chatgpt.oracle-triage/v1"
WATCH_SCHEMA = "codex.chatgpt.oracle-watch-event/v1"

# Ordered, mutually exclusive buckets.  The first matching rule wins, so keep
# pre-submit host/UI causes ahead of post-submit provider causes: a run that
# never reached the composer must never be reported as a recovery defect.
PRE_SUBMIT_HOST = "pre-submit-host-environment"
PRE_SUBMIT_UI = "pre-submit-ui-contract"
BROWSER_LIFETIME = "browser-lifetime-lost"
PROVIDER_INCOMPLETE = "post-submit-provider-incomplete"
RECOVERY_BINDING = "post-submit-recovery-binding"
TASK_NOT_EXECUTED = "terminal-task-not-executed"
COMPLETE = "complete"
LEGACY_COMPLETE = "complete-legacy-ledger"
ACTIVE = "active-or-uncertain"
UNCLASSIFIED = "unclassified"

BUCKETS = (
    COMPLETE,
    LEGACY_COMPLETE,
    ACTIVE,
    PRE_SUBMIT_HOST,
    PRE_SUBMIT_UI,
    BROWSER_LIFETIME,
    PROVIDER_INCOMPLETE,
    RECOVERY_BINDING,
    TASK_NOT_EXECUTED,
    UNCLASSIFIED,
)

SIGNATURE_RULES: tuple[tuple[str, str, str], ...] = (
    ("rsync", PRE_SUBMIT_HOST, "oracle-profile-copy-requires-rsync"),
    ("cannot be combined with", PRE_SUBMIT_HOST, "oracle-launch-flags-mutually-exclusive"),
    ("app mention suggestion did not appear", PRE_SUBMIT_UI, "app-mention-suggestion-absent"),
    ("app mention was not confirmed", PRE_SUBMIT_UI, "app-mention-not-confirmed"),
    ("Unable to find model option", PRE_SUBMIT_UI, "model-option-label-missing"),
    ("Thinking time: selection unverified", PRE_SUBMIT_UI, "thinking-time-selection-unverified"),
    ("Thinking time: unknown outcome selecting", PRE_SUBMIT_UI, "thinking-time-selection-unverified"),
    ("Chrome window closed", BROWSER_LIFETIME, "browser-window-closed-early"),
    ("disconnected before completion", BROWSER_LIFETIME, "browser-disconnected-early"),
    ("ECONNREFUSED", RECOVERY_BINDING, "recovery-cdp-connection-refused"),
    ("timed out before completion", PROVIDER_INCOMPLETE, "assistant-response-timeout"),
    (
        "Prompt did not appear in conversation before timeout",
        PROVIDER_INCOMPLETE,
        "submission-uncertain-prompt-not-observed",
    ),
)

# Every eligible pre-submit family gets its own signature and settlement reason
# so the report, the settlement receipt, and the audit trail cannot disagree.
ELIGIBILITY_SIGNATURES = {
    "oracle-chatgpt-session-absent/v1": "session-absent",
    "oracle-direct-app-route-unconfirmed/v1": "app-route-unconfirmed",
    "oracle-web-multi-child/v1": "web-multi-child",
}
ELIGIBILITY_SETTLEMENT_REASONS = {
    "oracle-chatgpt-session-absent/v1": "user-confirmed-no-submission-after-session-absent",
    "oracle-direct-app-route-unconfirmed/v1": "user-confirmed-no-submission-after-app-route-unconfirmed",
    "oracle-web-multi-child/v1": "user-confirmed-no-submission-after-prompt-timeout",
}
REMEDIATION = {
    PRE_SUBMIT_HOST: "Fix the local launch contract; no web submission occurred, so a fresh run is safe.",
    PRE_SUBMIT_UI: "Relax or realign the ChatGPT UI contract; no web submission occurred, so a fresh run is safe.",
    BROWSER_LIFETIME: "Keep the Oracle-owned browser alive for the run; recover the exact slug before any retry.",
    PROVIDER_INCOMPLETE: "Resume the exact slug with live recovery; never resubmit.",
    RECOVERY_BINDING: "Reopen only the persisted exact conversation URL; never resubmit.",
    TASK_NOT_EXECUTED: "Transport succeeded but the task did not run; inspect the durable output before deciding.",
    COMPLETE: "None.",
    LEGACY_COMPLETE: "None; durable output exists from a run recorded before terminal_harvested was tracked.",
    ACTIVE: "Leave ownership intact and observe the exact slug only.",
    UNCLASSIFIED: "Add a signature rule for this run before repairing anything.",
}


def _read_text(path: Path, *, limit: int = 200_000) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return data[-limit:].decode("utf-8", errors="replace")


def _output_is_nonempty(path: Path) -> bool:
    try:
        return path.is_file() and bool(path.read_bytes().strip())
    except OSError:
        return False


def classify_run(
    state: dict[str, Any],
    *,
    stdout_text: str,
    has_output: bool,
    transcript_text: str = "",
    output_text: str = "",
    user_confirmed_no_submission: bool = False,
    pre_submit_host_failure: dict[str, Any] | None = None,
    submission_authority: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return the bucket and signature for one persisted run.

    Ordering matters more than breadth here.  Local exit codes and local
    status never outrank durable evidence, and a pre-submit signature always
    wins over a post-submit interpretation.  The single submission-authority
    verdict from ``STATE.classify_submission_authority`` is binding when it is
    provided: a run the authority layer proved was never submitted must never
    be reported as an uncertain live session, and an eligible-but-unconfirmed
    pre-submit refusal stays a host-side decision awaiting explicit user
    confirmation.  No bucket beyond those two guarantees is re-derived here.
    """
    outcome = str(state.get("task_outcome") or "")
    # Single authority source, shared with the runner, so the report and the
    # runner can never disagree about what "finished" means.
    verdict = STATE.resolve_lifecycle(state, output_is_present=has_output)
    lifecycle = str(verdict["lifecycle"])
    source = str(verdict["authority_source"])

    authority_class = str((submission_authority or {}).get("class") or "")
    settlement_eligibility = (submission_authority or {}).get("settlement_eligibility")
    requires_user_confirmation = bool((submission_authority or {}).get("requires_user_confirmation"))
    if authority_class == "SUBMITTED_UNKNOWN" and requires_user_confirmation:
        # The authority layer bound exact eligible pre-submit evidence: nothing
        # was sent, but only an explicit user confirmation may release the
        # project lock.  This outranks the lifecycle-running fallback below so
        # an exited login-refusal run can never read as a live exact session.
        # The signature names the exact eligibility so this report, the
        # settlement receipt, and the audit trail cannot disagree.
        return {
            "bucket": PRE_SUBMIT_HOST,
            "signature": (
                f"{ELIGIBILITY_SIGNATURES.get(str(settlement_eligibility), 'pre-submit')}"
                "-awaiting-user-confirmation"
            ),
        }

    pre_submit_failure = state.get("pre_submit_failure")
    host_failure = pre_submit_failure if isinstance(pre_submit_failure, dict) else pre_submit_host_failure
    if (
        isinstance(host_failure, dict)
        and host_failure.get("output_absent") is True
        and host_failure.get("conversation_url_absent") is True
    ):
        code = str(host_failure.get("code") or "")
        if code == "ORACLE_ATTACHMENT_SIZE_PRELAUNCH_FAILED":
            return {"bucket": PRE_SUBMIT_HOST, "signature": "oracle-attachment-size-prelaunch-limit"}
        if code == "ORACLE_MODEL_SWITCHER_PRE_SUBMIT_FAILED":
            return {"bucket": PRE_SUBMIT_UI, "signature": "model-option-label-missing"}
        if code == "ORACLE_THINKING_TIME_PRE_SUBMIT_FAILED":
            return {"bucket": PRE_SUBMIT_UI, "signature": "thinking-time-selection-unverified"}
        if code == "ORACLE_MANUAL_LOGIN_PROFILE_UNINITIALIZED_PRELAUNCH_FAILED":
            return {"bucket": PRE_SUBMIT_HOST, "signature": "oracle-manual-login-profile-uninitialized"}
        if code == "SUBMISSION_NOT_READY" and host_failure.get("failed_checks"):
            return {"bucket": PRE_SUBMIT_HOST, "signature": "submission-readiness-not-ready"}
        if code == "DEVSPACE_SERVICE_RESTART_REQUIRED":
            return {"bucket": PRE_SUBMIT_HOST, "signature": "devspace-service-restart-required"}
        if code != "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED":
            return {"bucket": UNCLASSIFIED, "signature": "unrecognized-pre-submit-host-failure"}
        return {
            "bucket": PRE_SUBMIT_HOST,
            "signature": (
                "oracle-version-resolution-prelaunch-compatibility-drift"
                if host_failure.get("failure_reason") == "compatibility-version-drift"
                else "oracle-version-resolution-prelaunch-timeout"
            ),
        }
    if lifecycle == "abandoned":
        return {"bucket": ACTIVE, "signature": "explicitly-abandoned"}
    if has_output and outcome in {"blocked", "not_executed"}:
        return {
            "bucket": TASK_NOT_EXECUTED,
            "signature": (
                "durable-output-reports-blocked"
                if outcome == "blocked"
                else "durable-output-reports-no-execution"
            ),
        }
    evidence_texts = (stdout_text, transcript_text, output_text)
    if outcome not in {"executed", "legacy_unclassified", "not_applicable"} and any(
        "OAuth token request failed" in text and "503" in text
        for text in evidence_texts
    ):
        return {
            "bucket": PROVIDER_INCOMPLETE,
            "signature": "registered-app-oauth-token-request-503",
        }
    if lifecycle == "complete" and outcome in {
        "executed",
        "legacy_unclassified",
        "not_applicable",
    }:
        if source == "exact-terminal-evidence":
            return {"bucket": COMPLETE, "signature": "terminal-harvested-output"}
        return {"bucket": LEGACY_COMPLETE, "signature": "legacy-ledger-durable-output"}
    if lifecycle == "complete" and has_output and (
        outcome or str(state.get("task_outcome_contract") or "") == "v1"
    ):
        return {
            "bucket": PROVIDER_INCOMPLETE,
            "signature": "output-present-without-terminal-settlement",
        }
    if lifecycle == "complete":
        if source == "exact-terminal-evidence":
            return {"bucket": COMPLETE, "signature": "terminal-harvested-output"}
        return {"bucket": LEGACY_COMPLETE, "signature": "legacy-ledger-durable-output"}
    if user_confirmed_no_submission:
        if (
            str(state.get("task_outcome_reason") or "")
            == "user-confirmed-no-submission-after-session-absent"
        ):
            return {
                "bucket": PRE_SUBMIT_UI,
                "signature": "user-confirmed-no-submission-after-session-absent",
            }
        return {
            "bucket": PRE_SUBMIT_UI,
            "signature": (
                "user-confirmed-no-submission-after-app-route-unconfirmed"
                if STATE.ORACLE_APP_MENTION_ROUTE_UNCONFIRMED_MARKER in stdout_text
                else "user-confirmed-no-submission-after-prompt-timeout"
            ),
        }
    if str(state.get("transport_status") or "") == "post_submit_watchdog_timeout":
        return {
            "bucket": PROVIDER_INCOMPLETE,
            "signature": "host-wall-clock-expired-process-preserved",
        }

    for needle, bucket, signature in SIGNATURE_RULES:
        if needle in stdout_text:
            return {"bucket": bucket, "signature": signature}

    if lifecycle == "running" and authority_class != "PRE_SUBMIT_PROVEN":
        return {"bucket": ACTIVE, "signature": f"lifecycle-running-via-{source}"}
    if has_output:
        return {"bucket": PROVIDER_INCOMPLETE, "signature": "output-present-without-terminal-settlement"}
    if not has_output and ("Answer:" in stdout_text or "Answer:" in transcript_text):
        # The provider answered, but the durable artifact was never written, so
        # the run is recoverable rather than unknown.
        return {
            "bucket": PROVIDER_INCOMPLETE,
            "signature": "answer-observed-without-durable-output",
        }
    return {"bucket": UNCLASSIFIED, "signature": "no-recognized-signature"}


def iter_run_dirs(state_root: Path) -> Iterable[Path]:
    projects = state_root / "projects"
    if not projects.is_dir():
        return ()
    return sorted(path.parent for path in projects.glob("*/runs/*/state.json"))


def _state_root(value: Path | None) -> Path:
    return (value or STATE.oracle_state_root()).expanduser().resolve()


def _exact_run_dir(state_root: Path, value: Path) -> Path:
    directory = value.expanduser().resolve(strict=True)
    try:
        relative = directory.relative_to(state_root)
    except ValueError as exc:
        raise ValueError("RUN_DIR_OUTSIDE_STATE_ROOT") from exc
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or len(relative.parts) != 4
        or relative.parts[0] != "projects"
        or relative.parts[2] != "runs"
        or not (directory / "state.json").is_file()
    ):
        raise ValueError("RUN_DIR_INVALID")
    return directory


def _run_record(run_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    state_path = run_dir / "state.json"
    authority = STATE.classify_submission_authority(run_dir)
    try:
        state = STATE.load_state(state_path)
    except Exception as exc:  # noqa: BLE001 - corrupt state must remain actionable
        return None, {
            "run_dir": str(run_dir),
            "authority_class": str(authority.get("class") or ""),
            "settlement_eligibility": authority.get("settlement_eligibility"),
            "requires_user_confirmation": bool(authority.get("requires_user_confirmation")),
            "bucket": UNCLASSIFIED,
            "signature": "state-unreadable",
            "detail": type(exc).__name__,
        }
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output_path = Path(str(artifacts.get("output") or (run_dir / "output.md")))
    has_output = _output_is_nonempty(output_path)
    output_text = _read_text(output_path)
    lifecycle = STATE.resolve_lifecycle(state, output_is_present=has_output)
    verdict = classify_run(
        state,
        stdout_text=_read_text(run_dir / "stdout.log"),
        has_output=has_output,
        transcript_text=_read_text(run_dir / "transcript.md"),
        output_text=output_text,
        user_confirmed_no_submission=(
            STATE.proven_user_confirmed_no_submission(state_path) is not None
        ),
        pre_submit_host_failure=STATE.proven_pre_submit_host_failure(state_path),
        submission_authority=authority,
    )
    return state, {
        "run_dir": str(run_dir),
        "project_root": str(state.get("project_root") or ""),
        "status": str(state.get("status") or ""),
        "session_authority": str(state.get("session_authority") or ""),
        "lifecycle": str(lifecycle["lifecycle"]),
        "authority_source": str(lifecycle["authority_source"]),
        "authority_class": str(authority.get("class") or ""),
        "settlement_eligibility": authority.get("settlement_eligibility"),
        "requires_user_confirmation": bool(authority.get("requires_user_confirmation")),
        "output_path": str(output_path),
        **verdict,
    }


def _argv(script: str, *values: str) -> list[str]:
    return [sys.executable, str(BIN / script), *values]


def _next_action(
    state_root: Path,
    run_dir: Path,
    state: dict[str, Any] | None,
    record: dict[str, Any],
) -> dict[str, Any]:
    bucket = str(record["bucket"])
    lifecycle = str(record.get("lifecycle") or "needs_attention")
    owners: list[dict[str, str]] = []
    if state is not None and str(state.get("project_root") or ""):
        owners = STATE.unresolved_project_sessions(
            run_dir.parent,
            Path(str(state["project_root"])),
            exclude_run_id=str(state.get("run_id") or ""),
        )
    action: dict[str, Any] = {
        "kind": "none",
        "safe_for_fresh_run": False,
        "reason": REMEDIATION.get(bucket, ""),
        "argv": None,
    }
    if state is None or bucket == UNCLASSIFIED:
        action.update({
            "kind": "report_incident",
            "argv": _argv("chatgpt_oracle_incident.py", "report", "--run-dir", str(run_dir)),
        })
    elif (
        str(record.get("authority_class") or "") == "SUBMITTED_UNKNOWN"
        and record.get("requires_user_confirmation")
    ):
        # The authority layer bound an eligible pre-submit refusal.  Nothing was
        # sent, but the project lock may only be released by an explicit user
        # confirmation; never by a fresh run or state editing.
        action.update({
            "kind": "settle_no_submission",
            "reason": (
                "Exact pre-send refusal with no conversation URL; only an explicit "
                "user confirmation may release the project lock."
            ),
            "argv": _argv(
                "chatgpt_oracle_run.py",
                "settle-no-submission",
                "--run-dir",
                str(run_dir),
                "--confirmation",
                STATE.USER_CONFIRMED_NO_SUBMISSION,
                "--reason",
                ELIGIBILITY_SETTLEMENT_REASONS.get(
                    str(record.get("settlement_eligibility") or ""),
                    "user-confirmed-no-submission",
                ),
            ),
        })
    elif lifecycle == "running":
        action.update({
            "kind": "watch_exact_run",
            "argv": _argv(
                "chatgpt_oracle_diagnose.py",
                "--state-root",
                str(state_root),
                "watch",
                "--run-dir",
                str(run_dir),
            ),
        })
    elif bucket in {PRE_SUBMIT_HOST, PRE_SUBMIT_UI}:
        if owners:
            owner_dir = Path(owners[0]["state_path"]).parent
            action.update({
                "kind": "watch_exact_run",
                "reason": "Another exact session still owns this project.",
                "argv": _argv(
                    "chatgpt_oracle_diagnose.py",
                    "--state-root",
                    str(state_root),
                    "watch",
                    "--run-dir",
                    str(owner_dir),
                ),
            })
        else:
            action.update({"kind": "fix_then_fresh_run", "safe_for_fresh_run": True})
    elif bucket in {BROWSER_LIFETIME, PROVIDER_INCOMPLETE}:
        action.update({
            "kind": "recover_live",
            "argv": _argv(
                "chatgpt_oracle_run.py", "recover", "--run-dir", str(run_dir), "--action", "live"
            ),
        })
    elif bucket == RECOVERY_BINDING:
        action.update({
            "kind": "recover_harvest",
            "argv": _argv(
                "chatgpt_oracle_run.py", "recover", "--run-dir", str(run_dir), "--action", "harvest"
            ),
        })
    elif bucket == TASK_NOT_EXECUTED or lifecycle == "abandoned":
        action["kind"] = "inspect_output"
    return {**action, "unresolved_owners": owners}


def triage(
    *,
    state_root: Path | None = None,
    project_root: Path | None = None,
    run_dir: Path | None = None,
) -> dict[str, Any]:
    root = _state_root(state_root)
    if (project_root is None) == (run_dir is None):
        raise ValueError("CHOOSE_EXACTLY_ONE_OF_PROJECT_ROOT_OR_RUN_DIR")
    if run_dir is not None:
        directories = [_exact_run_dir(root, run_dir)]
        selector = {"run_dir": str(directories[0])}
    else:
        requested = project_root.expanduser()
        if not requested.is_absolute():
            raise ValueError("PROJECT_ROOT_ABSOLUTE_REQUIRED")
        expected = str(requested.resolve(strict=False)).casefold()
        directories = []
        for candidate in iter_run_dirs(root):
            try:
                state = STATE.load_state(candidate / "state.json")
            except Exception:  # corrupt state cannot be safely attributed to a project
                continue
            if str(Path(str(state.get("project_root") or "")).resolve(strict=False)).casefold() == expected:
                directories.append(candidate)
        selector = {"project_root": str(requested.resolve(strict=False))}
    entries: list[dict[str, Any]] = []
    for directory in sorted(directories, key=lambda item: item.name, reverse=True):
        state, record = _run_record(directory)
        entries.append({
            **record,
            "next_action": _next_action(root, directory, state, record),
        })
    return {
        "schema": TRIAGE_SCHEMA,
        "state_root": str(root),
        "selector": selector,
        "run_count": len(entries),
        "runs": entries,
    }


def watch(
    run_dir: Path,
    *,
    state_root: Path | None = None,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 0.0,
    emit: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    stderr: TextIO = sys.stderr,
) -> int:
    if not 0.25 <= poll_seconds <= 60:
        raise ValueError("POLL_SECONDS_OUT_OF_RANGE")
    if timeout_seconds < 0:
        raise ValueError("TIMEOUT_SECONDS_INVALID")
    root = _state_root(state_root)
    directory = _exact_run_dir(root, run_dir)
    writer = emit or (lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    started = clock()
    previous: tuple[str, ...] | None = None
    while True:
        report = triage(state_root=root, run_dir=directory)
        record = report["runs"][0]
        current = tuple(str(record.get(key) or "") for key in (
            "status", "session_authority", "lifecycle", "bucket", "signature"
        ))
        lifecycle = str(record["lifecycle"])
        if current != previous:
            writer({
                "schema": WATCH_SCHEMA,
                "event": "snapshot" if previous is None else "changed",
                "elapsed_seconds": round(clock() - started, 3),
                "run": record,
            })
            previous = current
        if lifecycle in {"complete", "needs_attention", "abandoned"}:
            if getattr(stderr, "isatty", lambda: False)():
                stderr.write("\a")
                stderr.flush()
            return 0 if record["bucket"] in {COMPLETE, LEGACY_COMPLETE} else 2
        elapsed = clock() - started
        if timeout_seconds and elapsed >= timeout_seconds:
            writer({
                "schema": WATCH_SCHEMA,
                "event": "timeout",
                "elapsed_seconds": round(elapsed, 3),
                "run_dir": str(directory),
            })
            return 3
        sleep(min(poll_seconds, max(0.0, timeout_seconds - elapsed)) if timeout_seconds else poll_seconds)


def diagnose(state_root: Path | None = None) -> dict[str, Any]:
    root = _state_root(state_root)
    runs: list[dict[str, Any]] = []
    for run_dir in iter_run_dirs(root):
        try:
            state = STATE.load_state(run_dir / "state.json")
        except Exception as exc:  # noqa: BLE001 - a corrupt run must stay visible
            authority = STATE.classify_submission_authority(run_dir)
            runs.append({
                "run_dir": str(run_dir),
                "authority_class": str(authority.get("class") or ""),
                "settlement_eligibility": authority.get("settlement_eligibility"),
                "requires_user_confirmation": bool(authority.get("requires_user_confirmation")),
                "bucket": UNCLASSIFIED,
                "signature": "state-unreadable",
                "detail": type(exc).__name__,
            })
            continue
        authority = STATE.classify_submission_authority(run_dir)
        artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
        output_path = Path(str(artifacts.get("output") or (run_dir / "output.md")))
        has_output = _output_is_nonempty(output_path)
        verdict = classify_run(
            state,
            stdout_text=_read_text(run_dir / "stdout.log"),
            has_output=has_output,
            transcript_text=_read_text(run_dir / "transcript.md"),
            output_text=_read_text(output_path),
            user_confirmed_no_submission=(
                STATE.proven_user_confirmed_no_submission(run_dir / "state.json") is not None
            ),
            pre_submit_host_failure=STATE.proven_pre_submit_host_failure(run_dir / "state.json"),
            submission_authority=authority,
        )
        runs.append({
            "run_dir": str(run_dir),
            "project_root": str(state.get("project_root") or ""),
            "status": str(state.get("status") or ""),
            "session_authority": str(state.get("session_authority") or ""),
            "authority_class": str(authority.get("class") or ""),
            "settlement_eligibility": authority.get("settlement_eligibility"),
            "requires_user_confirmation": bool(authority.get("requires_user_confirmation")),
            **verdict,
        })

    counts = {bucket: 0 for bucket in BUCKETS}
    for run in runs:
        counts[str(run["bucket"])] = counts.get(str(run["bucket"]), 0) + 1
    unresolved = [run for run in runs if run["bucket"] not in {COMPLETE, ACTIVE}]
    # A bucket is only fresh-run-safe when no run inside it still owns its
    # project.  An eligible pre-submit refusal awaiting explicit user
    # confirmation keeps its lock, so its bucket must not be advertised as safe.
    locked_buckets = {
        str(run["bucket"]) for run in runs if run.get("requires_user_confirmation")
    }
    safe_buckets = [
        bucket for bucket in (PRE_SUBMIT_HOST, PRE_SUBMIT_UI)
        if counts.get(bucket) and bucket not in locked_buckets
    ]
    return {
        "schema": SCHEMA,
        "state_root": str(root),
        "total_runs": len(runs),
        "bucket_counts": {name: count for name, count in counts.items() if count},
        "top_buckets": [
            {
                "bucket": name,
                "count": count,
                "remediation": (
                    REMEDIATION.get(name, "")
                    if name not in locked_buckets
                    else "A run in this bucket still owns its project: settle it with an "
                    "explicit user no-submission confirmation before any fresh run."
                ),
            }
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            if count
        ],
        "safe_for_fresh_run_buckets": safe_buckets,
        "unresolved_runs": unresolved,
    }


# `--summary-only` belongs to the aggregate no-subcommand report only.
# `triage` and `watch` are single-run forms and reject the flag; the rejection
# message spells out the exact usage so operators never conflate the two forms.
SUMMARY_ONLY_USAGE = (
    "SUMMARY_ONLY_FOR_AGGREGATE_DIAGNOSIS: `--summary-only` is accepted only by "
    "the aggregate no-subcommand report, e.g. "
    "`chatgpt_oracle_diagnose.py --summary-only`. `triage` and `watch` already "
    "target one exact run and reject `--summary-only`; call them without it."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Oracle failure-signature report.")
    parser.add_argument("--state-root", type=Path, default=None)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help=(
            "Aggregate-only: omit the per-run detail list from the no-subcommand "
            "report. Rejected with triage/watch, which are single-run forms."
        ),
    )
    commands = parser.add_subparsers(dest="command")
    triage_parser = commands.add_parser("triage", help="Classify one exact run or project and show safe next actions.")
    triage_selector = triage_parser.add_mutually_exclusive_group(required=True)
    triage_selector.add_argument("--project-root", type=Path)
    triage_selector.add_argument("--run-dir", type=Path)
    watch_parser = commands.add_parser("watch", help="Watch one exact persisted run without recovery or mutation.")
    watch_parser.add_argument("--run-dir", type=Path, required=True)
    watch_parser.add_argument("--poll-seconds", type=float, default=2.0)
    watch_parser.add_argument("--timeout-seconds", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "watch":
            if args.summary_only:
                raise ValueError(SUMMARY_ONLY_USAGE)
            return watch(
                args.run_dir,
                state_root=args.state_root,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
            )
        if args.command == "triage":
            if args.summary_only:
                raise ValueError(SUMMARY_ONLY_USAGE)
            report = triage(
                state_root=args.state_root,
                project_root=args.project_root,
                run_dir=args.run_dir,
            )
        else:
            report = diagnose(args.state_root)
            if args.summary_only:
                report = {key: value for key, value in report.items() if key != "unresolved_runs"}
    except (OSError, ValueError, STATE.OracleStateError) as exc:
        print(json.dumps({
            "ok": False,
            "error": {"code": "ORACLE_DIAGNOSE_FAILED", "message": str(exc)},
        }, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
