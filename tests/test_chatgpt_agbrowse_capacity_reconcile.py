from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "bin" / "chatgpt_agbrowse_bridge.py"
SPEC = importlib.util.spec_from_file_location("chatgpt_agbrowse_capacity_reconcile_test", SCRIPT)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BRIDGE
SPEC.loader.exec_module(BRIDGE)


def _lease(target: str, pid: int) -> dict:
    return {
        "owner": "web-ai", "vendor": "chatgpt", "sessionType": "send-poll",
        "browserProfileKey": "9222", "targetId": target, "sessionId": f"S-{target}",
        "url": "https://chatgpt.com/", "state": "active-session", "ownerPid": pid,
    }


def test_removes_only_dead_owner_absent_target(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "web-ai-tab-leases.json"
    path.write_text(json.dumps({"version": 1, "leases": [_lease("DEAD", 10), _lease("LIVE-TAB", 11), _lease("LIVE-PID", 12)]}), encoding="utf-8")
    monkeypatch.setattr(BRIDGE.STATE, "process_identity", lambda pid: {"alive": pid == 12})
    result = BRIDGE.reconcile_absent_dead_owner_capacity_leases(path, live_target_ids={"LIVE-TAB"}, browser_profile_key="9222")
    remaining = json.loads(path.read_text(encoding="utf-8"))["leases"]
    assert [item["targetId"] for item in remaining] == ["LIVE-TAB", "LIVE-PID"]
    assert [item["target_id"] for item in result["removed"]] == ["DEAD"]


def test_preserves_foreign_profile_and_non_active_leases(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "web-ai-tab-leases.json"
    foreign = {**_lease("FOREIGN", 10), "browserProfileKey": "9223"}
    pooled = {**_lease("POOLED", 11), "state": "pooled"}
    path.write_text(json.dumps({"version": 1, "leases": [foreign, pooled]}), encoding="utf-8")
    monkeypatch.setattr(BRIDGE.STATE, "process_identity", lambda _pid: {"alive": False})
    result = BRIDGE.reconcile_absent_dead_owner_capacity_leases(path, live_target_ids=set(), browser_profile_key="9222")
    assert result["state"] == "unchanged"
    assert len(json.loads(path.read_text(encoding="utf-8"))["leases"]) == 2
