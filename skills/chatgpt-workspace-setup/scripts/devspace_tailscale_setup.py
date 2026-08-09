from __future__ import annotations

"""Explicit DevSpace/Tailscale setup and read-only endpoint diagnostics.

This module deliberately contains no ChatGPT UI or browser automation.  It is a
one-time local setup helper; normal GPT execution consumes only its printed MCP
URL and must not invoke it.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_PORT = 7676
APP_NAME = "DevSpace"
AUTOSTART_NAME = "DevSpace MCP Server"
AUTOSTART_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
DEVSPACE_PACKAGE = "@waishnav/devspace@1.0.6"
SECRET_PATTERN = re.compile(r"(?i)(password|token|secret|authorization)\s*([:=])\s*[^\s,;]+")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*\.ts\.net$", re.IGNORECASE)


class SetupError(ValueError):
    pass


@dataclass(frozen=True)
class SetupConfig:
    roots: tuple[Path, ...]
    hostname: str
    local_port: int = DEFAULT_PORT
    public_port: int = 443

    @property
    def public_origin(self) -> str:
        suffix = "" if self.public_port == 443 else f":{self.public_port}"
        return f"https://{self.hostname}{suffix}"

    @property
    def registration_url(self) -> str:
        return f"{self.public_origin}/mcp"

    @property
    def local_mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}/mcp"

    @property
    def local_health_url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}/healthz"

    @property
    def public_health_url(self) -> str:
        suffix = "" if self.public_port == 443 else f":{self.public_port}"
        return f"https://{self.hostname}{suffix}/healthz"


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def _is_volume_root(path: Path) -> bool:
    anchor = Path(path.anchor)
    return bool(path.anchor) and path == anchor


def validate_roots(roots: Sequence[str]) -> tuple[Path, ...]:
    if not roots:
        raise SetupError("ALLOWED_ROOT_REQUIRED")
    resolved: list[Path] = []
    for raw_root in roots:
        path = Path(raw_root)
        if not path.is_absolute():
            raise SetupError("ALLOWED_ROOT_ABSOLUTE_REQUIRED")
        if not path.is_dir():
            raise SetupError("ALLOWED_ROOT_NOT_DIRECTORY")
        path = path.resolve()
        if _is_volume_root(path):
            raise SetupError("ALLOWED_ROOT_TOO_BROAD")
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved)


def validate_config(
    roots: Sequence[str],
    hostname: str,
    local_port: int = DEFAULT_PORT,
    public_port: int = 443,
) -> SetupConfig:
    resolved = validate_roots(roots)
    hostname = hostname.strip().lower().rstrip(".")
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        raise SetupError("TAILSCALE_HOSTNAME_REQUIRED")
    if not 1 <= local_port <= 65535:
        raise SetupError("LOCAL_PORT_INVALID")
    if public_port not in {443, 8443, 10000}:
        raise SetupError("TAILSCALE_FUNNEL_PORT_INVALID")
    return SetupConfig(resolved, hostname, local_port, public_port)


def devspace_config_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    directory = Path(values.get("DEVSPACE_CONFIG_DIR") or (Path.home() / ".devspace")).expanduser()
    return directory.resolve(strict=False) / "config.json"


def load_devspace_config(path: Path | None = None) -> tuple[Path, dict[str, Any]]:
    target = (path or devspace_config_path()).expanduser()
    if target.is_symlink() or not target.is_file():
        raise SetupError("DEVSPACE_CONFIG_FILE_REQUIRED")
    target = target.resolve(strict=True)
    try:
        value = json.loads(target.read_bytes().decode("utf-8", errors="strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError("DEVSPACE_CONFIG_INVALID") from exc
    if not isinstance(value, dict) or not isinstance(value.get("allowedRoots"), list) or not all(
        isinstance(item, str) for item in value["allowedRoots"]
    ):
        raise SetupError("DEVSPACE_CONFIG_INVALID")
    return target, value


def configure_roots(
    roots: Sequence[str],
    *,
    apply: bool,
    restart: bool = False,
    config_path: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    requested = validate_roots(roots)
    target, current = load_devspace_config(config_path)
    before = [str(Path(item).expanduser().resolve(strict=False)) for item in current["allowedRoots"]]
    after = [str(item) for item in requested]
    changed = before != after
    result: dict[str, Any] = {
        "action": "configure_devspace_roots",
        "config_path": str(target),
        "before": before,
        "after": after,
        "changed": changed,
        "auth_unchanged": True,
        "restart_required": changed and not restart,
        "restart_performed": False,
    }
    if not apply:
        return result
    if changed:
        updated = {**current, "allowedRoots": after}
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    _, readback = load_devspace_config(target)
    if readback.get("allowedRoots") != after:
        raise SetupError("DEVSPACE_ROOTS_READBACK_MISMATCH")
    result["readback"] = after
    if restart:
        local_port = int(readback.get("port", DEFAULT_PORT))
        run_checked(devspace_compat_argv(stop_exact_service=True, local_port=local_port), runner=runner)
        launch_hidden(bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "serve"]), popen_factory=popen_factory)
        run_checked(devspace_compat_argv(confirm_restarted=True, local_port=local_port), runner=runner)
        result["restart_required"] = False
        result["restart_performed"] = True
    return result


def git_bash_path() -> Path:
    candidate = Path(os.environ.get("DEVSPACE_GIT_BASH") or r"C:\Program Files\Git\bin\bash.exe")
    if not candidate.is_file():
        raise SetupError("GIT_BASH_NOT_FOUND")
    return candidate


def windows_subprocess_kwargs(platform_name: str | None = None) -> dict[str, Any]:
    if (platform_name or os.name) != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {"creationflags": creationflags, "startupinfo": startupinfo}


def bash_argv(command: Sequence[str]) -> list[str]:
    return [str(git_bash_path()), "-lc", "exec " + " ".join(shlex.quote(part) for part in command)]


def devspace_compat_argv(
    *,
    confirm_restarted: bool = False,
    stop_exact_service: bool = False,
    local_port: int = DEFAULT_PORT,
) -> list[str]:
    script = Path(__file__).resolve().parents[3] / "bin" / "chatgpt_devspace_compat.py"
    if not script.is_file():
        raise SetupError("DEVSPACE_COMPAT_MODULE_MISSING")
    argv = [sys.executable, str(script)]
    if confirm_restarted:
        argv.append("--confirm-service-restarted")
    if stop_exact_service:
        argv.append("--stop-exact-service")
    if local_port != DEFAULT_PORT:
        argv.extend(["--local-port", str(local_port)])
    return argv


def setup_plan(config: SetupConfig) -> dict[str, Any]:
    return {
        "action": "explicit_setup_only",
        "allowed_roots": [str(root) for root in config.roots],
        "devspace_init": bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "init"]),
        "devspace_serve": bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "serve"]),
        "tailscale_funnel": [
            "tailscale",
            "funnel",
            "--bg",
            f"--https={config.public_port}",
            f"http://127.0.0.1:{config.local_port}",
        ],
        "login_autostart": autostart_argv(),
        "public_origin_for_devspace_init": config.public_origin,
        "recommended_app_name": APP_NAME,
        "registration_url": config.registration_url,
        "requires_developer_mode": True,
        "requires_owner_approval": True,
    }


def run_checked(
    argv: Sequence[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    interactive: bool = False,
) -> None:
    kwargs = {} if interactive else windows_subprocess_kwargs()
    runner(list(argv), check=True, text=True, **kwargs)


def launch_hidden(argv: Sequence[str], *, popen_factory: Callable[..., Any] = subprocess.Popen) -> Any:
    return popen_factory(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **windows_subprocess_kwargs(),
    )


def autostart_argv() -> list[str]:
    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    command = subprocess.list2cmdline([pythonw, str(Path(__file__).resolve()), "serve"])
    return [
        "reg.exe",
        "add",
        AUTOSTART_REG_KEY,
        "/v",
        AUTOSTART_NAME,
        "/t",
        "REG_SZ",
        "/d",
        command,
        "/f",
    ]


def serve_foreground(*, runner: Callable[..., Any] = subprocess.run) -> None:
    run_checked(devspace_compat_argv(), runner=runner)
    run_checked(
        bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "serve"]),
        runner=runner,
    )


def apply_setup(
    config: SetupConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> None:
    # Init remains DevSpace's own interactive prompt so it can safely retain its
    # Owner credential.  The root list/public origin are displayed before this call.
    slot = funnel_status(config, runner=runner, allow_absent=True)
    if slot.get("mapping") == "conflict":
        raise SetupError("TAILSCALE_FUNNEL_PORT_IN_USE")
    run_checked(
        bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "init"]),
        runner=runner,
        interactive=True,
    )
    run_checked(devspace_compat_argv(), runner=runner)
    run_checked(
        devspace_compat_argv(stop_exact_service=True, local_port=config.local_port),
        runner=runner,
    )
    launch_hidden(
        bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "serve"]),
        popen_factory=popen_factory,
    )
    run_checked(
        devspace_compat_argv(confirm_restarted=True, local_port=config.local_port),
        runner=runner,
    )
    run_checked(
        ["tailscale", "funnel", "--bg", f"--https={config.public_port}", f"http://127.0.0.1:{config.local_port}"],
        runner=runner,
    )
    run_checked(autostart_argv(), runner=runner)


def http_probe(url: str, *, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json, text/plain;q=0.8"})
    try:
        with opener(request, timeout=5) as response:
            return {"ok": response.status in {200, 401, 403, 405, 406}, "status": response.status, "url": url}
    except urllib.error.HTTPError as error:
        return {"ok": error.code in {401, 403, 405, 406}, "status": error.code, "url": url}
    except OSError as error:
        return {"ok": False, "error": type(error).__name__, "url": url}


def devspace_health_probe(
    url: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=5) as response:
            raw = response.read(4097)
            if response.status != 200 or len(raw) > 4096:
                return {"ok": False, "status": response.status, "url": url}
    except urllib.error.HTTPError as error:
        return {"ok": False, "status": error.code, "url": url}
    except OSError as error:
        return {"ok": False, "error": type(error).__name__, "url": url}
    try:
        payload = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "error": "DEVSPACE_HEALTH_IDENTITY_INVALID", "url": url}
    exact = isinstance(payload, dict) and payload.get("ok") is True and payload.get("name") == "devspace"
    return {
        "ok": exact,
        "status": 200,
        "url": url,
        "identity": payload if exact else None,
        **({} if exact else {"error": "DEVSPACE_HEALTH_IDENTITY_INVALID"}),
    }


def funnel_status(
    config: SetupConfig | None = None,
    *,
    runner: Callable[..., Any] = subprocess.run,
    allow_absent: bool = False,
) -> dict[str, Any]:
    try:
        result = runner(
            ["tailscale", "funnel", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            **windows_subprocess_kwargs(),
        )
    except OSError as error:
        return {"ok": False, "error": type(error).__name__}
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode, "stderr": redact(result.stderr or "")}
    try:
        status = json.loads(result.stdout)
        if config is not None:
            web = status.get("Web") if isinstance(status, dict) else {}
            key = f"{config.hostname}:{config.public_port}"
            entry = web.get(key) if isinstance(web, dict) else None
            if entry is None:
                return {
                    "ok": bool(allow_absent),
                    "mapping": "absent",
                    "error": None if allow_absent else "TAILSCALE_FUNNEL_MAPPING_MISSING",
                }
            flattened = json.dumps(entry, ensure_ascii=False).casefold()
            if str(config.local_port) not in flattened:
                return {"ok": False, "mapping": "conflict", "error": "TAILSCALE_FUNNEL_MAPPING_MISMATCH"}
            return {"ok": True, "mapping": "match", "status": entry}
        return {"ok": True, "status": status}
    except json.JSONDecodeError:
        return {"ok": False, "error": "TAILSCALE_STATUS_JSON_INVALID"}


def doctor(config: SetupConfig, *, opener: Callable[..., Any] = urllib.request.urlopen, runner: Callable[..., Any] = subprocess.run, chatgpt_call_failed: bool = False) -> dict[str, Any]:
    local = devspace_health_probe(config.local_health_url, opener=opener)
    if not local.get("ok"):
        return {
            "local": local,
            "registration_url": config.registration_url,
            "recommended_app_name": APP_NAME,
            "next_action": "CHECK_DEVSPACE_LOCAL_SERVICE",
        }
    funnel = funnel_status(config, runner=runner)
    if not funnel.get("ok"):
        return {
            "local": local,
            "funnel": funnel,
            "registration_url": config.registration_url,
            "recommended_app_name": APP_NAME,
            "next_action": "CHECK_TAILSCALE_FUNNEL",
        }
    public = devspace_health_probe(config.public_health_url, opener=opener)
    report: dict[str, Any] = {
        "local": local,
        "funnel": funnel,
        "public": public,
        "registration_url": config.registration_url,
        "recommended_app_name": APP_NAME,
    }
    if public.get("ok") and chatgpt_call_failed:
        report["next_action"] = "MANUAL_CHATGPT_REGISTRATION_CHECK"
        report["message"] = "Public endpoint is healthy. Re-enter this URL manually in ChatGPT Developer Mode; do not automate re-registration."
    elif not public.get("ok"):
        report["next_action"] = "CHECK_PUBLIC_FUNNEL_ENDPOINT"
    else:
        report["next_action"] = "READY"
    return report


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    roots = sub.add_parser("roots")
    roots.add_argument("--root", action="append", default=[], help="Replacement allowed root; repeat as needed")
    roots.add_argument("--dry-run", action="store_true")
    roots.add_argument("--apply", action="store_true")
    roots.add_argument("--restart", action="store_true", help="Restart the exact DevSpace service after applying")
    for name in ("setup", "doctor"):
        command = sub.add_parser(name)
        command.add_argument("--root", action="append", default=[], help="Narrow allowed DevSpace root; repeat as needed")
        command.add_argument("--hostname", required=True, help="Tailscale MagicDNS hostname")
        command.add_argument("--local-port", type=int, default=DEFAULT_PORT)
        command.add_argument("--public-port", type=int, default=443)
    setup = sub.choices["setup"]
    setup.add_argument("--dry-run", action="store_true")
    setup.add_argument("--apply", action="store_true")
    sub.choices["doctor"].add_argument("--chatgpt-call-failed", action="store_true")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            serve_foreground()
            return 0
        if args.command == "roots":
            if not args.root:
                if args.dry_run or args.apply or args.restart:
                    raise SetupError("ALLOWED_ROOT_REQUIRED")
                target, config = load_devspace_config()
                print(json.dumps({"config_path": str(target), "allowed_roots": config["allowedRoots"]}, ensure_ascii=False, indent=2))
                return 0
            if args.dry_run == args.apply:
                raise SetupError("CHOOSE_EXACTLY_ONE_OF_DRY_RUN_OR_APPLY")
            if args.restart and not args.apply:
                raise SetupError("RESTART_REQUIRES_APPLY")
            print(json.dumps(configure_roots(args.root, apply=args.apply, restart=args.restart), ensure_ascii=False, indent=2))
            return 0
        config = validate_config(args.root, args.hostname, args.local_port, args.public_port)
        if args.command == "setup":
            if args.dry_run == args.apply:
                raise SetupError("CHOOSE_EXACTLY_ONE_OF_DRY_RUN_OR_APPLY")
            plan = setup_plan(config)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            if args.apply:
                apply_setup(config)
            return 0
        print(json.dumps(doctor(config, chatgpt_call_failed=args.chatgpt_call_failed), ensure_ascii=False, indent=2))
        return 0
    except SetupError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
