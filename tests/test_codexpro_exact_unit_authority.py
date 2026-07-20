from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "codexpro_exact_unit_authority.py"
SPEC = importlib.util.spec_from_file_location("codexpro_exact_unit_authority_test", MODULE_PATH)
assert SPEC and SPEC.loader
AUTH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = AUTH
SPEC.loader.exec_module(AUTH)


def inputs(tmp_path: Path) -> dict[str, object]:
    state = tmp_path / "state"
    canonical = tmp_path / "canonical"
    state.mkdir()
    canonical.mkdir()
    values = AUTH.derive_parent_run_topology(state, "project", "parent", "component", "unit", "attempt")
    Path(values["parent_run_dir"]).mkdir(parents=True)
    return {
        "state_root": str(state),
        "canonical_project_key": "project",
        "parent_run_id": "parent",
        "component_id": "component",
        "unit_id": "unit",
        "attempt_id": "attempt",
        "canonical_project_root": str(canonical),
        "staging_common_git_dir": str(Path(values["staging_repo_root"]) / ".git"),
        "allowed_roots": [values["unit_workspace_root"]],
    }


def test_planned_topology_is_fixed_and_exact(tmp_path: Path) -> None:
    receipt = AUTH.validate_and_build(inputs(tmp_path), phase="planned")
    assert receipt["schema"] == "codexpro.exact-unit-topology-receipt/v1"
    assert Path(receipt["unit_workspace_root"]["logical"]).name.startswith("u-")
    assert len(Path(receipt["unit_workspace_root"]["logical"]).name) == 26
    assert receipt["allowed_roots"] == [receipt["unit_workspace_root"]["logical"]]
    assert len(receipt["topology_receipt_sha256"]) == 64


def test_allowed_roots_must_be_exact_singleton(tmp_path: Path) -> None:
    value = inputs(tmp_path)
    value["allowed_roots"] = [str(tmp_path)]
    with pytest.raises(AUTH.ExactUnitAuthorityError) as exc:
        AUTH.validate_and_build(value, phase="planned")
    assert exc.value.code == "EXACT_UNIT_ALLOWED_ROOTS_NOT_SINGLETON"


def test_canonical_ancestor_overlap_is_rejected(tmp_path: Path) -> None:
    value = inputs(tmp_path)
    value["canonical_project_root"] = str(value["state_root"])
    with pytest.raises(AUTH.ExactUnitAuthorityError) as exc:
        AUTH.validate_and_build(value, phase="planned")
    assert exc.value.code == "EXACT_UNIT_TOPOLOGY_OVERLAP"


def test_supplied_topology_cannot_override_derived_path(tmp_path: Path) -> None:
    value = inputs(tmp_path)
    value["unit_workspace_root"] = str(tmp_path / "chosen-by-worker")
    with pytest.raises(AUTH.ExactUnitAuthorityError) as exc:
        AUTH.validate_and_build(value, phase="planned")
    assert exc.value.code == "EXACT_UNIT_TOPOLOGY_DERIVATION_MISMATCH"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="symlink unavailable")
def test_reparse_or_symlink_escape_is_fail_closed(tmp_path: Path) -> None:
    value = inputs(tmp_path)
    topology = AUTH.derive_parent_run_topology(
        str(value["state_root"]), "project", "parent", "component", "unit", "attempt"
    )
    runtime = Path(topology["runtime_root"])
    runtime.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    worktrees = Path(topology["worktrees_root"])
    try:
        worktrees.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not permitted")
    with pytest.raises(AUTH.ExactUnitAuthorityError) as exc:
        AUTH.validate_and_build(value, phase="planned")
    assert exc.value.code == "EXACT_UNIT_REPARSE_ESCAPE"
