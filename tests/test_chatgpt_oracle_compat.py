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


def test_exact_version_patch_is_hash_gated_idempotent_and_backed_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "0.17.0"}), encoding="utf-8")
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
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.0": {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }})
    monkeypatch.setattr(compat, "patch_root", lambda version: patches)
    backup = tmp_path / "backup"

    first = compat.ensure_oracle_compatibility("oracle 0.17.0", package_root=package, backup_root=backup)
    second = compat.ensure_oracle_compatibility("oracle 0.17.0", package_root=package, backup_root=backup)

    assert first["changed"] == ["sample.txt"]
    assert second["already_patched"] == ["sample.txt"]
    assert target.read_bytes() == b"after\n"
    assert (backup / "sample.txt").read_bytes() == b"before\n"


def test_unknown_oracle_version_or_file_hash_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = load_compat()
    with pytest.raises(compat.OracleCompatError) as version:
        compat.ensure_oracle_compatibility("oracle 0.18.0", package_root=tmp_path)
    assert version.value.code == "ORACLE_VERSION_UNVALIDATED"

    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "0.17.0"}), encoding="utf-8")
    (package / "sample.txt").write_bytes(b"unknown\n")
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.0": {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
        }
    }})
    monkeypatch.setattr(compat, "patch_root", lambda version: tmp_path)
    with pytest.raises(compat.OracleCompatError) as mismatch:
        compat.ensure_oracle_compatibility("oracle 0.17.0", package_root=package)
    assert mismatch.value.code == "ORACLE_FILE_HASH_MISMATCH"


def test_all_matching_npx_cache_roots_are_patched_and_legacy_is_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = load_compat()
    roots = [tmp_path / "cache-new", tmp_path / "cache-old"]
    for root in roots:
        root.mkdir()
        (root / "package.json").write_text(json.dumps({"version": "0.17.0"}), encoding="utf-8")
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
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.0": {
        "sample.txt": {
            "patch": "sample.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
            "legacy_patched": [digest(b"legacy\n")],
        }
    }})
    monkeypatch.setattr(compat, "patch_root", lambda version: patches)
    monkeypatch.setattr(compat, "_candidate_roots", lambda: roots)
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "sample.txt").write_bytes(b"before\n")

    result = compat.ensure_oracle_compatibility("oracle 0.17.0", backup_root=backup)

    assert result["package_roots"] == [str(root) for root in roots]
    assert all((root / "sample.txt").read_bytes() == b"after\n" for root in roots)
    assert len(result["changed"]) == 2


def test_prompt_composer_app_pill_probe_uses_the_composer_form_scope() -> None:
    compat = load_compat()
    patch = (
        compat.patch_root("0.17.0")
        / "promptComposer.patch"
    ).read_text(encoding="utf-8")

    assert "root.closest('form') || root.parentElement || root" in patch
    assert "scope.querySelectorAll(" in patch
    assert "target.click();" in patch
    assert "group.querySelectorAll('*')" in patch
    assert "if (pill) return true;" in patch
    assert "return !Array.from(document.querySelectorAll(" in patch
    assert "App mention confirmation diagnostic:" in patch
    assert 'logDomFailure(runtime, logger, "app-mention-pill-missing")' in patch
    assert "diagnostic.result?.value ?? null" in patch
    assert "__oracleAppApprovalWatcher" in patch
    assert "이 대화에 기억" in patch
    assert "remember for this chat" in patch
    assert "allowLabels.has" in patch


def test_app_mention_ui_observation_is_a_warning_not_a_hard_block() -> None:
    compat = load_compat()
    patch = (
        compat.patch_root("0.17.0")
        / "promptComposer.patch"
    ).read_text(encoding="utf-8")

    # The app is routed by the literal @name text in the submitted prompt, so an
    # unobservable suggestion overlay or pill must not fail the run.
    for removed in (
        'BrowserAutomationError("ChatGPT app mention suggestion did not appear."',
        'BrowserAutomationError("Exact ChatGPT app suggestion could not be clicked."',
        "BrowserAutomationError(`ChatGPT app mention was not confirmed in the composer",
    ):
        assert removed not in patch

    assert "let mentionUiConfirmed = true;" in patch
    assert patch.count("mentionUiConfirmed = false;") == 3
    assert "was sent as literal text without UI confirmation" in patch
    assert "confirmed in the composer.`" in patch


def test_oracle_0170_has_the_exact_eight_hash_gated_compatibility_patches() -> None:
    compat = load_compat()
    contracts = compat.VERSION_PATCHES["0.17.0"]

    assert compat.SUPPORTED_VERSION == "0.17.0"
    assert compat.RECOVERABLE_VERSIONS == ("0.16.1", "0.17.0")
    assert "dist/src/browser/actions/modelSelection.js" not in contracts
    assert {
        path: (contract["pristine"], contract["patched"])
        for path, contract in contracts.items()
    } == {
        "dist/src/browser/chromeLifecycle.js": ("55e9858f54fb625dbe349b64837be511694ec018074db66e1cca161f5d47182d", "d2d4842f11aff03ffe6f19840443ccf4cd02167334eb0cf9f67a5f78ef4e780c"),
        "dist/src/browser/recoverConversation.js": ("d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca", "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e"),
        "dist/src/browser/profileCopy.js": ("06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f", "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c"),
        "dist/src/cli/browserConfig.js": ("989f14399c8aa51913752306135e11d97e4f1c55b2baf984907f1b54959cc340", "bd18d11e4770fa5335c889b7856622f2da4199351ec65bc17a5ec1f472e2506f"),
        "dist/src/browser/index.js": ("335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11", "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895"),
        "dist/src/browser/actions/assistantResponse.js": ("0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d", "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4"),
        "dist/src/browser/actions/promptComposer.js": ("db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000", "02874d0f2fcd0f45c2c50385893a210e2be5822e1831fa81b99944728ed1cb79"),
        "dist/src/browser/actions/thinkingTime.js": ("9d0c8ae34d72c6ab5ca4176ba2ac2b8431fbb93d6d4e73c0cc02f5d2eb8863b7", "7d475ed81ccee29a5b4107ed166584bcd3b0266bfd25e02ca7743bf24301e7f0"),
    }

    patches = {
        path: (compat.patch_root("0.17.0") / contract["patch"]).read_text(encoding="utf-8")
        for path, contract in contracts.items()
    }
    assert 'process.platform === "win32"' in patches["dist/src/browser/profileCopy.js"]
    assert "options.browserManualLogin = false" in patches["dist/src/cli/browserConfig.js"]
    assert "config = { ...config, manualLogin: false" in patches["dist/src/browser/index.js"]
    assert "+      return matchesLevel(label);" in patches["dist/src/browser/actions/thinkingTime.js"]


def test_copy_profile_recovery_patch_reuses_only_the_persisted_profile_seed() -> None:
    compat = load_compat()
    contract = compat.VERSION_PATCHES["0.17.0"]["dist/src/browser/recoverConversation.js"]
    patch = (
        compat.patch_root("0.17.0")
        / contract["patch"]
    ).read_text(encoding="utf-8")

    assert "resolved.copyProfileSource" in patch
    assert "return copyProfileSource.trim();" in patch
    assert 'mkdtemp(path.join(os.tmpdir(), "oracle-recovery-"))' in patch
    assert "wrapEphemeralRecoveryChrome" in patch
    assert contract["pristine"] == "d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca"
    assert contract["patched"] == "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e"


def test_hidden_window_patch_supports_windows_without_headless_mode() -> None:
    compat = load_compat()
    contract = compat.VERSION_PATCHES["0.17.0"]["dist/src/browser/chromeLifecycle.js"]
    patch = (
        compat.patch_root("0.17.0")
        / contract["patch"]
    ).read_text(encoding="utf-8")

    assert 'process.platform === "win32"' in patch
    assert "--window-position=-32000,-32000" in patch
    assert contract["pristine"] == "55e9858f54fb625dbe349b64837be511694ec018074db66e1cca161f5d47182d"
    assert contract["patched"] == "d2d4842f11aff03ffe6f19840443ccf4cd02167334eb0cf9f67a5f78ef4e780c"


def test_browser_timeout_compat_patches_consume_one_overall_budget() -> None:
    compat = load_compat()
    index_contract = compat.VERSION_PATCHES["0.17.0"]["dist/src/browser/index.js"]
    index_patch = (
        compat.patch_root("0.17.0")
        / index_contract["patch"]
    ).read_text(encoding="utf-8")
    response_contract = compat.VERSION_PATCHES["0.17.0"]["dist/src/browser/actions/assistantResponse.js"]
    response_patch = (
        compat.patch_root("0.17.0")
        / response_contract["patch"]
    ).read_text(encoding="utf-8")

    assert "const startedAt = Date.now();" in index_patch
    assert "timeoutMs - (Date.now() - startedAt)" in index_patch
    assert "waitForAssistantResponse(Runtime, remainingMs" in index_patch
    assert index_patch.count("timeoutMs - (Date.now() - startedAt)") == 3
    assert index_patch.index("waitForResumedConversationHydration(Runtime, remainingMs") < index_patch.rindex(
        "timeoutMs - (Date.now() - startedAt)"
    ) < index_patch.index("waitForAssistantResponse(Runtime, remainingMs")
    assert "recoverAssistantResponse(Runtime, remainingMs" in response_patch
    assert "\n+                const recovered = await recoverAssistantResponse(Runtime, timeoutMs" not in response_patch
    assert index_contract["patched"] == "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895"
    assert response_contract["patched"] == "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4"
