import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import pytest

ROOT = Path(__file__).parents[1]


def run_powershell(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', *args],
        capture_output=True,
        env=env,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        completed.stdout.decode('utf-8', errors='replace'),
        completed.stderr.decode('utf-8', errors='replace'),
    )


def install_mutex_name(home: Path) -> str:
    canonical = str(home.resolve()).rstrip("\\/").upper()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return rf"Local\codexpro-automation-install-{digest}"


def start_mutex_holder(
    home: Path,
    ready: Path,
    *,
    acquire: bool = True,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["CODEXPRO_TEST_INSTALL_MUTEX"] = install_mutex_name(home)
    env["CODEXPRO_TEST_MUTEX_READY"] = str(ready)
    acquire_mutex = "if(!$mutex.WaitOne(0)){exit 2};" if acquire else ""
    script = (
        "$mutex=[Threading.Mutex]::new($false,$env:CODEXPRO_TEST_INSTALL_MUTEX);"
        f"{acquire_mutex}"
        "[IO.File]::WriteAllText($env:CODEXPRO_TEST_MUTEX_READY,'ready');"
        "Start-Sleep -Seconds 120"
    )
    holder = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not ready.exists() and holder.poll() is None:
        time.sleep(0.05)
    if not ready.exists():
        holder.wait(timeout=5)
        raise AssertionError(f"mutex holder did not become ready: exit={holder.returncode}")
    return holder


def make_removal_upgrade_fixture(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    repo = tmp_path / 'repo'
    home = tmp_path / 'home'
    (repo / 'bin').mkdir(parents=True)
    home.mkdir()
    for name in ('install.ps1', 'rollback.ps1', 'uninstall.ps1'):
        shutil.copy2(ROOT / name, repo / name)
    (repo / 'bin' / 'active.py').write_text('active\n', encoding='utf-8')
    (repo / 'install-manifest.json').write_text(json.dumps({
        'schema': 'codexpro.install-manifest/v1',
        'version': '1.9.0',
        'include': ['bin/active.py'],
        'external': {},
    }), encoding='utf-8')
    legacy = home / 'bin' / 'legacy.py'
    legacy.parent.mkdir(parents=True)
    legacy_bytes = b'owned-by-1.8.0\n'
    legacy.write_bytes(legacy_bytes)
    write_v3_receipt(home, legacy, 'previous')
    return repo, home, legacy, legacy_bytes


def write_v3_receipt(
    home: Path,
    installed: Path,
    name: str,
    *,
    previous_receipt: Path | None = None,
    manifest_version: str = '1.8.0',
) -> Path:
    relative = installed.relative_to(home).as_posix()
    digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    transaction = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
    backup = home / 'backups' / name
    replacement = backup / 'steps' / '0' / 'replacement.json'
    wal = backup / 'install.wal.json'
    receipt = home / 'receipts' / f'codexpro-automation-{name}.json'
    replacement.parent.mkdir(parents=True)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    replacement.write_text(json.dumps({
        'schema': 'codexpro.install-replacement/v1', 'path': relative,
        'action': 'created', 'installed_sha256': digest, 'backup_sha256': None,
    }), encoding='utf-8')
    record = {
        'sequence_number': 0, 'path': relative, 'action': 'created',
        'installed_sha256': digest, 'backup_sha256': None, 'phase': 'COMPLETE',
        'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED',
                        'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE'],
        'replacement': str(replacement),
    }
    wal.write_text(json.dumps({
        'schema': 'codexpro.install-wal/v2', 'transaction_id': transaction,
        'manifest_version': manifest_version, 'status': 'COMPLETE', 'backup': str(backup),
        'receipt': str(receipt), 'wal_path': str(wal), 'files': [record],
    }), encoding='utf-8')
    receipt_value = {
        'schema': 'codexpro.install-receipt/v3', 'transaction_id': transaction,
        'manifest_version': manifest_version, 'backup': str(backup), 'wal': str(wal),
        'files': [{k: record[k] for k in ('path', 'action', 'installed_sha256', 'backup_sha256')}],
        'dependency': {'mode': 'skipped'},
    }
    if previous_receipt is not None:
        receipt_value['previous_receipt'] = str(previous_receipt)
    receipt.write_text(json.dumps(receipt_value), encoding='utf-8')
    return receipt


def expand_manifest_files(manifest: dict) -> list[str]:
    """Mirror install.ps1 Get-ManifestFiles: expand include patterns against ROOT."""
    import fnmatch
    import re
    files: list[str] = []
    for pattern in manifest['include']:
        assert isinstance(pattern, str) and pattern, 'manifest include pattern must be a nonempty string'
        assert not re.search(r'(^|/)\.{1,2}(/|$)', pattern), f'unsafe manifest pattern: {pattern}'
        assert not os.path.isabs(pattern), f'unsafe manifest pattern: {pattern}'
        prefix = next((
            p for p in ('bin/', 'skills/', 'mcp_servers/', 'scripts/', 'contracts/', 'tests/fixtures/')
            if pattern.startswith(p)
        ), None)
        assert prefix is not None, f'unsupported manifest root: {pattern}'
        matched = []
        for item in (ROOT / prefix.rstrip('/')).rglob('*'):
            if not item.is_file():
                continue
            assert not item.is_symlink(), f'manifest refuses symlink: {item}'
            relative = item.relative_to(ROOT).as_posix()
            if fnmatch.fnmatchcase(relative, pattern):
                matched.append(relative)
        assert matched, f'manifest pattern matched no files: {pattern}'
        files.extend(matched)
    return sorted(set(files))


def install_v4_fixture(
    home: Path,
    name: str,
    *,
    manifest_version: str | None = None,
    state_module_text: str | None = None,
) -> Path:
    """Copy current manifest-expanded files into home and write a fully bound v4
    receipt + COMPLETE v3 WAL in install.ps1's exact shapes (no install.ps1 run).
    Returns the receipt path. Mutate one dimension after building to fail doctor."""
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    version = manifest['version'] if manifest_version is None else manifest_version
    transaction = hashlib.md5(name.encode(), usedforsecurity=False).hexdigest()
    backup = home / 'backups' / name
    wal = backup / 'install.wal.json'
    receipt = home / 'receipts' / f'codexpro-automation-{name}.json'
    receipt.parent.mkdir(parents=True, exist_ok=True)
    (backup / 'steps').mkdir(parents=True)
    records = []
    receipt_files = []
    for index, relative in enumerate(expand_manifest_files(manifest)):
        destination = home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative == 'bin/chatgpt_oracle_state.py' and state_module_text is not None:
            destination.write_text(state_module_text, encoding='utf-8')
        else:
            shutil.copy2(ROOT / relative, destination)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        replacement = backup / 'steps' / str(index) / 'replacement.json'
        replacement.parent.mkdir(parents=True)
        replacement.write_text(json.dumps({
            'schema': 'codexpro.install-replacement/v1', 'path': relative,
            'action': 'created', 'installed_sha256': digest, 'backup_sha256': None,
        }), encoding='utf-8')
        record = {
            'sequence_number': index, 'path': relative, 'action': 'created',
            'installed_sha256': digest, 'backup_sha256': None, 'phase': 'COMPLETE',
            'transitions': ['INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED',
                            'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE'],
            'replacement': str(replacement),
        }
        records.append(record)
        receipt_files.append({
            k: record[k] for k in ('path', 'action', 'installed_sha256', 'backup_sha256')
        })
    wal.write_text(json.dumps({
        'schema': 'codexpro.install-wal/v3', 'transaction_id': transaction,
        'manifest_version': version, 'status': 'COMPLETE', 'backup': str(backup),
        'receipt': str(receipt), 'wal_path': str(wal), 'created_at': '2026-08-14T00:00:00.0000000Z',
        'completed_at': '2026-08-14T00:00:00.0000000Z', 'files': records,
    }), encoding='utf-8')
    receipt.write_text(json.dumps({
        'schema': 'codexpro.install-receipt/v4', 'transaction_id': transaction,
        'installed_at': '2026-08-14T00:00:00.0000000Z', 'manifest_version': version,
        'backup': str(backup), 'files': receipt_files, 'previous_receipt': None,
        'wal': str(wal),
    }), encoding='utf-8')
    return receipt


def run_fixture_install(repo: Path, home: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return run_powershell('-File', str(repo / 'install.ps1'), '-CodexHome', str(home), env=env)

@pytest.mark.skipif(os.name != 'nt', reason='Windows MAX_PATH regression')
def test_install_overwrites_deep_skill_from_orca_length_codex_home(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    relative = Path('skills/chatgpt-deep-research-browser/agents/openai.yaml')
    source = repo / relative
    source.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'install.ps1', repo / 'install.ps1')
    source.write_text('new\n', encoding='utf-8')
    (repo / 'install-manifest.json').write_text(json.dumps({
        'schema': 'codexpro.install-manifest/v1',
        'version': '1.9.0',
        'include': [relative.as_posix()],
        'external': {},
    }), encoding='utf-8')
    home_base = tmp_path / 'home'
    padding = max(1, 100 - len(str(home_base)) - 1)
    home = home_base / ('h' * padding)
    destination = home / relative
    destination.parent.mkdir(parents=True)
    destination.write_text('old\n', encoding='utf-8')

    installed = run_powershell(
        '-File', str(repo / 'install.ps1'), '-CodexHome', str(home),
        '-SkipDependencyInstall',
    )

    assert installed.returncode == 0, installed.stderr
    assert destination.read_text(encoding='utf-8') == 'new\n'
    receipt_path = next((home / 'receipts').glob('*.json'))
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    record = next(item for item in receipt['files'] if item['path'] == relative.as_posix())
    assert record['action'] == 'overwritten'
    assert Path(receipt['backup'], relative).read_text(encoding='utf-8') == 'old\n'


def test_removal_upgrade_writes_wal_v3_receipt_v4_and_rolls_back_exactly(tmp_path: Path) -> None:
    repo, home, legacy, legacy_bytes = make_removal_upgrade_fixture(tmp_path)

    installed = run_fixture_install(repo, home)

    assert installed.returncode == 0, installed.stderr
    receipt_path = next(path for path in (home / 'receipts').glob('*.json')
                        if json.loads(path.read_text(encoding='utf-8'))['schema'] == 'codexpro.install-receipt/v4')
    receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
    wal = json.loads(Path(receipt['wal']).read_text(encoding='utf-8'))
    removed = next(record for record in receipt['files'] if record['action'] == 'removed')
    wal_removed = next(record for record in wal['files'] if record['action'] == 'removed')
    assert receipt['schema'] == 'codexpro.install-receipt/v4'
    assert wal['schema'] == 'codexpro.install-wal/v3'
    assert wal_removed['transitions'] == [
        'INTENT', 'BACKUP_DURABLE', 'MUTATED', 'VERIFIED',
        'REPLACEMENT_RECEIPT_DURABLE', 'COMPLETE',
    ]
    assert removed['expected_absence'] is True
    assert removed['transaction_id'] == receipt['transaction_id'] == removed['rollback_binding']
    assert Path(removed['backup']).read_bytes() == legacy_bytes
    assert not legacy.exists()

    rolled_back = run_powershell('-File', str(repo / 'rollback.ps1'), '-CodexHome', str(home),
                                 '-Receipt', str(receipt_path))
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert legacy.read_bytes() == legacy_bytes
    assert not (home / 'bin' / 'active.py').exists()


def test_removal_upgrade_reinstall_after_rollback_keeps_linear_receipt_chain(
    tmp_path: Path,
) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    baseline_receipts = set((home / 'receipts').glob('*.json'))
    first = run_fixture_install(repo, home)
    assert first.returncode == 0, first.stderr
    first_receipt = next(
        path
        for path in (home / 'receipts').glob('*.json')
        if json.loads(path.read_text(encoding='utf-8'))['schema']
        == 'codexpro.install-receipt/v4'
    )
    rolled_back = run_powershell(
        '-File',
        str(repo / 'rollback.ps1'),
        '-CodexHome',
        str(home),
        '-Receipt',
        str(first_receipt),
    )
    assert rolled_back.returncode == 0, rolled_back.stderr

    second = run_fixture_install(repo, home)
    assert second.returncode == 0, second.stderr
    second_paths = set((home / 'receipts').glob('*.json')) - baseline_receipts - {
        first_receipt,
    }
    assert len(second_paths) == 1
    second_receipt = second_paths.pop()

    third = run_fixture_install(repo, home)
    assert third.returncode == 0, third.stderr
    third_paths = set((home / 'receipts').glob('*.json')) - baseline_receipts - {
        first_receipt,
        second_receipt,
    }
    assert len(third_paths) == 1
    third_receipt = third_paths.pop()

    assert json.loads(second_receipt.read_text(encoding='utf-8'))[
        'previous_receipt'
    ] == str(first_receipt)
    assert json.loads(third_receipt.read_text(encoding='utf-8'))[
        'previous_receipt'
    ] == str(second_receipt)
    assert not legacy.exists()


def test_removal_upgrade_uses_nearest_owner_in_single_receipt_branch(
    tmp_path: Path,
) -> None:
    repo, home, _, _ = make_removal_upgrade_fixture(tmp_path)
    known: set[Path] = set((home / 'receipts').glob('*.json'))
    first = run_fixture_install(repo, home)
    assert first.returncode == 0, first.stderr
    first_receipt = (set((home / 'receipts').glob('*.json')) - known).pop()
    known.add(first_receipt)

    second = run_fixture_install(repo, home)
    assert second.returncode == 0, second.stderr
    second_receipt = (set((home / 'receipts').glob('*.json')) - known).pop()
    known.add(second_receipt)

    (repo / 'bin' / 'active.py').write_text('changed\n', encoding='utf-8')
    third = run_fixture_install(repo, home)
    assert third.returncode == 0, third.stderr
    third_receipt = (set((home / 'receipts').glob('*.json')) - known).pop()
    known.add(third_receipt)
    assert (home / 'bin' / 'active.py').read_text(encoding='utf-8') == 'changed\n'

    rolled_back = run_powershell(
        '-File',
        str(repo / 'rollback.ps1'),
        '-CodexHome',
        str(home),
        '-Receipt',
        str(third_receipt),
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert (home / 'bin' / 'active.py').read_text(encoding='utf-8') == 'active\n'

    fourth = run_fixture_install(repo, home)
    assert fourth.returncode == 0, fourth.stderr
    fourth_receipt = (set((home / 'receipts').glob('*.json')) - known).pop()
    fourth_value = json.loads(fourth_receipt.read_text(encoding='utf-8'))
    assert fourth_value['previous_receipt'] == str(third_receipt)
    assert json.loads(third_receipt.read_text(encoding='utf-8'))[
        'previous_receipt'
    ] == str(second_receipt)
    assert json.loads(second_receipt.read_text(encoding='utf-8'))[
        'previous_receipt'
    ] == str(first_receipt)
    assert (home / 'bin' / 'active.py').read_text(encoding='utf-8') == 'changed\n'




def test_removal_upgrade_refuses_single_chain_without_current_owner(
    tmp_path: Path,
) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    installed = run_fixture_install(repo, home)
    assert installed.returncode == 0, installed.stderr
    receipt = next(
        path
        for path in (home / 'receipts').glob('*.json')
        if json.loads(path.read_text(encoding='utf-8'))['schema']
        == 'codexpro.install-receipt/v4'
    )
    rolled_back = run_powershell(
        '-File',
        str(repo / 'rollback.ps1'),
        '-CodexHome',
        str(home),
        '-Receipt',
        str(receipt),
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    legacy.write_text('user-modified-after-rollback\n', encoding='utf-8')

    retried = run_fixture_install(repo, home)

    assert retried.returncode != 0
    assert 'INSTALL_UPGRADE_AMBIGUOUS_RECEIPT' in retried.stderr
    assert legacy.read_text(encoding='utf-8') == 'user-modified-after-rollback\n'
    assert not (home / 'bin' / 'active.py').exists()


@pytest.mark.parametrize('kind,expected', [
    ('modified', 'preserved_modified_removed'),
    ('symlink', 'preserved_symlink_removed'),
])
def test_removal_upgrade_preserves_changed_or_symlink_destination(
    tmp_path: Path, kind: str, expected: str,
) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    if kind == 'modified':
        legacy.write_text('user-modified\n', encoding='utf-8')
    else:
        target = home / 'user-target.py'
        target.write_text('user-target\n', encoding='utf-8')
        legacy.unlink()
        try:
            legacy.symlink_to(target)
        except OSError:
            target_bin = home / 'target-bin'
            shutil.move(str(home / 'bin'), target_bin)
            (target_bin / 'legacy.py').write_text('user-target\n', encoding='utf-8')
            linked = subprocess.run(
                ['cmd', '/c', 'mklink', '/J', str(home / 'bin'), str(target_bin)],
                capture_output=True,
            )
            assert linked.returncode == 0, linked.stderr.decode(errors='replace')

    installed = run_fixture_install(repo, home)

    assert installed.returncode != 0
    assert expected in ''.join(installed.stderr.split())
    assert legacy.exists()
    assert not (home / 'bin' / 'active.py').exists()


def test_removal_upgrade_refuses_ambiguous_receipts_before_mutation(tmp_path: Path) -> None:
    repo, home, legacy, legacy_bytes = make_removal_upgrade_fixture(tmp_path)
    write_v3_receipt(home, legacy, 'also-claims-current')

    installed = run_fixture_install(repo, home)

    assert installed.returncode != 0
    assert 'INSTALL_UPGRADE_AMBIGUOUS_RECEIPT' in installed.stderr
    assert legacy.read_bytes() == legacy_bytes
    assert not (home / 'bin' / 'active.py').exists()


def test_removal_upgrade_refuses_disconnected_receipt_cycle_before_mutation(
    tmp_path: Path,
) -> None:
    repo, home, legacy, legacy_bytes = make_removal_upgrade_fixture(tmp_path)
    stale_target = home / 'bin' / 'stale-cycle.py'
    stale_target.write_text('receipt-owned\n', encoding='utf-8')
    cycle = write_v3_receipt(home, stale_target, 'self-cycle')
    cycle_value = json.loads(cycle.read_text(encoding='utf-8'))
    cycle_value['previous_receipt'] = str(cycle)
    cycle.write_text(json.dumps(cycle_value), encoding='utf-8')
    stale_target.write_text('user-changed\n', encoding='utf-8')

    installed = run_fixture_install(repo, home)

    assert installed.returncode != 0
    assert 'INSTALL_UPGRADE_AMBIGUOUS_RECEIPT' in installed.stderr
    assert legacy.read_bytes() == legacy_bytes
    assert stale_target.read_text(encoding='utf-8') == 'user-changed\n'
    assert not (home / 'bin' / 'active.py').exists()


def test_removal_upgrade_selects_unique_receipt_chain_head(tmp_path: Path) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    previous = home / 'receipts' / 'codexpro-automation-previous.json'
    head = write_v3_receipt(
        home,
        legacy,
        'current-head',
        previous_receipt=previous,
    )

    installed = run_fixture_install(repo, home)

    assert installed.returncode == 0, installed.stderr
    receipts = [
        json.loads(path.read_text(encoding='utf-8'))
        for path in (home / 'receipts').glob('*.json')
        if json.loads(path.read_text(encoding='utf-8'))['schema']
        == 'codexpro.install-receipt/v4'
    ]
    assert len(receipts) == 1
    assert receipts[0]['previous_receipt'] == str(head)
    assert not legacy.exists()


def test_removal_upgrade_prefers_unique_owner_over_stale_valid_receipts(
    tmp_path: Path,
) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    stale_target = home / 'bin' / 'stale.py'
    stale_target.write_text('no-longer-owned\n', encoding='utf-8')
    write_v3_receipt(home, stale_target, 'stale-valid')
    stale_target.write_text('user-changed\n', encoding='utf-8')

    installed = run_fixture_install(repo, home)

    assert installed.returncode == 0, installed.stderr
    assert not legacy.exists()
    assert stale_target.read_text(encoding='utf-8') == 'user-changed\n'


def test_removal_upgrade_refuses_active_legacy_run_before_mutation(tmp_path: Path) -> None:
    repo, home, legacy, legacy_bytes = make_removal_upgrade_fixture(tmp_path)
    run = home / 'state' / 'chatgpt-agbrowse' / 'projects' / 'p' / 'runs' / 'r' / 'run.json'
    run.parent.mkdir(parents=True)
    run.write_text(json.dumps({'phase': 'SUBMITTED'}), encoding='utf-8')

    installed = run_fixture_install(repo, home)

    assert installed.returncode != 0
    assert 'INSTALL_UPGRADE_ACTIVE_LEGACY_RUN' in installed.stderr
    assert legacy.read_bytes() == legacy_bytes
    assert not (home / 'bin' / 'active.py').exists()


@pytest.mark.parametrize('crash_point', [
    'BEFORE_REMOVAL', 'AFTER_REMOVAL_BACKUP_DURABLE', 'AFTER_REMOVAL',
    'AFTER_REMOVAL_RECEIPT', 'AFTER_INSTALL_RECEIPT',
])
def test_removal_upgrade_hard_crash_recovers_before_retry(tmp_path: Path, crash_point: str) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    env = os.environ.copy()
    env['CODEXPRO_INSTALL_HARD_CRASH_POINT'] = crash_point

    crashed = run_fixture_install(repo, home, env)
    assert crashed.returncode == 86

    recovered = run_fixture_install(repo, home)

    assert recovered.returncode == 0, recovered.stderr
    old_wals = [json.loads(path.read_text(encoding='utf-8')) for path in (home / 'backups').glob('*/install.wal.json')]
    assert any(wal['status'] == 'ROLLED_BACK_AFTER_CRASH' for wal in old_wals)
    assert any(wal['status'] == 'COMPLETE' and wal['schema'] == 'codexpro.install-wal/v3' for wal in old_wals)
    assert not legacy.exists()


def test_removed_rollback_preflights_recreated_destination_before_any_inverse(tmp_path: Path) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    installed = run_fixture_install(repo, home)
    assert installed.returncode == 0, installed.stderr
    receipt = next(path for path in (home / 'receipts').glob('*.json')
                   if json.loads(path.read_text(encoding='utf-8'))['schema'] == 'codexpro.install-receipt/v4')
    legacy.write_text('user-created-after-upgrade\n', encoding='utf-8')
    active = home / 'bin' / 'active.py'

    rolled_back = run_powershell('-File', str(repo / 'rollback.ps1'), '-CodexHome', str(home),
                                 '-Receipt', str(receipt))

    assert rolled_back.returncode == 2
    assert 'destination_recreated_removed' in rolled_back.stdout
    assert legacy.read_text(encoding='utf-8') == 'user-created-after-upgrade\n'
    assert active.exists(), 'preflight conflict must prevent partial rollback'

def test_lifecycle_scripts_share_manifest_and_support_whatif() -> None:
    for name in ('install.ps1', 'doctor.ps1', 'uninstall.ps1', 'rollback.ps1'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'WhatIf' in text or 'SupportsShouldProcess' in text
    assert 'install-manifest.json' in (ROOT / 'install.ps1').read_text(encoding='utf-8')
    assert 'Get-ManifestFiles' in (ROOT / 'install.ps1').read_text(encoding='utf-8')
    assert 'git ' not in (ROOT / 'install.ps1').read_text(encoding='utf-8').lower()


def test_whatif_does_not_create_codex_home_or_receipts(tmp_path: Path) -> None:
    target = tmp_path / 'absent-home'

    result = run_powershell(
        '-File', str(ROOT / 'install.ps1'), '-CodexHome', str(target), '-WhatIf',
    )

    assert result.returncode == 0, result.stderr
    assert 'Would stage and install' in result.stdout
    assert not target.exists()


def test_public_file_hash_helpers_are_dotnet_stream_based() -> None:
    for name in ('install.ps1', 'rollback.ps1', 'doctor.ps1'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert 'Get-FileHash' not in text
        assert '[IO.File]::Open' in text
        assert '[Security.Cryptography.SHA256]::Create()' in text
        assert '.Dispose()' in text
        assert '.ToLowerInvariant()' in text

def test_installer_wal_records_actual_per_file_transition_order() -> None:
    with tempfile.TemporaryDirectory() as home:
        installed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home, '-SkipDependencyInstall',
        )
        assert installed.returncode == 0, installed.stderr
        receipt = json.loads(next((Path(home) / 'receipts').glob('codexpro-automation-*.json')).read_text(encoding='utf-8-sig'))
        wal = json.loads(Path(receipt['wal']).read_text(encoding='utf-8'))
        assert wal['schema'] == 'codexpro.install-wal/v3'
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
        first = codex_home / 'bin' / 'chatgpt_oracle_run.py'
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


def test_stage_cleanup_failure_does_not_mask_install_error() -> None:
    with tempfile.TemporaryDirectory() as home:
        env = os.environ.copy()
        env['CODEXPRO_INSTALL_FAULT_POINT'] = 'AFTER_MUTATION'
        env['CODEXPRO_INSTALL_CLEANUP_FAULT_POINT'] = 'BEFORE_STAGE_CLEANUP'

        failed = run_powershell(
            '-File', str(ROOT / 'install.ps1'), '-CodexHome', home,
            '-SkipDependencyInstall', env=env,
        )

        assert failed.returncode != 0
        assert 'INSTALL_FAULT_INJECTED: AFTER_MUTATION' in failed.stderr
        assert 'INSTALL_CLEANUP_FAULT_INJECTED' not in failed.stderr


def test_current_process_fault_removes_a_just_created_destination() -> None:
    with tempfile.TemporaryDirectory() as home:
        codex_home = Path(home)
        created = codex_home / 'bin' / 'chatgpt_oracle_run.py'
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
        first = codex_home / 'bin' / 'chatgpt_oracle_run.py'
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
        assert 'missing_overwritten_destination' in ''.join(recovered.stderr.split())
        assert not (codex_home / relative).exists()
        assert json.loads(wal.read_text(encoding='utf-8'))['status'] == 'ACTIVE'



def test_doctor_rejects_stale_manifest_version_receipt(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    install_v4_fixture(home, 'stale-1-8-0', manifest_version='1.8.0')

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    assert report['manifest_version'] == manifest['version']
    mismatch = next(
        issue for issue in report['issues']
        if issue['code'] == 'RECEIPT_MANIFEST_VERSION_MISMATCH'
    )
    assert mismatch['receipt'] == '1.8.0'
    assert mismatch['current'] == manifest['version']
    assert report['oracle'] is None


def test_doctor_rejects_installed_oracle_active_version_mismatch(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    tested = manifest['external']['oracle']['tested_version']
    source = (ROOT / 'bin' / 'chatgpt_oracle_state.py').read_text(encoding='utf-8')
    state_module_text = source.replace(
        f'ORACLE_ACTIVE_VERSION = "{tested}"',
        'ORACLE_ACTIVE_VERSION = "9.9.9"',
    )
    install_v4_fixture(home, 'active-map-mismatch', state_module_text=state_module_text)

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    mismatch = next(
        issue for issue in report['issues']
        if issue['code'] == 'ORACLE_ACTIVE_VERSION_MISMATCH'
    )
    assert mismatch['installed'] == '9.9.9'
    assert mismatch['expected'] == tested
    assert report['oracle'] is None


def test_doctor_rejects_compatibility_module_with_unreceipted_patch_asset(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    install_v4_fixture(home, 'current')
    missing = 'bin/oracle-compat/0.17.1/assistantResponse.patch'
    (home / missing).unlink()

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert [
        issue['path'] for issue in report['issues']
        if issue['code'] == 'COMPAT_PATCH_ASSET_MISSING'
    ] == [missing]


def test_doctor_rejects_truncated_receipt_with_null_oracle(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    receipt = install_v4_fixture(home, 'truncated')
    receipt.write_text('{"schema": "codexpro.install-receipt/v4"', encoding='utf-8')

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    assert any(issue['code'] == 'RECEIPT_INVALID' for issue in report['issues'])
    assert report['oracle'] is None


def test_doctor_rejects_tampered_state_module_hash_with_null_oracle(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    install_v4_fixture(home, 'tampered')
    state_module = home / 'bin' / 'chatgpt_oracle_state.py'
    state_module.write_text(
        state_module.read_text(encoding='utf-8') + '\n# user tampered after install\n',
        encoding='utf-8',
    )

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    assert any(
        issue['code'] == 'HASH_MISMATCH' and issue['path'] == 'bin/chatgpt_oracle_state.py'
        for issue in report['issues']
    )
    assert report['oracle'] is None


def test_doctor_rejects_exact_case_package_mismatch(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
    package = manifest['external']['oracle']['package']
    source = (ROOT / 'bin' / 'chatgpt_oracle_state.py').read_text(encoding='utf-8')
    state_module_text = source.replace(
        f'ORACLE_PACKAGE = "{package}"',
        f'ORACLE_PACKAGE = "{package.upper()}"',
    )
    install_v4_fixture(home, 'case-package', state_module_text=state_module_text)

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    mismatch = next(
        issue for issue in report['issues']
        if issue['code'] == 'ORACLE_PACKAGE_MISMATCH'
    )
    assert mismatch['installed'] == package.upper()
    assert mismatch['expected'] == package
    assert report['oracle'] is None


@pytest.mark.parametrize('mutation, fragment', [
    (
        lambda source: source + '\nORACLE_ACTIVE_VERSION = "0.17.3"\n',
        'DUPLICATE:ORACLE_ACTIVE_VERSION',
    ),
    (
        lambda source: source.replace(
            'ORACLE_ACTIVE_VERSION = "0.17.3"',
            'ORACLE_ACTIVE_VERSION = REGULAR_MODEL',
        ),
        'NONLITERAL:ORACLE_ACTIVE_VERSION',
    ),
    (
        lambda source: source + '\nORACLE_PACKAGE: str = "@steipete/oracle"\n',
        'ANNASSIGN:ORACLE_PACKAGE',
    ),
    (
        lambda source: source + '\nORACLE_ACTIVE_VERSION += ""\n',
        'AUGASSIGN:ORACLE_ACTIVE_VERSION',
    ),
    (
        lambda source: source + '\ndel ORACLE_ACTIVE_VERSION\n',
        'DELETE:ORACLE_ACTIVE_VERSION',
    ),
    (
        lambda source: source + '\ndef _rebind():\n    ORACLE_ACTIVE_VERSION = "0.17.3"\n',
        'NESTED:ORACLE_ACTIVE_VERSION',
    ),
    (
        lambda source: source + '\n(ORACLE_ACTIVE_VERSION := "9.9.9")\n',
        'REBIND:ORACLE_ACTIVE_VERSION',
    ),
])
def test_doctor_rejects_state_authority_rebinding(tmp_path: Path, mutation, fragment) -> None:
    home = tmp_path / 'home'
    source = (ROOT / 'bin' / 'chatgpt_oracle_state.py').read_text(encoding='utf-8')
    install_v4_fixture(home, 'rebinding', state_module_text=mutation(source))

    result = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', str(home))

    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report['status'] == 'FAIL'
    invalid = next(
        issue for issue in report['issues']
        if issue['code'] == 'ORACLE_STATE_INVALID'
    )
    assert fragment in invalid['detail']
    assert report['oracle'] is None


def test_uninstall_and_rollback_require_receipt_ownership() -> None:
    rollback = (ROOT / 'rollback.ps1').read_text(encoding='utf-8')
    uninstall = (ROOT / 'uninstall.ps1').read_text(encoding='utf-8')
    assert 'receipt must be owned by this CODEX_HOME' in rollback
    assert 'codexpro.install-receipt/v4' in rollback
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
        overwritten = codex_home / 'bin' / 'chatgpt_oracle_run.py'
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
        created = codex_home / 'bin' / 'chatgpt_oracle_state.py'
        installed_pro_skill = codex_home / 'skills' / 'chatgpt-pro-browser' / 'SKILL.md'
        installed_pro_metadata = codex_home / 'skills' / 'chatgpt-pro-browser' / 'agents' / 'openai.yaml'
        installed_oracle_patch = codex_home / 'bin' / 'oracle-compat' / '0.17.1' / 'assistantResponse.patch'
        installed_devspace_patch = codex_home / 'bin' / 'devspace-compat' / '1.0.7' / 'workspaces.patch'
        assert overwritten.read_bytes() != original
        assert created.is_file()
        assert installed_pro_skill.read_bytes() == (
            ROOT / 'skills' / 'chatgpt-pro-browser' / 'SKILL.md'
        ).read_bytes()
        assert installed_pro_metadata.read_bytes() == (
            ROOT / 'skills' / 'chatgpt-pro-browser' / 'agents' / 'openai.yaml'
        ).read_bytes()
        assert b'allow_implicit_invocation: false' in installed_pro_metadata.read_bytes()
        assert installed_oracle_patch.is_file()
        assert installed_devspace_patch.is_file()

        manifest = json.loads((ROOT / 'install-manifest.json').read_text(encoding='utf-8'))
        oracle_contract = manifest['external']['oracle']
        doctor = run_powershell('-File', str(ROOT / 'doctor.ps1'), '-CodexHome', home)
        assert doctor.returncode == 0, doctor.stdout
        report = json.loads(doctor.stdout)
        assert report['status'] == 'PASS'
        assert report['manifest_version'] == manifest['version']
        assert report['oracle']['package'] == (
            f"{oracle_contract['package']}@{oracle_contract['tested_version']}"
        )
        assert report['oracle']['tested_version'] == oracle_contract['tested_version']
        assert report['oracle']['command'] == oracle_contract['installation']
        assert Path(report['oracle']['evidence']).samefile(created)
        assert report['devspace']['tested_version'] == manifest['external']['devspace']['tested_version']
        assert f"{oracle_contract['installation']} --version" in report['commands']

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
        modified = codex_home / 'bin' / 'chatgpt_oracle_run.py'
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


def test_install_refuses_second_writer_before_mutation(tmp_path: Path) -> None:
    repo, home, legacy, legacy_bytes = make_removal_upgrade_fixture(tmp_path)
    wal_path = next((home / "backups").glob("*/install.wal.json"))
    interrupted = json.loads(wal_path.read_text(encoding="utf-8"))
    interrupted["status"] = "ACTIVE"
    wal_path.write_text(json.dumps(interrupted), encoding="utf-8")
    holder = start_mutex_holder(home, tmp_path / "holder-ready")
    try:
        installed = run_fixture_install(repo, home)
    finally:
        holder.terminate()
        holder.wait(timeout=10)

    assert installed.returncode != 0
    assert "INSTALL_ALREADY_RUNNING" in installed.stderr
    assert legacy.read_bytes() == legacy_bytes
    assert not (home / "bin" / "active.py").exists()
    assert len(list((home / "backups").glob("*/install.wal.json"))) == 1
    assert len(list((home / "receipts").glob("*.json"))) == 1
    assert json.loads(wal_path.read_text(encoding="utf-8"))["status"] == "ACTIVE"


def test_install_recovers_abandoned_writer_mutex(tmp_path: Path) -> None:
    repo, home, legacy, _ = make_removal_upgrade_fixture(tmp_path)
    holder = start_mutex_holder(home, tmp_path / "abandoned-owner-ready")
    keeper = start_mutex_holder(home, tmp_path / "abandoned-keeper-ready", acquire=False)
    holder.kill()
    holder.wait(timeout=10)
    try:
        installed = run_fixture_install(repo, home)
    finally:
        keeper.terminate()
        keeper.wait(timeout=10)

    assert installed.returncode == 0, installed.stderr
    assert "Recovered abandoned install lock" in installed.stdout
    assert not legacy.exists()
    assert (home / "bin" / "active.py").read_text(encoding="utf-8") == "active\n"


def test_install_releases_writer_mutex_after_success(tmp_path: Path) -> None:
    repo, home, _, _ = make_removal_upgrade_fixture(tmp_path)
    keeper = start_mutex_holder(home, tmp_path / "release-keeper-ready", acquire=False)
    try:
        first = run_fixture_install(repo, home)
        second = run_fixture_install(repo, home)
    finally:
        keeper.terminate()
        keeper.wait(timeout=10)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "Recovered abandoned install lock" not in second.stdout
