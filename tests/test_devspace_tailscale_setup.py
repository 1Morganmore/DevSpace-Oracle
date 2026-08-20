from __future__ import annotations

import importlib.util
import json
import sys
import time
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


def verified_service_inspector(*, local_port: int):
    return {
        "ok": True,
        "ready": True,
        "version": "1.0.7",
        "service_status": "match",
        "service_restart_required": False,
        "package_roots": [r"C:\Users\owner\AppData\Local\npm-cache\tested-package"],
        "service_identity": {
            "pid": 4242,
            "command_line": r'node "C:\tested\dist\cli.js" serve --token=never-persist',
            "started_at_unix_ns": 123456789,
            "local_port": local_port,
        },
    }


def mismatched_service_inspector(*, local_port: int):
    return {
        "ok": True,
        "ready": False,
        "version": "1.0.7",
        "service_status": "mismatch",
        "service_restart_required": False,
        "package_roots": [r"C:\secret\package-root"],
        "service_identity": {
            "pid": 9999,
            "command_line": "python impostor.py --password=never-persist",
            "started_at_unix_ns": 987654321,
            "local_port": local_port,
        },
    }


def test_roots_are_narrow_and_registration_url_is_exact(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    assert current.registration_url == "https://device.tailnet.ts.net/mcp"
    with pytest.raises(module.SetupError, match="ALLOWED_ROOT_REQUIRED"):
        module.validate_config([], "device.tailnet.ts.net")
    with pytest.raises(module.SetupError, match="ALLOWED_ROOT_TOO_BROAD"):
        module.validate_config([str(Path(tmp_path.drive + "\\"))], "device.tailnet.ts.net")


def test_roots_command_preserves_config_and_auth_then_reads_back(tmp_path: Path) -> None:
    module = load_module()
    config_dir = tmp_path / ".devspace"
    config_dir.mkdir()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config_path = config_dir / "config.json"
    auth_path = config_dir / "auth.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1", "port": 7676, "allowedRoots": [str(first)],
        "publicBaseUrl": "https://device.tailnet.ts.net",
    }), encoding="utf-8")
    auth_path.write_text('{"ownerToken":"do-not-touch"}', encoding="utf-8")
    before_auth = auth_path.read_bytes()

    preview = module.configure_roots([str(second)], apply=False, config_path=config_path)
    assert preview["changed"] is True
    assert json.loads(config_path.read_text(encoding="utf-8"))["allowedRoots"] == [str(first)]

    result = module.configure_roots([str(second)], apply=True, config_path=config_path)
    readback = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["readback"] == [str(second.resolve())]
    assert result["restart_required"] is True
    assert readback["allowedRoots"] == [str(second.resolve())]
    assert readback["publicBaseUrl"] == "https://device.tailnet.ts.net"
    assert auth_path.read_bytes() == before_auth


def test_roots_restart_reuses_exact_service_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"port": 8765, "allowedRoots": [str(root)]}), encoding="utf-8")
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))
    calls: list[list[str]] = []
    launched: list[list[str]] = []

    result = module.configure_roots(
        [str(root)], apply=True, restart=True, config_path=config_path,
        runner=lambda argv, **kwargs: calls.append(list(argv)) or SimpleNamespace(returncode=0),
        popen_factory=lambda argv, **kwargs: launched.append(list(argv)),
    )

    assert calls == [
        module.devspace_prepare_argv(),
        module.devspace_compat_argv(stop_exact_service=True, local_port=8765),
        module.devspace_compat_argv(confirm_restarted=True, local_port=8765),
    ]
    assert launched[0] == module.devspace_serve_argv()
    assert result["restart_performed"] is True


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
        "exec npx --yes @waishnav/devspace@1.0.7 init",
    ]


def test_managed_devspace_serve_advertises_offline_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))

    assert module.devspace_prepare_argv()[1:3] == [
        "-lc",
        "exec npx --yes @waishnav/devspace@1.0.7 --help",
    ]
    assert module.devspace_serve_argv()[1:3] == [
        "-lc",
        (
            "exec env DEVSPACE_OAUTH_SCOPES=devspace,offline_access "
            "npx --yes @waishnav/devspace@1.0.7 serve"
        ),
    ]


def test_doctor_orders_local_funnel_public_and_manual_failure_branch(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    seen: list[str] = []
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"allowedRoots": [str(tmp_path)]}), encoding="utf-8")

    class Response:
        def __init__(self, status: int = 200):
            self.status = status
        def read(self, limit):
            return json.dumps({"ok": True, "name": "devspace"}).encode("utf-8")
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

    report = module.doctor(
        current,
        opener=opener,
        runner=runner,
        chatgpt_call_failed=True,
        config_path=config_path,
    )
    assert seen == [current.local_health_url, current.public_health_url]
    assert report["next_action"] == "MANUAL_CHATGPT_REGISTRATION_CHECK"
    assert report["registration_url"] == current.registration_url
    assert report["config"] == {
        "ok": True,
        "path": str(config_path.resolve()),
        "configured_roots": [str(tmp_path.resolve())],
        "missing_roots": [],
    }


def test_doctor_returns_local_failure_before_funnel_or_public(tmp_path: Path) -> None:
    module, current = config(tmp_path)

    def opener(request, timeout):
        raise OSError("unavailable")

    def runner(argv, **kwargs):
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    report = module.doctor(current, opener=opener, runner=runner)
    assert report["next_action"] == "CHECK_DEVSPACE_LOCAL_SERVICE"


def test_doctor_reports_invalid_persisted_config(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text("not-json", encoding="utf-8")

    class Response:
        status = 200
        def read(self, limit):
            return json.dumps({"ok": True, "name": "devspace"}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    report = module.doctor(current, opener=lambda *args, **kwargs: Response(), config_path=config_path)

    assert report["next_action"] == "CHECK_DEVSPACE_CONFIG"
    assert report["config"] == {
        "ok": False,
        "error": "DEVSPACE_CONFIG_INVALID",
        "path": str(config_path),
    }


def test_doctor_reports_missing_requested_root(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"allowedRoots": [str(other)]}), encoding="utf-8")

    class Response:
        status = 200
        def read(self, limit):
            return json.dumps({"ok": True, "name": "devspace"}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    report = module.doctor(current, opener=lambda *args, **kwargs: Response(), config_path=config_path)

    assert report["next_action"] == "CHECK_DEVSPACE_ALLOWED_ROOTS"
    assert report["config"] == {
        "ok": False,
        "path": str(config_path.resolve()),
        "configured_roots": [str(other.resolve())],
        "missing_roots": [str(current.roots[0])],
    }


def test_module_has_no_chatgpt_ui_or_browser_automation() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8").lower()
    for forbidden in (
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


def test_funnel_status_decodes_status_json_as_strict_utf8_and_matches_unicode_hostname(tmp_path: Path) -> None:
    module = load_module()
    unicode_config = module.SetupConfig((tmp_path / "project",), "오사카-pc.tailnet.ts.net")
    seen: dict[str, object] = {}
    payload = json.dumps(
        {"Web": {f"{unicode_config.hostname}:443": {"Proxy": f"http://127.0.0.1:{unicode_config.local_port}"}}},
        ensure_ascii=False,
    )

    def runner(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    report = module.funnel_status(unicode_config, runner=runner)
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "strict"
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["check"] is False
    assert report == {
        "ok": True,
        "mapping": "match",
        "status": {"Proxy": f"http://127.0.0.1:{unicode_config.local_port}"},
    }


def test_funnel_status_never_silently_replaces_non_utf8_tailscale_output(tmp_path: Path) -> None:
    module = load_module()
    current = module.SetupConfig((tmp_path / "project",), "device.tailnet.ts.net")
    seen: dict[str, object] = {}

    def runner(argv, **kwargs):
        # Mimic subprocess.run text mode: decode the captured bytes with exactly
        # the encoding/errors the module requested.
        seen.update(kwargs)
        malformed = b'{"Web": {"device.tailnet.ts.net:443": {"Proxy": "http://127.0.0.1:7676"}}\x80}'
        return SimpleNamespace(
            returncode=0,
            stdout=malformed.decode(kwargs["encoding"], kwargs["errors"]),
            stderr="",
        )

    with pytest.raises(UnicodeDecodeError):
        module.funnel_status(current, runner=runner)
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "strict"


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


def test_doctor_requires_exact_devspace_health_identity(tmp_path: Path) -> None:
    module, current = config(tmp_path)

    class Response:
        status = 200
        def read(self, limit):
            return json.dumps({"ok": True, "name": "another-service"}).encode("utf-8")
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    report = module.doctor(
        current,
        opener=lambda *args, **kwargs: Response(),
        runner=lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )

    assert report["next_action"] == "CHECK_DEVSPACE_LOCAL_SERVICE"
    assert report["local"]["error"] == "DEVSPACE_HEALTH_IDENTITY_INVALID"


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


def test_ensure_waits_for_exact_local_health_then_creates_and_reads_back_funnel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, current = config(tmp_path)
    calls: list[list[str]] = []
    seen: list[str] = []
    timeouts: list[float] = []
    sleeps: list[float] = []
    local_reads = 0
    status_reads = 0

    class Response:
        status = 200

        def __init__(self, name: str):
            self.name = name

        def read(self, limit):
            return json.dumps({"ok": True, "name": self.name}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(request, timeout):
        nonlocal local_reads
        seen.append(request.full_url)
        timeouts.append(timeout)
        if request.full_url == current.local_health_url:
            local_reads += 1
            return Response("other-service" if local_reads == 1 else "devspace")
        return Response("devspace")

    def runner(argv, **kwargs):
        nonlocal status_reads
        calls.append(list(argv))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            status_reads += 1
            web = {} if status_reads == 1 else {
                current.hostname + ":443": {
                    "Proxy": f"http://127.0.0.1:{current.local_port}"
                }
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(time, "sleep", sleeps.append)
    report = module.ensure_public_route(
        current,
        opener=opener,
        runner=runner,
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is True
    assert report["created"] is True
    assert report["next_action"] == "READY"
    assert sleeps == [1]
    assert seen == [
        current.local_health_url,
        current.local_health_url,
        current.public_health_url,
    ]
    assert timeouts == [1, 1, 5]
    assert calls == [
        ["tailscale", "funnel", "status", "--json"],
        [
            "tailscale",
            "funnel",
            "--bg",
            "--https=443",
            f"http://127.0.0.1:{current.local_port}",
        ],
        ["tailscale", "funnel", "status", "--json"],
    ]


def test_ensure_reuses_matching_funnel_without_mutation(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    calls: list[list[str]] = []

    class Response:
        status = 200

        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "Web": {
                    current.hostname + ":443": {
                        "Handlers": {
                            "/": {"Proxy": f"http://127.0.0.1:{current.local_port}"}
                        }
                    }
                }
            }),
            stderr="",
        )

    report = module.ensure_public_route(
        current,
        opener=lambda *args, **kwargs: Response(),
        runner=runner,
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is True
    assert report["created"] is False
    assert calls == [["tailscale", "funnel", "status", "--json"]]


def test_ensure_refuses_conflicting_funnel_without_mutation(tmp_path: Path) -> None:
    module, current = config(tmp_path)
    calls: list[list[str]] = []

    class Response:
        status = 200

        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "Web": {
                    current.hostname + ":443": {
                        "Proxy": "http://127.0.0.1:9999",
                        "Note": f"unrelated port {current.local_port}",
                    }
                }
            }),
            stderr="",
        )

    report = module.ensure_public_route(
        current,
        opener=lambda *args, **kwargs: Response(),
        runner=runner,
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is False
    assert report["created"] is False
    assert report["funnel"]["mapping"] == "conflict"
    assert report["next_action"] == "CHECK_TAILSCALE_FUNNEL"
    assert calls == [["tailscale", "funnel", "status", "--json"]]


def test_windows_watchdog_launch_is_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    module = load_module()
    kwargs = module.windows_subprocess_kwargs(platform_name="nt")
    assert kwargs["creationflags"] & module.subprocess.CREATE_NO_WINDOW
    launched: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(module, "windows_subprocess_kwargs", lambda: {"creationflags": 123})
    module.launch_hidden(
        module.watchdog_launch_argv(),
        popen_factory=lambda argv, **options: launched.append((list(argv), dict(options))),
    )
    assert launched[0][0] == module.watchdog_launch_argv()
    assert launched[0][1]["creationflags"] == 123
    assert launched[0][1]["stdin"] is module.subprocess.DEVNULL
    assert launched[0][1]["stdout"] is module.subprocess.DEVNULL
    assert launched[0][1]["stderr"] is module.subprocess.DEVNULL
    assert launched[0][1]["shell"] is False


def test_setup_applies_hash_validated_devspace_compat_before_service_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, current = config(tmp_path)
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))
    calls: list[list[str]] = []
    call_kwargs: list[dict] = []
    launched: list[list[str]] = []
    status_reads = 0

    monkeypatch.setattr(module, "windows_subprocess_kwargs", lambda: {"creationflags": 123})

    def runner(argv, **kwargs):
        nonlocal status_reads
        calls.append(list(argv))
        call_kwargs.append(dict(kwargs))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            status_reads += 1
            web = {} if status_reads == 1 else {
                current.hostname + ":443": {
                    "Proxy": f"http://127.0.0.1:{current.local_port}",
                }
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.apply_setup(
        current,
        runner=runner,
        popen_factory=lambda argv, **kwargs: launched.append(list(argv)),
        service_inspector=verified_service_inspector,
    )

    assert calls[1][1:3] == [
        "-lc",
        "exec npx --yes @waishnav/devspace@1.0.7 init",
    ]
    assert "creationflags" not in call_kwargs[1]
    assert call_kwargs[2]["creationflags"] == 123
    assert calls[2] == module.devspace_compat_argv()
    assert calls[3] == module.devspace_compat_argv(stop_exact_service=True)
    assert calls[4] == module.devspace_compat_argv(confirm_restarted=True)
    assert launched == [module.devspace_serve_argv(), module.watchdog_launch_argv()]


def test_setup_registers_one_persistent_login_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, current = config(tmp_path)
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))
    calls: list[list[str]] = []
    status_reads = 0

    def runner(argv, **kwargs):
        nonlocal status_reads
        calls.append(list(argv))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            status_reads += 1
            web = {} if status_reads == 1 else {
                current.hostname + ":443": {
                    "Proxy": f"http://127.0.0.1:{current.local_port}",
                }
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    module.apply_setup(
        current,
        runner=runner,
        popen_factory=lambda *args, **kwargs: None,
        service_inspector=verified_service_inspector,
    )
    registry_calls = [call for call in calls if call[:2] == ["reg.exe", "add"]]
    assert len(registry_calls) == 1

    task = calls[-1]
    assert task[:3] == [
        "reg.exe",
        "add",
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
    ]
    assert task[3:5] == ["/v", "DevSpace MCP Server"]
    assert task[5:8] == ["/t", "REG_SZ", "/d"]
    assert "pythonw.exe" in task[8]
    assert "devspace_tailscale_setup.py" in task[8]
    assert task[8].endswith(" watchdog")
    assert str(current.roots[0]) not in task[8]
    assert current.hostname not in task[8]
    assert task[9] == "/f"
    assert module.watchdog_launch_argv()[-1] == "watchdog"
    assert "serve" not in module.parser()._subparsers._group_actions[0].choices


def test_watchdog_reloads_live_roots_and_endpoints_each_cycle_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    config_dir = tmp_path / ".devspace"
    config_dir.mkdir()
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    config_path = config_dir / "config.json"
    auth_path = config_dir / "auth.json"
    first = {
        "host": "127.0.0.1",
        "port": 7676,
        "allowedRoots": [str(first_root)],
        "publicBaseUrl": "https://device-one.tailnet.ts.net",
        "ownerMetadata": {"secret": "preserve-me"},
    }
    second = {
        **first,
        "port": 8765,
        "allowedRoots": [str(second_root)],
        "publicBaseUrl": "https://device-two.tailnet.ts.net:8443",
    }
    config_path.write_text(json.dumps(first), encoding="utf-8")
    auth_path.write_bytes(b'{"ownerToken":"do-not-touch"}')
    before_auth = auth_path.read_bytes()
    loaded: list[tuple[tuple[Path, ...], int, str, int]] = []
    original_loader = module.load_watchdog_config

    def recording_loader(path=None):
        target, current = original_loader(path)
        loaded.append((current.roots, current.local_port, current.hostname, current.public_port))
        return target, current

    monkeypatch.setattr(module, "load_watchdog_config", recording_loader)

    class Response:
        status = 200

        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    health_urls: list[str] = []
    status_calls: list[list[str]] = []

    def opener(request, timeout):
        health_urls.append(request.full_url)
        return Response()

    def runner(argv, **kwargs):
        status_calls.append(list(argv))
        if len(status_calls) == 1:
            web = {
                "device-one.tailnet.ts.net:443": {
                    "Proxy": "http://127.0.0.1:7676",
                }
            }
        else:
            web = {
                "device-two.tailnet.ts.net:8443": {
                    "Proxy": "http://127.0.0.1:8765",
                }
            }
        return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")

    released: list[bool] = []

    def between_cycles(seconds):
        assert seconds == 0
        config_path.write_text(json.dumps(second), encoding="utf-8")

    result = module.run_watchdog(
        interval_seconds=0,
        max_cycles=2,
        config_path=config_path,
        opener=opener,
        runner=runner,
        sleeper=between_cycles,
        mutex_factory=lambda: lambda: released.append(True),
        service_inspector=verified_service_inspector,
        status_path=tmp_path / "watchdog-status.json",
    )

    assert result["ok"] is True
    assert result["cycles"] == 2
    assert loaded == [
        ((first_root.resolve(),), 7676, "device-one.tailnet.ts.net", 443),
        ((second_root.resolve(),), 8765, "device-two.tailnet.ts.net", 8443),
    ]
    assert health_urls == [
        "http://127.0.0.1:7676/healthz",
        "http://127.0.0.1:8765/healthz",
    ]
    assert status_calls == [
        ["tailscale", "funnel", "status", "--json"],
        ["tailscale", "funnel", "status", "--json"],
    ]
    assert json.loads(config_path.read_text(encoding="utf-8")) == second
    assert auth_path.read_bytes() == before_auth
    assert released == [True]


def test_watchdog_repairs_dead_service_and_absent_funnel_with_exact_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    config_dir = tmp_path / ".devspace"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    auth_path = config_dir / "auth.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": 7676,
        "allowedRoots": [str(root)],
        "publicBaseUrl": "https://device.tailnet.ts.net",
        "unrelated": {"secret": "preserve-me"},
    }), encoding="utf-8")
    auth_path.write_bytes(b'{"ownerToken":"do-not-touch"}')
    before_config = config_path.read_bytes()
    before_auth = auth_path.read_bytes()
    bash = tmp_path / "bash.exe"
    bash.write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVSPACE_GIT_BASH", str(bash))

    class Response:
        status = 200

        def __init__(self, name: str):
            self.name = name

        def read(self, limit):
            return json.dumps({"ok": True, "name": self.name}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    health_names = iter(["other-service", "devspace"])
    calls: list[list[str]] = []
    launched: list[list[str]] = []
    status_reads = 0

    def opener(request, timeout):
        return Response(next(health_names))

    def runner(argv, **kwargs):
        nonlocal status_reads
        calls.append(list(argv))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            status_reads += 1
            web = {} if status_reads == 1 else {
                "device.tailnet.ts.net:443": {
                    "Proxy": "http://127.0.0.1:7676",
                }
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    report = module.watchdog_cycle(
        config_path=config_path,
        opener=opener,
        runner=runner,
        popen_factory=lambda argv, **kwargs: launched.append(list(argv)),
        sleeper=lambda seconds: pytest.fail(f"unexpected sleep: {seconds}"),
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is True
    assert report["service"]["healthy"] is True
    assert report["service"]["repaired"] is True
    assert report["service"]["identity_status"] == "verified"
    assert report["funnel"]["healthy"] is True
    assert report["funnel"]["repaired"] is True
    assert report["funnel"]["status"] == "verified"
    assert calls == [
        module.devspace_prepare_argv(),
        module.devspace_compat_argv(),
        module.devspace_compat_argv(stop_exact_service=True),
        module.devspace_compat_argv(confirm_restarted=True),
        ["tailscale", "funnel", "status", "--json"],
        module.tailscale_funnel_argv(module.validate_config([str(root)], "device.tailnet.ts.net")),
        ["tailscale", "funnel", "status", "--json"],
    ]
    assert launched == [module.devspace_serve_argv()]
    assert "@waishnav/devspace@1.0.7" in " ".join(module.devspace_prepare_argv())
    assert config_path.read_bytes() == before_config
    assert auth_path.read_bytes() == before_auth


def test_watchdog_healthy_cycle_is_a_read_only_noop(tmp_path: Path) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    config_dir = tmp_path / ".devspace"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    auth_path = config_dir / "auth.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": 7676,
        "allowedRoots": [str(root)],
        "publicBaseUrl": "https://device.tailnet.ts.net",
        "unrelated": {"secret": "preserve-me"},
    }), encoding="utf-8")
    auth_path.write_bytes(b'{"ownerToken":"do-not-touch"}')
    before_config = config_path.read_bytes()
    before_auth = auth_path.read_bytes()

    class Response:
        status = 200

        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "Web": {
                    "device.tailnet.ts.net:443": {
                        "Handlers": {
                            "/": {"Proxy": "http://127.0.0.1:7676"},
                        }
                    }
                }
            }),
            stderr="",
        )

    report = module.watchdog_cycle(
        config_path=config_path,
        opener=lambda *args, **kwargs: Response(),
        runner=runner,
        popen_factory=lambda *args, **kwargs: pytest.fail("healthy service was restarted"),
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is True
    assert report["service"]["healthy"] is True
    assert report["service"]["repaired"] is False
    assert report["service"]["identity_status"] == "verified"
    assert report["funnel"]["healthy"] is True
    assert report["funnel"]["repaired"] is False
    assert report["funnel"]["status"] == "verified"
    assert calls == [["tailscale", "funnel", "status", "--json"]]
    assert config_path.read_bytes() == before_config
    assert auth_path.read_bytes() == before_auth


def test_watchdog_refuses_conflicting_funnel_without_mutation(tmp_path: Path) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": 7676,
        "allowedRoots": [str(root)],
        "publicBaseUrl": "https://device.tailnet.ts.net",
    }), encoding="utf-8")
    before_config = config_path.read_bytes()

    class Response:
        status = 200

        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "Web": {
                    "device.tailnet.ts.net:443": {
                        "Proxy": "http://127.0.0.1:9999",
                    }
                }
            }),
            stderr="",
        )

    report = module.watchdog_cycle(
        config_path=config_path,
        opener=lambda *args, **kwargs: Response(),
        runner=runner,
        popen_factory=lambda *args, **kwargs: pytest.fail("healthy service was restarted"),
        service_inspector=verified_service_inspector,
    )

    assert report["ok"] is False
    assert report["funnel"]["healthy"] is False
    assert report["funnel"]["repaired"] is False
    assert report["funnel"]["status"] == "conflict"
    assert report["funnel"]["error"] == "TAILSCALE_FUNNEL_MAPPING_MISMATCH"
    assert calls == [["tailscale", "funnel", "status", "--json"]]
    assert config_path.read_bytes() == before_config


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named mutex contract")
def test_watchdog_windows_mutex_allows_only_one_owner() -> None:
    module = load_module()
    module.WATCHDOG_MUTEX_NAME = rf"Local\DevSpaceOracle.WatchdogTest.{time.time_ns()}"
    first_release = module.acquire_watchdog_mutex()
    assert callable(first_release)
    assert module.acquire_watchdog_mutex() is None
    first_release()
    next_release = module.acquire_watchdog_mutex()
    assert callable(next_release)
    next_release()


def test_watchdog_single_instance_skips_duplicate_and_releases_owner(tmp_path: Path) -> None:
    module = load_module()
    cycles: list[bool] = []
    duplicate = module.run_watchdog(
        interval_seconds=0,
        max_cycles=1,
        mutex_factory=lambda: None,
        cycle=lambda **kwargs: cycles.append(True) or {"ok": True},
        status_path=tmp_path / "duplicate-status.json",
    )
    assert duplicate == {"ok": True, "already_running": True, "cycles": 0}
    assert cycles == []

    released: list[bool] = []
    owner = module.run_watchdog(
        interval_seconds=0,
        max_cycles=1,
        mutex_factory=lambda: lambda: released.append(True),
        cycle=lambda **kwargs: cycles.append(True) or {"ok": True},
        status_path=tmp_path / "owner-status.json",
    )
    assert owner["ok"] is True
    assert owner["cycles"] == 1
    assert cycles == [True]
    assert released == [True]
    defaults = module.parser().parse_args(["watchdog"])
    assert defaults.interval_seconds == 300
    assert defaults.max_cycles is None


def test_watchdog_health_impostor_disables_only_exact_funnel_without_kill_or_exposure(
    tmp_path: Path,
) -> None:
    module = load_module()
    root = tmp_path / "project"
    root.mkdir()
    config_path = tmp_path / "config.json"
    auth_path = tmp_path / "auth.json"
    config_path.write_text(json.dumps({
        "host": "127.0.0.1",
        "port": 7676,
        "allowedRoots": [str(root)],
        "publicBaseUrl": "https://device.tailnet.ts.net",
        "ownerMetadata": {"secret": "preserve-me"},
    }), encoding="utf-8")
    auth_path.write_bytes(b'{"ownerToken":"do-not-touch"}')
    before_config = config_path.read_bytes()
    before_auth = auth_path.read_bytes()

    class Response:
        status = 200
        def read(self, limit):
            return b'{"ok":true,"name":"devspace"}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    calls: list[list[str]] = []
    status_reads = 0

    def runner(argv, **kwargs):
        nonlocal status_reads
        calls.append(list(argv))
        if argv == ["tailscale", "funnel", "status", "--json"]:
            status_reads += 1
            web = {
                "device.tailnet.ts.net:443": {
                    "Proxy": "http://127.0.0.1:7676",
                }
            } if status_reads == 1 else {}
            return SimpleNamespace(returncode=0, stdout=json.dumps({"Web": web}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    report = module.watchdog_cycle(
        config_path=config_path,
        opener=lambda *args, **kwargs: Response(),
        runner=runner,
        popen_factory=lambda *args, **kwargs: pytest.fail("impostor was killed or replaced"),
        service_inspector=mismatched_service_inspector,
    )

    current = module.validate_config([str(root)], "device.tailnet.ts.net")
    assert report["ok"] is False
    assert report["result_code"] == "DEVSPACE_SERVICE_IDENTITY_MISMATCH"
    assert report["funnel"]["status"] == "disabled"
    assert calls == [
        ["tailscale", "funnel", "status", "--json"],
        module.tailscale_funnel_off_argv(current),
        ["tailscale", "funnel", "status", "--json"],
    ]
    assert config_path.read_bytes() == before_config
    assert auth_path.read_bytes() == before_auth


def test_watchdog_status_is_atomic_restrictive_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_module()
    target = tmp_path / "state" / "devspace-watchdog" / "status.json"
    replacements: list[tuple[Path, Path]] = []
    permissions: list[tuple[Path, bool]] = []
    original_replace = module.os.replace

    def replace(source, destination):
        replacements.append((Path(source), Path(destination)))
        original_replace(source, destination)

    def restrict(path, *, directory):
        permissions.append((Path(path), directory))
        Path(path).chmod(0o700 if directory else 0o600)

    monkeypatch.setattr(module.os, "replace", replace)
    module.write_watchdog_status(
        ok=False,
        result_code="DEVSPACE_SERVICE_IDENTITY_MISMATCH",
        service_identity_status="mismatch",
        funnel_status="disabled",
        last_verified_service={
            "pid": 42,
            "started_at_unix_ns": 123,
            "local_port": 7676,
            "command_line": "serve --token=never-persist",
            "package_root": r"C:\secret\root",
        },
        last_verified_funnel={
            "hostname": "device.tailnet.ts.net",
            "public_port": 443,
            "local_port": 7676,
            "url": "https://user:password@device.tailnet.ts.net",
        },
        path=target,
        timestamp_ns=456,
        permission_setter=restrict,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert payload["schema"] == "codex.devspace-watchdog-status/v1"
    assert payload["timestamp_unix_ns"] == 456
    assert payload["result_code"] == "DEVSPACE_SERVICE_IDENTITY_MISMATCH"
    assert "command_line" not in serialized
    assert "package_root" not in serialized
    assert "never-persist" not in serialized
    assert "password" not in serialized
    assert len(replacements) == 1
    assert replacements[0][1] == target
    assert not replacements[0][0].exists()
    assert permissions[0] == (target.parent, True)
    assert permissions[-1] == (target, False)


def test_watchdog_permanent_failure_is_observable_in_status(tmp_path: Path) -> None:
    module = load_module()
    target = tmp_path / "status.json"
    sleeps: list[float] = []

    def persist(**kwargs):
        return module.write_watchdog_status(
            **kwargs,
            permission_setter=lambda path, *, directory: Path(path).chmod(
                0o700 if directory else 0o600
            ),
        )

    result = module.run_watchdog(
        interval_seconds=0,
        max_cycles=2,
        mutex_factory=lambda: lambda: None,
        sleeper=sleeps.append,
        cycle=lambda **kwargs: {
            "ok": False,
            "result_code": "DEVSPACE_SERVICE_IDENTITY_MISMATCH",
            "service": {
                "identity_status": "mismatch",
                "error": "DEVSPACE_SERVICE_IDENTITY_MISMATCH",
                "command_line": "never-persist",
            },
            "funnel": {"status": "disabled"},
        },
        status_path=target,
        status_writer=persist,
    )

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert result["ok"] is False
    assert result["cycles"] == 2
    assert payload["ok"] is False
    assert payload["result_code"] == "DEVSPACE_SERVICE_IDENTITY_MISMATCH"
    assert payload["service_identity_status"] == "mismatch"
    assert payload["funnel_status"] == "disabled"
    assert "never-persist" not in target.read_text(encoding="utf-8")
    assert sleeps == [0]
