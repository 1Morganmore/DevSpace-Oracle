from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_oracle_multi.py"


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
        missions.append({"id": f"s{index}", "mission_path": str(path.resolve())})
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
    }), encoding="utf-8")
    return manifest


def test_multi_uses_unique_child_manifests_waves_and_merger(tmp_path: Path) -> None:
    module = load()
    calls = []

    def fake_execute(path: Path, *, dry_run: bool):
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
    merger_text = Path(calls[-1]["mission_path"]).read_text(encoding="utf-8")
    assert merger_text.count(".md") == 7


def test_multi_preserves_partial_results_and_rejects_over_capacity(tmp_path: Path) -> None:
    module = load()
    manifest = make_manifest(tmp_path, 3)
    def fake_execute(path: Path, *, dry_run: bool):
        run_dir = path.parent / "fake-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        if path.parent.name == "s1":
            return {"ok": False, "run_dir": str(run_dir)}
        (run_dir / "output.md").write_text("ok", encoding="utf-8")
        return {"ok": True, "run_dir": str(run_dir)}

    result = module.run_multi(manifest, execute=fake_execute)
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
