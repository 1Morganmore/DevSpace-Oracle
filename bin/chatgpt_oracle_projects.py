from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "codex.chatgpt.oracle-projects/v1"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
BIN = Path(__file__).resolve().parent


def _load_state():
    path = BIN / "chatgpt_oracle_state.py"
    spec = importlib.util.spec_from_file_location("chatgpt_oracle_projects_state", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STATE = _load_state()


def default_store() -> Path:
    return Path.home() / ".codex" / "config" / "chatgpt-oracle-projects.json"


def normalize_name(value: Any) -> str:
    name = str(value or "").strip().casefold()
    if NAME_RE.fullmatch(name) is None:
        raise ValueError("project profile name must match [a-z0-9][a-z0-9_-]{0,63}")
    return name


def load_profiles(store: Path | None = None) -> dict[str, str]:
    path = (store or default_store()).expanduser()
    if not path.exists():
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"project profile store must be a regular non-symlink file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError(f"project profile store schema must be {SCHEMA}")
    raw_profiles = value.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise ValueError("project profile store profiles must be an object")
    profiles: dict[str, str] = {}
    for raw_name, raw_url in raw_profiles.items():
        name = normalize_name(raw_name)
        if name != raw_name:
            raise ValueError(f"project profile name is not canonical: {raw_name}")
        url = STATE.normalize_chatgpt_project_url(raw_url)
        if url is None:
            raise ValueError(f"project profile URL is missing: {name}")
        profiles[name] = url
    return profiles


def save_profiles(profiles: dict[str, str], store: Path | None = None) -> Path:
    path = (store or default_store()).expanduser()
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"project profile store must be a regular non-symlink file: {path}")
    normalized = {
        normalize_name(name): STATE.normalize_chatgpt_project_url(url)
        for name, url in profiles.items()
    }
    if any(url is None for url in normalized.values()):
        raise ValueError("project profile URL cannot be empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps({"schema": SCHEMA, "profiles": normalized}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return path


def resolve_profile(name: str, store: Path | None = None) -> str:
    canonical = normalize_name(name)
    try:
        return load_profiles(store)[canonical]
    except KeyError as exc:
        raise ValueError(f"unknown ChatGPT Project profile: {canonical}") from exc


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage exact ChatGPT Project URL profiles.")
    parser.add_argument("--store", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    set_parser = commands.add_parser("set")
    set_parser.add_argument("name")
    set_parser.add_argument("url")
    get_parser = commands.add_parser("get")
    get_parser.add_argument("name")
    commands.add_parser("list")
    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("name")
    args = parser.parse_args(argv)
    try:
        profiles = load_profiles(args.store)
        if args.command == "set":
            name = normalize_name(args.name)
            url = STATE.normalize_chatgpt_project_url(args.url)
            if url is None:
                raise ValueError("ChatGPT Project URL cannot be empty")
            profiles[name] = url
            store = save_profiles(profiles, args.store)
            result = {"ok": True, "store": str(store), "profile": {"name": name, "url": url}}
        elif args.command == "get":
            name = normalize_name(args.name)
            result = {"ok": True, "profile": {"name": name, "url": resolve_profile(name, args.store)}}
        elif args.command == "remove":
            name = normalize_name(args.name)
            if name not in profiles:
                raise ValueError(f"unknown ChatGPT Project profile: {name}")
            del profiles[name]
            store = save_profiles(profiles, args.store)
            result = {"ok": True, "store": str(store), "removed": name}
        else:
            result = {"ok": True, "profiles": profiles}
    except Exception as exc:
        result = {"ok": False, "error": {"code": "ORACLE_PROJECT_PROFILE_FAILED", "message": str(exc)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
