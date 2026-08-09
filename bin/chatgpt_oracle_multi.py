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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


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
            **({"chatgpt_project_url": config["chatgpt_project_url"]} if config.get("chatgpt_project_url") else {}),
            **({"bound_inputs": lane["bound_inputs"]} if "bound_inputs" in lane else {}),
            "web_multi_child_provenance_path": str(provenance),
        },
    )
    return manifest


def _run_lane(
    config: dict[str, Any],
    lane: dict[str, Any],
    parent_id: str,
    execute: Callable[..., dict[str, Any]],
    dry_run: bool,
) -> dict[str, Any]:
    manifest = _child_manifest(config, lane, parent_id)
    manifest_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
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
        "run_dir": result.get("run_dir"),
        "output_path": str(output) if output else None,
        "output_sha256": output_sha256,
        "session_locator": session_locator,
    }


def _merger_transport(
    config: dict[str, Any],
    successful: list[dict[str, Any]],
    parent_id: str,
) -> Path:
    source = _verified_bytes(
        config["merger_mission_path"], config["merger_mission_sha256"], "merger mission"
    ).decode("utf-8")
    paths = "\n".join(
        f"- path={item['output_path']}\n  sha256={item['output_sha256']}" for item in successful
    )
    target = config["output_dir"] / "merger" / "mission.md"
    target.parent.mkdir(parents=True, exist_ok=True)
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
    target.write_text(f"{source.rstrip()}\n\n[INPUT_HANDOFFS]\n{paths}\n{receipt_line}", encoding="utf-8")
    return target


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
    with lock:
        for start in range(0, len(config["solvers"]), config["max_concurrency"]):
            wave = config["solvers"][start : start + config["max_concurrency"]]
            with ThreadPoolExecutor(max_workers=len(wave), thread_name_prefix="oracle-multi") as pool:
                futures = [pool.submit(_run_lane, config, lane, parent_id, execute, dry_run) for lane in wave]
                lanes.extend(future.result() for future in as_completed(futures))
        order = {item["id"]: index for index, item in enumerate(config["solvers"])}
        lanes.sort(key=lambda item: order[item["id"]])
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
            _publish_result(config["output_dir"] / "result.json", result, terminal_seal)
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
        _verified_bytes(
            config["merger_mission_path"], config["merger_mission_sha256"], "merger mission"
        )
        if not dry_run:
            for item in successful:
                _verified_bytes(
                    Path(item["output_path"]), item["output_sha256"], "solver handoff"
                )
        merger = execute(
            merger_manifest,
            expected_manifest_sha256=hashlib.sha256(merger_manifest.read_bytes()).hexdigest(),
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
    return {"ok": status == "complete", **result}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run independent Oracle browser sessions in waves and merge handoffs.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.dry_run and args.expected_manifest_sha256 is None:
            raise MultiError(
                "MANIFEST_SHA256_REQUIRED: live Multi runs require "
                "--expected-manifest-sha256 from the exact dry-run preview"
            )
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
