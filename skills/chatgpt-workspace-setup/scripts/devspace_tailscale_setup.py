from __future__ import annotations

"""Explicit DevSpace/Tailscale setup, login watchdog, and diagnostics.

This module deliberately contains no ChatGPT UI or browser automation. Normal
GPT execution consumes only its printed MCP URL and must not invoke it.
The optional ``local-network`` subcommand manages the narrow Chrome Local
Network Access grant for the ChatGPT origin behind explicit consent; it never
runs automatically and never touches ChatGPT app registration or settings.
"""

import argparse
import importlib.util
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping, Sequence


DEFAULT_PORT = 7676
APP_NAME = "DevSpace"
AUTOSTART_NAME = "DevSpace MCP Server"
AUTOSTART_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
DEVSPACE_VERSION = "1.0.7"
DEVSPACE_PACKAGE = f"@waishnav/devspace@{DEVSPACE_VERSION}"
DEVSPACE_OAUTH_SCOPES = "devspace,offline_access"
DEVSPACE_COMPAT_PATH = Path(__file__).resolve().parents[3] / "bin" / "chatgpt_devspace_compat.py"
CHROME_LOCAL_NETWORK_PATH = Path(__file__).resolve().parents[3] / "bin" / "chatgpt_chrome_local_network.py"
SECRET_PATTERN = re.compile(r"(?i)(password|token|secret|authorization)\s*([:=])\s*[^\s,;]+")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*\.ts\.net$", re.IGNORECASE)
RESULT_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$")
LOCAL_HEALTH_ATTEMPTS = 15
LOCAL_HEALTH_INTERVAL_SECONDS = 1
LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS = 1
WATCHDOG_INTERVAL_SECONDS = 300.0
WATCHDOG_MUTEX_NAME = r"Local\DevSpaceOracle.DevSpaceMcpServerWatchdog"
WATCHDOG_STATUS_SCHEMA = "codex.devspace-watchdog-status/v1"
WATCHDOG_STATUS_RELATIVE_PATH = Path("state") / "devspace-watchdog" / "status.json"
WINDOWS_ERROR_ALREADY_EXISTS = 183


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


def load_watchdog_config(path: Path | None = None) -> tuple[Path, SetupConfig]:
    target, persisted = load_devspace_config(path)
    local_host = persisted.get("host", "127.0.0.1")
    local_port = persisted.get("port", DEFAULT_PORT)
    public_base_url = persisted.get("publicBaseUrl")
    if local_host != "127.0.0.1":
        raise SetupError("DEVSPACE_WATCHDOG_LOCAL_HOST_INVALID")
    if isinstance(local_port, bool) or not isinstance(local_port, int):
        raise SetupError("DEVSPACE_WATCHDOG_LOCAL_PORT_INVALID")
    if not isinstance(public_base_url, str) or public_base_url != public_base_url.strip():
        raise SetupError("DEVSPACE_WATCHDOG_PUBLIC_BASE_URL_INVALID")
    try:
        parsed = urllib.parse.urlsplit(public_base_url)
        public_port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:
        raise SetupError("DEVSPACE_WATCHDOG_PUBLIC_BASE_URL_INVALID") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise SetupError("DEVSPACE_WATCHDOG_PUBLIC_BASE_URL_INVALID")
    config = validate_config(
        persisted["allowedRoots"],
        parsed.hostname,
        local_port,
        public_port,
    )
    return target, config


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
        run_checked(devspace_prepare_argv(), runner=runner)
        run_checked(devspace_compat_argv(stop_exact_service=True, local_port=local_port), runner=runner)
        launch_hidden(devspace_serve_argv(), popen_factory=popen_factory)
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


def devspace_prepare_argv() -> list[str]:
    return bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "--help"])


def devspace_serve_argv() -> list[str]:
    return bash_argv([
        "env",
        f"DEVSPACE_OAUTH_SCOPES={DEVSPACE_OAUTH_SCOPES}",
        "npx",
        "--yes",
        DEVSPACE_PACKAGE,
        "serve",
    ])


def tailscale_funnel_argv(config: SetupConfig) -> list[str]:
    return [
        "tailscale",
        "funnel",
        "--bg",
        f"--https={config.public_port}",
        f"http://127.0.0.1:{config.local_port}",
    ]


def tailscale_funnel_off_argv(config: SetupConfig) -> list[str]:
    return [*tailscale_funnel_argv(config), "off"]


def devspace_compat_argv(
    *,
    confirm_restarted: bool = False,
    stop_exact_service: bool = False,
    local_port: int = DEFAULT_PORT,
) -> list[str]:
    if not DEVSPACE_COMPAT_PATH.is_file():
        raise SetupError("DEVSPACE_COMPAT_MODULE_MISSING")
    argv = [sys.executable, str(DEVSPACE_COMPAT_PATH)]
    if confirm_restarted:
        argv.append("--confirm-service-restarted")
    if stop_exact_service:
        argv.append("--stop-exact-service")
    if local_port != DEFAULT_PORT:
        argv.extend(["--local-port", str(local_port)])
    return argv


_DEVSPACE_COMPAT_MODULE: ModuleType | None = None


def load_devspace_compat_module() -> ModuleType:
    global _DEVSPACE_COMPAT_MODULE
    if _DEVSPACE_COMPAT_MODULE is not None:
        return _DEVSPACE_COMPAT_MODULE
    if not DEVSPACE_COMPAT_PATH.is_file():
        raise SetupError("DEVSPACE_COMPAT_MODULE_MISSING")
    spec = importlib.util.spec_from_file_location(
        "chatgpt_devspace_compat_watchdog_runtime",
        DEVSPACE_COMPAT_PATH,
    )
    if spec is None or spec.loader is None:
        raise SetupError("DEVSPACE_COMPAT_MODULE_MISSING")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(spec.name, None)
        raise SetupError("DEVSPACE_COMPAT_MODULE_INVALID") from error
    _DEVSPACE_COMPAT_MODULE = module
    return module


_CHROME_LOCAL_NETWORK_MODULE: ModuleType | None = None


def load_chrome_local_network_module() -> ModuleType:
    global _CHROME_LOCAL_NETWORK_MODULE
    if _CHROME_LOCAL_NETWORK_MODULE is not None:
        return _CHROME_LOCAL_NETWORK_MODULE
    if not CHROME_LOCAL_NETWORK_PATH.is_file():
        raise SetupError("CHROME_LOCAL_NETWORK_MODULE_MISSING")
    spec = importlib.util.spec_from_file_location(
        "chatgpt_chrome_local_network_setup_runtime",
        CHROME_LOCAL_NETWORK_PATH,
    )
    if spec is None or spec.loader is None:
        raise SetupError("CHROME_LOCAL_NETWORK_MODULE_MISSING")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(spec.name, None)
        raise SetupError("CHROME_LOCAL_NETWORK_MODULE_INVALID") from error
    _CHROME_LOCAL_NETWORK_MODULE = module
    return module


def chrome_local_network_report(
    *,
    apply: bool = False,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Explicit consent-gated Chrome Local Network grant (never automatic)."""
    module = load_chrome_local_network_module()
    try:
        if apply:
            return module.enable_policy(codex_home=codex_home)
        return module.check_policy()
    except PermissionError:
        return {"ok": False, "error": "CHROME_POLICY_WRITE_DENIED"}
    except RuntimeError as error:
        return {"ok": False, "error": str(error)}


def _result_code_from_exception(error: BaseException, fallback: str) -> str:
    candidate = str(getattr(error, "code", "") or "").strip()
    if RESULT_CODE_PATTERN.fullmatch(candidate):
        return candidate
    candidate = str(error).partition(":")[0].strip()
    if RESULT_CODE_PATTERN.fullmatch(candidate):
        return candidate
    return fallback


def inspect_exact_devspace_service(
    config: SetupConfig,
    *,
    inspector: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if inspector is None:
        inspector = load_devspace_compat_module().inspect_devspace_compatibility
    try:
        inspection = inspector(local_port=config.local_port)
    except Exception as error:
        return {
            "ok": False,
            "identity_status": "unverified",
            "error": _result_code_from_exception(
                error,
                "DEVSPACE_SERVICE_PROBE_FAILED",
            ),
        }
    if not isinstance(inspection, Mapping) or inspection.get("ok") is not True:
        error = inspection.get("error") if isinstance(inspection, Mapping) else None
        code = error.get("code") if isinstance(error, Mapping) else None
        return {
            "ok": False,
            "identity_status": "unverified",
            "error": (
                code
                if isinstance(code, str) and RESULT_CODE_PATTERN.fullmatch(code)
                else "DEVSPACE_SERVICE_PROBE_INVALID"
            ),
        }

    identity_status = str(inspection.get("service_status") or "unverified")
    if identity_status == "absent":
        return {
            "ok": False,
            "identity_status": "absent",
            "error": "DEVSPACE_SERVICE_NOT_LISTENING",
        }
    if identity_status != "match":
        return {
            "ok": False,
            "identity_status": "mismatch" if identity_status == "mismatch" else "unverified",
            "error": (
                "DEVSPACE_SERVICE_IDENTITY_MISMATCH"
                if identity_status == "mismatch"
                else "DEVSPACE_SERVICE_PROBE_INVALID"
            ),
        }

    identity = inspection.get("service_identity")
    if not isinstance(identity, Mapping):
        return {
            "ok": False,
            "identity_status": "unverified",
            "error": "DEVSPACE_SERVICE_IDENTITY_INVALID",
        }
    pid = identity.get("pid")
    started_at = identity.get("started_at_unix_ns")
    local_port = identity.get("local_port")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid < 1
        or isinstance(started_at, bool)
        or not isinstance(started_at, int)
        or started_at < 1
        or isinstance(local_port, bool)
        or not isinstance(local_port, int)
        or local_port != config.local_port
    ):
        return {
            "ok": False,
            "identity_status": "unverified",
            "error": "DEVSPACE_SERVICE_IDENTITY_INVALID",
        }
    safe_identity = {
        "pid": pid,
        "started_at_unix_ns": started_at,
        "local_port": local_port,
        "version": DEVSPACE_VERSION,
    }
    if inspection.get("ready") is not True or inspection.get("version") != DEVSPACE_VERSION:
        return {
            "ok": False,
            "identity_status": "verified",
            "identity": safe_identity,
            "error": "DEVSPACE_COMPATIBILITY_NOT_READY",
        }
    return {
        "ok": True,
        "identity_status": "verified",
        "identity": safe_identity,
    }


def watchdog_status_path(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    home = Path(values.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    return home.resolve(strict=False) / WATCHDOG_STATUS_RELATIVE_PATH


def _set_restrictive_watchdog_permissions(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    domain = str(os.environ.get("USERDOMAIN") or "").strip()
    username = str(os.environ.get("USERNAME") or "").strip()
    if not username:
        raise SetupError("DEVSPACE_WATCHDOG_STATUS_PERMISSIONS_FAILED")
    principal = f"{domain}\\{username}" if domain else username
    grant = f"{principal}:(OI)(CI)(F)" if directory else f"{principal}:(F)"
    completed = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            grant,
        ],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **windows_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        raise SetupError("DEVSPACE_WATCHDOG_STATUS_PERMISSIONS_FAILED")


def write_watchdog_status(
    *,
    ok: bool,
    result_code: str,
    service_identity_status: str,
    funnel_status: str,
    last_verified_service: Mapping[str, Any] | None = None,
    last_verified_funnel: Mapping[str, Any] | None = None,
    path: Path | None = None,
    timestamp_ns: int | None = None,
    permission_setter: Callable[..., None] = _set_restrictive_watchdog_permissions,
) -> Path:
    if not RESULT_CODE_PATTERN.fullmatch(result_code):
        raise SetupError("DEVSPACE_WATCHDOG_RESULT_CODE_INVALID")
    allowed_service_statuses = {
        "absent",
        "unverified",
        "mismatch",
        "compatibility_not_ready",
        "verified",
    }
    allowed_funnel_statuses = {
        "unknown",
        "absent",
        "disabled",
        "conflict",
        "unverified",
        "verified",
    }
    if service_identity_status not in allowed_service_statuses:
        raise SetupError("DEVSPACE_WATCHDOG_SERVICE_STATUS_INVALID")
    if funnel_status not in allowed_funnel_statuses:
        raise SetupError("DEVSPACE_WATCHDOG_FUNNEL_STATUS_INVALID")

    service: dict[str, Any] | None = None
    if last_verified_service is not None:
        service = {
            "pid": int(last_verified_service["pid"]),
            "started_at_unix_ns": int(last_verified_service["started_at_unix_ns"]),
            "local_port": int(last_verified_service["local_port"]),
            "version": DEVSPACE_VERSION,
        }
    funnel: dict[str, Any] | None = None
    if last_verified_funnel is not None:
        hostname = str(last_verified_funnel["hostname"])
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise SetupError("DEVSPACE_WATCHDOG_FUNNEL_IDENTITY_INVALID")
        funnel = {
            "hostname": hostname,
            "public_port": int(last_verified_funnel["public_port"]),
            "local_port": int(last_verified_funnel["local_port"]),
        }
    payload = {
        "schema": WATCHDOG_STATUS_SCHEMA,
        "timestamp_unix_ns": time.time_ns() if timestamp_ns is None else int(timestamp_ns),
        "ok": bool(ok),
        "result_code": result_code,
        "service_identity_status": service_identity_status,
        "funnel_status": funnel_status,
        "last_verified_service": service,
        "last_verified_funnel": funnel,
    }

    target = (path or watchdog_status_path()).expanduser().resolve(strict=False)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    permission_setter(parent, directory=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        permission_setter(temporary, directory=False)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            descriptor = -1
            json.dump(payload, destination, ensure_ascii=False, separators=(",", ":"))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
        permission_setter(target, directory=False)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return target


def setup_plan(config: SetupConfig) -> dict[str, Any]:
    return {
        "action": "explicit_setup_only",
        "allowed_roots": [str(root) for root in config.roots],
        "devspace_init": bash_argv(["npx", "--yes", DEVSPACE_PACKAGE, "init"]),
        "devspace_serve": devspace_serve_argv(),
        "tailscale_funnel": tailscale_funnel_argv(config),
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


def watchdog_launch_argv() -> list[str]:
    pythonw = str(Path(sys.executable).with_name("pythonw.exe"))
    return [pythonw, str(Path(__file__).resolve()), "watchdog"]


def autostart_argv() -> list[str]:
    command = subprocess.list2cmdline(watchdog_launch_argv())
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


def apply_setup(
    config: SetupConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    service_inspector: Callable[..., Mapping[str, Any]] | None = None,
) -> None:
    # Init remains DevSpace's own interactive prompt so it can safely retain its
    # Owner credential.  The root list/public origin are displayed before this call.
    slot = funnel_status(config, runner=runner, allow_absent=True)
    if slot.get("mapping") == "conflict":
        raise SetupError("TAILSCALE_FUNNEL_PORT_IN_USE")
    if not slot.get("ok"):
        raise SetupError(str(slot.get("error") or "TAILSCALE_FUNNEL_STATUS_FAILED"))
    if slot.get("mapping") == "match":
        service = inspect_exact_devspace_service(config, inspector=service_inspector)
        if not service.get("ok"):
            disable_matching_funnel(config, runner=runner, observed=slot)
            raise SetupError(str(service["error"]))

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
        devspace_serve_argv(),
        popen_factory=popen_factory,
    )
    run_checked(
        devspace_compat_argv(confirm_restarted=True, local_port=config.local_port),
        runner=runner,
    )

    slot = funnel_status(config, runner=runner, allow_absent=True)
    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not service.get("ok"):
        disable_matching_funnel(config, runner=runner, observed=slot)
        raise SetupError(str(service["error"]))
    if not slot.get("ok"):
        raise SetupError(str(slot.get("error") or "TAILSCALE_FUNNEL_STATUS_FAILED"))
    if slot.get("mapping") == "absent":
        run_checked(tailscale_funnel_argv(config), runner=runner)
        readback = funnel_status(config, runner=runner)
        if not readback.get("ok") or readback.get("mapping") != "match":
            raise SetupError(str(readback.get("error") or "TAILSCALE_FUNNEL_READBACK_FAILED"))

    run_checked(autostart_argv(), runner=runner)
    launch_hidden(watchdog_launch_argv(), popen_factory=popen_factory)


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
    timeout: float = 5,
) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with opener(request, timeout=timeout) as response:
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
            encoding="utf-8",
            errors="strict",
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
            proxy = None
            if isinstance(entry, dict):
                direct = entry.get("Proxy")
                handlers = entry.get("Handlers")
                if direct is not None and handlers is None:
                    proxy = direct
                elif direct is None and isinstance(handlers, dict) and set(handlers) == {"/"}:
                    root_handler = handlers["/"]
                    if isinstance(root_handler, dict):
                        proxy = root_handler.get("Proxy")
            expected_proxy = f"http://127.0.0.1:{config.local_port}"
            if proxy != expected_proxy:
                return {"ok": False, "mapping": "conflict", "error": "TAILSCALE_FUNNEL_MAPPING_MISMATCH"}
            return {"ok": True, "mapping": "match", "status": entry}
        return {"ok": True, "status": status}
    except json.JSONDecodeError:
        return {"ok": False, "error": "TAILSCALE_STATUS_JSON_INVALID"}


def funnel_identity(config: SetupConfig) -> dict[str, Any]:
    return {
        "hostname": config.hostname,
        "public_port": config.public_port,
        "local_port": config.local_port,
    }


def disable_matching_funnel(
    config: SetupConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    observed: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    slot = dict(observed) if observed is not None else funnel_status(
        config,
        runner=runner,
        allow_absent=True,
    )
    mapping = slot.get("mapping")
    if mapping == "absent":
        return {"ok": True, "status": "absent", "disabled": False}
    if mapping == "conflict":
        return {"ok": True, "status": "conflict", "disabled": False}
    if not slot.get("ok") or mapping != "match":
        return {
            "ok": False,
            "status": "unverified",
            "disabled": False,
            "error": str(slot.get("error") or "TAILSCALE_FUNNEL_STATUS_FAILED"),
        }
    try:
        run_checked(tailscale_funnel_off_argv(config), runner=runner)
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "ok": False,
            "status": "unverified",
            "disabled": False,
            "error": type(error).__name__,
        }
    readback = funnel_status(config, runner=runner, allow_absent=True)
    if not readback.get("ok") or readback.get("mapping") != "absent":
        return {
            "ok": False,
            "status": "unverified",
            "disabled": False,
            "error": str(readback.get("error") or "TAILSCALE_FUNNEL_DISABLE_NOT_PROVEN"),
        }
    return {"ok": True, "status": "disabled", "disabled": True}


def wait_for_exact_local_health(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], Any] | None = None,
) -> tuple[dict[str, Any], int]:
    pause = time.sleep if sleeper is None else sleeper
    local: dict[str, Any] = {"ok": False}
    attempts = 0
    for attempts in range(1, LOCAL_HEALTH_ATTEMPTS + 1):
        local = devspace_health_probe(
            config.local_health_url,
            opener=opener,
            timeout=LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        if local.get("ok"):
            break
        if attempts < LOCAL_HEALTH_ATTEMPTS:
            pause(LOCAL_HEALTH_INTERVAL_SECONDS)
    return local, attempts


def repair_exact_devspace_service(
    config: SetupConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> None:
    run_checked(devspace_prepare_argv(), runner=runner)
    run_checked(devspace_compat_argv(), runner=runner)
    run_checked(
        devspace_compat_argv(stop_exact_service=True, local_port=config.local_port),
        runner=runner,
    )
    launch_hidden(devspace_serve_argv(), popen_factory=popen_factory)
    run_checked(
        devspace_compat_argv(confirm_restarted=True, local_port=config.local_port),
        runner=runner,
    )


def _fail_closed_service_report(
    report: dict[str, Any],
    config: SetupConfig,
    service: Mapping[str, Any],
    *,
    healthy: bool,
    repaired: bool,
    attempts: int,
    runner: Callable[..., Any],
    observed_funnel: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    disabled = disable_matching_funnel(
        config,
        runner=runner,
        observed=observed_funnel,
    )
    identity_status = str(service.get("identity_status") or "unverified")
    report["service"] = {
        "healthy": healthy,
        "repaired": repaired,
        "attempts": attempts,
        "identity_status": identity_status,
        **({"identity": service["identity"]} if service.get("ok") else {}),
        "error": str(service.get("error") or "DEVSPACE_SERVICE_IDENTITY_UNVERIFIED"),
    }
    report["funnel"] = {
        "healthy": False,
        "repaired": False,
        "status": disabled["status"],
        "disabled": bool(disabled.get("disabled")),
        **({} if disabled.get("ok") else {"error": disabled.get("error")}),
    }
    return {
        **report,
        "result_code": str(service.get("error") or "DEVSPACE_SERVICE_IDENTITY_UNVERIFIED"),
        "next_action": "CHECK_DEVSPACE_LOCAL_SERVICE",
    }


def watchdog_cycle(
    *,
    config_path: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    sleeper: Callable[[float], Any] | None = None,
    service_inspector: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    target, config = load_watchdog_config(config_path)
    report: dict[str, Any] = {
        "ok": False,
        "config_path": str(target),
        "config_reloaded": True,
    }
    local = devspace_health_probe(
        config.local_health_url,
        opener=opener,
        timeout=LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS,
    )
    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    service_repaired = False
    local_attempts = 1
    repair_allowed = (
        service.get("identity_status") == "verified"
        or (service.get("identity_status") == "absent" and not local.get("ok"))
    )
    if (not local.get("ok") or not service.get("ok")) and repair_allowed:
        repair_exact_devspace_service(
            config,
            runner=runner,
            popen_factory=popen_factory,
        )
        service_repaired = True
        local, repair_attempts = wait_for_exact_local_health(
            config,
            opener=opener,
            sleeper=sleeper,
        )
        local_attempts += repair_attempts
        service = inspect_exact_devspace_service(config, inspector=service_inspector)

    if not local.get("ok"):
        if service.get("ok"):
            service = {
                **service,
                "ok": False,
                "error": "DEVSPACE_LOCAL_HEALTH_UNVERIFIED",
            }
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=False,
            repaired=service_repaired,
            attempts=local_attempts,
            runner=runner,
        )
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=service_repaired,
            attempts=local_attempts,
            runner=runner,
        )

    report["service"] = {
        "healthy": True,
        "repaired": service_repaired,
        "attempts": local_attempts,
        "identity_status": "verified",
        "identity": service["identity"],
    }
    funnel = funnel_status(config, runner=runner, allow_absent=True)
    if not funnel.get("ok"):
        report["funnel"] = {
            "healthy": False,
            "repaired": False,
            "status": "conflict" if funnel.get("mapping") == "conflict" else "unverified",
            "error": funnel.get("error"),
        }
        return {
            **report,
            "result_code": str(funnel.get("error") or "TAILSCALE_FUNNEL_STATUS_FAILED"),
            "next_action": "CHECK_TAILSCALE_FUNNEL",
        }

    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=service_repaired,
            attempts=local_attempts,
            runner=runner,
            observed_funnel=funnel,
        )

    funnel_repaired = False
    if funnel.get("mapping") == "absent":
        run_checked(tailscale_funnel_argv(config), runner=runner)
        funnel_repaired = True
        funnel = funnel_status(config, runner=runner)
    report["funnel"] = {
        "healthy": bool(funnel.get("ok") and funnel.get("mapping") == "match"),
        "repaired": funnel_repaired,
        "status": (
            "verified"
            if funnel.get("ok") and funnel.get("mapping") == "match"
            else "unverified"
        ),
        **({} if funnel.get("ok") else {"error": funnel.get("error")}),
    }
    if not report["funnel"]["healthy"]:
        return {
            **report,
            "result_code": str(funnel.get("error") or "TAILSCALE_FUNNEL_READBACK_FAILED"),
            "next_action": "CHECK_TAILSCALE_FUNNEL",
        }

    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=service_repaired,
            attempts=local_attempts,
            runner=runner,
            observed_funnel=funnel,
        )
    report["service"]["identity"] = service["identity"]
    report["funnel"]["identity"] = funnel_identity(config)
    return {
        **report,
        "ok": True,
        "result_code": "READY",
        "next_action": "READY",
    }


def acquire_watchdog_mutex() -> Callable[[], None] | None:
    if os.name != "nt":
        raise SetupError("DEVSPACE_WATCHDOG_REQUIRES_WINDOWS")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, WATCHDOG_MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        raise SetupError(f"DEVSPACE_WATCHDOG_MUTEX_CREATE_FAILED:{error}")
    if error == WINDOWS_ERROR_ALREADY_EXISTS:
        close_handle(handle)
        return None

    released = False

    def release() -> None:
        nonlocal released
        if not released:
            close_handle(handle)
            released = True

    return release


def _watchdog_cycle_status(
    cycle: Mapping[str, Any],
    *,
    last_verified_service: Mapping[str, Any] | None,
    last_verified_funnel: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], Mapping[str, Any] | None, Mapping[str, Any] | None]:
    service = cycle.get("service")
    service_status = "unverified"
    if isinstance(service, Mapping):
        service_status = str(service.get("identity_status") or "unverified")
        if service.get("error") == "DEVSPACE_COMPATIBILITY_NOT_READY":
            service_status = "compatibility_not_ready"
        identity = service.get("identity")
        if service_status == "verified" and isinstance(identity, Mapping):
            last_verified_service = identity
    funnel = cycle.get("funnel")
    funnel_status = "unknown"
    if isinstance(funnel, Mapping):
        funnel_status = str(funnel.get("status") or "unverified")
        identity = funnel.get("identity")
        if funnel_status == "verified" and isinstance(identity, Mapping):
            last_verified_funnel = identity

    candidate = str(cycle.get("result_code") or cycle.get("error") or "").partition(":")[0]
    if RESULT_CODE_PATTERN.fullmatch(candidate):
        result_code = candidate
    elif cycle.get("ok"):
        result_code = "READY"
    else:
        result_code = "DEVSPACE_WATCHDOG_CYCLE_FAILED"
    return (
        {
            "ok": bool(cycle.get("ok")),
            "result_code": result_code,
            "service_identity_status": service_status,
            "funnel_status": funnel_status,
            "last_verified_service": last_verified_service,
            "last_verified_funnel": last_verified_funnel,
        },
        last_verified_service,
        last_verified_funnel,
    )


def run_watchdog(
    *,
    interval_seconds: float = WATCHDOG_INTERVAL_SECONDS,
    max_cycles: int | None = None,
    config_path: Path | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    sleeper: Callable[[float], Any] = time.sleep,
    mutex_factory: Callable[[], Callable[[], None] | None] | None = None,
    cycle: Callable[..., dict[str, Any]] | None = None,
    service_inspector: Callable[..., Mapping[str, Any]] | None = None,
    status_path: Path | None = None,
    status_writer: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    persist = write_watchdog_status if status_writer is None else status_writer

    def persist_fatal(result_code: str) -> None:
        persist(
            ok=False,
            result_code=result_code,
            service_identity_status="unverified",
            funnel_status="unknown",
            path=status_path,
        )

    if (
        not math.isfinite(interval_seconds)
        or interval_seconds < 0
        or (interval_seconds == 0 and max_cycles is None)
    ):
        persist_fatal("DEVSPACE_WATCHDOG_INTERVAL_INVALID")
        raise SetupError("DEVSPACE_WATCHDOG_INTERVAL_INVALID")
    if max_cycles is not None and (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or max_cycles < 1
    ):
        persist_fatal("DEVSPACE_WATCHDOG_MAX_CYCLES_INVALID")
        raise SetupError("DEVSPACE_WATCHDOG_MAX_CYCLES_INVALID")

    acquire = acquire_watchdog_mutex if mutex_factory is None else mutex_factory
    try:
        release = acquire()
    except Exception as error:
        persist_fatal(
            _result_code_from_exception(
                error,
                "DEVSPACE_WATCHDOG_MUTEX_ACQUIRE_FAILED",
            )
        )
        raise
    if release is None:
        return {"ok": True, "already_running": True, "cycles": 0}

    cycle_runner = watchdog_cycle if cycle is None else cycle
    completed = 0
    last_cycle: dict[str, Any] = {
        "ok": False,
        "result_code": "DEVSPACE_WATCHDOG_NOT_STARTED",
    }
    last_verified_service: Mapping[str, Any] | None = None
    last_verified_funnel: Mapping[str, Any] | None = None
    try:
        while max_cycles is None or completed < max_cycles:
            completed += 1
            try:
                last_cycle = cycle_runner(
                    config_path=config_path,
                    opener=opener,
                    runner=runner,
                    popen_factory=popen_factory,
                    sleeper=sleeper,
                    service_inspector=service_inspector,
                )
            except Exception as error:
                last_cycle = {
                    "ok": False,
                    "result_code": _result_code_from_exception(
                        error,
                        "DEVSPACE_WATCHDOG_CYCLE_FAILED",
                    ),
                }
            status, last_verified_service, last_verified_funnel = _watchdog_cycle_status(
                last_cycle,
                last_verified_service=last_verified_service,
                last_verified_funnel=last_verified_funnel,
            )
            persist(**status, path=status_path)
            if max_cycles is None or completed < max_cycles:
                sleeper(interval_seconds)
    finally:
        try:
            release()
        except Exception as error:
            persist_fatal(
                _result_code_from_exception(
                    error,
                    "DEVSPACE_WATCHDOG_MUTEX_RELEASE_FAILED",
                )
            )
            raise
    return {
        "ok": bool(last_cycle.get("ok")),
        "already_running": False,
        "cycles": completed,
        "last_cycle": last_cycle,
    }


def ensure_public_route(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
    service_inspector: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    local, attempts = wait_for_exact_local_health(config, opener=opener)
    report: dict[str, Any] = {
        "ok": False,
        "created": False,
        "local": local,
        "local_attempts": attempts,
        "registration_url": config.registration_url,
        "recommended_app_name": APP_NAME,
    }
    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not local.get("ok"):
        if service.get("ok"):
            service = {
                **service,
                "ok": False,
                "error": "DEVSPACE_LOCAL_HEALTH_UNVERIFIED",
            }
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=False,
            repaired=False,
            attempts=attempts,
            runner=runner,
        )
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=False,
            attempts=attempts,
            runner=runner,
        )
    report["service"] = {
        "healthy": True,
        "repaired": False,
        "attempts": attempts,
        "identity_status": "verified",
        "identity": service["identity"],
    }

    funnel = funnel_status(config, runner=runner, allow_absent=True)
    report["funnel"] = funnel
    if not funnel.get("ok"):
        return {
            **report,
            "result_code": str(funnel.get("error") or "TAILSCALE_FUNNEL_STATUS_FAILED"),
            "next_action": "CHECK_TAILSCALE_FUNNEL",
        }

    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=False,
            attempts=attempts,
            runner=runner,
            observed_funnel=funnel,
        )
    if funnel.get("mapping") == "absent":
        try:
            run_checked(tailscale_funnel_argv(config), runner=runner)
        except (OSError, subprocess.SubprocessError) as error:
            report["funnel"] = {**funnel, "ok": False, "error": type(error).__name__}
            return {
                **report,
                "result_code": "TAILSCALE_FUNNEL_CREATE_FAILED",
                "next_action": "CHECK_TAILSCALE_FUNNEL",
            }
        report["created"] = True
        funnel = funnel_status(config, runner=runner)
        report["funnel"] = funnel
        if not funnel.get("ok") or funnel.get("mapping") != "match":
            return {
                **report,
                "result_code": str(funnel.get("error") or "TAILSCALE_FUNNEL_READBACK_FAILED"),
                "next_action": "CHECK_TAILSCALE_FUNNEL",
            }

    public = devspace_health_probe(config.public_health_url, opener=opener)
    report["public"] = public
    if not public.get("ok"):
        return {
            **report,
            "result_code": "DEVSPACE_PUBLIC_HEALTH_UNVERIFIED",
            "next_action": "CHECK_PUBLIC_FUNNEL_ENDPOINT",
        }
    service = inspect_exact_devspace_service(config, inspector=service_inspector)
    if not service.get("ok"):
        return _fail_closed_service_report(
            report,
            config,
            service,
            healthy=True,
            repaired=False,
            attempts=attempts,
            runner=runner,
            observed_funnel=funnel,
        )
    report["service"]["identity"] = service["identity"]
    report["funnel"] = {
        "ok": True,
        "mapping": "match",
        "identity": funnel_identity(config),
    }
    return {
        **report,
        "ok": True,
        "result_code": "READY",
        "next_action": "READY",
    }


def doctor(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
    chatgpt_call_failed: bool = False,
    config_path: Path | None = None,
) -> dict[str, Any]:
    local = devspace_health_probe(config.local_health_url, opener=opener)
    if not local.get("ok"):
        return {
            "local": local,
            "registration_url": config.registration_url,
            "recommended_app_name": APP_NAME,
            "next_action": "CHECK_DEVSPACE_LOCAL_SERVICE",
        }
    if config_path is not None:
        try:
            target, persisted = load_devspace_config(config_path)
            configured_roots = validate_roots(persisted["allowedRoots"])
        except (OSError, SetupError) as error:
            return {
                "local": local,
                "config": {"ok": False, "error": str(error), "path": str(Path(config_path).expanduser())},
                "registration_url": config.registration_url,
                "recommended_app_name": APP_NAME,
                "next_action": "CHECK_DEVSPACE_CONFIG",
            }
        configured_keys = {
            Path(os.path.normcase(os.path.normpath(str(root)))) for root in configured_roots
        }
        missing_roots = []
        for root in config.roots:
            requested = Path(os.path.normcase(os.path.normpath(str(root.resolve(strict=False)))))
            if not any(allowed == requested or allowed in requested.parents for allowed in configured_keys):
                missing_roots.append(root)
        config_evidence = {
            "ok": not missing_roots,
            "path": str(target),
            "configured_roots": [str(root) for root in configured_roots],
            "missing_roots": [str(root) for root in missing_roots],
        }
        if missing_roots:
            return {
                "local": local,
                "config": config_evidence,
                "registration_url": config.registration_url,
                "recommended_app_name": APP_NAME,
                "next_action": "CHECK_DEVSPACE_ALLOWED_ROOTS",
            }
    funnel = funnel_status(config, runner=runner)
    if not funnel.get("ok"):
        return {
            "local": local,
            "funnel": funnel,
            **({"config": config_evidence} if config_path is not None else {}),
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
    if config_path is not None:
        report["config"] = config_evidence
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
    watchdog = sub.add_parser("watchdog")
    watchdog.add_argument(
        "--interval-seconds",
        type=float,
        default=WATCHDOG_INTERVAL_SECONDS,
        help=argparse.SUPPRESS,
    )
    watchdog.add_argument("--max-cycles", type=int, help=argparse.SUPPRESS)
    roots = sub.add_parser("roots")
    roots.add_argument("--root", action="append", default=[], help="Replacement allowed root; repeat as needed")
    roots.add_argument("--dry-run", action="store_true")
    roots.add_argument("--apply", action="store_true")
    roots.add_argument("--restart", action="store_true", help="Restart the exact DevSpace service after applying")
    local_network = sub.add_parser(
        "local-network",
        help="Check the explicit Chrome Local Network Access grant, or --apply to write it after consent",
    )
    local_network.add_argument("--apply", action="store_true", help="Write the narrow grant after explicit consent")
    local_network.add_argument("--codex-home", type=Path, help=argparse.SUPPRESS)
    for name in ("setup", "ensure", "doctor"):
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
        if args.command == "watchdog":
            report = run_watchdog(
                interval_seconds=args.interval_seconds,
                max_cycles=args.max_cycles,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 2
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
        if args.command == "local-network":
            report = chrome_local_network_report(apply=args.apply, codex_home=args.codex_home)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if (report.get("enabled") is True or report.get("supported") is False) else 2
        config = validate_config(args.root, args.hostname, args.local_port, args.public_port)
        if args.command == "setup":
            if args.dry_run == args.apply:
                raise SetupError("CHOOSE_EXACTLY_ONE_OF_DRY_RUN_OR_APPLY")
            plan = setup_plan(config)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            if args.apply:
                apply_setup(config)
            return 0
        if args.command == "ensure":
            report = ensure_public_route(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 2
        print(json.dumps(doctor(
            config,
            chatgpt_call_failed=args.chatgpt_call_failed,
            config_path=devspace_config_path(),
        ), ensure_ascii=False, indent=2))
        return 0
    except SetupError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
