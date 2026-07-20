from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin" / "codexpro_exact_unit_cloudflare_bootstrap.ps1"


def test_exact_bootstrap_declares_fail_closed_server_contract() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert '"--bash", "off"' in text
    assert '"--write", "workspace"' in text
    assert '"--tool-mode", "full"' in text
    assert '"--allow-home"' not in text
    assert '"--tunnel", "cloudflare"' in text
    assert "codexpro_windows_process_identity.py" in text
    assert "parallel-exact-unit" in text


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("CODEXPRO_RUN_WINDOWS_EXACT_UNIT_INTEGRATION") != "1",
    reason="bounded live Windows exact-unit integration is opt-in",
)
def test_exact_bootstrap_dry_run_on_windows(tmp_path: Path) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if not powershell or not npx:
        pytest.skip("PowerShell or npx unavailable")
    unit = tmp_path / "unit"
    unit.mkdir()
    completed = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-File",
            str(SCRIPT),
            "-UnitRoot",
            str(unit),
            "-TopologyReceipt",
            "a" * 64,
            "-NpxPath",
            str(npx),
            "-StateRoot",
            str(tmp_path / "state"),
            "-DryRun",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert '"scope_mode":  "parallel-exact-unit"' in completed.stdout or '"scope_mode":"parallel-exact-unit"' in completed.stdout
