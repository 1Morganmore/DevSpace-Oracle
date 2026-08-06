import json
import runpy
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
        'bin/codexpro_fixed_runtime_watchdog.py',
        'bin/codexpro_project_cloudflare_bootstrap.ps1',
        'skills/chatgpt-pro-browser/SKILL.md',
        'skills/chatgpt-pro-browser/agents/openai.yaml',
        'skills/chatgpt-pro-browser/scripts/build_project_context_packet.py',
        'skills/chatgpt-pro-browser/scripts/run_chatgpt_pro.py',
        'skills/chatgpt-pro-plan-handoff/scripts/run_pro_plan_handoff.py',
        'skills/chatgpt-pro-plan-handoff/schemas/*.json',
        'scripts/run_v4_contract_tests.py',
        'scripts/run_posix_tree_child.py',
        'scripts/run_windows_job_child.py',
        'contracts/install/*.json',
        'contracts/multi-gpt/*.json',
        'tests/fixtures/planner-v7-app-trace-quiescent-incident.json',
        'tests/fixtures/planner-v8-app-trace-quiescent-incident.json',
    }
    assert required <= includes
    assert not any('*' in path for path in includes if not (path.endswith('/schemas/*.json') or path in {'contracts/install/*.json', 'contracts/multi-gpt/*.json'}))
    package_files = set(json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['files'])
    assert {
        'skills/chatgpt-pro-browser/SKILL.md',
        'skills/chatgpt-pro-browser/agents/openai.yaml',
        'skills/chatgpt-pro-browser/scripts/build_project_context_packet.py',
        'skills/chatgpt-pro-browser/scripts/run_chatgpt_pro.py',
        'skills/chatgpt-pro-browser/scripts/run_pro_browser.py',
    } <= package_files


def test_compatibility_patch_assets_are_packaged() -> None:
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    includes = set(manifest['include'])
    oracle = runpy.run_path(str(ROOT / 'bin/chatgpt_oracle_compat.py'))
    oracle_assets = {
        f'bin/oracle-compat/{version}/{contract["patch"]}'
        for version, patches in oracle['VERSION_PATCHES'].items()
        for contract in patches.values()
    }
    devspace = runpy.run_path(str(ROOT / 'bin/chatgpt_devspace_compat.py'))
    devspace_assets = {
        f'bin/devspace-compat/{devspace["SUPPORTED_VERSION"]}/{contract["patch"]}'
        for contract in devspace['PATCHES'].values()
    }
    required = oracle_assets | devspace_assets | {
        'bin/devspace-compat/1.0.4/workspaces.patch',
    }
    assert required <= includes
    assert not [path for path in required if not (ROOT / path).is_file()]
    package_files = set(json.loads((ROOT / 'package.json').read_text(encoding='utf-8'))['files'])
    assert {'bin/devspace-compat/', 'bin/oracle-compat/'} <= package_files
    assert {path.name for path in (ROOT / 'bin/oracle-compat/0.16.1').glob('*.patch')} == {
        'assistantResponse.patch',
        'browserConfig.patch',
        'browserIndex.patch',
        'browserTabs.patch',
        'browserTabs.pre-readiness.patch',
        'chromeLifecycle.patch',
        'modelSelection.patch',
        'profileCopy.patch',
        'promptComposer.patch',
        'recoverConversation.patch',
    }
    assert {path.name for path in (ROOT / 'bin/oracle-compat/0.17.0').glob('*.patch')} == {
        'assistantResponse.patch',
        'browserConfig.patch',
        'browserIndex.patch',
        'chromeLifecycle.patch',
        'profileCopy.patch',
        'promptComposer.patch',
        'recoverConversation.patch',
        'thinkingTime.patch',
    }


def test_quiescent_app_trace_fixtures_never_authorize_replacement_work() -> None:
    expected = {
        'planner-v7-app-trace-quiescent-incident.json': ('v7', 'preserve the parent lock'),
        'planner-v8-app-trace-quiescent-incident.json': ('v8', 'exact persisted parent/child/session/target/canonical URL tuple'),
    }
    for filename, (planner_version, recovery_guard) in expected.items():
        fixture = json.loads((ROOT / 'tests' / 'fixtures' / filename).read_text(encoding='utf-8'))
        assert fixture['schema'] == 'codexpro.web-multi.app-trace-incident/v1'
        assert fixture['planner_version'] == planner_version
        assert fixture['state'] == 'quiescent'
        assert recovery_guard in fixture['expected_recovery']
        assert 'new' not in fixture['expected_recovery'].casefold()


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
    assert package['version'] == lock['version'] == lock['packages']['']['version'] == '1.8.0'
    assert package['engines']['node'] == lock['packages']['']['engines']['node'] == '>=24 <27'
    assert package['license'] == lock['packages']['']['license'] == 'MIT'
    assert package['repository']['url'] == 'git+https://github.com/1Morganmore/DevSpace-Oracle.git'
    assert package['homepage'].startswith('https://github.com/1Morganmore/DevSpace-Oracle')
    assert {
        'bin/chatgpt_agbrowse_bridge.py',
        'skills/chatgpt-thinking-browser/SKILL.md',
        'install.ps1',
        'LICENSE',
        'scripts/run_v4_contract_tests.py',
        'contracts/install/',
        'contracts/multi-gpt/',
        'requirements-dev.txt',
    } <= set(package['files'])


def test_release_external_versions_and_integrities_are_exact() -> None:
    external = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))['external']
    assert external['oracle'] == {
        'package': '@steipete/oracle',
        'tested_version': '0.17.0',
        'license': 'MIT',
        'integrity': 'sha512-GOViubhL+fKaEGuyrmc9mDnDFPoKYoleYw85iiITFZu1QgcjDZzxKDycnFYSg+f2cAJ6h8RHEZqXSjSXuGJSxw==',
        'installation': 'npx -y @steipete/oracle@0.17.0',
    }
    assert external['devspace']['tested_version'] == '1.0.5'
    assert external['devspace']['integrity'] == 'sha512-eloUu+bZoJsBFxaaSNK2/hegNApT+HAfu/vjYYhm/Etu3gwx+Z7tnboZfM9KAU/hvX44P40kzEHS6JBNRPGOlg=='


def test_release_workflow_installs_pytest_before_running_contract_runner() -> None:
    workflow = (ROOT / '.github/workflows/release-portability.yml').read_text(encoding='utf-8')
    requirements = (ROOT / 'requirements-dev.txt').read_text(encoding='utf-8')
    install = workflow.index('python -m pip install -r requirements-dev.txt')
    run_tests = workflow.index('python scripts/run_v4_contract_tests.py --focused')
    assert install < run_tests
    assert 'pytest>=8,<10' in requirements
    assert 'PyYAML>=6,<7' in requirements


def test_release_workflow_runs_focused_and_full_contract_checks() -> None:
    workflow = (ROOT / '.github/workflows/release-portability.yml').read_text(encoding='utf-8')
    assert 'scripts/run_v4_contract_tests.py --focused' in workflow
    assert 'scripts/run_v3_contract_tests.py' in workflow
    assert 'scripts/run_v4_contract_tests.py --full' in workflow
