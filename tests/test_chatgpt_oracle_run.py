from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_run.py"
PRO_CONTEXT_BUILDER_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "chatgpt-pro-browser"
    / "scripts"
    / "build_project_context_packet.py"
)


def load_runner():
    name = "chatgpt_oracle_run_test"
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_pro_context_builder():
    name = "chatgpt_pro_context_builder_test"
    spec = importlib.util.spec_from_file_location(name, PRO_CONTEXT_BUILDER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def manifest(tmp_path: Path, *, test_profile: bool = True, **extra) -> Path:
    mission = tmp_path / "mission.md"
    mission.write_text("finish", encoding="utf-8")
    path = tmp_path / "job.json"
    payload = {
        "schema": "codex.chatgpt.oracle-run/v1",
        "project_root": str(tmp_path.resolve()),
        "mission_path": str(mission.resolve()),
        "app_name": "DevSpace",
        "mode": "browser",
        "run_root": str((tmp_path.parent / f"{tmp_path.name}-host-state" / "runs").resolve()),
        "oracle_command": [
            "npx.cmd" if os.name == "nt" else "npx",
            "-y",
            "@steipete/oracle@0.17.1",
        ],
    }
    if test_profile and "copy_profile" not in extra:
        profile = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
        profile.mkdir(exist_ok=True)
        payload["copy_profile"] = str(profile.resolve())
    payload.update(extra)
    payload["mission_sha256"] = hashlib.sha256(
        Path(payload["mission_path"]).read_bytes()
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    return path.resolve()


def rebind_manifest_mission(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["mission_sha256"] = hashlib.sha256(
        Path(payload["mission_path"]).read_bytes()
    ).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")


def pro_manifest(tmp_path: Path, prompt_text: str = "pro instructions", **extra) -> Path:
    builder = load_pro_context_builder()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    evidence = tmp_path / "evidence.txt"
    context_manifest = tmp_path / "project-context-manifest.json"
    prompt.write_text(prompt_text, encoding="utf-8")
    evidence.write_text("implementation evidence", encoding="utf-8")
    context_manifest.write_text(json.dumps({
        "schema": builder.SCHEMA,
        "project_root": str(tmp_path.resolve()),
        "question": "Review the attached project evidence.",
        "mission_path": str(prompt.resolve()),
        "mission_sha256": builder.sha256_file(prompt),
        "required_categories": ["implementation"],
        "category_omissions": [],
        "local_transport_envelope_bytes": builder.TOTAL_ENVELOPE_BYTES,
        "answer_headroom_bytes": builder.TRANSPORT_ANSWER_HEADROOM_BYTES,
        "metadata_reserve_bytes": builder.METADATA_RESERVE_BYTES,
        "packet_path": str(packet.resolve()),
        "evidence": [{
            "path": str(evidence.resolve()),
            "category": "implementation",
            "priority": 0,
            "sha256": builder.sha256_file(evidence),
        }],
    }), encoding="utf-8")
    receipt = builder.build(context_manifest)
    return manifest(
        tmp_path,
        transport="pro-attachment-only",
        app_name=None,
        model="gpt-5.5-pro",
        model_strategy="select",
        thinking_time="heavy",
        attachments=[str(prompt.resolve()), str(packet.resolve())],
        attachment_sha256s=[builder.sha256_file(prompt), str(receipt["packet_sha256"])],
        project_context_manifest_path=str(context_manifest.resolve()),
        project_context_manifest_sha256=builder.sha256_file(context_manifest),
        mission_path=str(prompt.resolve()),
        **extra,
    )


def version_runner(command, **kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="oracle 0.17.1\n", stderr="")


def recovery_version_runner(command, **kwargs):
    package = next(item for item in command if item.startswith("@steipete/oracle@"))
    return subprocess.CompletedProcess(
        command,
        0,
        stdout=f"oracle {package.rsplit('@', 1)[1]}\n",
        stderr="",
    )


def version_timeout_runner(command, **kwargs):
    raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 30))


def test_no_submission_preflight_checks_runtime_without_creating_run_state(tmp_path: Path) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-profile"
    profile.mkdir()
    job = manifest(tmp_path, copy_profile=str(profile.resolve()))
    host_state = Path(os.environ["CODEX_ORACLE_STATE_ROOT"])
    doctor_calls = []

    result = runner.preflight_run(
        job,
        expected_manifest_sha256=hashlib.sha256(job.read_bytes()).hexdigest(),
        devspace_hostname="device.tailnet.ts.net",
        run_factory=version_runner,
        oracle_inspector=lambda version: {"ok": True, "ready": True, "version": version},
        devspace_inspector=lambda **kwargs: {"ok": True, "ready": True},
        devspace_doctor=lambda config: doctor_calls.append(config) or {"next_action": "READY"},
    )

    assert result["schema"] == "codex.chatgpt.oracle-preflight/v1"
    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["failed_checks"] == []
    assert result["chatgpt_ui"]["checked"] is False
    assert len(doctor_calls) == 1
    assert doctor_calls[0].registration_url == "https://device.tailnet.ts.net/mcp"
    assert not host_state.exists()


def test_preflight_reports_missing_profile_and_devspace_without_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_runner()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "missing-profile"))
    job = manifest(tmp_path, test_profile=False)

    result = runner.preflight_run(
        job,
        expected_manifest_sha256=hashlib.sha256(job.read_bytes()).hexdigest(),
        devspace_hostname="device.tailnet.ts.net",
        run_factory=version_runner,
        oracle_inspector=lambda version: {"ok": True, "ready": True},
        devspace_inspector=lambda **kwargs: {"ok": True, "ready": False},
        devspace_doctor=lambda config: {"next_action": "CHECK_DEVSPACE_LOCAL_SERVICE"},
    )

    assert result["ok"] is False
    assert result["status"] == "not_ready"
    assert {"profile_seed", "devspace_compatibility", "devspace_endpoint"}.issubset(
        result["failed_checks"]
    )


def test_windows_live_submission_requires_profile_seed_before_run_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = load_runner()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path / "missing-profile"))
    job = manifest(tmp_path, test_profile=False)
    run_root = runner.STATE.load_manifest(job, platform_name="nt").run_root

    with pytest.raises(runner.OracleRunError) as error:
        execute_run(runner, job, platform_name="nt")

    assert error.value.code == "COPY_PROFILE_REQUIRED"
    assert not run_root.exists()


def test_pro_preflight_skips_devspace_and_rejects_endpoint_options(tmp_path: Path) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-profile"
    profile.mkdir()
    job = pro_manifest(tmp_path, copy_profile=str(profile.resolve()))
    expected = hashlib.sha256(job.read_bytes()).hexdigest()

    result = runner.preflight_run(
        job,
        expected_manifest_sha256=expected,
        run_factory=version_runner,
        oracle_inspector=lambda version: {"ok": True, "ready": True},
    )

    assert result["ok"] is True
    assert next(check for check in result["checks"] if check["name"] == "devspace")["not_applicable"] is True
    with pytest.raises(runner.OracleRunError) as error:
        runner.preflight_run(
            job,
            expected_manifest_sha256=expected,
            devspace_hostname="device.tailnet.ts.net",
            run_factory=version_runner,
            oracle_inspector=lambda version: {"ok": True, "ready": True},
        )
    assert error.value.code == "PRO_DEVSPACE_PREFLIGHT_FORBIDDEN"


def test_pro_live_submission_never_calls_devspace_readiness_adapters(tmp_path: Path) -> None:
    runner = load_runner()

    def forbidden(*args, **kwargs):
        raise AssertionError("Pro attachment-only must not call DevSpace readiness")

    result = runner.execute_run(
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"pro answer", {}, []),
        compat_factory=lambda version: {"ok": True, "version": version},
        runtime_command_factory=lambda compatibility, version: ("node", "validated-oracle-cli.js"),
        devspace_compat_factory=forbidden,
        devspace_doctor=forbidden,
    )

    assert result["ok"] is True


def test_version_resolution_allows_a_bounded_slow_valid_oracle_0171() -> None:
    runner = load_runner()
    captured = {}

    def slow_valid(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(command, 0, stdout="oracle 0.17.1\n", stderr="")

    assert runner.resolve_oracle_version(
        ["npx.cmd", "-y", "@steipete/oracle@0.17.1"], run_factory=slow_valid
    ) == "oracle 0.17.1"
    assert captured == {
        "command": ["npx.cmd", "-y", "@steipete/oracle@0.17.1", "--version"],
        "timeout": runner.ORACLE_VERSION_RESOLUTION_TIMEOUT_SECONDS,
    }
    assert runner.ORACLE_VERSION_RESOLUTION_TIMEOUT_SECONDS == 90


def test_validated_package_root_is_the_exact_runtime_popen_target(tmp_path: Path) -> None:
    runner = load_runner()
    root = tmp_path / "npm-cache" / "_npx" / "exact" / "node_modules" / "@steipete" / "oracle"
    cli = root / "dist" / "bin" / "oracle-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// exact validated cli", encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"version": "0.17.1"}), encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_bytes(b"node")
    compatibility = {
        "ok": True,
        "version": "0.17.1",
        "package_root": str(root),
        "package_roots": [str(root)],
    }

    runtime_command = runner.validated_oracle_runtime_command(
        compatibility,
        "oracle 0.17.1",
        which_runner=lambda name: str(node),
    )
    assert runtime_command == (str(node.resolve()), str(cli.resolve()))

    captured: dict = {}
    events: list[str] = []
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        compat_factory=lambda version: compatibility,
        runtime_command_factory=lambda report, version: runner.validated_oracle_runtime_command(
            report,
            version,
            which_runner=lambda name: str(node),
        ),
        popen_factory=popen_for(0, b"answer\nTASK_OUTCOME: EXECUTED\n", captured, events),
    )

    assert result["ok"] is True
    assert captured["command"][:2] == [str(node.resolve()), str(cli.resolve())]


def test_execute_run_persists_out_of_band_manifest_identity(tmp_path: Path) -> None:
    runner = load_runner()
    job = manifest(tmp_path)
    expected = hashlib.sha256(job.read_bytes()).hexdigest()

    result = execute_run(
        runner,
        job,
        expected_manifest_sha256=expected,
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer\nTASK_OUTCOME: EXECUTED\n", {}, []),
    )
    state = runner.STATE.load_state(Path(result["run_dir"]) / "state.json")

    assert state["manifest"] == {
        "path": str(job),
        "actual_sha256": expected,
        "expected_sha256": expected,
    }


def test_validated_runtime_rejects_an_unlisted_compatibility_root(tmp_path: Path) -> None:
    runner = load_runner()
    root = tmp_path / "oracle"
    root.mkdir()
    (root / "package.json").write_text(json.dumps({"version": "0.17.1"}), encoding="utf-8")

    with pytest.raises(runner.OracleRunError) as exc:
        runner.validated_oracle_runtime_command(
            {
                "ok": True,
                "version": "0.17.1",
                "package_root": str(root),
                "package_roots": [str(tmp_path / "different")],
            },
            "0.17.1",
            which_runner=lambda name: str(tmp_path / "node.exe"),
        )

    assert exc.value.code == "ORACLE_COMPATIBILITY_ROOT_UNBOUND"


def test_default_oracle_command_is_pinned_to_the_hash_validated_version() -> None:
    runner = load_runner()

    assert runner.STATE.default_oracle_command(platform_name="nt") == (
        "npx.cmd", "-y", "@steipete/oracle@0.17.1",
    )
    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.STATE.validate_oracle_command(["npx.cmd", "-y", "@steipete/oracle@0.16.1"])
    assert exc.value.code == "ORACLE_COMMAND_FORBIDDEN"


def test_conversation_url_helpers_preserve_exact_binding_and_detect_conflicts(tmp_path: Path) -> None:
    runner = load_runner()
    observer = tmp_path / "recovery-live-stdout.log"
    observer.write_text(
        "URL: https://chatgpt.com/c/oracle-old\nURL: https://chatgpt.com/c/oracle-current\n",
        encoding="utf-8",
    )
    state = {"oracle": {"conversation_url": "https://chatgpt.com/c/oracle-current"}}

    assert runner.exact_session_url(observer) == "https://chatgpt.com/c/oracle-current"
    assert runner.historical_conversation_url(tmp_path, state) == "https://chatgpt.com/c/oracle-current"
    assert runner.conversation_url_conflict(state, "https://chatgpt.com/c/oracle-other") == {
        "persisted": "https://chatgpt.com/c/oracle-current",
        "observed": "https://chatgpt.com/c/oracle-other",
    }


def execute_run(runner, *args, **kwargs):
    kwargs.setdefault("compat_factory", lambda version: {"ok": True, "version": version})
    kwargs.setdefault("runtime_command_factory", lambda compatibility, version: ("node", "validated-oracle-cli.js"))
    kwargs.setdefault(
        "devspace_compat_factory",
        lambda: {"ok": True, "changed": [], "service_restart_required": False},
    )
    kwargs.setdefault("devspace_hostname", "device.tailnet.ts.net")
    kwargs.setdefault("devspace_doctor", lambda config: {"next_action": "READY"})
    return runner.execute_run(*args, **kwargs)


def recover_run(runner, *args, **kwargs):
    kwargs.setdefault("run_factory", recovery_version_runner)
    kwargs.setdefault("compat_factory", lambda version: {"ok": True, "version": version})
    kwargs.setdefault("runtime_command_factory", lambda compatibility, version: ("node", "validated-oracle-cli.js"))
    return runner.recover_run(*args, **kwargs)


def test_new_submission_rejects_recovery_only_oracle_0161_before_launch(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="oracle 0.16.1\n", stderr=""
        ),
        popen_factory=lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert result["result"]["pre_submit_failure"]["failure_reason"] == "compatibility-version-drift"
    assert launched == []


class Process:
    def __init__(self, code: int, events: list[str]):
        self.code = code
        self.events = events
        self.pid = 1234
        self.wait_timeout = None

    def wait(self, timeout=None):
        self.wait_timeout = timeout
        self.events.append("wait")
        return self.code


def popen_for(code: int, output: bytes | None, captured: dict, events: list[str]):
    def popen(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        events.append("popen")
        if output is not None:
            Path(command[command.index("--write-output") + 1]).write_bytes(output)
        kwargs["stdout"].write(b"stdout\n")
        kwargs["stdout"].flush()
        return Process(code, events)
    return popen


def duplicate_prompt_popen(command, **kwargs):
    kwargs["stdout"].write(
        b'oracle 0.16.1\nA session with the same prompt is already running '
        b'(oracle-global-agent-instructio-f39cc47ba5). Reattach with '
        b'"oracle session oracle-global-agent-instructio-f39cc47ba5" or rerun with '
        b'--force to start another run.\n'
    )
    kwargs["stdout"].flush()
    return Process(1, [])


def profile_copy_ebusy_popen(command, **kwargs):
    source = Path(command[command.index("--copy-profile") + 1]) / "Default" / "Network" / "Cookies"
    destination = Path(kwargs["env"]["TEMP"]) / "oracle-browser-test" / "Default" / "Network" / "Cookies"
    kwargs["stdout"].write(
        f"ERROR: EBUSY: resource busy or locked, copyfile '{source}' -> '{destination}'\n".encode("utf-8")
    )
    kwargs["stdout"].flush()
    return Process(1, [])


def test_dry_run_never_executes_and_has_no_file_flag(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError
    job = manifest(tmp_path)
    result = execute_run(runner, job, dry_run=True, run_factory=forbidden, popen_factory=forbidden)
    assert result["ok"] is True
    assert result["prompt_first_line"].startswith("@DevSpace ")
    assert str((tmp_path / "mission.md").resolve()) in result["prompt_first_line"]
    assert result["mission_sha256"]
    assert result["manifest"] == {
        "path": str(job),
        "actual_sha256": hashlib.sha256(job.read_bytes()).hexdigest(),
        "expected_sha256": None,
    }
    assert Path(result["mission_path"]).is_absolute()
    assert str((tmp_path / "mission.md").resolve()) in result["argv"][result["argv"].index("--prompt") + 1]
    assert "--file" not in result["argv"]
    assert result["argv"][result["argv"].index("--browser-model-strategy") + 1] == "select"
    assert result["argv"][result["argv"].index("--browser-thinking-time") + 1] == "extra-high"
    assert result["argv"].count("--wait") == 1
    assert result["argv"].count("--browser-hide-window") == 1
    assert calls == []
    assert not (tmp_path / "runs").exists()


def test_live_cli_requires_and_propagates_manifest_hash(monkeypatch, capsys, tmp_path: Path) -> None:
    runner = load_runner()
    job = manifest(tmp_path)
    expected = hashlib.sha256(job.read_bytes()).hexdigest()
    calls = []

    monkeypatch.setattr(runner, "execute_run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert runner.main(["run", "--manifest", str(job)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "MANIFEST_SHA256_REQUIRED"
    assert calls == []

    monkeypatch.setattr(
        runner,
        "execute_run",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    assert runner.main([
        "run", "--manifest", str(job), "--expected-manifest-sha256", expected,
    ]) == 0
    capsys.readouterr()
    assert calls == [((job,), {"expected_manifest_sha256": expected, "dry_run": False})]


def test_copy_profile_is_first_class_and_outside_project(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
    profile.mkdir()
    # Profile copying depends on rsync, which is absent on many Windows hosts.
    # Pin the dependency so this argv contract stays deterministic.
    monkeypatch.setattr(
        runner.STATE.shutil,
        "which",
        lambda name: "/usr/bin/rsync" if name == runner.STATE.PROFILE_COPY_DEPENDENCY else None,
    )
    result = execute_run(runner, manifest(tmp_path, copy_profile=str(profile.resolve())), dry_run=True)
    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(profile.resolve())


def test_default_signed_in_profile_is_copied_per_run_and_window_is_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(
        runner.STATE.shutil,
        "which",
        lambda name: "/usr/bin/rsync" if name == runner.STATE.PROFILE_COPY_DEPENDENCY else None,
    )

    result = execute_run(runner, manifest(tmp_path, test_profile=False), dry_run=True)

    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(profile.resolve())
    assert result["argv"].count("--browser-hide-window") == 1


def test_missing_posix_copy_dependency_still_launches_without_profile_copy(
    tmp_path: Path, monkeypatch
) -> None:
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(runner.STATE.shutil, "which", lambda name: None)

    result = execute_run(
        runner, manifest(tmp_path, test_profile=False), dry_run=True, platform_name="posix"
    )

    assert "--copy-profile" not in result["argv"]
    assert result["argv"].count("--browser-hide-window") == 1


def test_windows_lanes_keep_profile_isolation_without_rsync(
    tmp_path: Path, monkeypatch
) -> None:
    """Windows uses the pinned native profile copy, so lanes stay isolated.

    Probing PATH for rsync here dropped `--copy-profile` and blocked parallel
    Web Multi lanes before submission.
    """
    runner = load_runner()
    profile = tmp_path.parent / f"{tmp_path.name}-signed-in-oracle-profile"
    profile.mkdir()
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(profile.resolve()))
    monkeypatch.setattr(runner.STATE.shutil, "which", lambda name: None)

    result = execute_run(
        runner, manifest(tmp_path, test_profile=False), dry_run=True, platform_name="nt"
    )

    assert result["argv"][result["argv"].index("--copy-profile") + 1] == str(
        profile.resolve()
    )
    assert result["argv"].count("--browser-hide-window") == 1


def test_explicit_hide_window_arg_is_safe_and_not_duplicated(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, oracle_args=["--browser-hide-window"]),
        dry_run=True,
    )
    assert result["argv"].count("--browser-hide-window") == 1


def test_regular_runs_raise_the_answer_timeout_above_the_upstream_default(
    tmp_path: Path,
) -> None:
    """Heavy Extra High lanes get one explicit overall answer budget."""
    runner = load_runner()

    result = execute_run(runner, manifest(tmp_path), dry_run=True)

    argv = result["argv"]
    assert argv.count("--browser-timeout") == 1
    assert argv[argv.index("--browser-timeout") + 1] == runner.STATE.DEFAULT_BROWSER_ANSWER_TIMEOUT
    assert runner.STATE.DEFAULT_BROWSER_ANSWER_TIMEOUT == "90m"
    assert runner.STATE.DEFAULT_BROWSER_ANSWER_CEILING_MINUTES == 90
    assert result["host_watchdog_timeout_seconds"] == 5430


def test_explicit_answer_timeout_is_honored_without_duplication(tmp_path: Path) -> None:
    runner = load_runner()

    result = execute_run(
        runner,
        manifest(tmp_path, oracle_args=["--browser-timeout", "70m"]),
        dry_run=True,
    )

    argv = result["argv"]
    assert argv.count("--browser-timeout") == 1
    assert argv[argv.index("--browser-timeout") + 1] == "70m"
    assert result["host_watchdog_timeout_seconds"] == 4230


@pytest.mark.parametrize("duration", ["9d", "999999999h", "9" * 400])
def test_answer_timeout_must_produce_a_finite_bounded_host_deadline(
    tmp_path: Path,
    duration: str,
) -> None:
    runner = load_runner()

    with pytest.raises(runner.OracleRunError) as exc:
        execute_run(
            runner,
            manifest(tmp_path, oracle_args=["--browser-timeout", duration]),
            dry_run=True,
        )

    assert exc.value.code in {"BROWSER_TIMEOUT_INVALID", "BROWSER_TIMEOUT_OUT_OF_RANGE"}


def test_pro_uses_the_bounded_original_session_answer_wait(tmp_path: Path) -> None:
    runner = load_runner()

    result = execute_run(runner, pro_manifest(tmp_path), dry_run=True)

    assert result["argv"].count("--browser-timeout") == 1
    assert result["argv"][result["argv"].index("--browser-timeout") + 1] == "90m"
    assert result["host_watchdog_timeout_seconds"] == 5430


def test_pro_dry_run_uses_oracle_attachments_and_no_app_mention(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(runner, pro_manifest(tmp_path), dry_run=True)
    argv = result["argv"]
    prompt = argv[argv.index("--prompt") + 1]
    attachments = [argv[index + 1] for index, value in enumerate(argv) if value == "--file"]
    assert result["transport"] == "pro-attachment-only"
    assert result["contains_file_flag"] is True
    assert argv[argv.index("--model") + 1] == "gpt-5.5-pro"
    assert argv[argv.index("--browser-attachments") + 1] == "always"
    assert argv.count("--wait") == 1
    assert attachments == [
        str((tmp_path / "prompt.txt").resolve()),
        str((tmp_path / "packet.zip").resolve()),
    ]
    assert prompt.startswith(
        "Read the attached prompt/instructions and all attached files, then complete the task. "
        "Task identity: oracle-pro-"
    )
    assert prompt.endswith(".")
    assert "@DevSpace" not in prompt
    assert all(item["sha256"] for item in result["attachments"])


def test_pro_attachment_preflight_accepts_one_mib_and_rejects_one_byte_more(tmp_path: Path) -> None:
    runner = load_runner()
    job = pro_manifest(tmp_path)
    config = runner.STATE.load_manifest(job)
    packet = tmp_path / "packet.zip"
    packet.write_bytes(b"x" * runner.ORACLE_PRO_ATTACHMENT_MAX_BYTES)

    runner.validate_oracle_attachment_sizes(config)

    packet.write_bytes(b"x" * (runner.ORACLE_PRO_ATTACHMENT_MAX_BYTES + 1))
    with pytest.raises(runner.OracleRunError) as exc:
        runner.validate_oracle_attachment_sizes(config)

    assert exc.value.code == "ORACLE_ATTACHMENT_SIZE_PRELAUNCH_FAILED"
    assert exc.value.evidence == {
        "limit_bytes": 1024 * 1024,
        "attachments": [{
            "path": str(packet.resolve()),
            "size_bytes": 1024 * 1024 + 1,
            "limit_bytes": 1024 * 1024,
        }],
    }


def test_pro_runner_rejects_an_unvalidated_packet_before_layout(tmp_path: Path) -> None:
    runner = load_runner()
    job = pro_manifest(tmp_path)
    run_root = runner.STATE.load_manifest(job).run_root
    (tmp_path / "packet.zip").write_bytes(b"not-a-validated-packet")

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        execute_run(runner, job, dry_run=True)

    assert exc.value.code == "PRO_ATTACHMENT_SHA256_MISMATCH"
    assert not run_root.exists()


def test_pro_runner_revalidates_nonattachment_evidence_before_popen(tmp_path: Path) -> None:
    runner = load_runner()
    job = pro_manifest(tmp_path)
    launched: list[bool] = []

    def mutate_after_initial_preflight(version: str) -> dict[str, object]:
        (tmp_path / "evidence.txt").write_text("changed", encoding="utf-8")
        return {"ok": True, "version": version}

    result = execute_run(
        runner,
        job,
        run_factory=version_runner,
        compat_factory=mutate_after_initial_preflight,
        popen_factory=lambda *args, **kwargs: launched.append(True),
    )

    assert result["ok"] is False
    assert launched == []
    assert result["result"]["session_authority"] == "pre_submit"


def test_complete_requires_zero_exit_and_nonempty_output(tmp_path: Path) -> None:
    runner = load_runner()
    cases = [
        (0, b"answer", "complete", True),
        (0, b" \n", "attention_required", False),
        (3, b"answer", "attention_required", False),
    ]
    for index, (code, output, status, ok) in enumerate(cases):
        root = tmp_path / str(index)
        root.mkdir()
        captured, events = {}, []
        result = execute_run(runner, manifest(root), run_factory=version_runner, popen_factory=popen_for(code, output, captured, events))
        assert result["ok"] is ok
        assert result["result"]["status"] == status
        assert result["result"]["oracle"]["resolved_version"] == "oracle 0.17.1"
        assert "--file" not in captured["command"]
        assert events == ["popen", "wait"]
        assert Path(result["result"]["artifacts"]["transcript"]).is_file()


def test_v1_task_outcome_separates_transport_success_from_execution(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    (tmp_path / "executed").mkdir()
    (tmp_path / "not-executed").mkdir()
    executed = execute_run(
        runner,
        manifest(
            tmp_path / "executed",
            task_outcome_contract="v1",
            run_id="e" * 32,
        ),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"done\nTASK_OUTCOME: EXECUTED\n", {}, []),
    )
    not_executed = execute_run(
        runner,
        manifest(
            tmp_path / "not-executed",
            task_outcome_contract="v1",
            run_id="n" * 32,
        ),
        run_factory=version_runner,
        popen_factory=popen_for(
            0,
            b"workspace open timed out\nTASK_OUTCOME: NOT_EXECUTED\n",
            {},
            [],
        ),
    )

    assert executed["ok"] is True
    assert executed["result"]["status"] == "complete"
    assert executed["result"]["transport_status"] == "complete"
    assert executed["result"]["task_outcome"] == "executed"
    assert not_executed["ok"] is False
    assert not_executed["result"]["status"] == "attention_required"
    assert not_executed["result"]["transport_status"] == "complete"
    assert not_executed["result"]["task_outcome"] == "not_executed"
    assert not_executed["result"]["session_authority"] == "terminal"
    assert not_executed["result"]["terminal_harvested"] is True


def test_v1_missing_task_outcome_marker_never_claims_execution(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, task_outcome_contract="v1"),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"nonempty but semantically ambiguous", {}, []),
    )

    assert result["ok"] is False
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["transport_status"] == "complete"
    assert result["result"]["task_outcome"] == "unknown"


def test_v1_task_outcome_marker_must_be_the_final_nonempty_line(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, task_outcome_contract="v1"),
        run_factory=version_runner,
        popen_factory=popen_for(
            0,
            b"TASK_OUTCOME: EXECUTED\nActually no files were changed.\n",
            {},
            [],
        ),
    )

    assert result["ok"] is False
    assert result["result"]["task_outcome"] == "unknown"


def test_devspace_patch_change_blocks_before_submission_until_restart(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    launched = []
    result = runner.execute_run(
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=lambda *args, **kwargs: launched.append(True),
        compat_factory=lambda version: {"ok": True, "version": version},
        runtime_command_factory=lambda compatibility, version: ("node", "validated-oracle-cli.js"),
        devspace_compat_factory=lambda: {
            "ok": True,
            "changed": ["dist/workspaces.js"],
            "package_roots": ["package"],
            "service_restart_required": True,
        },
    )

    assert result["ok"] is False
    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert result["result"]["session_authority"] == "pre_submit"
    assert result["result"]["pre_submit_failure"]["code"] == "DEVSPACE_SERVICE_RESTART_REQUIRED"
    assert launched == []
    stderr = Path(result["result"]["artifacts"]["stderr"]).read_text(encoding="utf-8")
    assert "DEVSPACE_SERVICE_RESTART_REQUIRED" in stderr


def test_preflight_and_live_run_share_endpoint_failure_without_oracle_launch(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    job = manifest(tmp_path)
    expected = hashlib.sha256(job.read_bytes()).hexdigest()
    endpoint_failure = {
        "local": {"ok": False, "error": "ConnectionRefusedError"},
        "next_action": "CHECK_DEVSPACE_LOCAL_SERVICE",
    }

    preflight = runner.preflight_run(
        job,
        expected_manifest_sha256=expected,
        devspace_hostname="device.tailnet.ts.net",
        run_factory=version_runner,
        oracle_inspector=lambda version: {"ok": True, "ready": True},
        devspace_inspector=lambda **kwargs: {"ok": True, "ready": True},
        devspace_doctor=lambda config: endpoint_failure,
    )
    launched = []
    live = runner.execute_run(
        job,
        expected_manifest_sha256=expected,
        run_factory=version_runner,
        popen_factory=lambda *args, **kwargs: launched.append(True),
        compat_factory=lambda version: {"ok": True, "version": version},
        runtime_command_factory=lambda compatibility, version: ("node", "validated-oracle-cli.js"),
        devspace_compat_factory=lambda: {"ok": True, "ready": True},
        devspace_hostname="device.tailnet.ts.net",
        devspace_doctor=lambda config: endpoint_failure,
    )

    assert preflight["failed_checks"] == ["devspace_endpoint"]
    assert live["status"] == "pre_submit_failed"
    assert live["safe_for_fresh_run"] is True
    assert live["result"]["session_authority"] == "pre_submit"
    assert live["result"]["pre_submit_failure"]["failed_checks"] == ["devspace_endpoint"]
    assert launched == []


def test_exact_output_hash_adjudication_marks_legacy_task_not_executed(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    completed = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"workspace timeout; no files changed", {}, []),
    )
    run_dir = Path(completed["run_dir"])
    output = run_dir / "output.md"
    adjudicated = runner.adjudicate_task_outcome(
        run_dir,
        expected_output_sha256=runner.STATE.sha256_file(output),
        task_outcome="not_executed",
        reason="exact output proves workspace open timeout before file reads",
    )

    assert adjudicated["ok"] is False
    assert adjudicated["safe_for_fresh_retry"] is True
    assert adjudicated["task_outcome"] == "not_executed"
    assert adjudicated["result"]["status"] == "complete"
    assert adjudicated["result"]["transport_status"] == "complete"
    assert adjudicated["result"]["session_authority"] == "terminal"


def test_blocked_adjudication_never_authorizes_fresh_retry(tmp_path: Path) -> None:
    runner = load_runner()
    completed = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"partial work then blocked", {}, []),
    )
    run_dir = Path(completed["run_dir"])
    output = run_dir / "output.md"
    adjudicated = runner.adjudicate_task_outcome(
        run_dir,
        expected_output_sha256=runner.STATE.sha256_file(output),
        task_outcome="blocked",
        reason="partial execution cannot authorize duplicate side effects",
    )

    assert adjudicated["safe_for_fresh_retry"] is False


def test_post_submit_nonzero_requires_exact_recovery_and_never_restarts(tmp_path: Path) -> None:
    runner = load_runner()
    calls = []
    def popen(command, **kwargs):
        calls.append(list(command))
        return Process(9, [])
    result = execute_run(runner, manifest(tmp_path), run_factory=version_runner, popen_factory=popen)
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert len(calls) == 1
    assert "restart" not in calls[0]
    for action in ("harvest", "live"):
        recovery = recover_run(runner, Path(result["run_dir"]), action=action, dry_run=True)
        assert f"--{action}" in recovery["argv"]
        assert "--write-output" in recovery["argv"]
        assert "--no-recover" not in recovery["argv"]
        assert "restart" not in recovery["argv"]
        assert "--prompt" not in recovery["argv"]


def test_post_submit_response_timeout_retains_passive_live_authority(tmp_path: Path) -> None:
    runner = load_runner()
    launches: list[list[str]] = []

    def response_timeout_popen(command, **kwargs):
        launches.append(list(command))
        kwargs["stdout"].write(b"Session: exact\nprompt submitted; response streaming\n")
        kwargs["stderr"].write(
            b"ERROR: Assistant response timed out before completion; reattach later to capture the answer.\n"
        )
        kwargs["stdout"].flush()
        kwargs["stderr"].flush()
        return Process(1, [])

    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=response_timeout_popen,
    )

    state = result["result"]
    assert result["ok"] is False
    assert result["status"] == "post_submit_response_timeout"
    assert result["safe_for_fresh_run"] is False
    assert "do not relaunch recovery" in result["next_action"]
    assert len(launches) == 1
    assert state["status"] == "running"
    assert state["session_authority"] == "live"
    assert state["terminal_harvested"] is False
    assert state["transport_status"] == "post_submit_response_timeout"
    assert state["task_outcome_reason"] == "assistant-response-timeout-passive-wait"


@pytest.mark.parametrize("parallel_parent_id", [None, "d" * 64])
def test_post_submit_host_watchdog_preserves_exact_process_and_returns_attention(
    tmp_path: Path,
    parallel_parent_id: str | None,
) -> None:
    runner = load_runner()
    waits: list[float | None] = []
    process_actions: list[str] = []
    launches: list[list[str]] = []

    class HungProcess:
        pid = 4242

        def wait(self, timeout=None):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("oracle", timeout)

        def terminate(self):
            process_actions.append("terminate")

        def kill(self):
            process_actions.append("kill")

    def hung_popen(command, **kwargs):
        launches.append(list(command))
        kwargs["stdout"].write(b"Session: exact\nprompt submitted; response streaming\n")
        kwargs["stdout"].flush()
        return HungProcess()

    extras = {
        "oracle_args": ["--browser-timeout", "1s"],
        "run_id": "4" * 32,
    }
    if parallel_parent_id is not None:
        extras["parallel_parent_id"] = parallel_parent_id
    result = execute_run(
        runner,
        manifest(tmp_path, **extras),
        run_factory=version_runner,
        popen_factory=hung_popen,
    )
    state = result["result"]

    assert result["ok"] is False
    assert result["status"] == "post_submit_watchdog_timeout"
    assert result["safe_for_fresh_run"] is False
    assert result["process_preserved"] is True
    assert result["oracle_process_pid"] == 4242
    assert waits == [31]
    assert process_actions == []
    assert len(launches) == 1
    assert state["status"] == "attention_required"
    assert state["exit_code"] is None
    assert state["session_authority"] == "submitted_unknown"
    assert state["terminal_harvested"] is False
    assert state["transport_status"] == "post_submit_watchdog_timeout"
    assert state["task_outcome_reason"] == "host-wall-clock-expired-process-preserved"
    assert state["host_watchdog"] == {
        "status": "expired",
        "timeout_seconds": 31,
        "oracle_process_pid": 4242,
        "process_action": "preserved",
        "next_action": "observe-or-recover-exact-session-only",
    }
    assert Path(state["artifacts"]["browser_temp"]).is_dir()
    assert not Path(state["artifacts"]["output"]).exists()
    assert not list(Path(result["run_dir"]).glob("recovery-*-stdout.log"))


def test_host_watchdog_deadline_race_accepts_only_a_process_that_already_exited(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    output_path: Path | None = None

    class RacedExitProcess:
        pid = 4343

        def wait(self, timeout=None):
            assert timeout == 31
            assert output_path is not None
            output_path.write_text("durable answer\nTASK_OUTCOME: EXECUTED\n", encoding="utf-8")
            raise subprocess.TimeoutExpired("oracle", timeout)

        def poll(self):
            return 0

    def raced_popen(command, **kwargs):
        nonlocal output_path
        output_path = Path(command[command.index("--write-output") + 1])
        return RacedExitProcess()

    result = execute_run(
        runner,
        manifest(
            tmp_path,
            oracle_args=["--browser-timeout", "1s"],
            run_id="5" * 32,
            task_outcome_contract="v1",
        ),
        run_factory=version_runner,
        popen_factory=raced_popen,
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "complete"
    assert result["result"]["session_authority"] == "terminal"
    assert result["result"]["terminal_harvested"] is True
    assert result["result"]["host_watchdog"]["status"] == "process-exited"


def test_pro_recovery_uses_exact_slug_without_attachments_or_resubmit(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    state = runner.STATE.load_state(Path(result["run_dir"]) / "state.json")
    runner.assess_submission_readiness = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("recovery must not call new-submission readiness")
    )
    recovery = recover_run(
        runner,
        Path(result["run_dir"]),
        action="harvest",
        dry_run=True,
    )
    argv = recovery["argv"]
    assert argv[argv.index("session") + 1] == state["oracle"]["slug"]
    assert "--prompt" not in argv
    assert "--file" not in argv
    assert "--browser-attachments" not in argv
    assert "--no-recover" not in argv


def test_historical_0161_recovery_replaces_the_unpinned_stored_command(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(result["run_dir"])
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["oracle"]["resolved_version"] = "oracle 0.16.1"
    state["oracle"]["command"] = ["npx.cmd", "-y", "@steipete/oracle"]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    recovery = recover_run(
        runner,
        run_dir,
        action="harvest",
        dry_run=True,
        platform_name="nt",
    )

    assert recovery["argv"][:3] == ["npx.cmd", "-y", "@steipete/oracle@0.16.1"]


@pytest.mark.parametrize("stored_version", ["0.16.1", "0.17.0", "0.17.1"])
def test_recovery_resolves_and_compat_checks_the_exact_stored_version(
    tmp_path: Path,
    stored_version: str,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["oracle"]["resolved_version"] = f"oracle {stored_version}"
    state["oracle"]["command"] = ["oracle"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    resolved: list[list[str]] = []
    compatible: list[str] = []
    launched: list[list[str]] = []

    def resolve(command, **kwargs):
        resolved.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout=f"oracle {stored_version}\n", stderr="")

    def launch(command, **kwargs):
        launched.append(list(command))
        kwargs["stdout"].write(b"State: running\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    result = recover_run(
        runner,
        run_dir,
        action="live",
        platform_name="nt",
        run_factory=resolve,
        compat_factory=lambda version: compatible.append(version) or {"ok": True},
        popen_factory=launch,
    )

    exact_pin = ["npx.cmd", "-y", f"@steipete/oracle@{stored_version}"]
    assert resolved == [[*exact_pin, "--version"]]
    assert compatible == [f"oracle {stored_version}"]
    assert launched[0][:3] == ["node", "validated-oracle-cli.js", "session"]
    assert result["status"] == "session_live"


@pytest.mark.parametrize(
    "override",
    [
        ["oracle"],
        ["npx.cmd", "-y", "@steipete/oracle@0.16.1"],
    ],
)
def test_recovery_rejects_non_exact_override_without_resolve_or_popen(
    tmp_path: Path,
    override: list[str],
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    calls: list[str] = []

    result = recover_run(
        runner,
        Path(initial["run_dir"]),
        action="harvest",
        oracle_command=override,
        platform_name="nt",
        run_factory=lambda *args, **kwargs: calls.append("resolve"),
        compat_factory=lambda version: calls.append("compat"),
        popen_factory=lambda *args, **kwargs: calls.append("popen"),
    )

    assert result["status"] == "recovery_preflight_failed"
    assert result["error"]["code"] == "RECOVERY_COMMAND_VERSION_MISMATCH"
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert calls == []


def test_recovery_resolved_version_mismatch_never_compat_checks_or_popens(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    calls: list[str] = []

    result = recover_run(
        runner,
        Path(initial["run_dir"]),
        action="harvest",
        run_factory=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout="oracle 0.16.1\n", stderr=""
        ),
        compat_factory=lambda version: calls.append("compat"),
        popen_factory=lambda *args, **kwargs: calls.append("popen"),
    )

    assert result["error"]["code"] == "RECOVERY_RESOLVED_VERSION_MISMATCH"
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert calls == []


def test_recovery_compat_hash_failure_never_popens(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    launched: list[bool] = []

    def hash_failure(_version):
        raise runner.COMPAT.OracleCompatError(
            "ORACLE_FILE_HASH_MISMATCH",
            "unknown package bytes",
        )

    result = recover_run(
        runner,
        Path(initial["run_dir"]),
        action="harvest",
        compat_factory=hash_failure,
        popen_factory=lambda *args, **kwargs: launched.append(True),
    )

    assert result["error"]["code"] == "ORACLE_FILE_HASH_MISMATCH"
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert launched == []


@pytest.mark.parametrize("stored_version", [None, "oracle 0.18.0"])
def test_recovery_rejects_missing_or_unrecognized_stored_version_without_popen(
    tmp_path: Path,
    stored_version: str | None,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    state_path = Path(initial["run_dir"]) / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if stored_version is None:
        state["oracle"].pop("resolved_version")
    else:
        state["oracle"]["resolved_version"] = stored_version
    state_path.write_text(json.dumps(state), encoding="utf-8")
    calls: list[str] = []

    result = recover_run(
        runner,
        Path(initial["run_dir"]),
        action="harvest",
        run_factory=lambda *args, **kwargs: calls.append("resolve"),
        compat_factory=lambda version: calls.append("compat"),
        popen_factory=lambda *args, **kwargs: calls.append("popen"),
    )

    assert result["error"]["code"] == "RECOVERY_STORED_VERSION_UNVALIDATED"
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert calls == []


def test_windows_launch_uses_no_window_and_waits(tmp_path: Path) -> None:
    runner = load_runner()
    captured, events = {}, []
    class Mutex:
        def __enter__(self):
            events.append("enter")
        def __exit__(self, *args):
            events.append("exit")
    runner.STATE.project_submit_mutex = lambda *args, **kwargs: Mutex()
    runner.STATE.unresolved_project_sessions = (
        lambda *args, **kwargs: events.append("owner") or []
    )
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", captured, events),
        platform_name="nt",
        devspace_doctor=lambda config: events.append("endpoint") or {"next_action": "READY"},
    )
    assert result["ok"] is True
    assert captured["command"].count("--wait") == 1
    assert captured["kwargs"]["creationflags"] & runner.STATE.CREATE_NO_WINDOW
    assert Path(captured["kwargs"]["env"]["TEMP"]).name == "browser-temp"
    assert captured["kwargs"]["env"]["TMP"] == captured["kwargs"]["env"]["TEMP"]
    assert not Path(captured["kwargs"]["env"]["TEMP"]).exists()
    assert events == ["enter", "owner", "endpoint", "popen", "wait", "exit"]


def test_transport_mission_change_blocks_before_oracle_launch(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            transport = next((tmp_path / "runs").glob("*/mission.md"))
            transport.write_text("changed", encoding="utf-8")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed mission bytes")

    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert launched == []


def test_pro_attachment_change_blocks_before_submit(tmp_path: Path) -> None:
    runner = load_runner()
    launched = []

    class MutatingMutex:
        def __enter__(self):
            (tmp_path / "packet.zip").write_bytes(b"changed")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    def forbidden_popen(*args, **kwargs):
        launched.append(True)
        raise AssertionError("Oracle must not launch with changed attachments")

    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=forbidden_popen,
    )
    assert result["ok"] is False
    assert result["result"]["status"] == "failed"
    assert result["result"]["session_authority"] == "pre_submit"
    assert launched == []


@pytest.mark.parametrize("mutation", ["packet", "context"])
def test_caller_pinned_pro_files_reject_preparse_replacement(tmp_path: Path, mutation: str) -> None:
    runner = load_runner()
    job = pro_manifest(tmp_path)
    expected_manifest_sha256 = runner.STATE.sha256_file(job)
    payload = json.loads(job.read_text(encoding="utf-8"))
    target = (
        Path(payload["attachments"][1])
        if mutation == "packet"
        else Path(payload["project_context_manifest_path"])
    )
    target.write_bytes(target.read_bytes() + b"changed")
    launched = []

    with pytest.raises(runner.STATE.OracleStateError) as caught:
        execute_run(
            runner,
            job,
            expected_manifest_sha256=expected_manifest_sha256,
            run_factory=version_runner,
            popen_factory=lambda *args, **kwargs: launched.append(True),
        )

    assert caught.value.code in {
        "PRO_ATTACHMENT_SHA256_MISMATCH",
        "PRO_CONTEXT_MANIFEST_SHA256_MISMATCH",
    }
    assert launched == []


def test_bound_input_change_inside_submit_mutex_blocks_before_popen(tmp_path: Path) -> None:
    runner = load_runner()
    bound = tmp_path / "handoff.md"
    bound.write_text("validated", encoding="utf-8")
    launched = []
    job = manifest(tmp_path, bound_inputs=[{
        "path": str(bound.resolve()),
        "sha256": hashlib.sha256(bound.read_bytes()).hexdigest(),
    }])

    class MutatingMutex:
        def __enter__(self):
            bound.write_text("changed", encoding="utf-8")

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda *args, **kwargs: MutatingMutex()

    result = execute_run(
        runner,
        job,
        run_factory=version_runner,
        popen_factory=lambda *args, **kwargs: launched.append(True),
    )

    assert result["ok"] is False
    assert result["result"]["session_authority"] == "pre_submit"
    assert launched == []
    assert "BOUND_INPUT_SHA256_MISMATCH" in (Path(result["run_dir"]) / "stderr.log").read_text(encoding="utf-8")


def test_oracle_global_prompt_duplicate_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    first = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=duplicate_prompt_popen,
    )
    first_state = runner.STATE.load_state(Path(first["run_dir"]) / "state.json")
    assert first["status"] == "pre_submit_rejected"
    assert first["safe_for_fresh_run"] is True
    assert first_state["session_authority"] == "pre_submit"
    assert first_state["transport_status"] == "rejected_pre_submit"
    assert first_state["pre_submit_rejection"]["code"] == "ORACLE_GLOBAL_PROMPT_DUPLICATE"
    assert first_state["pre_submit_rejection"]["output_absent"] is True
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(pro_manifest(tmp_path)).run_root,
        tmp_path,
    ) == []

    launches: list[list[str]] = []
    second = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, launches),
    )
    assert second["ok"] is True
    assert launches


def test_unconfirmed_extra_high_is_proven_pre_submit_and_releases_project(
    tmp_path: Path,
) -> None:
    runner = load_runner()

    def extra_high_unconfirmed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        marker = (
            "Thinking time: option not found (requested Extra-high); "
            "refusing to submit without confirmed Extra High."
        )
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                f"ERROR: {marker}\n"
                f"User error (browser-automation): {marker}\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=extra_high_unconfirmed,
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["task_outcome"] == "not_executed"
    assert state["pre_submit_failure"]["code"] == (
        "ORACLE_THINKING_TIME_UNCONFIRMED_PRE_SUBMIT"
    )
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest(tmp_path)).run_root,
        tmp_path,
    ) == []


def test_unconfirmed_pro_heavy_is_proven_pre_submit_and_releases_project(
    tmp_path: Path,
) -> None:
    runner = load_runner()

    def pro_heavy_unconfirmed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        marker = (
            "Thinking time: option not found for pro (requested Heavy); "
            "refusing to submit without confirmed Pro Heavy."
        )
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                f"ERROR: {marker}\n"
                f"User error (browser-automation): {marker}\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    result = execute_run(
        runner,
        pro_manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=pro_heavy_unconfirmed,
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["task_outcome"] == "not_executed"
    assert state["pre_submit_failure"]["code"] == (
        "ORACLE_PRO_HEAVY_UNCONFIRMED_PRE_SUBMIT"
    )
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(pro_manifest(tmp_path)).run_root,
        tmp_path,
    ) == []


def test_profile_copy_ebusy_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    seed = tmp_path.parent / f"{tmp_path.name}-profile"
    cookies = seed / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("seed", encoding="utf-8")
    result = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="c" * 32, copy_profile=str(seed)),
        run_factory=version_runner,
        popen_factory=profile_copy_ebusy_popen,
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["transport_status"] == "failed_pre_submit"
    assert state["task_outcome"] == "not_executed"
    assert state["pre_submit_failure"]["code"] == "ORACLE_PROFILE_COPY_EBUSY_PRELAUNCH_FAILED"
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(pro_manifest(tmp_path)).run_root,
        tmp_path,
    ) == []


def test_recovery_repairs_legacy_profile_copy_ebusy_without_oracle_call(tmp_path: Path) -> None:
    runner = load_runner()
    seed = tmp_path.parent / f"{tmp_path.name}-profile"
    cookies = seed / "Default" / "Network" / "Cookies"
    cookies.parent.mkdir(parents=True)
    cookies.write_text("seed", encoding="utf-8")
    initial = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="d" * 32, copy_profile=str(seed)),
        run_factory=version_runner,
        popen_factory=profile_copy_ebusy_popen,
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    legacy = runner.STATE.load_state(state_path)
    legacy.update({"session_authority": "submitted_unknown", "transport_status": "failed", "task_outcome": "pending"})
    legacy.pop("pre_submit_failure", None)
    runner.STATE.write_json_atomic(state_path, legacy)
    calls: list[bool] = []

    recovered = runner.recover_run(
        run_dir,
        action="harvest",
        oracle_command=["oracle"],
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    settled = runner.STATE.load_state(state_path)

    assert recovered["status"] == "pre_submit_failed"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert settled["task_outcome"] == "not_executed"
    assert calls == []


def test_recovery_settles_legacy_duplicate_prompt_lock_without_oracle_call(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        pro_manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=duplicate_prompt_popen,
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    legacy["session_authority"] = "submitted_unknown"
    legacy["transport_status"] = "incomplete"
    legacy.pop("pre_submit_rejection", None)
    state_path.write_text(json.dumps(legacy), encoding="utf-8")
    calls = []

    recovered = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    settled = runner.STATE.load_state(state_path)
    assert recovered["status"] == "pre_submit_rejected"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert calls == []


def test_version_resolution_timeout_is_proven_pre_submit_and_releases_project(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path, run_id="c" * 32),
        run_factory=version_timeout_runner,
        popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Oracle must not launch after version timeout")
        ),
    )
    run_dir = Path(result["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")

    assert result["status"] == "pre_submit_failed"
    assert result["safe_for_fresh_run"] is True
    assert state["session_authority"] == "pre_submit"
    assert state["transport_status"] == "failed_pre_submit"
    assert state["pre_submit_failure"]["code"] == "ORACLE_VERSION_RESOLUTION_PRELAUNCH_FAILED"
    assert state["pre_submit_failure"]["conversation_url_absent"] is True
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest(tmp_path)).run_root,
        tmp_path,
    ) == []


def test_recovery_repairs_legacy_version_timeout_authority_without_oracle_call(tmp_path: Path) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path, run_id="d" * 32),
        run_factory=version_timeout_runner,
    )
    run_dir = Path(initial["run_dir"])
    state_path = run_dir / "state.json"
    legacy = json.loads(state_path.read_text(encoding="utf-8"))
    legacy["session_authority"] = "submitted_unknown"
    legacy["transport_status"] = "incomplete"
    legacy.pop("pre_submit_failure", None)
    state_path.write_text(json.dumps(legacy), encoding="utf-8")
    calls: list[bool] = []

    recovered = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    settled = runner.STATE.load_state(state_path)

    assert recovered["status"] == "pre_submit_failed"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert calls == []


def test_recovery_no_session_keeps_pre_submit_authority_and_allows_fresh_attempt(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="e" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    runner.STATE.write_json_atomic(
        layout.state_path,
        runner.STATE.state_payload(config, layout, status="failed", resolved_version="oracle 0.16.1"),
    )
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    def no_session(command, **kwargs):
        kwargs["stderr"].write(f"No session found with ID {layout.slug}.\n".encode())
        kwargs["stderr"].flush()
        return Process(1, [])

    recovered = recover_run(
        runner,
        layout.run_dir,
        action="harvest",
        popen_factory=no_session,
    )
    settled = runner.STATE.load_state(layout.state_path)

    assert recovered["status"] == "pre_submit_session_absent"
    assert recovered["safe_for_fresh_run"] is True
    assert settled["session_authority"] == "pre_submit"
    assert settled["pre_submit_session_absence"]["oracle_locator"] == layout.slug


def test_recovery_no_session_never_releases_submitted_unknown_run(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="f" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="oracle 0.16.1")
    state["session_authority"] = "submitted_unknown"
    runner.STATE.write_json_atomic(layout.state_path, state)
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    def no_session(command, **kwargs):
        kwargs["stderr"].write(f"No session found with ID {layout.slug}.\n".encode())
        kwargs["stderr"].flush()
        return Process(1, [])

    recovered = recover_run(
        runner,
        layout.run_dir,
        action="live",
        popen_factory=no_session,
    )
    settled = runner.STATE.load_state(layout.state_path)

    assert recovered["status"] == "attention_required"
    assert recovered.get("safe_for_fresh_run") is not True
    assert settled["session_authority"] == "submitted_unknown"


@pytest.mark.parametrize(
    ("submission_failure", "task_outcome_reason"),
    (
        (
            "ERROR: Prompt did not appear in conversation before timeout (send may have failed)",
            "user-confirmed-no-submission-after-prompt-timeout",
        ),
        (
            "ERROR: APP_MENTION_ROUTE_UNCONFIRMED\n"
            "User error (browser-automation): APP_MENTION_ROUTE_UNCONFIRMED",
            "user-confirmed-no-submission-after-app-route-unconfirmed",
        ),
    ),
    ids=("prompt-timeout", "app-route-unconfirmed"),
)
def test_user_confirmed_no_submission_is_hash_bound_idempotent_and_fail_closed(
    tmp_path: Path,
    submission_failure: str,
    task_outcome_reason: str,
) -> None:
    runner = load_runner()
    run_id = "a" * 32
    workflow_id = "b4362f04-3cf2-4f5e-b6a2-8d9443175298"
    parallel_parent_id = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    manifest_path = manifest(
        tmp_path,
        run_id=run_id,
        parallel_parent_id=parallel_parent_id,
    )
    input_mission = tmp_path / "input.md"
    input_mission.write_text("bound input", encoding="utf-8")
    input_sha = hashlib.sha256(input_mission.read_bytes()).hexdigest()
    (tmp_path / "mission.md").write_text(
        "\n".join((
            "mission body",
            "",
            "[HOST_STAGE_CONTRACT]",
            f"workflow_id={workflow_id}",
            "stage=implementation",
            f"attempt_id={run_id}",
            f"input_mission_sha256={input_sha}",
            f"exact_project_root={tmp_path.resolve()}",
            f"exact_input_mission_path={input_mission.resolve()}",
            f"Write the small UTF-8 stage receipt to: {(tmp_path / 'stage-result.json').resolve()}",
            "",
            "[DEVSPACE_WORKSPACE_ENTRY_CONTRACT]",
            "workspace body",
            "",
        )),
        encoding="utf-8",
    )
    rebind_manifest_mission(manifest_path)

    def prompt_not_observed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                f"{submission_failure}\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=prompt_not_observed,
    )
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    state = runner.STATE.load_state(state_path)
    slug = state["oracle"]["slug"]
    recovery_stdout = run_dir / "recovery-harvest-stdout.log"
    recovery_stderr = run_dir / "recovery-harvest-stderr.log"
    recovery_stdout.write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    recovery_stderr.write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )

    assert runner.exact_recovery_binding_unavailable(recovery_stdout, recovery_stderr) is True
    if task_outcome_reason.endswith("app-route-unconfirmed"):
        state["oracle"]["resolved_version"] = "0.16.1"
        runner.STATE.write_json_atomic(state_path, state)
        with pytest.raises(runner.STATE.OracleStateError) as exc:
            runner.STATE.settle_user_confirmed_no_submission(
                state_path,
                confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
                reason="user inspected the exact ChatGPT state and confirmed no submission",
            )
        assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"
        state["oracle"]["resolved_version"] = runner.STATE.ORACLE_ACTIVE_VERSION
        runner.STATE.write_json_atomic(state_path, state)
    settled = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    settlement_path = run_dir / "user-confirmed-no-submission.json"
    proof = runner.STATE.proven_user_confirmed_no_submission(state_path)

    assert settled["ok"] is True
    assert settled["safe_for_fresh_run"] is True
    assert settled["result"]["session_authority"] == "pre_submit"
    assert settled["result"]["task_outcome_reason"] == task_outcome_reason
    assert proof is not None
    assert proof["workflow_id"] == workflow_id
    assert proof["stage"] == "implementation"
    assert proof["attempt_id"] == run_id
    assert proof["input_mission_sha256"] == input_sha
    assert settlement_path.is_file()
    # Repeating the exact adjudication is idempotent and launches nothing.
    repeated = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    assert repeated["result"] == settled["result"]
    other_run_id = "9" * 32
    other_state_path = run_dir.parent / other_run_id / "state.json"
    other_state_path.parent.mkdir()
    other_state = {
        "schema": "codex.chatgpt.oracle-run-state/v1",
        "run_id": other_run_id,
        "project_root": str(tmp_path.resolve()),
        "status": "running",
        "session_authority": "submitted_unknown",
        "oracle": {"session_locator": "oracle-project-other"},
    }
    runner.STATE.write_json_atomic(other_state_path, other_state)
    blocked = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user inspected the exact ChatGPT state and confirmed no submission",
    )
    assert blocked["safe_for_fresh_run"] is False
    assert [owner["run_id"] for owner in blocked["unresolved_owners"]] == [other_run_id]
    other_state.update({"status": "attention_required", "session_authority": "pre_submit"})
    runner.STATE.write_json_atomic(other_state_path, other_state)
    assert runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id="e" * 64,
    ) == []

    reference = settled["result"]["user_confirmed_no_submission"]
    missing_reference_state = runner.STATE.load_state(state_path)
    missing_reference_state.pop("user_confirmed_no_submission")
    runner.STATE.write_json_atomic(state_path, missing_reference_state)
    owners = runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id=parallel_parent_id,
    )
    assert owners[0]["run_id"] == run_id
    restored = runner.STATE.load_state(state_path)
    restored["user_confirmed_no_submission"] = reference
    runner.STATE.write_json_atomic(state_path, restored)

    # Any contradictory later recovery revokes the release even though the
    # original no-tab/no-URL recovery still exists.
    (run_dir / "recovery-live-stdout.log").write_text(
        "State: running\n",
        encoding="utf-8",
    )
    (run_dir / "recovery-live-stderr.log").write_text("", encoding="utf-8")
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None
    owners = runner.STATE.unresolved_project_sessions(
        runner.STATE.load_manifest(manifest_path).run_root,
        tmp_path,
        parallel_parent_id="e" * 64,
    )
    assert owners[0]["run_id"] == run_id


def test_user_confirmation_rejects_bare_bindings_without_host_contract(tmp_path: Path) -> None:
    runner = load_runner()
    run_id = "e" * 32
    workflow_id = "b4362f04-3cf2-4f5e-b6a2-8d9443175298"
    parent_id = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
    manifest_path = manifest(tmp_path, run_id=run_id, parallel_parent_id=parent_id)
    (tmp_path / "mission.md").write_text(
        "\n".join((
            f"workflow_id={workflow_id}",
            "stage=implementation",
            f"attempt_id={run_id}",
            f"input_mission_sha256={'f' * 64}",
            "",
        )),
        encoding="utf-8",
    )
    rebind_manifest_mission(manifest_path)

    def prompt_not_observed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                "ERROR: Prompt did not appear in conversation before timeout (send may have failed)\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=prompt_not_observed,
    )
    run_dir = Path(failed["run_dir"])
    state = runner.STATE.load_state(run_dir / "state.json")
    slug = state["oracle"]["slug"]
    (run_dir / "recovery-harvest-stdout.log").write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n',
        encoding="utf-8",
    )
    (run_dir / "recovery-harvest-stderr.log").write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n",
        encoding="utf-8",
    )

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.settle_user_confirmed_no_submission(
            run_dir,
            confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
            reason="user said no submission",
        )
    assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"


def test_user_confirmation_cannot_replace_missing_recovery_evidence(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, run_id="f" * 32))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="0.16.1")
    state["session_authority"] = "submitted_unknown"
    runner.STATE.write_json_atomic(layout.state_path, state)
    for path in (layout.stdout_path, layout.stderr_path):
        path.touch()

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.settle_user_confirmed_no_submission(
            layout.run_dir,
            confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
            reason="user said no submission",
        )
    assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"


def test_direct_app_route_unconfirmed_can_be_user_settled_without_recovery(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    manifest_path = manifest(tmp_path)

    def app_route_unconfirmed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write(
            (
                f"Session: {slug}\n"
                "ERROR: APP_MENTION_ROUTE_UNCONFIRMED\n"
                "User error (browser-automation): APP_MENTION_ROUTE_UNCONFIRMED\n"
            ).encode()
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=app_route_unconfirmed,
    )
    run_dir = Path(failed["run_dir"])
    settled = runner.settle_user_confirmed_no_submission(
        run_dir,
        confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION,
        reason="user confirmed the exact direct run was not submitted",
    )
    proof = runner.STATE.proven_user_confirmed_no_submission(run_dir / "state.json")

    assert settled["ok"] is True
    assert settled["safe_for_fresh_run"] is True
    assert settled["result"]["session_authority"] == "pre_submit"
    assert settled["result"]["task_outcome_reason"] == (
        "user-confirmed-no-submission-after-app-route-unconfirmed"
    )
    assert proof is not None
    assert proof["settlement_eligibility"] == "oracle-direct-app-route-unconfirmed/v1"
    assert proof["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert proof["source_mission_sha256"] == proof["transport_mission_sha256"]
    assert proof["recovery_evidence"] == []
    manifest_bytes = manifest_path.read_bytes()
    manifest_path.write_bytes(manifest_bytes + b"\n")
    assert runner.STATE.proven_user_confirmed_no_submission(run_dir / "state.json") is None
    manifest_path.write_bytes(manifest_bytes)
    assert runner.STATE.proven_user_confirmed_no_submission(run_dir / "state.json") is not None


def test_direct_web_multi_child_no_submission_settlement_is_hash_bound(tmp_path: Path) -> None:
    runner = load_runner()
    parent_id = "d" * 64
    manifest_path = manifest(tmp_path, parallel_parent_id=parent_id)
    (tmp_path / "mission.md").write_text("direct web multi lane", encoding="utf-8")
    rebind_manifest_mission(manifest_path)

    def prompt_not_observed(command, **kwargs):
        slug = command[command.index("--slug") + 1]
        kwargs["stdout"].write((f"Session: {slug}\nERROR: Prompt did not appear in conversation before timeout (send may have failed)\n").encode())
        kwargs["stdout"].flush()
        return Process(1, [])

    failed = execute_run(runner, manifest_path, run_factory=version_runner, popen_factory=prompt_not_observed)
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    state = runner.STATE.load_state(state_path)
    slug = state["oracle"]["slug"]
    oracle_output = tmp_path / "runtime" / "legacy" / "oracle_output"
    lane_dir = oracle_output / "lanes" / "lane"
    lane_dir.mkdir(parents=True)
    (lane_dir / "oracle.json").write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-run/v1", "project_root": str(tmp_path.resolve()),
        "mission_path": str((tmp_path / "mission.md").resolve()), "parallel_parent_id": parent_id,
    }), encoding="utf-8")
    (oracle_output / "result.json").write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-multi-result/v1", "parent_id": parent_id,
        "lanes": [{"id": "lane", "run_dir": str(run_dir), "session_locator": slug}],
    }), encoding="utf-8")
    (run_dir / "recovery-harvest-stdout.log").write_text(
        f'No live ChatGPT tab matched session "{slug}". Attempting recovery.\n', encoding="utf-8"
    )
    (run_dir / "recovery-harvest-stderr.log").write_text(
        "Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n", encoding="utf-8"
    )

    settled = runner.settle_user_confirmed_no_submission(
        run_dir, confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION, reason="exact child inspected"
    )
    proof = runner.STATE.proven_user_confirmed_no_submission(state_path)

    assert settled["result"]["session_authority"] == "pre_submit"
    assert proof is not None
    assert proof["settlement_eligibility"] == "oracle-web-multi-child/v1"
    assert proof["provenance_mode"] == "legacy-result-lane/v1"
    assert proof["parallel_parent_id"] == parent_id
    assert proof["source_mission_sha256"] == proof["transport_mission_sha256"]
    assert proof["legacy_result_sha256"]
    assert proof["legacy_lane_manifest_sha256"]
    for path in (
        run_dir / "stdout.log", run_dir / "stderr.log", run_dir / "transcript.md",
        run_dir / "recovery-harvest-stdout.log", run_dir / "recovery-harvest-stderr.log",
    ):
        original = path.read_text(encoding="utf-8")
        path.write_text(original + "https://chatgpt.com/c/exact-child\n", encoding="utf-8")
        assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None
        path.write_text(original, encoding="utf-8")
    for path, replacement in (
        (tmp_path / "mission.md", "changed source"),
        (run_dir / "mission.md", "changed transport"),
        (oracle_output / "lanes" / "lane" / "oracle.json", "{}"),
        (oracle_output / "result.json", "{}"),
    ):
        original = path.read_text(encoding="utf-8")
        path.write_text(replacement, encoding="utf-8")
        assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None
        path.write_text(original, encoding="utf-8")
    (run_dir / "output.md").write_text("unexpected output", encoding="utf-8")
    assert runner.STATE.proven_user_confirmed_no_submission(state_path) is None


def test_direct_web_multi_child_settlement_requires_recovery_pair(tmp_path: Path) -> None:
    runner = load_runner()
    parent_id = "f" * 64
    config = runner.STATE.load_manifest(manifest(tmp_path, parallel_parent_id=parent_id))
    layout = runner.STATE.create_layout(config)
    layout.run_dir.mkdir(parents=True)
    layout.output_path.touch()
    layout.stdout_path.write_text(f"Session: {layout.slug}\nERROR: Prompt did not appear in conversation before timeout (send may have failed)\n", encoding="utf-8")
    layout.stderr_path.touch()
    layout.transcript_path.touch()
    (layout.run_dir / "mission.md").write_bytes(config.mission_path.read_bytes())
    oracle_output = tmp_path / "runtime" / "legacy" / "oracle_output"
    lane_dir = oracle_output / "lanes" / "lane"
    lane_dir.mkdir(parents=True)
    (lane_dir / "oracle.json").write_text(json.dumps({"schema": "codex.chatgpt.oracle-run/v1", "project_root": str(tmp_path.resolve()), "mission_path": str(config.mission_path), "parallel_parent_id": parent_id}), encoding="utf-8")
    (oracle_output / "result.json").write_text(json.dumps({"schema": "codex.chatgpt.oracle-multi-result/v1", "parent_id": parent_id, "lanes": [{"id": "lane", "run_dir": str(layout.run_dir), "session_locator": layout.slug}]}), encoding="utf-8")
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="0.16.1")
    state["session_authority"] = "submitted_unknown"
    runner.STATE.write_json_atomic(layout.state_path, state)

    with pytest.raises(runner.STATE.OracleStateError) as exc:
        runner.settle_user_confirmed_no_submission(layout.run_dir, confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION, reason="no recovery")
    assert exc.value.code == "NO_SUBMISSION_EVIDENCE_INCOMPLETE"


def test_settlement_transcript_scan_uses_canonical_path_not_state_mapping(tmp_path: Path) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path))
    layout = runner.STATE.create_layout(config)
    layout.run_dir.mkdir(parents=True)
    layout.stdout_path.touch()
    layout.stderr_path.touch()
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="0.16.1")
    runner.STATE.write_json_atomic(layout.state_path, state)
    layout.transcript_path.write_text("https://chatgpt.com/c/hidden-in-canonical\n", encoding="utf-8")
    state["artifacts"].pop("transcript")
    runner.STATE.write_json_atomic(layout.state_path, state)
    assert runner.STATE._settlement_logs_have_conversation_url(layout.state_path) is True

    layout.transcript_path.unlink()
    state["artifacts"]["transcript"] = str(tmp_path / "foreign.md")
    runner.STATE.write_json_atomic(layout.state_path, state)
    assert runner.STATE._settlement_logs_have_conversation_url(layout.state_path) is True
    state["artifacts"]["transcript"] = str(layout.transcript_path)
    runner.STATE.write_json_atomic(layout.state_path, state)
    assert runner.STATE._settlement_logs_have_conversation_url(layout.state_path) is False

    layout.transcript_path.write_bytes(b"\xff")
    assert runner.STATE._settlement_logs_have_conversation_url(layout.state_path) is True
    layout.transcript_path.unlink()
    target = tmp_path / "transcript-target.md"
    target.write_text("no url", encoding="utf-8")
    try:
        layout.transcript_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")
    assert runner.STATE._settlement_logs_have_conversation_url(layout.state_path) is True


@pytest.mark.parametrize("field", ["parallel_parent_id", "mission_sha256", "oracle_locator", "requested_run_id"])
def test_direct_web_multi_child_settlement_rejects_identity_mismatch(tmp_path: Path, field: str) -> None:
    runner = load_runner()
    config = runner.STATE.load_manifest(manifest(tmp_path, parallel_parent_id="b" * 64))
    layout = runner.STATE.create_layout(config, run_id=config.requested_run_id)
    layout.run_dir.mkdir(parents=True)
    (layout.run_dir / "mission.md").write_bytes(config.mission_path.read_bytes())
    layout.output_path.touch()
    layout.transcript_path.touch()
    layout.stdout_path.write_text(f"Session: {layout.slug}\nERROR: Prompt did not appear in conversation before timeout (send may have failed)\n", encoding="utf-8")
    layout.stderr_path.touch()
    (layout.run_dir / "recovery-harvest-stdout.log").write_text(f'No live ChatGPT tab matched session "{layout.slug}".\n', encoding="utf-8")
    (layout.run_dir / "recovery-harvest-stderr.log").write_text("Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n", encoding="utf-8")
    oracle_output = tmp_path / "runtime" / "legacy" / "oracle_output"
    lane_dir = oracle_output / "lanes" / "lane"
    lane_dir.mkdir(parents=True)
    (lane_dir / "oracle.json").write_text(json.dumps({"schema": "codex.chatgpt.oracle-run/v1", "project_root": str(tmp_path.resolve()), "mission_path": str(config.mission_path), "parallel_parent_id": "b" * 64}), encoding="utf-8")
    (oracle_output / "result.json").write_text(json.dumps({"schema": "codex.chatgpt.oracle-multi-result/v1", "parent_id": "b" * 64, "lanes": [{"id": "lane", "run_dir": str(layout.run_dir), "session_locator": layout.slug}]}), encoding="utf-8")
    state = runner.STATE.state_payload(config, layout, status="attention_required", resolved_version="0.16.1")
    state["session_authority"] = "submitted_unknown"
    if field == "parallel_parent_id":
        state[field] = "invalid"
    elif field == "mission_sha256":
        state["mission"]["sha256"] = "0" * 64
    else:
        if field == "oracle_locator":
            state["oracle"]["session_locator"] = "oracle-foreign"
        else:
            state["requested_run_id"] = layout.run_id
    runner.STATE.write_json_atomic(layout.state_path, state)

    with pytest.raises((runner.STATE.OracleStateError, runner.OracleRunError)) as exc:
        runner.settle_user_confirmed_no_submission(layout.run_dir, confirmation=runner.STATE.USER_CONFIRMED_NO_SUBMISSION, reason="identity mismatch")
    assert exc.value.code in {"NO_SUBMISSION_EVIDENCE_INCOMPLETE", "SETTLEMENT_PARALLEL_PARENT_ID_INVALID"}


def test_recovery_captures_output_and_updates_state(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def recovery_popen(command, **kwargs):
        captured_env.update(kwargs["env"])
        output = Path(command[command.index("--write-output") + 1])
        output.write_text("recovered answer", encoding="utf-8")
        kwargs["stdout"].write(b"State: complete\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    captured_env = {}
    recovered = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=recovery_popen,
    )
    assert recovered["ok"] is True
    assert recovered["status"] == "complete"
    assert Path(recovered["output_path"]).read_text(encoding="utf-8") == "recovered answer"
    assert recovered["result"]["status"] == "complete"
    assert Path(captured_env["TEMP"]).name == "recovery-harvest-browser-temp"
    assert not Path(captured_env["TEMP"]).exists()
    transcript = Path(recovered["result"]["artifacts"]["transcript"]).read_text(encoding="utf-8")
    assert "recovered answer" in transcript


def test_running_exact_session_cannot_publish_partial_harvest(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def live_harvest(command, **kwargs):
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("partial answer still flushing", encoding="utf-8")
        kwargs["stdout"].write(b"State: running\nSignals: stop=yes send=no\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    recovered = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=live_harvest,
    )

    state = runner.STATE.load_state(run_dir / "state.json")
    assert recovered["status"] == "session_live"
    assert recovered["ok"] is False
    assert state["session_authority"] == "live"
    assert state["terminal_harvested"] is False
    assert not Path(state["artifacts"]["output"]).exists()
    assert not (run_dir / "recovery-harvest-candidate.md").exists()


def test_delivery_timeout_after_visible_work_cannot_settle_a_terminal_harvest(tmp_path: Path) -> None:
    """Regression: ChatGPT can keep executing after Oracle sees this error text."""
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def timed_out_recovery(command, **kwargs):
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("Message delivery timed out. Please try again.", encoding="utf-8")
        kwargs["stdout"].write(
            b"State: running\n"
            b"State: completed\n"
            b"Message delivery timed out. Please try again.\n"
        )
        kwargs["stdout"].flush()
        return Process(0, [])

    recovered = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=timed_out_recovery,
    )
    state = runner.STATE.load_state(run_dir / "state.json")

    assert recovered["ok"] is False
    assert recovered["status"] == "provider_delivery_timeout"
    assert state["status"] == "running"
    assert state["session_authority"] == "live"
    assert state["terminal_harvested"] is False
    assert state["transport_status"] == "post_submit_provider_delivery_timeout"
    assert state["task_outcome"] == "pending"
    assert not Path(state["artifacts"]["output"]).exists()
    assert not (run_dir / "recovery-live-candidate.md").exists()


def test_terminal_observation_cannot_regress_to_live_and_later_harvest_settles(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    run_dir = Path(result["run_dir"])

    def observation(state: str, answer: str | None = None):
        def popen(command, **kwargs):
            if answer is not None:
                Path(command[command.index("--write-output") + 1]).write_text(answer, encoding="utf-8")
            kwargs["stdout"].write(f"State: {state}\n".encode())
            kwargs["stdout"].flush()
            return Process(0, [])
        return popen

    terminal = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=observation("completed"),
    )
    # Reproduce state already regressed by the previously installed runner;
    # the durable exact live-observer log must restore terminal authority.
    regressed = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    regressed["status"] = "running"
    regressed["session_authority"] = "live"
    (run_dir / "state.json").write_text(json.dumps(regressed), encoding="utf-8")
    disagreement = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=observation("running", "partial"),
    )
    output_absent_during_disagreement = not Path(
        disagreement["result"]["artifacts"]["output"]
    ).exists()
    duplicate_launches: list[list[str]] = []
    blocked_duplicate = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"duplicate", {}, duplicate_launches),
    )
    settled = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=observation("completed", "durable answer"),
    )

    assert terminal["status"] == "terminal_observed"
    assert terminal["result"]["session_authority"] == "terminal_observed"
    assert disagreement["status"] == "terminal_settle_disagreement"
    assert disagreement["result"]["status"] == "attention_required"
    assert disagreement["result"]["session_authority"] == "terminal_observed"
    assert disagreement["result"]["terminal_harvested"] is False
    assert output_absent_during_disagreement
    assert blocked_duplicate["ok"] is False
    assert duplicate_launches == []
    assert "still owns this project" in Path(
        blocked_duplicate["result"]["artifacts"]["stderr"]
    ).read_text(encoding="utf-8")
    assert settled["ok"] is True
    assert settled["status"] == "complete"
    assert settled["result"]["session_authority"] == "terminal"
    assert Path(settled["output_path"]).read_text(encoding="utf-8") == "durable answer"


def test_pro_structured_mission_rejects_short_terminal_preamble(tmp_path: Path) -> None:
    runner = load_runner()
    mission_text = (
        "# Mission\n\n## Required answer schema\n\n"
        "1. `DIRECTION_VERDICT`: decision.\n"
        "2. `NEXT_ACTION`: action.\n"
    )
    manifest_path = pro_manifest(tmp_path, prompt_text=mission_text)
    initial = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])

    def completed_preamble(command, **kwargs):
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("I'll cross-check the evidence, then deliver the decision.", encoding="utf-8")
        kwargs["stdout"].write(b"State: completed\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    rejected = recover_run(
        runner,
        run_dir,
        action="harvest",
        popen_factory=completed_preamble,
    )
    state = runner.STATE.load_state(run_dir / "state.json")

    assert rejected["ok"] is False
    assert rejected["status"] == "pro_output_incomplete"
    assert state["session_authority"] == "terminal_observed"
    assert state["terminal_harvested"] is False
    assert not Path(state["artifacts"]["output"]).exists()

    def running_observer(command, **kwargs):
        kwargs["stdout"].write(b"State: running\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    # Exact session authority is monotonic: a later live observer cannot
    # regress the persisted terminal_observed.  The disagreement stays
    # attention_required under the same lock until a terminal harvest.
    disagreement = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=running_observer,
    )
    state_after_disagreement = runner.STATE.load_state(run_dir / "state.json")
    assert disagreement["status"] == "terminal_settle_disagreement"
    assert disagreement["result"]["status"] == "attention_required"
    assert disagreement["result"]["session_authority"] == "terminal_observed"
    assert state_after_disagreement["session_authority"] == "terminal_observed"
    assert state_after_disagreement["terminal_harvested"] is False


def test_pro_terminal_candidate_with_all_ticked_sections_promotes_without_browser(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    mission_text = (
        "# Mission\n\n## Required answer schema\n\n"
        "1. `DIRECTION_VERDICT`: decision.\n"
        "2. `WEB_MULTI_NEEDED: YES|NO`: reason.\n"
        "3. `WHY`: evidence.\n"
        "4. `SELECTED_ROUTE_ID`: route.\n"
        "5. `SEED_AUTHORITY`: authority.\n"
        "6. `MANAGER_AUTHORITY`: authority.\n"
        "7. `DATA_AND_WINDOW`: data.\n"
        "8. `COST_AND_FUNDING`: costs.\n"
        "9. `PRE_PNL_GATES`: gates.\n"
        "10. `CONTROLS`: controls.\n"
        "11. `RISK_AND_LEVERAGE`: risk.\n"
        "12. `RESOURCE_CONTRACT`: resources.\n"
        "13. `NO_RETUNE_BOUNDARY`: boundary.\n"
        "14. `EXACT_TERMINAL_VERDICTS`: verdicts.\n"
        "15. `REGULAR_WEB_IMPLEMENTATION_MISSION`: mission.\n"
        "16. `NEXT_ACTION`: action.\n"
    )
    manifest_path = pro_manifest(tmp_path, prompt_text=mission_text)
    initial = execute_run(
        runner,
        manifest_path,
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    labels = (
        "DIRECTION_VERDICT", "WEB_MULTI_NEEDED: NO", "WHY", "SELECTED_ROUTE_ID",
        "SEED_AUTHORITY", "MANAGER_AUTHORITY", "DATA_AND_WINDOW", "COST_AND_FUNDING",
        "PRE_PNL_GATES", "CONTROLS", "RISK_AND_LEVERAGE", "RESOURCE_CONTRACT",
        "NO_RETUNE_BOUNDARY", "EXACT_TERMINAL_VERDICTS",
        "REGULAR_WEB_IMPLEMENTATION_MISSION", "NEXT_ACTION",
    )
    answer = "\n\n".join(
        f"## {index}. `{label}`\n\ncontent {index}"
        for index, label in enumerate(labels, start=1)
    )

    candidate = run_dir / "recovery-harvest-candidate.md"
    candidate.write_text(answer, encoding="utf-8")
    runner.STATE.update_state(
        run_dir / "state.json",
        status="attention_required",
        session_authority="terminal_observed",
        terminal_harvested=False,
    )
    state = runner.STATE.load_state(run_dir / "state.json")
    expected_sha256 = runner.STATE.sha256_file(candidate)

    assert state["session_authority"] == "terminal_observed"
    assert runner.pro_output_satisfies_required_schema(state, candidate) is True

    promoted = runner.promote_terminal_harvest_candidate(
        run_dir,
        candidate_path=candidate,
        expected_candidate_sha256=expected_sha256,
    )
    completed = runner.STATE.load_state(run_dir / "state.json")

    assert promoted["ok"] is True
    assert promoted["artifact_sha256"] == expected_sha256
    assert candidate.is_file()
    assert Path(promoted["output_path"]).read_text(encoding="utf-8") == answer
    assert completed["status"] == "complete"
    assert completed["session_authority"] == "terminal"
    assert completed["terminal_harvested"] is True


def test_live_recovery_holds_one_exact_slug_connection_until_terminal(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    runner.STATE.update_state(
        run_dir / "state.json",
        status="running",
        exit_code=7,
        session_authority="submitted_unknown",
    )
    calls: list[str] = []
    live_timeout_ms: list[str] = []

    def recovery(command, **kwargs):
        assert "--live" in command
        calls.append("live")
        live_timeout_ms.append(kwargs["env"]["ORACLE_LIVE_TERMINAL_TIMEOUT_MS"])
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("durable exact answer", encoding="utf-8")
        # The compatibility-patched live tail keeps one recovered browser and
        # observes both states before it returns a terminal harvest.
        kwargs["stdout"].write(b"State: running\nState: completed\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    settled = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=recovery,
        settle_timeout_seconds=5,
    )

    assert calls == ["live"]
    assert live_timeout_ms == ["5000"]
    assert settled["ok"] is True
    assert settled["status"] == "complete"
    assert settled["result"]["session_authority"] == "terminal"
    assert settled["result"]["terminal_harvested"] is True
    assert Path(settled["output_path"]).read_text(encoding="utf-8") == "durable exact answer"


def test_live_recovery_slow_working_page_keeps_one_recovered_tab_until_terminal(
    tmp_path: Path,
) -> None:
    """E2E-like recovery fixture: a recovered Pro page works before it is ready."""
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    runner.STATE.update_state(
        run_dir / "state.json",
        status="running",
        exit_code=7,
        session_authority="live",
    )
    calls: list[list[str]] = []
    live_timeout_ms: list[str] = []

    def slow_working_recovery(command, **kwargs):
        calls.append(list(command))
        live_timeout_ms.append(kwargs["env"]["ORACLE_LIVE_TERMINAL_TIMEOUT_MS"])
        candidate = Path(command[command.index("--write-output") + 1])
        candidate.write_text("durable exact answer after slow readiness", encoding="utf-8")
        kwargs["stdout"].write(
            b"[browser] Recovery: Chrome listening on 127.0.0.1:53582; tab loaded.\n"
            b"[2026-08-04T12:55:00.000Z] state=working stop=yes send=no model=Pro snippet=\n"
            b"State: running\n"
            b"State: completed\n"
        )
        kwargs["stdout"].flush()
        return Process(0, [])

    settled = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=slow_working_recovery,
        settle_timeout_seconds=3600,
    )

    assert len(calls) == 1
    assert "--live" in calls[0]
    assert live_timeout_ms == ["3600000"]
    assert settled["ok"] is True
    assert settled["status"] == "complete"
    assert settled["result"]["session_authority"] == "terminal"


def test_stalled_exact_observation_retains_live_authority_and_project_lock(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])

    def stalled_observer(command, **kwargs):
        kwargs["stdout"].write(b"State: stalled\n")
        kwargs["stdout"].flush()
        return Process(0, [])

    recovered = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=stalled_observer,
        settle_timeout_seconds=0,
    )

    state = runner.STATE.load_state(run_dir / "state.json")
    assert recovered["ok"] is False
    assert recovered["status"] == "session_live"
    assert recovered["exact_session_state"] == "stalled"
    assert state["status"] == "running"
    assert state["session_authority"] == "live"
    assert state["terminal_harvested"] is False


def test_live_recovery_cli_defaults_to_one_ninety_minute_settle_process() -> None:
    runner = load_runner()
    args = runner.build_parser().parse_args([
        "recover", "--run-dir", r"C:\host-state\exact-run", "--action", "live",
    ])
    assert args.settle_timeout_seconds == 5400
    assert args.settle_interval_seconds == 15


def test_live_recovery_returns_once_when_exact_binding_is_unavailable(
    tmp_path: Path,
) -> None:
    runner = load_runner()
    initial = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(7, None, {}, []),
    )
    run_dir = Path(initial["run_dir"])
    calls: list[str] = []
    sleeps: list[float] = []

    def no_binding(command, **kwargs):
        calls.append("live")
        kwargs["stdout"].write(
            b'No live ChatGPT tab matched session "exact". Attempting recovery by reopening the saved conversation URL.\n'
            b'Cannot recover conversation: session metadata has no recoverable ChatGPT conversation URL.\n'
        )
        kwargs["stdout"].flush()
        return Process(1, [])

    result = recover_run(
        runner,
        run_dir,
        action="live",
        popen_factory=no_binding,
        settle_timeout_seconds=5400,
        settle_interval_seconds=15,
        sleep=sleeps.append,
    )

    assert calls == ["live"]
    assert sleeps == []
    assert result["ok"] is False
    assert result["status"] == "recovery_binding_unavailable"
    assert result["exact_session_state"] is None
    assert "never replace or resubmit" in result["next_action"]
    assert result["result"]["status"] == "attention_required"
    assert result["result"]["session_authority"] == "submitted_unknown"
    assert result["result"]["terminal_harvested"] is False
    assert not (run_dir / "recovery-live-candidate.md").exists()


def test_unresolved_exact_session_blocks_different_parent_submission(tmp_path: Path) -> None:
    runner = load_runner()
    first_parent = "a" * 64
    second_parent = "b" * 64
    first = execute_run(
        runner,
        manifest(tmp_path, run_id="a" * 32, parallel_parent_id=first_parent),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    launches: list[list[str]] = []

    def forbidden_launch(command, **kwargs):
        launches.append(list(command))
        raise AssertionError("a different workflow must not submit while the exact session owns the project")

    second = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32, parallel_parent_id=second_parent),
        run_factory=version_runner,
        popen_factory=forbidden_launch,
    )

    assert first["result"]["session_authority"] == "submitted_unknown"
    assert second["ok"] is False
    assert second["result"]["status"] == "failed"
    assert launches == []
    assert "still owns this project" in Path(second["result"]["artifacts"]["stderr"]).read_text(encoding="utf-8")


def test_legacy_attention_without_session_authority_is_not_a_permanent_project_lock(tmp_path: Path) -> None:
    runner = load_runner()
    first = execute_run(
        runner,
        manifest(tmp_path, run_id="a" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, None, {}, []),
    )
    first_state_path = Path(first["run_dir"]) / "state.json"
    first_state = json.loads(first_state_path.read_text(encoding="utf-8"))
    first_state["status"] = "attention_required"
    first_state.pop("session_authority", None)
    first_state_path.write_text(json.dumps(first_state), encoding="utf-8")

    launches: list[list[str]] = []
    second = execute_run(
        runner,
        manifest(tmp_path, run_id="b" * 32),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, launches),
    )

    assert second["ok"] is True
    assert launches


def test_recovery_never_downgrades_durable_complete(tmp_path: Path) -> None:
    runner = load_runner()
    result = execute_run(
        runner,
        manifest(tmp_path),
        run_factory=version_runner,
        popen_factory=popen_for(0, b"answer", {}, []),
    )
    calls = []
    recovered = recover_run(
        runner,
        Path(result["run_dir"]),
        action="harvest",
        popen_factory=lambda *args, **kwargs: calls.append(True),
    )
    assert recovered["ok"] is True
    assert recovered["monotonic_noop"] is True
    assert calls == []


def test_parallel_recovery_reuses_the_parent_scoped_submit_mutex(tmp_path: Path) -> None:
    runner = load_runner()
    parent_id = "a" * 32
    roots: list[Path] = []

    class Mutex:
        def __init__(self, root: Path):
            self.root = root

        def __enter__(self):
            roots.append(self.root)

        def __exit__(self, *args):
            return None

    runner.STATE.project_submit_mutex = lambda root, **kwargs: Mutex(root)
    result = execute_run(
        runner,
        manifest(tmp_path, parallel_parent_id=parent_id),
        run_factory=version_runner,
        popen_factory=popen_for(4, None, {}, []),
    )
    recovered = recover_run(runner, Path(result["run_dir"]), action="harvest", dry_run=True)
    expected = tmp_path.resolve() / ".oracle-parallel-submit" / parent_id
    assert result["result"]["status"] == "attention_required"
    assert recovered["status"] == "dry-run"
    assert roots == [expected, expected]
