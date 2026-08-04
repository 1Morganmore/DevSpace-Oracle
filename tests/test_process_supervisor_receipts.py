from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'


def load_script(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(f'test_{name}', SCRIPTS / name)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


@pytest.mark.parametrize(
    'script_name',
    ['run_posix_tree_child.py', 'run_windows_job_child.py'],
)
def test_receipt_writer_does_not_follow_the_old_predictable_temporary_alias(
    tmp_path: Path,
    script_name: str,
) -> None:
    module = load_script(script_name)
    result_path = tmp_path / 'receipt.json'
    protected = tmp_path / 'protected.txt'
    protected.write_text('do not overwrite', encoding='utf-8')
    predictable = result_path.with_name(f'.{result_path.name}.{os.getpid()}.tmp')
    try:
        predictable.symlink_to(protected)
    except OSError:
        os.link(protected, predictable)

    module._write_result(result_path, {'exit_code': 0}, 'nonce-expected')

    assert protected.read_text(encoding='utf-8') == 'do not overwrite'
    leaf = os.lstat(result_path)
    assert stat.S_ISREG(leaf.st_mode)
    assert not stat.S_ISLNK(leaf.st_mode)
    assert json.loads(result_path.read_text(encoding='utf-8')) == {
        'exit_code': 0,
        'receipt_nonce': 'nonce-expected',
    }


@pytest.mark.parametrize(
    'script_name',
    ['run_posix_tree_child.py', 'run_windows_job_child.py'],
)
def test_receipt_nonce_is_required_by_the_supervisor_cli(
    tmp_path: Path,
    script_name: str,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / script_name),
            '--hard-timeout-seconds', '1',
            '--result-file', str(tmp_path / 'result.json'),
            '--cancel-file', str(tmp_path / 'cancel'),
            '--parent-pid', str(os.getpid()),
            '--', sys.executable, '-c', 'raise SystemExit(0)',
        ],
        text=True,
        capture_output=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert '--receipt-nonce' in completed.stderr


def test_windows_parent_death_signal_is_process_local() -> None:
    module = load_script('run_windows_job_child.py')
    parent_dead = module.threading.Event()

    module._handle_parent_wait_result(0, parent_dead)

    assert parent_dead.is_set()


def test_windows_parent_wait_failure_is_rejected(tmp_path: Path) -> None:
    module = load_script('run_windows_job_child.py')
    parent_dead = module.threading.Event()

    with pytest.raises(RuntimeError, match='parent process wait failed'):
        module._handle_parent_wait_result(0xFFFFFFFF, parent_dead)

    assert not parent_dead.is_set()
