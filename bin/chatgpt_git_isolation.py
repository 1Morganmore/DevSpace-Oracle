from __future__ import annotations

"""Host-only Git isolation primitives for parallel implementation v3.

Workers receive a checked-out unit directory only.  Clone/worktree metadata,
commits, integration refs, verification and canonical fast-forward are owned by
this host module.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Sequence

HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
ZERO_OID_RE = re.compile(r"^0+$")
FORBIDDEN_CONFIG_PREFIXES = (
    "credential.", "http.", "url.", "include.", "includeif.", "core.hookspath",
    "core.sshcommand", "gpg.", "user.signingkey", "remote.", "submodule.",
)


class GitIsolationError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.evidence = dict(evidence or {})


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path, Mapping[str, str]], CommandResult]


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def default_runner(argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> CommandResult:
    completed = subprocess.run(
        list(argv), cwd=str(cwd), env=dict(env), check=False, capture_output=True,
        text=True, timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return CommandResult(tuple(argv), str(cwd), completed.returncode, completed.stdout, completed.stderr)


def isolated_git_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never",
        "GIT_ASKPASS": "", "SSH_ASKPASS": "",
    })
    for key in tuple(env):
        if key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_") or key in {"GIT_CONFIG_COUNT", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"}:
            env.pop(key, None)
    if os.name == "nt":
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "core.longpaths"
        env["GIT_CONFIG_VALUE_0"] = "true"
    return env


def canonical_git_home(root: Path) -> Path:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return root.parent / f".codexpro-git-home-{digest}"


def _run_git(repo: Path, args: Sequence[str], *, env: Mapping[str, str], runner: Runner = default_runner, check: bool = True) -> CommandResult:
    result = runner(("git", *args), repo, env)
    if check and result.returncode != 0:
        raise GitIsolationError("GIT_COMMAND_FAILED", "Git isolation command failed", {"argv": list(result.argv), "cwd": result.cwd, "returncode": result.returncode, "stderr": result.stderr[-4000:]})
    return result


def _git_text(repo: Path, args: Sequence[str], *, env: Mapping[str, str], runner: Runner = default_runner) -> str:
    return _run_git(repo, args, env=env, runner=runner).stdout.strip()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    attrs = int(getattr(info, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attrs & flag)


def _canonical_relpath(value: str) -> str:
    text = unicodedata.normalize("NFC", value.replace("\\", "/")).strip("/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise GitIsolationError("IMPLEMENTATION_PATH_INVALID", "claimed or changed path is not canonical relative", {"path": value})
    if pure.parts[0].casefold() == ".git":
        raise GitIsolationError("IMPLEMENTATION_GIT_METADATA_FORBIDDEN", "workers cannot claim Git metadata", {"path": value})
    return pure.as_posix()


def _parse_z(payload: str) -> list[str]:
    return [part for part in payload.split("\0") if part]


def _normalize_windows_eol_status(
    root: Path,
    status: str,
    *,
    env: Mapping[str, str],
    runner: Runner,
) -> str:
    if os.name != "nt" or not status:
        return status
    kept: list[str] = []
    for record in _parse_z(status):
        fields = record.split(" ", 8)
        if len(fields) == 9 and fields[0] == "1" and fields[1] == ".M":
            path = fields[8]
            content_diff = _run_git(
                root,
                ("diff", "--quiet", "--ignore-cr-at-eol", "--", path),
                env=env,
                runner=runner,
                check=False,
            )
            if content_diff.returncode == 0:
                continue
        kept.append(record)
    return "".join(f"{record}\0" for record in kept)


def canonical_snapshot(repo: str | os.PathLike[str], *, runner: Runner = default_runner) -> dict[str, Any]:
    root = Path(repo).resolve(strict=True)
    env = isolated_git_env(canonical_git_home(root))
    head = _git_text(root, ("rev-parse", "--verify", "HEAD"), env=env, runner=runner)
    tree = _git_text(root, ("rev-parse", "HEAD^{tree}"), env=env, runner=runner)
    common = _git_text(root, ("rev-parse", "--path-format=absolute", "--git-common-dir"), env=env, runner=runner)
    git_dir = _git_text(root, ("rev-parse", "--path-format=absolute", "--git-dir"), env=env, runner=runner)
    status = _run_git(root, ("status", "--porcelain=v2", "-z", "--untracked-files=all"), env=env, runner=runner).stdout
    status = _normalize_windows_eol_status(root, status, env=env, runner=runner)
    worktrees = _run_git(root, ("worktree", "list", "--porcelain", "-z"), env=env, runner=runner).stdout
    submodules = _run_git(root, ("submodule", "status", "--recursive"), env=env, runner=runner, check=False).stdout
    config_rows = _run_git(root, ("config", "--local", "--null", "--list"), env=env, runner=runner, check=False).stdout
    filesystem = {
        "root_final": str(root),
        "git_dir_final": str(Path(git_dir).resolve(strict=True)),
        "common_git_dir_final": str(Path(common).resolve(strict=True)),
        "root_reparse": _is_reparse(root),
        "git_dir_reparse": _is_reparse(Path(git_dir)),
        "common_git_dir_reparse": _is_reparse(Path(common)),
    }
    snapshot = {
        "schema": "codex.chatgpt.canonical-baseline-identity/v1",
        "head_oid": head,
        "tree_oid": tree,
        "status_sha256": hashlib.sha256(status.encode("utf-8", "surrogateescape")).hexdigest(),
        "status_empty": not bool(status),
        "worktrees_sha256": hashlib.sha256(worktrees.encode("utf-8", "surrogateescape")).hexdigest(),
        "submodules_sha256": hashlib.sha256(submodules.encode("utf-8", "surrogateescape")).hexdigest(),
        "config_sha256": hashlib.sha256(config_rows.encode("utf-8", "surrogateescape")).hexdigest(),
        "filesystem": filesystem,
    }
    snapshot["baseline_identity_sha256"] = canonical_sha256(snapshot)
    return snapshot


def assert_snapshot_equal(expected: Mapping[str, Any], actual: Mapping[str, Any], *, code: str = "CANONICAL_BASELINE_DRIFT") -> None:
    if str(expected.get("baseline_identity_sha256") or "") != str(actual.get("baseline_identity_sha256") or ""):
        raise GitIsolationError(code, "canonical repository identity changed", {"expected": expected.get("baseline_identity_sha256"), "actual": actual.get("baseline_identity_sha256")})


def _assert_no_alternates(repo: Path) -> None:
    common = Path(subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"], check=True, capture_output=True, text=True).stdout.strip())
    alternates = common / "objects" / "info" / "alternates"
    if alternates.exists():
        raise GitIsolationError("STAGING_ALTERNATES_FORBIDDEN", "staging repository uses an alternate object database", {"path": str(alternates)})


def safe_clone(canonical_repo: str | os.PathLike[str], staging_repo: str | os.PathLike[str], *, runner: Runner = default_runner) -> dict[str, Any]:
    source = Path(canonical_repo).resolve(strict=True)
    destination = Path(staging_repo).absolute()
    if destination.exists():
        raise GitIsolationError("STAGING_REPO_EXISTS", "staging clone destination already exists", {"path": str(destination)})
    destination.parent.mkdir(parents=True, exist_ok=True)
    home = destination.parent / ".git-host-home"
    env = isolated_git_env(home)
    result = runner(("git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(source), str(destination)), destination.parent, env)
    if result.returncode != 0:
        raise GitIsolationError("STAGING_CLONE_FAILED", "isolated staging clone failed", {"stderr": result.stderr[-4000:]})
    _run_git(destination, ("config", "--local", "core.hooksPath", os.devnull), env=env, runner=runner)
    _run_git(destination, ("config", "--local", "credential.helper", ""), env=env, runner=runner)
    _run_git(destination, ("config", "--local", "commit.gpgSign", "false"), env=env, runner=runner)
    config = _run_git(destination, ("config", "--local", "--name-only", "--null", "--list"), env=env, runner=runner).stdout.casefold()
    for forbidden in ("include.path", "includeif.", "core.sshcommand", "remote.origin.promisor", "extensions.partialclone"):
        if forbidden in config:
            raise GitIsolationError("STAGING_CONFIG_FORBIDDEN", "staging clone inherited unsafe Git configuration", {"key": forbidden})
    _assert_no_alternates(destination)
    common = _git_text(destination, ("rev-parse", "--path-format=absolute", "--git-common-dir"), env=env, runner=runner)
    objects = Path(common) / "objects"
    if not objects.is_dir():
        raise GitIsolationError("STAGING_OBJECT_STORE_MISSING", "staging clone object store is missing")
    receipt = {
        "schema": "codex.chatgpt.staging-clone-receipt/v1",
        "source_root": str(source),
        "staging_repo_root": str(destination.resolve(strict=True)),
        "staging_common_git_dir": str(Path(common).resolve(strict=True)),
        "clone_argv": ["git", "clone", "--no-local", "--no-hardlinks", "--no-checkout", "--", str(source), str(destination)],
        "source_head_oid": _git_text(source, ("rev-parse", "HEAD"), env=env, runner=runner),
        "staging_head_oid": _git_text(destination, ("rev-parse", "HEAD"), env=env, runner=runner),
    }
    if receipt["source_head_oid"] != receipt["staging_head_oid"]:
        raise GitIsolationError("STAGING_OBJECT_IDENTITY_MISMATCH", "staging and canonical HEAD differ")
    receipt["staging_clone_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def _common_metadata_tree_sha256(common: Path) -> str:
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(common.rglob("*"), key=lambda item: item.relative_to(common).as_posix()):
        relative = path.relative_to(common)
        if relative.parts and relative.parts[0] == "objects":
            continue
        if _is_reparse(path):
            raise GitIsolationError("STAGING_COMMON_METADATA_REPARSE", "staging common Git metadata contains a reparse path", {"path": str(path)})
        if not path.is_file():
            continue
        file_count += 1
        size = path.stat().st_size
        total_bytes += size
        if file_count > 10000 or total_bytes > 64 * 1024 * 1024:
            raise GitIsolationError("STAGING_COMMON_METADATA_BOUNDS_EXCEEDED", "staging common Git metadata exceeded bounded verification limits")
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def common_metadata_identity(staging_repo: str | os.PathLike[str], *, runner: Runner = default_runner) -> dict[str, Any]:
    repo = Path(staging_repo).resolve(strict=True)
    env = isolated_git_env(repo.parent / ".git-host-home")
    common = Path(_git_text(repo, ("rev-parse", "--path-format=absolute", "--git-common-dir"), env=env, runner=runner)).resolve(strict=True)
    refs = _run_git(repo, ("for-each-ref", "--format=%(refname)%00%(objectname)%00", "refs/codexpro/"), env=env, runner=runner).stdout
    worktrees = _run_git(repo, ("worktree", "list", "--porcelain", "-z"), env=env, runner=runner).stdout
    config = (common / "config").read_bytes() if (common / "config").is_file() else b""
    identity = {
        "common_git_dir": str(common),
        "refs_sha256": hashlib.sha256(refs.encode("utf-8", "surrogateescape")).hexdigest(),
        "worktrees_sha256": hashlib.sha256(worktrees.encode("utf-8", "surrogateescape")).hexdigest(),
        "config_sha256": hashlib.sha256(config).hexdigest(),
        "metadata_tree_sha256": _common_metadata_tree_sha256(common),
        "alternates_present": (common / "objects" / "info" / "alternates").exists(),
    }
    identity["common_metadata_identity_sha256"] = canonical_sha256(identity)
    return identity


def assert_common_metadata(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if expected.get("alternates_present") or actual.get("alternates_present") or expected.get("common_metadata_identity_sha256") != actual.get("common_metadata_identity_sha256"):
        raise GitIsolationError("STAGING_COMMON_METADATA_DRIFT", "staging common Git authority changed")


def create_unit_worktree(staging_repo: str | os.PathLike[str], unit_root: str | os.PathLike[str], input_base_oid: str, *, runner: Runner = default_runner) -> dict[str, Any]:
    repo = Path(staging_repo).resolve(strict=True)
    target = Path(unit_root).absolute()
    if target.exists():
        raise GitIsolationError("UNIT_WORKTREE_EXISTS", "unit worktree already exists")
    env = isolated_git_env(repo.parent / ".git-host-home")
    _git_text(repo, ("cat-file", "-e", f"{input_base_oid}^{{commit}}"), env=env, runner=runner)
    _run_git(repo, ("worktree", "add", "--detach", "--no-checkout", "--", str(target), input_base_oid), env=env, runner=runner)
    _run_git(target, ("checkout", "--detach", "--force", input_base_oid, "--"), env=env, runner=runner)
    head = _git_text(target, ("rev-parse", "HEAD"), env=env, runner=runner)
    tree = _git_text(target, ("rev-parse", "HEAD^{tree}"), env=env, runner=runner)
    if head != input_base_oid:
        raise GitIsolationError("UNIT_INPUT_BASE_MISMATCH", "unit worktree did not materialize immutable input base")
    git_file = target / ".git"
    if not git_file.is_file() or git_file.is_symlink():
        raise GitIsolationError("UNIT_GIT_METADATA_EXPOSED", "unit worktree must expose only a regular .git pointer file")
    receipt = {"schema": "codex.chatgpt.unit-worktree-receipt/v1", "unit_workspace_root": str(target.resolve(strict=True)), "input_base_oid": head, "input_base_tree_oid": tree}
    receipt["unit_worktree_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def changed_paths(worktree: str | os.PathLike[str], *, runner: Runner = default_runner) -> list[dict[str, Any]]:
    root = Path(worktree).resolve(strict=True)
    env = isolated_git_env(root.parent / ".git-host-home")
    raw = _run_git(root, ("status", "--porcelain=v2", "-z", "--untracked-files=all"), env=env, runner=runner).stdout
    parts = _parse_z(raw)
    changes: list[dict[str, Any]] = []
    index = 0
    while index < len(parts):
        row = parts[index]
        if row.startswith("1 "):
            path = row.split(" ", 8)[-1]
            changes.append({"kind": "path", "path": _canonical_relpath(path)})
        elif row.startswith("2 "):
            path = row.split(" ", 9)[-1]
            index += 1
            if index >= len(parts):
                raise GitIsolationError("UNIT_DIFF_INVALID", "rename record is incomplete")
            old = parts[index]
            changes.append({"kind": "rename", "path": _canonical_relpath(path), "old_path": _canonical_relpath(old)})
        elif row.startswith("? "):
            changes.append({"kind": "untracked", "path": _canonical_relpath(row[2:])})
        elif row.startswith("u "):
            raise GitIsolationError("UNIT_DIFF_UNMERGED", "unit worktree contains an unmerged path")
        index += 1
    return changes


def _path_matches_claim(path: str, claims: set[str]) -> bool:
    return any(path == claim or path.startswith(claim.rstrip("/") + "/") for claim in claims)


def validate_unit_diff(worktree: str | os.PathLike[str], claimed_paths: Iterable[str], *, runner: Runner = default_runner) -> dict[str, Any]:
    root = Path(worktree).resolve(strict=True)
    claims = {_canonical_relpath(item) for item in claimed_paths}
    if not claims:
        raise GitIsolationError("UNIT_CLAIMS_EMPTY", "implementation unit must own at least one path")
    changes = changed_paths(root, runner=runner)
    for item in changes:
        for key in ("path", "old_path"):
            if key in item and not _path_matches_claim(item[key], claims):
                raise GitIsolationError("UNIT_DIFF_OUT_OF_SCOPE", "unit changed a path it does not own", {"path": item[key], "claims": sorted(claims)})
        full = root / item["path"]
        if full.exists() and _is_reparse(full):
            raise GitIsolationError("UNIT_DIFF_REPARSE_ESCAPE", "unit introduced or changed a reparse path", {"path": item["path"]})
        if item["path"].casefold() == ".git" or item["path"].casefold().startswith(".git/"):
            raise GitIsolationError("IMPLEMENTATION_GIT_METADATA_FORBIDDEN", "unit changed Git metadata")
    env = isolated_git_env(root.parent / ".git-host-home")
    raw_modes = _run_git(root, ("diff", "--raw", "--no-abbrev", "--cached"), env=env, runner=runner, check=False).stdout + _run_git(root, ("diff", "--raw", "--no-abbrev"), env=env, runner=runner, check=False).stdout
    if " 160000 " in raw_modes or raw_modes.startswith(":160000") or "160000" in raw_modes:
        raise GitIsolationError("UNIT_GITLINK_FORBIDDEN", "unit diff contains a gitlink/submodule change")
    receipt = {"schema": "codex.chatgpt.unit-diff-validation/v1", "claimed_paths": sorted(claims), "changes": changes}
    receipt["unit_diff_validation_sha256"] = canonical_sha256(receipt)
    return receipt


def deterministic_commit(worktree: str | os.PathLike[str], *, parent_oid: str, unit_id: str, message: str, runner: Runner = default_runner) -> dict[str, Any]:
    root = Path(worktree).resolve(strict=True)
    env = isolated_git_env(root.parent / ".git-host-home")
    if _git_text(root, ("rev-parse", "HEAD"), env=env, runner=runner) != parent_oid:
        raise GitIsolationError("UNIT_INPUT_BASE_DRIFT", "unit HEAD changed before host commit")
    _run_git(root, ("add", "--all", "--", "."), env=env, runner=runner)
    tree_oid = _git_text(root, ("write-tree",), env=env, runner=runner)
    parent_tree = _git_text(root, ("rev-parse", f"{parent_oid}^{{tree}}"), env=env, runner=runner)
    if tree_oid == parent_tree:
        raise GitIsolationError("UNIT_EMPTY_DIFF", "accepted implementation unit has no tree change")
    commit_env = dict(env)
    commit_env.update({
        "GIT_AUTHOR_NAME": "CodexPro Parallel Host", "GIT_AUTHOR_EMAIL": "codexpro@invalid.local",
        "GIT_COMMITTER_NAME": "CodexPro Parallel Host", "GIT_COMMITTER_EMAIL": "codexpro@invalid.local",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    })
    commit = _run_git(root, ("commit-tree", tree_oid, "-p", parent_oid, "-m", f"{message}\n\nunit-id: {unit_id}"), env=commit_env, runner=runner).stdout.strip()
    _run_git(root, ("reset", "--hard", commit), env=env, runner=runner)
    if _git_text(root, ("rev-parse", f"{commit}^"), env=env, runner=runner) != parent_oid:
        raise GitIsolationError("UNIT_COMMIT_PARENT_MISMATCH", "host-created commit parent is not immutable input base")
    receipt = {"schema": "codex.chatgpt.unit-commit-receipt/v1", "unit_id": unit_id, "parent_oid": parent_oid, "tree_oid": tree_oid, "commit_oid": commit}
    receipt["unit_commit_receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def create_temp_ref(repo: str | os.PathLike[str], ref_name: str, oid: str, *, expected_old_oid: str | None = None, runner: Runner = default_runner) -> None:
    root = Path(repo).resolve(strict=True)
    if not ref_name.startswith("refs/codexpro/parallel/") or ".." in ref_name:
        raise GitIsolationError("INTEGRATION_REF_INVALID", "temporary integration ref is outside the reserved namespace")
    env = isolated_git_env(root.parent / ".git-host-home")
    args = ["update-ref", ref_name, oid]
    if expected_old_oid is not None:
        args.append(expected_old_oid)
    _run_git(root, tuple(args), env=env, runner=runner)


def import_integration_ref(
    canonical_repo: str | os.PathLike[str],
    staging_repo: str | os.PathLike[str],
    target_oid: str,
    ref_name: str,
    *,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    canonical = Path(canonical_repo).resolve(strict=True)
    staging = Path(staging_repo).resolve(strict=True)
    if not ref_name.startswith("refs/codexpro/parallel/") or ".." in ref_name:
        raise GitIsolationError("INTEGRATION_REF_INVALID", "temporary integration ref is outside the reserved namespace")
    env = isolated_git_env(canonical_git_home(canonical))
    # Do not fetch an otherwise-unadvertised raw OID from a non-bare staging
    # repository.  Windows upload-pack can race the linked-worktree HEAD and
    # fail to traverse the target's parent even though the object closure is
    # present.  Pin the verified target under our reserved namespace for the
    # duration of the synchronous fetch, then remove only that source ref.
    source_ref = f"{ref_name}/export"
    staging_env = isolated_git_env(staging.parent / ".git-host-home")
    bundle = canonical.parent / f".codexpro-integration-{hashlib.sha256(ref_name.encode('utf-8')).hexdigest()[:16]}.bundle"
    _run_git(staging, ("cat-file", "-e", f"{target_oid}^{{commit}}"), env=staging_env, runner=runner)
    _run_git(staging, ("update-ref", source_ref, target_oid), env=staging_env, runner=runner)
    try:
        # Fetching directly from a deeply nested non-bare repository launches
        # upload-pack without reliably preserving core.longpaths on Windows.
        # A bundle created by the already validated staging process avoids
        # that child-path failure and is removed after the synchronous import.
        if bundle.exists():
            bundle.unlink()
        _run_git(staging, ("bundle", "create", str(bundle), source_ref), env=staging_env, runner=runner)
        _run_git(canonical, ("fetch", "--no-tags", "--no-write-fetch-head", "--", str(bundle), f"{source_ref}:{ref_name}"), env=env, runner=runner)
    finally:
        _run_git(staging, ("update-ref", "-d", source_ref, target_oid), env=staging_env, runner=runner, check=False)
        if bundle.exists():
            bundle.unlink()
    imported = _git_text(canonical, ("rev-parse", ref_name), env=env, runner=runner)
    if imported != target_oid:
        raise GitIsolationError("INTEGRATION_REF_IDENTITY_MISMATCH", "imported integration ref differs from the verified target")
    receipt = {"schema": "codex.chatgpt.integration-ref-import/v1", "target_oid": target_oid, "ref_name": ref_name, "staging_repo": str(staging)}
    receipt["integration_ref_import_sha256"] = canonical_sha256(receipt)
    return receipt


def integrate_component_heads(
    staging_repo: str | os.PathLike[str],
    aggregate_worktree: str | os.PathLike[str],
    baseline_oid: str,
    component_heads: Mapping[str, str],
    *,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    repo = Path(staging_repo).resolve(strict=True)
    aggregate = Path(aggregate_worktree).absolute()
    env = isolated_git_env(repo.parent / ".git-host-home")
    if aggregate.exists():
        raise GitIsolationError("AGGREGATE_WORKTREE_EXISTS", "aggregate worktree already exists")
    _run_git(repo, ("worktree", "add", "--detach", "--", str(aggregate), baseline_oid), env=env, runner=runner)
    current = baseline_oid
    integrated: list[dict[str, Any]] = []
    commit_env = dict(env)
    commit_env.update({
        "GIT_AUTHOR_NAME": "CodexPro Parallel Host", "GIT_AUTHOR_EMAIL": "codexpro@invalid.local",
        "GIT_COMMITTER_NAME": "CodexPro Parallel Host", "GIT_COMMITTER_EMAIL": "codexpro@invalid.local",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    })
    try:
        for component_id, head_oid in sorted(component_heads.items()):
            if not HASH_RE.fullmatch(head_oid):
                raise GitIsolationError("COMPONENT_HEAD_INVALID", "component integration head is invalid", {"component_id": component_id})
            ancestor = _run_git(repo, ("merge-base", "--is-ancestor", baseline_oid, head_oid), env=env, runner=runner, check=False)
            if ancestor.returncode != 0:
                raise GitIsolationError("COMPONENT_HEAD_BASE_MISMATCH", "component head is not based on canonical baseline", {"component_id": component_id})
            commits = [line for line in _run_git(repo, ("rev-list", "--reverse", f"{baseline_oid}..{head_oid}"), env=env, runner=runner).stdout.splitlines() if line]
            for commit_oid in commits:
                cherry = _run_git(aggregate, ("cherry-pick", "--no-commit", commit_oid), env=env, runner=runner, check=False)
                if cherry.returncode != 0:
                    _run_git(aggregate, ("cherry-pick", "--abort"), env=env, runner=runner, check=False)
                    raise GitIsolationError("COMPONENT_INTEGRATION_CONFLICT", "deterministic component integration conflicted", {"component_id": component_id, "commit_oid": commit_oid, "stderr": cherry.stderr[-4000:]})
            tree_oid = _git_text(aggregate, ("write-tree",), env=env, runner=runner)
            parent_tree = _git_text(aggregate, ("rev-parse", f"{current}^{{tree}}"), env=env, runner=runner)
            if tree_oid != parent_tree:
                current = _run_git(aggregate, ("commit-tree", tree_oid, "-p", current, "-m", f"Integrate component {component_id}"), env=commit_env, runner=runner).stdout.strip()
                _run_git(aggregate, ("reset", "--hard", current), env=env, runner=runner)
            integrated.append({"component_id": component_id, "source_head_oid": head_oid, "aggregate_head_oid": current, "commit_count": len(commits)})
        receipt = {"schema": "codex.chatgpt.deterministic-integration/v1", "baseline_oid": baseline_oid, "integration_head_oid": current, "components": integrated}
        receipt["deterministic_integration_sha256"] = canonical_sha256(receipt)
        return receipt
    except Exception:
        if aggregate.exists():
            _run_git(aggregate, ("reset", "--hard"), env=env, runner=runner, check=False)
        raise


def ff_only_apply(canonical_repo: str | os.PathLike[str], expected_baseline: Mapping[str, Any], target_oid: str, *, runner: Runner = default_runner) -> dict[str, Any]:
    root = Path(canonical_repo).resolve(strict=True)
    actual = canonical_snapshot(root, runner=runner)
    assert_snapshot_equal(expected_baseline, actual)
    env = isolated_git_env(canonical_git_home(root))
    head = str(actual["head_oid"])
    ancestor = _run_git(root, ("merge-base", "--is-ancestor", head, target_oid), env=env, runner=runner, check=False)
    if ancestor.returncode != 0:
        raise GitIsolationError("CANONICAL_APPLY_NOT_FAST_FORWARD", "target is not a descendant of canonical baseline")
    branch_ref = _git_text(root, ("symbolic-ref", "-q", "HEAD"), env=env, runner=runner)
    if not branch_ref.startswith("refs/heads/"):
        raise GitIsolationError("CANONICAL_HEAD_DETACHED", "ff-only apply requires an attached canonical branch")
    _run_git(root, ("update-ref", branch_ref, target_oid, head), env=env, runner=runner)
    _run_git(root, ("reset", "--hard", target_oid), env=env, runner=runner)
    result = {"schema": "codex.chatgpt.ff-only-apply-receipt/v1", "baseline_oid": head, "target_oid": target_oid, "branch_ref": branch_ref, "post_apply_tree_oid": _git_text(root, ("rev-parse", "HEAD^{tree}"), env=env, runner=runner)}
    result["ff_only_apply_receipt_sha256"] = canonical_sha256(result)
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(description="Host-only Git isolation helper")
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("repo")
    clone = sub.add_parser("clone")
    clone.add_argument("canonical_repo")
    clone.add_argument("staging_repo")
    args = parser.parse_args()
    try:
        result = canonical_snapshot(args.repo) if args.command == "snapshot" else safe_clone(args.canonical_repo, args.staging_repo)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, sort_keys=True))
        return 0
    except GitIsolationError as exc:
        print(json.dumps({"ok": False, "error": {"errorCode": exc.code, "message": str(exc), "evidence": exc.evidence}}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
