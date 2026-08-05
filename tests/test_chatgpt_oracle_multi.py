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


def test_manifest_rejects_non_devspace_app(tmp_path: Path) -> None:
    module = load()
    path = make_manifest(tmp_path, 2)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["app_name"] = "OtherWorkspace"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(module.MultiError, match="exactly DevSpace"):
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
    assert all(item["thinking_time"] == "heavy" for item in calls)
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
