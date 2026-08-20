#!/usr/bin/env python
"""Read-only independent upstream identity and compatibility drift report."""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Exact observed/audited parent HEAD and the last integrated donor are
# intentionally different (see docs/VS_UPSTREAM.md).
PARENT_AUDITED_HEAD = "731aec0a2d76c3c1c02815344accd118c177daff"
PARENT_LAST_INTEGRATED_DONOR = "9542abeef6aa544f4ee6af03bab61cef3474f9e4"
PARENT_REPOSITORY = "ventianima-lab/codex-web-gpt-automation"
PARENT_VENDOR_REF = "vendor/codex-web-main"


def fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "DevSpace-Oracle-drift-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def patch_targets(module: Path, assignment: str) -> set[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == assignment
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            return set(value)
    raise ValueError(f"{assignment} is not a literal mapping in {module}")


def github_release_identity(repository: str) -> dict[str, Any]:
    value = fetch(f"https://api.github.com/repos/{repository}/releases/latest")
    return {
        "id": value.get("id"),
        "tag_name": str(value["tag_name"]),
        "target_commitish": str(value.get("target_commitish") or ""),
        "published_at": value.get("published_at"),
        "draft": bool(value.get("draft")),
        "prerelease": bool(value.get("prerelease")),
    }


def source_tag_identity(repository: str, tag_name: str) -> dict[str, Any]:
    encoded_tag = urllib.parse.quote(tag_name, safe="")
    ref = fetch(f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded_tag}")
    target = ref["object"]
    target_type = str(target["type"])
    target_sha = str(target["sha"])
    identity: dict[str, Any] = {
        "name": tag_name,
        "ref_target_type": target_type,
        "ref_target_sha": target_sha,
        "annotated": target_type == "tag",
        "tag_object_sha": target_sha if target_type == "tag" else None,
        "peeled_commit": None,
        "signature_verified": False,
        "signature_reason": "lightweight-tag" if target_type == "commit" else None,
    }
    if target_type == "commit":
        identity["peeled_commit"] = target_sha
        return identity
    if target_type != "tag":
        raise ValueError(f"unsupported tag target type {target_type!r} for {repository}@{tag_name}")

    tag = fetch(str(target["url"]))
    if str(tag["tag"]) != tag_name:
        raise ValueError(f"tag object name mismatch for {repository}@{tag_name}")
    peeled = tag["object"]
    if str(peeled["type"]) != "commit":
        raise ValueError(f"tag object does not peel directly to a commit for {repository}@{tag_name}")
    verification = tag.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    identity["peeled_commit"] = str(peeled["sha"])
    identity["signature_verified"] = bool(verification.get("verified"))
    identity["signature_reason"] = str(verification.get("reason") or "unknown")
    return identity

def latest_source_tag_identity(repository: str) -> dict[str, Any]:
    values = fetch(f"https://api.github.com/repos/{repository}/tags?per_page=1")
    if not isinstance(values, list) or not values:
        raise ValueError(f"no source tag for {repository}")
    return source_tag_identity(repository, str(values[0]["name"]))


def default_branch_identity(repository: str) -> dict[str, str]:
    base = f"https://api.github.com/repos/{repository}"
    value = fetch(base)
    branch = str(value["default_branch"])
    head = fetch(f"{base}/commits/{urllib.parse.quote(branch, safe='')}")
    return {"name": branch, "head": str(head["sha"])}


def compare_files(repository: str, base_ref: str, head_ref: str) -> list[str]:
    if base_ref == head_ref:
        return []
    base = urllib.parse.quote(base_ref, safe="")
    head = urllib.parse.quote(head_ref, safe="")
    value = fetch(f"https://api.github.com/repos/{repository}/compare/{base}...{head}")
    return sorted({str(item["filename"]) for item in value.get("files", [])})


def impacted_targets(targets: set[str], changed_files: list[str]) -> list[str]:
    changed = set(changed_files)
    return sorted(
        target
        for target in targets
        if target in changed
        or target.removeprefix("dist/") in changed
        or target.removeprefix("dist/").removesuffix(".js") + ".ts" in changed
    )


def run_git(*args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def check_parent() -> dict[str, Any]:
    """Read-only advisory report against the parent project.

    Never fetches or mutates the local repository: the vendored ref, merge
    base, and ahead/behind counts are computed from the refs already present
    locally, and the live parent HEAD comes from the GitHub API.
    """
    vendored = run_git("rev-parse", "--verify", PARENT_VENDOR_REF)
    merge_base = run_git("merge-base", "HEAD", PARENT_VENDOR_REF) if vendored else None
    counts = run_git("rev-list", "--left-right", "--count", "HEAD...%s" % PARENT_VENDOR_REF) if vendored else None
    ahead: int | None = None
    behind: int | None = None
    if counts:
        parts = counts.split()
        try:
            ahead, behind = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            ahead = behind = None
    flags: list[str] = []
    live_head: str | None = None
    try:
        live_head = default_branch_identity(PARENT_REPOSITORY)["head"]
    except Exception as exc:  # advisory checker must keep the rest of the report
        flags.append(f"PARENT_HEAD_UNREACHABLE: {type(exc).__name__}")
    if live_head and live_head != PARENT_AUDITED_HEAD:
        flags.append("PARENT_HEAD_UNAUDITED")
    if vendored is None:
        flags.append("PARENT_VENDOR_REF_ABSENT")
    else:
        if live_head and vendored != live_head:
            flags.append("PARENT_VENDOR_REF_STALE")
        if vendored != PARENT_AUDITED_HEAD:
            flags.append("PARENT_AUDITED_HEAD_MISMATCH")
    if merge_base is None:
        flags.append("PARENT_MERGE_BASE_UNAVAILABLE")
    return {
        "name": "parent",
        "repository": PARENT_REPOSITORY,
        "audited_parent_head": PARENT_AUDITED_HEAD,
        "audited_donor": PARENT_AUDITED_HEAD,
        "last_integrated_donor": PARENT_LAST_INTEGRATED_DONOR,
        "live_parent_head": live_head,
        "vendored_parent_ref": PARENT_VENDOR_REF,
        "vendored_parent_head": vendored,
        "merge_base": merge_base,
        "ahead": ahead,
        "behind": behind,
        "vendored_head_audited": vendored == PARENT_AUDITED_HEAD,
        "donor_audited": vendored == PARENT_AUDITED_HEAD,
        "status": flags[-1] if flags else "CURRENT",
        "flags": flags or ["CURRENT"],
        "manual_validation": ["re-audit the donor chain before adopting any new parent commit"] if flags else [],
    }


def normalize_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def check(name: str, contract: dict[str, Any], targets: set[str]) -> dict[str, Any]:
    package = str(contract["package"])
    tested = str(contract["tested_version"])
    repository = str(contract["repository"])
    encoded = urllib.parse.quote(package, safe="")
    registry = fetch(f"https://registry.npmjs.org/{encoded}")
    versions = registry["versions"]
    npm_latest = str(registry["dist-tags"]["latest"])
    tested_metadata = versions[tested]
    latest_metadata = versions[npm_latest]
    tested_integrity = str(tested_metadata["dist"]["integrity"])
    npm_latest_integrity = str(latest_metadata["dist"]["integrity"])
    npm_latest_git_head = str(latest_metadata.get("gitHead") or "") or None
    tested_tag = str(contract["release_tag_convention"]).format(version=tested)

    flags: list[str] = []
    signal_errors: dict[str, str] = {}
    if tested_integrity != contract["integrity"]:
        flags.append("INTEGRITY_DRIFT")
    if npm_latest != tested:
        flags.append("NPM_VERSION_DRIFT")
    if npm_latest_git_head is None:
        flags.append("NPM_GIT_HEAD_MISSING")

    source_tag: dict[str, Any] | None = None
    try:
        source_tag = latest_source_tag_identity(repository)
    except Exception as exc:
        signal_errors["source_tag"] = f"{type(exc).__name__}: {exc}"
        flags.append("SOURCE_TAG_CHECK_FAILED")
    if source_tag is not None:
        if normalize_tag(str(source_tag["name"])) != npm_latest:
            flags.append("SOURCE_TAG_NPM_VERSION_MISMATCH")
        if (
            npm_latest_git_head
            and source_tag["peeled_commit"] != npm_latest_git_head
        ):
            flags.append("SOURCE_TAG_NPM_GIT_HEAD_MISMATCH")

    github_release: dict[str, Any] | None = None
    try:
        github_release = github_release_identity(repository)
    except Exception as exc:
        signal_errors["github_release"] = f"{type(exc).__name__}: {exc}"
        flags.append("GITHUB_RELEASE_CHECK_FAILED")
    if (
        github_release is not None
        and normalize_tag(str(github_release["tag_name"])) != npm_latest
    ):
        flags.append("GITHUB_RELEASE_NPM_VERSION_MISMATCH")

    default_branch: dict[str, str] | None = None
    try:
        default_branch = default_branch_identity(repository)
    except Exception as exc:
        signal_errors["default_branch"] = f"{type(exc).__name__}: {exc}"
        flags.append("DEFAULT_BRANCH_CHECK_FAILED")
    if (
        default_branch is not None
        and npm_latest_git_head
        and default_branch["head"] != npm_latest_git_head
    ):
        flags.append("DEFAULT_BRANCH_NPM_GIT_HEAD_MISMATCH")

    source_tag_changed_files: list[str] = []
    if (
        source_tag is not None
        and normalize_tag(str(source_tag["name"])) != tested
    ):
        try:
            source_tag_changed_files = compare_files(
                repository,
                tested_tag,
                str(source_tag["peeled_commit"]),
            )
        except Exception as exc:
            signal_errors["source_tag_compare"] = f"{type(exc).__name__}: {exc}"
            flags.append("SOURCE_TAG_COMPARE_CHECK_FAILED")

    default_branch_changed_files: list[str] = []
    if (
        source_tag is not None
        and default_branch is not None
        and source_tag["peeled_commit"] != default_branch["head"]
    ):
        try:
            default_branch_changed_files = compare_files(
                repository,
                str(source_tag["peeled_commit"]),
                default_branch["head"],
            )
        except Exception as exc:
            signal_errors["default_branch_compare"] = f"{type(exc).__name__}: {exc}"
            flags.append("DEFAULT_BRANCH_COMPARE_CHECK_FAILED")

    source_tag_impacted = impacted_targets(targets, source_tag_changed_files)
    default_branch_impacted = impacted_targets(
        targets,
        default_branch_changed_files,
    )
    if source_tag_impacted:
        flags.append("SOURCE_TAG_SOURCE_IMPACT")
    if default_branch_impacted:
        flags.append("DEFAULT_BRANCH_SOURCE_IMPACT")

    manual_validation: list[str] = []
    if flags:
        manual_validation.append(
            "review npm, source tag, GitHub Release, and default-branch identities independently"
        )
    if {
        "INTEGRITY_DRIFT",
        "NPM_VERSION_DRIFT",
        "SOURCE_TAG_SOURCE_IMPACT",
    } & set(flags):
        manual_validation.extend(
            [
                "download the exact npm tarball",
                "calculate pristine hashes",
                "dry-apply existing patches",
                "review every impacted patch target",
            ]
        )
    if "DEFAULT_BRANCH_NPM_GIT_HEAD_MISMATCH" in flags:
        manual_validation.append(
            "review unreleased default-branch changes without substituting them for npm bytes"
        )

    return {
        "name": name,
        "package": package,
        "repository": repository,
        "npm": {
            "tested_version": tested,
            "latest_version": npm_latest,
            "tested_integrity": tested_integrity,
            "manifest_integrity": contract["integrity"],
            "latest_integrity": npm_latest_integrity,
            "latest_git_head": npm_latest_git_head,
        },
        "source_tag": source_tag,
        "github_release": github_release,
        "default_branch": default_branch,
        "status": flags[-1] if flags else "CURRENT",
        "flags": flags or ["CURRENT"],
        "signal_errors": signal_errors,
        "source_tag_changed_files": source_tag_changed_files,
        "default_branch_changed_files": default_branch_changed_files,
        "patch_targets": sorted(targets),
        "source_tag_impacted_patch_targets": source_tag_impacted,
        "default_branch_impacted_patch_targets": default_branch_impacted,
        "manual_validation": manual_validation,
    }


def report() -> dict[str, Any]:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    external = manifest["external"]
    oracle_version = str(external["oracle"]["tested_version"]).replace(".", "")
    specs = {
        "oracle": (external["oracle"], patch_targets(ROOT / "bin/chatgpt_oracle_compat.py", f"PATCHES_{oracle_version}")),
        "devspace": (external["devspace"], patch_targets(ROOT / "bin/chatgpt_devspace_compat.py", "PATCHES")),
    }
    results = []
    for name, (contract, targets) in specs.items():
        try:
            results.append(check(name, contract, targets))
        except Exception as exc:  # advisory checker must preserve independent results
            results.append({"name": name, "status": "CHECK_FAILED", "flags": ["CHECK_FAILED"], "error": f"{type(exc).__name__}: {exc}"})
    try:
        results.append(check_parent())
    except Exception as exc:  # the parent check must never break the npm checks
        results.append({"name": "parent", "status": "CHECK_FAILED", "flags": ["CHECK_FAILED"], "error": f"{type(exc).__name__}: {exc}"})
    return {"schema": "devspace-oracle.upstream-drift/v2", "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("human", "json", "both"), default="both")
    args = parser.parse_args()
    value = report()
    if args.format in {"human", "both"}:
        for item in value["results"]:
            print(f"{item['name']}: {item['status']} ({', '.join(item['flags'])})")
            if item.get("name") != "parent" and item.get("npm"):
                npm = item["npm"]
                print(
                    "  npm tested/latest: "
                    + str(npm["tested_version"])
                    + "/"
                    + str(npm["latest_version"])
                    + " gitHead: "
                    + str(npm.get("latest_git_head") or "unavailable")
                )
                source_tag = item.get("source_tag")
                if source_tag:
                    print(
                        "  source tag: "
                        + str(source_tag["name"])
                        + " object: "
                        + str(source_tag.get("tag_object_sha") or source_tag["ref_target_sha"])
                        + " peeled: "
                        + str(source_tag.get("peeled_commit") or "unavailable")
                        + " signature-verified: "
                        + str(source_tag.get("signature_verified"))
                    )
                else:
                    print("  source tag: unavailable")
                release = item.get("github_release")
                print(
                    "  GitHub Release: "
                    + (
                        str(release["tag_name"])
                        + " published: "
                        + str(release.get("published_at") or "unknown")
                        if release
                        else "unavailable"
                    )
                )
                branch = item.get("default_branch")
                print(
                    "  default branch: "
                    + (
                        str(branch["name"]) + "@" + str(branch["head"])
                        if branch
                        else "unavailable"
                    )
                )
                if item.get("source_tag_impacted_patch_targets"):
                    print(
                        "  source-tag impacted: "
                        + ", ".join(item["source_tag_impacted_patch_targets"])
                    )
                if item.get("default_branch_impacted_patch_targets"):
                    print(
                        "  default-branch impacted: "
                        + ", ".join(item["default_branch_impacted_patch_targets"])
                    )
            if item.get("name") == "parent":
                print(
                    "  parent live/vendored: " + str(item.get("live_parent_head") or "unavailable")
                    + "/" + str(item.get("vendored_parent_head") or "absent")
                    + " audited: " + str(item.get("audited_parent_head"))
                    + " match: " + str(item.get("vendored_head_audited"))
                    + " last-integrated: " + str(item.get("last_integrated_donor"))
                    + " merge-base: " + str(item.get("merge_base") or "unavailable")
                    + f" ahead/behind: {item.get('ahead')}/{item.get('behind')}"
                )
            for signal, error in item.get("signal_errors", {}).items():
                print(f"  {signal}: {error}")
            if item.get("error"):
                print("  " + item["error"])
    if args.format in {"json", "both"}:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
