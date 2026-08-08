#!/usr/bin/env python
"""Read-only Oracle and DevSpace upstream drift report."""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Exact latest audited parent-project donor commit (see docs/VS_UPSTREAM.md).
PARENT_DONOR = "9542abeef6aa544f4ee6af03bab61cef3474f9e4"
PARENT_REPOSITORY = "ventianima-lab/codexpro-automation"
PARENT_VENDOR_REF = "vendor/codexpro-main"


def fetch(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "DevSpace-Oracle-drift-check"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def patch_targets(module: Path, assignment: str) -> set[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == assignment for target in node.targets):
            value = ast.literal_eval(node.value)
            return set(value)
    raise ValueError(f"{assignment} is not a literal mapping in {module}")


def latest_tag(repository: str) -> str:
    base = f"https://api.github.com/repos/{repository}"
    try:
        value = fetch(f"{base}/releases/latest")
        return str(value["tag_name"])
    except (urllib.error.HTTPError, KeyError):
        values = fetch(f"{base}/tags?per_page=1")
        if not values:
            raise ValueError(f"no release or tag for {repository}")
        return str(values[0]["name"])


def default_branch_head(repository: str) -> str:
    value = fetch(f"https://api.github.com/repos/{repository}")
    branch = str(value["default_branch"])
    head = fetch(f"https://api.github.com/repos/{repository}/commits/{urllib.parse.quote(branch, safe='')}")
    return str(head["sha"])


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
    if counts and " " in counts:
        parts = counts.split()
        try:
            ahead, behind = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            ahead = behind = None
    flags: list[str] = []
    live_head: str | None = None
    try:
        live_head = default_branch_head(PARENT_REPOSITORY)
    except Exception as exc:  # advisory checker must keep the rest of the report
        flags.append(f"PARENT_HEAD_UNREACHABLE: {type(exc).__name__}")
    if vendored is None:
        flags.append("PARENT_VENDOR_REF_ABSENT")
    elif live_head and vendored != live_head:
        flags.append("PARENT_VENDOR_REF_STALE")
    if merge_base is None:
        flags.append("PARENT_MERGE_BASE_UNAVAILABLE")
    return {
        "name": "parent",
        "repository": PARENT_REPOSITORY,
        "audited_donor": PARENT_DONOR,
        "live_parent_head": live_head,
        "vendored_parent_ref": PARENT_VENDOR_REF,
        "vendored_parent_head": vendored,
        "merge_base": merge_base,
        "ahead": ahead,
        "behind": behind,
        "donor_audited": bool(vendored and PARENT_DONOR.startswith(vendored[:12])),
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
    latest = str(registry["dist-tags"]["latest"])
    tested_integrity = str(registry["versions"][tested]["dist"]["integrity"])
    tag = latest_tag(repository)
    changed: list[str] = []
    if latest != tested:
        base_tag = str(contract["release_tag_convention"]).format(version=tested)
        compare = fetch(f"https://api.github.com/repos/{repository}/compare/{urllib.parse.quote(base_tag, safe='')}...{urllib.parse.quote(tag, safe='')}")
        changed = [str(item["filename"]) for item in compare.get("files", [])]
    impacted = sorted(target for target in targets if target in changed or target.removeprefix("dist/") in changed)
    flags: list[str] = []
    if tested_integrity != contract["integrity"]:
        flags.append("INTEGRITY_DRIFT")
    if normalize_tag(tag) != latest:
        flags.append("TAG_REGISTRY_MISMATCH")
    if latest != tested:
        flags.append("VERSION_DRIFT")
    if impacted:
        flags.append("SOURCE_IMPACT")
    return {
        "name": name,
        "package": package,
        "tested_version": tested,
        "latest_version": latest,
        "tested_integrity": tested_integrity,
        "manifest_integrity": contract["integrity"],
        "latest_tag": tag,
        "status": flags[-1] if flags else "CURRENT",
        "flags": flags or ["CURRENT"],
        "changed_files": changed,
        "patch_targets": sorted(targets),
        "impacted_patch_targets": impacted,
        "manual_validation": ["download exact npm tarball", "calculate pristine hashes", "dry-apply existing patches", "review every impacted patch target"] if flags else [],
    }


def report() -> dict[str, Any]:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    external = manifest["external"]
    specs = {
        "oracle": (external["oracle"], patch_targets(ROOT / "bin/chatgpt_oracle_compat.py", "PATCHES_0171")),
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
    return {"schema": "devspace-oracle.upstream-drift/v1", "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("human", "json", "both"), default="both")
    args = parser.parse_args()
    value = report()
    if args.format in {"human", "both"}:
        for item in value["results"]:
            print(f"{item['name']}: {item['status']} ({', '.join(item['flags'])})")
            if item.get("impacted_patch_targets"):
                print("  impacted: " + ", ".join(item["impacted_patch_targets"]))
            if item.get("name") == "parent":
                print(
                    "  parent HEAD: " + str(item.get("vendored_parent_head") or "absent")
                    + " merge-base: " + str(item.get("merge_base") or "unavailable")
                    + f" ahead/behind: {item.get('ahead')}/{item.get('behind')}"
                )
            if item.get("error"):
                print("  " + item["error"])
    if args.format in {"json", "both"}:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
