from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "chatgpt-workspace-setup" / "scripts" / "devspace_tailscale_setup.py"


def load_module():
    spec = importlib.util.spec_from_file_location("devspace_tailscale_setup_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def config(tmp_path: Path):
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    return module, module.validate_config([str(root)], "device.tailnet.ts.net")


def test_roots_are_narrow_and_registration_url_is_exact(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    assert current.registration_url == "https://device.tailnet.ts.net/mcp"
    with pytest.raises(module.SetupError, match="ALLOWED_ROOT_REQUIRED"):
        module.validate_config([], "device.tailnet.ts.net")
    with pytest.raises(module.SetupError, match="ALLOWED_ROOT_TOO_BROAD"):
        module.validate_config([str(Path(tmp_path.drive + "\\"))], "device.tailnet.ts.net")


def test_setup_plan_has_no_secrets_and_is_explicit_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, current = config(tmp_path)
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))
    plan = module.setup_plan(current)
    text = json.dumps(plan)
    assert "password" not in text.lower()
    assert "token" not in text.lower()
    assert plan["registration_url"] == "https://device.tailnet.ts.net/mcp"
    assert plan["recommended_app_name"] == "DevSpace"
    assert plan["devspace_init"][1:3] == [
        "-lc",
        "exec npx --yes @waishnav/devspace@1.0.4 init",
    ]


def test_doctor_orders_local_funnel_public_and_manual_failure_branch(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    seen: list[str] = []

    class Response:
        def __init__(self, status: int = 200):
            self.status = status
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        seen.append(request.full_url)
        return Response()

    def runner(argv, **kwargs):
        assert argv == ["tailscale", "funnel", "status", "--json"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"Web": {current.hostname + ":443": {"Proxy": f"http://127.0.0.1:{current.local_port}"}}}),
            stderr="",
        )

    report = module.doctor(current, opener=opener, runner=runner, chatgpt_call_failed=True)
    assert seen == [current.local_mcp_url, current.registration_url]
    assert report["next_action"] == "MANUAL_CHATGPT_REGISTRATION_CHECK"
    assert report["registration_url"] == current.registration_url


def test_doctor_returns_local_failure_before_funnel_or_public(tmp_path: Path) -> None:
    module, current = config(tmp_path)

    def opener(request, timeout):
        raise OSError("unavailable")

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    report = module.doctor(current, opener=opener, runner=runner)
    assert report["next_action"] == "CHECK_DEVSPACE_LOCAL_SERVICE"


def test_module_has_no_chatgpt_ui_or_browser_automation() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
        "agbrowse",
        "selenium",
        "playwright",
        "tab-switch",
        ".click(",
        "chatgpt.com",
    ):
        assert forbidden not in source


def test_secret_text_is_redacted_from_funnel_diagnostics() -> None:
    module = load_module()

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="owner_token=very-secret password: also-secret")

    report = module.funnel_status(runner=runner)
    assert "very-secret" not in report["stderr"]
    assert "also-secret" not in report["stderr"]
    assert "[REDACTED]" in report["stderr"]


def test_doctor_rejects_404_and_unrelated_funnel_mapping(tmp_path: Path) -> None:
    module, current = config(tmp_path)

    class NotFound:
        status = 404
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    local_fail = module.http_probe(current.local_mcp_url, opener=lambda *args, **kwargs: NotFound())
    assert local_fail["ok"] is False
    report = module.funnel_status(
        current,
        runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"Web": {"other.ts.net:443": {"Proxy": "http://127.0.0.1:9999"}}}),
            stderr="",
        ),
    )
    assert report["ok"] is False
    assert report["error"] == "TAILSCALE_FUNNEL_MAPPING_MISSING"


def test_nondefault_public_port_is_explicit_and_existing_mapping_is_not_overwritten(tmp_path: Path) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    current = module.validate_config([str(root)], "device.tailnet.ts.net", public_port=8443)
    assert current.registration_url == "https://device.tailnet.ts.net:8443/mcp"
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"Web": {current.hostname + ":8443": {"Proxy": "http://127.0.0.1:9999"}}}),
            stderr="",
        )

    with pytest.raises(module.SetupError, match="TAILSCALE_FUNNEL_PORT_IN_USE"):
        module.apply_setup(current, runner=runner, popen_factory=lambda *args, **kwargs: None)
    assert calls == [["tailscale", "funnel", "status", "--json"]]


def test_windows_launch_is_hidden() -> None:
    module = load_module()
    kwargs = module.windows_subprocess_kwargs(platform_name="nt")
    assert kwargs["creationflags"] & module.subprocess.CREATE_NO_WINDOW


def test_setup_applies_hash_validated_devspace_compat_before_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, current = config(tmp_path)
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))
    calls: list[list[str]] = []
    launched: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": {}}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.apply_setup(
        current,
        runner=runner,
        popen_factory=lambda argv, **kwargs: launched.append(list(argv)),
    )

    assert calls[1][1:3] == [
        "-lc",
        "exec npx --yes @waishnav/devspace@1.0.4 init",
    ]
    assert calls[2] == module.devspace_compat_argv()
    assert calls[3] == module.devspace_compat_argv(stop_exact_service=True)
    assert calls[4] == module.devspace_compat_argv(confirm_restarted=True)
    assert launched and launched[0][1:3] == [
        "-lc",
        "exec npx --yes @waishnav/devspace@1.0.4 serve",
    ]
