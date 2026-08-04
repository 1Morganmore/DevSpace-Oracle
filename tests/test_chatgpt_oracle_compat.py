from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_compat.py"
PRISTINE_PROMPT_COMPOSER = Path(__file__).parent / "fixtures/oracle-0.17.0/promptComposer.js"


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


def run_prompt_route_case(
    tmp_path: Path,
    *,
    suggestion: str | None,
    semantic_token: str | None,
    semantic_scope: str = "editor",
    semantic_visible: bool = True,
    fallback_overwrite: bool = False,
    semantic_identity_attr: str | None = "data-app-name",
    semantic_marker: str = "data-lexical-decorator",
    semantic_identity_value: str | None = None,
) -> tuple[dict[str, object], str]:
    compat = load_compat()
    relative = Path("dist/src/browser/actions/promptComposer.js")
    pristine = PRISTINE_PROMPT_COMPOSER
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(pristine, target)
    compat._apply_patch(package, compat.patch_root("0.17.0") / "promptComposer.patch")
    source = target.read_text(encoding="utf-8")
    source = source[source.index("const ENTER_KEY_EVENT") :]
    stubs = """
const INPUT_SELECTORS = ['#prompt-textarea'];
const PROMPT_PRIMARY_SELECTOR = '#prompt-textarea';
const PROMPT_FALLBACK_SELECTOR = 'textarea';
const SEND_BUTTON_SELECTORS = ['button[data-testid="send"]'];
const STOP_BUTTON_SELECTOR = 'button[data-testid="stop"]';
const ASSISTANT_ROLE_SELECTOR = '[data-message-author-role="assistant"]';
const buildConversationTurnCountExpression = () => '0';
const buildConversationTurnListExpression = () => '[]';
const delay = async () => {};
const logDomFailure = async () => {};
const buildClickDispatcher = () => '';
class BrowserAutomationError extends Error {
  constructor(message, details) {
    super(message);
    this.name = 'BrowserAutomationError';
    this.category = 'browser-automation';
    this.details = details;
  }
}
"""
    scenario = json.dumps(
        {
            "suggestion": suggestion,
            "semanticToken": semantic_token,
            "semanticScope": semantic_scope,
            "semanticVisible": semantic_visible,
            "fallbackOverwrite": fallback_overwrite,
            "semanticIdentityAttr": semantic_identity_attr,
            "semanticMarker": semantic_marker,
            "semanticIdentityValue": semantic_identity_value,
        }
    )
    harness = f"""
const scenario = {scenario};
let now = 0;
Date.now = () => (now += 1_000);
let suggestionClicks = 0;
let clearCount = 0;
let sendAttempts = 0;
let verificationCalls = 0;
let semanticTokenPresent = true;
const inserted = [];

class FakeNode {{
  constructor(text, attrs = {{}}, visible = true) {{
    this.innerText = text;
    this.textContent = text;
    this.attrs = attrs;
    this.visible = visible;
    this.children = [];
    this.tagName = 'SPAN';
    this.parentElement = null;
  }}
  getAttribute(name) {{ return this.attrs[name] ?? null; }}
  hasAttribute(name) {{ return Object.hasOwn(this.attrs, name); }}
  getBoundingClientRect() {{ return {{ width: this.visible ? 10 : 0, height: this.visible ? 10 : 0 }}; }}
  querySelectorAll() {{ return this.children; }}
  closest() {{ return this; }}
  click() {{ suggestionClicks += 1; }}
}}

const suggestionNode = scenario.suggestion === null ? null : new FakeNode(scenario.suggestion);
const tokenAttrs = {{
  [scenario.semanticMarker]: scenario.semanticMarker === 'contenteditable' ? 'false' : 'true',
}};
if (scenario.semanticIdentityAttr !== null)
  tokenAttrs[scenario.semanticIdentityAttr] =
    scenario.semanticIdentityValue ?? scenario.semanticToken;
const tokenNode = scenario.semanticToken === null
  ? null
  : new FakeNode(
      scenario.semanticToken,
      tokenAttrs,
      scenario.semanticVisible,
    );
const form = {{
  querySelectorAll: () => tokenNode && scenario.semanticScope === 'form' ? [tokenNode] : [],
}};
const composer = new FakeNode('');
composer.querySelectorAll = () =>
  semanticTokenPresent && tokenNode && scenario.semanticScope === 'editor' ? [tokenNode] : [];
composer.closest = () => form;
composer.parentElement = form;
const document = {{
  activeElement: composer,
  querySelectorAll(selector) {{
    if (selector.includes('[role="option"]')) return suggestionNode ? [suggestionNode] : [];
    if (selector.includes('#prompt-textarea')) return [composer];
    return [];
  }},
}};
const window = {{
  getComputedStyle(node) {{
    return {{ display: node.visible ? 'block' : 'none', visibility: 'visible', opacity: '1' }};
  }},
}};
const execute = (expression) =>
  Function('document', 'window', `return (${{expression}});`)(document, window);
const runtime = {{
  async evaluate({{ expression }}) {{
    if (expression.includes("document.readyState === 'complete'"))
      return {{ result: {{ value: {{ ready: true, composer: true }} }} }};
    if (expression.includes('return {{ cleared, remaining }}')) {{
      clearCount += 1;
      return {{ result: {{ value: {{ cleared: true, remaining: [] }} }} }};
    }}
    if (expression.includes('return {{ focused: true }}'))
      return {{ result: {{ value: {{ focused: true }} }} }};
    if (expression.includes('semanticMentionSelectors') || expression.includes('const exact = candidates.find') ||
        (expression.includes('[role="option"]') && expression.includes('.some((node) =>'))) {{
      return {{ result: {{ value: execute(expression) }} }};
    }}
    if (expression.includes('return {{ editors, active: describe(document.activeElement) }}'))
      return {{ result: {{ value: {{ editors: [], active: null }} }} }};
    if (expression.includes('__oracleAppApprovalWatcher'))
      return {{ result: {{ value: true }} }};
    if (expression.includes('fallback.value =')) {{
      semanticTokenPresent = false;
      return {{ result: {{ value: true }} }};
    }}
    if (expression.includes('activeValue: active ? readValue(active)')) {{
      verificationCalls += 1;
      const value = scenario.fallbackOverwrite && verificationCalls === 1
        ? ''
        : inserted.join('');
      return {{ result: {{ value: {{ editorText: value, fallbackValue: '', activeValue: value }} }} }};
    }}
    if (expression.includes("return {{ status: 'missing' }}")) {{
      sendAttempts += 1;
      return {{ result: {{ value: {{ status: 'clicked' }} }} }};
    }}
    if (expression.includes('turnsCount: normalizedTurns.length'))
      return {{ result: {{ value: {{ baseline: 0, lastMatched: true, hasNewTurn: true, turnsCount: 1 }} }} }};
    throw new Error(`Unexpected evaluate expression: ${{expression.slice(0, 100)}}`);
  }},
}};
const input = {{
  async insertText({{ text }}) {{ inserted.push(text); }},
  async dispatchKeyEvent() {{}},
}};
let error = null;
let result = null;
try {{
  result = await submitPrompt({{ runtime, input, baselineTurns: 0 }}, '@DevSpace mission', console.error);
}} catch (caught) {{
  error = {{
    name: caught.name,
    message: caught.message,
    category: caught.category,
    code: caught.details?.code,
  }};
  process.stderr.write(`${{caught.message}}\n`);
}}
console.log(JSON.stringify({{ error, result, suggestionClicks, clearCount, sendAttempts, inserted }}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module"],
        input=stubs + source + harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1]), completed.stderr


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
    roots = [tmp_path / "cache-old", tmp_path / "cache-new"]
    for root in roots:
        root.mkdir()
        (root / "package.json").write_text(json.dumps({"version": "0.17.0"}), encoding="utf-8")
    (roots[0] / "sample.txt").write_bytes(b"legacy\n")
    (roots[1] / "sample.txt").write_bytes(b"before\n")
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

    with pytest.raises(compat.OracleCompatError) as missing_backup:
        compat.ensure_oracle_compatibility("oracle 0.17.0", backup_root=backup)
    assert missing_backup.value.code == "ORACLE_LEGACY_PATCH_BACKUP_INVALID"
    assert (roots[0] / "sample.txt").read_bytes() == b"legacy\n"

    backup.mkdir()
    (backup / "sample.txt").write_bytes(b"before\n")

    result = compat.ensure_oracle_compatibility("oracle 0.17.0", backup_root=backup)

    assert result["package_roots"] == [str(root) for root in roots]
    assert all((root / "sample.txt").read_bytes() == b"after\n" for root in roots)
    assert len(result["changed"]) == 2


def test_prompt_composer_patch_applies_to_pristine_0170_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compat = load_compat()
    relative = "dist/src/browser/actions/promptComposer.js"
    contract = compat.VERSION_PATCHES["0.17.0"][relative]
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"0.17.0"}', encoding="utf-8")
    shutil.copy2(
        PRISTINE_PROMPT_COMPOSER,
        target,
    )
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.0": {relative: contract}})
    backup = tmp_path / "backup"

    first = compat.ensure_oracle_compatibility(
        "oracle 0.17.0", package_root=package, backup_root=backup
    )
    second = compat.ensure_oracle_compatibility(
        "oracle 0.17.0", package_root=package, backup_root=backup
    )

    assert first["changed"] == [relative]
    assert second["already_patched"] == [relative]
    assert compat.sha256_file(target) == contract["patched"]
    assert compat.sha256_file(backup / relative) == contract["pristine"]


def test_literal_devspace_without_semantic_token_clears_and_fails_before_send(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(tmp_path, suggestion=None, semantic_token=None)

    assert result["error"] == {
        "name": "BrowserAutomationError",
        "message": "APP_MENTION_ROUTE_UNCONFIRMED",
        "category": "browser-automation",
        "code": "APP_MENTION_ROUTE_UNCONFIRMED",
    }
    assert result["clearCount"] == 2
    assert result["sendAttempts"] == 0
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_exact_semantic_devspace_token_proceeds_when_transient_ui_is_absent(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(tmp_path, suggestion=None, semantic_token="DevSpace")

    assert result["error"] is None
    assert result["result"] == 1
    assert result["sendAttempts"] == 1
    assert result["inserted"] == ["@DevSpace", " mission"]
    assert "APP_MENTION_ROUTE_UNCONFIRMED" not in stderr


@pytest.mark.parametrize(
    "semantic_marker", ["data-lexical-decorator", "contenteditable"]
)
def test_generic_exact_text_semantic_nodes_cannot_authorize_send(
    tmp_path: Path, semantic_marker: str
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=None,
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_marker=semantic_marker,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


@pytest.mark.parametrize("identity_value", ["@DevSpace", " DevSpace", "DevSpace "])
def test_normalized_lookalike_app_identity_attributes_cannot_authorize_send(
    tmp_path: Path, identity_value: str
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=None,
        semantic_token="DevSpace",
        semantic_identity_value=identity_value,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_fallback_literal_overwrite_loses_semantic_route_and_fails_before_send(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=None,
        semantic_token="DevSpace",
        fallback_overwrite=True,
    )

    assert result["sendAttempts"] == 0
    assert result["clearCount"] == 2
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


@pytest.mark.parametrize(
    ("semantic_token", "semantic_scope", "semantic_visible"),
    [
        pytest.param("DevSpace", "form", True, id="outside-editor"),
        pytest.param("DevSpace", "editor", False, id="hidden"),
        pytest.param("DevSpace Helper", "editor", True, id="lookalike"),
    ],
)
def test_only_visible_exact_devspace_token_inside_editor_can_authorize_send(
    tmp_path: Path,
    semantic_token: str,
    semantic_scope: str,
    semantic_visible: bool,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token=semantic_token,
        semantic_scope=semantic_scope,
        semantic_visible=semantic_visible,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_devspace_lookalike_suggestion_is_rejected_before_send(tmp_path: Path) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace Helper",
        semantic_token=None,
    )

    assert result["suggestionClicks"] == 0
    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


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
        "dist/src/browser/actions/promptComposer.js": ("db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000", "8bba8fd9a663c4c404ccf479a0193672624c0e42afb3a3e04edf832a4d9820f6"),
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
    assert contracts["dist/src/browser/actions/promptComposer.js"]["legacy_patched"] == [
        "02874d0f2fcd0f45c2c50385893a210e2be5822e1831fa81b99944728ed1cb79",
        "99e4307ccdda8256e352d09b149f795ba0766584cd3fa838ea1adb22fd5b63ba",
        "71769d77b50d2c66bf281a6d70a965eaa0d43bfd23aa7c0c6645d774f95604fa",
        "7523e315eb6c6f29e5567a994084a39b73adf0adc1aecb013831885a3474e9b8",
        "f34821a5c4ac51d55bf2da0e0b8c2a8a3b3cafd3b9b6b6010726f0b032a5ece8",
    ]


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
