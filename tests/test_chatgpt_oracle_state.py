from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

STATE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_state.py"
REFERENCE_FOOTER_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "oracle-task-outcome-reference-footer.md"
)


def load_state():
    name = "chatgpt_oracle_state_test"
    spec = importlib.util.spec_from_file_location(name, STATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_v1_task_outcome_accepts_exact_provider_reference_footer(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_bytes(REFERENCE_FOOTER_FIXTURE.read_bytes())

    assert state.classify_task_outcome(
        output,
        contract="v1",
        transport="devspace",
    ) == "executed"


@pytest.mark.parametrize(
    "suffix",
    [
        "Actually no files were changed.\n",
        "[note]: this is ordinary prose, not a URL\n",
        "TASK_OUTCOME: BLOCKED\n",
    ],
)
def test_v1_task_outcome_reference_footer_stays_fail_closed(
    tmp_path: Path,
    suffix: str,
) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    fixture = REFERENCE_FOOTER_FIXTURE.read_text(encoding="utf-8")
    output.write_text(f"{fixture}{suffix}", encoding="utf-8")

    assert state.classify_task_outcome(
        output,
        contract="v1",
        transport="devspace",
    ) == "unknown"


def test_v1_task_outcome_rejects_multiline_http_definition_after_marker(
    tmp_path: Path,
) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text(
        "TASK_OUTCOME: NOT_EXECUTED\n"
        "[1]: https://example.com/a\n"
        "    continued definition line\n",
        encoding="utf-8",
    )

    assert state.classify_task_outcome(
        output,
        contract="v1",
        transport="devspace",
    ) == "unknown"


def test_pro_attachment_output_is_never_marker_classified(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text(
        "TASK_OUTCOME: EXECUTED\n",
        encoding="utf-8",
    )

    assert state.classify_task_outcome(
        output,
        contract="legacy",
        transport="pro-attachment-only",
    ) == "not_applicable"


def manifest(tmp_path: Path, mission_path: Path | str, **extra) -> Path:
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path.parent / f"{tmp_path.name}-host-state").resolve())
    value = {
        "schema": "codex.chatgpt.oracle-run/v1",
        "project_root": str(tmp_path.resolve()),
        "mission_path": str(mission_path),
        "app_name": "DevSpace",
        "mode": "browser",
        "oracle_command": ["npx", "-y", "@steipete/oracle@0.17.2"],
    }
    candidate_mission = Path(str(mission_path))
    if candidate_mission.is_absolute() and candidate_mission.is_file():
        value["mission_sha256"] = hashlib.sha256(candidate_mission.read_bytes()).hexdigest()
    value.update(extra)
    if value.get("transport") == "pro-attachment-only" and "project_context_manifest_path" not in extra:
        context_manifest = tmp_path / "pro-context-manifest.json"
        context_manifest.write_text("{}", encoding="utf-8")
        value["project_context_manifest_path"] = str(context_manifest.resolve())
    if value.get("transport") == "pro-attachment-only":
        raw_attachments = value.get("attachments")
        if (
            "attachment_sha256s" not in extra
            and isinstance(raw_attachments, list)
            and all(Path(item).is_absolute() and Path(item).is_file() for item in raw_attachments)
        ):
            value["attachment_sha256s"] = [
                hashlib.sha256(Path(item).read_bytes()).hexdigest() for item in raw_attachments
            ]
        context_value = value.get("project_context_manifest_path")
        if "project_context_manifest_sha256" not in extra and isinstance(context_value, str):
            context_path = Path(context_value)
            if context_path.is_file():
                value["project_context_manifest_sha256"] = hashlib.sha256(context_path.read_bytes()).hexdigest()
    path = tmp_path / "job.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.resolve()


def test_invalid_utf8_and_relative_mission_are_rejected(tmp_path: Path) -> None:
    state = load_state()
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, bad.resolve()))
    assert exc.value.code == "UTF8_REQUIRED"
    good = tmp_path / "good.md"
    good.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, "good.md"))
    assert exc.value.code == "MISSION_PATH_ABSOLUTE_REQUIRED"


def test_manifest_mission_sha256_binds_current_bytes(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    expected = state.sha256_file(mission)

    config = state.load_manifest(manifest(tmp_path, mission.resolve(), mission_sha256=expected))
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")

    assert config.mission_sha256 == expected
    assert state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.17.0")["mission"]["sha256"] == expected


@pytest.mark.parametrize("invalid", [None, "A" * 64, "0" * 63])
def test_manifest_mission_sha256_requires_exact_lowercase_hex(tmp_path: Path, invalid: object) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), mission_sha256=invalid))

    assert exc.value.code == "MISSION_SHA256_INVALID"


def test_manifest_mission_sha256_is_required(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    job = manifest(tmp_path, mission.resolve())
    payload = json.loads(job.read_text(encoding="utf-8"))
    payload.pop("mission_sha256")
    job.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(state.OracleStateError) as caught:
        state.load_manifest(job)

    assert caught.value.code == "MISSION_SHA256_INVALID"


def test_manifest_mission_sha256_rejects_stale_bytes(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("current", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), mission_sha256="0" * 64))

    assert exc.value.code == "MISSION_SHA256_MISMATCH"
    assert exc.value.evidence == {
        "expected": "0" * 64,
        "actual": state.sha256_file(mission),
    }


def test_expected_manifest_sha256_binds_exact_parsed_bytes(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    job = manifest(tmp_path, mission.resolve())
    expected = state.sha256_file(job)

    config = state.load_manifest(job, expected_manifest_sha256=expected)
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    payload = state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.17.0")

    assert config.manifest_sha256 == expected
    assert config.expected_manifest_sha256 == expected
    assert payload["manifest"] == {
        "path": str(job),
        "actual_sha256": expected,
        "expected_sha256": expected,
    }


def test_expected_manifest_sha256_requires_exact_lowercase_hex(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    job = manifest(tmp_path, mission.resolve())

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(job, expected_manifest_sha256="A" * 64)

    assert exc.value.code == "MANIFEST_SHA256_INVALID"


def test_expected_manifest_sha256_rejects_stale_manifest_before_parsing(tmp_path: Path) -> None:
    state = load_state()
    job = tmp_path / "job.json"
    job.write_text("{", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(job.resolve(), expected_manifest_sha256="0" * 64)

    assert exc.value.code == "MANIFEST_SHA256_MISMATCH"


def test_bound_inputs_are_validated_and_persisted(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    bound = tmp_path / "handoff.md"
    mission.write_text("work", encoding="utf-8")
    bound.write_text("evidence", encoding="utf-8")
    expected = state.sha256_file(bound)

    config = state.load_manifest(manifest(
        tmp_path,
        mission.resolve(),
        bound_inputs=[{"path": str(bound.resolve()), "sha256": expected}],
    ))
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")

    assert config.bound_inputs == (bound.resolve(),)
    assert config.bound_input_sha256s == (expected,)
    assert state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.17.0")["bound_inputs"] == [
        {"path": str(bound.resolve()), "sha256": expected}
    ]


def test_bound_inputs_use_a_closed_exact_schema(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    bound = tmp_path / "handoff.md"
    mission.write_text("work", encoding="utf-8")
    bound.write_text("evidence", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), bound_inputs=[{
            "path": str(bound.resolve()), "sha256": state.sha256_file(bound), "extra": True,
        }]))
    assert exc.value.code == "BOUND_INPUT_INVALID"

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), bound_inputs=[{
            "path": str(bound.resolve()), "sha256": "A" * 64,
        }]))
    assert exc.value.code == "BOUND_INPUT_SHA256_INVALID"


def test_bound_input_must_stay_inside_project(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    mission.write_text("work", encoding="utf-8")
    outside.write_text("evidence", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            bound_inputs=[{"path": str(outside.resolve()), "sha256": state.sha256_file(outside)}],
        ))

    assert exc.value.code == "BOUND_INPUT_OUTSIDE_PROJECT"


def test_bound_input_must_not_be_a_symlink(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    target = tmp_path / "target.md"
    link = tmp_path / "link.md"
    mission.write_text("work", encoding="utf-8")
    target.write_text("evidence", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(state.OracleStateError) as caught:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            bound_inputs=[{"path": str(link.absolute()), "sha256": state.sha256_file(target)}],
        ))

    assert caught.value.code == "BOUND_INPUT_FILE_INVALID"


def test_bound_input_rejects_parent_traversal(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    bound = tmp_path / "handoff.md"
    mission.write_text("work", encoding="utf-8")
    bound.write_text("evidence", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as caught:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            bound_inputs=[{
                "path": str(tmp_path / "unused" / ".." / bound.name),
                "sha256": state.sha256_file(bound),
            }],
        ))

    assert caught.value.code == "BOUND_INPUT_PATH_TRAVERSAL"


def test_bound_input_rejects_ancestor_symlink(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    target = tmp_path / "target"
    alias = tmp_path / "alias"
    mission.write_text("work", encoding="utf-8")
    target.mkdir()
    bound = target / "handoff.md"
    bound.write_text("evidence", encoding="utf-8")
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(state.OracleStateError) as caught:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            bound_inputs=[{
                "path": str(alias / bound.name),
                "sha256": state.sha256_file(bound),
            }],
        ))

    assert caught.value.code == "BOUND_INPUT_FILE_INVALID"


def test_bound_input_rejects_stale_hash(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    bound = tmp_path / "handoff.md"
    mission.write_text("work", encoding="utf-8")
    bound.write_text("current", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            bound_inputs=[{"path": str(bound.resolve()), "sha256": "0" * 64}],
        ))

    assert exc.value.code == "BOUND_INPUT_SHA256_MISMATCH"


def test_prompt_is_plain_app_plus_absolute_mission_instruction(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    config = state.load_manifest(manifest(tmp_path, mission.resolve()))
    prompt = state.composer_prompt(config)
    # Single composer authority: exactly at-DevSpace plus the absolute UTF-8
    # mission path, with no task body or extra operational prose.
    assert prompt == f"@DevSpace {mission.resolve()}"
    assert prompt == state.composer_prompt(config, mission.resolve())
    assert "\n" not in prompt


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"model": "other-model"}, "REGULAR_MODEL_INVALID"),
        ({"model_strategy": "current"}, "REGULAR_MODEL_STRATEGY_INVALID"),
        ({"thinking_time": "light"}, "REGULAR_THINKING_TIME_INVALID"),
    ],
)
def test_regular_manifest_cannot_downgrade_the_browser_profile(tmp_path: Path, extra: dict, code: str) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), **extra))
    assert exc.value.code == code


@pytest.mark.parametrize(
    "url",
    [
        "http://chatgpt.com/g/g-p-example/project",
        "https://evil.example/g/g-p-example/project",
        "https://chatgpt.com/c/not-a-project",
        "https://chatgpt.com/g/g-p-example/project?temporary-chat=true",
    ],
)
def test_project_url_is_exact_and_fail_closed(tmp_path: Path, url: str) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), chatgpt_project_url=url))
    assert exc.value.code == "CHATGPT_PROJECT_URL_INVALID"


def test_project_url_is_normalized_and_retained_in_state(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    config = state.load_manifest(manifest(
        tmp_path, mission.resolve(), chatgpt_project_url="https://chatgpt.com/g/g-p-example/project/"
    ))
    assert config.chatgpt_project_url == "https://chatgpt.com/g/g-p-example/project"
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    payload = state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.17.2")
    assert payload["profile"]["chatgpt_project_url"] == config.chatgpt_project_url


def test_pro_manifest_is_attachment_only_and_hashes_exact_files(tmp_path: Path) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"PK\x03\x04packet")
    config = state.load_manifest(
        manifest(
            tmp_path,
            prompt.resolve(),
            transport="pro-attachment-only",
            app_name=None,
            model="gpt-5.6-sol",
            thinking_time="heavy",
            attachments=[str(prompt.resolve()), str(packet.resolve())],
        )
    )
    assert config.app_name is None
    assert config.transport == "pro-attachment-only"
    assert config.attachments == (prompt.resolve(), packet.resolve())
    assert config.attachment_sha256s == (
        state.sha256_file(prompt.resolve()),
        state.sha256_file(packet.resolve()),
    )
    context_manifest = (tmp_path / "pro-context-manifest.json").resolve()
    assert config.project_context_manifest_path == context_manifest
    assert config.project_context_manifest_sha256 == state.sha256_file(context_manifest)
    composer = state.composer_prompt(config)
    assert composer.startswith(
        "Read the attached prompt/instructions and all attached files, then complete the task. "
        "Task identity: oracle-pro-"
    )
    assert composer.endswith(".")
    assert len(composer.rsplit("oracle-pro-", 1)[1][:-1]) == 24
    assert composer == state.composer_prompt(config)
    assert str(tmp_path.resolve()) not in composer
    assert "@DevSpace" not in composer
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    payload = state.state_payload(config, layout, status="prepared", resolved_version="oracle 0.17.0")
    assert payload["transport"] == "pro-attachment-only"
    assert payload["attachments"][1]["sha256"] == state.sha256_file(packet.resolve())
    assert payload["project_context_manifest"]["sha256"] == state.sha256_file(context_manifest)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing-attachments", "PRO_ATTACHMENT_SHA256S_INVALID"),
        ("uppercase-attachment", "PRO_ATTACHMENT_SHA256S_INVALID"),
        ("stale-attachment", "PRO_ATTACHMENT_SHA256_MISMATCH"),
        ("missing-context", "PRO_CONTEXT_MANIFEST_SHA256_INVALID"),
        ("stale-context", "PRO_CONTEXT_MANIFEST_SHA256_MISMATCH"),
    ],
)
def test_pro_manifest_requires_caller_pinned_attachment_and_context_hashes(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"packet")
    job = manifest(
        tmp_path,
        prompt.resolve(),
        transport="pro-attachment-only",
        app_name=None,
        model="gpt-5.6-sol",
        thinking_time="heavy",
        attachments=[str(prompt.resolve()), str(packet.resolve())],
    )
    payload = json.loads(job.read_text(encoding="utf-8"))
    if mutation == "missing-attachments":
        payload.pop("attachment_sha256s")
    elif mutation == "uppercase-attachment":
        payload["attachment_sha256s"][0] = "A" * 64
    elif mutation == "stale-attachment":
        payload["attachment_sha256s"][0] = "0" * 64
    elif mutation == "missing-context":
        payload.pop("project_context_manifest_sha256")
    else:
        payload["project_context_manifest_sha256"] = "0" * 64
    job.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(state.OracleStateError) as caught:
        state.load_manifest(job)

    assert caught.value.code == expected_code


def test_context_manifest_is_required_only_for_pro(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), project_context_manifest_path=str(mission.resolve())))
    assert exc.value.code == "CONTEXT_MANIFEST_FORBIDDEN"

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            transport="pro-attachment-only",
            app_name=None,
            model="gpt-5.6-sol",
            thinking_time="heavy",
            attachments=[str(mission.resolve())],
            project_context_manifest_path=None,
        ))
    assert exc.value.code == "PROJECT_CONTEXT_MANIFEST_PATH_ABSOLUTE_REQUIRED"

    outside = tmp_path.parent / f"{tmp_path.name}-outside-context.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(
            tmp_path,
            mission.resolve(),
            transport="pro-attachment-only",
            app_name=None,
            model="gpt-5.6-sol",
            thinking_time="heavy",
            attachments=[str(mission.resolve())],
            project_context_manifest_path=str(outside.resolve()),
        ))
    assert exc.value.code == "PRO_CONTEXT_MANIFEST_OUTSIDE_PROJECT"


def test_pro_composer_identity_changes_with_project_or_attachment_bytes(tmp_path: Path) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    packet = tmp_path / "packet.zip"
    prompt.write_text("instructions", encoding="utf-8")
    packet.write_bytes(b"first")

    def load_for(root: Path):
        root.mkdir(parents=True, exist_ok=True)
        return state.load_manifest(manifest(
            root,
            prompt.resolve(),
            transport="pro-attachment-only",
            app_name=None,
            model="gpt-5.6-sol",
            thinking_time="heavy",
            attachments=[str(prompt.resolve()), str(packet.resolve())],
        ))

    first = load_for(tmp_path / "project-one")
    other_project = load_for(tmp_path / "project-two")
    first_prompt = state.composer_prompt(first)
    assert first_prompt != state.composer_prompt(other_project)

    packet.write_bytes(b"second")
    changed_packet = load_for(tmp_path / "project-one")
    assert first_prompt != state.composer_prompt(changed_packet)


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"attachments": []}, "PRO_ATTACHMENTS_REQUIRED"),
        ({"attachments": None}, "PRO_ATTACHMENTS_REQUIRED"),
        ({"attachments": ["missing.txt"]}, "ATTACHMENT_0_ABSOLUTE_REQUIRED"),
        ({"model": "gpt-5.6"}, "PRO_MODEL_INVALID"),
        ({"model_strategy": "current"}, "PRO_MODEL_STRATEGY_INVALID"),
        ({"thinking_time": "extended"}, "PRO_THINKING_TIME_INVALID"),
        ({"research": "deep"}, "PRO_RESEARCH_FORBIDDEN"),
        ({"app_name": "DevSpace"}, "PRO_APP_FORBIDDEN"),
    ],
)
def test_pro_manifest_fails_closed_without_exact_contract(tmp_path: Path, extra: dict, code: str) -> None:
    state = load_state()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("instructions", encoding="utf-8")
    value = {
        "transport": "pro-attachment-only",
        "app_name": None,
        "model": "gpt-5.6-sol",
        "thinking_time": "heavy",
        "attachments": [str(prompt.resolve())],
    }
    value.update(extra)
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, prompt.resolve(), **value))
    assert exc.value.code == code


def test_regular_manifest_requires_exact_devspace_app(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), app_name="OtherWorkspace"))

    assert exc.value.code == "DEVSPACE_APP_REQUIRED"


def test_oracle_commands_pin_the_active_and_recoverable_versions() -> None:
    state = load_state()

    assert state.ORACLE_ACTIVE_VERSION == "0.17.2"
    assert state.ORACLE_RECOVERABLE_VERSIONS == ("0.16.1", "0.17.0", "0.17.1", "0.17.2")
    assert state.WAIT_CAPABLE_VERSIONS == {"0.17.0", "0.17.1", "0.17.2"}
    assert state.ORACLE_UI_FAILURE_SETTLEMENT_VERSIONS == {"0.17.1", "0.17.2"}
    assert state.default_oracle_command(platform_name="nt") == (
        "npx.cmd", "-y", "@steipete/oracle@0.17.2",
    )
    assert state.pinned_oracle_command("oracle 0.16.1", platform_name="posix") == (
        "npx", "-y", "@steipete/oracle@0.16.1",
    )
    assert state.validate_oracle_command(["npx", "--yes", "@steipete/oracle@0.17.2"])

    for command in (
        ["oracle"],
        ["npx", "-y", "@steipete/oracle"],
        ["npx", "-y", "@steipete/oracle@0.16.1"],
        ["npx", "-y", "@steipete/oracle@0.18.0"],
    ):
        with pytest.raises(state.OracleStateError) as exc:
            state.validate_oracle_command(command)
        assert exc.value.code == "ORACLE_COMMAND_FORBIDDEN"


def test_layout_uses_oracle_exact_ten_character_session_suffix(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    config = state.load_manifest(manifest(tmp_path, mission.resolve()))
    layout = state.create_layout(config, run_id="20260725T151414Z-a3aeba967d99")
    assert layout.slug == "oracle-test-layout-uses-a3aeba967d"


def test_nonempty_output_mutex_and_windows_flags(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    assert state.output_is_nonempty(output) is False
    output.write_text(" \n", encoding="utf-8")
    assert state.output_is_nonempty(output) is False
    output.write_text("answer", encoding="utf-8")
    assert state.output_is_nonempty(output) is True
    assert state.mutex_wait_succeeded(state.WAIT_ABANDONED) is True
    assert state.mutex_wait_succeeded(state.WAIT_TIMEOUT) is False
    assert state.windows_subprocess_kwargs(platform_name="nt")["creationflags"] & state.CREATE_NO_WINDOW


def test_run_owned_browser_temp_is_removed_and_prior_boot_orphans_are_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state()
    run_root = tmp_path / "runs"
    stale = run_root / "old-run" / "browser-temp"
    live = run_root / "live-run" / "browser-temp"
    monkeypatch.setattr(state, "host_uptime_ms", lambda **kwargs: 500)
    state.browser_temp_environment(stale)
    state.browser_temp_environment(live)
    stale_marker = json.loads((stale / ".owner.json").read_text(encoding="utf-8"))
    stale_marker["host_uptime_ms"] = 900
    state.write_json_atomic(stale / ".owner.json", stale_marker)

    cleaned = state.cleanup_prior_boot_browser_temps(run_root, current_uptime_ms=600)

    assert cleaned == [str(stale.resolve())]
    assert not stale.exists()
    assert live.exists()
    assert state.cleanup_owned_browser_temp(live) is True
    assert not live.exists()


def test_unsafe_oracle_args_are_rejected(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    for unsafe in (
        ["--file", "x"],
        ["restart"],
        ["--browser-tab", "current"],
        ["--force"],
        ["--chatgpt-url=https://chatgpt.com/c/foreign"],
    ):
        with pytest.raises(state.OracleStateError) as exc:
            state.load_manifest(manifest(tmp_path, mission.resolve(), oracle_args=unsafe))
        assert exc.value.code == "ORACLE_ARG_FORBIDDEN"
    config = state.load_manifest(
        manifest(
            tmp_path,
            mission.resolve(),
            oracle_args=["--timeout", "45m", "--no-notify", "--heartbeat=20", "--browser-hide-window"],
        )
    )
    assert config.oracle_args == (
        "--timeout",
        "45m",
        "--no-notify",
        "--heartbeat=20",
        "--browser-hide-window",
    )
    assert config.model_strategy == "select"
    assert config.thinking_time == "extra-high"
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), thinking_time="xhigh"))
    assert exc.value.code == "THINKING_TIME_INVALID"
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(manifest(tmp_path, mission.resolve(), oracle_command=["powershell", "-Command", "echo unsafe"]))
    assert exc.value.code == "ORACLE_COMMAND_FORBIDDEN"


def test_control_state_must_be_outside_devspace_project(tmp_path: Path) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(
            manifest(tmp_path, mission.resolve(), run_root=str((tmp_path / ".ai-bridge" / "runs").resolve()))
        )
    assert exc.value.code in {"RUN_ROOT_OUTSIDE_HOST_STATE", "HOST_STATE_OVERLAPS_PROJECT"}
    mission = tmp_path / "mission.md"
    overlap_manifest = manifest(tmp_path, mission.resolve())
    os.environ["CODEX_ORACLE_STATE_ROOT"] = str((tmp_path / "host-state").resolve())
    with pytest.raises(state.OracleStateError) as overlap:
        state.load_manifest(overlap_manifest)
    assert overlap.value.code == "HOST_STATE_OVERLAPS_PROJECT"


def test_default_profile_copy_is_skipped_when_the_copy_dependency_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    seed = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
    seed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(seed.resolve()))
    monkeypatch.setattr(state.shutil, "which", lambda name: None)

    config = state.load_manifest(manifest(tmp_path, mission.resolve()), platform_name="posix")

    assert config.copy_profile is None


def test_default_profile_copy_is_used_when_the_copy_dependency_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    seed = tmp_path.parent / f"{tmp_path.name}-oracle-profile"
    seed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(seed.resolve()))
    monkeypatch.setattr(
        state.shutil,
        "which",
        lambda name: "/usr/bin/rsync" if name == state.PROFILE_COPY_DEPENDENCY else None,
    )

    config = state.load_manifest(manifest(tmp_path, mission.resolve()), platform_name="posix")

    assert config.copy_profile == seed.resolve()


def test_windows_profile_copy_needs_no_external_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned Windows compat patch copies profiles without rsync.

    Requiring rsync on `nt` silently removed per-run profile isolation and
    blocked every parallel Web Multi lane before submission.
    """
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    seed = tmp_path.parent / f"{tmp_path.name}-windows-profile"
    seed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(seed.resolve()))
    monkeypatch.setattr(state.shutil, "which", lambda name: None)

    assert state.profile_copy_is_supported(platform_name="nt") is True
    assert state.profile_copy_is_supported(platform_name="posix") is False

    default_config = state.load_manifest(
        manifest(tmp_path, mission.resolve()), platform_name="nt"
    )
    assert default_config.copy_profile == seed.resolve()

    explicit = tmp_path.parent / f"{tmp_path.name}-windows-explicit"
    explicit.mkdir(parents=True, exist_ok=True)
    explicit_config = state.load_manifest(
        manifest(tmp_path, mission.resolve(), copy_profile=str(explicit.resolve())),
        platform_name="nt",
    )
    assert explicit_config.copy_profile == explicit.resolve()


def test_explicit_profile_copy_fails_closed_without_the_copy_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = load_state()
    mission = tmp_path / "mission.md"
    mission.write_text("work", encoding="utf-8")
    seed = tmp_path.parent / f"{tmp_path.name}-explicit-profile"
    seed.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state.shutil, "which", lambda name: None)

    with pytest.raises(state.OracleStateError) as exc:
        state.load_manifest(
            manifest(tmp_path, mission.resolve(), copy_profile=str(seed.resolve())),
            platform_name="posix",
        )

    assert exc.value.code == "COPY_PROFILE_DEPENDENCY_MISSING"
    assert exc.value.evidence["dependency"] == state.PROFILE_COPY_DEPENDENCY


def test_lifecycle_vocabulary_is_bounded_to_four_states() -> None:
    state = load_state()

    assert state.LIFECYCLE_STATES == ("running", "complete", "needs_attention", "abandoned")
    assert set(state._STATUS_TO_LIFECYCLE) == state.STATUSES
    assert set(state._STATUS_TO_LIFECYCLE.values()) <= set(state.LIFECYCLE_STATES)


def test_exact_terminal_web_evidence_outranks_stored_artifact_and_ledger(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text("answer", encoding="utf-8")

    verdict = state.resolve_lifecycle({
        "status": "failed",
        "session_authority": "terminal",
        "terminal_harvested": True,
        "artifacts": {"output": str(output)},
    })

    assert verdict == {"lifecycle": "complete", "authority_source": "exact-terminal-evidence"}


def test_durable_artifact_outranks_ledger_for_legacy_records(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text("legacy answer", encoding="utf-8")

    verdict = state.resolve_lifecycle({
        "status": "complete",
        "session_authority": "",
        "terminal_harvested": False,
        "artifacts": {"output": str(output)},
    })

    assert verdict == {"lifecycle": "complete", "authority_source": "durable-artifact"}


def test_owned_live_session_stays_running_despite_local_failure(tmp_path: Path) -> None:
    state = load_state()

    verdict = state.resolve_lifecycle(
        {
            "status": "failed",
            "session_authority": "submitted_unknown",
            "terminal_harvested": False,
            "artifacts": {"output": str(tmp_path / "missing.md")},
        },
        output_is_present=False,
    )

    assert verdict == {"lifecycle": "running", "authority_source": "exact-session-ownership"}


def test_not_executed_outcome_needs_attention_even_when_terminal(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text("TASK_OUTCOME: not_executed", encoding="utf-8")

    verdict = state.resolve_lifecycle({
        "status": "complete",
        "session_authority": "terminal",
        "terminal_harvested": True,
        "task_outcome": "not_executed",
        "artifacts": {"output": str(output)},
    })

    assert verdict["lifecycle"] == "needs_attention"


def test_markdown_bold_terminal_outcome_is_classified(tmp_path: Path) -> None:
    state = load_state()
    output = tmp_path / "output.md"
    output.write_text("Task could not run.\n\n**TASK_OUTCOME: BLOCKED**\n", encoding="utf-8")

    assert state.classify_task_outcome(
        output, contract="v1", transport="devspace"
    ) == "blocked"


def test_local_ledger_is_the_lowest_authority(tmp_path: Path) -> None:
    state = load_state()

    running = state.resolve_lifecycle({"status": "prepared"}, output_is_present=False)
    failed = state.resolve_lifecycle({"status": "failed"}, output_is_present=False)
    abandoned = state.resolve_lifecycle({"status": "abandoned"}, output_is_present=False)

    assert running == {"lifecycle": "running", "authority_source": "local-ledger"}
    assert failed == {"lifecycle": "needs_attention", "authority_source": "local-ledger"}
    assert abandoned == {"lifecycle": "abandoned", "authority_source": "explicit-abandonment"}


def test_abandoned_is_a_valid_persisted_status(tmp_path: Path) -> None:
    state = load_state()

    assert "abandoned" in state.STATUSES


def test_ledger_completion_without_a_durable_artifact_is_not_complete() -> None:
    state = load_state()

    verdict = state.resolve_lifecycle({"status": "complete"}, output_is_present=False)

    assert verdict == {"lifecycle": "needs_attention", "authority_source": "local-ledger"}
