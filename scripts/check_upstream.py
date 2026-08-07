#!/usr/bin/env python
"""Read-only Oracle and DevSpace upstream drift report."""
from __future__ import annotations

import argparse
import ast
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


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
            if item.get("error"):
                print("  " + item["error"])
    if args.format in {"json", "both"}:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
