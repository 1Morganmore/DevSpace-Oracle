from __future__ import annotations

"""Fail-closed authority for v3 exact-unit workspace topology.

This module is intentionally independent from the browser and Git runtimes.  All
v3 consumers bind the same canonical receipt instead of reimplementing path
containment rules.
"""

import argparse
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "codexpro.exact-unit-topology-receipt/v1"
TOPOLOGY_VERSION = "parent-run-sibling-v1"
RUNTIME_DIR_NAME = "parallel-runtime-v1"
STAGING_DIR_NAME = "staging-repo"
WORKTREES_DIR_NAME = "worktrees"
AGGREGATE_DIR_NAME = "aggregate-worktree"
UNIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ExactUnitAuthorityError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"errorCode": self.code, "message": str(self), "evidence": self.evidence}}


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        item = path.lstat()
    except OSError:
        return False
    attrs = int(getattr(item, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attrs & flag)


def _existing_ancestor(path: Path) -> Path:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            raise ExactUnitAuthorityError("EXACT_UNIT_TRUSTED_ANCESTOR_MISSING", "no existing ancestor for planned path", {"path": str(path)})
        current = parent
    return current


def _assert_no_reparse_chain(path: Path, stop: Path | None = None) -> None:
    current = path if path.exists() else _existing_ancestor(path)
    stop_existing = _existing_ancestor(stop) if stop is not None else None
    stop_key = _path_key(stop_existing) if stop_existing is not None else None
    while True:
        if _is_reparse(current):
            raise ExactUnitAuthorityError("EXACT_UNIT_REPARSE_ESCAPE", "reparse point in exact-unit authority path", {"path": str(current)})
        if stop_key is not None and _path_key(current) == stop_key:
            return
        parent = current.parent
        if parent == current:
            if stop_key is not None:
                raise ExactUnitAuthorityError("EXACT_UNIT_TRUSTED_ANCESTOR_MISMATCH", "path escaped its trusted ancestor", {"path": str(path), "trusted_ancestor": str(stop)})
            return
        current = parent


def _logical(path: str | os.PathLike[str]) -> Path:
    raw = _nfc(os.path.expanduser(os.fspath(path)))
    return Path(os.path.abspath(raw))


def _final(path: Path, *, require_exists: bool) -> Path:
    if require_exists:
        try:
            return path.resolve(strict=True)
        except OSError as exc:
            raise ExactUnitAuthorityError("EXACT_UNIT_PATH_MISSING", "required exact-unit path does not exist", {"path": str(path)}) from exc
    ancestor = _existing_ancestor(path)
    resolved = ancestor.resolve(strict=True)
    suffix = path.relative_to(ancestor)
    return resolved.joinpath(suffix)


def _path_key(path: str | os.PathLike[str] | Path | None) -> str:
    if path is None:
        return ""
    text = _nfc(os.path.normpath(os.fspath(path)))
    return os.path.normcase(text).rstrip("\\/") or os.path.normcase(text)


def canonicalize_windows_path(
    path: str | os.PathLike[str],
    *,
    require_exists: bool = False,
    trusted_ancestor: str | os.PathLike[str] | None = None,
    reject_reparse: bool = True,
) -> dict[str, str]:
    logical = _logical(path)
    trusted = _logical(trusted_ancestor) if trusted_ancestor is not None else None
    if trusted is not None:
        try:
            logical.relative_to(trusted)
        except ValueError as exc:
            raise ExactUnitAuthorityError("EXACT_UNIT_TRUSTED_ANCESTOR_MISMATCH", "path is outside its trusted ancestor", {"path": str(logical), "trusted_ancestor": str(trusted)}) from exc
    if reject_reparse:
        _assert_no_reparse_chain(logical, trusted)
    final = _final(logical, require_exists=require_exists)
    if trusted is not None:
        trusted_final = _final(trusted, require_exists=trusted.exists())
        try:
            final.relative_to(trusted_final)
        except ValueError as exc:
            raise ExactUnitAuthorityError("EXACT_UNIT_REPARSE_ESCAPE", "final target escaped its trusted ancestor", {"path": str(logical), "final": str(final), "trusted_final": str(trusted_final)}) from exc
    return {"logical": str(logical), "final": str(final)}


def _relation(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> str:
    a = _path_key(left)
    b = _path_key(right)
    if a == b:
        return "equal"
    sep = os.sep
    if b.startswith(a + sep):
        return "ancestor"
    if a.startswith(b + sep):
        return "descendant"
    return "disjoint"


def _validate_id(label: str, value: str) -> str:
    if not UNIT_ID_RE.fullmatch(value):
        raise ExactUnitAuthorityError("EXACT_UNIT_ID_INVALID", f"{label} is not a bounded identifier", {label: value})
    return value


def derive_parent_run_topology(
    state_root: str | os.PathLike[str],
    canonical_project_key: str,
    parent_run_id: str,
    component_id: str,
    unit_id: str,
    attempt_id: str,
) -> dict[str, str]:
    for label, value in (("canonical_project_key", canonical_project_key), ("parent_run_id", parent_run_id), ("component_id", component_id), ("unit_id", unit_id), ("attempt_id", attempt_id)):
        _validate_id(label, str(value))
    parent_run_dir = _logical(state_root) / "projects" / canonical_project_key / "runs" / parent_run_id
    runtime_root = parent_run_dir / RUNTIME_DIR_NAME
    seed = "\0".join((component_id, unit_id, attempt_id)).encode("utf-8")
    unit_leaf = "u-" + hashlib.sha256(seed).hexdigest()[:24]
    return {
        "runtime_topology_version": TOPOLOGY_VERSION,
        "parent_run_dir": str(parent_run_dir),
        "runtime_root": str(runtime_root),
        "staging_repo_root": str(runtime_root / STAGING_DIR_NAME),
        "worktrees_root": str(runtime_root / WORKTREES_DIR_NAME),
        "aggregate_worktree_root": str(runtime_root / AGGREGATE_DIR_NAME),
        "unit_workspace_root": str(runtime_root / WORKTREES_DIR_NAME / unit_leaf),
    }


def _canonical_pair(path: str | os.PathLike[str], *, require_exists: bool, trusted: str | os.PathLike[str] | None = None, reject_reparse: bool = True) -> dict[str, str]:
    return canonicalize_windows_path(path, require_exists=require_exists, trusted_ancestor=trusted, reject_reparse=reject_reparse)


def _assert_disjoint(name: str, left: Mapping[str, str], right: Mapping[str, str]) -> None:
    for dimension in ("logical", "final"):
        relation = _relation(left[dimension], right[dimension])
        if relation != "disjoint":
            raise ExactUnitAuthorityError("EXACT_UNIT_TOPOLOGY_OVERLAP", "exact-unit authority paths overlap", {"pair": name, "dimension": dimension, "relation": relation, "left": left[dimension], "right": right[dimension]})


def validate_exact_unit_topology(inputs: Mapping[str, Any], *, phase: str = "planned") -> dict[str, Any]:
    if phase not in {"planned", "materialized"}:
        raise ExactUnitAuthorityError("EXACT_UNIT_VALIDATION_PHASE_INVALID", "validation phase must be planned or materialized")
    required = (
        "state_root", "canonical_project_key", "parent_run_id", "component_id", "unit_id", "attempt_id",
        "canonical_project_root", "staging_common_git_dir",
    )
    missing = [key for key in required if not str(inputs.get(key) or "")]
    if missing:
        raise ExactUnitAuthorityError("EXACT_UNIT_TOPOLOGY_INPUT_MISSING", "topology inputs are incomplete", {"missing": missing})
    derived = derive_parent_run_topology(
        str(inputs["state_root"]), str(inputs["canonical_project_key"]), str(inputs["parent_run_id"]),
        str(inputs["component_id"]), str(inputs["unit_id"]), str(inputs["attempt_id"]),
    )
    for key in ("parent_run_dir", "runtime_root", "staging_repo_root", "worktrees_root", "aggregate_worktree_root", "unit_workspace_root"):
        supplied = inputs.get(key)
        if supplied is not None and _path_key(str(supplied)) != _path_key(derived[key]):
            raise ExactUnitAuthorityError("EXACT_UNIT_TOPOLOGY_DERIVATION_MISMATCH", "supplied topology differs from the fixed parent-run topology", {"field": key, "supplied": str(supplied), "derived": derived[key]})
    require_runtime = phase == "materialized"
    parent = _canonical_pair(derived["parent_run_dir"], require_exists=require_runtime, trusted=str(inputs["state_root"]))
    runtime = _canonical_pair(derived["runtime_root"], require_exists=require_runtime, trusted=derived["parent_run_dir"])
    staging = _canonical_pair(derived["staging_repo_root"], require_exists=require_runtime, trusted=derived["runtime_root"])
    worktrees = _canonical_pair(derived["worktrees_root"], require_exists=require_runtime, trusted=derived["runtime_root"])
    # The aggregate worktree is created only after all required units integrate.
    # Its planned final path is still validated against reparse/overlap escape.
    aggregate = _canonical_pair(derived["aggregate_worktree_root"], require_exists=False, trusted=derived["runtime_root"])
    unit = _canonical_pair(derived["unit_workspace_root"], require_exists=require_runtime, trusted=derived["worktrees_root"])
    canonical = _canonical_pair(str(inputs["canonical_project_root"]), require_exists=True, reject_reparse=False)
    common = _canonical_pair(str(inputs["staging_common_git_dir"]), require_exists=require_runtime, trusted=derived["staging_repo_root"])
    drive_root = Path(unit["logical"]).anchor or os.path.splitdrive(unit["logical"])[0] + os.sep
    home_path = str(inputs.get("user_home") or Path.home())
    home = _canonical_pair(home_path, require_exists=True, reject_reparse=False)
    if drive_root and _relation(unit["logical"], drive_root) == "equal":
        raise ExactUnitAuthorityError("EXACT_UNIT_ROOT_EQUALS_DRIVE_ROOT", "unit workspace cannot equal a drive root")
    if _relation(unit["logical"], home["logical"]) == "equal" or _relation(unit["final"], home["final"]) == "equal":
        raise ExactUnitAuthorityError("EXACT_UNIT_ROOT_EQUALS_USER_HOME", "unit workspace cannot equal the user home")
    for label, other in (("canonical", canonical), ("staging", staging), ("common-git", common), ("aggregate", aggregate)):
        _assert_disjoint(label, unit, other)
    siblings: list[dict[str, str]] = []
    for raw in inputs.get("sibling_unit_roots") or []:
        sibling = _canonical_pair(str(raw), require_exists=phase == "materialized", trusted=derived["worktrees_root"])
        _assert_disjoint("sibling-unit", unit, sibling)
        siblings.append(sibling)
    allowed_roots = [str(item) for item in (inputs.get("allowed_roots") or [unit["logical"]])]
    if len(allowed_roots) != 1 or _path_key(allowed_roots[0]) != _path_key(unit["logical"]):
        raise ExactUnitAuthorityError("EXACT_UNIT_ALLOWED_ROOTS_NOT_SINGLETON", "allowedRoots must contain exactly the unit workspace root", {"allowed_roots": allowed_roots, "unit_workspace_root": unit["logical"]})
    return {
        "schema": SCHEMA,
        "runtime_topology_version": TOPOLOGY_VERSION,
        "validation_phase": phase,
        "canonical_project_key": str(inputs["canonical_project_key"]),
        "parent_run_id": str(inputs["parent_run_id"]),
        "component_id": str(inputs["component_id"]),
        "unit_id": str(inputs["unit_id"]),
        "attempt_id": str(inputs["attempt_id"]),
        "canonical_project_root": canonical,
        "parent_run_dir": parent,
        "runtime_root": runtime,
        "staging_repo_root": staging,
        "staging_common_git_dir": common,
        "worktrees_root": worktrees,
        "aggregate_worktree_root": aggregate,
        "unit_workspace_root": unit,
        "drive_root": drive_root,
        "user_home": home,
        "allowed_roots": [unit["logical"]],
        "sibling_unit_roots_sha256": canonical_sha256(sorted((_path_key(item["logical"]) for item in siblings))),
        "validator_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def build_topology_receipt(validated: Mapping[str, Any]) -> dict[str, Any]:
    receipt = dict(validated)
    receipt["topology_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def validate_and_build(inputs: Mapping[str, Any], *, phase: str = "planned") -> dict[str, Any]:
    return build_topology_receipt(validate_exact_unit_topology(inputs, phase=phase))


def _main() -> int:
    parser = argparse.ArgumentParser(description="Validate a v3 exact-unit authority topology")
    parser.add_argument("--input", required=True)
    parser.add_argument("--phase", choices=("planned", "materialized"), default="planned")
    parser.add_argument("--receipt-out")
    args = parser.parse_args()
    try:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        receipt = validate_and_build(payload, phase=args.phase)
        text = json.dumps({"ok": True, "receipt": receipt}, ensure_ascii=False, sort_keys=True)
        if args.receipt_out:
            output = Path(args.receipt_out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(text)
        return 0
    except (ExactUnitAuthorityError, OSError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ExactUnitAuthorityError):
            print(json.dumps(exc.envelope(), ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps({"ok": False, "error": {"errorCode": "EXACT_UNIT_AUTHORITY_ERROR", "message": str(exc)}}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
