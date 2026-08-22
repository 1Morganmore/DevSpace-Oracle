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
    shipped = include | set(package["files"])
    assert not any("ultra-gpt-mode" in path for path in shipped)
    assert not (ROOT / "skills/ultra-gpt-mode").exists()


def test_exact_external_versions_integrities_and_source_authority() -> None:
    external = values()[0]["external"]
    assert external["oracle"] == {
        "package": "@steipete/oracle",
        "tested_version": "0.18.0",
        "license": "MIT",
        "integrity": "sha512-o8KFd66zNt36jw5zdtQAV74bgrOlJibbyvnLsVikIWDamesYtez/dIUhQ4zqtD9jkx+7A6vcP9+JgcJt0H5pOw==",
        "installation": "npx -y @steipete/oracle@0.18.0",
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
    manifest, package = values()
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package["private"] is False
    assert package["name"] == lock["name"] == lock["packages"][""]["name"]
    assert (
        manifest["version"]
        == package["version"]
        == lock["version"]
        == lock["packages"][""]["version"]
        == "1.10.0"
    )
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
    monkeypatch.setitem(
        check_parent.__globals__,
        "default_branch_identity",
        lambda _repository: {"name": "main", "head": observed},
    )
    result = check_parent()

    assert (result["ahead"], result["behind"]) == (26, 27)
    assert result["repository"] == "ventianima-lab/codex-web-gpt-automation"
    assert result["vendored_parent_ref"] == "vendor/codex-web-main"
    assert result["audited_parent_head"] == "bcf78feffb39970991adc64d3ff053338fdc2f7f"
    assert result["last_integrated_donor"] == "9542abeef6aa544f4ee6af03bab61cef3474f9e4"
    assert result["vendored_head_audited"] is False
    assert {"PARENT_HEAD_UNAUDITED", "PARENT_AUDITED_HEAD_MISMATCH"} <= set(result["flags"])


def test_upstream_report_keeps_npm_tag_release_and_default_branch_distinct(
    monkeypatch,
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/check_upstream.py"))
    check = module["check"]
    integrity = "sha512-exact"
    release_head = "a" * 40
    tag_object = "b" * 40
    calls: list[str] = []

    def fake_fetch(url: str) -> dict | list[dict]:
        calls.append(url)
        if "registry.npmjs.org" in url:
            return {
                "dist-tags": {"latest": "0.18.0"},
                "versions": {
                    "0.18.0": {
                        "gitHead": release_head,
                        "dist": {"integrity": integrity},
                    }
                },
            }
        if url.endswith("/tags?per_page=1"):
            return [{"name": "v0.18.0"}]
        if url.endswith("/git/ref/tags/v0.18.0"):
            return {
                "object": {
                    "type": "tag",
                    "sha": tag_object,
                    "url": "https://api.github.test/oracle-tag-object",
                }
            }
        if url == "https://api.github.test/oracle-tag-object":
            return {
                "sha": tag_object,
                "tag": "v0.18.0",
                "object": {"type": "commit", "sha": release_head},
                "verification": {"verified": True, "reason": "valid"},
            }
        if url.endswith("/releases/latest"):
            return {
                "id": 17,
                "tag_name": "v0.17.3",
                "target_commitish": "main",
                "published_at": "2026-08-13T17:04:58Z",
            }
        if url == "https://api.github.com/repos/steipete/oracle":
            return {"default_branch": "main"}
        if url.endswith("/commits/main"):
            return {"sha": release_head}
        raise AssertionError(url)

    monkeypatch.setitem(check.__globals__, "fetch", fake_fetch)
    result = check(
        "oracle",
        {
            "package": "@steipete/oracle",
            "repository": "steipete/oracle",
            "tested_version": "0.18.0",
            "integrity": integrity,
            "release_tag_convention": "v{version}",
        },
        {"dist/src/browser/actions/thinkingTime.js"},
    )

    assert result["npm"]["latest_version"] == "0.18.0"
    assert result["npm"]["latest_git_head"] == release_head
    assert result["source_tag"] == {
        "name": "v0.18.0",
        "ref_target_type": "tag",
        "ref_target_sha": tag_object,
        "annotated": True,
        "tag_object_sha": tag_object,
        "peeled_commit": release_head,
        "signature_verified": True,
        "signature_reason": "valid",
    }
    assert "latest_tag" not in result
    assert "latest_version" not in result
    assert result["github_release"]["tag_name"] == "v0.17.3"
    assert result["default_branch"] == {"name": "main", "head": release_head}
    assert result["flags"] == ["GITHUB_RELEASE_NPM_VERSION_MISMATCH"]
    assert any("/git/ref/tags/v0.18.0" in url for url in calls)
    assert any(url.endswith("/tags?per_page=1") for url in calls)
    assert any(url.endswith("/releases/latest") for url in calls)


def test_upstream_typescript_source_change_impacts_compiled_patch_target(
    monkeypatch,
) -> None:
    module = runpy.run_path(str(ROOT / "scripts/check_upstream.py"))
    check = module["check"]
    tested_integrity = "sha512-tested"
    release_head = "c" * 40
    tag_object = "d" * 40

    def fake_fetch(url: str) -> dict | list[dict]:
        if "registry.npmjs.org" in url:
            return {
                "dist-tags": {"latest": "0.18.0"},
                "versions": {
                    "0.17.3": {"dist": {"integrity": tested_integrity}},
                    "0.18.0": {
                        "gitHead": release_head,
                        "dist": {"integrity": "sha512-latest"},
                    },
                },
            }
        if url.endswith("/tags?per_page=1"):
            return [{"name": "v0.18.0"}]
        if url.endswith("/git/ref/tags/v0.18.0"):
            return {
                "object": {
                    "type": "tag",
                    "sha": tag_object,
                    "url": "https://api.github.test/published-tag-object",
                }
            }
        if url == "https://api.github.test/published-tag-object":
            return {
                "tag": "v0.18.0",
                "object": {"type": "commit", "sha": release_head},
                "verification": {"verified": True, "reason": "valid"},
            }
        if url.endswith("/releases/latest"):
            return {
                "tag_name": "v0.18.0",
                "target_commitish": "main",
                "published_at": "2026-08-20T00:00:00Z",
            }
        if url == "https://api.github.com/repos/steipete/oracle":
            return {"default_branch": "main"}
        if url.endswith("/commits/main"):
            return {"sha": release_head}
        if "/compare/" in url:
            return {
                "files": [
                    {"filename": "src/browser/actions/thinkingTime.ts"}
                ]
            }
        raise AssertionError(url)

    monkeypatch.setitem(check.__globals__, "fetch", fake_fetch)
    result = check(
        "oracle",
        {
            "package": "@steipete/oracle",
            "repository": "steipete/oracle",
            "tested_version": "0.17.3",
            "integrity": tested_integrity,
            "release_tag_convention": "v{version}",
        },
        {"dist/src/browser/actions/thinkingTime.js"},
    )

    assert result["source_tag_impacted_patch_targets"] == [
        "dist/src/browser/actions/thinkingTime.js"
    ]
    assert result["default_branch_impacted_patch_targets"] == []
    assert {"NPM_VERSION_DRIFT", "SOURCE_TAG_SOURCE_IMPACT"} <= set(
        result["flags"]
    )


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
