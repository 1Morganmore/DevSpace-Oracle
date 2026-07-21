from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SETTINGS_URL = "https://chatgpt.com/#settings/Plugins"
APP_DIRECTORY_URL = "https://chatgpt.com/plugins"
COMPOSER_URL = "https://chatgpt.com/"
GLOBAL_BROWSER_MUTATION_LOCK = Path.home() / ".codex" / "state" / "chatgpt-agbrowse" / "global-dispatch.lock"
APP_MANAGER_PATH = Path(__file__).resolve().with_name("codexpro_project_app_manager.py")
ALLOWED_COMMANDS = {
    "status",
    "start",
    "tabs",
    "active-tab",
    "select-tab",
    "new-tab",
    "get-dom",
    "navigate",
    "observe-bundle",
    "click",
    "type",
    "select",
    "check",
    "press",
    "tab-switch",
    "tab-close",
}


@contextmanager
def exclusive_browser_mutation_lock(path: Path = GLOBAL_BROWSER_MUTATION_LOCK, timeout_seconds: int = 900):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.time() + max(1, int(timeout_seconds))
    locked = False
    try:
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (OSError, BlockingIOError):
                if time.time() >= deadline:
                    raise AppBridgeError(
                        "APP_BROWSER_MUTATION_LOCK_TIMEOUT",
                        "timed out waiting for exclusive ChatGPT browser mutation ownership",
                    )
                time.sleep(0.05)
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _load_app_manager():
    bin_dir = str(APP_MANAGER_PATH.parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    spec = importlib.util.spec_from_file_location("codexpro_project_app_manager_agbrowse", APP_MANAGER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"app manager unavailable: {APP_MANAGER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


APP_MANAGER = _load_app_manager()


def _hydrate_redacted_decision_url(decision: dict[str, Any]) -> dict[str, Any]:
    """Resolve a bootstrap report's redacted connector URL from its local transaction."""
    public_url = str(decision.get("public_url") or "")
    if "codexpro_token=<redacted>" not in public_url:
        return decision
    root = str(decision.get("root") or "")
    app_name = str(decision.get("app_name") or "")
    transaction_id = str(decision.get("transaction_id") or "")
    registry = APP_MANAGER.load_registry()
    pending = registry.get("pending_reconciles") if isinstance(registry, dict) else None
    transaction = pending.get(transaction_id) if isinstance(pending, dict) and transaction_id else None
    candidate = transaction.get("candidate") if isinstance(transaction, dict) else None
    if not (
        isinstance(transaction, dict)
        and str(transaction.get("root") or "") == root
        and isinstance(candidate, dict)
        and str(candidate.get("app_name") or "") == app_name
    ):
        raise AppBridgeError(
            "APP_DECISION_SECRET_UNAVAILABLE",
            "redacted bootstrap decision has no exact pending registry candidate",
            {"root": root, "app_name": app_name, "transaction_id": transaction_id},
        )
    resolved_url = str(candidate.get("public_url") or "")
    if not (
        resolved_url.startswith("https://")
        and "/mcp?" in resolved_url
        and "codexpro_token=" in resolved_url
        and "<redacted>" not in resolved_url
    ):
        raise AppBridgeError(
            "APP_DECISION_SECRET_UNAVAILABLE",
            "pending registry candidate did not contain a complete unredacted MCP URL",
            {"root": root, "app_name": app_name, "transaction_id": transaction_id},
        )
    return {**decision, "public_url": resolved_url}


class AppBridgeError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}

    def envelope(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": str(self), "evidence": self.evidence}}


Runner = Callable[[list[str], dict[str, str], int], subprocess.CompletedProcess[str]]


def _direct_agbrowse_argv(
    command: list[str],
    *,
    platform: str | None = None,
    node_executable: str | None = None,
) -> list[str]:
    """Avoid Windows cmd.exe reparsing npm-shim arguments such as URL '&'."""
    if not command:
        return command
    platform = os.name if platform is None else platform
    shim = Path(command[0])
    if not (platform == "nt" and shim.suffix.casefold() == ".cmd" and shim.stem.casefold() == "agbrowse"):
        return command
    entrypoint = shim.parent / "node_modules" / "agbrowse" / "bin" / "agbrowse.mjs"
    if not entrypoint.is_file():
        raise FileNotFoundError(f"agbrowse package entrypoint not found beside npm shim: {entrypoint}")
    node = node_executable or str((shim.parent / "node.exe") if (shim.parent / "node.exe").is_file() else (shutil.which("node") or ""))
    if not node:
        raise FileNotFoundError("node executable required for the selected agbrowse entrypoint")
    return [node, str(entrypoint), *command[1:]]


def default_runner(command: list[str], env: dict[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _direct_agbrowse_argv(command),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


@dataclass(frozen=True)
class Node:
    ref: str
    role: str
    name: str
    value: str
    checked: bool | None


class Snapshot:
    def __init__(self, payload: dict[str, Any]):
        self.url = str(payload.get("url") or "")
        self.target_id = str(payload.get("targetId") or payload.get("target_id") or "")
        self.text_summary = str(payload.get("textSummary") or payload.get("text_summary") or "")
        raw_nodes = payload.get("snapshotNodes") or payload.get("nodes") or payload.get("refs") or []
        self.nodes = [
            Node(
                ref=str(item.get("ref") or "").lstrip("@"),
                role=str(item.get("role") or "").lower(),
                name=str(item.get("name") or "").strip(),
                value=str(item.get("value") or "").strip(),
                checked=item.get("checked") if isinstance(item.get("checked"), bool) else None,
            )
            for item in raw_nodes
            if isinstance(item, dict) and item.get("ref")
        ]

    def exact(self, *, roles: set[str], names: tuple[str, ...], required: bool = True) -> Node | None:
        wanted = {name.casefold() for name in names}
        matches = [node for node in self.nodes if node.role in roles and node.name.casefold() in wanted]
        if len(matches) == 1:
            return matches[0]
        if not matches and not required:
            return None
        raise AppBridgeError(
            "APP_UI_DRIFT",
            "expected exactly one role/name match",
            {"roles": sorted(roles), "names": list(names), "match_count": len(matches), "url": self.url},
        )

    def exact_prefix(self, *, roles: set[str], names: tuple[str, ...], required: bool = True) -> Node | None:
        prefixes = tuple(name.casefold() for name in names)
        matches = [
            node
            for node in self.nodes
            if node.role in roles and any(node.name.casefold().startswith(prefix) for prefix in prefixes)
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches and not required:
            return None
        raise AppBridgeError(
            "APP_UI_DRIFT",
            "expected exactly one role/name-prefix match",
            {"roles": sorted(roles), "name_prefixes": list(names), "match_count": len(matches), "url": self.url},
        )

    def exact_value(self, *, names: tuple[str, ...]) -> str | None:
        wanted = {name.casefold() for name in names}
        matches = [node.value for node in self.nodes if node.name.casefold() in wanted and node.value]
        if len(matches) > 1 and len(set(matches)) != 1:
            raise AppBridgeError("APP_UI_DRIFT", "ambiguous exact field values", {"names": list(names), "values": matches})
        return matches[0] if matches else None

    def exact_app(self, app_name: str, *, required: bool = True) -> Node | None:
        """Match an installed app when ChatGPT appends permission text to its name."""
        wanted = app_name.casefold()
        allowed_suffixes = (
            "모두 허용",
            "저위험 액션 허용",
            "다시 연결",
            "열기",
            "연결됨",
            "allow all",
            "allow low risk",
            "reconnect",
            "open",
            "connected",
        )
        matches = [
            node
            for node in self.nodes
            if node.role in {"button", "link", "row"}
            and (
                node.name.casefold() == wanted
                or node.name.casefold() in {f"{wanted} dev", f"{wanted} {wanted} dev"}
                or any(node.name.casefold().startswith(wanted + " " + suffix) for suffix in allowed_suffixes)
            )
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches and not required:
            return None
        raise AppBridgeError(
            "APP_UI_DRIFT",
            "expected exactly one installed-app match",
            {"app_name": app_name, "match_count": len(matches), "url": self.url},
        )


class AgbrowseGateway:
    def __init__(self, *, executable: str = "agbrowse", runner: Runner | None = None, timeout: int = 60):
        self.executable = (shutil.which(executable) or executable) if runner is None else executable
        self.runner = runner or default_runner
        self.timeout = timeout
        self.env = os.environ.copy()
        self.env["AGBROWSE_JSON_ERRORS"] = "1"
        self.env["AGBROWSE_UPDATE_CHECK"] = "0"
        self.env["AGBROWSE_WEB_AI_AUTO_START"] = "0"
        self._browser_ready = False
        self._owned_startup_targets: dict[str, str] = {}
        self._owned_utility_targets: set[str] = set()
        self._owned_composer_targets: set[str] = set()
        self._pinned_target_id: str | None = None
        self.command_count = 0

    def pin_target(self, target_id: str) -> None:
        if target_id not in self._owned_utility_targets and target_id not in self._owned_composer_targets:
            raise AppBridgeError(
                "APP_UTILITY_TARGET_NOT_OWNED",
                "cannot pin a target that was not created by this utility operation",
                {"target_id": target_id},
            )
        self._pinned_target_id = target_id

    def unpin_target(self, target_id: str) -> None:
        if self._pinned_target_id == target_id:
            self._pinned_target_id = None

    def adopt_owned_startup_targets(self, targets: Mapping[str, str]) -> None:
        """Accept startup ownership proved by the outer bridge's exact start."""
        for target_id, url in targets.items():
            normalized_id = str(target_id or "").strip()
            normalized_url = str(url or "").strip()
            if normalized_id and normalized_url == "about:blank":
                self._owned_startup_targets[normalized_id] = normalized_url

    def _invoke(self, command: str, *args: str, json_output: bool = True) -> Any:
        if command not in ALLOWED_COMMANDS:
            raise AppBridgeError("APP_COMMAND_FORBIDDEN", f"non-agbrowse or unapproved command: {command}")
        self.command_count += 1
        argv = [self.executable, command, *[str(item) for item in args]]
        if json_output and "--json" not in argv:
            argv.append("--json")
        completed = self.runner(argv, self.env, self.timeout)
        if completed.returncode != 0:
            raise AppBridgeError(
                "APP_AGBROWSE_COMMAND_FAILED",
                f"agbrowse {command} failed",
                {"exit_code": completed.returncode, "stderr": (completed.stderr or "")[-1000:]},
            )
        if not json_output:
            return {"ok": True, "stdout": completed.stdout or ""}
        try:
            payload = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise AppBridgeError("APP_AGBROWSE_JSON_INVALID", f"agbrowse {command} returned invalid JSON") from exc
        return payload

    def call(self, command: str, *args: str, json_output: bool = True) -> dict[str, Any]:
        payload = self._invoke(command, *args, json_output=json_output)
        if not isinstance(payload, dict):
            raise AppBridgeError("APP_AGBROWSE_JSON_INVALID", f"agbrowse {command} JSON must be an object")
        return payload

    def list_tabs(self) -> list[dict[str, Any]]:
        payload = self._invoke("tabs")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise AppBridgeError("APP_TAB_LIST_INVALID", "agbrowse tabs JSON must be a list of objects")
        return payload

    def navigate(self, url: str) -> None:
        # Upstream 0.1.18 navigate is intentionally text-only even when an
        # unknown --json flag is present; observe-bundle supplies the
        # authoritative structured URL/target readback immediately after it.
        self.call("navigate", url, json_output=False)

    def ensure_started(self, *, timeout_seconds: int = 20, start_attempts: int = 3) -> None:
        if self._browser_ready:
            return

        def running() -> bool:
            result = self.call("status", json_output=False)
            text = str(result.get("stdout") or "")
            lowered = text.casefold()
            return bool(
                __import__("re").search(r'\"?running\"?\s*:\s*true\b', lowered)
                or __import__("re").search(r"\brunning\s+true\b", lowered)
            )

        started_here = False
        is_running = running()
        start_errors: list[str] = []
        if not is_running:
            started_here = True
            attempts = max(1, min(5, int(start_attempts)))
            per_attempt_wait = max(1.0, min(5.0, float(timeout_seconds) / attempts))
            for _attempt in range(1, attempts + 1):
                start_failed = False
                try:
                    self.call("start", "--headed", json_output=False)
                except AppBridgeError as exc:
                    start_errors.append(str(exc))
                    start_failed = True
                is_running = running()
                if is_running:
                    break
                if start_failed:
                    continue
                deadline = time.monotonic() + per_attempt_wait
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    is_running = running()
                    if is_running:
                        break
                if is_running:
                    break
            if not is_running:
                raise AppBridgeError(
                    "APP_BROWSER_START_FAILED",
                    "agbrowse browser did not become ready after bounded start retries",
                    {"attempts": attempts, "start_errors": start_errors[-attempts:]},
                )
        self._browser_ready = True
        if started_here:
            for tab in self.list_tabs():
                target_id = str(tab.get("targetId") or tab.get("target_id") or "")
                url = str(tab.get("url") or "")
                if target_id and url == "about:blank":
                    self._owned_startup_targets[target_id] = url

    def snapshot(self) -> Snapshot:
        last_drift: dict[str, Any] | None = None
        for attempt in range(1, 4):
            payload = self.call("observe-bundle", "--max-nodes", "400")
            reported_target_id = str(payload.get("targetId") or payload.get("target_id") or "")
            # agbrowse 0.1.18 observe-bundle reports the CDP runtime identity
            # (``cdp:<port>``), while tabs/new-tab/active-tab use page target IDs.
            if not reported_target_id.startswith("cdp:"):
                return Snapshot(payload)
            active = self.call("active-tab")
            active_target_id = str(active.get("targetId") or active.get("target_id") or "")
            active_url = str(active.get("url") or "")
            observation_url = str(payload.get("url") or "")
            if not active_target_id or not active_url:
                raise AppBridgeError(
                    "APP_ACTIVE_TARGET_MISSING",
                    "active-tab did not return an exact page target and URL for the fresh observation",
                    {"observation_target_id": reported_target_id},
                )
            if active_url == observation_url and (
                self._pinned_target_id is None or active_target_id == self._pinned_target_id
            ):
                return Snapshot({
                    **payload,
                    "observationTargetId": reported_target_id,
                    "targetId": active_target_id,
                })
            last_drift = {
                "observation_target_id": reported_target_id,
                "observation_url": observation_url,
                "active_target_id": active_target_id,
                "active_url": active_url,
                "pinned_target_id": self._pinned_target_id,
                "snapshot_attempt": attempt,
            }
            # A pinned target mismatch is ownership drift, not a retry cue.
            # Never switch tabs from inside a read-only observation.
            if (
                self._pinned_target_id is None
                or active_target_id != self._pinned_target_id
                or attempt >= 3
            ):
                break
            self.activate_target(self._pinned_target_id)
            time.sleep(0.1)
        raise AppBridgeError(
            "APP_OBSERVATION_TARGET_DRIFT",
            "observe-bundle and active-tab did not identify the same exact active page",
            last_drift or {},
        )

    def click(self, node: Node) -> None:
        self.call("click", node.ref, json_output=False)

    def type(self, node: Node, value: str) -> None:
        self.call("type", node.ref, value, json_output=False)

    def select(self, node: Node, value: str) -> None:
        self.call("select", node.ref, value, json_output=False)

    def check(self, node: Node) -> None:
        self.call("check", node.ref, json_output=False)

    def press(self, key: str) -> None:
        self.call("press", key, json_output=False)

    def new_tab(self, url: str) -> dict[str, Any]:
        return self.call("new-tab", url)

    def _retire_owned_startup_targets(
        self,
        tabs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove only connector-owned blank startup pages before ``new-tab``.

        agbrowse may implement ``new-tab`` by navigating Chrome's sole blank
        startup page and returning that page's existing target id.  That is a
        legitimate browser optimization but cannot satisfy the bridge's fresh
        target contract.  Close only targets that this gateway recorded while
        starting Chrome and that are still exactly ``about:blank``, then prove
        their absence before creating the work target.
        """
        candidates = {
            str(tab.get("targetId") or tab.get("target_id") or "")
            for tab in tabs
            if str(tab.get("targetId") or tab.get("target_id") or "")
            in self._owned_startup_targets
            and str(tab.get("url") or "") == "about:blank"
        }
        candidates.discard("")
        if not candidates:
            return tabs
        # Closing Chrome's sole page terminates the headed runtime on Windows,
        # making the immediately following new-tab fail. Keep that one
        # connector-owned blank page and allow open_composer_target to promote
        # it only after an exact URL re-read. Utility/settings targets retain
        # the stricter absent-before-new-tab rule.
        live_ids = {
            str(tab.get("targetId") or tab.get("target_id") or "")
            for tab in tabs
            if str(tab.get("targetId") or tab.get("target_id") or "")
        }
        if live_ids == candidates and len(candidates) == 1:
            return tabs
        for target_id in sorted(candidates):
            self.call("tab-close", target_id)
        after = self.list_tabs()
        survivors = {
            str(tab.get("targetId") or tab.get("target_id") or "")
            for tab in after
        } & candidates
        if survivors:
            raise AppBridgeError(
                "APP_STARTUP_TARGET_CLEANUP_FAILED",
                "connector-owned blank startup target remained after close",
                {"target_ids": sorted(survivors)},
            )
        for target_id in candidates:
            self._owned_startup_targets.pop(target_id, None)
        return after

    def open_composer_target(self, url: str) -> dict[str, Any]:
        """Create and pin one composer target proven absent before new-tab."""
        before = self._retire_owned_startup_targets(self.list_tabs())
        preexisting = {
            str(tab.get("targetId") or tab.get("target_id") or "") for tab in before
        }
        preexisting.discard("")
        created = self.new_tab(url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        if not target_id:
            raise AppBridgeError("APP_COMPOSER_TARGET_MISSING", "new composer tab did not return a target id")
        if target_id in preexisting:
            owned_startup_promotion = (
                target_id in self._owned_startup_targets
                and self._owned_startup_targets.get(target_id) == "about:blank"
            )
            if owned_startup_promotion:
                after = self.list_tabs()
                matches = [
                    tab for tab in after
                    if str(tab.get("targetId") or tab.get("target_id") or "") == target_id
                ]
                expected_url = url.rstrip("/")
                actual_url = str(matches[0].get("url") or "").rstrip("/") if len(matches) == 1 else ""
                if len(matches) == 1 and actual_url == expected_url:
                    self._owned_startup_targets.pop(target_id, None)
                    self._owned_composer_targets.add(target_id)
                    self._pinned_target_id = target_id
                    return {
                        **created,
                        "newTargetProven": True,
                        "startupTargetPromoted": True,
                        "promotionUrlVerified": True,
                    }
            raise AppBridgeError(
                "APP_COMPOSER_TARGET_REUSED_FOREIGN",
                "agbrowse new-tab returned a preexisting composer target",
                {"target_id": target_id},
            )
        self._owned_composer_targets.add(target_id)
        self._pinned_target_id = target_id
        return {**created, "newTargetProven": True}

    def open_utility_target(self, url: str) -> dict[str, Any]:
        self.ensure_started()
        before = self._retire_owned_startup_targets(self.list_tabs())
        preexisting_target_ids = {
            str(tab.get("targetId") or tab.get("target_id") or "") for tab in before
        }
        preexisting_target_ids.discard("")
        created = self.new_tab(url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        if not target_id:
            raise AppBridgeError("APP_UTILITY_TARGET_MISSING", "new utility tab did not return a target id")
        if target_id in preexisting_target_ids:
            raise AppBridgeError(
                "APP_UTILITY_TARGET_REUSED_FOREIGN",
                "agbrowse new-tab returned a preexisting target; refusing to navigate or close it",
                {"target_id": target_id, "preexisting_target_ids": sorted(preexisting_target_ids)},
            )
        self._owned_utility_targets.add(target_id)
        try:
            self.activate_target(target_id)
            after = self.list_tabs()
            matches = [
                tab for tab in after
                if str(tab.get("targetId") or tab.get("target_id") or "") == target_id
            ]
            if len(matches) != 1:
                raise AppBridgeError(
                    "APP_UTILITY_TARGET_UNPROVEN",
                    "new utility target was not uniquely present after creation",
                    {"target_id": target_id, "match_count": len(matches)},
                )
            self.pin_target(target_id)
            return {
                "ok": True,
                "target_id": target_id,
                "requested_url": url,
                "preexisting_target_ids": sorted(preexisting_target_ids),
            }
        except Exception:
            try:
                self.close_owned_target(target_id)
            except Exception:
                pass
            raise

    def close_owned_target(self, target_id: str) -> dict[str, Any]:
        if not target_id:
            raise AppBridgeError("APP_UTILITY_TARGET_MISSING", "owned utility target id is required for cleanup")
        if target_id not in self._owned_utility_targets:
            raise AppBridgeError(
                "APP_UTILITY_TARGET_NOT_OWNED",
                "refusing to close a target not created by this utility operation",
                {"target_id": target_id},
            )
        before = self.list_tabs()
        matches = [
            tab for tab in before
            if str(tab.get("targetId") or tab.get("target_id") or "") == target_id
        ]
        if len(matches) > 1:
            raise AppBridgeError(
                "APP_UTILITY_TARGET_AMBIGUOUS",
                "owned utility target matched more than one live tab",
                {"target_id": target_id, "match_count": len(matches)},
            )
        closed = False
        if matches:
            self.call("tab-close", target_id)
            closed = True
        after = self.list_tabs()
        if any(str(tab.get("targetId") or tab.get("target_id") or "") == target_id for tab in after):
            raise AppBridgeError(
                "APP_UTILITY_TARGET_CLOSE_UNCONFIRMED",
                "owned utility target remained after exact tab-close",
                {"target_id": target_id},
            )
        self.unpin_target(target_id)
        self._owned_utility_targets.discard(target_id)

        startup_closed: list[str] = []
        for startup_id, startup_url in list(self._owned_startup_targets.items()):
            current = self.list_tabs()
            startup_matches = [
                tab for tab in current
                if str(tab.get("targetId") or tab.get("target_id") or "") == startup_id
            ]
            if len(startup_matches) == 1 and str(startup_matches[0].get("url") or "") == startup_url == "about:blank":
                self.call("tab-close", startup_id)
                verify = self.list_tabs()
                if any(str(tab.get("targetId") or tab.get("target_id") or "") == startup_id for tab in verify):
                    raise AppBridgeError(
                        "APP_STARTUP_TARGET_CLOSE_UNCONFIRMED",
                        "run-owned startup blank target remained after exact tab-close",
                        {"target_id": startup_id},
                    )
                startup_closed.append(startup_id)
            self._owned_startup_targets.pop(startup_id, None)
        return {
            "ok": True,
            "target_id": target_id,
            "closed": closed,
            "absence_verified": True,
            "startup_targets_closed": startup_closed,
        }

    def switch_tab(self, target_id: str) -> None:
        self.call("tab-switch", target_id, json_output=False)

    def activate_target(self, target_id: str) -> dict[str, Any]:
        if not target_id:
            raise AppBridgeError("APP_COMPOSER_TARGET_MISSING", "composer target id is required")
        actual = ""
        try:
            active = self.call("active-tab")
            actual = str(active.get("targetId") or active.get("target_id") or "")
        except AppBridgeError:
            pass
        if actual != target_id:
            self.switch_tab(target_id)
            active = self.call("active-tab")
            actual = str(active.get("targetId") or active.get("target_id") or "")
        if actual != target_id:
            raise AppBridgeError(
                "APP_COMPOSER_TARGET_MISMATCH",
                "agbrowse did not activate the exact prepared composer target",
                {"expected_target_id": target_id, "actual_target_id": actual},
            )
        return {"ok": True, "target_id": actual}

    def dom(self, selector: str, max_chars: int = 20_000) -> str:
        result = self.call("get-dom", "--selector", selector, "--max-chars", str(max_chars), json_output=False)
        return str(result.get("stdout") or "")

    @staticmethod
    def settle() -> None:
        time.sleep(0.2)


class AppConnector:
    def __init__(self, gateway: AgbrowseGateway, *, registry: Any = APP_MANAGER):
        self.ui = gateway
        self.registry = registry
        self._utility_target_id: str | None = None

    def _run_with_utility_target(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        if self._utility_target_id:
            return operation()
        opener = getattr(self.ui, "open_utility_target", None)
        closer = getattr(self.ui, "close_owned_target", None)
        if not callable(opener) or not callable(closer):
            raise AppBridgeError(
                "APP_UTILITY_TARGET_API_MISSING",
                "app inspection and reconciliation require exact utility-target ownership APIs",
            )
        utility = opener(SETTINGS_URL)
        target_id = str(utility.get("target_id") or "") if isinstance(utility, dict) else ""
        if not target_id:
            raise AppBridgeError("APP_UTILITY_TARGET_MISSING", "utility target ownership was not returned")
        self._utility_target_id = target_id
        result: dict[str, Any] | None = None
        operation_error: BaseException | None = None
        try:
            result = operation()
        except BaseException as exc:
            operation_error = exc
        finally:
            self._utility_target_id = None
        cleanup: dict[str, Any] | None = None
        try:
            cleanup = closer(target_id)
        except Exception as cleanup_error:
            evidence = {
                "target_id": target_id,
                "operation_error": str(operation_error) if operation_error else None,
                "cleanup_error": str(cleanup_error),
            }
            raise AppBridgeError(
                "APP_UTILITY_TARGET_CLEANUP_FAILED",
                "exact app utility target cleanup failed",
                evidence,
            ) from cleanup_error
        if operation_error is not None:
            raise operation_error
        if not isinstance(result, dict):
            raise AppBridgeError("APP_RESULT_INVALID", "app utility operation must return an object")
        return {**result, "utility_cleanup": cleanup}

    def _navigate_snapshot(
        self,
        url: str,
        *,
        route_ok: Callable[[str], bool],
        error_code: str,
        attempts: int = 8,
    ) -> Snapshot:
        self.ui.ensure_started()
        if self._utility_target_id:
            self.ui.activate_target(self._utility_target_id)
        self.ui.navigate(url)
        last: Snapshot | None = None
        for _ in range(max(1, attempts)):
            settle = getattr(self.ui, "settle", None)
            if callable(settle):
                settle()
            last = self.ui.snapshot()
            if self._utility_target_id and last.target_id != self._utility_target_id:
                raise AppBridgeError(
                    "APP_UTILITY_TARGET_DRIFT",
                    "settings navigation left the exact run-owned utility target",
                    {"expected_target_id": self._utility_target_id, "actual_target_id": last.target_id},
                )
            if route_ok(last.url):
                return last
        raise AppBridgeError(
            error_code,
            "agbrowse navigation did not reach the required ChatGPT app route",
            {"requested_url": url, "observed_url": last.url if last else None},
        )

    def _settled_snapshot(self) -> Snapshot:
        settle = getattr(self.ui, "settle", None)
        if callable(settle):
            settle()
        page = self.ui.snapshot()
        if self._utility_target_id and page.target_id != self._utility_target_id:
            raise AppBridgeError(
                "APP_UTILITY_TARGET_DRIFT",
                "app mutation readback came from a foreign target",
                {"expected_target_id": self._utility_target_id, "actual_target_id": page.target_id},
            )
        return page

    def _settings(self) -> Snapshot:
        return self._navigate_snapshot(
            SETTINGS_URL,
            route_ok=lambda value: value.startswith(SETTINGS_URL),
            error_code="APP_SETTINGS_ROUTE_NOT_READY",
        )

    def _settings_detail(self, app_name: str) -> Snapshot:
        listing = self._settings()
        app = listing.exact_app(app_name, required=False)
        for _ in range(5):
            if app is not None:
                break
            listing = self._settled_snapshot()
            app = listing.exact_app(app_name, required=False)
        if app is None:
            raise AppBridgeError(
                "APP_NOT_VISIBLE_AFTER_MUTATION",
                "exact app was not visible on the hydrated settings route",
                {"app_name": app_name, "target_id": listing.target_id},
            )
        self.ui.click(app)
        return self._settled_snapshot()

    def _directory(self) -> Snapshot:
        return self._navigate_snapshot(
            APP_DIRECTORY_URL,
            route_ok=lambda value: value.startswith(APP_DIRECTORY_URL),
            error_code="APP_DIRECTORY_ROUTE_NOT_READY",
        )

    def expected_registration_for_scope(self, app_name: str, project_root: str) -> dict[str, Any] | None:
        root = Path(project_root).expanduser().resolve()
        scopes = [root]
        if root.anchor:
            drive_root = Path(root.anchor).resolve()
            if drive_root != root:
                scopes.append(drive_root)
        registry = self.registry.load_registry()
        projects = registry.get("projects") if isinstance(registry.get("projects"), dict) else {}
        for scope in scopes:
            wanted = os.path.normcase(str(scope))
            matches: list[dict[str, Any]] = []
            for key, item in projects.items():
                if not isinstance(item, dict):
                    continue
                try:
                    normalized_key = os.path.normcase(str(Path(str(key)).expanduser().resolve()))
                except (OSError, RuntimeError, ValueError):
                    continue
                if (
                    normalized_key == wanted
                    and str(item.get("app_name") or "") == app_name
                    and str(item.get("status") or "active") == "active"
                    and str(item.get("public_url") or "")
                ):
                    matches.append(
                        {
                            "root": str(scope),
                            "app_name": app_name,
                            "public_url": str(item["public_url"]),
                            "port": item.get("port"),
                        }
                    )
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise AppBridgeError(
                    "APP_REGISTRY_SCOPE_AMBIGUOUS",
                    "multiple active registry URLs match the exact app scope",
                    {"app_name": app_name, "scope": str(scope), "match_count": len(matches)},
                )
        return None

    def expected_url_for_scope(self, app_name: str, project_root: str) -> str | None:
        registration = self.expected_registration_for_scope(app_name, project_root)
        return str(registration.get("public_url") or "") if registration else None

    @staticmethod
    def _snapshot_full_access_selected(detail: Snapshot) -> bool:
        selected = detail.exact_prefix(
            roles={"radio", "option"},
            names=("Allow all actions", "모든 액션 허용"),
            required=False,
        )
        if selected is not None and selected.checked is True:
            return True
        summary = detail.exact_prefix(
            roles={"button"},
            names=("Permissions", "권한", "Allow all actions", "모든 액션 허용"),
            required=False,
        )
        if summary is not None and (
            "allow all actions" in summary.name.casefold() or "모든 액션 허용" in summary.name
        ):
            return True
        return False

    def _full_access_selected(self, detail: Snapshot) -> bool:
        if self._snapshot_full_access_selected(detail):
            return True
        dom_reader = getattr(self.ui, "dom", None)
        if not callable(dom_reader):
            return False
        try:
            dom = dom_reader('[role="radio"][aria-checked="true"][value="full_access"]', max_chars=5_000)
        except AppBridgeError:
            return False
        return 'aria-checked="true"' in dom and 'value="full_access"' in dom

    def inspect(self, app_name: str, *, expected_url: str | None = None) -> dict[str, Any]:
        return self._run_with_utility_target(
            lambda: self._inspect_on_utility_target(app_name, expected_url=expected_url)
        )

    def _inspect_on_utility_target(self, app_name: str, *, expected_url: str | None = None) -> dict[str, Any]:
        listing = self._settings()
        app = listing.exact_app(app_name, required=False)
        # The hash route can become current before the installed-plugin list
        # has hydrated.  A single route-ready snapshot is therefore not
        # sufficient evidence that an exact app is absent.
        for _ in range(5):
            if app is not None:
                break
            settle = getattr(self.ui, "settle", None)
            if callable(settle):
                settle()
            listing = self.ui.snapshot()
            app = listing.exact_app(app_name, required=False)
        if app is None:
            return {"ok": True, "state": "missing", "app_name": app_name, "target_id": listing.target_id}
        self.ui.click(app)
        settle = getattr(self.ui, "settle", None)
        if callable(settle):
            settle()
        detail = self.ui.snapshot()
        disconnect = detail.exact(
            roles={"button", "menuitem"},
            names=("Disconnect", "연결 해제"),
            required=False,
        )
        connect = detail.exact(
            roles={"button", "link"},
            names=("Connect", "연결", "Connect app", "연결하기"),
            required=False,
        )
        permission_entry = detail.exact_prefix(
            roles={"button"},
            names=("Permissions", "권한"),
            required=False,
        )
        permission_detail = detail
        if permission_entry is not None and not self._snapshot_full_access_selected(detail):
            # Refs are single-snapshot capabilities.  Mutate before any DOM
            # read or second snapshot can invalidate this exact ref.
            self.ui.click(permission_entry)
            permission_detail = self._settled_snapshot()
        full_access = self._full_access_selected(permission_detail)
        connected = bool(
            disconnect
            or f"{app_name}에 연결됨" in detail.text_summary
            or f"Connected to {app_name}" in detail.text_summary
            or (permission_entry is not None and connect is None)
        )

        # URL inspection is deliberately last: get-dom may invalidate all
        # refs from the preceding accessibility snapshot.
        url = detail.exact_value(names=("Server URL", "서버 URL", "MCP Server URL", "MCP 서버 URL"))
        url_source = "ui-field" if url else None
        if not url and expected_url and expected_url in detail.text_summary:
            url = expected_url
            url_source = "exact-detail-text-match"
        if not url and expected_url:
            try:
                detail_dom = self.ui.dom("body", max_chars=1_000_000)
            except AppBridgeError:
                detail_dom = ""
            if expected_url in detail_dom:
                url = expected_url
                url_source = "exact-detail-dom-match"
        return {
            "ok": True,
            "state": "detail",
            "app_name": app_name,
            "url": url,
            "connected": connected,
            "full_access": full_access,
            "url_source": url_source,
            "target_id": detail.target_id,
            "route": detail.url,
        }

    @staticmethod
    def _composer_textbox_matches(page: Snapshot) -> list[Node]:
        names = {
            "chatgpt와 채팅",
            "message chatgpt",
            "ask chatgpt",
            "chat with chatgpt",
        }
        return [
            node
            for node in page.nodes
            if node.role == "textbox" and node.name.casefold() in names
        ]

    @staticmethod
    def _known_rate_limit_fixture(page: Snapshot) -> bool:
        text = page.text_summary.casefold()
        return any(
            marker in text
            for marker in (
                "rate limit",
                "reached the limit",
                "한도에 도달",
                "요청 한도",
            )
        )

    def _dismiss_exact_rate_limit_ack(
        self,
        page: Snapshot,
        *,
        target_id: str,
    ) -> bool:
        if page.target_id != target_id or not self._known_rate_limit_fixture(page):
            return False
        matches = [
            node
            for node in page.nodes
            if node.role == "button" and node.name == "알겠습니다"
        ]
        if len(matches) != 1:
            return False
        self.ui.click(matches[0])
        return True

    def _fresh_composer_textbox(
        self,
        target_id: str,
        *,
        attempts: int = 3,
    ) -> tuple[Node, Snapshot, dict[str, Any]]:
        settle = getattr(self.ui, "settle", None)
        ambiguity_counts: list[int] = []
        rate_limit_dismissed = False
        target_mismatches: list[str] = []
        for attempt in range(1, max(1, min(3, attempts)) + 1):
            self.ui.activate_target(target_id)
            if callable(settle):
                settle()
            try:
                page = self.ui.snapshot()
            except AppBridgeError as exc:
                if exc.code != "APP_OBSERVATION_TARGET_DRIFT":
                    raise
                target_mismatches.append(str(exc.evidence.get("active_target_id") or "unknown"))
                ambiguity_counts.append(-1)
                continue
            if page.target_id != target_id:
                target_mismatches.append(page.target_id)
                ambiguity_counts.append(-1)
                continue
            if (
                not page.url.startswith("https://chatgpt.com/")
                or "#settings" in page.url.casefold()
            ):
                raise AppBridgeError(
                    "APP_COMPOSER_ROUTE_INVALID",
                    "fresh composer snapshot was not on a ChatGPT composer route",
                    {"target_id": target_id, "url": page.url, "snapshot_attempts": attempt},
                )
            matches = self._composer_textbox_matches(page)
            ambiguity_counts.append(len(matches))
            if len(matches) == 1:
                return matches[0], page, {
                    "snapshot_attempts": attempt,
                    "ambiguity_counts": ambiguity_counts,
                    "rate_limit_dismissed": rate_limit_dismissed,
                }
            if (
                not rate_limit_dismissed
                and self._dismiss_exact_rate_limit_ack(page, target_id=target_id)
            ):
                rate_limit_dismissed = True
        if target_mismatches and len(target_mismatches) == len(ambiguity_counts):
            raise AppBridgeError(
                "APP_COMPOSER_TARGET_MISMATCH",
                "fresh composer snapshots did not belong to the run-owned target",
                {
                    "expected_target_id": target_id,
                    "actual_target_ids": target_mismatches,
                    "snapshot_attempts": len(ambiguity_counts),
                },
            )
        raise AppBridgeError(
            "APP_UI_DRIFT",
            "composer textbox was not uniquely available after bounded fresh snapshots",
            {
                "target_id": target_id,
                "snapshot_attempts": len(ambiguity_counts),
                "ambiguity_counts": ambiguity_counts,
                "rate_limit_dismissed": rate_limit_dismissed,
            },
        )

    def prepare_composer_app(self, app_name: str, *, composer_url: str = COMPOSER_URL) -> dict[str, Any]:
        if not composer_url.startswith("https://chatgpt.com/"):
            raise AppBridgeError("APP_COMPOSER_URL_INVALID", "composer URL must use the exact https://chatgpt.com/ origin")
        started = time.monotonic()
        starting_command_count = int(getattr(self.ui, "command_count", 0))
        self.ui.ensure_started()
        opener = getattr(self.ui, "open_composer_target", None)
        created = opener(composer_url) if callable(opener) else self.ui.new_tab(composer_url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        new_target_proven = bool(created.get("newTargetProven"))
        if not target_id:
            raise AppBridgeError("APP_COMPOSER_TARGET_MISSING", "new composer tab did not return a target id")
        try:
            settle = getattr(self.ui, "settle", None)
            mention_text = f"@{app_name}"
            textbox, _, resolution = self._fresh_composer_textbox(target_id, attempts=3)
            # Refs are snapshot capabilities.  Type immediately without a DOM
            # read or second snapshot that could invalidate this exact ref.
            try:
                self.ui.type(textbox, mention_text)
            except AppBridgeError as type_error:
                if type_error.code != "APP_AGBROWSE_COMMAND_FAILED":
                    raise
                retry_textbox, _, retry_resolution = self._fresh_composer_textbox(target_id, attempts=2)
                observed_value = str(retry_textbox.value or "").strip()
                if observed_value == mention_text:
                    # The first command may have reached the page even though
                    # its transport response failed.  Do not duplicate input.
                    pass
                elif observed_value:
                    raise AppBridgeError(
                        "APP_COMPOSER_TEXTBOX_STATE_AMBIGUOUS",
                        "composer textbox changed after a transient type failure",
                        {"value_sha256": hashlib.sha256(observed_value.encode("utf-8")).hexdigest()},
                    ) from type_error
                else:
                    self.ui.type(retry_textbox, mention_text)
                resolution = {
                    "snapshot_attempts": int(resolution["snapshot_attempts"]) + int(retry_resolution["snapshot_attempts"]),
                    "ambiguity_counts": [*resolution["ambiguity_counts"], *retry_resolution["ambiguity_counts"]],
                    "rate_limit_dismissed": bool(resolution["rate_limit_dismissed"] or retry_resolution["rate_limit_dismissed"]),
                }
            if callable(settle):
                settle()
            self.ui.press("Tab")
            return {
                "ok": True,
                "state": "composer-app-mention-tab-confirmed",
                "app_name": app_name,
                "target_id": target_id,
                "url": composer_url,
                "selection_method": "exact-at-mention-then-tab",
                "mention_text_sha256": hashlib.sha256(mention_text.encode("utf-8")).hexdigest(),
                "new_target_proven": new_target_proven,
                "textbox_resolution_attempts": resolution["snapshot_attempts"],
                "textbox_ambiguity_counts": resolution["ambiguity_counts"],
                "rate_limit_dismissed": resolution["rate_limit_dismissed"],
                "duration_ms": round((time.monotonic() - started) * 1000),
                "agbrowse_command_count": max(
                    0,
                    int(getattr(self.ui, "command_count", starting_command_count)) - starting_command_count,
                ),
            }
        except AppBridgeError as exc:
            raise AppBridgeError(
                exc.code,
                str(exc),
                {
                    **exc.evidence,
                    "owned_target_id": target_id,
                    "owned_stage": "pre-submit-composer",
                    "new_target_proven": new_target_proven,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "agbrowse_command_count": max(
                        0,
                        int(getattr(self.ui, "command_count", starting_command_count)) - starting_command_count,
                    ),
                },
            ) from exc
        except Exception as exc:
            raise AppBridgeError(
                "APP_COMPOSER_PREP_INTERNAL",
                "composer preparation failed after creating a tab",
                {
                    "owned_target_id": target_id,
                    "owned_stage": "pre-submit-composer",
                    "new_target_proven": new_target_proven,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "agbrowse_command_count": max(
                        0,
                        int(getattr(self.ui, "command_count", starting_command_count)) - starting_command_count,
                    ),
                },
            ) from exc

    def prepare_connected_app_chat(self, app_name: str, *, composer_url: str = COMPOSER_URL) -> dict[str, Any]:
        if not composer_url.startswith("https://chatgpt.com/"):
            raise AppBridgeError("APP_COMPOSER_URL_INVALID", "composer URL must use the exact https://chatgpt.com/ origin")
        self.ui.ensure_started()
        created = self.ui.new_tab(composer_url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        if not target_id:
            raise AppBridgeError("APP_COMPOSER_TARGET_MISSING", "new connected-app tab did not return a target id")
        try:
            settle = getattr(self.ui, "settle", None)
            if callable(settle):
                settle()
            page = self.ui.snapshot()
            chat = page.exact(roles={"radio"}, names=("Chat",), required=False)
            if chat is not None:
                self.ui.click(chat)
                if callable(settle):
                    settle()
                page = self.ui.snapshot()
            page.exact(
                roles={"textbox"},
                names=("ChatGPT와 채팅", "Message ChatGPT", "Ask ChatGPT", "Chat with ChatGPT"),
            )
            return {
                "ok": True,
                "state": "connected-app-chat-ready",
                "app_name": app_name,
                "target_id": target_id,
                "url": composer_url,
            }
        except AppBridgeError as exc:
            raise AppBridgeError(
                exc.code,
                str(exc),
                {**exc.evidence, "owned_target_id": target_id, "owned_stage": "pre-submit-connected-app"},
            ) from exc
        except Exception as exc:
            raise AppBridgeError(
                "APP_CHAT_SURFACE_PREP_INTERNAL",
                "connected-app preparation failed after creating a tab",
                {"owned_target_id": target_id, "owned_stage": "pre-submit-connected-app"},
            ) from exc

    def prepare_plain_composer(self, *, composer_url: str = COMPOSER_URL) -> dict[str, Any]:
        """Create one exact run-owned composer without selecting an app."""
        if not composer_url.startswith("https://chatgpt.com/"):
            raise AppBridgeError("APP_COMPOSER_URL_INVALID", "composer URL must use the exact https://chatgpt.com/ origin")
        self.ui.ensure_started()
        created = self.ui.open_composer_target(composer_url)
        target_id = str(created.get("targetId") or created.get("target_id") or "")
        new_target_proven = bool(created.get("newTargetProven"))
        if not target_id or not new_target_proven:
            raise AppBridgeError(
                "APP_COMPOSER_TARGET_UNPROVEN",
                "plain composer must be a newly proven run-owned target",
                {"target_id": target_id, "new_target_proven": new_target_proven},
            )
        try:
            self.ui.activate_target(target_id)
            return {
                "ok": True,
                "state": "plain-composer-ready",
                "target_id": target_id,
                "url": composer_url,
                "new_target_proven": True,
            }
        except Exception as exc:
            raise AppBridgeError(
                "APP_COMPOSER_TARGET_MISMATCH",
                "plain composer target could not be activated exactly",
                {
                    "owned_target_id": target_id,
                    "owned_stage": "pre-submit-plain-composer",
                    "new_target_proven": True,
                },
            ) from exc

    def activate_composer_target(self, target_id: str) -> dict[str, Any]:
        return self.ui.activate_target(target_id)

    def _fill_create_form(self, decision: dict[str, Any]) -> None:
        page = self._directory()
        create = page.exact(
            roles={"button", "link"},
            names=("Create app", "앱 만들기"),
            required=False,
        )
        if create is None:
            developer_mode = page.exact_prefix(
                roles={"switch", "checkbox", "button"},
                names=("Developer mode", "개발자 모드", "Advanced settings", "고급 설정"),
                required=False,
            )
            raise AppBridgeError(
                "CHATGPT_DEVELOPER_MODE_REQUIRED",
                (
                    "custom app creation is unavailable; enable ChatGPT Developer Mode before "
                    "registering the CodexPro app"
                ),
                {
                    "settings_path": "Settings > Apps > Advanced settings > Developer mode",
                    "workspace_admin_path": "Workspace settings > Apps > Create",
                    "developer_mode_control_visible": developer_mode is not None,
                    "account_note": (
                        "If the toggle or Create app control is absent, the current account or "
                        "workspace has not granted custom MCP app access. Ask an admin/owner or "
                        "use an eligible account; do not retry registration blindly."
                    ),
                    "target_id": page.target_id,
                    "url": page.url,
                },
            )
        self.ui.click(create)
        form = self._settled_snapshot()
        fields = (
            (("Name", "이름", "App name", "앱 이름"), str(decision["app_name"])),
            (("Description", "Description (optional)", "설명", "설명 (선택)"), "Codex project connector"),
            (("Server URL", "서버 URL", "MCP Server URL", "MCP 서버 URL"), str(decision["public_url"])),
        )
        for labels, value in fields:
            self.ui.type(form.exact(roles={"textbox"}, names=labels), value)
        auth = form.exact(roles={"combobox", "listbox"}, names=("Authentication", "인증"), required=False)
        if auth:
            no_auth = form.exact(roles={"option"}, names=("No authentication", "인증 없음"))
            self.ui.select(auth, no_auth.name)
        trust = form.exact(
            roles={"checkbox"},
            names=(
                "I trust this application",
                "이 애플리케이션을 신뢰합니다",
                "Trust this app",
                "앱 신뢰",
                "내용을 이해했으며 계속 진행하길 원합니다 OpenAI가 이 MCP 서버를 검토하지 않았습니다. 공격자들이 데이터를 훔치려 하거나 모델을 속여 의도하지 않은 작업을 하게 만들 수 있으며 여기에는 데이터를 파괴하는 행위가 포함됩니다.",
            ),
            required=False,
        )
        if trust and trust.checked is not True:
            self.ui.check(trust)
        refreshed = self._settled_snapshot()
        submit = refreshed.exact(roles={"button"}, names=("Create", "만들기", "Save", "저장"))
        self.ui.click(submit)
        # Creation is asynchronous on the current Connectors UI.  Do not let
        # the next stage interpret the still-open creation form as app detail.
        for _ in range(8):
            current = self._settled_snapshot()
            name_field = current.exact(
                roles={"textbox"},
                names=("Name", "이름", "App name", "앱 이름"),
                required=False,
            )
            if name_field is None:
                return
        raise AppBridgeError(
            "APP_CREATE_NOT_CONFIRMED",
            "create form remained open after the app creation request",
            {"app_name": decision.get("app_name")},
        )

    def _connect_and_maximize_permission(self, app_name: str) -> None:
        detail = self._settings_detail(app_name)
        connect = detail.exact(
            roles={"button", "link"},
            names=("Connect", "연결", "Connect app", "연결하기"),
            required=False,
        )
        if connect:
            self.ui.click(connect)
            # The current Connectors UI uses a second confirmation modal.  The
            # detail control is "연결/Connect" while the modal is
            # "연결하기/Connect app" in the observed locale.
            modal = self._settled_snapshot()
            confirm_connect = modal.exact(
                roles={"button", "link"},
                names=("Connect app", "연결하기", "Confirm connection"),
                required=False,
            )
            if confirm_connect is not None:
                self.ui.click(confirm_connect)
                self._settled_snapshot()
            # Return to the exact settings entry before touching permissions;
            # this also verifies that the modal mutation persisted.
            detail = self._settings_detail(app_name)
        if self._snapshot_full_access_selected(detail):
            return
        permission_entry = detail.exact_prefix(
            roles={"button", "link"},
            names=("Permissions", "권한"),
        )
        self.ui.click(permission_entry)
        maximum = None
        for _ in range(8):
            detail = self._settled_snapshot()
            maximum = detail.exact_prefix(
                roles={"radio", "option", "button"},
                names=("Allow all actions", "모든 액션 허용"),
                required=False,
            )
            if maximum is not None:
                break
        if maximum is None:
            raise AppBridgeError(
                "APP_PERMISSION_CONTROL_MISSING",
                "full_access permission control did not appear after opening Permissions",
                {"app_name": app_name, "target_id": detail.target_id},
            )
        if not self._snapshot_full_access_selected(detail):
            self.ui.click(maximum)
            for _ in range(8):
                detail = self._settled_snapshot()
                if self._full_access_selected(detail):
                    break
        if not self._full_access_selected(detail):
            raise AppBridgeError(
                "APP_PERMISSION_NOT_CONFIRMED",
                "full_access was not selected after the permission mutation",
                {"app_name": app_name, "target_id": detail.target_id},
            )

    def _delete_retired(self, app_name: str) -> dict[str, Any]:
        listing = self._settings()
        app = listing.exact_app(app_name, required=False)
        if not app:
            absence_confirmations = 1
            for _ in range(5):
                listing = self._settled_snapshot()
                if listing.exact_app(app_name, required=False):
                    raise AppBridgeError(
                        "APP_OLD_DELETE_NOT_CONFIRMED",
                        "retired app appeared while confirming prior absence",
                        {"app_name": app_name, "absence_confirmations": absence_confirmations},
                    )
                absence_confirmations += 1
            return {
                "ok": True,
                "state": "already-absent",
                "app_name": app_name,
                "absence_confirmations": absence_confirmations,
            }
        self.ui.click(app)
        detail = self._settled_snapshot()
        more = detail.exact(
            roles={"button"},
            names=(
                "More",
                "More actions",
                "더 보기",
                "추가 작업",
                "Actions",
                "작업",
                "Plugin actions",
                "플러그인 작업",
            ),
        )
        self.ui.click(more)
        menu = self._settled_snapshot()
        delete = menu.exact(roles={"menuitem"}, names=("Delete", "삭제"))
        self.ui.click(delete)
        post_delete = self._settled_snapshot()
        confirm = post_delete.exact(roles={"button"}, names=("Delete", "삭제"), required=False)
        if confirm is not None:
            self.ui.click(confirm)
            self._settled_snapshot()
        final = self._settings()
        absence_confirmations = 0
        for _ in range(6):
            if final.exact_app(app_name, required=False):
                raise AppBridgeError(
                    "APP_OLD_DELETE_NOT_CONFIRMED",
                    "retired app remained visible after delete",
                    {"app_name": app_name, "absence_confirmations": absence_confirmations},
                )
            absence_confirmations += 1
            if absence_confirmations < 6:
                final = self._settled_snapshot()
        return {
            "ok": True,
            "state": "deleted-and-not-visible",
            "app_name": app_name,
            "absence_confirmations": absence_confirmations,
        }

    def _cleanup_pending_retired(self, decision: dict[str, Any]) -> list[dict[str, Any]]:
        root = str(decision.get("root") or "")
        active_name = str(decision.get("app_name") or "")
        registry = self.registry.load_registry()
        pending = [
            item
            for item in (registry.get("retired_apps") or [])
            if isinstance(item, dict)
            and str(item.get("root") or "") == root
            and str(item.get("superseded_by") or "") == active_name
            and str(item.get("status") or "") == "retire-pending"
        ]
        names = [str(item.get("app_name") or "") for item in pending]
        if not names:
            return []
        active = (registry.get("projects") or {}).get(root)
        if not isinstance(active, dict) or str(active.get("app_name") or "") != active_name:
            raise AppBridgeError(
                "APP_RETIRE_ACTIVE_OWNERSHIP_UNPROVEN",
                "retired-app recovery requires the exact active registry owner",
                {"root": root, "app_name": active_name},
            )
        if any(not name or name == active_name for name in names) or len(set(names)) != len(names):
            raise AppBridgeError(
                "APP_RETIRE_RECORD_AMBIGUOUS",
                "retired-app recovery records were empty, duplicated, or targeted the active app",
                {"root": root, "app_name": active_name, "record_count": len(names)},
            )
        results: list[dict[str, Any]] = []
        for old_name in names:
            cleanup = self._delete_retired(old_name)
            cleanup_decision = {**decision, "old_app_name": old_name}
            recorded = self.registry.record_retired_cleanup(cleanup_decision, cleanup)
            if recorded.get("ok") is not True:
                raise AppBridgeError("APP_RETIRE_RECORD_FAILED", "retired-app cleanup evidence was not recorded", recorded)
            results.append(cleanup)
        return results

    @staticmethod
    def _registry_confirmation(inspection: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "state": "confirmed-visible",
            "app_name": inspection.get("app_name"),
            "route": inspection.get("route"),
            "connect_confirm": {"ok": inspection.get("connected") is True},
            "final_url_check": {"ok": bool(inspection.get("url")), "url": inspection.get("url")},
            "final_permission_check": {"ok": inspection.get("full_access") is True, "value": "full_access"},
        }

    def reconcile(self, decision: dict[str, Any]) -> dict[str, Any]:
        return self._run_with_utility_target(lambda: self._reconcile_on_utility_target(decision))

    def _reconcile_on_utility_target(self, decision: dict[str, Any]) -> dict[str, Any]:
        if not decision.get("app_name") or not decision.get("public_url"):
            raise AppBridgeError("APP_DECISION_INVALID", "decision requires app_name and full public_url")
        expected_url = str(decision["public_url"])
        current = self.inspect(str(decision["app_name"]), expected_url=expected_url)
        if current.get("state") == "detail" and current.get("url") == expected_url and current.get("connected") and current.get("full_access"):
            confirmed = self._registry_confirmation(current)
            committed = self.registry.record_reconcile_confirmation(decision, confirmed)
            if committed.get("ok") is not True:
                raise AppBridgeError("APP_REGISTRY_CAS_FAILED", "registry reuse confirmation failed", committed)
            retired_cleanup = self._cleanup_pending_retired(decision)
            return {
                "ok": True,
                "phase": "COMPLETE",
                "action": "reuse",
                "inspection": current,
                "retired_cleanup": retired_cleanup,
            }

        started = self.registry.record_reconcile_started(decision)
        if started.get("ok") is not True:
            raise AppBridgeError("APP_REGISTRY_START_FAILED", "registry transaction could not start", started)
        try:
            if current.get("state") == "missing":
                self._fill_create_form(decision)
            self._connect_and_maximize_permission(str(decision["app_name"]))
            verified = self.inspect(str(decision["app_name"]), expected_url=expected_url)
            if not (
                verified.get("url") == expected_url
                and verified.get("connected")
                and verified.get("full_access")
            ):
                raise AppBridgeError("APP_POSTCONDITION_FAILED", "full URL, connection, or full_access verification failed", verified)
            confirmation = self._registry_confirmation(verified)
            committed = self.registry.record_reconcile_confirmation(decision, confirmation)
            if committed.get("ok") is not True:
                raise AppBridgeError("APP_REGISTRY_CAS_FAILED", "candidate registry commit failed", committed)
            cleanup = {"ok": True, "skipped": True, "reason": "no-old-app"}
            old_name = str(decision.get("old_app_name") or "")
            if old_name and old_name != str(decision["app_name"]):
                authorized_old_name = str(committed.get("retired_app_name") or "")
                if not (
                    decision.get("transaction_id")
                    and str(committed.get("action") or "") == "candidate-committed"
                    and str(committed.get("root") or "") == str(decision.get("root") or "")
                    and authorized_old_name == old_name
                ):
                    raise AppBridgeError(
                        "APP_RETIRE_OWNERSHIP_UNPROVEN",
                        "old app cleanup requires a committed candidate transaction for the same registry root",
                        {
                            "app_name": decision.get("app_name"),
                            "old_app_name": old_name,
                            "root": decision.get("root"),
                            "commit_action": committed.get("action"),
                            "authorized_old_app_name": authorized_old_name,
                        },
                    )
                cleanup = self._delete_retired(old_name)
                self.registry.record_retired_cleanup(decision, cleanup)
            return {
                "ok": True,
                "phase": "COMPLETE",
                "action": "reconciled",
                "inspection": verified,
                "commit": committed,
                "cleanup": cleanup,
            }
        except Exception as exc:
            failure = exc.envelope() if isinstance(exc, AppBridgeError) else {"ok": False, "reason": str(exc)}
            self.registry.record_reconcile_failure(decision, failure)
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic CodexPro app connector over unmodified agbrowse commands.")
    parser.add_argument("--executable", default="agbrowse")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect_source = inspect.add_mutually_exclusive_group(required=True)
    inspect_source.add_argument("--app-name")
    inspect_source.add_argument("--root", help="Inspect the active exact app and endpoint stored for this normalized root.")
    reconcile = sub.add_parser("reconcile")
    source = reconcile.add_mutually_exclusive_group(required=True)
    source.add_argument("--decision", type=Path)
    source.add_argument("--root", help="Resume the exact pending candidate for this normalized root without writing a decision file.")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    connector = AppConnector(AgbrowseGateway(executable=args.executable))
    try:
        with exclusive_browser_mutation_lock():
            if args.command == "inspect":
                if args.root:
                    try:
                        decision = APP_MANAGER.decide(
                            root=APP_MANAGER.drive_app_root(Path(args.root)),
                            public_url=None,
                            preferred_port=None,
                            update=False,
                        ).to_dict()
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise AppBridgeError("APP_DECISION_RESOLVE_FAILED", "active app could not be resolved for root", {"root": args.root, "reason": str(exc)}) from exc
                    result = connector.inspect(str(decision["app_name"]), expected_url=str(decision["public_url"]))
                else:
                    result = connector.inspect(args.app_name)
            else:
                if args.root:
                    try:
                        decision = APP_MANAGER.decide(
                            root=APP_MANAGER.drive_app_root(Path(args.root)),
                            public_url=None,
                            preferred_port=None,
                            update=False,
                        ).to_dict()
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise AppBridgeError("APP_DECISION_RESOLVE_FAILED", "pending candidate could not be resolved for root", {"root": args.root, "reason": str(exc)}) from exc
                else:
                    # Windows PowerShell commonly writes UTF-8 JSON with a BOM.
                    # The bootstrap decision is still strict UTF-8 JSON; utf-8-sig
                    # merely consumes that optional marker instead of rejecting it.
                    decision = json.loads(args.decision.read_text(encoding="utf-8-sig"))
                    if not isinstance(decision, dict):
                        raise AppBridgeError("APP_DECISION_INVALID", "decision JSON must be an object")
                    decision = _hydrate_redacted_decision_url(decision)
                result = connector.reconcile(decision)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except AppBridgeError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
