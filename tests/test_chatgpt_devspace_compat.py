from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_devspace_compat.py"


def load_compat():
    name = "chatgpt_devspace_compat_test"
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_exact_devspace_patch_is_hash_gated_idempotent_and_backed_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "1.0.7"}), encoding="utf-8")
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
    monkeypatch.setenv("CODEX_DEVSPACE_COMPAT_STATE_ROOT", str(tmp_path / "state"))
    backup = tmp_path / "backup"

    first = compat.ensure_devspace_compatibility(package_root=package, backup_root=backup)
    second = compat.ensure_devspace_compatibility(package_root=package, backup_root=backup)
    confirmed = compat.confirm_service_restarted(
        package_root=package,
        service_probe=lambda port: {
            "pid": 22,
            "command_line": f"node {package / 'dist' / 'cli.js'} serve",
            "started_at_unix_ns": 2**63 - 1,
            "local_port": port,
        },
        sleep=lambda _: None,
    )
    third = compat.ensure_devspace_compatibility(package_root=package, backup_root=backup)

    assert first["changed"] == ["sample.txt"]
    assert first["service_restart_required"] is True
    assert second["already_patched"] == ["sample.txt"]
    assert second["service_restart_required"] is True
    assert confirmed["restart_marker_cleared"] is True
    assert third["service_restart_required"] is False
    assert target.read_bytes() == b"after\n"
    assert (backup / "sample.txt").read_bytes() == b"before\n"


def test_compatibility_inspection_requires_patched_exact_listener_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "1.0.7"}), encoding="utf-8")
    target = package / "sample.txt"
    target.write_bytes(b"after\n")
    compat.PATCHES = {
        "sample.txt": {
            "patch": "unused.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }
    monkeypatch.setenv("CODEX_DEVSPACE_COMPAT_STATE_ROOT", str(tmp_path / "state"))
    identity = {
        "pid": 22,
        "command_line": f"node {package / 'dist' / 'cli.js'} serve",
        "started_at_unix_ns": 1,
        "local_port": 7676,
    }

    ready = compat.inspect_devspace_compatibility(
        package_root=package,
        service_probe=lambda port: identity,
    )
    before = target.read_bytes()
    target.write_bytes(b"before\n")
    patch_required = compat.inspect_devspace_compatibility(
        package_root=package,
        service_probe=lambda port: identity,
    )
    target.write_bytes(b"unknown\n")
    drift = compat.inspect_devspace_compatibility(
        package_root=package,
        service_probe=lambda port: identity,
    )

    assert ready["ready"] is True
    assert ready["service_status"] == "match"
    assert patch_required["files"][0]["status"] == "patch_required"
    assert patch_required["ready"] is False
    assert drift["files"][0]["status"] == "drift"
    assert drift["ready"] is False
    assert before == b"after\n"
    assert not (tmp_path / "backup").exists()


def test_restart_confirmation_rejects_old_or_foreign_listener(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "1.0.7"}), encoding="utf-8")
    (package / "sample.txt").write_bytes(b"after\n")
    compat.PATCHES = {
        "sample.txt": {
            "patch": "unused.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }
    monkeypatch.setenv("CODEX_DEVSPACE_COMPAT_STATE_ROOT", str(tmp_path / "state"))
    marker = compat._write_restart_marker([package])
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    patched_at = int(marker_payload["created_at_unix_ns"])

    with pytest.raises(compat.DevSpaceCompatError) as old:
        compat.confirm_service_restarted(
            package_root=package,
            wait_timeout_seconds=0,
            service_probe=lambda port: {
                "pid": 1,
                "command_line": f"node {package / 'dist' / 'cli.js'} serve",
                "started_at_unix_ns": patched_at - 1,
            },
        )
    assert old.value.code == "DEVSPACE_RESTART_NOT_PROVEN"
    assert marker.is_file()

    with pytest.raises(compat.DevSpaceCompatError) as foreign:
        compat.confirm_service_restarted(
            package_root=package,
            wait_timeout_seconds=0,
            service_probe=lambda port: {
                "pid": 2,
                "command_line": "node other-server.js",
                "started_at_unix_ns": patched_at + 1,
            },
        )
    assert foreign.value.code == "DEVSPACE_SERVICE_IDENTITY_MISMATCH"
    assert marker.is_file()


def test_stop_service_requires_exact_devspace_identity() -> None:
    compat = load_compat()
    stopped: list[int] = []
    package = Path("C:/tested/devspace")
    result = compat.stop_exact_devspace_service(
        service_probe=lambda port: {
            "pid": 44,
            "command_line": f"node {package / 'dist' / 'cli.js'} serve",
            "started_at_unix_ns": 1,
        },
        stopper=stopped.append,
        package_roots=[package],
    )
    assert result["stopped"] is True
    assert stopped == [44]

    with pytest.raises(compat.DevSpaceCompatError) as foreign:
        compat.stop_exact_devspace_service(
            service_probe=lambda port: {
                "pid": 55,
                "command_line": "node unrelated.js",
                "started_at_unix_ns": 1,
            },
            stopper=stopped.append,
            package_roots=[package],
        )
    assert foreign.value.code == "DEVSPACE_SERVICE_IDENTITY_MISMATCH"


def test_service_identity_normalizes_npx_bin_parent_path() -> None:
    compat = load_compat()
    package = Path(r"C:\cache\node_modules\@waishnav\devspace")
    identity = {
        "pid": 44,
        "command_line": (
            r'"node" "C:\cache\node_modules\.bin\\..\@waishnav\devspace\dist\cli.js" serve'
        ),
        "started_at_unix_ns": 1,
    }

    assert compat._assert_devspace_service_identity(identity, [package]) is identity


def test_unknown_devspace_version_or_file_hash_fails_closed(tmp_path: Path) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "2.0.0"}), encoding="utf-8")
    with pytest.raises(compat.DevSpaceCompatError) as version:
        compat.ensure_devspace_compatibility(package_root=package)
    assert version.value.code == "DEVSPACE_VERSION_UNVALIDATED"

    (package / "package.json").write_text(json.dumps({"version": "1.0.7"}), encoding="utf-8")
    (package / "sample.txt").write_bytes(b"unknown\n")
    compat.PATCHES = {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }
    with pytest.raises(compat.DevSpaceCompatError) as mismatch:
        compat.ensure_devspace_compatibility(package_root=package)
    assert mismatch.value.code == "DEVSPACE_FILE_HASH_MISMATCH"


def test_bounded_workspace_patch_skips_transient_trees_and_batches_discovery() -> None:
    compat = load_compat()
    patch_path = (
        MODULE_PATH.parent
        / "devspace-compat"
        / compat.SUPPORTED_VERSION
        / "workspaces.patch"
    )
    patch = patch_path.read_text(encoding="utf-8")
    parsed = subprocess.run(
        ["git", "apply", "--numstat", "--", str(patch_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert parsed.returncode == 0, parsed.stderr
    assert compat.SUPPORTED_VERSION == "1.0.7"
    assert compat.PATCHES["dist/workspaces.js"]["pristine"] == (
        "e11517f291cac33e37a66e84aeb80e1664a5abd0b6eb1e9bdb933d84c186efad"
    )
    assert compat.PATCHES["dist/workspaces.js"]["patched"] == (
        "68a4c61ae0f509bd40d2a682e0b9bbbac72cb00dc96693f7646e6a535cc872ed"
    )
    assert 'entry.name.startsWith(".pytest-")' in patch
    assert '".tmp"' in patch
    assert '".venv"' in patch
    assert "const batchSize = 24" in patch
    assert "await Promise.all(batch.map" in patch


def test_oauth_discovery_patch_exposes_chatgpt_path_specific_metadata() -> None:
    compat = load_compat()
    patch_path = (
        MODULE_PATH.parent
        / "devspace-compat"
        / compat.SUPPORTED_VERSION
        / "server.patch"
    )
    patch = patch_path.read_text(encoding="utf-8")
    parsed = subprocess.run(
        ["git", "apply", "--numstat", "--", str(patch_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert parsed.returncode == 0, parsed.stderr
    assert compat.PATCHES["dist/server.js"]["pristine"] == (
        "42d340924421182eea7f2580f96c8d1d5aae459061a6a90804e6900905ef2d72"
    )
    assert compat.PATCHES["dist/server.js"]["patched"] == (
        "5bd899c33e5db3afd1f41eb220c6346ee27d29421fb58c47db498ae3b691a8f7"
    )
    assert 'req.path === "/.well-known/oauth-authorization-server/mcp"' in patch
    assert 'req.url = "/.well-known/oauth-authorization-server"' in patch
