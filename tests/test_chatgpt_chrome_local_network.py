from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "bin" / "chatgpt_chrome_local_network.py"
SETUP_MODULE_PATH = ROOT / "skills" / "chatgpt-workspace-setup" / "scripts" / "devspace_tailscale_setup.py"


def load_module():
    spec = importlib.util.spec_from_file_location("chatgpt_chrome_local_network_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_setup_module():
    spec = importlib.util.spec_from_file_location("devspace_tailscale_setup_wiring_test", SETUP_MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeKey:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def __enter__(self) -> "FakeKey":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeRegistry:
    HKEY_CURRENT_USER = object()
    KEY_READ = 0x1
    KEY_WRITE = 0x2
    REG_SZ = 1

    def __init__(
        self,
        values: dict[str, str] | None = None,
        *,
        missing: bool = False,
        fail_writes: bool = False,
        drop_writes: bool = False,
    ) -> None:
        self._values: dict[str, str] = dict(values or {})
        self.missing = missing
        self.fail_writes = fail_writes
        self.drop_writes = drop_writes
        self.write_calls: list[tuple[str, str]] = []

    def OpenKey(self, root: object, subkey: str, reserved: int, access: int) -> FakeKey:
        if self.missing:
            raise FileNotFoundError("policy subkey absent")
        return FakeKey(self._values)

    def CreateKeyEx(self, root: object, subkey: str, reserved: int, access: int) -> FakeKey:
        if self.fail_writes:
            raise PermissionError("denied")
        return FakeKey(self._values)

    def EnumValue(self, key: FakeKey, index: int) -> tuple[str, str, int]:
        items = list(key.values.items())
        if index >= len(items):
            raise OSError(259, "no more values")
        name, value = items[index]
        return (name, value, FakeRegistry.REG_SZ)

    def SetValueEx(self, key: FakeKey, name: str, reserved: int, kind: int, value: str) -> None:
        self.write_calls.append((name, value))
        if self.fail_writes:
            raise PermissionError("denied")
        if not self.drop_writes:
            key.values[name] = value


def write_seed_grant(profile: Path, *, setting: object = 1, raw: str | None = None) -> Path:
    preferences = profile / "Default" / "Preferences"
    preferences.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        preferences.write_text(raw, encoding="utf-8")
    else:
        preferences.write_text(
            json.dumps(
                {
                    "content_settings": {
                        "exceptions": {
                            "local_network": {
                                "https://chatgpt.com:443,*": {"setting": setting, "last_modified": "13370000000000000"}
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    return preferences


def test_exact_origin_write_and_sparse_value_name_selection(tmp_path: Path) -> None:
    module = load_module()
    fake = FakeRegistry(
        {
            "1": "https://example.com",
            "3": "https://other.example",
            "note": "https://keep.example",
        }
    )
    result = module.enable_policy(codex_home=tmp_path, registry=fake)
    assert fake.write_calls == [("2", "https://chatgpt.com")]
    assert result["created_value_name"] == "2"
    assert result["preserved_entry_count"] == 3
    assert result["enabled"] is True
    assert result["changed"] is True
    assert result["matching_value_names"] == ["2"]


def test_next_policy_value_name_uses_smallest_free_numeric_name() -> None:
    module = load_module()
    assert module.next_policy_value_name({"1": "a", "3": "b"}) == "2"
    assert module.next_policy_value_name({"1": "a", "2": "b", "5": "c"}) == "3"
    assert module.next_policy_value_name({}) == "1"
    assert module.next_policy_value_name({"0": "a", "note": "b"}) == "1"


def test_unrelated_entries_are_preserved_verbatim(tmp_path: Path) -> None:
    module = load_module()
    before = {
        "1": "https://example.com",
        "4": "https://other.example",
        "default": "https://keep.example",
    }
    fake = FakeRegistry(dict(before))
    module.enable_policy(codex_home=tmp_path, registry=fake)
    assert fake._values == {
        "1": "https://example.com",
        "2": "https://chatgpt.com",
        "4": "https://other.example",
        "default": "https://keep.example",
    }


def test_exact_origin_matching_is_exact_and_idempotent(tmp_path: Path) -> None:
    module = load_module()
    fake = FakeRegistry({"1": "https://chatgpt.com/", "2": "https://other.example"})
    result = module.enable_policy(codex_home=tmp_path, registry=fake)
    assert fake.write_calls == []
    assert result["changed"] is False
    assert result["created_value_name"] is None
    assert result["enabled"] is True
    assert module.policy_contains_origin({"1": "https://chatgpt.com"}) is True
    assert module.policy_contains_origin({"1": "https://chatgpt.com/"}) is True
    assert module.policy_contains_origin({"1": "https://evilchatgpt.com"}) is False
    assert module.policy_contains_origin({"1": "https://chatgpt.com:443"}) is False
    assert module.policy_contains_origin({"1": "*"}) is False


def test_readback_mismatch_fails_closed(tmp_path: Path) -> None:
    module = load_module()
    fake = FakeRegistry(drop_writes=True)
    with pytest.raises(RuntimeError, match="CHATGPT_LOCAL_NETWORK_POLICY_NOT_DURABLE"):
        module.enable_policy(codex_home=tmp_path, registry=fake)
    assert fake.write_calls == [("1", "https://chatgpt.com")]
    assert not (tmp_path / "state").exists()


def test_missing_policy_subkey_reads_as_empty() -> None:
    module = load_module()
    status = module.policy_status(registry=FakeRegistry(missing=True))
    assert status["supported"] is True
    assert status["enabled"] is False
    assert status["entry_count"] == 0


def test_permission_denial_exits_2_with_write_denied_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setattr(module, "_resolve_registry", lambda registry=None: FakeRegistry(fail_writes=True))
    assert module.main(["enable", "--codex-home", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "CHROME_POLICY_WRITE_DENIED" in err
    assert "dedicated Oracle browser profile" in err
    assert not (tmp_path / "state").exists()


def test_non_windows_enable_reports_supported_false_and_exits_0(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.main(["enable"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["supported"] is False
    assert result["enabled"] is False
    assert result["reason"] == "WINDOWS_CHROME_POLICY_ONLY"
    assert result["changed"] is False
    assert result["receipt"] is None
    direct = module.enable_policy(platform_name="linux")
    assert direct["supported"] is False
    assert module.policy_status(platform_name="linux")["supported"] is False


def test_check_is_fail_closed_on_non_windows_without_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path))
    assert module.main(["check"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["supported"] is False
    assert result["enabled"] is False
    assert result["seed_profile"]["reason"] == "SEED_PREFERENCES_MISSING"


def test_seed_grant_is_accepted(tmp_path: Path) -> None:
    module = load_module()
    profile = tmp_path / "profile"
    preferences = write_seed_grant(profile)
    seed = module.seed_grant_status(profile_dir=profile)
    assert seed["granted"] is True
    assert seed["reason"] == "SEED_GRANT_PRESENT"
    assert Path(seed["preferences_path"]) == preferences
    result = module.check_policy(platform_name="linux", profile_dir=profile)
    assert result["enabled"] is True
    assert result["supported"] is False
    assert result["seed_profile"]["granted"] is True


def test_check_or_legs_policy_and_seed(tmp_path: Path) -> None:
    module = load_module()
    profile = tmp_path / "profile"
    write_seed_grant(profile)
    result = module.check_policy(registry=FakeRegistry(), profile_dir=profile)
    assert result["enabled"] is True
    assert result["policy"]["enabled"] is False
    assert result["seed_profile"]["granted"] is True


def test_check_fails_closed_when_neither_leg_present(tmp_path: Path) -> None:
    module = load_module()
    result = module.check_policy(registry=FakeRegistry(), profile_dir=tmp_path)
    assert result["enabled"] is False
    assert result["policy"]["enabled"] is False
    assert result["seed_profile"]["granted"] is False


def test_seed_grant_requires_exact_setting_one_and_valid_json(tmp_path: Path) -> None:
    module = load_module()
    profile = tmp_path / "blocked"
    write_seed_grant(profile, setting=2)
    seed = module.seed_grant_status(profile_dir=profile)
    assert seed["granted"] is False
    assert seed["reason"] == "SEED_GRANT_ABSENT"
    other = tmp_path / "invalid"
    write_seed_grant(other, raw="{not json")
    seed = module.seed_grant_status(profile_dir=other)
    assert seed["granted"] is False
    assert seed["reason"] == "SEED_PREFERENCES_INVALID"
    missing = tmp_path / "missing"
    seed = module.seed_grant_status(profile_dir=missing)
    assert seed["granted"] is False
    assert seed["reason"] == "SEED_PREFERENCES_MISSING"


def test_seed_profile_dir_uses_oracle_default_with_env_override(tmp_path: Path) -> None:
    module = load_module()
    assert module.seed_profile_dir(env={}) == (Path.home() / ".oracle" / "browser-profile").resolve()
    assert module.seed_profile_dir(env={"ORACLE_BROWSER_PROFILE_DIR": str(tmp_path)}) == tmp_path.resolve()


def test_enable_writes_atomic_receipt(tmp_path: Path) -> None:
    module = load_module()
    fake = FakeRegistry()
    result = module.enable_policy(codex_home=tmp_path, registry=fake)
    receipts = list((tmp_path / "state").glob("chatgpt-local-network-policy-*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert payload["schema"] == "codex.chatgpt.chrome-local-network-receipt/v1"
    assert payload["origin"] == "https://chatgpt.com"
    assert payload["policy_subkey"] == r"Software\Policies\Google\Chrome\LocalNetworkAccessAllowedForUrls"
    assert payload["changed"] is True
    assert payload["created_value_name"] == "1"
    assert payload["preserved_entry_count"] == 0
    assert payload["enabled"] is True
    assert result["receipt"] == str(receipts[0])
    assert result["enabled"] is True
    assert result["supported"] is True


def test_main_check_exits_0_when_policy_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_resolve_registry", lambda registry=None: FakeRegistry({"1": "https://chatgpt.com"}))
    assert module.main(["check"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True


def test_main_check_fails_closed_when_policy_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = load_module()
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setenv("ORACLE_BROWSER_PROFILE_DIR", str(tmp_path))
    monkeypatch.setattr(module, "_resolve_registry", lambda registry=None: FakeRegistry())
    assert module.main(["check"]) == 3
    assert json.loads(capsys.readouterr().out)["enabled"] is False


def test_setup_script_exposes_consent_gated_local_network_subcommand() -> None:
    setup = load_setup_module()
    choices = setup.parser()._subparsers._group_actions[0].choices
    assert "local-network" in choices
    assert "serve" not in choices
    check_args = setup.parser().parse_args(["local-network"])
    assert check_args.command == "local-network"
    assert check_args.apply is False
    apply_args = setup.parser().parse_args(["local-network", "--apply"])
    assert apply_args.apply is True


def test_setup_script_local_network_report_routes_check_and_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = load_setup_module()
    calls: list[dict[str, object]] = []

    def check_policy(**kwargs: object) -> dict[str, object]:
        calls.append({"kind": "check", **kwargs})
        return {"schema": "codex.chatgpt.chrome-local-network/v1", "supported": True, "enabled": False}

    def enable_policy(**kwargs: object) -> dict[str, object]:
        calls.append({"kind": "enable", **kwargs})
        return {
            "schema": "codex.chatgpt.chrome-local-network/v1",
            "supported": True,
            "enabled": True,
            "changed": True,
            "receipt": str(tmp_path / "state" / "receipt.json"),
        }

    monkeypatch.setattr(
        setup,
        "_CHROME_LOCAL_NETWORK_MODULE",
        SimpleNamespace(check_policy=check_policy, enable_policy=enable_policy),
    )
    assert setup.chrome_local_network_report(apply=False)["enabled"] is False
    assert setup.chrome_local_network_report(apply=True, codex_home=tmp_path)["changed"] is True
    assert [call["kind"] for call in calls] == ["check", "enable"]
    assert calls[1]["codex_home"] == tmp_path


def test_setup_script_local_network_report_relays_permission_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = load_setup_module()

    def denied(**kwargs: object) -> dict[str, object]:
        raise PermissionError("denied")

    monkeypatch.setattr(
        setup,
        "_CHROME_LOCAL_NETWORK_MODULE",
        SimpleNamespace(check_policy=lambda **kwargs: {}, enable_policy=denied),
    )
    assert setup.chrome_local_network_report(apply=True) == {
        "ok": False,
        "error": "CHROME_POLICY_WRITE_DENIED",
    }


def test_setup_script_main_dispatches_local_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup = load_setup_module()
    monkeypatch.setattr(
        setup,
        "_CHROME_LOCAL_NETWORK_MODULE",
        SimpleNamespace(
            check_policy=lambda **kwargs: {
                "schema": "codex.chatgpt.chrome-local-network/v1",
                "supported": True,
                "enabled": False,
            },
            enable_policy=lambda **kwargs: {
                "schema": "codex.chatgpt.chrome-local-network/v1",
                "supported": True,
                "enabled": True,
                "changed": True,
                "receipt": "receipt.json",
            },
        ),
    )
    assert setup.main(["local-network"]) == 2
    assert json.loads(capsys.readouterr().out)["enabled"] is False
    assert setup.main(["local-network", "--apply"]) == 0
    assert json.loads(capsys.readouterr().out)["enabled"] is True
