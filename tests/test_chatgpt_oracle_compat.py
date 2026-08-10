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
PRISTINE_THINKING_TIME_0172 = (
    Path(__file__).parent / "fixtures/oracle-0.17.2/thinkingTime.pristine.js"
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


def run_gpt56_pro_diagnostic_recovery_case(
    tmp_path: Path,
    *,
    states: list[bool],
    hidden_stale: bool = False,
    hidden_ancestor: bool = False,
    version: str = "0.17.1",
) -> tuple[int, str | None, list[str]]:
    compat = load_compat()
    relative = Path("dist/src/browser/actions/thinkingTime.js")
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(
        PRISTINE_THINKING_TIME_0172 if version == "0.17.2" else PRISTINE_THINKING_TIME
    )
    compat._apply_patch(package, compat.patch_root(version) / "thinkingTime.strict.patch")
    source = "\n".join(target.read_text(encoding="utf-8").splitlines()[3:])
    scenario = json.dumps(
        {
            "states": states,
            "hiddenStale": hidden_stale,
            "hiddenAncestor": hidden_ancestor,
        }
    )
    harness = f"""
const scenario = {scenario};
const logDomFailure = async () => {{}};
const buildClickDispatcher = () => '';
const MENU_CONTAINER_SELECTOR = '[data-testid="model-menu"]';
const MENU_ITEM_SELECTOR = '[role="menuitem"]';
const MODEL_BUTTON_SELECTOR = '[data-testid="model-button"]';
let observation = -1;
let active = scenario.states[0];
const node = (kind, text, attrs, stale = false) => ({{
  kind, textContent: text, attrs, stale,
  getAttribute(name) {{ return this.attrs[name] ?? null; }},
  querySelector(selector) {{
    return selector.includes('slider') || selector.includes('range') ? this : null;
  }},
  querySelectorAll() {{ return []; }},
  getBoundingClientRect() {{ return {{ width: 10, height: 10 }}; }},
  matches() {{ return false; }},
}});
const stale = scenario.hiddenStale;
const page = node('page', '', {{}});
const picker = node('picker', '', {{ id: 'picker-a' }});
picker.parentElement = page;
const model = node('model', stale ? 'GPT-5.6 Sol' : 'Pro', {{
  'aria-label': stale ? 'GPT-5.6 Sol' : 'Pro',
  'aria-controls': 'picker-a',
}});
const slider = node('slider', stale ? 'Power 4 of 5 Extra High' : 'Power 5 of 5 Pro', {{ 'aria-valuenow': stale ? '4' : '5' }});
const advanced = node('advanced', stale ? 'Model GPT-5.6 Sol Effort Extra High' : 'Model GPT-5.6 Sol Effort Pro', {{}});
const staleModel = node('model', 'Pro', {{ 'aria-label': 'Pro' }}, stale);
const staleSlider = node('slider', 'Power 5 of 5 Pro', {{ 'aria-valuenow': '5' }}, stale);
const staleAdvanced = node('advanced', 'Model GPT-5.6 Sol Effort Pro', {{}}, stale);
model.parentElement = page;
slider.parentElement = picker;
advanced.parentElement = picker;
staleModel.parentElement = page;
staleSlider.parentElement = picker;
staleAdvanced.parentElement = picker;
picker.querySelectorAll = (selector) => {{
  if (selector.includes('slider-simple')) return stale ? [staleSlider, slider] : [slider];
  if (selector.includes('slider-advanced')) return stale ? [staleAdvanced, advanced] : [advanced];
  return [];
}};
const candidates = (selector) => {{
  if (selector.includes('model-button')) return stale ? [staleModel, model] : [model];
  if (selector.includes('slider-simple')) return stale ? [staleSlider, slider] : [slider];
  if (selector.includes('slider-advanced')) return stale ? [staleAdvanced, advanced] : [advanced];
  return [];
}};
const document = {{
  querySelectorAll: candidates,
  querySelector: (selector) => candidates(selector)[0] ?? null,
  getElementById: (id) => id === 'picker-a' ? picker : null,
}};
const window = {{
  getComputedStyle(candidate) {{
    if (candidate === page) {{
      return {{ display: 'block', visibility: 'visible', opacity: scenario.hiddenAncestor ? '0' : '1' }};
    }}
    if (candidate === picker) {{
      return {{ display: 'block', visibility: 'visible', opacity: '1' }};
    }}
    if (candidate.kind === 'model' && !candidate.stale) {{
      observation += 1;
      active = scenario.states[Math.min(observation, scenario.states.length - 1)];
    }}
    const visible = !candidate.stale && active;
    return {{ display: visible ? 'block' : 'none', visibility: 'visible', opacity: '1', pointerEvents: 'auto' }};
  }},
}};
let tick = 0;
const fakePerformance = {{ now: () => (tick += 100) }};
const fakeSetTimeout = (resolve) => {{ resolve(); return 0; }};
const execute = (expression) => Function('document', 'window', 'performance', 'setTimeout', `return (${{expression}});`)(
  document, window, fakePerformance, fakeSetTimeout,
);
const diagnostic = {{
  modelButton: {{ text: 'Pro' }},
  menus: [{{ items: [
    {{ testid: 'composer-model-picker-slider-simple-view', text: 'Power 5 of 5 Pro' }},
    {{ testid: 'composer-model-picker-slider-advanced-view', text: 'Model GPT-5.6 Sol Effort Pro' }},
  ] }}],
}};
let calls = 0;
const Runtime = {{
  async evaluate({{ expression }}) {{
    calls += 1;
    if (calls === 1) return {{ result: {{ value: {{ status: 'selection-unverified', diagnostic }} }} }};
    return {{ result: {{ value: await execute(expression) }} }};
  }},
}};
const logs = [];
let error = null;
try {{
  await ensureThinkingTime(Runtime, 'heavy', (message) => logs.push(message), 'gpt-5.6-sol');
}} catch (caught) {{
  error = caught.message;
}}
console.log(JSON.stringify({{ calls, error, logs }}));
"""
    completed = subprocess.run(
        ["node", "--input-type=module"],
        input=source + harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    return int(result["calls"]), result["error"], list(result["logs"])


def test_gpt56_pro_diagnostic_recovery_requires_independent_visible_proof(
    tmp_path: Path,
) -> None:
    one_calls, one_error, _ = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "one-observation", states=[True, False]
    )
    assert one_calls == 2
    assert "refusing to submit" in str(one_error)

    hidden_calls, hidden_error, _ = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "hidden-stale", states=[True, True], hidden_stale=True
    )
    assert hidden_calls == 2
    assert "refusing to submit" in str(hidden_error)

    ancestor_calls, ancestor_error, _ = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "hidden-ancestor", states=[True, True], hidden_ancestor=True
    )
    assert ancestor_calls == 2
    assert "refusing to submit" in str(ancestor_error)

    stable_calls, stable_error, stable_logs = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "stable", states=[True, True]
    )
    assert stable_calls == 2
    assert stable_error is None
    assert stable_logs == ["[browser] Thinking time: Power 5 of 5 (Pro) (already selected)"]


def run_gpt56_primary_css_visibility_cases(
    tmp_path: Path, *, version: str = "0.17.1"
) -> dict[str, str]:
    compat = load_compat()
    relative = Path("dist/src/browser/actions/thinkingTime.js")
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(
        PRISTINE_THINKING_TIME_0172 if version == "0.17.2" else PRISTINE_THINKING_TIME
    )
    compat._apply_patch(package, compat.patch_root(version) / "thinkingTime.strict.patch")
    source = "\n".join(target.read_text(encoding="utf-8").splitlines()[3:])
    scenarios = json.dumps([
        {"label": "visible", "display": "block", "visibility": "visible", "opacity": "1", "ariaHidden": False},
        {"label": "display-none", "display": "none", "visibility": "visible", "opacity": "1", "ariaHidden": False},
        {"label": "visibility-hidden", "display": "block", "visibility": "hidden", "opacity": "1", "ariaHidden": False},
        {"label": "visibility-collapse", "display": "block", "visibility": "collapse", "opacity": "1", "ariaHidden": False},
        {"label": "opacity-zero", "display": "block", "visibility": "visible", "opacity": "0", "ariaHidden": False},
        {"label": "ancestor-opacity-zero", "display": "block", "visibility": "visible", "opacity": "1", "ancestorOpacity": "0", "ariaHidden": False},
        {"label": "aria-hidden", "display": "block", "visibility": "visible", "opacity": "1", "ariaHidden": True},
        {"label": "split-picker", "display": "block", "visibility": "visible", "opacity": "1", "ariaHidden": False, "splitPicker": True},
    ])
    harness = """
const scenarios = SCENARIOS;
const logDomFailure = async () => {};
const buildClickDispatcher = () => '';
const MENU_CONTAINER_SELECTOR = '[data-testid="model-menu"]';
const MENU_ITEM_SELECTOR = '[role="menuitem"]';
const MODEL_BUTTON_SELECTOR = '[data-testid="model-button"]';
const node = (text, attrs = {}) => ({
  textContent: text, attrs, parentElement: null,
  getAttribute(name) { return this.attrs[name] ?? null; },
  querySelector() { return this; },
  querySelectorAll() { return []; },
  getBoundingClientRect() { return { width: 10, height: 10 }; },
  matches() { return false; },
  focus() {},
});
const expression = buildThinkingTimeExpressionForTest('heavy', 'gpt-5.6-sol');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const evaluateScenario = async (scenario) => {
  const hiddenAttrs = scenario.ariaHidden ? { 'aria-hidden': 'true' } : {};
  const page = node('');
  const pickerA = node('', { id: 'picker-a' });
  const pickerB = node('', { id: 'picker-b' });
  pickerA.parentElement = page;
  pickerB.parentElement = page;
  const model = node('Pro', {
    ...hiddenAttrs,
    'aria-label': 'Pro',
    'aria-expanded': 'true',
    'aria-controls': 'picker-a',
  });
  const slider = node('Power 5 of 5 Pro', { ...hiddenAttrs, 'aria-valuenow': '5' });
  const advanced = node('Model GPT-5.6 Sol Effort Pro', hiddenAttrs);
  model.parentElement = page;
  slider.parentElement = pickerA;
  advanced.parentElement = scenario.splitPicker ? pickerB : pickerA;
  pickerA.querySelectorAll = (selector) => {
    if (selector.includes('slider-simple')) return [slider];
    if (selector.includes('slider-advanced')) return scenario.splitPicker ? [] : [advanced];
    return [];
  };
  pickerB.querySelectorAll = (selector) =>
    selector.includes('slider-advanced') && scenario.splitPicker ? [advanced] : [];
  const nodes = (selector) => {
    if (selector.includes('model-menu')) return scenario.splitPicker ? [pickerA, pickerB] : [pickerA];
    if (selector.includes('model-button')) return [model];
    if (selector.includes('slider-simple')) return [slider];
    if (selector.includes('slider-advanced')) return [advanced];
    return [];
  };
  const document = {
    querySelectorAll: nodes,
    querySelector: (selector) => nodes(selector)[0] ?? null,
    getElementById: (id) => id === 'picker-a' ? pickerA : id === 'picker-b' ? pickerB : null,
  };
  const window = {
    getComputedStyle: (target) => target === page
      ? { ...scenario, opacity: scenario.ancestorOpacity ?? '1' }
      : scenario,
  };
  let tick = 0;
  const performance = { now: () => (tick += 100) };
  const setTimeout = (resolve) => { resolve(); return 0; };
  const result = await AsyncFunction('document', 'window', 'performance', 'setTimeout', `return (${expression});`)(
    document, window, performance, setTimeout,
  );
  return result.status;
};
const statuses = {};
for (const scenario of scenarios) statuses[scenario.label] = await evaluateScenario(scenario);
console.log(JSON.stringify(statuses));
""".replace("SCENARIOS", scenarios)
    completed = subprocess.run(
        ["node", "--input-type=module"],
        input=source + harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return {str(label): str(status) for label, status in json.loads(completed.stdout.strip().splitlines()[-1]).items()}


def run_gpt56_0172_advanced_owner_cases(tmp_path: Path) -> dict[str, dict[str, int | str]]:
    compat = load_compat()
    relative = Path("dist/src/browser/actions/thinkingTime.js")
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(PRISTINE_THINKING_TIME_0172)
    compat._apply_patch(package, compat.patch_root("0.17.2") / "thinkingTime.strict.patch")
    source = "\n".join(target.read_text(encoding="utf-8").splitlines()[3:])
    scenarios = json.dumps([
        {"label": "pro-stable", "level": "heavy", "power": 5, "observations": [True, True]},
        {"label": "ancestor-display-none", "level": "heavy", "power": 5, "ancestorDisplay": "none"},
        {"label": "ancestor-opacity-zero", "level": "heavy", "power": 5, "ancestorOpacity": "0"},
        {"label": "aria-hidden", "level": "heavy", "power": 5, "ariaHidden": True},
        {"label": "split-picker", "level": "heavy", "power": 5, "splitPicker": True},
        {"label": "one-observation-then-lost", "level": "heavy", "power": 5, "observations": [True, False]},
        {"label": "power4-owned", "level": "extra-high", "power": 4},
        {"label": "power4-unrelated", "level": "extra-high", "power": 3, "unrelatedPower": 4},
    ])
    harness = """
const scenarios = SCENARIOS;
const logDomFailure = async () => {};
const buildClickDispatcher = () => '';
const MENU_CONTAINER_SELECTOR = '[data-testid="model-menu"]';
const MENU_ITEM_SELECTOR = '[role="menuitem"]';
const MODEL_BUTTON_SELECTOR = '[data-testid="model-button"]';
class FakeNode extends EventTarget {
  constructor(kind, text = '', attrs = {}) {
    super();
    this.kind = kind;
    this.textContent = text;
    this.attrs = attrs;
    this.parentElement = null;
    this.query = () => [];
  }
  getAttribute(name) { return this.attrs[name] ?? null; }
  getBoundingClientRect() { return { width: 10, height: 10 }; }
  querySelectorAll(selector) { return this.query(selector); }
  querySelector(selector) { return this.query(selector)[0] ?? null; }
  matches(selector) { return this.kind === 'pill' && selector.includes('button.__composer-pill'); }
  contains(candidate) {
    for (let node = candidate; node; node = node.parentElement) if (node === this) return true;
    return false;
  }
  closest() { return null; }
  focus() {}
}
globalThis.HTMLElement = FakeNode;
globalThis.KeyboardEvent = class { constructor(type, init) { this.type = type; this.init = init; } };
const expressionFor = (level) => buildThinkingTimeExpressionForTest(level, 'gpt-5.6-sol');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = async (scenario) => {
  const effort = scenario.level === 'heavy' ? 'Pro' : 'Extra High';
  const page = new FakeNode('page');
  const owner = new FakeNode('owner', '', { id: 'picker-owner', 'data-testid': 'composer-intelligence-picker-content' });
  const effortMenu = new FakeNode('menu', '', { id: 'effort-menu' });
  const splitRoot = new FakeNode('split', '', { id: 'picker-split' });
  const unrelatedRoot = new FakeNode('unrelated', '', { id: 'picker-unrelated' });
  owner.parentElement = page;
  splitRoot.parentElement = page;
  unrelatedRoot.parentElement = page;
  effortMenu.parentElement = page;
  const pill = new FakeNode('pill', effort, {
    'aria-label': effort,
    'aria-expanded': 'true',
    'aria-controls': 'picker-owner',
    'data-testid': 'model-button',
  });
  pill.parentElement = page;
  const sliderAttrs = { 'aria-valuenow': String(scenario.power) };
  if (scenario.ariaHidden) sliderAttrs['aria-hidden'] = 'true';
  const ownerSlider = new FakeNode('slider', `Power ${scenario.power} of 5 ${effort}`, sliderAttrs);
  ownerSlider.parentElement = owner;
  const unrelatedSlider = scenario.unrelatedPower
    ? new FakeNode('slider', `Power ${scenario.unrelatedPower} of 5 Extra High`, { 'aria-valuenow': String(scenario.unrelatedPower) })
    : null;
  if (unrelatedSlider) unrelatedSlider.parentElement = unrelatedRoot;
  const advanced = new FakeNode(
    'advanced',
    scenario.splitPicker ? 'Advanced' : `Model GPT-5.6 Sol Effort ${effort}`,
  );
  advanced.parentElement = owner;
  const splitAdvanced = scenario.splitPicker
    ? new FakeNode('advanced', `Model GPT-5.6 Sol Effort ${effort}`)
    : null;
  if (splitAdvanced) splitAdvanced.parentElement = splitRoot;
  const advancedToggle = new FakeNode('toggle', 'Advanced', { role: 'menuitem', 'aria-expanded': 'true' });
  advancedToggle.parentElement = owner;
  const opener = new FakeNode('opener', `Effort ${effort}`, {
    role: 'menuitem', 'aria-haspopup': 'menu', 'aria-expanded': 'true', 'aria-controls': 'effort-menu',
  });
  opener.parentElement = advanced;
  const high = new FakeNode('option', 'High', { role: 'menuitem', 'aria-checked': 'false' });
  const extraHigh = new FakeNode('option', 'Extra High', {
    role: 'menuitem', 'aria-checked': scenario.level === 'extra-high' ? 'true' : 'false',
  });
  const pro = new FakeNode('option', 'Pro', {
    role: 'menuitem', 'aria-checked': scenario.level === 'heavy' ? 'true' : 'false',
  });
  for (const option of [high, extraHigh, pro]) option.parentElement = effortMenu;
  advanced.query = (selector) => selector.includes('aria-haspopup="menu"') ? [opener] : [];
  owner.query = (selector) => {
    if (selector.includes('slider-simple')) return [ownerSlider];
    if (selector.includes('slider-advanced')) return [advanced];
    if (selector === '[role="menuitem"]') return [advancedToggle];
    return [];
  };
  splitRoot.query = (selector) => selector.includes('slider-advanced') && splitAdvanced ? [splitAdvanced] : [];
  unrelatedRoot.query = (selector) => selector.includes('slider-simple') && unrelatedSlider ? [unrelatedSlider] : [];
  effortMenu.query = (selector) => selector === '[role="menuitem"]' ? [high, extraHigh, pro] : [];
  const ids = { 'picker-owner': owner, 'picker-split': splitRoot, 'picker-unrelated': unrelatedRoot, 'effort-menu': effortMenu };
  const allSliders = [ownerSlider, ...(unrelatedSlider ? [unrelatedSlider] : [])];
  const allAdvanced = [advanced, ...(splitAdvanced ? [splitAdvanced] : [])];
  const document = {
    body: page,
    dispatchEvent() {},
    getElementById: (id) => ids[id] ?? null,
    querySelectorAll(selector) {
      if (selector === MODEL_BUTTON_SELECTOR || selector.includes('__composer-pill')) return [pill];
      if (selector === MENU_CONTAINER_SELECTOR) return [owner, effortMenu, splitRoot, unrelatedRoot];
      if (selector.includes('slider-simple')) return allSliders;
      if (selector.includes('slider-advanced')) return allAdvanced;
      if (selector.includes('composer-intelligence-picker-content')) return [owner];
      return [];
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; },
  };
  let proofObservations = 0;
  const observations = scenario.observations ?? [true, true];
  const window = {
    getComputedStyle(node) {
      let display = node === page ? (scenario.ancestorDisplay ?? 'block') : 'block';
      if (node === pill) {
        const visible = observations[Math.min(proofObservations, observations.length - 1)];
        proofObservations += 1;
        if (!visible) display = 'none';
      }
      return {
        display,
        visibility: 'visible',
        opacity: node === page ? (scenario.ancestorOpacity ?? '1') : '1',
      };
    },
  };
  let tick = 0;
  const performance = { now: () => (tick += 100) };
  const setTimeout = (resolve) => { resolve(); return 0; };
  const status = (await AsyncFunction(
    'document', 'window', 'performance', 'setTimeout',
    `return (${expressionFor(scenario.level)});`,
  )(document, window, performance, setTimeout)).status;
  return { status, observations: proofObservations };
};
const results = {};
for (const scenario of scenarios) results[scenario.label] = await run(scenario);
console.log(JSON.stringify(results));
""".replace("SCENARIOS", scenarios)
    completed = subprocess.run(
        ["node", "--input-type=module"],
        input=source + harness,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_oracle_0172_generated_advanced_owner_proof_is_stable_and_bound(
    tmp_path: Path,
) -> None:
    results = run_gpt56_0172_advanced_owner_cases(tmp_path)

    assert results["pro-stable"] == {"status": "already-selected", "observations": 2}
    assert results["power4-owned"] == {"status": "already-selected", "observations": 2}
    for label in (
        "ancestor-display-none",
        "ancestor-opacity-zero",
        "aria-hidden",
        "split-picker",
        "one-observation-then-lost",
        "power4-unrelated",
    ):
        assert results[label]["status"] == "selection-unverified"


def test_gpt56_primary_proof_rejects_css_hidden_stale_candidates(tmp_path: Path) -> None:
    statuses = run_gpt56_primary_css_visibility_cases(tmp_path)
    assert statuses["visible"] == "already-selected"
    for label in (
        "display-none",
        "visibility-hidden",
        "visibility-collapse",
        "opacity-zero",
        "ancestor-opacity-zero",
        "aria-hidden",
        "split-picker",
    ):
        assert statuses[label] not in {"already-selected", "switched"}


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
    # 300e910c1f...) must migrate to the final strict result under
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

    prior_visible_work = tmp_path / "stage-prior-visible-proof"
    prior_visible_target = prior_visible_work / Path(relative)
    prior_visible_target.parent.mkdir(parents=True)
    prior_visible_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(
        prior_visible_work, patches / "thinkingTime.strict.pre-stable-visible-proof.patch"
    )
    prior_visible_bytes = prior_visible_target.read_bytes()
    assert digest(prior_visible_bytes) == "fd7e6fcf2f38e0367b50501e7546244f0e3e2cdb95e8905c388798c5fed5a4f5"

    prior_primary_css_work = tmp_path / "stage-prior-primary-css-proof"
    prior_primary_css_target = prior_primary_css_work / Path(relative)
    prior_primary_css_target.parent.mkdir(parents=True)
    prior_primary_css_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(
        prior_primary_css_work, patches / "thinkingTime.strict.pre-primary-css-proof.patch"
    )
    prior_primary_css_bytes = prior_primary_css_target.read_bytes()
    assert digest(prior_primary_css_bytes) == (
        "5378da62f4374fcbf0d89fad17fba576c58859ebc5e072540d2222537c835225"
    )

    prior_ancestor_work = tmp_path / "stage-prior-ancestor-opacity-proof"
    prior_ancestor_target = prior_ancestor_work / Path(relative)
    prior_ancestor_target.parent.mkdir(parents=True)
    prior_ancestor_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(
        prior_ancestor_work,
        patches / "thinkingTime.strict.pre-ancestor-opacity-proof.patch",
    )
    prior_ancestor_bytes = prior_ancestor_target.read_bytes()
    assert digest(prior_ancestor_bytes) == (
        "2cf9f56afc8815533403020cde71063c775146acbac1fd5932906f9bf626d6a8"
    )

    prior_coherent_work = tmp_path / "stage-prior-coherent-picker-proof"
    prior_coherent_target = prior_coherent_work / Path(relative)
    prior_coherent_target.parent.mkdir(parents=True)
    prior_coherent_target.write_bytes(pristine.replace(b"\r\n", b"\n"))
    compat._apply_patch(
        prior_coherent_work,
        patches / "thinkingTime.strict.pre-coherent-picker-proof.patch",
    )
    prior_coherent_bytes = prior_coherent_target.read_bytes()
    assert digest(prior_coherent_bytes) == (
        "01ad2aca046895140729866ab5da3b0e7cfd92a00618d61f1d4b9b4cf36365eb"
    )

    backup = tmp_path / "backup"
    for label, legacy_bytes in (
        ("era-lf", era_bytes),
        ("era-crlf", era_bytes.replace(b"\n", b"\r\n")),
        ("upgraded-lf", upgraded),
        ("upgraded-crlf", upgraded.replace(b"\n", b"\r\n")),
        ("prior-strict-lf", prior_bytes),
        ("prior-strict-crlf", prior_bytes.replace(b"\n", b"\r\n")),
        ("prior-visible-proof-lf", prior_visible_bytes),
        ("prior-visible-proof-crlf", prior_visible_bytes.replace(b"\n", b"\r\n")),
        ("prior-primary-css-proof-lf", prior_primary_css_bytes),
        ("prior-primary-css-proof-crlf", prior_primary_css_bytes.replace(b"\n", b"\r\n")),
        ("prior-ancestor-opacity-proof-lf", prior_ancestor_bytes),
        ("prior-ancestor-opacity-proof-crlf", prior_ancestor_bytes.replace(b"\n", b"\r\n")),
        ("prior-coherent-picker-proof-lf", prior_coherent_bytes),
        ("prior-coherent-picker-proof-crlf", prior_coherent_bytes.replace(b"\n", b"\r\n")),
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
            "c973d2801a75bc1e37526184ba257d47ae3994185776107fca60158f9f2526d8"
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

    assert compat.SUPPORTED_VERSION == "0.17.2"
    assert compat.RECOVERABLE_VERSIONS == ("0.16.1", "0.17.0", "0.17.1", "0.17.2")
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
        "dist/src/browser/actions/thinkingTime.js": ("508f1fbc175b82e6bfd4c978da6199306800615f432e28d7721c155c402795ca", "c973d2801a75bc1e37526184ba257d47ae3994185776107fca60158f9f2526d8"),
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
    assert "aria-controls" in thinking_patch
    assert "proofTrees" in thinking_patch
    assert "collectGpt56ProProofDiagnostic" in thinking_patch
    assert "waitForStableGpt56ProProof" in thinking_patch
    assert "consecutive >= 2" in thinking_patch
    assert thinking_patch.count("waitForStableGpt56ProProof") >= 6
    assert thinking_patch.count(
        "TARGET_LEVEL !== 'heavy' || await waitForStableGpt56ProProof()"
    ) == 2
    assert "diagnosticProProof" in thinking_patch
    assert "const POWER_TARGET" in thinking_patch
    assert "strictGpt56Effort" in thinking_patch
    assert "selection-unverified" in thinking_patch
    assert "refusing to submit without confirmed ${requiredEffortLabel}" in thinking_patch
    thinking = contracts["dist/src/browser/actions/thinkingTime.js"]
    assert thinking["legacy_patched"] == [
        "01ad2aca046895140729866ab5da3b0e7cfd92a00618d61f1d4b9b4cf36365eb",
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
        "fd7e6fcf2f38e0367b50501e7546244f0e3e2cdb95e8905c388798c5fed5a4f5",
        "5378da62f4374fcbf0d89fad17fba576c58859ebc5e072540d2222537c835225",
        "2cf9f56afc8815533403020cde71063c775146acbac1fd5932906f9bf626d6a8",
    ]
    assert thinking["legacy_patch"] == "thinkingTime.strict.pre-power.patch"
    assert thinking["legacy_patches"]["01ad2aca046895140729866ab5da3b0e7cfd92a00618d61f1d4b9b4cf36365eb"] == (
        "thinkingTime.strict.pre-coherent-picker-proof.patch"
    )
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
    assert thinking["legacy_patches"]["fd7e6fcf2f38e0367b50501e7546244f0e3e2cdb95e8905c388798c5fed5a4f5"] == (
        "thinkingTime.strict.pre-stable-visible-proof.patch"
    )
    assert thinking["legacy_patches"]["5378da62f4374fcbf0d89fad17fba576c58859ebc5e072540d2222537c835225"] == (
        "thinkingTime.strict.pre-primary-css-proof.patch"
    )
    assert thinking["legacy_patches"]["2cf9f56afc8815533403020cde71063c775146acbac1fd5932906f9bf626d6a8"] == (
        "thinkingTime.strict.pre-ancestor-opacity-proof.patch"
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
        "thinkingTime.strict.pre-ancestor-opacity-proof.patch",
        "thinkingTime.strict.pre-primary-css-proof.patch",
        "thinkingTime.strict.pre-stable-visible-proof.patch",
        "thinkingTime.extra-high-fail-closed.patch",
        "thinkingTime.pro-heavy-upgrade.patch",
        "promptComposer.pre-key-event-trigger.patch",
        "promptComposer.pre-authority-chain.patch",
        "promptComposer.pre-split-trigger.patch",
        "promptComposer.pre-observational-census.patch",
    }:
        assert (compat.patch_root("0.17.1") / patch_name).is_file(), patch_name


def test_oracle_0172_has_exact_hash_gated_patches_and_preserves_0171_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    compat = load_compat()
    contracts = compat.VERSION_PATCHES["0.17.2"]
    old = compat.VERSION_PATCHES["0.17.1"]["dist/src/browser/actions/thinkingTime.js"]

    assert {
        path: (contract["pristine"], contract["patched"])
        for path, contract in contracts.items()
    } == {
        "dist/src/browser/chromeLifecycle.js": ("312b45c44d4cd69a3a057e7bd1584b58182b4b37bc88f6ce6c7d11e216267c81", "61440e467d51031efb7bfc319aef05de7c9061585e5eec148d0e353938eb2093"),
        "dist/src/browser/recoverConversation.js": ("d7e39d21acf07e6d227e761944519e11cd8d93930629cc87555d7de75a42d1ca", "cc2a036f6e2409ae7edceee1f381a5062cd6cc5cd1618af465a1b384081ed69e"),
        "dist/src/browser/profileCopy.js": ("06c692861f8a4c1a8769f957b9c582426a13bf4972262c47c1f24a87b239064f", "71459a25b7c46f57bae6f23a5498301f6f6a1d39addf0c1cd4eee1d99b03372c"),
        "dist/src/cli/browserConfig.js": ("8a355cd8828a5025ea66c401b54140152bd1fe5538254893d577d52bc4a0f852", "78d022150b959aa4cb26f2e2a743f88277246979f96813d91a4bcc55835dec18"),
        "dist/src/browser/index.js": ("335f29c8864399cf2795333e4da8b87bc1b3591c30862eb9e82ea12cd3b37d11", "9a78695ba89a6e7eb6761dd06b9be74d500ac65b585158d75f8fd3c7a6eb8895"),
        "dist/src/browser/actions/assistantResponse.js": ("0bbc106f79c6abf253690c83794a2dab1b432378f57e16542d15cfcd5365e16d", "18661304c7fb545bc327876d38045818cbd23257488137836d43661be8742af4"),
        "dist/src/browser/actions/promptComposer.js": ("db090a5fb6d13c4c88a68b5e474a53a19c3857295a64c3ba4a0eef1868d06000", "3767d8a6702e42191e8195641ad2f0834882bed9cda1362a723c906249402d96"),
        "dist/src/browser/actions/thinkingTime.js": ("303d33ebe915b27407ca22ec0da1d18729464ce50417f405ddb628c31f6fb867", "91c5d356a597fbf1a8e08cde922fd468a94f8cd3a9e441d7534fb7877a117828"),
    }
    assert all((compat.patch_root("0.17.2") / value["patch"]).is_file() for value in contracts.values())

    relative = Path("dist/src/browser/actions/thinkingTime.js")
    package = tmp_path / "package"
    target = package / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(PRISTINE_THINKING_TIME_0172)
    (package / "package.json").write_text('{"version":"0.17.2"}', encoding="utf-8")
    monkeypatch.setattr(
        compat,
        "VERSION_PATCHES",
        {"0.17.2": {str(relative).replace("\\", "/"): contracts[str(relative).replace("\\", "/")]}},
    )

    result = compat.ensure_oracle_compatibility("oracle 0.17.2", package_root=package)

    assert result["changed"] == [str(relative).replace("\\", "/")]
    assert digest(target.read_bytes()) == contracts[str(relative).replace("\\", "/")]["patched"]
    compat._apply_patch(
        package,
        compat.patch_root("0.17.2") / "thinkingTime.strict.patch",
        reverse=True,
    )
    assert digest(target.read_bytes()) == contracts[str(relative).replace("\\", "/")]["pristine"]
    compat._apply_patch(package, compat.patch_root("0.17.2") / "thinkingTime.strict.patch")
    assert digest(target.read_bytes()) == contracts[str(relative).replace("\\", "/")]["patched"]
    source = target.read_text(encoding="utf-8")
    assert "selectEffortFromAdvancedSubmenu" in source
    assert "collectGpt56PowerProofDiagnostic" in source
    assert "consecutive >= 2" in source
    assert old["patched"] == "c973d2801a75bc1e37526184ba257d47ae3994185776107fca60158f9f2526d8"
    assert old["legacy_patches"]["01ad2aca046895140729866ab5da3b0e7cfd92a00618d61f1d4b9b4cf36365eb"] == (
        "thinkingTime.strict.pre-coherent-picker-proof.patch"
    )


def test_oracle_0172_pro_diagnostic_proof_is_visible_stable_and_same_picker(
    tmp_path: Path,
) -> None:
    hidden_calls, hidden_error, _ = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "hidden", states=[True, True], hidden_ancestor=True, version="0.17.2"
    )
    stable_calls, stable_error, stable_logs = run_gpt56_pro_diagnostic_recovery_case(
        tmp_path / "stable", states=[True, True], version="0.17.2"
    )

    assert hidden_calls == stable_calls == 2
    assert "refusing to submit" in str(hidden_error)
    assert stable_error is None
    assert stable_logs == ["[browser] Thinking time: Power 5 of 5 (Pro) (already selected)"]


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
