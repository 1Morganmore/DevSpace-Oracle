from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
BRIDGE_PATH = Path(__file__).resolve().with_name("chatgpt_agbrowse_bridge.py")
DEFAULT_CONTRACT = CODEX_HOME / "contracts" / "agbrowse-0.1.18.json"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("chatgpt_agbrowse_bridge_run", BRIDGE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"bridge unavailable: {BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BRIDGE = _load_bridge()


def load_manifest(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("manifest is not JSON and PyYAML is unavailable") from exc
        value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise RuntimeError("manifest root must be an object")
    return value


def project_root(manifest: dict[str, Any], manifest_path: Path) -> str:
    value = manifest.get("project_root") or manifest.get("workspace_root") or manifest.get("cwd")
    return str(Path(str(value)).expanduser().resolve()) if value else str(manifest_path.parent.resolve())


def dry_run(manifest: dict[str, Any], contract_path: Path) -> dict[str, Any]:
    contract = BRIDGE.read_contract(contract_path)
    package = contract.get("agbrowse") if isinstance(contract.get("agbrowse"), dict) else {}
    executable = str(package.get("executablePath") or contract.get("executable") or "agbrowse")
    mode = str(manifest.get("mode_label") or "GPT-5.6")
    app_policy = str(
        manifest.get("app_policy")
        or ("forbidden" if mode.casefold() == "pro" else "required")
    )
    selection_transport = str(manifest.get("app_selection_transport") or "inline-pill-reuse").strip()
    app_requested = app_policy == "required" and bool(manifest.get("chatgpt_app_name") or manifest.get("app_name"))
    research_requested = mode.casefold() in {"deep-research", "deep research"}
    if app_requested and selection_transport != "inline-pill-reuse":
        raise RuntimeError("required app dry-run requires explicit inline-pill-reuse selection evidence")
    if research_requested and (
        str(manifest.get("research_selection_transport") or "") != "preselected-research"
        or str(manifest.get("research_selection_contract") or "")
        != "codex.chatgpt.capability-selection/v1"
    ):
        raise RuntimeError("Deep Research dry-run requires the preselected-research capability contract")
    command = BRIDGE.build_send_command(
        {"requested": {"app_policy": app_policy}},
        manifest,
        executable,
        preselected_app=app_requested and selection_transport == "inline-pill-reuse",
        connected_app_auto=False,
        preselected_research=research_requested,
    )
    prompt_contract = BRIDGE.STATE.prompt_contract(manifest, require_file=True)
    redacted = []
    skip_next = False
    for item in command:
        if skip_next:
            redacted.append("<prompt>")
            skip_next = False
        elif item == "--prompt":
            redacted.append(item)
            skip_next = True
        else:
            redacted.append(item)
    return {
        "ok": True,
        "status": "dry-run",
        "command": redacted,
        "contract": str(contract_path),
        "app_selection_transport": selection_transport if app_requested else None,
        "app_selection_evidence_required": app_requested,
        "research_selection_evidence_required": research_requested,
        "prompt_transport": prompt_contract["transport"],
        "prompt_file": prompt_contract["prompt_file"],
        "prompt_file_sha256": prompt_contract["prompt_sha256"],
        "inline_task_prompt_exposed": False,
    }


def prepare_browser(executable: str = "agbrowse", manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    values = dict(manifest or {})
    port = int(values.get("cdp_port") or 9222)
    completed = BRIDGE.default_runner(
        [executable, "start", "--headed", "--port", str(port)],
        BRIDGE.bridge_env(values),
        90,
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "agbrowse start failed").strip())
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"ok": True, "status": "started", "output": completed.stdout.strip()}


def execute(manifest_path: Path, contract_path: Path, state_root: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    bridge = BRIDGE.Bridge(state_root=state_root)
    retry_limit = int(
        manifest.get("provider_failure_retry_limit")
        if manifest.get("provider_failure_retry_limit") is not None
        else 1
    )
    if not 0 <= retry_limit <= 2:
        raise RuntimeError("provider_failure_retry_limit must be 0..2")
    terminal_failures: list[dict[str, Any]] = []
    for retry_index in range(retry_limit + 1):
        record = bridge.prepare(
            project_root=project_root(manifest, manifest_path),
            manifest_path=str(manifest_path),
            contract_path=str(contract_path),
        )
        run_dir = str(record["run_dir"])
        record = bridge.send(run_dir)
        if record.get("phase") in {"SUBMITTED", "URL_BOUND", "RESPONSE_IN_PROGRESS"}:
            record = bridge.poll(run_dir)
        if record.get("phase") == "COMPLETE":
            try:
                cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=False)
            except Exception as exc:
                cleanup = {
                    "ok": False,
                    "state": "cleanup-pending",
                    "error_code": str(getattr(exc, "code", type(exc).__name__)),
                }
                bridge.store.record_terminal_cleanup(run_dir, cleanup)
            _, record = bridge.store.load(run_dir)
            return {
                "ok": True,
                "run_dir": run_dir,
                "result": record,
                "cleanup": cleanup,
                "provider_terminal_failures": terminal_failures,
            }
        if record.get("phase") != "PROVIDER_FAILED_TERMINAL":
            return {
                "ok": False,
                "run_dir": run_dir,
                "result": record,
                "provider_terminal_failures": terminal_failures,
            }
        cleanup = bridge.cleanup_completed(run_dir, explicit_user_request=False)
        _, record = bridge.store.load(run_dir)
        terminal_failures.append(
            {
                "retry_index": retry_index,
                "run_id": record.get("run_id"),
                "conversation_url": record.get("conversation_url"),
                "terminal_block_code": record.get("terminal_block_code"),
                "cleanup_state": cleanup.get("state"),
            }
        )
        if retry_index >= retry_limit:
            return {
                "ok": False,
                "run_dir": run_dir,
                "result": record,
                "provider_terminal_failures": terminal_failures,
            }
    raise RuntimeError("provider terminal retry loop exhausted")


def _json_stdout(completed: subprocess.CompletedProcess[str]) -> Any:
    text = (completed.stdout or "").strip()
    if not text:
        return None
    return json.loads(text)


def observe_exact_run(run_dir: str, state_root: Path | None = None) -> dict[str, Any]:
    """Read one persisted run without activating, navigating, stopping, or closing a tab."""
    bridge = BRIDGE.Bridge(state_root=state_root)
    state_file, record = bridge.store.load(run_dir)
    manifest_path = Path(str(record.get("manifest_path") or "")).expanduser()
    manifest = load_manifest(manifest_path) if manifest_path.is_file() else {}
    contract_path = Path(str(manifest.get("agbrowse_contract") or DEFAULT_CONTRACT)).expanduser()
    contract = BRIDGE.read_contract(contract_path)
    executable = BRIDGE.contract_executable(contract)
    env = BRIDGE.bridge_env(manifest)

    expected = {
        "project_root": record.get("project_root"),
        "project_key": record.get("project_key"),
        "run_id": record.get("run_id"),
        "session_id": record.get("session_id"),
        "target_id": record.get("current_target_id") or record.get("target_id"),
        "canonical_url": record.get("conversation_url"),
    }
    missing = [key for key, value in expected.items() if not str(value or "").strip()]

    session_payload: Any = None
    session_error: str | None = None
    if expected["session_id"]:
        completed = BRIDGE.default_runner(
            [executable, "web-ai", "sessions", "show", str(expected["session_id"]), "--json"],
            env,
            30,
        )
        try:
            session_payload = _json_stdout(completed)
        except json.JSONDecodeError:
            session_error = "SESSION_JSON_INVALID"
        if completed.returncode != 0 and session_error is None:
            session_error = "SESSION_SHOW_FAILED"
    session = (
        session_payload.get("session")
        if isinstance(session_payload, dict) and isinstance(session_payload.get("session"), dict)
        else session_payload
    )
    session = session if isinstance(session, dict) else {}

    tabs_completed = BRIDGE.default_runner([executable, "tabs", "--json"], env, 30)
    tabs_payload = _json_stdout(tabs_completed) if tabs_completed.returncode == 0 else None
    tabs = tabs_payload.get("tabs") if isinstance(tabs_payload, dict) else tabs_payload
    tabs = tabs if isinstance(tabs, list) else []

    target_matches = [
        tab for tab in tabs
        if isinstance(tab, dict)
        and str(tab.get("targetId") or tab.get("target_id") or "") == str(expected["target_id"] or "")
    ]
    url_matches = [
        tab for tab in tabs
        if isinstance(tab, dict)
        and str(tab.get("url") or "") == str(expected["canonical_url"] or "")
    ]
    exact_tab = target_matches[0] if len(target_matches) == 1 else (
        url_matches[0] if not target_matches and len(url_matches) == 1 else {}
    )
    active_command = exact_tab.get("activeCommand") if isinstance(exact_tab, dict) else {}
    active_command = active_command if isinstance(active_command, dict) else {}
    active_session_id = str(active_command.get("sessionId") or active_command.get("session_id") or "")
    observed_session_id = str(session.get("sessionId") or session.get("session_id") or "")
    observed_target_id = str(
        session.get("targetId") or session.get("target_id") or session.get("tabId") or ""
    )
    observed_url = str(
        session.get("conversationUrl") or session.get("conversation_url") or session.get("originalUrl") or ""
    )
    live_target_url = str(exact_tab.get("url") or "") if isinstance(exact_tab, dict) else ""
    live_target_canonical = bool(BRIDGE.STATE.CANONICAL_CHAT_RE.fullmatch(live_target_url))
    session_url_canonical = bool(BRIDGE.STATE.CANONICAL_CHAT_RE.fullmatch(observed_url))
    # agbrowse can persist a temporary /c/WEB:<uuid> locator while the same
    # exact target has already committed a real /c/<id> URL.  The live exact
    # target is stronger identity evidence; a temporary session locator must
    # never turn that into an active-tab mismatch.
    effective_url = live_target_url if live_target_canonical else observed_url
    provider_status = str(session.get("status") or "").casefold()
    active_statuses = {"created", "sent", "polling", "running", "pending", "response_in_progress", "streaming"}
    terminal_statuses = {"complete", "completed", "done", "cancelled", "canceled", "failed"}

    mismatches: list[str] = []
    if observed_session_id and observed_session_id != str(expected["session_id"] or ""):
        mismatches.append("session_id")
    if observed_target_id and expected["target_id"] and observed_target_id != str(expected["target_id"]):
        mismatches.append("target_id")
    if (
        session_url_canonical
        and expected["canonical_url"]
        and observed_url != str(expected["canonical_url"])
        and not (live_target_canonical and live_target_url == str(expected["canonical_url"]))
    ):
        mismatches.append("canonical_url")
    if len(target_matches) > 1 or len(url_matches) > 1:
        mismatches.append("ambiguous_live_target")
    if exact_tab and expected["canonical_url"] and live_target_url != str(expected["canonical_url"]):
        mismatches.append("live_target_url")
    if active_session_id and active_session_id != str(expected["session_id"] or ""):
        mismatches.append("active_command_session")

    if missing:
        state = "IDENTITY_INCOMPLETE"
        next_action = "recover the exact persisted run; do not inspect or submit through another tab"
    elif mismatches:
        state = "IDENTITY_MISMATCH"
        next_action = "run exact-session recovery/history adjudication; do not stop any helper or mutate any tab"
    elif active_session_id == str(expected["session_id"]) or provider_status in active_statuses:
        state = "EXACT_ACTIVE"
        next_action = "continue polling only this recorded run/session; elapsed time or empty text is not terminal evidence"
    elif provider_status in terminal_statuses:
        state = "EXACT_TERMINAL_PENDING_CAPTURE" if record.get("phase") != "COMPLETE" else "EXACT_COMPLETE"
        next_action = "capture and verify this exact run before exact-owned cleanup"
    elif not exact_tab:
        state = "TARGET_ABSENT"
        next_action = "recover only the recorded session and canonical URL; do not adopt the current active tab"
    else:
        state = "EXACT_UNCERTAIN"
        next_action = "preserve the run and poll/recover only its exact identity"

    return {
        "schema": "codex.chatgpt.exact-run-observation/v1",
        "ok": not mismatches and not missing,
        "state_file": str(state_file),
        "state": state,
        "expected_identity": expected,
        "observed": {
            "session_id": observed_session_id or None,
            "target_id": observed_target_id or None,
            "canonical_url": observed_url or None,
            "effective_canonical_url": effective_url or None,
            "session_url_temporary": bool(observed_url and not session_url_canonical),
            "provider_status": provider_status or None,
            "active_command_session_id": active_session_id or None,
            "target_match_count": len(target_matches),
            "url_match_count": len(url_matches),
        },
        "missing_identity_fields": missing,
        "identity_mismatches": sorted(set(mismatches)),
        "session_error": session_error,
        "process_termination_authorized": False,
        "tab_mutation_authorized": False,
        "next_action": next_action,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ChatGPT only through one exact contract-validated, unmodified agbrowse installation.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prepare-session", action="store_true")
    parser.add_argument("--recover-run")
    parser.add_argument("--poll-run")
    parser.add_argument("--show-run")
    parser.add_argument("--observe-run")
    parser.add_argument("--doctor-project-lock")
    parser.add_argument("--reconcile-project-lock")
    parser.add_argument("--abandon-uncertain-run")
    parser.add_argument("--explicit-user-request", action="store_true")
    parser.add_argument("--reason")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args, unknown = build_parser().parse_known_args(list(argv) if argv is not None else None)
    if unknown:
        raise SystemExit(f"unsupported legacy arguments: {' '.join(unknown)}")
    try:
        if args.prepare_session:
            contract = BRIDGE.read_contract(args.contract)
            executable = BRIDGE.contract_executable(contract)
            manifest = load_manifest(args.config or args.manifest) if (args.config or args.manifest) else {}
            result = {"ok": True, "result": prepare_browser(executable, manifest)}
        elif args.doctor_project_lock or args.reconcile_project_lock:
            store = BRIDGE.STATE.RunStore(args.state_root)
            root = args.reconcile_project_lock or args.doctor_project_lock
            record = store.reconcile_project_lock(
                root,
                apply_safe_pre_submission=bool(args.reconcile_project_lock),
            )
            result = {"ok": bool(record.get("ok")), "result": record}
        elif args.abandon_uncertain_run:
            if not args.explicit_user_request:
                raise RuntimeError("--abandon-uncertain-run requires --explicit-user-request")
            if not str(args.reason or "").strip():
                raise RuntimeError("--abandon-uncertain-run requires --reason")
            bridge = BRIDGE.Bridge(state_root=args.state_root)
            record = bridge.abandon_uncertain(
                args.abandon_uncertain_run,
                explicit_user_request=True,
                reason=str(args.reason),
            )
            result = {"ok": record.get("phase") == "ABANDONED_UNCERTAIN", "result": record}
        elif args.observe_run:
            result = {"ok": True, "result": observe_exact_run(args.observe_run, args.state_root)}
        elif args.recover_run or args.poll_run or args.show_run:
            bridge = BRIDGE.Bridge(state_root=args.state_root)
            if args.recover_run:
                record = bridge.recover(args.recover_run)
            elif args.poll_run:
                record = bridge.poll(args.poll_run)
            else:
                _, record = bridge.store.load(args.show_run)
            result = {"ok": True, "result": record}
        else:
            manifest_path = (args.config or args.manifest)
            if not manifest_path:
                raise RuntimeError("--config/--manifest is required")
            manifest_path = manifest_path.expanduser().resolve()
            manifest = load_manifest(manifest_path)
            result = dry_run(manifest, args.contract) if args.dry_run else execute(manifest_path, args.contract, args.state_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 2
    except Exception as exc:
        if isinstance(exc, BRIDGE.BridgeError):
            payload = exc.envelope()
        elif isinstance(exc, BRIDGE.STATE.StateError):
            payload = exc.envelope()
        else:
            payload = {"ok": False, "error": {"code": "AGBROWSE_RUN_FAILED", "message": str(exc)}}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
