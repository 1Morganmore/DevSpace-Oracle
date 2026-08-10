from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_multi.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load():
    spec = importlib.util.spec_from_file_location("oracle_multi_test", PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_manifest(tmp_path: Path, count: int = 7) -> Path:
    missions = []
    for index in range(count):
        path = tmp_path / f"solver-{index}.md"
        path.write_text(f"solve {index}", encoding="utf-8")
        missions.append({
            "id": f"s{index}",
            "mission_path": str(path.resolve()),
            "mission_sha256": digest(path),
        })
    merger = tmp_path / "merge.md"
    merger.write_text("Merge every listed handoff.", encoding="utf-8")
    manifest = tmp_path / "multi.json"
    manifest.write_text(json.dumps({
        "schema": "codex.chatgpt.oracle-multi/v1",
        "project_root": str(tmp_path.resolve()),
        "output_dir": str((tmp_path / "out").resolve()),
        "app_name": "DevSpace",
        "model": "gpt-5.6",
        "max_concurrency": 5,
        "solvers": missions,
        "merger_mission_path": str(merger.resolve()),
        "merger_mission_sha256": digest(merger),
    }), encoding="utf-8")
    return manifest


def prepare_recovered_execution(module, manifest: Path, parent_id: str) -> tuple[dict, Path]:
    config = module.load_manifest(manifest)
    lanes = []
    for lane in config["solvers"]:
        child_manifest = module._child_manifest(config, lane, parent_id)
        child_sha256 = digest(child_manifest)
        child_config = module.RUNNER.STATE.load_manifest(
            child_manifest,
            expected_manifest_sha256=child_sha256,
        )
        assert child_config.requested_run_id
        layout = module.RUNNER.STATE.create_layout(
            child_config,
            run_id=child_config.requested_run_id,
        )
        layout.run_dir.mkdir(parents=True)
        layout.output_path.write_text(f"recovered {lane['id']}", encoding="utf-8")
        output_sha256 = digest(layout.output_path)
        state = module.RUNNER.STATE.state_payload(
            child_config,
            layout,
            status="complete",
            resolved_version="oracle 0.17.1",
        )
        state["status"] = "complete"
        state["session_authority"] = "terminal"
        state["terminal_harvested"] = True
        state["artifact_sha256"] = output_sha256
        state["oracle"]["session_locator"] = layout.slug
        module.RUNNER.STATE.write_json_atomic(layout.state_path, state)
        provenance = child_manifest.parent / "child-provenance.json"
        lanes.append({
            "id": lane["id"],
            "ok": False,
            "run_dir": str(layout.run_dir),
            "session_locator": layout.slug,
            "child_manifest_path": str(child_manifest),
            "child_manifest_sha256": child_sha256,
            "child_provenance_path": str(provenance),
            "child_provenance_sha256": digest(provenance),
        })
    execution_path = config["output_dir"] / "execution.json"
    module.RUNNER.STATE.write_json_atomic(execution_path, {
        "schema": module.EXECUTION_SCHEMA,
        "status": "lanes_settled",
        "parent_id": parent_id,
        "manifest_path": str(config["manifest_path"]),
        "manifest_sha256": config["manifest_sha256"],
        "lanes": lanes,
    })
    return config, execution_path


def set_short_state_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    suffix = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:8]
    monkeypatch.setenv(
        "CODEX_ORACLE_STATE_ROOT",
        str((tmp_path.parent / f"s-{suffix}").resolve()),
    )


def mark_typed_lane_failure(module, execution_path: Path, index: int = 1):
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    failed = execution["lanes"][index]
    failed.update({
        "output_path": None,
        "output_sha256": None,
        "error": {
            "code": "ORACLE_MULTI_LANE_EXCEPTION",
            "type": "RuntimeError",
            "message": "lane transport exploded",
        },
    })
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    return execution, failed, Path(failed["run_dir"])


def test_manifest_rejects_non_devspace_app(tmp_path: Path) -> None:
    module = load()
    path = make_manifest(tmp_path, 2)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["app_name"] = "OtherWorkspace"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.MultiError, match="exactly DevSpace"):
        module.load_manifest(path)


def test_project_url_is_normalized_and_propagated_to_every_lane(tmp_path: Path) -> None:
    module = load()
    path = make_manifest(tmp_path, 2)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["chatgpt_project_url"] = "https://chatgpt.com/g/g-p-example/project/"
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = module.load_manifest(path)
    child = json.loads(module._child_manifest(config, config["solvers"][0], "a" * 64).read_text(encoding="utf-8"))
    assert child["chatgpt_project_url"] == "https://chatgpt.com/g/g-p-example/project"


def test_explicit_parallel_policy_caps_sessions_and_is_reported(tmp_path: Path) -> None:
    module = load()
    path = make_manifest(tmp_path, 2)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["max_concurrency"] = 2
    payload["parallel_policy"] = {
        "when": "explicit-user-request",
        "max_total_sessions": 3,
        "max_concurrency": 2,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    config = module.load_manifest(path)
    assert config["parallel_plan"] == {
        "when": "explicit-user-request",
        "solver_sessions": 2,
        "merger_sessions": 1,
        "total_sessions": 3,
        "max_total_sessions": 3,
        "max_concurrency": 2,
        "policy_max_concurrency": 2,
    }
    result = module.run_multi(
        path,
        expected_manifest_sha256=digest(path),
        parent_id="a" * 64,
        dry_run=True,
        parent_lock_held=True,
        execute=lambda *args, **kwargs: {"ok": True},
    )
    assert result["parallel_plan"] == config["parallel_plan"]

    payload["parallel_policy"]["max_total_sessions"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MultiError, match="total session cap exceeded"):
        module.load_manifest(path)

    payload["parallel_policy"] = {
        "when": "explicit-user-request",
        "max_total_sessions": 3,
        "max_concurrency": 2.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(module.MultiError, match="JSON integers"):
        module.load_manifest(path)


def test_multi_uses_unique_child_manifests_waves_and_merger(tmp_path: Path) -> None:
    module = load()
    calls = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        calls.append(value)
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text(f"answer {path.parent.name}", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    result = module.run_multi(make_manifest(tmp_path), execute=fake_execute)
    assert result["ok"] is True
    assert result["status"] == "complete"
    assert len(result["lanes"]) == 7
    assert len(calls) == 8
    assert len({item["parallel_parent_id"] for item in calls}) == 1
    assert all(item["app_name"] == "DevSpace" for item in calls)
    assert all(item["model"] == "gpt-5.6" for item in calls)
    assert all(item["model_strategy"] == "select" for item in calls)
    assert all(item["thinking_time"] == "extra-high" for item in calls)
    assert all(item["copy_profile"] for item in calls)
    assert all(len(item["mission_sha256"]) == 64 for item in calls)
    merger_text = Path(calls[-1]["mission_path"]).read_text(encoding="utf-8")
    assert merger_text.count(".md") == 7
    assert merger_text.count("sha256=") == 7
    assert all(item["output_sha256"] in merger_text for item in result["lanes"])


def test_multi_preserves_partial_results_and_rejects_over_capacity(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 3)
    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        if path.parent.name == "s1":
            return {"ok": False, "run_dir": str(run_dir)}
        (run_dir / "output.md").write_text("ok", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    result = module.run_multi(manifest, execute=fake_execute)
    assert result["ok"] is False
    assert result["status"] == "partial"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["max_concurrency"] = 6
    manifest.write_text(json.dumps(value), encoding="utf-8")
    try:
        module.load_manifest(manifest)
    except module.MultiError:
        pass
    else:
        raise AssertionError("capacity > 5 must fail")


def test_multi_settles_raised_lane_and_preserves_successful_siblings(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 3)
    sealed = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        if path.parent.name == "s1":
            raise RuntimeError("lane transport exploded")
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text(f"answer {path.parent.name}", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    result = module.run_multi(
        manifest,
        execute=fake_execute,
        terminal_seal=lambda path, raw: sealed.append((path, raw)),
    )

    assert result["status"] == "partial"
    assert [lane["id"] for lane in result["lanes"]] == ["s0", "s1", "s2"]
    assert [lane["id"] for lane in result["lanes"] if lane["ok"]] == ["s0", "s2"]
    failed = result["lanes"][1]
    assert failed["error"] == {
        "code": "ORACLE_MULTI_LANE_EXCEPTION",
        "type": "RuntimeError",
        "message": "lane transport exploded",
    }
    assert [Path(lane["output_path"]).read_text(encoding="utf-8") for lane in result["lanes"] if lane["ok"]] == [
        "answer s0",
        "answer s2",
    ]
    assert len(sealed) == 1
    assert (tmp_path / "out" / "result.json").read_bytes() == sealed[0][1]
    with pytest.raises(module.MultiError, match="terminal-sealed"):
        module.run_multi(manifest, execute=fake_execute)
    assert len(sealed) == 1


def test_multi_rejects_lane_path_traversal(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["solvers"][0]["id"] = "../../outside"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    try:
        module.load_manifest(manifest)
    except module.MultiError:
        pass
    else:
        raise AssertionError("unsafe lane id must fail")


def test_multi_rejects_manifest_changed_after_preflight_before_any_lane(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    expected = module.load_manifest(manifest)["manifest_sha256"]
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    calls = []

    with pytest.raises(module.MultiError, match="changed after preflight"):
        module.run_multi(manifest, expected_manifest_sha256=expected, execute=lambda *args, **kwargs: calls.append(args))

    assert calls == []


def test_multi_rejects_solver_mission_changed_immediately_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["max_concurrency"] = 1
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    mission = tmp_path / "solver-0.md"
    calls = []
    child_manifest = module._child_manifest

    def mutate_after_child_manifest(config, lane, parent_id):
        result = child_manifest(config, lane, parent_id)
        if lane["id"] == "s0":
            mission.write_text("stale", encoding="utf-8")
        return result

    monkeypatch.setattr(module, "_child_manifest", mutate_after_child_manifest)

    with pytest.raises(module.MultiError, match="solver mission changed after authoring"):
        module.run_multi(
            manifest,
            parent_lock_held=True,
            execute=lambda *args, **kwargs: calls.append(args),
        )

    assert calls == []


def test_multi_rejects_merger_mission_changed_before_submission(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    calls = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        calls.append(path)
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text("answer", encoding="utf-8")
        if path.parent.name == "s0":
            (tmp_path / "merge.md").write_text("stale", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    with pytest.raises(module.MultiError, match="merger mission changed after authoring"):
        module.run_multi(manifest, execute=fake_execute)

    assert len(calls) == 2


def test_multi_rejects_handoff_changed_before_merger_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    calls = []
    child_manifest = module._child_manifest

    def mutate_before_merger(config, lane, parent_id):
        result = child_manifest(config, lane, parent_id)
        if lane["id"] == "merger":
            (config["output_dir"] / "handoffs" / "s0.md").write_text("stale", encoding="utf-8")
        return result

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        calls.append(path)
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text("answer", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    monkeypatch.setattr(module, "_child_manifest", mutate_before_merger)

    with pytest.raises(module.MultiError, match="solver handoff changed after authoring"):
        module.run_multi(manifest, execute=fake_execute)

    assert len(calls) == 2
    assert all(path.parent.name != "merger" for path in calls)


def test_merger_child_binds_handoffs_inside_runner_boundary(tmp_path: Path) -> None:
    module = load()
    calls = []
    provider_calls = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        lane = path.parent.name
        calls.append(lane)
        if lane == "merger":
            child = json.loads(path.read_text(encoding="utf-8"))
            expected_inputs = [
                {
                    "path": str((tmp_path / "out" / "handoffs" / f"s{index}.md").resolve()),
                    "sha256": digest(tmp_path / "out" / "handoffs" / f"s{index}.md"),
                }
                for index in range(2)
            ]
            assert child["bound_inputs"] == expected_inputs
            Path(expected_inputs[0]["path"]).write_text("stale", encoding="utf-8")
            for item in child["bound_inputs"]:
                if digest(Path(item["path"])) != item["sha256"]:
                    raise module.MultiError("simulated runner rejected stale bound input")
            provider_calls.append(lane)
            return {"ok": True}
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "output.md").write_text(f"answer {lane}", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    with pytest.raises(module.MultiError, match="runner rejected stale bound input"):
        module.run_multi(make_manifest(tmp_path, 2), execute=fake_execute)

    assert sorted(calls[:-1]) == ["s0", "s1"]
    assert calls[-1] == "merger"
    assert provider_calls == []


def test_multi_accepts_bound_manifest_and_missions(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 2)
    expected = module.load_manifest(manifest)["manifest_sha256"]

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        return {"ok": True}

    result = module.run_multi(
        manifest,
        expected_manifest_sha256=expected,
        parent_id="a" * 64,
        dry_run=True,
        execute=fake_execute,
    )

    assert result["ok"] is True
    assert result["status"] == "complete"
    assert result["parent_id"] == "a" * 64
    assert result["manifest_sha256"] == expected


def test_terminal_seal_precedes_result_publish(tmp_path: Path) -> None:
    module = load()
    job = make_manifest(tmp_path, 2)
    result_path = tmp_path / "out" / "result.json"
    sealed = {}

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        return {"ok": True}

    def terminal_seal(path: Path, raw: bytes) -> None:
        assert path == result_path
        assert not path.exists()
        sealed["raw"] = raw

    result = module.run_multi(
        job,
        expected_manifest_sha256=digest(job),
        parent_id="a" * 64,
        dry_run=True,
        execute=fake_execute,
        terminal_seal=terminal_seal,
    )

    assert result["ok"] is True
    assert result_path.read_bytes() == sealed["raw"]


def test_reconcile_recovered_lanes_binds_exact_children_without_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 3)
    parent_id = "b" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    monkeypatch.setattr(module, "_run_lane", lambda *args, **kwargs: pytest.fail("solver executed"))

    result = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )

    assert result["status"] == "merger_ready"
    assert not (config["output_dir"] / "result.json").exists()
    assert [lane["id"] for lane in result["lanes"]] == ["s0", "s1", "s2"]
    assert all(lane["ok"] for lane in result["lanes"])
    assert all(digest(Path(lane["output_path"])) == lane["output_sha256"] for lane in result["lanes"])
    mission = Path(result["merger_mission_path"])
    assert digest(mission) == result["merger_mission_sha256"]
    mission_text = mission.read_text(encoding="utf-8")
    positions = [mission_text.index(f"handoffs\\s{index}.md") for index in range(3)]
    assert positions == sorted(positions)
    assert json.loads(execution_path.read_text(encoding="utf-8"))["status"] == "merger_ready"


def test_recovered_mixed_lanes_merge_only_successes_and_publish_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 3)
    parent_id = "9" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    failed = execution["lanes"][1]
    (Path(failed["run_dir"]) / "state.json").unlink()
    (Path(failed["run_dir"]) / "output.md").unlink()
    Path(failed["run_dir"]).rmdir()
    failed.update({
        "output_path": None,
        "output_sha256": None,
        "error": {
            "code": "ORACLE_MULTI_LANE_EXCEPTION",
            "type": "RuntimeError",
            "message": "lane transport exploded",
        },
    })
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    monkeypatch.setattr(module, "_run_lane", lambda *args, **kwargs: pytest.fail("solver executed"))

    prepared = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )

    assert [lane["id"] for lane in prepared["lanes"]] == ["s0", "s1", "s2"]
    assert prepared["lanes"][1]["error"] == failed["error"]
    assert prepared["successful_lane_count"] == 2
    assert [Path(item["path"]).stem for item in prepared["bound_inputs"]] == ["s0", "s2"]
    mission_text = Path(prepared["merger_mission_path"]).read_text(encoding="utf-8")
    assert "handoffs\\s0.md" in mission_text
    assert "handoffs\\s1.md" not in mission_text
    assert "handoffs\\s2.md" in mission_text
    calls = []
    sealed = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        calls.append(json.loads(path.read_text(encoding="utf-8")))
        return {"ok": True, "run_dir": str(tmp_path / "merger-run")}

    result = module.resume_recovered_merger(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
        terminal_seal=lambda path, raw: sealed.append((path, raw)),
        execute=fake_execute,
    )

    assert result["status"] == "partial"
    assert result["successful_lane_count"] == 2
    assert len(calls) == 1
    assert calls[0]["bound_inputs"] == prepared["bound_inputs"]
    assert (config["output_dir"] / "result.json").read_bytes() == sealed[0][1]


def test_reconcile_refuses_typed_failure_with_live_exact_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "7" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution, _, run_dir = mark_typed_lane_failure(module, execution_path)
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "status": "attention_required",
        "session_authority": "submitted_unknown",
        "terminal_harvested": False,
        "artifact_sha256": None,
    })
    (run_dir / "output.md").unlink()
    module.RUNNER.STATE.write_json_atomic(state_path, state)

    with pytest.raises(module.MultiError, match="terminal authority"):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )

    assert json.loads(execution_path.read_text(encoding="utf-8")) == execution
    assert not (config["output_dir"] / "merger").exists()
    assert not (config["output_dir"] / "result.json").exists()


def test_reconcile_promotes_stale_typed_failure_after_exact_terminal_harvest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "6" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    _, _, run_dir = mark_typed_lane_failure(module, execution_path)
    monkeypatch.setattr(module, "_run_lane", lambda *args, **kwargs: pytest.fail("solver executed"))

    prepared = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )

    recovered = prepared["lanes"][1]
    assert recovered["ok"] is True
    assert "error" not in recovered
    assert Path(recovered["output_path"]).read_text(encoding="utf-8") == "recovered s1"
    assert digest(Path(recovered["output_path"])) == recovered["output_sha256"]
    assert prepared["successful_lane_count"] == 2
    merger_calls = []
    sealed = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        merger_calls.append(path)
        return {"ok": True, "run_dir": str(tmp_path / "merger-run")}

    result = module.resume_recovered_merger(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
        terminal_seal=lambda path, raw: sealed.append((path, raw)),
        execute=fake_execute,
    )

    assert result["status"] == "complete"
    assert len(merger_calls) == 1
    assert (config["output_dir"] / "result.json").read_bytes() == sealed[0][1]


def test_reconcile_accepts_exact_proven_pre_submit_execute_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "5" * 64
    _, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    failed = execution["lanes"][1]
    failed.update({"output_path": None, "output_sha256": None})
    run_dir = Path(failed["run_dir"])
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update({
        "status": "attention_required",
        "session_authority": "pre_submit",
        "terminal_harvested": False,
        "artifact_sha256": None,
        "submission_readiness": {
            "schema": "codex.chatgpt.oracle-submission-readiness/v1",
            "ready": False,
            "checks": [{"code": "DEVSPACE_REACHABLE", "ok": False}],
            "failed_checks": ["DEVSPACE_REACHABLE"],
            "error": {"code": "SUBMISSION_NOT_READY"},
        },
    })
    (run_dir / "output.md").unlink()
    Path(state["artifacts"]["stdout"]).write_bytes(b"")
    Path(state["artifacts"]["stderr"]).write_bytes(b"")
    module.RUNNER.STATE.write_json_atomic(state_path, state)
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    monkeypatch.setattr(module, "_run_lane", lambda *args, **kwargs: pytest.fail("solver executed"))

    prepared = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )

    recovered = prepared["lanes"][1]
    assert recovered["ok"] is False
    assert recovered["error"] == {
        "code": "ORACLE_MULTI_LANE_EXCEPTION",
        "type": "OraclePreSubmitFailure",
        "message": "SUBMISSION_NOT_READY",
    }
    assert prepared["successful_lane_count"] == 1
    assert [Path(item["path"]).stem for item in prepared["bound_inputs"]] == ["s0"]


@pytest.mark.parametrize("state_bytes", [None, b"{"], ids=["missing", "corrupt"])
def test_reconcile_refuses_existing_failed_run_without_exact_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state_bytes: bytes | None,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "4" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution, _, run_dir = mark_typed_lane_failure(module, execution_path)
    state_path = run_dir / "state.json"
    if state_bytes is None:
        state_path.unlink()
    else:
        state_path.write_bytes(state_bytes)

    with pytest.raises(module.MultiError, match="exact run state is unavailable"):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )

    assert json.loads(execution_path.read_text(encoding="utf-8")) == execution
    assert not (config["output_dir"] / "merger").exists()
    assert not (config["output_dir"] / "result.json").exists()


def test_reconcile_recovered_lanes_refuses_invalid_or_all_failed_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "8" * 64
    _, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    typed_error = {
        "code": "ORACLE_MULTI_LANE_EXCEPTION",
        "type": "RuntimeError",
        "message": "lane transport exploded",
    }
    first = execution["lanes"][0]
    first_session_locator = first["session_locator"]
    first.update({"output_path": None, "output_sha256": None, "error": {**typed_error, "code": "UNTYPED"}})
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)

    with pytest.raises(module.MultiError, match="typed settled failure"):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )

    first["error"] = typed_error
    first["session_locator"] = "oracle-foreign-session"
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    with pytest.raises(module.MultiError, match="session identity mismatch"):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )

    first["session_locator"] = first_session_locator
    execution["lanes"][1].update({
        "output_path": None,
        "output_sha256": None,
        "error": typed_error,
    })
    for lane in execution["lanes"]:
        run_dir = Path(lane["run_dir"])
        (run_dir / "state.json").unlink()
        (run_dir / "output.md").unlink()
        run_dir.rmdir()
    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    with pytest.raises(module.MultiError, match="no successful recovered lanes"):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("parent-manifest", "parent manifest identity mismatch"),
        ("child-manifest", "child manifest changed"),
        ("provenance", "child provenance changed"),
        ("lane-mission", "solver mission changed"),
        ("parent-id", "parent identity mismatch"),
        ("run-dir", "run directory identity mismatch"),
        ("session", "session identity mismatch"),
        ("authority", "terminal authority"),
        ("output", "durable output hash mismatch"),
    ],
)
def test_reconcile_recovered_lanes_rejects_any_exact_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "c" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    first = execution["lanes"][0]
    run_dir = Path(first["run_dir"])
    state_path = run_dir / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))

    match case:
        case "parent-manifest":
            execution["manifest_sha256"] = "0" * 64
        case "child-manifest":
            Path(first["child_manifest_path"]).write_bytes(
                Path(first["child_manifest_path"]).read_bytes() + b"\n"
            )
        case "provenance":
            Path(first["child_provenance_path"]).write_bytes(
                Path(first["child_provenance_path"]).read_bytes() + b"\n"
            )
        case "lane-mission":
            config["solvers"][0]["mission_path"].write_text("changed", encoding="utf-8")
        case "parent-id":
            state["parallel_parent_id"] = "d" * 64
        case "run-dir":
            state["run_id"] = "foreign-run"
        case "session":
            first["session_locator"] = "oracle-foreign-session"
        case "authority":
            state["session_authority"] = "live"
        case "output":
            (run_dir / "output.md").write_text("changed", encoding="utf-8")
        case _:
            raise AssertionError(case)

    module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    if case in {"parent-id", "run-dir", "authority"}:
        module.RUNNER.STATE.write_json_atomic(state_path, state)

    with pytest.raises(module.MultiError, match=message):
        module.reconcile_recovered_lanes(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
        )

    assert not (config["output_dir"] / "result.json").exists()


def test_resume_recovered_merger_executes_once_and_seals_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "e" * 64
    config, _ = prepare_recovered_execution(module, manifest, parent_id)
    prepared = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )
    calls = []
    sealed = []

    def fake_execute(path: Path, *, expected_manifest_sha256: str, dry_run: bool):
        assert expected_manifest_sha256 == digest(path)
        child = json.loads(path.read_text(encoding="utf-8"))
        calls.append(child)
        assert child["bound_inputs"] == prepared["bound_inputs"]
        return {"ok": True, "run_dir": str(tmp_path / "merger-run")}

    result = module.resume_recovered_merger(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
        terminal_seal=lambda path, raw: sealed.append((path, raw)),
        execute=fake_execute,
    )

    assert result["status"] == "complete"
    assert len(calls) == 1
    assert calls[0]["parallel_parent_id"] == parent_id
    assert "-merger-" in calls[0]["run_id"]
    result_path = config["output_dir"] / "result.json"
    assert len(sealed) == 1
    assert result_path.read_bytes() == sealed[0][1]
    with pytest.raises(module.MultiError, match="terminal-sealed"):
        module.resume_recovered_merger(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
            execute=fake_execute,
        )
    assert len(calls) == 1


@pytest.mark.parametrize("target", ["mission", "handoff", "uncertain", "existing-run"])
def test_resume_recovered_merger_refuses_drift_or_uncertain_existing_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    module = load()
    set_short_state_root(monkeypatch, tmp_path)
    manifest = make_manifest(tmp_path, 2)
    parent_id = "f" * 64
    config, execution_path = prepare_recovered_execution(module, manifest, parent_id)
    prepared = module.reconcile_recovered_lanes(
        manifest,
        expected_manifest_sha256=digest(manifest),
        parent_id=parent_id,
        parent_lock_held=True,
    )
    if target == "mission":
        Path(prepared["merger_mission_path"]).write_text("changed", encoding="utf-8")
    elif target == "handoff":
        Path(prepared["bound_inputs"][0]["path"]).write_text("changed", encoding="utf-8")
    elif target == "uncertain":
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        execution["status"] = "merger_submitting"
        execution["merger_run_dir"] = str(tmp_path / "uncertain-merger")
        module.RUNNER.STATE.write_json_atomic(execution_path, execution)
    else:
        merger_manifest = module._child_manifest(
            config,
            {
                "id": "merger",
                "mission_path": Path(prepared["merger_mission_path"]),
                "mission_sha256": prepared["merger_mission_sha256"],
                "bound_inputs": prepared["bound_inputs"],
            },
            parent_id,
        )
        child_config = module.RUNNER.STATE.load_manifest(
            merger_manifest,
            expected_manifest_sha256=digest(merger_manifest),
        )
        layout = module.RUNNER.STATE.create_layout(
            child_config,
            run_id=child_config.requested_run_id,
        )
        layout.run_dir.mkdir(parents=True)
    calls = []

    with pytest.raises(module.MultiError):
        module.resume_recovered_merger(
            manifest,
            expected_manifest_sha256=digest(manifest),
            parent_id=parent_id,
            parent_lock_held=True,
            execute=lambda *args, **kwargs: calls.append(args),
        )

    assert calls == []


def test_live_cli_requires_and_propagates_manifest_hash(monkeypatch, capsys, tmp_path: Path) -> None:
    module = load()
    job = make_manifest(tmp_path, 2)
    expected = digest(job)
    calls = []

    monkeypatch.setattr(module, "run_multi", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert module.main(["--manifest", str(job)]) == 1
    assert "MANIFEST_SHA256_REQUIRED" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert calls == []

    monkeypatch.setattr(
        module,
        "run_multi",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"ok": True},
    )
    assert module.main([
        "--manifest", str(job), "--expected-manifest-sha256", expected,
    ]) == 0
    capsys.readouterr()
    assert calls == [((job,), {"expected_manifest_sha256": expected, "dry_run": False})]


def test_recovery_cli_allows_reconcile_but_refuses_unsealed_resume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    module = load()
    job = make_manifest(tmp_path, 2)
    expected = digest(job)
    calls = []
    monkeypatch.setattr(
        module,
        "reconcile_recovered_lanes",
        lambda *args, **kwargs: calls.append(("reconcile", args, kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "resume_recovered_merger",
        lambda *args, **kwargs: calls.append(("resume", args, kwargs)) or {"ok": True},
    )

    assert module.main([
        "--manifest",
        str(job),
        "--expected-manifest-sha256",
        expected,
        "--reconcile-recovered",
    ]) == 0
    capsys.readouterr()
    assert calls == [
        (
            "reconcile",
            (job,),
            {"expected_manifest_sha256": expected},
        )
    ]
    calls.clear()

    assert module.main([
        "--manifest",
        str(job),
        "--expected-manifest-sha256",
        expected,
        "--resume-merger",
    ]) == 1
    assert "comprehensive terminal-seal callback" in json.loads(
        capsys.readouterr().out
    )["error"]["message"]
    assert calls == []

    assert module.main([
        "--manifest",
        str(job),
        "--expected-manifest-sha256",
        expected,
        "--reconcile-recovered",
        "--resume-merger",
    ]) == 1
    assert "choose exactly one recovery action" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert calls == []
