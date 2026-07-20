from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from handoff_contract import canonical_json_bytes, canonical_sha256, sha256_bytes


SNAPSHOT_SCHEMA = "codex.chatgpt.source-snapshot/v1"


class WorkspaceGuardError(ValueError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400)
    return bool(attributes & reparse_flag)


def _assert_no_reparse_chain(root: Path, candidate: Path) -> None:
    current = candidate
    while True:
        if _is_reparse_point(current):
            raise WorkspaceGuardError("REPARSE_POINT_NOT_ALLOWED", str(current))
        if current == root:
            return
        parent = current.parent
        if parent == current:
            raise WorkspaceGuardError("PATH_ESCAPES_WORKSPACE", str(candidate))
        current = parent


def resolve_workspace_path(
    root: Path,
    path_value: str | os.PathLike[str],
    *,
    require_exists: bool = True,
) -> Path:
    root = root.resolve(strict=True)
    raw = Path(path_value)
    candidate = raw if raw.is_absolute() else root / raw
    _assert_no_reparse_chain(root, candidate)
    try:
        resolved = candidate.resolve(strict=require_exists)
    except FileNotFoundError as exc:
        raise WorkspaceGuardError("WORKSPACE_PATH_MISSING", str(candidate)) from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceGuardError("PATH_ESCAPES_WORKSPACE", str(candidate)) from exc
    return resolved


def resolve_workspace_file(root: Path, path_value: str | os.PathLike[str]) -> Path:
    resolved = resolve_workspace_path(root, path_value, require_exists=True)
    if not resolved.is_file():
        raise WorkspaceGuardError("SOURCE_FILE_REQUIRED", str(resolved))
    return resolved


def _git_output(root: Path, args: list[str]) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _canonical_file_entries(
    root: Path,
    paths: Iterable[str | os.PathLike[str]],
    *,
    role: str,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_casefold: dict[str, str] = {}
    for value in paths:
        resolved = resolve_workspace_file(root, value)
        relative = resolved.relative_to(root).as_posix()
        key = relative.casefold()
        if key in seen_casefold:
            raise WorkspaceGuardError(
                "CASE_COLLISION",
                f"{seen_casefold[key]} vs {relative}",
            )
        seen_casefold[key] = relative
        entries.append(
            {
                "path": relative,
                "role": role,
                "bytes": resolved.stat().st_size,
                "sha256": file_sha256(resolved),
            }
        )
    return sorted(entries, key=lambda item: str(item["path"]).casefold())


def build_workspace_snapshot(
    *,
    workspace_root: Path,
    selected_paths: Iterable[str | os.PathLike[str]],
    policy_paths: Iterable[str | os.PathLike[str]] = (),
    question_sha256: str,
    include_git_head: bool = True,
    include_dirty_status: bool = True,
) -> dict[str, Any]:
    root = workspace_root.resolve(strict=True)
    selected = _canonical_file_entries(root, selected_paths, role="source")
    policies = _canonical_file_entries(root, policy_paths, role="policy")
    combined_keys: dict[str, str] = {}
    for entry in [*selected, *policies]:
        key = str(entry["path"]).casefold()
        if key in combined_keys:
            raise WorkspaceGuardError(
                "CASE_COLLISION",
                f"{combined_keys[key]} vs {entry['path']}",
            )
        combined_keys[key] = str(entry["path"])
    if not selected:
        raise WorkspaceGuardError("SOURCE_PATHS_EMPTY")

    git_head: str | None = None
    dirty_status_sha256: str | None = None
    if include_git_head:
        raw_head = _git_output(root, ["rev-parse", "HEAD"])
        git_head = raw_head.decode("ascii", errors="replace").strip() if raw_head else None
    if include_dirty_status:
        raw_status = _git_output(root, ["status", "--porcelain=v1", "-z"])
        dirty_status_sha256 = sha256_bytes(raw_status) if raw_status is not None else None

    fingerprint_payload = {
        "schema": SNAPSHOT_SCHEMA,
        "workspace_root": str(root),
        "question_sha256": question_sha256,
        "git_head": git_head,
        "dirty_status_sha256": dirty_status_sha256,
        "files": [*selected, *policies],
    }
    return {
        **fingerprint_payload,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_sha256": canonical_sha256(fingerprint_payload),
    }


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def build_source_archive(
    *,
    workspace_root: Path,
    snapshot: dict[str, Any],
    output_zip: Path,
) -> dict[str, Any]:
    root = workspace_root.resolve(strict=True)
    entries = list(snapshot.get("files") or [])
    archive_names: set[str] = set()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        snapshot_info = zipfile.ZipInfo("SOURCE_SNAPSHOT.json", date_time=(1980, 1, 1, 0, 0, 0))
        snapshot_info.compress_type = zipfile.ZIP_DEFLATED
        archive_snapshot = {key: value for key, value in snapshot.items() if key != "generated_at"}
        archive.writestr(snapshot_info, canonical_json_bytes(archive_snapshot))
        archive_names.add("source_snapshot.json")
        for entry in sorted(entries, key=lambda item: str(item["path"]).casefold()):
            relative = str(entry["path"]).replace("\\", "/")
            archive_name = f"files/{relative}"
            collision_key = archive_name.casefold()
            if collision_key in archive_names:
                raise WorkspaceGuardError("ZIP_ENTRY_COLLISION", archive_name)
            archive_names.add(collision_key)
            source = resolve_workspace_file(root, relative)
            info = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
    return {
        "path": str(output_zip),
        "bytes": output_zip.stat().st_size,
        "sha256": file_sha256(output_zip),
        "entry_count": len(entries) + 1,
    }


def compare_workspace_snapshots(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before.get("snapshot_sha256") != after.get("snapshot_sha256"):
        raise WorkspaceGuardError(
            "WORKSPACE_SNAPSHOT_CHANGED",
            f"before={before.get('snapshot_sha256')} after={after.get('snapshot_sha256')}",
        )
