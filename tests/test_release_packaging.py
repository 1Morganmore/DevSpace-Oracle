import json
from pathlib import Path

ROOT = Path(__file__).parents[1]

RETIRED_PATHS = {
    'bin/chatgpt_browser_runtime.py',
    'bin/chatgpt_browser_runtime_server.py',
    'bin/chatgpt_browser_runtime_worker.py',
    'bin/chatgpt_execution_evidence.py',
    'bin/chatgpt_mode_evidence.py',
    'bin/chatgpt_question_contract.py',
    'bin/chatgpt_rate_limit_modal_watcher.py',
    'bin/codexpro_connector_supervisor.ps1',
    'bin/codexpro_debug_create_submit.py',
    'bin/codexpro_developer_app_cdp.py',
    'bin/codexpro_developer_app_reconcile.mjs',
    'bin/codexpro_ensure_project_app.py',
    'bin/codexpro_ensure_project_app.ps1',
    'skills/chatgpt-pro-browser/scripts/export_result.py',
    'skills/chatgpt-pro-browser/scripts/official_search_evidence.py',
    'skills/chatgpt-pro-browser/scripts/profile_manager.py',
    'skills/chatgpt-pro-browser/scripts/recover_live_dom_transcript.py',
    'skills/chatgpt-pro-browser/scripts/report_writer.py',
    'skills/chatgpt-pro-browser/scripts/selectors.py',
    'skills/chatgpt-pro-browser/scripts/self_heal_repair_executor.py',
    'skills/chatgpt-pro-browser/scripts/self_heal_supervisor.py',
    'skills/chatgpt-pro-browser/scripts/self_heal_types.py',
    'skills/chatgpt-pro-browser/scripts/tab_lease_registry.py',
    'skills/chatgpt-pro-browser/scripts/utils.py',
    'skills/chatgpt-pro-browser/scripts/weblatch_sidecar.py',
    'skills/chatgpt-pro-browser/tests/test_chatgpt_browser_runtime.py',
    'skills/chatgpt-pro-browser/tests/test_chatgpt_mode_evidence.py',
    'skills/chatgpt-pro-browser/tests/test_codexpro_developer_app_ui_contract.py',
    'skills/chatgpt-pro-browser/tests/test_generated_image_capture.py',
    'skills/chatgpt-pro-browser/tests/test_mode_selection_helpers.py',
    'skills/chatgpt-pro-browser/tests/test_module_isolation.py',
    'skills/chatgpt-pro-browser/tests/test_profile_manager.py',
    'skills/chatgpt-pro-browser/tests/test_self_heal_supervisor.py',
    'skills/chatgpt-pro-browser/tests/test_weblatch_sidecar.py',
    'tests/test_chatgpt_execution_evidence.py',
}

def test_manifest_covers_runtime_and_schemas() -> None:
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    assert manifest['schema'] == 'codexpro.install-manifest/v1'
    includes = set(manifest['include'])
    required = {
        'bin/chatgpt_agbrowse_bridge.py',
        'bin/chatgpt_agbrowse_run.py',
        'bin/chatgpt_web_multi_runtime.py',
        'bin/chatgpt_web_multi_upstream.py',
        'bin/codexpro_agbrowse_app.py',
        'bin/codexpro_project_cloudflare_bootstrap.ps1',
        'skills/chatgpt-pro-browser/scripts/run_chatgpt_pro.py',
        'skills/chatgpt-pro-plan-handoff/scripts/run_pro_plan_handoff.py',
        'skills/chatgpt-pro-plan-handoff/schemas/*.json',
    }
    assert required <= includes
    assert not any('*' in path for path in includes if not path.endswith('/schemas/*.json'))


def test_public_install_and_npm_surface_exclude_legacy_browser_engines() -> None:
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    install_paths = set(manifest['include'])
    package_paths = set(package['files'])
    assert RETIRED_PATHS.isdisjoint(install_paths)
    assert RETIRED_PATHS.isdisjoint(package_paths)
    assert {'bin/', 'skills/', 'bin/*.py', 'skills/**/scripts/*.py'}.isdisjoint(install_paths | package_paths)


def test_retired_automation_surface_is_absent_from_repository() -> None:
    assert not [path for path in RETIRED_PATHS if (ROOT / path).exists()]

def test_public_notices_and_no_vendoring() -> None:
    assert 'Copyright (c) 2026 ventianima-lab' in (ROOT / 'LICENSE').read_text(encoding='utf-8')
    notice = (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')
    assert 'hehee9/multi-gpt@4f5e130' in notice and 'server.mjs' in notice
    assert 'missing' in notice and 'agbrowse@0.1.18' in notice
    assert not any((ROOT / name).exists() for name in ('node_modules', 'agbrowse', 'browser'))

def test_package_is_publishable_and_lockfile_matches() -> None:
    package = json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))
    lock = json.loads((ROOT / 'package-lock.json').read_text(encoding='utf-8'))
    assert package['private'] is False
    assert package['name'] == lock['name'] == lock['packages']['']['name']
    assert package['version'] == lock['version'] == lock['packages']['']['version']
    assert package['license'] == lock['packages']['']['license'] == 'MIT'
    assert package['repository']['url'] == 'git+https://github.com/ventianima-lab/codexpro-automation.git'
    assert package['homepage'].startswith('https://github.com/ventianima-lab/codexpro-automation')
    assert {
        'bin/chatgpt_agbrowse_bridge.py',
        'skills/chatgpt-thinking-browser/SKILL.md',
        'install.ps1',
        'LICENSE',
    } <= set(package['files'])


def test_release_workflow_installs_pytest_before_running_tests() -> None:
    workflow = (ROOT / '.github/workflows/release-portability.yml').read_text(encoding='utf-8')
    install = workflow.index('python -m pip install "pytest>=8,<10"')
    run_tests = workflow.index('python -m pytest -q')
    assert install < run_tests
