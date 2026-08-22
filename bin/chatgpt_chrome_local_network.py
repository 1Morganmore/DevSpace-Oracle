from __future__ import annotations

"""Persist and verify the narrow Chrome Local Network Access grant used by DevSpace.

ChatGPT's DevSpace connector calls a local MCP endpoint, so the browser needs
a Local Network Access grant for the exact chatgpt.com origin.  On Windows the
per-user enterprise policy key is writable without admin rights; this module
appends only that one exact origin and never replaces unrelated policy
entries.  The action is always explicit and consent-gated: the workspace setup
helper exposes it as a separate `local-network` command, nothing runs it
automatically, and this module never touches ChatGPT app registration or
settings.
"""

import argparse
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence


CHATGPT_ORIGIN = "https://chatgpt.com"
SEED_CONTENT_SETTING_KEY = "https://chatgpt.com:443,*"
POLICY_SUBKEY = r"Software\Policies\Google\Chrome\LocalNetworkAccessAllowedForUrls"
STATUS_SCHEMA = "codex.chatgpt.chrome-local-network/v1"
RECEIPT_SCHEMA = "codex.chatgpt.chrome-local-network-receipt/v1"
ERROR_POLICY_NOT_DURABLE = "CHATGPT_LOCAL_NETWORK_POLICY_NOT_DURABLE"
ERROR_WRITE_DENIED = "CHROME_POLICY_WRITE_DENIED"


def _normalized(value: object) -> str:
    return str(value).strip().rstrip("/").casefold()


def policy_contains_origin(values: Mapping[str, object], origin: str = CHATGPT_ORIGIN) -> bool:
    expected = _normalized(origin)
    return any(_normalized(value) == expected for value in values.values())


def next_policy_value_name(values: Mapping[str, object]) -> str:
    used = {int(name) for name in values if str(name).isdigit() and int(name) > 0}
    candidate = 1
    while candidate in used:
        candidate += 1
    return str(candidate)


def _resolve_registry(registry: Any | None = None) -> Any | None:
    if registry is not None:
        return registry
    if sys.platform != "win32":
        return None
    import winreg

    return winreg


def _read_windows_policy(registry: Any) -> dict[str, str]:
    try:
        key = registry.OpenKey(registry.HKEY_CURRENT_USER, POLICY_SUBKEY, 0, registry.KEY_READ)
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    with key:
        index = 0
        while True:
            try:
                name, value, _kind = registry.EnumValue(key, index)
            except OSError:
                break
            values[str(name)] = str(value)
            index += 1
    return values


def _write_policy_value(registry: Any, value_name: str) -> None:
    with registry.CreateKeyEx(
        registry.HKEY_CURRENT_USER,
        POLICY_SUBKEY,
        0,
        registry.KEY_READ | registry.KEY_WRITE,
    ) as key:
        registry.SetValueEx(key, value_name, 0, registry.REG_SZ, CHATGPT_ORIGIN)


def policy_status(*, registry: Any | None = None, platform_name: str | None = None) -> dict[str, Any]:
    if (platform_name or sys.platform) != "win32":
        return {
            "schema": STATUS_SCHEMA,
            "supported": False,
            "enabled": False,
            "origin": CHATGPT_ORIGIN,
            "reason": "WINDOWS_CHROME_POLICY_ONLY",
        }
    values = _read_windows_policy(_resolve_registry(registry))
    return {
        "schema": STATUS_SCHEMA,
        "supported": True,
        "enabled": policy_contains_origin(values),
        "origin": CHATGPT_ORIGIN,
        "policy_subkey": POLICY_SUBKEY,
        "matching_value_names": [
            name for name, value in values.items() if _normalized(value) == _normalized(CHATGPT_ORIGIN)
        ],
        "entry_count": len(values),
    }


def seed_profile_dir(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    profile_override = str(values.get("ORACLE_BROWSER_PROFILE_DIR") or "").strip()
    if profile_override:
        return Path(profile_override).expanduser().resolve()
    return (Path.home() / ".oracle" / "browser-profile").resolve()


def _content_setting_entry(preferences: Any) -> Any | None:
    if not isinstance(preferences, dict):
        return None
    content_settings = preferences.get("content_settings")
    if not isinstance(content_settings, dict):
        return None
    exceptions = content_settings.get("exceptions")
    if not isinstance(exceptions, dict):
        return None
    local_network = exceptions.get("local_network")
    if not isinstance(local_network, dict):
        return None
    entry = local_network.get(SEED_CONTENT_SETTING_KEY)
    if isinstance(entry, dict) and entry.get("setting") == 1:
        return entry
    return None


def seed_grant_status(
    *,
    profile_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    base = (profile_dir if profile_dir is not None else seed_profile_dir(env=env)).expanduser().resolve()
    preferences = base / "Default" / "Preferences"
    granted = False
    reason = "SEED_PREFERENCES_MISSING"
    try:
        raw = preferences.read_text(encoding="utf-8")
    except OSError:
        reason = "SEED_PREFERENCES_MISSING"
    else:
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            reason = "SEED_PREFERENCES_INVALID"
        else:
            if _content_setting_entry(parsed) is not None:
                granted = True
                reason = "SEED_GRANT_PRESENT"
            else:
                reason = "SEED_GRANT_ABSENT"
    return {
        "granted": granted,
        "profile_dir": str(base),
        "preferences_path": str(preferences),
        "reason": reason,
    }


def check_policy(
    *,
    registry: Any | None = None,
    platform_name: str | None = None,
    profile_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    policy = policy_status(registry=registry, platform_name=platform_name)
    seed = seed_grant_status(profile_dir=profile_dir, env=env)
    return {
        "schema": STATUS_SCHEMA,
        "supported": policy["supported"],
        "enabled": bool(policy["enabled"]) or bool(seed["granted"]),
        "origin": CHATGPT_ORIGIN,
        "policy": policy,
        "seed_profile": seed,
    }


def _receipt_path(*, codex_home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    root = (codex_home or Path(values.get("CODEX_HOME") or (Path.home() / ".codex"))).expanduser().resolve()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    return root / "state" / f"chatgpt-local-network-policy-{stamp}.json"


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def enable_policy(
    *,
    codex_home: Path | None = None,
    registry: Any | None = None,
    platform_name: str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    status = policy_status(registry=registry, platform_name=platform_name)
    if not status["supported"]:
        return {**status, "changed": False, "receipt": None}
    winreg_module = _resolve_registry(registry)
    before = _read_windows_policy(winreg_module)
    changed = not policy_contains_origin(before)
    value_name: str | None = None
    if changed:
        value_name = next_policy_value_name(before)
        _write_policy_value(winreg_module, value_name)
    after = _read_windows_policy(winreg_module)
    if not policy_contains_origin(after):
        raise RuntimeError(ERROR_POLICY_NOT_DURABLE)
    receipt_path = _receipt_path(codex_home=codex_home, env=env)
    payload = {
        "schema": RECEIPT_SCHEMA,
        "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "origin": CHATGPT_ORIGIN,
        "policy_subkey": POLICY_SUBKEY,
        "changed": changed,
        "created_value_name": value_name,
        "preserved_entry_count": len(before),
        "enabled": True,
    }
    _write_json_atomic(receipt_path, payload)
    return {
        **policy_status(registry=registry, platform_name=platform_name),
        "changed": changed,
        "created_value_name": value_name,
        "preserved_entry_count": len(before),
        "receipt": str(receipt_path),
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Manage the exact chatgpt.com Chrome Local Network Access grant used by DevSpace."
    )
    sub = value.add_subparsers(dest="command", required=True)
    enable = sub.add_parser("enable", help="Write the exact chatgpt.com origin into the per-user Chrome policy")
    enable.add_argument("--codex-home", type=Path, help=argparse.SUPPRESS)
    sub.add_parser(
        "check",
        help="Fail-closed check: Chrome policy grant or signed-in seed profile grant",
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "enable":
        try:
            result = enable_policy(codex_home=args.codex_home)
        except PermissionError:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": ERROR_WRITE_DENIED,
                        "next_action": (
                            "Grant Local network once in the dedicated Oracle browser profile, "
                            "then fully exit Chrome."
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        except RuntimeError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if (result.get("enabled") or not result.get("supported")) else 3
    result = check_policy()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("enabled") else 3


if __name__ == "__main__":
    raise SystemExit(main())
