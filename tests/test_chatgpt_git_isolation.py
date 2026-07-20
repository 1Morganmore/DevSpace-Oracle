from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_git_isolation.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_git_isolation_test", MODULE_PATH)
assert SPEC and SPEC.loader
GITISO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GITISO
SPEC.loader.exec_module(GITISO)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git unavailable")


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "canonical"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "baseline")
    return repo


def test_snapshot_does_not_modify_canonical_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    before = git(repo, "status", "--porcelain=v2", "--untracked-files=all")
    snapshot = GITISO.canonical_snapshot(repo)
    after = git(repo, "status", "--porcelain=v2", "--untracked-files=all")
    assert before == after == ""
    assert snapshot["status_empty"] is True
    assert len(snapshot["baseline_identity_sha256"]) == 64
    assert not (repo / ".codexpro-isolated-home").exists()


def test_clone_uses_independent_object_store_and_exact_flags(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    staging = tmp_path / "staging"
    receipt = GITISO.safe_clone(repo, staging)
    assert receipt["clone_argv"][2:5] == ["--no-local", "--no-hardlinks", "--no-checkout"]
    common = Path(receipt["staging_common_git_dir"])
    assert not (common / "objects" / "info" / "alternates").exists()
    assert receipt["source_head_oid"] == receipt["staging_head_oid"]


def test_unit_diff_enforces_claims_and_host_creates_commit(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    baseline = git(repo, "rev-parse", "HEAD")
    staging = tmp_path / "staging"
    GITISO.safe_clone(repo, staging)
    unit = tmp_path / "unit"
    GITISO.create_unit_worktree(staging, unit, baseline)
    (unit / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    validation = GITISO.validate_unit_diff(unit, ["src/a.py"])
    assert validation["changes"][0]["path"] == "src/a.py"
    commit = GITISO.deterministic_commit(unit, parent_oid=baseline, unit_id="u-a", message="Implement a")
    assert git(unit, "rev-parse", "HEAD^") == baseline
    assert commit["commit_oid"] == git(unit, "rev-parse", "HEAD")


def test_out_of_scope_and_git_metadata_changes_are_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    baseline = git(repo, "rev-parse", "HEAD")
    staging = tmp_path / "staging"
    GITISO.safe_clone(repo, staging)
    unit = tmp_path / "unit"
    GITISO.create_unit_worktree(staging, unit, baseline)
    (unit / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
    with pytest.raises(GITISO.GitIsolationError) as exc:
        GITISO.validate_unit_diff(unit, ["src/a.py"])
    assert exc.value.code == "UNIT_DIFF_OUT_OF_SCOPE"
    with pytest.raises(GITISO.GitIsolationError) as exc2:
        GITISO.validate_unit_diff(unit, [".git/config"])
    assert exc2.value.code == "IMPLEMENTATION_GIT_METADATA_FORBIDDEN"


def test_deterministic_component_integration_import_and_ff_only_apply(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    baseline_snapshot = GITISO.canonical_snapshot(repo)
    baseline = baseline_snapshot["head_oid"]
    staging = tmp_path / "staging"
    GITISO.safe_clone(repo, staging)

    first = tmp_path / "u1"
    GITISO.create_unit_worktree(staging, first, baseline)
    (first / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    GITISO.validate_unit_diff(first, ["src/a.py"])
    c1 = GITISO.deterministic_commit(first, parent_oid=baseline, unit_id="u1", message="u1")["commit_oid"]

    second = tmp_path / "u2"
    GITISO.create_unit_worktree(staging, second, baseline)
    (second / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
    GITISO.validate_unit_diff(second, ["src/b.py"])
    c2 = GITISO.deterministic_commit(second, parent_oid=baseline, unit_id="u2", message="u2")["commit_oid"]

    aggregate = tmp_path / "aggregate"
    integration = GITISO.integrate_component_heads(staging, aggregate, baseline, {"component-b": c2, "component-a": c1})
    target = integration["integration_head_oid"]
    imported = GITISO.import_integration_ref(repo, staging, target, "refs/codexpro/parallel/test")
    assert imported["target_oid"] == target
    applied = GITISO.ff_only_apply(repo, baseline_snapshot, target)
    assert applied["target_oid"] == git(repo, "rev-parse", "HEAD")
    assert (repo / "src" / "a.py").read_text(encoding="utf-8") == "a = 2\n"
    assert (repo / "src" / "b.py").read_text(encoding="utf-8") == "b = 2\n"
