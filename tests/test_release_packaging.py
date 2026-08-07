from __future__ import annotations

import json
import runpy
from pathlib import Path


ROOT = Path(__file__).parents[1]


def values() -> tuple[dict, dict]:
    return (
        json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "package.json").read_text(encoding="utf-8")),
    )


def test_manifest_and_package_cover_the_active_release_surface() -> None:
    manifest, package = values()
    include = set(manifest["include"])
    required = {
        "bin/chatgpt_oracle_run.py",
        "bin/chatgpt_oracle_dispatch.py",
        "bin/chatgpt_oracle_multi.py",
        "bin/chatgpt_oracle_comprehensive.py",
        "bin/chatgpt_devspace_compat.py",
        "skills/chatgpt-pro-plan-handoff/SKILL.md",
        "scripts/run_release_contract_tests.py",
        "scripts/check_upstream.py",
        "contracts/install/*.json",
        "contracts/multi-gpt/*.json",
    }
    assert manifest["schema"] == "codexpro.install-manifest/v1"
    assert required <= include
    assert manifest["routing"] == {
        "new_work_engine": "oracle",
        "regular_workspace_transport": "devspace",
        "pro_transport": "oracle-attachment-only",
    }
    assert {"bin/", "skills/", "bin/*.py", "skills/**/scripts/*.py"}.isdisjoint(include | set(package["files"]))


def test_exact_external_versions_integrities_and_source_authority() -> None:
    external = values()[0]["external"]
    assert external["oracle"] == {
        "package": "@steipete/oracle",
        "tested_version": "0.17.1",
        "license": "MIT",
        "integrity": "sha512-bq4SqMvRtT5Im+R57UPSXTV5p/BFTU24OXgGXqx2ckABWFX9uLDuKeJLoOdfBm7RzllrzjrlSSGgiMsrrvh+9Q==",
        "installation": "npx -y @steipete/oracle@0.17.1",
        "repository": "steipete/oracle",
        "release_tag_convention": "v{version}",
    }
    assert external["devspace"]["tested_version"] == "1.0.6"
    assert external["devspace"]["integrity"] == "sha512-lLwUip5Wv1mwpEmAbpms7bourW5g0a0US1PDHCD2CITgCK6DnMTh5++6z8ODIEY+T30oxoTQlxdH4T+VkWlbNA=="
    assert external["devspace"]["repository"] == "waishnav/devspace"


def test_all_hash_gated_compatibility_patch_assets_are_installed() -> None:
    include = set(values()[0]["include"])
    oracle = runpy.run_path(str(ROOT / "bin/chatgpt_oracle_compat.py"))
    devspace = runpy.run_path(str(ROOT / "bin/chatgpt_devspace_compat.py"))
    required = {
        f"bin/oracle-compat/{version}/{contract['patch']}"
        for version, patches in oracle["VERSION_PATCHES"].items()
        for contract in patches.values()
    } | {
        f"bin/devspace-compat/{devspace['SUPPORTED_VERSION']}/{contract['patch']}"
        for contract in devspace["PATCHES"].values()
    }
    assert required <= include
    assert not [path for path in required if not (ROOT / path).is_file()]


def test_package_metadata_is_publishable_and_lockfile_matches() -> None:
    _, package = values()
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is False
    assert package["name"] == lock["name"] == lock["packages"][""]["name"]
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == "1.8.0"
    assert package["engines"]["node"] == lock["packages"][""]["engines"]["node"] == ">=24 <27"
    assert package["repository"]["url"] == "git+https://github.com/1Morganmore/DevSpace-Oracle.git"


def test_release_workflow_uses_only_the_current_focused_and_full_runner() -> None:
    workflow = (ROOT / ".github/workflows/release-portability.yml").read_text(encoding="utf-8")
    install = workflow.index("python -m pip install -r requirements-dev.txt")
    focused = workflow.index("python scripts/run_release_contract_tests.py --focused")
    full = workflow.index("python scripts/run_release_contract_tests.py --full")
    assert install < focused < full


def test_upstream_drift_workflow_is_separate_read_only_and_non_required() -> None:
    workflow = (ROOT / ".github/workflows/upstream-drift.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release-portability.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow and "workflow_dispatch:" in workflow
    assert "contents: read" in workflow and "check_upstream.py" in workflow
    for forbidden in ("pull-request", "git push", "git commit", "npm install"):
        assert forbidden not in workflow.casefold()
    assert "check_upstream.py" not in release


def test_public_notices_and_no_vendoring() -> None:
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "@steipete/oracle" in notice and "@waishnav/devspace" in notice
    assert not any((ROOT / name).exists() for name in ("node_modules", "browser"))
