from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

STATE_PATH = Path(__file__).resolve().with_name("chatgpt_oracle_state.py")


def load_state_module():
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_state_runtime", STATE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Oracle state module unavailable: {STATE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STATE = load_state_module()


class OracleRunError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


def build_oracle_argv(config, layout, prompt: str) -> list[str]:
    command = [
        *config.oracle_command,
        "--engine", "browser",
        "--browser-model-strategy", "current",
        *config.oracle_args,
        "--slug", layout.slug,
        "--prompt", prompt,
        "--write-output", str(layout.output_path),
    ]
    if any(item == "--file" or item.startswith("--file=") or item == "-f" for item in command):
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
        "output_path": str(layout.output_path),
        "transcript_path": str(layout.transcript_path),
        "stdout_path": str(layout.stdout_path),
        "stderr_path": str(layout.stderr_path),
        "contains_file_flag": False,
    }


def append_error(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write((message.rstrip() + "\n").encode("utf-8", errors="replace"))


def execute_run(
    manifest_path: Path,
    *,
    dry_run: bool = False,
    run_factory: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    platform_name: str | None = None,
) -> dict[str, Any]:
    config = STATE.load_manifest(manifest_path, platform_name=platform_name)
    layout = STATE.create_layout(config)
    transport_mission_path = layout.run_dir / "mission.md"
    prompt = STATE.composer_prompt(config, transport_mission_path)
    argv = build_oracle_argv(config, layout, prompt)
    if dry_run:
        return dry_run_payload(config, layout, argv, prompt)

    mission_bytes = config.mission_path.read_bytes()
    actual_mission_sha256 = hashlib.sha256(mission_bytes).hexdigest()
    if actual_mission_sha256 != config.mission_sha256:
        raise OracleRunError(
            "MISSION_CHANGED_BEFORE_PREPARE",
            "mission bytes changed after manifest validation",
            {"expected": config.mission_sha256, "actual": actual_mission_sha256},
        )
    layout.run_dir.mkdir(parents=True, exist_ok=False)
    transport_mission_path.write_bytes(mission_bytes)
    STATE.write_json_atomic(layout.state_path, STATE.state_payload(config, layout, status="prepared", resolved_version="unresolved"))
    layout.stdout_path.touch()
    layout.stderr_path.touch()
    try:
        version = resolve_oracle_version(config.oracle_command, run_factory=run_factory, platform_name=platform_name)
        STATE.update_state(layout.state_path, status="prepared", resolved_version=version)
    except Exception as exc:
        append_error(layout.stderr_path, f"version resolution failed: {exc}")
        STATE.write_transcript(layout)
        return {"ok": False, "run_dir": str(layout.run_dir), "result": STATE.update_state(layout.state_path, status="failed")}

    try:
        with layout.stdout_path.open("wb") as stdout_handle, layout.stderr_path.open("wb") as stderr_handle:
            with STATE.project_submit_mutex(config.project_root, timeout_seconds=config.submit_mutex_timeout_seconds, platform_name=platform_name):
                current_mission_sha256 = STATE.sha256_file(transport_mission_path)
                if current_mission_sha256 != config.mission_sha256:
                    raise OracleRunError(
                        "MISSION_CHANGED_BEFORE_SUBMIT",
                        "mission bytes changed after manifest validation",
                        {"expected": config.mission_sha256, "actual": current_mission_sha256},
                    )
                process = popen_factory(
                    argv,
                    cwd=str(config.project_root),
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **STATE.windows_subprocess_kwargs(platform_name=platform_name),
                )
                STATE.update_state(layout.state_path, status="running", resolved_version=version)
                exit_code = int(process.wait())
    except Exception as exc:
        append_error(layout.stderr_path, f"Oracle launch/run failed: {exc}")
        STATE.write_transcript(layout)
        return {"ok": False, "run_dir": str(layout.run_dir), "result": STATE.update_state(layout.state_path, status="failed")}

    STATE.write_transcript(layout)
    status = "complete" if exit_code == 0 and STATE.output_is_nonempty(layout.output_path) else ("attention_required" if exit_code == 0 else "failed")
    state = STATE.update_state(layout.state_path, status=status, exit_code=exit_code)
    return {"ok": status == "complete", "run_dir": str(layout.run_dir), "result": state}


def recovery_argv(command: Sequence[str], locator: str, action: str, output_path: Path) -> list[str]:
    if action not in {"harvest", "live"}:
        raise OracleRunError("RECOVERY_ACTION_INVALID", "recovery action must be harvest or live")
    argv = [*command, "session", locator, f"--{action}", "--write-output", str(output_path)]
    if "restart" in argv or "--prompt" in argv or "-p" in argv:
        raise OracleRunError("RECOVERY_COMMAND_UNSAFE", "recovery must not restart or submit a new prompt")
    return argv


def recover_run(
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
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    locator = str(oracle.get("session_locator") or oracle.get("slug") or "").strip()
    if not locator:
        raise OracleRunError("SESSION_LOCATOR_MISSING", "run state has no Oracle session locator")
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    output_path = Path(str(artifacts.get("output") or (directory / "output.md"))).expanduser().resolve()
    argv = recovery_argv(tuple(oracle_command or STATE.default_oracle_command(platform_name)), locator, action, output_path)
    if dry_run:
        return {"ok": True, "status": "dry-run", "run_dir": str(directory), "action": action, "argv": STATE.command_for_display(argv)}
    stdout_path = directory / f"recovery-{action}-stdout.log"
    stderr_path = directory / f"recovery-{action}-stderr.log"
    with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
        process = popen_factory(
            argv,
            cwd=str(state["project_root"]),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            shell=False,
            **STATE.windows_subprocess_kwargs(platform_name=platform_name),
        )
        exit_code = int(process.wait())
    layout = STATE.RunLayout(
        str(state["run_id"]),
        str(oracle.get("slug") or locator),
        directory,
        directory / "state.json",
        output_path,
        Path(str(artifacts.get("transcript") or (directory / "transcript.md"))),
        Path(str(artifacts.get("stdout") or (directory / "stdout.log"))),
        Path(str(artifacts.get("stderr") or (directory / "stderr.log"))),
    )
    STATE.write_transcript(layout)
    status = "complete" if exit_code == 0 and STATE.output_is_nonempty(output_path) else ("attention_required" if exit_code == 0 else "failed")
    updated = STATE.update_state(layout.state_path, status=status, exit_code=exit_code)
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = execute_run(args.manifest, dry_run=args.dry_run) if args.command == "run" else recover_run(
            args.run_dir,
            action=args.action,
            dry_run=args.dry_run,
            oracle_command=args.oracle_command,
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
