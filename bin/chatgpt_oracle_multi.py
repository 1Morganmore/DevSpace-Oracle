from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import uuid
import re
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = "codex.chatgpt.oracle-multi/v1"
RESULT_SCHEMA = "codex.chatgpt.oracle-multi-result/v1"
EXECUTION_SCHEMA = "codex.chatgpt.oracle-multi-execution/v1"
LANE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = _load("chatgpt_oracle_multi_runner", BIN / "chatgpt_oracle_run.py")
STATE = RUNNER.STATE


class MultiError(RuntimeError):
    pass


def _git_common_dir(root: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        **STATE.windows_subprocess_kwargs(),
    )
    if completed.returncode != 0 or not (completed.stdout or "").strip():
        raise MultiError(f"write worktree is not a Git worktree: {root}")
    return Path(completed.stdout.strip()).resolve()


def _json_bytes(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise MultiError("manifest must be a JSON object")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    return _json_bytes(path.read_bytes())


def _expected_sha256(value: Any, label: str) -> str:
    expected = str(value or "")
    if SHA256_RE.fullmatch(expected) is None:
        raise MultiError(f"{label} must be exact lowercase SHA-256")
    return expected


def _verified_bytes(path: Path, expected: str, label: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise MultiError(f"{label} changed after authoring: {path}")
    return raw


def _inside(root: Path, value: Any, *, exists: bool = True) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise MultiError("all paths must be absolute")
    path = path.resolve(strict=exists)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MultiError(f"path outside project: {path}") from exc
    return path


def _parallel_plan(value: dict[str, Any], solver_count: int, concurrency: int) -> dict[str, Any]:
    total_sessions = solver_count + 1
    raw = value.get("parallel_policy")
    if raw is None:
        return {
            "when": "manifest-without-explicit-policy",
            "solver_sessions": solver_count,
            "merger_sessions": 1,
            "total_sessions": total_sessions,
            "max_concurrency": concurrency,
        }
    if not isinstance(raw, dict) or set(raw) != {"when", "max_total_sessions", "max_concurrency"}:
        raise MultiError(
            "parallel_policy must contain exactly when, max_total_sessions, and max_concurrency"
        )
    if raw["when"] != "explicit-user-request":
        raise MultiError("parallel_policy.when must be explicit-user-request")
    if any(
        isinstance(raw[field], bool) or not isinstance(raw[field], int)
        for field in ("max_total_sessions", "max_concurrency")
    ):
        raise MultiError("parallel policy caps must be JSON integers")
    max_total = raw["max_total_sessions"]
    policy_concurrency = raw["max_concurrency"]
    if not 3 <= max_total <= 26 or total_sessions > max_total:
        raise MultiError("parallel policy total session cap exceeded")
    if not 1 <= policy_concurrency <= 5 or concurrency > policy_concurrency:
        raise MultiError("parallel policy concurrency cap exceeded")
    return {
        "when": raw["when"],
        "solver_sessions": solver_count,
        "merger_sessions": 1,
        "total_sessions": total_sessions,
        "max_total_sessions": max_total,
        "max_concurrency": concurrency,
        "policy_max_concurrency": policy_concurrency,
    }


def load_manifest(path: Path, *, expected_manifest_sha256: str | None = None) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    raw = resolved.read_bytes()
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    if expected_manifest_sha256 is not None:
        expected = _expected_sha256(expected_manifest_sha256, "expected manifest SHA-256")
        if manifest_sha256 != expected:
            raise MultiError(f"manifest changed after preflight: {resolved}")
    value = _json_bytes(raw)
    if value.get("schema") != SCHEMA:
        raise MultiError(f"schema must be {SCHEMA}")
    root = Path(str(value.get("project_root") or "")).expanduser().resolve(strict=True)
    output_dir = _inside(root, value.get("output_dir"), exists=False)
    allowed_worktrees = []
    for raw in value.get("allowed_worktree_roots") or []:
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            raise MultiError("allowed worktree roots must be absolute")
        allowed_worktrees.append(candidate.resolve(strict=True))
    solvers = value.get("solvers")
    if not isinstance(solvers, list) or not 2 <= len(solvers) <= 25:
        raise MultiError("solvers must contain 2..25 lanes")
    normalized = []
    seen = set()
    for index, item in enumerate(solvers):
        if not isinstance(item, dict):
            raise MultiError("each solver must be an object")
        lane = str(item.get("id") or f"solver-{index}").strip()
        if LANE_RE.fullmatch(lane) is None or lane in seen:
            raise MultiError("solver ids must be unique")
        seen.add(lane)
        access = str(item.get("access") or "read-only")
        if access not in {"read-only", "worktree-write"}:
            raise MultiError("solver access must be read-only or worktree-write")
        lane_root = Path(str(item.get("project_root") or root)).expanduser().resolve(strict=True)
        if lane_root != root and lane_root not in allowed_worktrees:
            raise MultiError("external worktree root must be explicitly allowed")
        mission_path = _inside(lane_root, item.get("mission_path"))
        mission_sha256 = _expected_sha256(item.get("mission_sha256"), "solver mission_sha256")
        _verified_bytes(mission_path, mission_sha256, "solver mission")
        normalized.append({
            "id": lane,
            "mission_path": mission_path,
            "mission_sha256": mission_sha256,
            "access": access,
            "project_root": lane_root,
        })
    write_roots = [item["project_root"] for item in normalized if item["access"] == "worktree-write"]
    if len(write_roots) != len(set(write_roots)) or any(path == root for path in write_roots):
        raise MultiError("write solvers require distinct pre-created worktree roots")
    if write_roots:
        canonical_common = _git_common_dir(root)
        if any(_git_common_dir(path) != canonical_common for path in write_roots):
            raise MultiError("write solver worktrees must belong to the canonical repository")
    merger = _inside(root, value.get("merger_mission_path"))
    merger_sha256 = _expected_sha256(value.get("merger_mission_sha256"), "merger_mission_sha256")
    _verified_bytes(merger, merger_sha256, "merger mission")
    next_stage_result = (
        _inside(root, value.get("next_stage_result_path"), exists=False)
        if value.get("next_stage_result_path")
        else None
    )
    concurrency = int(value.get("max_concurrency", 5))
    if not 1 <= concurrency <= 5:
        raise MultiError("max_concurrency must be within 1..5")
    app_name = str(value.get("app_name") or "DevSpace").strip()
    if app_name != "DevSpace":
        raise MultiError("app_name must be exactly DevSpace")
    parallel_plan = _parallel_plan(value, len(normalized), concurrency)
    chatgpt_project_url = STATE.normalize_chatgpt_project_url(value.get("chatgpt_project_url"))
    return {
        **value,
        "project_root": root,
        "output_dir": output_dir,
        "solvers": normalized,
        "merger_mission_path": merger,
        "merger_mission_sha256": merger_sha256,
        "next_stage_result_path": next_stage_result,
        "max_concurrency": concurrency,
        "parallel_plan": parallel_plan,
        "app_name": app_name,
        "chatgpt_project_url": chatgpt_project_url,
        "model": str(value.get("model") or "gpt-5.6").strip(),
        "copy_profile": Path(
            str(value.get("copy_profile") or (Path.home() / ".oracle" / "browser-profile"))
        ).expanduser().resolve(),
        "allowed_worktree_roots": allowed_worktrees,
        "manifest_sha256": manifest_sha256,
        "manifest_path": resolved,
        "next_stage_binding": value.get("next_stage_binding") if isinstance(value.get("next_stage_binding"), dict) else {},
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    STATE.write_json_atomic(path, value)


def _publish_result(
    path: Path,
    value: dict[str, Any],
    terminal_seal: Callable[[Path, bytes], None] | None,
) -> None:
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if terminal_seal is not None:
        terminal_seal(path, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _child_manifest(config: dict[str, Any], lane: dict[str, Any], parent_id: str) -> Path:
    lane_root = config["output_dir"] / "lanes" / lane["id"]
    manifest = lane_root / "oracle.json"
    provenance = lane_root / "child-provenance.json"
    run_token = hashlib.sha256(f"{parent_id}:{lane['id']}".encode()).hexdigest()[:12]
    _write_json(provenance, {
        "schema": "codex.chatgpt.oracle-multi-child-provenance/v1",
        "parent_id": parent_id,
        "parent_manifest_path": str(config["manifest_path"]),
        "parent_manifest_sha256": config["manifest_sha256"],
        "project_root": str(lane.get("project_root") or config["project_root"]),
        "lane_id": lane["id"],
        "mission_path": str(lane["mission_path"]),
        "mission_sha256": hashlib.sha256(lane["mission_path"].read_bytes()).hexdigest(),
    })
    _write_json(
        manifest,
        {
            "schema": STATE.SCHEMA,
            "project_root": str(lane.get("project_root") or config["project_root"]),
            "mission_path": str(lane["mission_path"]),
            "mission_sha256": lane["mission_sha256"],
            "app_name": config["app_name"],
            "mode": "browser",
            "model": config["model"],
            "model_strategy": "select",
            "thinking_time": "extra-high",
            "copy_profile": str(config["copy_profile"]),
            "research": "off",
            "archive": "auto",
            "parallel_parent_id": parent_id,
            "run_id": f"multi-{parent_id[:16]}-{lane['id']}-{run_token}",
            **({"chatgpt_project_url": config["chatgpt_project_url"]} if config.get("chatgpt_project_url") else {}),
            **({"bound_inputs": lane["bound_inputs"]} if "bound_inputs" in lane else {}),
            "web_multi_child_provenance_path": str(provenance),
        },
    )
    return manifest


def _prepare_solver_lane(
    config: dict[str, Any],
    lane: dict[str, Any],
    parent_id: str,
) -> dict[str, Any]:
    manifest = _child_manifest(config, lane, parent_id)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
    _verified_bytes(lane["mission_path"], lane["mission_sha256"], "solver mission")
    child_config = STATE.load_manifest(
        manifest,
        expected_manifest_sha256=manifest_sha256,
    )
    if child_config.requested_run_id is None:
        raise MultiError(f"lane {lane['id']} child run identity is missing")
    layout = STATE.create_layout(child_config, run_id=child_config.requested_run_id)
    provenance = manifest.parent / "child-provenance.json"
    return {
        **lane,
        "_child_manifest_path": manifest,
        "_child_manifest_sha256": manifest_sha256,
        "_child_provenance_path": provenance,
        "_child_provenance_sha256": hashlib.sha256(provenance.read_bytes()).hexdigest(),
        "_expected_run_dir": layout.run_dir,
        "_expected_session_locator": layout.slug,
    }


def _run_lane(
    config: dict[str, Any],
    lane: dict[str, Any],
    parent_id: str,
    execute: Callable[..., dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    manifest = lane.get("_child_manifest_path")
    if not isinstance(manifest, Path):
        lane = _prepare_solver_lane(config, lane, parent_id)
        manifest = lane["_child_manifest_path"]
    manifest_sha256 = str(lane["_child_manifest_sha256"])
    _verified_bytes(lane["mission_path"], lane["mission_sha256"], "solver mission")
    result = execute(
        manifest,
        expected_manifest_sha256=manifest_sha256,
        dry_run=dry_run,
    )
    output = None
    output_sha256 = None
    session_locator = None
    if not dry_run and result.get("run_dir"):
        run_dir = Path(str(result["run_dir"]))
        source = run_dir / "output.md"
        state_path = run_dir / "state.json"
        if state_path.is_file():
            state = _read_json(state_path)
            oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
            session_locator = oracle.get("session_locator")
        source_bytes = source.read_bytes() if source.is_file() else b""
        if source_bytes.strip():
            output = config["output_dir"] / "handoffs" / f"{lane['id']}.md"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(source_bytes)
            output_sha256 = hashlib.sha256(source_bytes).hexdigest()
    return {
        "id": lane["id"],
        "ok": bool(result.get("ok")),
        "run_dir": result.get("run_dir") or str(lane["_expected_run_dir"]),
        "output_path": str(output) if output else None,
        "output_sha256": output_sha256,
        "session_locator": session_locator or lane["_expected_session_locator"],
        "child_manifest_path": str(manifest),
        "child_manifest_sha256": manifest_sha256,
        "child_provenance_path": str(lane["_child_provenance_path"]),
        "child_provenance_sha256": lane["_child_provenance_sha256"],
    }


def _merger_transport(
    config: dict[str, Any],
    successful: list[dict[str, Any]],
    parent_id: str,
) -> Path:
    target = config["output_dir"] / "merger" / "mission.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp.{uuid.uuid4().hex}")
    temporary.write_bytes(_merger_transport_bytes(config, successful, parent_id))
    temporary.replace(target)
    return target


def _merger_transport_bytes(
    config: dict[str, Any],
    successful: list[dict[str, Any]],
    parent_id: str,
) -> bytes:
    source = _verified_bytes(
        config["merger_mission_path"], config["merger_mission_sha256"], "merger mission"
    ).decode("utf-8")
    paths = "\n".join(
        f"- path={item['output_path']}\n  sha256={item['output_sha256']}" for item in successful
    )
    receipt_line = (
        "\n[NEXT_STAGE_RECEIPT_BINDING]\n"
        f"workflow_id={config['next_stage_binding'].get('workflow_id', '')}\n"
        f"stage={config['next_stage_binding'].get('stage', '')}\n"
        f"attempt_id={parent_id}\n"
        f"input_mission_sha256={config['manifest_sha256']}\n"
        f"Write the bound next-stage receipt to: {config['next_stage_result_path']}\n"
        if config.get("next_stage_result_path")
        else ""
    )
    return f"{source.rstrip()}\n\n[INPUT_HANDOFFS]\n{paths}\n{receipt_line}".encode("utf-8")


def _load_execution(
    config: dict[str, Any],
    *,
    parent_id: str | None,
) -> tuple[Path, dict[str, Any], str]:
    path = config["output_dir"] / "execution.json"
    value = _read_json(path)
    recorded_parent = _expected_sha256(value.get("parent_id"), "execution parent_id")
    expected_parent = (
        _expected_sha256(parent_id, "parent_id")
        if parent_id is not None
        else recorded_parent
    )
    recorded_manifest = Path(str(value.get("manifest_path") or "")).expanduser()
    if (
        value.get("schema") != EXECUTION_SCHEMA
        or recorded_parent != expected_parent
        or not recorded_manifest.is_absolute()
        or recorded_manifest.resolve() != config["manifest_path"]
        or value.get("manifest_sha256") != config["manifest_sha256"]
    ):
        raise MultiError("parent manifest identity mismatch")
    return path, value, expected_parent


def _reject_existing_terminal_result(config: dict[str, Any]) -> None:
    path = config["output_dir"] / "result.json"
    if not path.exists():
        return
    value = _read_json(path)
    if value.get("schema") == RESULT_SCHEMA and value.get("status") in {
        "complete",
        "partial",
        "failed",
    }:
        raise MultiError("multi result is already terminal-sealed")
    raise MultiError("existing multi result is invalid")


def _is_typed_lane_failure(recorded: dict[str, Any]) -> bool:
    error = recorded.get("error")
    return (
        set(recorded) == {
            "id",
            "ok",
            "run_dir",
            "output_path",
            "output_sha256",
            "session_locator",
            "child_manifest_path",
            "child_manifest_sha256",
            "child_provenance_path",
            "child_provenance_sha256",
            "error",
        }
        and recorded.get("ok") is False
        and recorded.get("output_path") is None
        and recorded.get("output_sha256") is None
        and isinstance(error, dict)
        and set(error) == {"code", "type", "message"}
        and error.get("code") == "ORACLE_MULTI_LANE_EXCEPTION"
        and isinstance(error.get("type"), str)
        and bool(error["type"])
        and isinstance(error.get("message"), str)
    )


def _recovered_lane(
    config: dict[str, Any],
    lane: dict[str, Any],
    recorded: dict[str, Any],
    parent_id: str,
) -> dict[str, Any]:
    lane_id = lane["id"]
    if recorded.get("id") != lane_id:
        raise MultiError(f"lane {lane_id} identity mismatch")
    _verified_bytes(lane["mission_path"], lane["mission_sha256"], "solver mission")
    manifest_path = Path(str(recorded.get("child_manifest_path") or "")).expanduser()
    provenance_path = Path(str(recorded.get("child_provenance_path") or "")).expanduser()
    if (
        not manifest_path.is_absolute()
        or manifest_path.resolve() != config["output_dir"] / "lanes" / lane_id / "oracle.json"
        or not provenance_path.is_absolute()
        or provenance_path.resolve() != manifest_path.parent / "child-provenance.json"
    ):
        raise MultiError(f"lane {lane_id} child manifest identity mismatch")
    manifest_sha256 = _expected_sha256(
        recorded.get("child_manifest_sha256"),
        f"lane {lane_id} child manifest SHA-256",
    )
    provenance_sha256 = _expected_sha256(
        recorded.get("child_provenance_sha256"),
        f"lane {lane_id} child provenance SHA-256",
    )
    try:
        manifest_bytes = _verified_bytes(
            manifest_path.resolve(strict=True),
            manifest_sha256,
            "child manifest",
        )
        provenance_bytes = _verified_bytes(
            provenance_path.resolve(strict=True),
            provenance_sha256,
            "child provenance",
        )
        child = _json_bytes(manifest_bytes)
        provenance = _json_bytes(provenance_bytes)
    except OSError as exc:
        raise MultiError(f"lane {lane_id} child identity is unavailable") from exc
    try:
        child_config = STATE.load_manifest(
            manifest_path,
            expected_manifest_sha256=manifest_sha256,
        )
    except STATE.OracleStateError as exc:
        raise MultiError(f"lane {lane_id} child manifest identity mismatch") from exc
    run_id = str(child.get("run_id") or "")
    if any((
        child.get("schema") != STATE.SCHEMA,
        child.get("project_root") != str(lane["project_root"]),
        child.get("mission_path") != str(lane["mission_path"]),
        child.get("mission_sha256") != lane["mission_sha256"],
        child.get("parallel_parent_id") != parent_id,
        child.get("web_multi_child_provenance_path") != str(provenance_path),
        not run_id,
    )):
        raise MultiError(f"lane {lane_id} child manifest identity mismatch")
    if any((
        provenance.get("schema") != "codex.chatgpt.oracle-multi-child-provenance/v1",
        provenance.get("parent_id") != parent_id,
        provenance.get("parent_manifest_path") != str(config["manifest_path"]),
        provenance.get("parent_manifest_sha256") != config["manifest_sha256"],
        provenance.get("project_root") != str(lane["project_root"]),
        provenance.get("lane_id") != lane_id,
        provenance.get("mission_path") != str(lane["mission_path"]),
        provenance.get("mission_sha256") != lane["mission_sha256"],
    )):
        raise MultiError(f"lane {lane_id} child provenance identity mismatch")
    if child_config.requested_run_id != run_id:
        raise MultiError(f"lane {lane_id} child run identity mismatch")
    expected_layout = STATE.create_layout(child_config, run_id=run_id)
    run_dir = Path(str(recorded.get("run_dir") or "")).expanduser()
    if (
        not run_dir.is_absolute()
        or run_dir.resolve() != expected_layout.run_dir
        or not STATE.is_within(STATE.oracle_state_root(), run_dir.resolve())
    ):
        raise MultiError(f"lane {lane_id} run directory identity mismatch")
    typed_failure = "error" in recorded
    if typed_failure:
        if not _is_typed_lane_failure(recorded):
            raise MultiError(f"lane {lane_id} is not a typed settled failure")
        if str(recorded.get("session_locator") or "").strip() != expected_layout.slug:
            raise MultiError(f"lane {lane_id} session identity mismatch")
    if not run_dir.exists():
        if typed_failure and not run_dir.is_symlink():
            return {
                **recorded,
                "run_dir": str(run_dir.resolve()),
                "session_locator": expected_layout.slug,
            }
        raise MultiError(f"lane {lane_id} exact run state is unavailable")
    if not run_dir.is_dir():
        raise MultiError(f"lane {lane_id} exact run state is unavailable")
    state_path = run_dir.resolve() / "state.json"
    try:
        state = _read_json(state_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MultiError(f"lane {lane_id} exact run state is unavailable") from exc
    state_manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else {}
    state_mission = state.get("mission") if isinstance(state.get("mission"), dict) else {}
    state_provenance = (
        state.get("web_multi_child_provenance")
        if isinstance(state.get("web_multi_child_provenance"), dict)
        else {}
    )
    oracle = state.get("oracle") if isinstance(state.get("oracle"), dict) else {}
    exact_locator = str(oracle.get("session_locator") or "").strip()
    if state.get("parallel_parent_id") != parent_id:
        raise MultiError(f"lane {lane_id} parent identity mismatch")
    if state.get("run_id") != run_id or state.get("requested_run_id") != run_id:
        raise MultiError(f"lane {lane_id} run directory identity mismatch")
    if any((
        state.get("schema") != STATE.STATE_SCHEMA,
        state.get("project_root") != str(lane["project_root"]),
        state_manifest.get("path") != str(manifest_path),
        state_manifest.get("actual_sha256") != manifest_sha256,
        state_manifest.get("expected_sha256") != manifest_sha256,
        state_mission.get("path") != str(lane["mission_path"]),
        state_mission.get("sha256") != lane["mission_sha256"],
        state_provenance.get("path") != str(provenance_path),
        state_provenance.get("sha256") != provenance_sha256,
    )):
        raise MultiError(f"lane {lane_id} exact run identity mismatch")
    if (
        exact_locator != expected_layout.slug
        or exact_locator != str(recorded.get("session_locator") or "").strip()
    ):
        raise MultiError(f"lane {lane_id} session identity mismatch")
    try:
        pre_submit_failure = STATE.proven_pre_submit_failure(state_path)
    except (OSError, STATE.OracleStateError) as exc:
        raise MultiError(f"lane {lane_id} exact run state is unavailable") from exc
    if pre_submit_failure is not None:
        if typed_failure:
            return {
                **recorded,
                "run_dir": str(run_dir.resolve()),
                "session_locator": exact_locator,
            }
        if (
            set(recorded) != {
                "id",
                "ok",
                "run_dir",
                "output_path",
                "output_sha256",
                "session_locator",
                "child_manifest_path",
                "child_manifest_sha256",
                "child_provenance_path",
                "child_provenance_sha256",
            }
            or recorded.get("ok") is not False
            or recorded.get("output_path") is not None
            or recorded.get("output_sha256") is not None
        ):
            raise MultiError(f"lane {lane_id} is not a settled pre-submit failure")
        proof_code = str(pre_submit_failure.get("code") or "").strip()
        if not proof_code:
            raise MultiError(f"lane {lane_id} pre-submit failure proof is invalid")
        return {
            **recorded,
            "run_dir": str(run_dir.resolve()),
            "session_locator": exact_locator,
            "error": {
                "code": "ORACLE_MULTI_LANE_EXCEPTION",
                "type": "OraclePreSubmitFailure",
                "message": proof_code,
            },
        }
    if (
        state.get("status") != "complete"
        or state.get("session_authority") != "terminal"
        or state.get("terminal_harvested") is not True
    ):
        raise MultiError(f"lane {lane_id} lacks terminal authority")
    output_path = run_dir.resolve() / "output.md"
    artifacts = state.get("artifacts") if isinstance(state.get("artifacts"), dict) else {}
    if Path(str(artifacts.get("output") or "")).resolve() != output_path:
        raise MultiError(f"lane {lane_id} durable output identity mismatch")
    output_bytes = output_path.read_bytes() if output_path.is_file() else b""
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    if not output_bytes.strip() or state.get("artifact_sha256") != output_sha256:
        raise MultiError(f"lane {lane_id} durable output hash mismatch")
    handoff = config["output_dir"] / "handoffs" / f"{lane_id}.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    temporary = handoff.with_name(f"{handoff.name}.tmp.{uuid.uuid4().hex}")
    temporary.write_bytes(output_bytes)
    temporary.replace(handoff)
    if hashlib.sha256(handoff.read_bytes()).hexdigest() != output_sha256:
        raise MultiError(f"lane {lane_id} copied handoff hash mismatch")
    return {
        **{key: value for key, value in recorded.items() if key != "error"},
        "id": lane_id,
        "ok": True,
        "run_dir": str(run_dir.resolve()),
        "output_path": str(handoff),
        "output_sha256": output_sha256,
        "session_locator": exact_locator,
    }


def reconcile_recovered_lanes(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    parent_id: str | None = None,
    parent_lock_held: bool = False,
) -> dict[str, Any]:
    config = load_manifest(
        manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    lock = (
        nullcontext()
        if parent_lock_held
        else STATE.project_submit_mutex(config["project_root"], timeout_seconds=30)
    )
    with lock:
        _reject_existing_terminal_result(config)
        execution_path, execution, expected_parent = _load_execution(
            config,
            parent_id=parent_id,
        )
        if execution.get("status") not in {"running", "lanes_settled"}:
            raise MultiError("execution ledger is not recoverable")
        recorded = execution.get("lanes")
        if not isinstance(recorded, list) or not all(isinstance(item, dict) for item in recorded):
            raise MultiError("execution lane ledger is invalid")
        by_id = {str(item.get("id") or ""): item for item in recorded}
        expected_ids = [lane["id"] for lane in config["solvers"]]
        if len(by_id) != len(recorded) or set(by_id) != set(expected_ids):
            raise MultiError("execution lane ledger does not match the manifest")
        lanes = [
            _recovered_lane(config, lane, by_id[lane["id"]], expected_parent)
            for lane in config["solvers"]
        ]
        successful = [lane for lane in lanes if lane["ok"]]
        if not successful:
            raise MultiError("no successful recovered lanes")
        merger_mission = _merger_transport(config, successful, expected_parent)
        merger_mission_sha256 = hashlib.sha256(merger_mission.read_bytes()).hexdigest()
        bound_inputs = [
            {"path": lane["output_path"], "sha256": lane["output_sha256"]}
            for lane in successful
        ]
        updated = {
            **execution,
            "status": "merger_ready",
            "lanes": lanes,
            "successful_lane_count": len(successful),
            "merger_mission_path": str(merger_mission),
            "merger_mission_sha256": merger_mission_sha256,
            "bound_inputs": bound_inputs,
        }
        _write_json(execution_path, updated)
        return {"ok": True, **updated}


def resume_recovered_merger(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    parent_id: str | None = None,
    execute: Callable[..., dict[str, Any]] = RUNNER.execute_run,
    parent_lock_held: bool = False,
    terminal_seal: Callable[[Path, bytes], None] | None = None,
) -> dict[str, Any]:
    config = load_manifest(
        manifest_path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    lock = (
        nullcontext()
        if parent_lock_held
        else STATE.project_submit_mutex(config["project_root"], timeout_seconds=30)
    )
    with lock:
        _reject_existing_terminal_result(config)
        execution_path, execution, expected_parent = _load_execution(
            config,
            parent_id=parent_id,
        )
        if execution.get("status") != "merger_ready":
            raise MultiError("execution ledger is not ready for merger-only resume")
        lanes = execution.get("lanes")
        if not isinstance(lanes, list) or not all(isinstance(item, dict) for item in lanes):
            raise MultiError("merger-ready lane ledger is invalid")
        expected_ids = [lane["id"] for lane in config["solvers"]]
        if [str(lane.get("id") or "") for lane in lanes] != expected_ids:
            raise MultiError("merger-ready lane order does not match the manifest")
        successful = [lane for lane in lanes if lane.get("ok") is True]
        failed = [lane for lane in lanes if lane.get("ok") is False]
        if (
            len(successful) + len(failed) != len(lanes)
            or any("error" in lane for lane in successful)
            or any(not _is_typed_lane_failure(lane) for lane in failed)
            or execution.get("successful_lane_count") != len(successful)
            or not successful
        ):
            raise MultiError("merger-ready lane settlement is invalid")
        for source in config["solvers"]:
            _verified_bytes(source["mission_path"], source["mission_sha256"], "solver mission")
        bound_inputs = [
            {"path": lane.get("output_path"), "sha256": lane.get("output_sha256")}
            for lane in successful
        ]
        if execution.get("bound_inputs") != bound_inputs:
            raise MultiError("merger bound input identity mismatch")
        for item in bound_inputs:
            path = _inside(config["project_root"], item["path"])
            _verified_bytes(path, _expected_sha256(item["sha256"], "handoff SHA-256"), "solver handoff")
        merger_mission = Path(str(execution.get("merger_mission_path") or "")).expanduser()
        expected_mission = config["output_dir"] / "merger" / "mission.md"
        if not merger_mission.is_absolute() or merger_mission.resolve() != expected_mission:
            raise MultiError("generated merger mission identity mismatch")
        mission_bytes = _verified_bytes(
            merger_mission.resolve(strict=True),
            _expected_sha256(
                execution.get("merger_mission_sha256"),
                "generated merger mission SHA-256",
            ),
            "generated merger mission",
        )
        if mission_bytes != _merger_transport_bytes(config, successful, expected_parent):
            raise MultiError("generated merger mission content mismatch")
        merger_lane = {
            "id": "merger",
            "mission_path": merger_mission,
            "mission_sha256": hashlib.sha256(mission_bytes).hexdigest(),
            "bound_inputs": bound_inputs,
        }
        merger_manifest = _child_manifest(config, merger_lane, expected_parent)
        merger_manifest_sha256 = hashlib.sha256(merger_manifest.read_bytes()).hexdigest()
        merger_config = STATE.load_manifest(
            merger_manifest,
            expected_manifest_sha256=merger_manifest_sha256,
        )
        if merger_config.requested_run_id is None:
            raise MultiError("merger child run identity is missing")
        merger_layout = STATE.create_layout(
            merger_config,
            run_id=merger_config.requested_run_id,
        )
        if merger_layout.run_dir.exists():
            raise MultiError("exact merger run already exists; refusing replacement submission")
        _verified_bytes(
            config["merger_mission_path"],
            config["merger_mission_sha256"],
            "merger mission",
        )
        if merger_mission.read_bytes() != mission_bytes:
            raise MultiError("generated merger mission changed before submission")
        for item in bound_inputs:
            _verified_bytes(Path(item["path"]), item["sha256"], "solver handoff")
        _write_json(execution_path, {
            **execution,
            "status": "merger_submitting",
            "merger_manifest_path": str(merger_manifest),
            "merger_manifest_sha256": merger_manifest_sha256,
        })
        merger = execute(
            merger_manifest,
            expected_manifest_sha256=merger_manifest_sha256,
            dry_run=False,
        )
        status = "complete" if merger.get("ok") and not failed else (
            "partial" if merger.get("ok") else "failed"
        )
        result = {
            "schema": RESULT_SCHEMA,
            "status": status,
            "parent_id": expected_parent,
            "manifest_sha256": config["manifest_sha256"],
            "parallel_plan": config["parallel_plan"],
            "lanes": lanes,
            "merger_run_dir": merger.get("run_dir"),
            "successful_lane_count": len(successful),
            "next_stage_result_path": (
                str(config["next_stage_result_path"])
                if config.get("next_stage_result_path")
                and config["next_stage_result_path"].is_file()
                else None
            ),
        }
        _publish_result(config["output_dir"] / "result.json", result, terminal_seal)
        return {"ok": status == "complete", **result}


def run_multi(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str | None = None,
    parent_id: str | None = None,
    dry_run: bool = False,
    execute: Callable[..., dict[str, Any]] = RUNNER.execute_run,
    parent_lock_held: bool = False,
    terminal_seal: Callable[[Path, bytes], None] | None = None,
) -> dict[str, Any]:
    config = load_manifest(manifest_path, expected_manifest_sha256=expected_manifest_sha256)
    parent_id = (
        _expected_sha256(parent_id, "parent_id")
        if parent_id is not None
        else hashlib.sha256(f"{config['project_root']}:{uuid.uuid4().hex}".encode()).hexdigest()
    )
    config["output_dir"].mkdir(parents=True, exist_ok=True)
    lanes: list[dict[str, Any]] = []
    # The parent owns normal same-project exclusion. Children use the separate
    # parent-scoped launch mutex and may wait concurrently after submission.
    lock = nullcontext() if parent_lock_held else STATE.project_submit_mutex(config["project_root"], timeout_seconds=30)
    execution_path = config["output_dir"] / "execution.json"
    lane_ledger: dict[str, dict[str, Any]] = {}
    with lock:
        if not dry_run:
            _reject_existing_terminal_result(config)
            if execution_path.exists():
                raise MultiError("existing multi execution requires exact recovery")
        solvers = [
            _prepare_solver_lane(config, lane, parent_id)
            for lane in config["solvers"]
        ]
        if not dry_run:
            lane_ledger = {
                lane["id"]: {
                    "id": lane["id"],
                    "ok": False,
                    "run_dir": str(lane["_expected_run_dir"]),
                    "output_path": None,
                    "output_sha256": None,
                    "session_locator": lane["_expected_session_locator"],
                    "child_manifest_path": str(lane["_child_manifest_path"]),
                    "child_manifest_sha256": lane["_child_manifest_sha256"],
                    "child_provenance_path": str(lane["_child_provenance_path"]),
                    "child_provenance_sha256": lane["_child_provenance_sha256"],
                }
                for lane in solvers
            }
            _write_json(execution_path, {
                "schema": EXECUTION_SCHEMA,
                "status": "running",
                "parent_id": parent_id,
                "manifest_path": str(config["manifest_path"]),
                "manifest_sha256": config["manifest_sha256"],
                "lanes": [lane_ledger[lane["id"]] for lane in solvers],
            })
        for start in range(0, len(solvers), config["max_concurrency"]):
            wave = solvers[start : start + config["max_concurrency"]]
            with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="oracle-multi") as pool:
                futures = {
                    pool.submit(_run_lane, config, lane, parent_id, execute, dry_run): lane
                    for lane in wave
                }
                for future in as_completed(futures):
                    lane = futures[future]
                    try:
                        settled = future.result()
                    except Exception as exc:
                        settled = {
                            **(lane_ledger.get(lane["id"]) or {}),
                            "id": lane["id"],
                            "ok": False,
                            "output_path": None,
                            "output_sha256": None,
                            "error": {
                                "code": "ORACLE_MULTI_LANE_EXCEPTION",
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        }
                    lanes.append(settled)
                    if not dry_run:
                        lane_ledger[lane["id"]] = settled
                        _write_json(execution_path, {
                            "schema": EXECUTION_SCHEMA,
                            "status": "running",
                            "parent_id": parent_id,
                            "manifest_path": str(config["manifest_path"]),
                            "manifest_sha256": config["manifest_sha256"],
                            "lanes": [lane_ledger[item["id"]] for item in solvers],
                        })
        order = {item["id"]: index for index, item in enumerate(config["solvers"])}
        lanes.sort(key=lambda item: order[item["id"]])
        if not dry_run:
            _write_json(execution_path, {
                "schema": EXECUTION_SCHEMA,
                "status": "lanes_settled",
                "parent_id": parent_id,
                "manifest_path": str(config["manifest_path"]),
                "manifest_sha256": config["manifest_sha256"],
                "lanes": lanes,
            })
        successful = [item for item in lanes if item["ok"] and (dry_run or item["output_path"])]
        if not successful:
            result = {
                "schema": RESULT_SCHEMA,
                "status": "failed",
                "parent_id": parent_id,
                "manifest_sha256": config["manifest_sha256"],
                "parallel_plan": config["parallel_plan"],
                "lanes": lanes,
            }
            if not dry_run:
                _write_json(execution_path, {
                    "schema": EXECUTION_SCHEMA,
                    "status": "terminal_pending_publication",
                    "parent_id": parent_id,
                    "manifest_path": str(config["manifest_path"]),
                    "manifest_sha256": config["manifest_sha256"],
                    "lanes": lanes,
                })
            _publish_result(config["output_dir"] / "result.json", result, terminal_seal)
            if not dry_run:
                _write_json(execution_path, {
                    "schema": EXECUTION_SCHEMA,
                    "status": "terminal_published",
                    "parent_id": parent_id,
                    "manifest_path": str(config["manifest_path"]),
                    "manifest_sha256": config["manifest_sha256"],
                    "lanes": lanes,
                })
            return {"ok": False, **result}
        merger_mission = _merger_transport(config, successful, parent_id) if not dry_run else config["merger_mission_path"]
        merger_lane = {
            "id": "merger",
            "mission_path": merger_mission,
            "mission_sha256": hashlib.sha256(merger_mission.read_bytes()).hexdigest(),
        }
        if not dry_run:
            merger_lane["bound_inputs"] = [
                {"path": item["output_path"], "sha256": item["output_sha256"]}
                for item in successful
            ]
        merger_manifest = _child_manifest(config, merger_lane, parent_id)
        merger_manifest_sha256 = hashlib.sha256(merger_manifest.read_bytes()).hexdigest()
        _verified_bytes(
            config["merger_mission_path"], config["merger_mission_sha256"], "merger mission"
        )
        if not dry_run:
            for item in successful:
                _verified_bytes(
                    Path(item["output_path"]), item["output_sha256"], "solver handoff"
                )
            _write_json(execution_path, {
                "schema": EXECUTION_SCHEMA,
                "status": "merger_submitting",
                "parent_id": parent_id,
                "manifest_path": str(config["manifest_path"]),
                "manifest_sha256": config["manifest_sha256"],
                "lanes": lanes,
                "merger_mission_path": str(merger_mission),
                "merger_mission_sha256": merger_lane["mission_sha256"],
                "bound_inputs": merger_lane["bound_inputs"],
                "merger_manifest_path": str(merger_manifest),
                "merger_manifest_sha256": merger_manifest_sha256,
            })
        merger = execute(
            merger_manifest,
            expected_manifest_sha256=merger_manifest_sha256,
            dry_run=dry_run,
        )
    status = "complete" if merger.get("ok") and len(successful) == len(lanes) else (
        "partial" if merger.get("ok") else "failed"
    )
    result = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "parent_id": parent_id,
        "manifest_sha256": config["manifest_sha256"],
        "parallel_plan": config["parallel_plan"],
        "lanes": lanes,
        "merger_run_dir": merger.get("run_dir"),
        "successful_lane_count": len(successful),
        "next_stage_result_path": (
            str(config["next_stage_result_path"])
            if config.get("next_stage_result_path") and config["next_stage_result_path"].is_file()
            else None
        ),
    }
    _publish_result(config["output_dir"] / "result.json", result, terminal_seal)
    if not dry_run:
        execution = _read_json(execution_path)
        _write_json(execution_path, {
            **execution,
            "status": "terminal_published",
            "merger_run_dir": merger.get("run_dir"),
        })
    return {"ok": status == "complete", **result}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run independent Oracle browser sessions in waves and merge handoffs.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reconcile-recovered", action="store_true")
    parser.add_argument("--resume-merger", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.reconcile_recovered and args.resume_merger:
            raise MultiError("choose exactly one recovery action")
        if args.dry_run and (args.reconcile_recovered or args.resume_merger):
            raise MultiError("recovery actions cannot be combined with --dry-run")
        if not args.dry_run and args.expected_manifest_sha256 is None:
            raise MultiError(
                "MANIFEST_SHA256_REQUIRED: live Multi runs require "
                "--expected-manifest-sha256 from the exact dry-run preview"
            )
        if args.reconcile_recovered:
            result = reconcile_recovered_lanes(
                args.manifest,
                expected_manifest_sha256=args.expected_manifest_sha256,
            )
        elif args.resume_merger:
            raise MultiError(
                "merger-only resume requires the comprehensive terminal-seal callback"
            )
        else:
            result = run_multi(
                args.manifest,
                expected_manifest_sha256=args.expected_manifest_sha256,
                dry_run=args.dry_run,
            )
    except Exception as exc:
        result = {"ok": False, "error": {"code": "ORACLE_MULTI_FAILED", "message": str(exc)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
