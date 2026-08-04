import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).parents[1]


def run_powershell(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', *args],
        text=True,
        encoding='utf-8',
        errors='replace',
        capture_output=True,
        env=env,
    )

def test_lifecycle_scripts_share_manifest_and_support_whatif() -> None:
    for name in ('install.ps1', 'doctor.ps1', 'update.ps1', 'uninstall.ps1', 'rollback.ps1'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'WhatIf' in text or 'SupportsShouldProcess' in text
    assert 'install-manifest.json' in (ROOT / 'install.ps1').read_text(encoding='utf-8')
    assert 'DEFER_ACTIVE_WORK' in (ROOT / 'update.ps1').read_text(encoding='utf-8')
    assert 'Get-ManifestFiles' in (ROOT / 'install.ps1').read_text(encoding='utf-8')
    assert 'git ' not in (ROOT / 'install.ps1').read_text(encoding='utf-8').lower()


def test_public_file_hash_helpers_are_dotnet_stream_based() -> None:
    for name in ('install.ps1', 'update.ps1', 'rollback.ps1', 'doctor.ps1'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'Get-FileHash' not in text
        assert '[IO.File]::Open' in text
        assert '[Security.Cryptography.SHA256]::Create()' in text
        assert '.Dispose()' in text
        assert '.ToLowerInvariant()' in text

def test_update_is_explicit_not_scheduled() -> None:
    workflows = list((ROOT / '.github/workflows').glob('*'))
    assert workflows
    assert all('schedule' not in path.read_text(encoding='utf-8').lower() for path in workflows)

def test_update_records_and_validates_an_atomic_contract() -> None:
    text = (ROOT / 'update.ps1').read_text(encoding='utf-8')
    for token in ('prior', 'executable_sha256', 'contract_sha256', '--expected-version', '--expected-integrity', 'DEFER_ACTIVE_WORK', 'agbrowse-update.lock'):
        assert token in text
    assert 'schedule' not in text.lower()


def test_installer_freezes_legacy_dependency_unless_explicitly_requested() -> None:
    text = (ROOT / 'install.ps1').read_text(encoding='utf-8')
    assert '$InstallLegacyRecoveryDependency' in text
    assert '$ManageLegacyDependency' in text
    assert 'legacy-recovery-dependencies-frozen' in text
    assert "Join-Path $RepoRoot 'update.ps1'" in text
    assert 'agbrowse dependency install failed' in text
    assert '-PreflightToken $dependencyPreflightToken' in text
    assert '-Preflight -AgbrowseVersion' in text


def test_installer_wal_records_actual_per_file_transition_order() -> None:
    with tempfile.TemporaryDirectory() as home:
        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )
        assert installed.returncode == 0, installed.stderr
        receipt = json.loads(next((Path(home) / 'receipts').glob('codexpro-automation-*.json')).read_text(encoding='utf-8-sig'))
        wal = json.loads(Path(receipt['wal']).read_text(encoding='utf-8'))
        assert wal['schema'] == 'codexpro.install-wal/v2'
        assert wal['transaction_id'] == receipt['transaction_id']
        assert wal['status'] == 'COMPLETE'
        assert wal['files']
        for index, entry in enumerate(wal['files']):
            assert entry['phase'] == 'COMPLETE'
            assert entry['transitions'] == [
                'INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED',
                'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE',
            ]
            replacement = Path(entry['replacement'])
            assert replacement.name == 'replacement.json'
            assert replacement.parent.name == str(index)
            assert replacement.is_file()


def test_interrupted_install_recovery_rolls_back_completed_steps_and_preserves_unmutated_intent() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        backup_root = codex_home / 'backups' / 'interrupted'
        completed_path = codex_home / 'bin' / 'completed.py'
        intent_path = codex_home / 'bin' / 'intent.py'
        completed_backup = backup_root / 'bin' / 'completed.py'
        intent_backup = backup_root / 'bin' / 'intent.py'
        for path in (completed_path, intent_path, completed_backup, intent_backup):
            path.parent.mkdir(parents=True, exist_ok=True)
        completed_path.write_bytes(b'new-completed\n')
        intent_path.write_bytes(b'old-intent\n')
        completed_backup.write_bytes(b'old-completed\n')
        intent_backup.write_bytes(b'old-intent\n')

        import hashlib
        digest = lambda value: hashlib.sha256(value).hexdigest()
        journal = {
            'schema': 'codexpro.install-wal/v1',
            'status': 'ACTIVE',
            'backup': str(backup_root),
            'files': [
                {
                    'path': 'bin/completed.py', 'action': 'overwritten',
                    'installed_sha256': digest(b'new-completed\n'),
                    'backup_sha256': digest(b'old-completed\n'),
                    'phase': 'COMPLETE', 'transitions': ['INTENT', 'MUTATED', 'VERIFIED', 'COMPLETE'],
                    'replacement': str(backup_root / 'steps/0/replacement.json'),
                },
                {
                    'path': 'bin/intent.py', 'action': 'overwritten',
                    'installed_sha256': digest(b'new-intent\n'),
                    'backup_sha256': digest(b'old-intent\n'),
                    'phase': 'INTENT', 'transitions': ['INTENT'],
                    'replacement': str(backup_root / 'steps/1/replacement.json'),
                },
            ],
        }
        wal = backup_root / 'install.wal.json'
        wal.write_text(json.dumps(journal), encoding='utf-8')

        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall',
        )
        assert recovered.returncode == 0, recovered.stderr
        assert completed_path.read_bytes() == b'old-completed\n'
        assert intent_path.read_bytes() == b'old-intent\n'
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ROLLED_BACK_AFTER_CRASH'


def test_wal_v1_contract_remains_compatible_and_v2_receipt_binding_fails_closed() -> None:
    v1 = json.loads((ROOT / 'contracts/install/install-wal-v1.schema.json').read_text(encoding='utf-8'))
    assert v1['$id'] == 'codexpro.install-wal/v1'
    assert v1['properties']['status']['enum'] == ['ACTIVE', 'COMPLETE', 'ROLLED_BACK_AFTER_CRASH']
    assert v1['$defs']['file']['properties']['phase']['enum'] == ['INTENT', 'MUTATED', 'VERIFIED', 'COMPLETE']

    with tempfile.TemporaryDirectory() as home:
        import hashlib

        codex_home = Path(home)
        backup_root = codex_home / 'backups' / 'ambiguous-receipt'
        relative = Path('bin/owned.py')
        destination = codex_home / relative
        backup = backup_root / relative
        destination.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        installed = b'installer-owned-current\n'
        original = b'original-before-install\n'
        destination.write_bytes(installed)
        backup.write_bytes(original)
        receipt = codex_home / 'receipts' / 'ambiguous.json'
        receipt.parent.mkdir(parents=True)
        wal = backup_root / 'install.wal.json'
        receipt.write_text(json.dumps({
            'schema': 'codexpro.install-receipt/v3',
            'transaction_id': 'f' * 32,
            'wal': str(wal),
            'files': [],
        }), encoding='utf-8')
        wal.write_text(json.dumps({
            'schema': 'codexpro.install-wal/v2',
            'transaction_id': 'a' * 32,
            'manifest_version': '1.7.0',
            'status': 'ACTIVE',
            'backup': str(backup_root),
            'receipt': str(receipt),
            'wal_path': str(wal),
            'files': [{
                'sequence_number': 0,
                'path': str(relative).replace('\\', '/'),
                'action': 'overwritten',
                'installed_sha256': hashlib.sha256(installed).hexdigest(),
                'backup_sha256': hashlib.sha256(original).hexdigest(),
                'phase': 'MUTATED',
                'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED'],
                'replacement': str(backup_root / 'steps/0/replacement.json'),
            }],
        }), encoding='utf-8')

        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )

        assert recovered.returncode != 0
        assert 'receipt_binding_ambiguous' in recovered.stderr
        assert destination.read_bytes() == installed
        assert receipt.is_file()
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ACTIVE'


@pytest.mark.parametrize(
    'tamper_kind',
    [
        'stored_wal_path', 'backup_binding', 'receipt_outside_root', 'sequence',
        'sequence_type', 'installed_hash', 'receipt_record', 'receipt_nullability',
        'replacement_record', 'files_type', 'completed_missing_receipt',
        'unsupported_schema', 'intent_with_backup_hash',
    ],
)
def test_wal_v2_tampering_is_rejected_before_any_recovery_mutation(tamper_kind: str) -> None:
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
        import hashlib

        codex_home = Path(home)
        backup_root = codex_home / 'backups' / 'tampered-v2'
        relative = Path('bin/owned.py')
        destination = codex_home / relative
        backup = backup_root / relative
        destination.parent.mkdir(parents=True)
        backup.parent.mkdir(parents=True)
        installed = b'installer-owned-current\n'
        original = b'user-owned-original\n'
        destination.write_bytes(installed)
        backup.write_bytes(original)
        wal = backup_root / 'install.wal.json'
        receipt = codex_home / 'receipts' / 'bound.json'
        receipt.parent.mkdir(parents=True)
        file_record = {
            'sequence_number': 0,
            'path': str(relative).replace('\\', '/'),
            'action': 'overwritten',
            'installed_sha256': hashlib.sha256(installed).hexdigest(),
            'backup_sha256': hashlib.sha256(original).hexdigest(),
            'phase': 'COMPLETE',
            'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED', 'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE'],
            'replacement': str(backup_root / 'steps/0/replacement.json'),
        }
        journal = {
            'schema': 'codexpro.install-wal/v2',
            'transaction_id': 'd' * 32,
            'manifest_version': '1.7.0',
            'status': 'ACTIVE',
            'backup': str(backup_root),
            'receipt': str(receipt),
            'wal_path': str(wal),
            'files': [file_record],
        }
        receipt_value = {
            'schema': 'codexpro.install-receipt/v3',
            'transaction_id': journal['transaction_id'],
            'manifest_version': journal['manifest_version'],
            'backup': journal['backup'],
            'wal': journal['wal_path'],
            'files': [{key: file_record[key] for key in ('path', 'action', 'installed_sha256', 'backup_sha256')}],
        }
        replacement_value = {
            'schema': 'codexpro.install-replacement/v1',
            **{key: file_record[key] for key in ('path', 'action', 'installed_sha256', 'backup_sha256')},
            'mutated_at': '2026-08-03T00:00:00Z',
        }
        replacement_path = Path(file_record['replacement'])
        replacement_path.parent.mkdir(parents=True)
        if tamper_kind == 'stored_wal_path':
            journal['wal_path'] = str(backup_root / 'other.wal.json')
        elif tamper_kind == 'backup_binding':
            journal['backup'] = str(codex_home / 'backups' / 'other-transaction')
        elif tamper_kind == 'receipt_outside_root':
            journal['receipt'] = str(Path(outside) / 'outside-receipt.json')
        elif tamper_kind == 'sequence':
            file_record['sequence_number'] = 3
        elif tamper_kind == 'sequence_type':
            file_record['sequence_number'] = '0'
        elif tamper_kind == 'installed_hash':
            file_record['installed_sha256'] = 'not-a-sha256'
        elif tamper_kind == 'receipt_record':
            receipt_value['files'][0]['path'] = 'bin/different.py'
        elif tamper_kind == 'receipt_nullability':
            receipt_value['files'][0]['backup_sha256'] = None
        elif tamper_kind == 'replacement_record':
            replacement_value['installed_sha256'] = 'e' * 64
        elif tamper_kind == 'files_type':
            journal['files'] = file_record
        elif tamper_kind == 'completed_missing_receipt':
            journal['status'] = 'COMPLETE'
        elif tamper_kind == 'unsupported_schema':
            journal['schema'] = 'codexpro.install-wal/v999'
        elif tamper_kind == 'intent_with_backup_hash':
            file_record['phase'] = 'INTENT'
            file_record['transitions'] = ['INTENT']
        replacement_path.write_text(json.dumps(replacement_value), encoding='utf-8')
        wal.write_text(json.dumps(journal), encoding='utf-8')
        if tamper_kind != 'completed_missing_receipt':
            receipt.write_text(json.dumps(receipt_value), encoding='utf-8')

        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )

        assert recovered.returncode != 0
        assert destination.read_bytes() == installed
        assert backup.read_bytes() == original
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == journal['status']


def test_wal_rollback_preflights_every_backup_before_first_mutation() -> None:
    with tempfile.TemporaryDirectory() as home:
        import hashlib

        codex_home = Path(home)
        backup_root = codex_home / 'backups' / 'two-entry-preflight'
        wal = backup_root / 'install.wal.json'
        receipt = codex_home / 'receipts' / 'planned.json'
        files = []
        destinations = []
        for index in range(2):
            relative = Path(f'bin/owned-{index}.py')
            destination = codex_home / relative
            backup = backup_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup.parent.mkdir(parents=True, exist_ok=True)
            installed = f'installed-{index}\n'.encode()
            original = f'original-{index}\n'.encode()
            destination.write_bytes(installed)
            backup.write_bytes(b'tampered\n' if index == 0 else original)
            destinations.append((destination, installed))
            files.append({
                'sequence_number': index,
                'path': str(relative).replace('\\', '/'),
                'action': 'overwritten',
                'installed_sha256': hashlib.sha256(installed).hexdigest(),
                'backup_sha256': hashlib.sha256(original).hexdigest(),
                'phase': 'MUTATED',
                'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED'],
                'replacement': str(backup_root / f'steps/{index}/replacement.json'),
            })
        journal = {
            'schema': 'codexpro.install-wal/v2', 'transaction_id': 'c' * 32,
            'manifest_version': '1.7.0', 'status': 'ACTIVE', 'backup': str(backup_root),
            'receipt': str(receipt), 'wal_path': str(wal), 'files': files,
        }
        wal.parent.mkdir(parents=True, exist_ok=True)
        wal.write_text(json.dumps(journal), encoding='utf-8')

        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )

        assert recovered.returncode != 0
        assert 'missing_interrupted_backup' in recovered.stderr
        for destination, installed in destinations:
            assert destination.read_bytes() == installed
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ACTIVE'


@pytest.mark.parametrize(
    'fault_point',
    [
        'AFTER_INTENT', 'AFTER_BACKUP_DURABLE', 'AFTER_MUTATION',
        'AFTER_VERIFICATION', 'AFTER_REPLACEMENT_RECEIPT',
    ],
)
def test_current_process_fault_rolls_back_the_active_wal_entry(fault_point: str) -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        first = codex_home / 'bin' / 'chatgpt_agbrowse_bridge.py'
        first.parent.mkdir(parents=True)
        original = b'user-owned-before-fault\x00\n'
        first.write_bytes(original)
        env = os.environ.copy()
        env['CODEXPRO_INSTALL_FAULT_POINT'] = fault_point

        failed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall', env=env,
        )

        assert failed.returncode != 0
        assert 'INSTALL_FAULT_INJECTED' in failed.stderr
        assert first.read_bytes() == original
        assert not list((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        wal = next((codex_home / 'backups').glob('*/install.wal.json'))
        state = json.loads(wal.read_text(encoding='utf-8'))
        assert state['status'] == 'ROLLED_BACK_AFTER_ERROR'
        assert state['files'][0]['phase'] in {
            'INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED', 'REPLACEMENT_RECEIPT_DURABLE',
        }


def test_current_process_fault_removes_a_just_created_destination() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        created = codex_home / 'bin' / 'chatgpt_agbrowse_bridge.py'
        env = os.environ.copy()
        env['CODEXPRO_INSTALL_FAULT_POINT'] = 'AFTER_MUTATION'

        failed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall', env=env,
        )

        assert failed.returncode != 0
        assert not created.exists()


def test_install_receipt_failure_window_rolls_back_files_and_removes_receipt() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        first = codex_home / 'bin' / 'chatgpt_agbrowse_bridge.py'
        first.parent.mkdir(parents=True)
        original = b'original-before-install-receipt\n'
        first.write_bytes(original)
        env = os.environ.copy()
        env['CODEXPRO_INSTALL_FAULT_POINT'] = 'AFTER_INSTALL_RECEIPT'

        failed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall', env=env,
        )

        assert failed.returncode != 0
        assert first.read_bytes() == original
        assert not list((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        wal = next((codex_home / 'backups').glob('*/install.wal.json'))
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ROLLED_BACK_AFTER_ERROR'


def test_wal_v1_missing_overwritten_destination_remains_absent() -> None:
    with tempfile.TemporaryDirectory() as home:
        import hashlib

        codex_home = Path(home)
        relative = Path('bin/legacy-missing.py')
        backup_root = codex_home / 'backups' / 'missing-destination'
        backup = backup_root / relative
        backup.parent.mkdir(parents=True)
        original = b'original-before-crash\n'
        backup.write_bytes(original)
        installed = b'installer-owned-before-crash\n'
        journal = {
            'schema': 'codexpro.install-wal/v1',
            'status': 'ACTIVE',
            'backup': str(backup_root),
            'files': [{
                'path': str(relative).replace('\\', '/'),
                'action': 'overwritten',
                'installed_sha256': hashlib.sha256(installed).hexdigest(),
                'backup_sha256': hashlib.sha256(original).hexdigest(),
                'phase': 'MUTATED',
                'transitions': ['INTENT', 'MUTATED'],
                'replacement': str(backup_root / 'steps/0/replacement.json'),
            }],
        }
        wal = backup_root / 'install.wal.json'
        wal.write_text(json.dumps(journal), encoding='utf-8')
        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall',
        )

        assert recovered.returncode == 0, recovered.stderr
        assert not (codex_home / relative).exists()
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ROLLED_BACK_AFTER_CRASH'


def test_wal_v2_missing_overwritten_destination_is_a_conflict() -> None:
    with tempfile.TemporaryDirectory() as home:
        import hashlib

        codex_home = Path(home)
        relative = Path('bin/v2-missing.py')
        backup_root = codex_home / 'backups' / 'missing-v2-destination'
        backup = backup_root / relative
        backup.parent.mkdir(parents=True)
        original = b'original-before-crash\n'
        installed = b'installer-owned-before-crash\n'
        backup.write_bytes(original)
        wal = backup_root / 'install.wal.json'
        receipt = codex_home / 'receipts' / 'planned.json'
        receipt.parent.mkdir(parents=True)
        journal = {
            'schema': 'codexpro.install-wal/v2',
            'transaction_id': 'b' * 32,
            'manifest_version': '1.7.0',
            'status': 'ACTIVE',
            'backup': str(backup_root),
            'receipt': str(receipt),
            'wal_path': str(wal),
            'files': [{
                'sequence_number': 0,
                'path': str(relative).replace('\\', '/'),
                'action': 'overwritten',
                'installed_sha256': hashlib.sha256(installed).hexdigest(),
                'backup_sha256': hashlib.sha256(original).hexdigest(),
                'phase': 'MUTATED',
                'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED'],
                'replacement': str(backup_root / 'steps/0/replacement.json'),
            }],
        }
        wal.write_text(json.dumps(journal), encoding='utf-8')

        recovered = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )

        assert recovered.returncode != 0
        assert 'missing_overwritten_destination' in recovered.stderr
        assert not (codex_home / relative).exists()
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ACTIVE'


def test_doctor_accepts_current_v3_install_receipt_schema() -> None:
    with tempfile.TemporaryDirectory() as home:
        root = Path(home)
        receipt = root / 'receipts' / 'codexpro-automation-current.json'
        receipt.parent.mkdir(parents=True)
        receipt.write_text(
            json.dumps({
                'schema': 'codexpro.install-receipt/v3',
                'backup': str(root / 'backups' / 'owned'),
                'files': [],
                'dependency': {'mode': 'skipped'},
            }),
            encoding='utf-8',
        )

        result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', home)

        assert result.returncode == 0, result.stdout
        report = json.loads(result.stdout)
        assert report['status'] == 'PASS'
        assert report['oracle']['package'] == '@steipete/oracle@0.17.0'
        assert report['devspace']['tested_version'] == '1.0.5'
        assert 'npx -y @steipete/oracle@0.17.0 --version' in report['commands']
        assert any(item['code'] == 'LEGACY_CONTRACT_UNAVAILABLE' for item in report['warnings'])
        assert 'RECEIPT_INVALID' not in result.stdout
        assert 'unsupported install receipt schema' not in result.stdout


def test_doctor_rejects_compatibility_module_with_unreceipted_patch_asset() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )
        assert installed.returncode == 0, installed.stderr
        readback = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', home)
        assert readback.returncode == 0, readback.stdout
        receipt = next((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        value = json.loads(receipt.read_text(encoding='utf-8-sig'))
        missing = 'bin/oracle-compat/0.17.0/assistantResponse.patch'
        value['files'] = [record for record in value['files'] if record['path'] != missing]
        (codex_home / missing).unlink()
        receipt.write_text(json.dumps(value), encoding='utf-8')

        result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', home)

        assert result.returncode != 0
        report = json.loads(result.stdout)
        assert [
            issue['path'] for issue in report['issues']
            if issue['code'] == 'COMPAT_PATCH_ASSET_MISSING'
        ] == [missing]


def test_failed_dependency_preflight_leaves_existing_managed_file_byte_identical() -> None:
    """The read-only dependency gate must run before staging or committing manifest files."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as mock_bin:
        codex_home = Path(home)
        managed = codex_home / 'bin' / 'chatgpt_agbrowse_tabs.py'
        managed.parent.mkdir(parents=True)
        original = b'user-owned-before-preflight\x00\n'
        managed.write_bytes(original)
        mock = Path(mock_bin)
        (mock / 'npm.cmd').write_text(
            '@echo off\nif "%~1"=="view" (echo "wrong-integrity"\nexit /b 0)\nexit /b 1\n',
            encoding='utf-8',
        )
        (mock / 'python.cmd').write_text('@echo off\nexit /b 0\n', encoding='utf-8')
        env = os.environ.copy()
        env['PATH'] = str(mock) + os.pathsep + env['PATH']
        result = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-InstallLegacyRecoveryDependency', env=env,
        )
        assert result.returncode != 0
        assert managed.read_bytes() == original

def test_uninstall_and_rollback_require_receipt_ownership() -> None:
    rollback = (ROOT / 'rollback.ps1').read_text(encoding='utf-8')
    uninstall = (ROOT / 'uninstall.ps1').read_text(encoding='utf-8')
    assert 'receipt must be owned by this CODEX_HOME' in rollback
    assert 'codexpro.install-receipt/v3' in rollback
    assert "'rollback.ps1'" in uninstall

def test_receipt_lifecycle_rejects_forged_traversal_and_preserves_modified_file() -> None:
    with tempfile.TemporaryDirectory() as home:
        root = Path(home)
        receipt = root / 'receipts' / 'codexpro-automation-forged.json'
        receipt.parent.mkdir()
        receipt.write_text('{"schema":"codexpro.install-receipt/v2","backup":"'+str(root / 'backups').replace('\\','\\\\')+'","files":[{"path":"../outside","action":"created","installed_sha256":"0"}]}', encoding='utf-8')
        result = run_powershell('-File', str(ROOT/'rollback.ps1'), '-CodexHome', home, '-Receipt', str(receipt))
        assert result.returncode != 0


def test_temp_codex_home_install_and_rollback_is_exact_inverse() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        overwritten = codex_home / 'bin' / 'chatgpt_agbrowse_tabs.py'
        overwritten.parent.mkdir(parents=True)
        original = b'user-owned-original\n'
        overwritten.write_bytes(original)

        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'),
            '-CodexHome', home,
            '-SkipDependencyInstall',
        )
        assert installed.returncode == 0, installed.stderr
        receipts = sorted((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        assert len(receipts) == 1
        created = codex_home / 'bin' / 'chatgpt_agbrowse_composer.py'
        installed_pro_skill = codex_home / 'skills' / 'chatgpt-pro-browser' / 'SKILL.md'
        installed_pro_metadata = codex_home / 'skills' / 'chatgpt-pro-browser' / 'agents' / 'openai.yaml'
        installed_oracle_patch = codex_home / 'bin' / 'oracle-compat' / '0.17.0' / 'assistantResponse.patch'
        installed_devspace_patch = codex_home / 'bin' / 'devspace-compat' / '1.0.5' / 'workspaces.patch'
        assert overwritten.read_bytes() != original
        assert created.is_file()
        assert installed_pro_skill.read_bytes() == (
            ROOT / 'skills' / 'chatgpt-pro-browser' / 'SKILL.md'
        ).read_bytes()
        assert installed_pro_metadata.read_bytes() == (
            ROOT / 'skills' / 'chatgpt-pro-browser' / 'agents' / 'openai.yaml'
        ).read_bytes()
        assert b'allow_implicit_invocation: true' in installed_pro_metadata.read_bytes()
        assert installed_oracle_patch.is_file()
        assert installed_devspace_patch.is_file()

        rolled_back = run_powershell(
            '-File', str(ROOT / 'rollback.ps1'),
            '-CodexHome', home,
            '-Receipt', str(receipts[0]),
        )
        assert rolled_back.returncode == 0, rolled_back.stderr
        assert overwritten.read_bytes() == original
        assert not created.exists()
        assert not installed_pro_skill.exists()
        assert not installed_pro_metadata.exists()
        assert not installed_oracle_patch.exists()
        assert not installed_devspace_patch.exists()
        assert '"status":  "COMPLETE"' in rolled_back.stdout


def test_normal_install_dependency_receipt_rolls_back_mocked_npm_and_contract_exactly() -> None:
    """The normal (non-skip) path must own update.ps1's exact inverse evidence."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as mock_bin:
        codex_home = Path(home)
        state = Path(home) / 'mock-npm-version.txt'
        state.write_text('1.2.3\n', encoding='utf-8')
        contracts = codex_home / 'contracts'
        contracts.mkdir()
        target_contract = contracts / 'agbrowse-0.1.18.json'
        target_contract.write_text('{"before":"target"}\n', encoding='utf-8')
        previous_update = codex_home / 'agbrowse-update-receipt.json'
        previous_update.write_text('{"before":"update"}\n', encoding='utf-8')
        mock = Path(mock_bin)
        baseline_integrity = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))['external']['agbrowse']['integrity']
        (mock / 'npm.cmd').write_text(
            '@echo off\nsetlocal EnableDelayedExpansion\nset state=%MOCK_NPM_STATE%\n'
            'if "%~1"=="list" (set /p version=<"%state%"\nif "!version!"=="" (echo {"dependencies":{}}) else echo {"dependencies":{"agbrowse":{"version":"!version!"}}}\nexit /b 0)\n'
            f'if "%~1"=="view" (if "%~2"=="agbrowse@0.1.18" (echo "{baseline_integrity}") else echo "fake-prior-integrity"\nexit /b 0)\n'
            'if "%~1"=="install" (set package=%~3\nset version=!package:agbrowse@=!\n>"%state%" echo !version!\nexit /b 0)\n'
            'if "%~1"=="uninstall" (>"%state%" echo.\nexit /b 0)\nexit /b 1\n', encoding='utf-8')
        (mock / 'python.cmd').write_text(
            '@echo off\nsetlocal EnableDelayedExpansion\n:loop\nif "%~1"=="" exit /b 0\nif "%~1"=="--output" (set output=%~2\n>"!output!" echo {"agbrowse":{"npmIntegrity":"fake-integrity"}}\nexit /b 0)\nshift\ngoto loop\n', encoding='utf-8')
        (mock / 'agbrowse.cmd').write_text('@echo off\necho mocked\n', encoding='utf-8')
        env = os.environ.copy()
        env['PATH'] = str(mock) + os.pathsep + env['PATH']
        env['MOCK_NPM_STATE'] = str(state)

        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-InstallLegacyRecoveryDependency', env=env,
        )
        assert installed.returncode == 0, installed.stderr
        receipt = next((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        value = json.loads(receipt.read_text(encoding='utf-8-sig'))
        assert value['schema'] == 'codexpro.install-receipt/v3'
        assert value['dependency']['mode'] == 'applied'
        assert Path(value['dependency']['receipt']).is_file()
        assert state.read_text(encoding='utf-8').strip() == '0.1.18'

        rolled_back = run_powershell('-File', str(ROOT / 'rollback.ps1'), '-CodexHome', home, '-Receipt', str(receipt), env=env)
        assert rolled_back.returncode == 0, rolled_back.stderr
        assert state.read_text(encoding='utf-8').strip() == '1.2.3'
        assert target_contract.read_text(encoding='utf-8') == '{"before":"target"}\n'
        assert previous_update.read_text(encoding='utf-8') == '{"before":"update"}\n'
        assert '"status":  "COMPLETE"' in rolled_back.stdout


def test_dependency_inverse_conflict_is_non_complete_and_preserves_current_dependency() -> None:
    # Contract checks in update.ps1 must refuse a drifted target rather than claim COMPLETE.
    update = (ROOT / 'update.ps1').read_text(encoding='utf-8')
    rollback = (ROOT / 'rollback.ps1').read_text(encoding='utf-8')
    assert "status='CONFLICT'" in update
    assert "status='PARTIAL'" in update
    assert "status='CONFLICT'" in rollback
    assert 'dependency_preflight_incomplete' in rollback
    assert 'dependency_rollback_incomplete' in rollback


def test_dependency_inverse_rejects_registry_integrity_mismatch_after_mocked_install() -> None:
    """A successful npm exit is insufficient: the recorded prior integrity must still match."""
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as mock_bin:
        codex_home = Path(home)
        state = codex_home / 'mock-npm-version.txt'
        state.write_text('1.2.3\n', encoding='utf-8')
        mock = Path(mock_bin)
        baseline_integrity = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))['external']['agbrowse']['integrity']
        (mock / 'npm.cmd').write_text(
            '@echo off\nsetlocal EnableDelayedExpansion\nset state=%MOCK_NPM_STATE%\n'
            'if "%~1"=="list" (set /p version=<"%state%"\nif "!version!"=="" (echo {"dependencies":{}}) else echo {"dependencies":{"agbrowse":{"version":"!version!"}}}\nexit /b 0)\n'
            f'if "%~1"=="view" (if "%~2"=="agbrowse@0.1.18" (echo "{baseline_integrity}") else echo "%MOCK_PRIOR_INTEGRITY%"\nexit /b 0)\n'
            'if "%~1"=="install" (set package=%~3\nset version=!package:agbrowse@=!\n>"%state%" echo !version!\nexit /b 0)\n'
            'if "%~1"=="uninstall" (>"%state%" echo.\nexit /b 0)\nexit /b 1\n', encoding='utf-8')
        (mock / 'python.cmd').write_text('@echo off\n:loop\nif "%~1"=="" exit /b 0\nif "%~1"=="--output" (>"%~2" echo {"agbrowse":{"npmIntegrity":"mock"}}\nexit /b 0)\nshift\ngoto loop\n', encoding='utf-8')
        (mock / 'agbrowse.cmd').write_text('@echo off\necho mocked\n', encoding='utf-8')
        env = os.environ.copy()
        env.update({'PATH': str(mock) + os.pathsep + env['PATH'], 'MOCK_NPM_STATE': str(state), 'MOCK_PRIOR_INTEGRITY': 'recorded-prior-integrity'})
        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-InstallLegacyRecoveryDependency', env=env,
        )
        assert installed.returncode == 0, installed.stderr
        receipt = next((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        env['MOCK_PRIOR_INTEGRITY'] = 'drifted-prior-integrity'
        rolled_back = run_powershell('-File', str(ROOT / 'rollback.ps1'), '-CodexHome', home, '-Receipt', str(receipt), env=env)
        assert rolled_back.returncode == 3
        assert 'PARTIAL' in rolled_back.stdout
        assert state.read_text(encoding='utf-8').strip() == '1.2.3'


def test_install_dependency_recovery_and_rollback_preflight_are_ordered() -> None:
    install = (ROOT / 'install.ps1').read_text(encoding='utf-8')
    rollback = (ROOT / 'rollback.ps1').read_text(encoding='utf-8')
    update = (ROOT / 'update.ps1').read_text(encoding='utf-8')
    assert '$dependencyApplied=$true' in install
    assert '$dependencySourceReceipt' in install
    assert '$isCurrentUpdateReceipt' in update
    assert 'dependency_preflight_incomplete' in rollback
    assert rollback.index('$dependencyPreflight=') < rollback.index('$conflicts=@();foreach($record')


def test_uninstall_preserves_modified_created_file_and_reports_conflict() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'),
            '-CodexHome', home,
            '-SkipDependencyInstall',
        )
        assert installed.returncode == 0, installed.stderr
        receipt = next((codex_home / 'receipts').glob('codexpro-automation-*.json'))
        modified = codex_home / 'bin' / 'chatgpt_agbrowse_tabs.py'
        modified.write_text('user modified after install\n', encoding='utf-8')

        uninstalled = run_powershell(
            '-File', str(ROOT / 'uninstall.ps1'),
            '-CodexHome', home,
            '-Receipt', str(receipt),
        )

        assert uninstalled.returncode == 2
        assert modified.read_text(encoding='utf-8') == 'user modified after install\n'
        assert 'preserved_modified_created' in uninstalled.stdout


def test_receipt_sibling_prefix_and_external_backup_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        sibling = codex_home / 'receipts-evil' / 'forged.json'
        sibling.parent.mkdir()
        sibling.write_text(json.dumps({
            'schema': 'codexpro.install-receipt/v2',
            'backup': str(codex_home / 'backups' / 'owned'),
            'files': [],
        }), encoding='utf-8')
        sibling_result = run_powershell(
            '-File', str(ROOT / 'rollback.ps1'), '-CodexHome', home, '-Receipt', str(sibling)
        )
        assert sibling_result.returncode != 0

        receipt = codex_home / 'receipts' / 'forged.json'
        receipt.parent.mkdir()
        receipt.write_text(json.dumps({
            'schema': 'codexpro.install-receipt/v2',
            'backup': str(codex_home.parent / 'external-backup'),
            'files': [],
        }), encoding='utf-8')
        backup_result = run_powershell(
            '-File', str(ROOT / 'rollback.ps1'), '-CodexHome', home, '-Receipt', str(receipt)
        )
        assert backup_result.returncode != 0
