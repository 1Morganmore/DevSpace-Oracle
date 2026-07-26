from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_compat.py"


def load_compat():
    name = "chatgpt_oracle_compat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_exact_version_patch_is_hash_gated_idempotent_and_backed_up(tmp_path: Path) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "0.16.1"}), encoding="utf-8")
    target = package / "sample.txt"
    target.write_bytes(b"before\n")
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "sample.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n",
        encoding="utf-8",
    )
    compat.PATCHES = {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }
    compat.patch_root = lambda: patches
    backup = tmp_path / "backup"

    first = compat.ensure_oracle_compatibility("oracle 0.16.1", package_root=package, backup_root=backup)
    second = compat.ensure_oracle_compatibility("oracle 0.16.1", package_root=package, backup_root=backup)

    assert first["changed"] == ["sample.txt"]
    assert second["already_patched"] == ["sample.txt"]
    assert target.read_bytes() == b"after\n"
    assert (backup / "sample.txt").read_bytes() == b"before\n"


def test_unknown_oracle_version_or_file_hash_fails_closed(tmp_path: Path) -> None:
    compat = load_compat()
    with pytest.raises(compat.OracleCompatError) as version:
        compat.ensure_oracle_compatibility("oracle 0.17.0", package_root=tmp_path)
    assert version.value.code == "ORACLE_VERSION_UNVALIDATED"

    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "0.16.1"}), encoding="utf-8")
    (package / "sample.txt").write_bytes(b"unknown\n")
    compat.PATCHES = {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }
    with pytest.raises(compat.OracleCompatError) as mismatch:
        compat.ensure_oracle_compatibility("oracle 0.16.1", package_root=package)
    assert mismatch.value.code == "ORACLE_FILE_HASH_MISMATCH"


def test_all_matching_npx_cache_roots_are_patched_and_legacy_is_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = load_compat()
    roots = [tmp_path / "cache-new", tmp_path / "cache-old"]
    for root in roots:
        root.mkdir()
        (root / "package.json").write_text(json.dumps({"version": "0.16.1"}), encoding="utf-8")
    (roots[0] / "sample.txt").write_bytes(b"before\n")
    (roots[1] / "sample.txt").write_bytes(b"legacy\n")
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "sample.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n",
        encoding="utf-8",
    )
    compat.PATCHES = {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
            "legacy_patched": [digest(b"legacy\n")],
        }
    }
    compat.patch_root = lambda: patches
    monkeypatch.setattr(compat, "_candidate_roots", lambda: roots)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "sample.txt").write_bytes(b"before\n")

    result = compat.ensure_oracle_compatibility("oracle 0.16.1", backup_root=backup)

    assert result["package_roots"] == [str(root) for root in roots]
    assert all((root / "sample.txt").read_bytes() == b"after\n" for root in roots)
    assert len(result["changed"]) == 2


def test_prompt_composer_app_pill_probe_uses_the_composer_form_scope() -> None:
    patch = (
        MODULE_PATH.parent
        / "oracle-compat"
        / "0.16.1"
        / "promptComposer.patch"
    ).read_text(encoding="utf-8")

    assert "root.closest('form') || root.parentElement || root" in patch
    assert "scope.querySelectorAll(" in patch
    assert "Exact ChatGPT app suggestion could not be clicked." in patch
    assert "target.click();" in patch
    assert "group.querySelectorAll('*')" in patch
    assert "if (pill) return true;" in patch
    assert "return !Array.from(document.querySelectorAll(" in patch
    assert "App mention confirmation diagnostic:" in patch
    assert 'logDomFailure(runtime, logger, "app-mention-pill-missing")' in patch
    assert "diagnostic.result?.value ?? null" in patch
