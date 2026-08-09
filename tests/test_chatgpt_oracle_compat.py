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
PRISTINE_THINKING_TIME = (
    Path(__file__).parent / "fixtures/oracle-0.17.1/thinkingTime.pristine.js"
).read_bytes()


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
    semantic_layout: str = "flat",
    pill_icon_text: str = "D",
    pill_icon_visible: bool = False,
    pill_editable: bool = False,
    suggestion_case: str = "option",
    diagnostic_stop_before_send: bool = False,
    trigger_key_event: bool = False,
    key_event_ignored: bool = False,
    caret_outside_editor: bool = False,
    prompt: str = "@DevSpace mission",
) -> tuple[dict[str, object], str]:
    compat = load_compat()
    relative = Path("dist/src/browser/actions/promptComposer.js")
    pristine = PRISTINE_PROMPT_COMPOSER
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    shutil.copy2(pristine, target)
    compat._apply_patch(package, compat.patch_root("0.17.1") / "promptComposer.patch")
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
            "semanticLayout": semantic_layout,
            "pillIconText": pill_icon_text,
            "pillIconVisible": pill_icon_visible,
            "pillEditable": pill_editable,
            "suggestionCase": suggestion_case,
            "triggerKeyEvent": trigger_key_event,
            "keyEventIgnored": key_event_ignored,
            "caretOutsideEditor": caret_outside_editor,
            "diagnosticStopBeforeSend": diagnostic_stop_before_send,
        }
    )
    harness = f"""
const scenario = {scenario};
if (scenario.diagnosticStopBeforeSend)
  process.env.ORACLE_APP_MENTION_DIAGNOSTIC_NO_SUBMISSION = '1';
else
  delete process.env.ORACLE_APP_MENTION_DIAGNOSTIC_NO_SUBMISSION;
let now = 0;
Date.now = () => (now += 1_000);
let suggestionClicks = 0;
let clearCount = 0;
let sendAttempts = 0;
let verificationCalls = 0;
let semanticTokenPresent = scenario.suggestion === null;
let suggestionObserved = false;
let initialRouteConfirmed = false;
let finalRouteConfirmed = false;
let routeChecks = 0;
let mentionKeyTriggered = false;
const censuses = [];
const inserted = [];
const keyEvents = [];

class FakeNode {{
  constructor(text, attrs = {{}}, visible = true) {{
    this.innerText = text;
    this.textContent = text;
    this.attrs = attrs;
    this.visible = visible;
    this.children = [];
    this.tagName = 'SPAN';
    this.parentElement = null;
    this.suggestionAction = false;
  }}
  getAttribute(name) {{ return this.attrs[name] ?? null; }}
  hasAttribute(name) {{ return Object.hasOwn(this.attrs, name); }}
  getBoundingClientRect() {{ return {{ width: this.visible ? 10 : 0, height: this.visible ? 10 : 0 }}; }}
  querySelectorAll() {{
    return this.children.flatMap((child) => [child, ...child.querySelectorAll('*')]);
  }}
  closest(selector) {{
    if (selector.includes('[contenteditable="false"]')) {{
      let current = this;
      while (current) {{
        if (current.getAttribute?.('contenteditable') === 'false') return current;
        current = current.parentElement;
      }}
      return null;
    }}
    let current = this;
    while (current) {{
      const role = current.getAttribute?.('role');
      if (current.tagName === 'BUTTON' || role === 'option' || role === 'menuitem' ||
          current.hasAttribute?.('data-radix-collection-item') ||
          (current.hasAttribute?.('data-fill') && current.hasAttribute?.('tabindex'))) {{
        return current;
      }}
      current = current.parentElement;
    }}
    return null;
  }}
  contains(node) {{ return node === this || this.querySelectorAll('*').includes(node); }}
  click() {{
    if (this.suggestionAction) {{
      suggestionClicks += 1;
      semanticTokenPresent = true;
    }}
  }}
}}

const liveGroup = (disabled = false, withDescription = false) => {{
  const secondaryText = withDescription
    ? `${{scenario.suggestion}} - Oracle`
    : scenario.suggestion;
  const group = new FakeNode(
    withDescription
      ? `Plugins ${{scenario.suggestion}} ${{secondaryText}}`
      : `${{scenario.suggestion}} ${{secondaryText}}`,
    {{ role: 'group' }},
  );
  const action = new FakeNode(
    `${{scenario.suggestion}} ${{secondaryText}}`,
    {{ 'data-fill': '', tabindex: '0', ...(disabled ? {{ 'aria-disabled': 'true' }} : {{}}) }},
  );
  action.tagName = 'DIV';
  action.suggestionAction = !disabled;
  const primary = new FakeNode(scenario.suggestion);
  const secondary = new FakeNode(secondaryText);
  primary.parentElement = action;
  secondary.parentElement = action;
  action.children = [primary, secondary];
  action.parentElement = group;
  if (withDescription) {{
    const category = new FakeNode('Plugins');
    category.parentElement = group;
    group.children = [category, action];
  }} else {{
    group.children = [action];
  }}
  return group;
}};

let suggestionSurfaces = [];
if (scenario.suggestion !== null && scenario.suggestionCase === 'option') {{
  const option = new FakeNode(scenario.suggestion, {{ role: 'option' }});
  option.suggestionAction = true;
  suggestionSurfaces = [option];
}} else if (scenario.suggestion !== null && scenario.suggestionCase !== 'outside-menu') {{
  const group = liveGroup(
    scenario.suggestionCase === 'disabled',
    scenario.suggestionCase === 'plugin-description',
  );
  if (scenario.suggestionCase === 'ambiguous') {{
    suggestionSurfaces = [group, liveGroup()];
  }} else if (scenario.suggestionCase === 'group-lookalike') {{
    const lookalike = new FakeNode(`${{scenario.suggestion}} Helper`);
    lookalike.parentElement = group;
    group.children.push(lookalike);
  }} else if (scenario.suggestionCase === 'nested-unrelated') {{
    const unrelated = new FakeNode('Settings');
    unrelated.parentElement = group;
    group.children.push(unrelated);
  }} else if (scenario.suggestionCase === 'hidden') {{
    group.visible = false;
  }}
  if (suggestionSurfaces.length === 0) suggestionSurfaces = [group];
}}
const tokenAttrs = {{
  [scenario.semanticMarker]: scenario.semanticMarker === 'contenteditable' ? 'false' : 'true',
}};
if (scenario.semanticIdentityAttr !== null)
  tokenAttrs[scenario.semanticIdentityAttr] =
    scenario.semanticIdentityValue ?? scenario.semanticToken;
let tokenNode = null;
let semanticNodes = [];
if (scenario.semanticToken !== null && scenario.semanticLayout === 'pill') {{
  const icon = new FakeNode(
    scenario.pillIconText,
    scenario.pillIconVisible ? {{}} : {{ 'aria-hidden': 'true' }},
    true,
  );
  const label = new FakeNode(scenario.semanticToken, {{}}, scenario.semanticVisible);
  tokenNode = new FakeNode(
    `${{scenario.pillIconText}} ${{scenario.semanticToken}}`,
    {{ contenteditable: scenario.pillEditable ? 'true' : 'false' }},
    true,
  );
  tokenNode.children = [icon, label];
  icon.parentElement = tokenNode;
  label.parentElement = tokenNode;
  semanticNodes = [tokenNode, icon, label];
}} else if (scenario.semanticToken !== null) {{
  tokenNode = new FakeNode(
    scenario.semanticToken,
    tokenAttrs,
    scenario.semanticVisible,
  );
  semanticNodes = [tokenNode];
}}
const toolControl = new FakeNode('', {{
  'data-testid': 'composer-tools-menu-button',
  'aria-label': 'Add files and more',
  'aria-haspopup': 'menu',
  'aria-expanded': 'false',
}});
toolControl.tagName = 'BUTTON';
const form = {{
  querySelectorAll: (selector) => selector.includes('button')
    ? [toolControl]
    : tokenNode && scenario.semanticScope === 'form' ? semanticNodes : [],
}};
const composer = new FakeNode('');
composer.querySelectorAll = () =>
  semanticTokenPresent && tokenNode && scenario.semanticScope === 'editor' ? semanticNodes : [];
composer.closest = () => form;
composer.parentElement = form;
const document = {{
  activeElement: composer,
  querySelectorAll(selector) {{
    if (selector.includes('[role="option"]')) {{
      if (scenario.keyEventIgnored) return [];
      if (scenario.triggerKeyEvent && !mentionKeyTriggered) return [];
      return suggestionSurfaces;
    }}
    if (selector.includes('#prompt-textarea')) return [composer];
    return [];
  }},
}};
const window = {{
  getComputedStyle(node) {{
    return {{ display: node.visible ? 'block' : 'none', visibility: 'visible', opacity: '1', pointerEvents: 'auto' }};
  }},
  getSelection: () => ({{ anchorNode: scenario.caretOutsideEditor ? form : composer }}),
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
    if (expression.includes('MENTION_CENSUS_STAGE')) {{
      const value = execute(expression);
      censuses.push(value);
      return {{ result: {{ value }} }};
    }}
    if (expression.includes('const surfaceSelector =')) {{
      const value = execute(expression);
      if (value?.status === 'unique') suggestionObserved = true;
      return {{ result: {{ value }} }};
    }}
    if (expression.includes('semanticMentionSelectors')) {{
      const value = execute(expression);
      routeChecks += 1;
      if (value === true && routeChecks === 1) initialRouteConfirmed = true;
      if (value === true && routeChecks > 1) finalRouteConfirmed = true;
      return {{ result: {{ value }} }};
    }}
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
  async insertText({{ text }}) {{
    inserted.push(text);
  }},
  async dispatchKeyEvent(event) {{
    keyEvents.push(event);
    if (event.type === 'keyDown' && event.text) {{
      inserted.push(event.text);
      if (scenario.triggerKeyEvent && event.text === '@') mentionKeyTriggered = true;
    }}
  }},
}};
let error = null;
let result = null;
try {{
  result = await submitPrompt({{ runtime, input, baselineTurns: 0 }}, {json.dumps(prompt)}, console.error);
}} catch (caught) {{
  error = {{
    name: caught.name,
    message: caught.message,
    category: caught.category,
    code: caught.details?.code,
  }};
  process.stderr.write(`${{caught.message}}\n`);
}}
console.log(JSON.stringify({{
  error,
  result,
  suggestionClicks,
  suggestionObserved,
  initialRouteConfirmed,
  finalRouteConfirmed,
  routeChecks,
  clearCount,
  sendAttempts,
  inserted,
  keyEvents,
  censuses,
}}));
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


def test_compatibility_inspection_classifies_hashes_without_writing(tmp_path: Path) -> None:
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": "0.17.0"}), encoding="utf-8")
    target = package / "sample.txt"
    compat.VERSION_PATCHES = {"0.17.0": {
        "sample.txt": {
            "patch": "unused.patch",
            "pristine": digest(b"before\n"),
            "patched": digest(b"after\n"),
            "legacy_patched": [digest(b"legacy\n")],
        }
    }}

    for content, expected in (
        (b"after\n", "patched"),
        (b"before\n", "patch_required"),
        (b"legacy\n", "legacy_patch_required"),
        (b"unknown\n", "drift"),
    ):
        target.write_bytes(content)
        before = target.read_bytes()
        result = compat.inspect_oracle_compatibility("oracle 0.17.0", package_root=package)
        assert result["files"][0]["status"] == expected
        assert result["ready"] is (expected == "patched")
        assert target.read_bytes() == before
        assert not (tmp_path / "backup").exists()


def test_canonical_hash_binds_both_lf_and_crlf_deployed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fresh npx installs keep LF bytes; older Windows deployments can carry
    # CRLF bytes for the same logical file.  One canonical (LF) contract hash
    # must recognize both flavors instead of accepting two ambiguous hashes.
    compat = load_compat()
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
    contract = {
        "patch": "sample.patch",
        "pristine": digest(b"before\n"),
        "patched": digest(b"after\n"),
    }
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {"sample.txt": contract}})
    monkeypatch.setattr(compat, "patch_root", lambda version: patches)

    for index, content in enumerate((b"before\n", b"before\r\n")):
        package = tmp_path / f"package-{index}"
        package.mkdir()
        (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
        (package / "sample.txt").write_bytes(content)
        result = compat.ensure_oracle_compatibility("oracle 0.17.1", package_root=package)
        assert result["changed"] == ["sample.txt"]
        assert compat.sha256_file(package / "sample.txt") == contract["patched"]
        assert compat.inspect_oracle_compatibility(
            "oracle 0.17.1", package_root=package
        )["ready"] is True


def test_unknown_oracle_version_or_file_hash_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_known_legacy_patch_chain_migrates_and_preserves_exact_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A former level produced by two stacked patches must restore pristine
    # bytes by reversing the chain in reverse order, keep an exact pristine
    # backup, and stay idempotent on the next run.
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    target = package / "sample.txt"
    target.write_bytes(b"legacy2\r\n")
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "step1.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-pristine\n"
        "+legacy1\n",
        encoding="utf-8",
    )
    (patches / "step2.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-legacy1\n"
        "+legacy2\n",
        encoding="utf-8",
    )
    (patches / "final.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-pristine\n"
        "+final\n",
        encoding="utf-8",
    )
    contract = {
        "patch": "final.patch",
        "pristine": digest(b"pristine\n"),
        "patched": digest(b"final\n"),
        "legacy_patched": [digest(b"legacy1\n"), digest(b"legacy2\n")],
        "legacy_patch": "step1.patch",
        "legacy_patches": {
            digest(b"legacy2\n"): ["step1.patch", "step2.patch"],
        },
    }
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {"sample.txt": contract}})
    monkeypatch.setattr(compat, "patch_root", lambda version: patches)
    backup = tmp_path / "backup"

    first = compat.ensure_oracle_compatibility(
        "oracle 0.17.1", package_root=package, backup_root=backup
    )
    second = compat.ensure_oracle_compatibility(
        "oracle 0.17.1", package_root=package, backup_root=backup
    )

    assert first["changed"] == ["sample.txt"]
    assert second["already_patched"] == ["sample.txt"]
    assert compat.sha256_file(target) == contract["patched"]
    assert compat.sha256_file(backup / "sample.txt") == contract["pristine"]
    assert (backup / "sample.txt").read_bytes() == b"pristine\n"


def test_known_legacy_patch_chain_without_any_backup_still_restores_pristine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The legacy level is restored from the exact known patches when no
    # pristine backup exists yet; an unknown level still fails closed.
    compat = load_compat()
    package = tmp_path / "package"
    package.mkdir()
    (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    target = package / "sample.txt"
    patches = tmp_path / "patches"
    patches.mkdir()
    (patches / "step1.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-pristine\n"
        "+legacy1\n",
        encoding="utf-8",
    )
    (patches / "step2.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-legacy1\n"
        "+legacy2\n",
        encoding="utf-8",
    )
    (patches / "final.patch").write_text(
        "diff --git a/sample.txt b/sample.txt\n"
        "--- a/sample.txt\n"
        "+++ b/sample.txt\n"
        "@@ -1 +1 @@\n"
        "-pristine\n"
        "+final\n",
        encoding="utf-8",
    )
    contract = {
        "patch": "final.patch",
        "pristine": digest(b"pristine\n"),
        "patched": digest(b"final\n"),
        "legacy_patched": [digest(b"legacy1\n"), digest(b"legacy2\n")],
        "legacy_patch": "step1.patch",
        "legacy_patches": {
            digest(b"legacy2\n"): ["step1.patch", "step2.patch"],
        },
    }
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {"sample.txt": contract}})
    monkeypatch.setattr(compat, "patch_root", lambda version: patches)
    backup = tmp_path / "backup"

    target.write_bytes(b"legacy1\n")
    result = compat.ensure_oracle_compatibility(
        "oracle 0.17.1", package_root=package, backup_root=backup
    )
    assert result["changed"] == ["sample.txt"]
    assert compat.sha256_file(backup / "sample.txt") == contract["pristine"]

    target.write_bytes(b"legacy2\r\n")
    result = compat.ensure_oracle_compatibility(
        "oracle 0.17.1", package_root=package, backup_root=backup
    )
    assert result["changed"] == ["sample.txt"]
    assert compat.sha256_file(target) == contract["patched"]

    target.write_bytes(b"unknown\r\n")
    with pytest.raises(compat.OracleCompatError) as mismatch:
        compat.ensure_oracle_compatibility(
            "oracle 0.17.1", package_root=package, backup_root=backup
        )
    assert mismatch.value.code == "ORACLE_FILE_HASH_MISMATCH"


def test_fork_legacy_thinking_time_levels_migrate_to_final_strict_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The two shipped fork levels (raw deployed CRLF hashes 21027b691a... and
    # 300e910c1f...) must migrate to the final upstream strict result under
    # canonical hashing, from both LF and CRLF deployments, while unknown
    # bytes fail closed.
    compat = load_compat()
    relative = "dist/src/browser/actions/thinkingTime.js"
    pristine = PRISTINE_THINKING_TIME
    patches = compat.patch_root("0.17.1")
    contract = compat.VERSION_PATCHES["0.17.1"][relative]
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {relative: contract}})

    era_level = pristine.replace(b"\r\n", b"\n")
    upgraded = era_level
    for patch_name in ("thinkingTime.extra-high-fail-closed.patch", "thinkingTime.pro-heavy-upgrade.patch"):
        work = tmp_path / f"stage-{patch_name}"
        target = work / Path(relative)
        target.parent.mkdir(parents=True)
        (work / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
        target.write_bytes(upgraded)
        compat._apply_patch(work, patches / patch_name)
        upgraded = target.read_bytes()
    assert digest(upgraded) == "1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4"
    assert digest(upgraded.replace(b"\r\n", b"\n")) == "1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4"

    era_work = tmp_path / "stage-era"
    era_target = era_work / Path(relative)
    era_target.parent.mkdir(parents=True)
    (era_work / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    era_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(era_work, patches / "thinkingTime.extra-high-fail-closed.patch")
    era_bytes = era_target.read_bytes()
    assert digest(era_bytes) == "4106ed89a032d06fadcf1c1600e238e26243c02d1c3ef4261ea70169396d464e"

    prior_work = tmp_path / "stage-prior-strict"
    prior_target = prior_work / Path(relative)
    prior_target.parent.mkdir(parents=True)
    prior_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(
        prior_work, patches / "thinkingTime.strict.pre-diagnostic-proof.patch"
    )
    prior_bytes = prior_target.read_bytes()
    assert digest(prior_bytes) == "3f969712b184588d1f34ef4f55b439c86256d112bb0fa1688bb473b61fd3dcc3"

    backup = tmp_path / "backup"
    for label, legacy_bytes in (
        ("era-lf", era_bytes),
        ("era-crlf", era_bytes.replace(b"\n", b"\r\n")),
        ("upgraded-lf", upgraded),
        ("upgraded-crlf", upgraded.replace(b"\n", b"\r\n")),
        ("prior-strict-lf", prior_bytes),
        ("prior-strict-crlf", prior_bytes.replace(b"\n", b"\r\n")),
    ):
        package = tmp_path / f"package-{label}"
        target = package / Path(relative)
        target.parent.mkdir(parents=True)
        (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
        target.write_bytes(legacy_bytes)
        result = compat.ensure_oracle_compatibility(
            "oracle 0.17.1", package_root=package, backup_root=backup
        )
        assert result["changed"] == [relative]
        assert compat.sha256_file(target) == contract["patched"] == (
            "fd7e6fcf2f38e0367b50501e7546244f0e3e2cdb95e8905c388798c5fed5a4f5"
        )
        assert compat.sha256_file(backup / Path(relative)) == contract["pristine"]

    package = tmp_path / "package-unknown"
    target = package / Path(relative)
    target.parent.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    target.write_bytes(upgraded + b"// drift\n")
    with pytest.raises(compat.OracleCompatError) as mismatch:
        compat.ensure_oracle_compatibility(
            "oracle 0.17.1", package_root=package, backup_root=backup
        )
    assert mismatch.value.code == "ORACLE_FILE_HASH_MISMATCH"


def test_fresh_lf_install_applies_final_strict_patch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A pristine LF install must patch directly to the final strict result
    # without any legacy migration step.
    compat = load_compat()
    relative = "dist/src/browser/actions/thinkingTime.js"
    contract = compat.VERSION_PATCHES["0.17.1"][relative]
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {relative: contract}})
    package = tmp_path / "package"
    target = package / Path(relative)
    target.parent.mkdir(parents=True)
    (package / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    target.write_bytes(PRISTINE_THINKING_TIME)
    assert compat.sha256_file(target) == contract["pristine"]

    result = compat.ensure_oracle_compatibility("oracle 0.17.1", package_root=package)
    assert result["changed"] == [relative]
    assert compat.sha256_file(target) == contract["patched"]
    assert compat.inspect_oracle_compatibility(
        "oracle 0.17.1", package_root=package
    )["ready"] is True


def test_old_prompt_composer_levels_migrate_to_diagnostic_census(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every shipped prompt-composer level, including the immediately preceding
    # census/locator result, must migrate from LF or CRLF deployments while
    # unknown bytes fail closed.
    compat = load_compat()
    relative = "dist/src/browser/actions/promptComposer.js"
    patches = compat.patch_root("0.17.1")
    contract = compat.VERSION_PATCHES["0.17.1"][relative]
    monkeypatch.setattr(compat, "VERSION_PATCHES", {"0.17.1": {relative: contract}})

    key_event_patch = patches / "promptComposer.key-event-trigger.patch"
    assert digest(key_event_patch.read_bytes().replace(b"\r\n", b"\n")) == (
        "8800a03a3a9a62005b59bdcc635ae2628fa3dbd92884273b66ce59fcb29795e3"
    )
    preceding_patch = patches / "promptComposer.pre-authority-chain.patch"
    assert digest(preceding_patch.read_bytes().replace(b"\r\n", b"\n")) == (
        "5a494c2f550923e3d22ed486dba88984dc1bceee0fdc7d3320e86a22806057e2"
    )
    observational_patch = patches / "promptComposer.pre-observational-census.patch"
    assert digest(observational_patch.read_bytes().replace(b"\r\n", b"\n")) == (
        "e38977eab590ce054db39c16c0320c435851a2b1f2f87377cddf1836591dd8d3"
    )

    legacy_levels = []
    for label, patch_name, expected in (
        (
            "one-shot",
            "promptComposer.pre-split-trigger.patch",
            "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f",
        ),
        (
            "split-insert-text",
            "promptComposer.pre-key-event-trigger.patch",
            "bb85c6f09f23c4e0c9093bd472c83b17b1ef7325bcd89a3348429610eeefbd74",
        ),
        (
            "key-event-trigger",
            "promptComposer.key-event-trigger.patch",
            "87911b46026d3dd08a643b90aab5dc7009704956a3b5fed493cc600abcb7739a",
        ),
        (
            "pre-authority-chain",
            "promptComposer.pre-authority-chain.patch",
            "e9f28f36f652f209a6c8e2aac42f0ddccae7e24bcd6c9b826eafcc4abf86b682",
        ),
        (
            "pre-observational-census",
            "promptComposer.pre-observational-census.patch",
            "dfbe8bfe8ff616dfe94d71de6c17f906f72eae96fa30aa22bce4afd787ebc4fc",
        ),
    ):
        package = tmp_path / f"fixture-{label}"
        target = package / Path(relative)
        target.parent.mkdir(parents=True)
        (package / "package.json").write_text(
            '{"version":"0.17.1"}', encoding="utf-8"
        )
        target.write_bytes(PRISTINE_PROMPT_COMPOSER.read_bytes())
        compat._apply_patch(package, patches / patch_name)
        legacy_bytes = target.read_bytes()
        assert digest(legacy_bytes) == expected
        legacy_levels.append((label, legacy_bytes))

    backup = tmp_path / "backup"
    for level, legacy_bytes in legacy_levels:
        for newline, deployed_bytes in (
            ("lf", legacy_bytes),
            ("crlf", legacy_bytes.replace(b"\n", b"\r\n")),
        ):
            instance = tmp_path / f"package-{level}-{newline}"
            instance_target = instance / Path(relative)
            instance_target.parent.mkdir(parents=True)
            (instance / "package.json").write_text(
                '{"version":"0.17.1"}', encoding="utf-8"
            )
            instance_target.write_bytes(deployed_bytes)
            result = compat.ensure_oracle_compatibility(
                "oracle 0.17.1", package_root=instance, backup_root=backup
            )
            assert result["changed"] == [relative]
            assert compat.sha256_file(instance_target) == contract["patched"] == (
                "3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96"
            )
            assert compat.sha256_file(backup / Path(relative)) == contract["pristine"]

    unknown = tmp_path / "package-unknown"
    unknown_target = unknown / Path(relative)
    unknown_target.parent.mkdir(parents=True)
    (unknown / "package.json").write_text('{"version":"0.17.1"}', encoding="utf-8")
    unknown_target.write_bytes(legacy_levels[0][1] + b"// drift\n")
    with pytest.raises(compat.OracleCompatError) as mismatch:
        compat.ensure_oracle_compatibility(
            "oracle 0.17.1", package_root=unknown, backup_root=backup
        )
    assert mismatch.value.code == "ORACLE_FILE_HASH_MISMATCH"


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


def test_semantic_devspace_token_without_exact_action_fails_closed(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(tmp_path, suggestion=None, semantic_token="DevSpace")

    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert result["result"] is None
    assert result["sendAttempts"] == 0
    assert result["inserted"] == ["@", "DevSpace"]
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_diagnostic_no_submission_blocks_non_app_prompt_before_send(tmp_path: Path) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=None,
        semantic_token=None,
        diagnostic_stop_before_send=True,
        prompt="plain diagnostic mission",
    )

    assert result["error"]["code"] == "APP_MENTION_DIAGNOSTIC_NO_SUBMISSION"
    assert result["sendAttempts"] == 0
    assert result["inserted"] == ["plain diagnostic mission"]
    assert "no routed app established; send suppressed" in stderr


@pytest.mark.parametrize("pill_icon_visible", [False, True], ids=["hidden-icon", "visible-icon"])
def test_exact_clicked_app_accepts_live_noneditable_pill_with_exact_visible_label(
    tmp_path: Path, pill_icon_visible: bool,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
        pill_icon_visible=pill_icon_visible,
        suggestion_case="group",
    )

    assert result["error"] is None
    assert result["suggestionClicks"] == 1
    assert result["sendAttempts"] == 1
    assert "APP_MENTION_ROUTE_UNCONFIRMED" not in stderr


def test_live_group_composer_sequence_stops_before_send_with_both_route_checks(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
        suggestion_case="group",
        diagnostic_stop_before_send=True,
    )

    assert result["error"]["code"] == "APP_MENTION_DIAGNOSTIC_NO_SUBMISSION"
    assert result["result"] is None
    assert result["suggestionObserved"] is True
    assert result["suggestionClicks"] == 1
    assert result["initialRouteConfirmed"] is True
    assert result["finalRouteConfirmed"] is True
    assert result["routeChecks"] == 2
    assert result["sendAttempts"] == 0
    assert "APP_MENTION_ROUTE_UNCONFIRMED" not in stderr
    assert "APP_MENTION_DIAGNOSTIC_NO_SUBMISSION" in stderr


@pytest.mark.parametrize(
    ("suggestion", "suggestion_case"),
    [
        pytest.param("DevSpace", "ambiguous", id="ambiguous-exact-actions"),
        pytest.param("DevSpace", "hidden", id="hidden-group"),
        pytest.param("DevSpace", "disabled", id="disabled-exact-action"),
        pytest.param("DevSpace", "outside-menu", id="outside-suggestion-surface"),
        pytest.param("DevSpace Helper", "group", id="wrong-app"),
    ],
)
def test_group_suggestion_resolver_refuses_non_authoritative_candidates_before_send(
    tmp_path: Path, suggestion: str, suggestion_case: str
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=suggestion,
        suggestion_case=suggestion_case,
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
    )

    assert result["suggestionClicks"] == 0
    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


@pytest.mark.parametrize(
    "suggestion_case",
    ["group-lookalike", "nested-unrelated", "plugin-description"],
)
def test_exact_app_action_ignores_nonidentity_group_metadata(
    tmp_path: Path, suggestion_case: str,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        suggestion_case=suggestion_case,
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
        diagnostic_stop_before_send=True,
    )

    assert result["suggestionClicks"] == 1
    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_DIAGNOSTIC_NO_SUBMISSION"
    assert result["initialRouteConfirmed"] is True
    assert result["finalRouteConfirmed"] is True
    assert "APP_MENTION_ROUTE_UNCONFIRMED" not in stderr


def test_keyboard_mention_trigger_opens_suggestion_before_app_name(tmp_path: Path) -> None:
    # The picker requires a key event, not CDP Input.insertText. Emit one
    # keyDown/keyUp pair for '@', then retain the bounded settle and fast text
    # insertion for the app name.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
        suggestion_case="group",
        trigger_key_event=True,
    )

    assert result["error"] is None
    assert result["inserted"] == ["@", "DevSpace", " mission"]
    mention_events = [event for event in result["keyEvents"] if event.get("key") == "@"]
    assert mention_events == [
        {
            "type": "keyDown",
            "key": "@",
            "code": "Digit2",
            "text": "@",
            "unmodifiedText": "@",
            "windowsVirtualKeyCode": 50,
            "nativeVirtualKeyCode": 50,
            "modifiers": 8,
        },
        {
            "type": "keyUp",
            "key": "@",
            "code": "Digit2",
            "windowsVirtualKeyCode": 50,
            "nativeVirtualKeyCode": 50,
            "modifiers": 8,
        },
    ]
    assert result["suggestionObserved"] is True
    assert result["suggestionClicks"] == 1
    assert result["initialRouteConfirmed"] is True
    assert result["finalRouteConfirmed"] is True
    assert result["routeChecks"] == 2
    assert result["sendAttempts"] == 1
    assert "APP_MENTION_ROUTE_UNCONFIRMED" not in stderr


def test_keyboard_mention_trigger_without_surface_fails_closed(tmp_path: Path) -> None:
    # With no suggestion surface at all (e.g. the app is not selectable in
    # the account), the keyboard trigger must keep the exact fail-closed path:
    # no click, no send, APP_MENTION_ROUTE_UNCONFIRMED before submission.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion=None,
        semantic_token=None,
        trigger_key_event=True,
    )

    assert result["suggestionClicks"] == 0
    assert result["sendAttempts"] == 0
    assert result["clearCount"] == 2
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_key_event_miss_census_discriminates_before_send(tmp_path: Path) -> None:
    # The @ keyDown/keyUp pair is dispatched but the mention picker never
    # opens. The failure census must pin this as a key-event failure, with
    # zero click and zero send attempts.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token=None,
        key_event_ignored=True,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr
    assert [event for event in result["keyEvents"] if event.get("key") == "@"]
    assert result["censuses"], "the census logs must be emitted"
    assert result["censuses"][-1]["stage"] == "P2"
    assert result["censuses"][-1]["discriminator"] == "no-visible-action-surface"
    assert result["censuses"][-1]["controls"] == [
        {
            "tag": "BUTTON",
            "role": None,
            "testid": "composer-tools-menu-button",
            "ariaLabel": "Add files and more",
            "title": None,
            "ariaExpanded": "false",
            "ariaHaspopup": "menu",
            "text": "",
            "visible": True,
            "rect": {"width": 10, "height": 10},
        }
    ]


def test_caret_outside_editor_census_discriminates_before_send(tmp_path: Path) -> None:
    # The editor is still focused but the Lexical caret/selection anchor sits
    # outside the editor, so the mention key event cannot open the picker.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token=None,
        caret_outside_editor=True,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr
    assert result["censuses"], "the census logs must be emitted"
    assert result["censuses"][-1]["stage"] == "P2"
    assert result["censuses"][-1]["discriminator"] == "caret-outside-editor"


def test_visible_generic_surface_without_exact_app_does_not_claim_picker_evidence(
    tmp_path: Path,
) -> None:
    # A generic surface with no exact app action is observable, but it is not
    # authority that the mention picker opened or that the app is unavailable.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="OtherApp",
        semantic_token=None,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr
    assert result["censuses"], "the census logs must be emitted"
    assert result["censuses"][-1]["stage"] == "P2"
    assert result["censuses"][-1]["discriminator"] == "no-exact-app-action-observed"
    assert result["censuses"][-1]["actionSurfaceVisible"] is True
    assert "pickerOpen" not in result["censuses"][-1]


def test_devspace_item_rejected_by_current_locator_census_discriminates_before_send(
    tmp_path: Path,
) -> None:
    # A DevSpace item with exact text is present, but the current locator
    # rejects it (here: disabled action), so the failure census must report
    # locator-drift with the per-candidate rejection reason.
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        suggestion_case="disabled",
        semantic_token=None,
    )

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr
    assert result["censuses"], "the census logs must be emitted"
    census = result["censuses"][-1]
    assert census["stage"] == "P2"
    assert census["discriminator"] == "candidate-rejected-by-locator"
    assert any(entry["reason"] == "target-not-enabled" for entry in census["rejections"])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param({"suggestion": "DevSpace", "semantic_token": "DevSpace Helper"}, id="lookalike-label"),
        pytest.param({"suggestion": "DevSpace", "semantic_visible": False}, id="hidden-inner-label"),
        pytest.param({"suggestion": "DevSpace", "pill_editable": True}, id="editable-wrapper"),
        pytest.param({"suggestion": None}, id="no-exact-click"),
        pytest.param({"suggestion": "DevSpace", "semantic_token": "OtherApp"}, id="wrong-app"),
        pytest.param({"suggestion": "DevSpace", "semantic_scope": "form"}, id="outside-editor"),
    ],
)
def test_live_pill_refusals_fail_before_send(tmp_path: Path, case: dict[str, object]) -> None:
    options: dict[str, object] = {
        "suggestion": "DevSpace",
        "semantic_token": "DevSpace",
        "semantic_identity_attr": None,
        "semantic_layout": "pill",
        **case,
    }
    result, stderr = run_prompt_route_case(tmp_path, **options)

    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_live_pill_lost_before_send_fails_closed(tmp_path: Path) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token="DevSpace",
        semantic_identity_attr=None,
        semantic_layout="pill",
        fallback_overwrite=True,
    )

    assert result["suggestionClicks"] == 1
    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


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


def test_exact_app_suggestion_with_wrong_semantic_decorator_fails_before_send(
    tmp_path: Path,
) -> None:
    result, stderr = run_prompt_route_case(
        tmp_path,
        suggestion="DevSpace",
        semantic_token="DevSpace Helper",
        semantic_identity_attr=None,
        semantic_marker="contenteditable",
    )

    assert result["suggestionClicks"] == 1
    assert result["sendAttempts"] == 0
    assert result["error"]["code"] == "APP_MENTION_ROUTE_UNCONFIRMED"
    assert "APP_MENTION_ROUTE_UNCONFIRMED" in stderr


def test_oracle_0171_has_the_exact_eight_hash_gated_compatibility_patches() -> None:
    compat = load_compat()
    contracts = compat.VERSION_PATCHES["0.17.1"]

    assert compat.SUPPORTED_VERSION == "0.17.1"
    assert compat.RECOVERABLE_VERSIONS == ("0.16.1", "0.17.0", "0.17.1")
    assert "dist/src/browser/actions/modelSelection.js" not in contracts
    assert {
        path: (contract["pristine"], contract["patched"])
        for path, contract in contracts.items()
    } == {
        "dist/src/browser/chromeLifecycle.js": ("312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81", "61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093"),
        "dist/src/browser/recoverConversation.js": ("d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca", "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e"),
        "dist/src/browser/profileCopy.js": ("06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f", "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c"),
        "dist/src/cli/browserConfig.js": ("989f14399c8aa51913752306135e11d97e4f1c55b2baf984907f1b54959cc340", "bd18d11e4770fa5335c889b7856622f2da4199351ec65bc17a5ec1f472e2506f"),
        "dist/src/browser/index.js": ("335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11", "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895"),
        "dist/src/browser/actions/assistantResponse.js": ("0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d", "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4"),
        "dist/src/browser/actions/promptComposer.js": ("db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000", "3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96"),
        "dist/src/browser/actions/thinkingTime.js": ("508f1fbc175b82e6bfd4c978da6199306800615f432e28d7721c155c402795ca", "fd7e6fcf2f38e0367b50501e7546244f0e3e2cdb95e8905c388798c5fed5a4f5"),
    }

    patches = {
        path: (compat.patch_root("0.17.1") / contract["patch"]).read_text(encoding="utf-8")
        for path, contract in contracts.items()
    }
    assert 'process.platform === "win32"' in patches["dist/src/browser/profileCopy.js"]
    assert "options.browserManualLogin = false" in patches["dist/src/cli/browserConfig.js"]
    assert "config = { ...config, manualLogin: false" in patches["dist/src/browser/index.js"]
    thinking_patch = patches["dist/src/browser/actions/thinkingTime.js"]
    assert 'composer-model-picker-slider-simple-view' in thinking_patch
    assert 'composer-model-picker-slider-advanced-view' in thinking_patch
    assert "exactGpt56ProProof" in thinking_patch
    assert "diagnosticProProof" in thinking_patch
    assert "const POWER_TARGET" in thinking_patch
    assert "strictGpt56Effort" in thinking_patch
    assert "selection-unverified" in thinking_patch
    assert "refusing to submit without confirmed ${requiredEffortLabel}" in thinking_patch
    thinking = contracts["dist/src/browser/actions/thinkingTime.js"]
    assert thinking["legacy_patched"] == [
        # Fork legacy levels (canonical LF hashes; deployed copies carried raw
        # CRLF hashes 21027b691a... and 300e910c1f...).
        "4106ed89a032d06fadcf1c1600e238e26243c02d1c3ef4261ea70169396d464e",
        "1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4",
        # Parent-project legacy levels from the audited 9542abee donor.
        "536571fccc3f8137bfbf0ea96dfd827f1eabdaf92f93fe7cff92af242ef01d53",
        "fe6db3c1d48ccf7eff212dab7e69a2b3c7439f44b5cc823d474aa4fbd0925151",
        "ce0fa250ba4b28aeff9e3e80267b3f55bd08f7d25c9890a0eb09debcae447b8b",
        "686e80ee7480686622eab7bc8863eccdf3ad57e64f662bfcbfbc4852802c7aaa",
        "4e73e1c1d9c04e7bea7811a5e32bf17c559a2e1171581dc4cc33f48163ef28e7",
        "374f0fabd62ea82ecf359c3050995da7a3de2d791905d04742f91ebe098d910a",
        "864f8365ecbd0aef9b631f7ae61c80b3e43424dc37c34cdfd5c6e5aa06b0c1b3",
        "d8fbe1394314efaa38343539ad7be519212fd5301f74e4aa92336f6925e3b5fd",
        "9ac1cab3200fb848ca2f88c07f98b19d94c7d4ad5a9b2e578c1c5a9dee4df15f",
        "2baba20f9162eea8b4659ff42d85c26064d037bb18dd90f2022cf4764ddd710d",
        "0cb7bf4774e5507fb97682cf4e350fea03998c2a44548065bf8e9eb57fe16707",
        "b55897a9d90627b226e39e77339819e446927ffc66f78181f5c2851cbcfe5f97",
        "3f969712b184588d1f34ef4f55b439c86256d112bb0fa1688bb473b61fd3dcc3",
    ]
    assert thinking["legacy_patch"] == "thinkingTime.strict.pre-power.patch"
    assert thinking["legacy_patches"]["4106ed89a032d06fadcf1c1600e238e26243c02d1c3ef4261ea70169396d464e"] == [
        "thinkingTime.extra-high-fail-closed.patch",
    ]
    assert thinking["legacy_patches"]["1464b79c1d0bb8913963ab12c55fbc843fe760f367be2b68b1c36d62e43ff5e4"] == [
        "thinkingTime.extra-high-fail-closed.patch",
        "thinkingTime.pro-heavy-upgrade.patch",
    ]
    assert thinking["legacy_patches"]["b55897a9d90627b226e39e77339819e446927ffc66f78181f5c2851cbcfe5f97"] == (
        "thinkingTime.strict.pre-advanced-view-sibling.patch"
    )
    assert thinking["legacy_patches"]["3f969712b184588d1f34ef4f55b439c86256d112bb0fa1688bb473b61fd3dcc3"] == (
        "thinkingTime.strict.pre-diagnostic-proof.patch"
    )
    assert "536571fccc3f8137bfbf0ea96dfd827f1eabdaf92f93fe7cff92af242ef01d53" not in thinking["legacy_patches"]
    composer = contracts["dist/src/browser/actions/promptComposer.js"]
    assert composer["legacy_patched"] == [
        "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f",
        "bb85c6f09f23c4e0c9093bd472c83b17b1ef7325bcd89a3348429610eeefbd74",
        "87911b46026d3dd08a643b90aab5dc7009704956a3b5fed493cc600abcb7739a",
        "e9f28f36f652f209a6c8e2aac42f0ddccae7e24bcd6c9b826eafcc4abf86b682",
        "dfbe8bfe8ff616dfe94d71de6c17f906f72eae96fa30aa22bce4afd787ebc4fc",
    ]
    assert composer["legacy_patches"] == {
        "a3882c7881a7e787a33092350c494d950a6f67c38e6801cd1eaff20ac317532f": (
            "promptComposer.pre-split-trigger.patch"
        ),
        "bb85c6f09f23c4e0c9093bd472c83b17b1ef7325bcd89a3348429610eeefbd74": (
            "promptComposer.pre-key-event-trigger.patch"
        ),
        "87911b46026d3dd08a643b90aab5dc7009704956a3b5fed493cc600abcb7739a": (
            "promptComposer.key-event-trigger.patch"
        ),
        "e9f28f36f652f209a6c8e2aac42f0ddccae7e24bcd6c9b826eafcc4abf86b682": (
            "promptComposer.pre-authority-chain.patch"
        ),
        "dfbe8bfe8ff616dfe94d71de6c17f906f72eae96fa30aa22bce4afd787ebc4fc": (
            "promptComposer.pre-observational-census.patch"
        ),
    }
    key_event_legacy = (
        compat.patch_root("0.17.1") / "promptComposer.key-event-trigger.patch"
    ).read_text(encoding="utf-8")
    assert key_event_legacy.startswith(
        "diff --git a/dist/src/browser/actions/promptComposer.js b/dist/src/browser/actions/promptComposer.js\n"
    )
    assert "await input.dispatchKeyEvent({" in key_event_legacy
    prompt_patch = patches["dist/src/browser/actions/promptComposer.js"]
    assert 'type: "keyDown"' in prompt_patch
    assert 'key: "@"' in prompt_patch
    assert 'text: "@"' in prompt_patch
    assert 'type: "keyUp"' in prompt_patch
    assert 'code: "Digit2"' in prompt_patch
    assert "await input.insertText({ text: \"@\" })" not in prompt_patch
    assert "await delay(250)" in patches["dist/src/browser/actions/promptComposer.js"]
    assert "await input.insertText({ text: appName })" in patches["dist/src/browser/actions/promptComposer.js"]
    assert "mentionSurfaceDeadline" not in patches["dist/src/browser/actions/promptComposer.js"]
    assert "atMentionSurfaceProbe" not in patches["dist/src/browser/actions/promptComposer.js"]
    for patch_name in {
        "thinkingTime.strict.patch",
        "thinkingTime.strict.pre-power.patch",
        "thinkingTime.strict.broken-power.patch",
        "thinkingTime.strict.double-escaped-power.patch",
        "thinkingTime.strict.single-escaped-power.patch",
        "thinkingTime.strict.regex-power.patch",
        "thinkingTime.strict.compact-power.patch",
        "thinkingTime.strict.hidden-slider.patch",
        "thinkingTime.strict.pro-proof-model-bound.patch",
        "thinkingTime.strict.null-model-menu-closed.patch",
        "thinkingTime.strict.pre-outer-model-proof.patch",
        "thinkingTime.strict.pre-visible-advanced-proof.patch",
        "thinkingTime.strict.pre-advanced-view-sibling.patch",
        "thinkingTime.strict.pre-diagnostic-proof.patch",
        "thinkingTime.extra-high-fail-closed.patch",
        "thinkingTime.pro-heavy-upgrade.patch",
        "promptComposer.pre-key-event-trigger.patch",
        "promptComposer.pre-authority-chain.patch",
        "promptComposer.pre-split-trigger.patch",
        "promptComposer.pre-observational-census.patch",
    }:
        assert (compat.patch_root("0.17.1") / patch_name).is_file(), patch_name


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


def test_legacy_recover_conversation_patch_sets_the_longer_ready_budget() -> None:
    compat = load_compat()
    contract = compat.VERSION_PATCHES["0.16.1"]["dist/src/browser/recoverConversation.js"]
    patch = (
        compat.patch_root("0.16.1")
        / contract["patch"]
    ).read_text(encoding="utf-8")

    assert "const DEFAULT_READY_TIMEOUT_MS = 90_000;" in patch
    assert contract["pristine"] == "8c7d841bc078af20c8922ec435f62e00df7a40605583fbd89334696b3ddb386b"
    assert contract["patched"] == "168d665fa7c6cc0ef5094a990e94e7a3ae57f2d3bebcc5c2625cb6cff0cb89b1"
    assert "650ffe9bdbbaf799510e8cacaa8ba8407322bbbb175e790a3cf7777fa14772fe" in contract["legacy_patched"]


def test_live_tail_patch_keeps_one_recovered_browser_connection_until_its_deadline() -> None:
    compat = load_compat()
    contract = compat.VERSION_PATCHES["0.16.1"]["dist/src/cli/browserTabs.js"]
    patch = (
        MODULE_PATH.parent
        / "oracle-compat"
        / "0.16.1"
        / contract["patch"]
    ).read_text(encoding="utf-8")

    assert "ORACLE_LIVE_TERMINAL_TIMEOUT_MS" in patch
    assert "const terminalDeadlineMs" in patch
    assert "Date.now() < terminalDeadlineMs" in patch
    assert "recoveredContentDeadlineMs = holdRecoveredConnection" in patch
    assert "? terminalDeadlineMs" in patch
    assert contract["legacy_patched"] == ["1a6d3b9d7044d84300f630fe669b16d9cfec3925c427cfb4c3d1291205406dab"]
    assert contract["legacy_patch"] == "browserTabs.pre-readiness.patch"
    assert contract["pristine"] == "05256692ffa9b35415346963adde5ff42aeacd78ce46dd6f484496678f5d0281"


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
