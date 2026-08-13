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
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_PORT = 7676
APP_NAME = "DevSpace"
AUTOSTART_NAME = "DevSpace MCP Server"
AUTOSTART_REG_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
DEVSPACE_PACKAGE = "@waishnav/devspace@1.0.7"
DEVSPACE_OAUTH_SCOPES = "devspace,offline_access"
SECRET_PATTERN = re.compile(r"(?i)(password|token|secret|authorization)\s*([:=])\s*[^\s,;]+")
HOSTNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*\.ts\.net$", re.IGNORECASE)
LOCAL_HEALTH_ATTEMPTS = 15
LOCAL_HEALTH_INTERVAL_SECONDS = 1
LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS = 1


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
        run_checked(devspace_prepare_argv(), runner=runner)
        run_checked(devspace_compat_argv(stop_exact_service=True, local_port=local_port), runner=runner)
        launch_hidden(devspace_serve_argv(), popen_factory=popen_factory)
        run_checked(devspace_compat_argv(confirm_restarted=True, local_port=local_port), runner=runner)
        result["restart_required"] = False
        result["restart_performed"] = True
    return result


def persist_existing_setup_config(config_path: Path, config: SetupConfig) -> Path:
    """Atomically merge the managed roots into an existing DevSpace config.

    The existing config is backed up, then rewritten with the exact requested
    root list while every other key (Owner/OAuth state, tool mode, unknown
    fields) is preserved.  The staged bytes are parsed strictly before the
    atomic replace, and the live file is read back afterwards.  Symlinked or
    invalid configs fail closed without any mutation.
    """
    target = Path(config_path).expanduser()
    if target.is_symlink():
        raise SetupError("DEVSPACE_CONFIG_SYMLINK_UNSUPPORTED")
    _, payload = load_devspace_config(target)
    backup_path = target.with_name(f"{target.name}.bak-{time.time_ns()}")
    shutil.copy2(target, backup_path)
    payload.update(
        {
            "host": payload.get("host") or "127.0.0.1",
            "port": config.local_port,
            "allowedRoots": [str(root) for root in config.roots],
            "publicBaseUrl": f"https://{config.hostname}",
        }
    )
    expected_roots = [str(root) for root in config.roots]
    temporary = target.with_name(f".{target.name}.tmp-{time.time_ns()}")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            temporary.chmod(0o600)
        # Parse the staged bytes strictly before replacing the live file.
        load_devspace_config(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    _, readback = load_devspace_config(target)
    if readback.get("allowedRoots") != expected_roots:
        raise SetupError("DEVSPACE_ROOTS_READBACK_MISMATCH")
    return backup_path


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
        "devspace_serve": devspace_serve_argv(),
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
    run_checked(devspace_prepare_argv(), runner=runner)
    run_checked(devspace_compat_argv(), runner=runner)
    run_checked(devspace_serve_argv(), runner=runner)


def apply_setup(
    config: SetupConfig,
    *,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    config_path: Path | None = None,
) -> None:
    # Interactive init remains DevSpace's own prompt so it can safely retain its
    # Owner credential.  An existing installation is never re-initialized:
    # its config is backed up and merged atomically instead, preserving the
    # Owner/OAuth state and every other key.  The root list/public origin are
    # displayed before this call.
    slot = funnel_status(config, runner=runner, allow_absent=True)
    if slot.get("mapping") == "conflict":
        raise SetupError("TAILSCALE_FUNNEL_PORT_IN_USE")
    if config_path is not None and config_path.exists():
        persist_existing_setup_config(config_path, config)
    else:
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


def wait_for_local_service(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[dict[str, Any], int]:
    """Wait for the exact loopback DevSpace health identity; return last probe and attempts."""
    last: dict[str, Any] = {"ok": False, "error": "DEVSPACE_LOCAL_SERVICE_NOT_READY"}
    for index in range(1, LOCAL_HEALTH_ATTEMPTS + 1):
        last = devspace_health_probe(
            config.local_health_url,
            opener=opener,
            timeout=LOCAL_HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        if last.get("ok"):
            return last, index
        if index < LOCAL_HEALTH_ATTEMPTS:
            time.sleep(LOCAL_HEALTH_INTERVAL_SECONDS)
    return last, LOCAL_HEALTH_ATTEMPTS


def ensure_public_route(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    local, attempts = wait_for_local_service(config, opener=opener)
    report: dict[str, Any] = {
        "ok": False,
        "created": False,
        "local": local,
        "local_attempts": attempts,
        "registration_url": config.registration_url,
        "recommended_app_name": APP_NAME,
    }
    if not local.get("ok"):
        return {**report, "next_action": "CHECK_DEVSPACE_LOCAL_SERVICE"}

    funnel = funnel_status(config, runner=runner, allow_absent=True)
    report["funnel"] = funnel
    if not funnel.get("ok"):
        return {**report, "next_action": "CHECK_TAILSCALE_FUNNEL"}

    if funnel.get("mapping") == "absent":
        try:
            run_checked(
                [
                    "tailscale",
                    "funnel",
                    "--bg",
                    f"--https={config.public_port}",
                    f"http://127.0.0.1:{config.local_port}",
                ],
                runner=runner,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            report["funnel"] = {**funnel, "ok": False, "error": type(error).__name__}
            return {**report, "next_action": "CHECK_TAILSCALE_FUNNEL"}
        report["created"] = True
        funnel = funnel_status(config, runner=runner)
        report["funnel"] = funnel
        if not funnel.get("ok"):
            return {**report, "next_action": "CHECK_TAILSCALE_FUNNEL"}

    public = devspace_health_probe(config.public_health_url, opener=opener)
    report["public"] = public
    if not public.get("ok"):
        return {**report, "next_action": "CHECK_PUBLIC_FUNNEL_ENDPOINT"}
    return {**report, "ok": True, "next_action": "READY"}


def refresh_after_app_registration(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Recycle the managed server after manual ChatGPT OAuth registration.

    DevSpace can leave a newly approved ChatGPT connector unable to create its
    first tool session until the server is recycled.  This command is
    deliberately explicit: it never opens ChatGPT settings and it preserves the
    existing config, Owner credential, OAuth database, roots, and Funnel
    hostname.
    """
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
    result = refresh_exact_public_route(config, opener=opener, runner=runner)
    return {
        **result,
        "service_restarted": True,
        "credentials_preserved": True,
        "next_action": "VERIFY_REGISTERED_CHATGPT_APP_WITH_ORACLE",
        "verification_boundary": (
            "Use a fresh regular Oracle @codex read-only probe; Codex Desktop's "
            "DevSpace plugin tools are a different connector and are not proof of "
            "the manually registered ChatGPT app."
        ),
    }


def _exclusive_exact_funnel_entry(config: SetupConfig, entry: Any) -> bool:
    """Return true only for one root handler owned by this managed route."""
    if not isinstance(entry, dict):
        return False
    handlers = entry.get("Handlers")
    if not isinstance(handlers, dict) or set(handlers) != {"/"}:
        return False
    root = handlers.get("/")
    if not isinstance(root, dict):
        return False
    proxy = str(root.get("Proxy") or "").rstrip("/").casefold()
    expected = f"http://127.0.0.1:{config.local_port}".casefold()
    return proxy == expected


def refresh_exact_public_route(
    config: SetupConfig,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Recycle only the exact managed HTTPS slot before reasserting it.

    A matching local Funnel status can survive while its public relay path is
    stale.  First require the exact local DevSpace ``/healthz`` identity; then
    turn the slot off only when it is one ``/`` handler proxying exactly
    ``http://127.0.0.1:<local_port>`` on the exact host and HTTPS port.  Never
    use the global ``funnel reset`` here: it would erase unrelated ports.
    Shared path handlers, conflicting mappings, and other ports are never
    removed; any conflict fails closed before mutation.
    """
    local, _ = wait_for_local_service(config, opener=opener)
    if not local.get("ok"):
        raise SetupError("DEVSPACE_LOCAL_SERVICE_NOT_READY")
    current = funnel_status(config, runner=runner, allow_absent=True)
    if current.get("mapping") == "conflict":
        raise SetupError("TAILSCALE_FUNNEL_PORT_IN_USE")
    recycled = current.get("mapping") == "match" and _exclusive_exact_funnel_entry(
        config, current.get("status")
    )
    if recycled:
        run_checked(
            ["tailscale", "funnel", "--bg", f"--https={config.public_port}", "off"],
            runner=runner,
        )
    result = ensure_public_route(config, opener=opener, runner=runner)
    return {
        **result,
        "exact_funnel_recycled": recycled,
        "funnel_recycle_scope": f"https:{config.public_port}" if recycled else None,
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
        report["next_action"] = "POST_REGISTER_REFRESH_OR_EXTERNAL_APP_CHECK"
        report["message"] = (
            "Public endpoint is healthy. If manual registration or reconnect just completed, "
            "run post-register once and verify the registered app with a fresh regular Oracle "
            "@codex read-only probe. Do not use Codex Desktop DevSpace plugin tools as proof, "
            "and do not automate or repeat app registration."
        )
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
    for name in ("setup", "ensure", "doctor", "post-register"):
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
                apply_setup(config, config_path=devspace_config_path())
            return 0
        if args.command == "ensure":
            report = ensure_public_route(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 2
        if args.command == "post-register":
            report = refresh_after_app_registration(config)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report.get("ok") else 2
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
