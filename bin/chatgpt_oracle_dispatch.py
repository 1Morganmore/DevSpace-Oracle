from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Iterable

BIN = Path(__file__).resolve().parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROFILES = _load("oracle_dispatch_profiles", BIN / "chatgpt_oracle_profiles.py")
RUNNER = _load("oracle_dispatch_runner", BIN / "chatgpt_oracle_run.py")


def compile_manifest(
    *,
    mode: str,
    project_root: Path,
    mission_path: Path | None,
    output_path: Path,
    reasoning_level: str | None = None,
    attachment_paths: Iterable[Path] | None = None,
    context_manifest_path: Path | None = None,
) -> dict[str, Any]:
    contract = PROFILES.build_launch_contract(
        mode,
        mission_path=mission_path,
        reasoning_level=reasoning_level,
        attachment_paths=list(attachment_paths or ()),
    )
    is_pro = contract["mode"] == "pro"
    if is_pro and context_manifest_path is None:
        raise ValueError("PRO_CONTEXT_MANIFEST_REQUIRED: --context-manifest is required for Pro mode")
    if not is_pro and context_manifest_path is not None:
        raise ValueError("CONTEXT_MANIFEST_FORBIDDEN: --context-manifest is only valid for Pro mode")
    result = {"ok": True, "contract": contract, "oracle_manifest_path": None}
    if not contract["oracle_launch"]:
        return result
    root = project_root.expanduser().resolve(strict=True)
    target = output_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema": RUNNER.STATE.SCHEMA,
        "project_root": str(root),
        "mission_path": contract["mission_path"],
        "mission_sha256": RUNNER.STATE.sha256_file(Path(contract["mission_path"]).resolve(strict=True)),
        "mode": "browser",
        "transport": "pro-attachment-only" if contract["mode"] == "pro" else "devspace",
        "model": contract.get("model") or "gpt-5.6",
        "model_strategy": "select",
        "thinking_time": "heavy",
        "research": "deep" if contract["research"] else "off",
        "archive": "auto",
    }
    if is_pro:
        assert context_manifest_path is not None
        raw_context_manifest = context_manifest_path.expanduser()
        if not raw_context_manifest.is_absolute():
            raise ValueError("PRO_CONTEXT_MANIFEST_ABSOLUTE_REQUIRED: --context-manifest must be absolute")
        if raw_context_manifest.is_symlink() or not raw_context_manifest.is_file():
            raise ValueError("PRO_CONTEXT_MANIFEST_FILE_REQUIRED: --context-manifest must be a regular non-symlink file")
        context_manifest = raw_context_manifest.resolve(strict=True)
        try:
            context_manifest.relative_to(root)
        except ValueError as exc:
            raise ValueError("PRO_CONTEXT_MANIFEST_ROOT_ESCAPE: --context-manifest must be inside project_root") from exc
        manifest["attachments"] = contract["attachments"]
        manifest["attachment_sha256s"] = [
            RUNNER.STATE.sha256_file(Path(value).expanduser().resolve(strict=True))
            for value in contract["attachments"]
        ]
        manifest["project_context_manifest_path"] = str(context_manifest)
        manifest["project_context_manifest_sha256"] = RUNNER.STATE.sha256_file(context_manifest)
    else:
        manifest["app_name"] = "DevSpace"
        manifest["task_outcome_contract"] = "v1"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["oracle_manifest_path"] = str(target)
    result["oracle_manifest_sha256"] = RUNNER.STATE.sha256_file(target)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve a GPT mode and dispatch it to Oracle + DevSpace.")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--mission-path", type=Path)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--reasoning-level")
    parser.add_argument("--attachment", type=Path, action="append", default=[])
    parser.add_argument("--context-manifest", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        compiled = compile_manifest(
            mode=args.mode,
            project_root=args.project_root,
            mission_path=args.mission_path,
            output_path=args.manifest_output,
            reasoning_level=args.reasoning_level,
            attachment_paths=args.attachment,
            context_manifest_path=args.context_manifest,
        )
        if compiled["oracle_manifest_path"]:
            run = RUNNER.execute_run(
                Path(compiled["oracle_manifest_path"]),
                expected_manifest_sha256=str(compiled["oracle_manifest_sha256"]),
                dry_run=args.dry_run,
            )
            value = {**compiled, "run": run, "ok": bool(run.get("ok"))}
        else:
            value = compiled
    except Exception as exc:
        value = {"ok": False, "error": {"code": "ORACLE_DISPATCH_FAILED", "message": str(exc)}}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0 if value.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
