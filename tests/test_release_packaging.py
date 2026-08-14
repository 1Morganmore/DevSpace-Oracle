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
        "bin/chatgpt_oracle_projects.py",
        "bin/chatgpt_oracle_multi.py",
        "bin/chatgpt_oracle_comprehensive.py",
        "bin/chatgpt_devspace_compat.py",
        "skills/chatgpt-pro-plan-handoff/SKILL.md",
        "skills/devspace-oracle-router/SKILL.md",
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
        "pro_transport": "oracle-pro-devspace",
        "pro_evidence_transport": "oracle-pro-attachment-only",
    }
    assert {"bin/", "skills/", "bin/*.py", "skills/**/scripts/*.py"}.isdisjoint(include | set(package["files"]))


def test_exact_external_versions_integrities_and_source_authority() -> None:
    external = values()[0]["external"]
    assert external["oracle"] == {
        "package": "@steipete/oracle",
        "tested_version": "0.17.3",
        "license": "MIT",
        "integrity": "sha512-xoziw8brto9rEtOROHcMr4vHu70DDGQJ41bwMHpkJgA77MIZ11B+IQtGqKpZ48WkihmHkEUVEvWsf+eDwxtwgg==",
        "installation": "npx -y @steipete/oracle@0.17.3",
        "repository": "steipete/oracle",
        "release_tag_convention": "v{version}",
    }
    assert external["devspace"]["tested_version"] == "1.0.7"
    assert external["devspace"]["integrity"] == "sha512-kP+Wk52qiMRwdqAP+nV4OZ4HU8feivZQ0k6u4ZUkvqxu8j0Rp/AU8H0K4T43G+zmu9WJKlYLTet7vIUeZHU72A=="
    assert external["devspace"]["repository"] == "waishnav/devspace"


def test_all_hash_gated_compatibility_patch_assets_are_installed() -> None:
    include = set(values()[0]["include"])
    oracle = runpy.run_path(str(ROOT / "bin/chatgpt_oracle_compat.py"))
    devspace = runpy.run_path(str(ROOT / "bin/chatgpt_devspace_compat.py"))

    def patch_names(contract: dict) -> list[str]:
        names = [contract["patch"]]
        if contract.get("legacy_patch"):
            names.append(contract["legacy_patch"])
        for value in contract.get("legacy_patches", {}).values():
            names.extend([value] if isinstance(value, str) else value)
        return names

    required = {
        f"bin/oracle-compat/{version}/{patch}"
        for version, patches in oracle["VERSION_PATCHES"].items()
        for contract in patches.values()
        for patch in patch_names(contract)
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
    assert package["version"] == lock["version"] == lock["packages"][""]["version"] == "1.9.0"
    assert package["engines"]["node"] == lock["packages"][""]["engines"]["node"] == ">=24 <27"
    assert package["repository"]["url"] == "git+https://github.com/1Morganmore/DevSpace-Oracle.git"


def test_release_workflow_runs_only_the_current_full_runner_after_install() -> None:
    workflow = (ROOT / ".github/workflows/release-portability.yml").read_text(encoding="utf-8")
    install = workflow.index("python -m pip install -r requirements-dev.txt")
    full = workflow.index("python scripts/run_release_contract_tests.py --full")
    assert install < full
    assert "python scripts/run_release_contract_tests.py --focused" not in workflow


def test_workflows_use_current_node24_action_majors() -> None:
    workflows = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / ".github/workflows").glob("*.yml")
    )
    for action in ("actions/checkout", "actions/setup-python", "actions/setup-node", "actions/upload-artifact"):
        if action in workflows:
            assert f"{action}@v7" in workflows
    assert "actions/checkout@v4" not in workflows
    assert "actions/setup-python@v5" not in workflows
    assert "actions/setup-node@v4" not in workflows
    assert "actions/upload-artifact@v4" not in workflows


def test_upstream_drift_workflow_is_separate_read_only_and_non_required() -> None:
    workflow = (ROOT / ".github/workflows/upstream-drift.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release-portability.yml").read_text(encoding="utf-8")
    assert "schedule:" in workflow and "workflow_dispatch:" in workflow
    assert "contents: read" in workflow and "check_upstream.py" in workflow
    for forbidden in ("pull-request", "git push", "git commit", "npm install"):
        assert forbidden not in workflow.casefold()
    assert "check_upstream.py" not in release


def test_parent_upstream_report_parses_tab_counts_and_flags_unaudited_head(monkeypatch) -> None:
    module = runpy.run_path(str(ROOT / "scripts/check_upstream.py"))
    check_parent = module["check_parent"]
    observed = "f" * 40

    def fake_git(*args: str) -> str:
        if args[0] == "rev-parse":
            return observed
        if args[0] == "merge-base":
            return "b" * 40
        if args[0] == "rev-list":
            return "26\t27"
        raise AssertionError(args)

    monkeypatch.setitem(check_parent.__globals__, "run_git", fake_git)
    monkeypatch.setitem(check_parent.__globals__, "default_branch_head", lambda _repository: observed)
    result = check_parent()

    assert (result["ahead"], result["behind"]) == (26, 27)
    assert result["repository"] == "ventianima-lab/codex-web-gpt-automation"
    assert result["vendored_parent_ref"] == "vendor/codex-web-main"
    assert result["audited_parent_head"] == "9bd6843ee9424b260cdc6968feace2bb46ef1ceb"
    assert result["last_integrated_donor"] == "9542abeef6aa544f4ee6af03bab61cef3474f9e4"
    assert result["vendored_head_audited"] is False
    assert {"PARENT_HEAD_UNAUDITED", "PARENT_AUDITED_HEAD_MISMATCH"} <= set(result["flags"])


def test_upstream_typescript_source_change_impacts_compiled_patch_target(monkeypatch) -> None:
    module = runpy.run_path(str(ROOT / "scripts/check_upstream.py"))
    check = module["check"]
    integrity = "sha512-exact"

    def fake_fetch(url: str) -> dict:
        if "registry.npmjs.org" in url:
            return {
                "dist-tags": {"latest": "0.18.0"},
                "versions": {"0.17.3": {"dist": {"integrity": integrity}}},
            }
        return {"files": [{"filename": "src/browser/actions/thinkingTime.ts"}]}

    monkeypatch.setitem(check.__globals__, "fetch", fake_fetch)
    monkeypatch.setitem(
        check.__globals__,
        "latest_tag",
        lambda _repository: "v0.18.0",
    )

    result = check(
        "oracle",
        {
            "package": "@steipete/oracle",
            "repository": "steipete/oracle",
            "tested_version": "0.17.3",
            "integrity": integrity,
            "release_tag_convention": "v{version}",
        },
        {"dist/src/browser/actions/thinkingTime.js"},
    )

    assert result["impacted_patch_targets"] == [
        "dist/src/browser/actions/thinkingTime.js"
    ]


def test_upstream_drift_tracks_the_active_oracle_patch_map() -> None:
    module = runpy.run_path(str(ROOT / "scripts/check_upstream.py"))
    compat = runpy.run_path(str(ROOT / "bin/chatgpt_oracle_compat.py"))
    expected = set(compat["VERSION_PATCHES"][compat["SUPPORTED_VERSION"]])
    assignment = "PATCHES_" + compat["SUPPORTED_VERSION"].replace(".", "")
    assert module["patch_targets"](
        ROOT / "bin/chatgpt_oracle_compat.py", assignment
    ) == expected
    assert expected == set(compat["PATCHES"])


def test_public_notices_and_no_vendoring() -> None:
    notice = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "@steipete/oracle" in notice and "@waishnav/devspace" in notice
    assert not any((ROOT / name).exists() for name in ("node_modules", "browser"))
