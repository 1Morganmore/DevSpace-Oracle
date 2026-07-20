#!/usr/bin/env python
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from codexpro_mcp_identity import endpoint_key


STATE_DIR = Path(os.environ.get("CODEXPRO_STATE_DIR") or (Path.home() / ".codex" / "state" / "codexpro-project-apps"))
REGISTRY_PATH = STATE_DIR / "registry.json"
REGISTRY_LOCK_PATH = STATE_DIR / "registry.lock"
DEFAULT_PORT_BASE = 8787
DEFAULT_PORT_LIMIT = 8899
APP_PREFIX = "CodexPro"
C_DRIVE_SLUG = "CDrive"
C_DRIVE_ROOTS = {"c:\\", "c:/"}
GENERIC_ROOT_NAMES = {
    "new-chat",
    "chat",
    "project",
    "repo",
    "workspace",
    "codex",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def stable_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def drive_root_letter(path: Path) -> str | None:
    normalized = stable_path(path).replace("/", "\\")
    match = re.fullmatch(r"([A-Za-z]):\\?", normalized)
    return match.group(1).upper() if match else None


def is_cdrive_root(path: Path) -> bool:
    return drive_root_letter(path) == "C"


def git_root(path: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        return None
    root = completed.stdout.strip()
    if completed.returncode == 0 and root:
        try:
            return Path(root).resolve()
        except OSError:
            return Path(root)
    return None


def project_root(path: Path, *, use_git_root: bool = True) -> Path:
    resolved = path.expanduser().resolve()
    if use_git_root:
        found = git_root(resolved)
        if found is not None:
            return found
    return resolved


def drive_app_root(path: Path) -> Path:
    """Return the deterministic per-drive app scope for any local workspace."""
    resolved = path.expanduser().resolve()
    if resolved.anchor:
        return Path(resolved.anchor).resolve()
    return resolved


def drive_tunnel_policy_entry(root: Path) -> dict[str, Any] | None:
    policy_path = REGISTRY_PATH.parent / "drive-tunnel-policy.json"
    if not policy_path.is_file():
        return None
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid drive tunnel policy: {policy_path}") from exc
    drives = policy.get("drives") if isinstance(policy, dict) else None
    if not isinstance(drives, dict):
        return None
    wanted = os.path.normcase(stable_path(drive_app_root(root)))
    matches = [
        dict(value)
        for key, value in drives.items()
        if isinstance(value, dict)
        and os.path.normcase(stable_path(drive_app_root(Path(str(key))))) == wanted
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous drive tunnel policy for {wanted}")
    return matches[0] if matches else None


def enforce_drive_tunnel_policy(root: Path, public_url: str | None) -> None:
    entry = drive_tunnel_policy_entry(root)
    if not entry or str(entry.get("provider") or "").strip().casefold() != "ngrok" or not public_url:
        return
    hostname = str(entry.get("hostname") or "").strip().casefold()
    actual = str(urlparse(public_url).hostname or "").strip().casefold()
    if not hostname or actual != hostname:
        raise RuntimeError(
            "DRIVE_TUNNEL_POLICY_MISMATCH: fixed ngrok drive scope cannot be replaced by a dynamic endpoint"
        )


def ascii_slug(text: str) -> str:
    lowered = text.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug


def readable_slug(text: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "-", text.strip())
    cleaned = re.sub(r"\s+", "-", cleaned).strip(" .-_")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned[:36].strip(" .-_")


def raw_root_slug(root: Path) -> str:
    drive_letter = drive_root_letter(root)
    if drive_letter:
        return f"{drive_letter}Drive"
    name_slug = readable_slug(root.name)
    ascii_name = ascii_slug(root.name)
    parent_slug = readable_slug(root.parent.name) if root.parent and root.parent != root else ""
    if name_slug:
        if ascii_name in GENERIC_ROOT_NAMES and parent_slug and ascii_slug(parent_slug) not in GENERIC_ROOT_NAMES:
            return f"{parent_slug}-{name_slug}"
        return name_slug
    digest = hashlib.sha1(stable_path(root).lower().encode("utf-8")).hexdigest()[:8]
    return f"project-{digest}"


def root_digest(root: Path) -> str:
    return hashlib.sha1(stable_path(root).lower().encode("utf-8")).hexdigest()[:8]


def unique_slug(registry: dict[str, Any], root: Path) -> str:
    candidate = raw_root_slug(root)
    canonical_root = stable_path(root)
    for other_root, entry in registry.get("projects", {}).items():
        if other_root == canonical_root:
            continue
        if str(entry.get("slug") or "") == candidate:
            return f"{candidate}-{root_digest(root)}"
    return candidate


def load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {"schema_version": 2, "projects": {}, "retired_apps": [], "pending_reconciles": {}}
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CodexPro project app registry is unreadable or invalid: {REGISTRY_PATH}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"CodexPro project app registry root must be an object: {REGISTRY_PATH}")
    data["schema_version"] = max(int(data.get("schema_version") or 1), 2)
    data.setdefault("projects", {})
    data.setdefault("retired_apps", [])
    if not isinstance(data.get("pending_reconciles"), dict):
        data["pending_reconciles"] = {}
    return data


@contextmanager
def registry_lock(timeout_seconds: int = 30, lock_path: Path | None = None):
    """Cross-process lock for registry read-modify-write transactions."""
    effective_lock_path = lock_path or REGISTRY_LOCK_PATH
    effective_lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = effective_lock_path.open("a+b")
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    deadline = time.time() + timeout_seconds
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
                    raise TimeoutError(f"timed out waiting for registry lock: {effective_lock_path}")
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


def write_registry(data: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = REGISTRY_PATH.with_name(
        f"{REGISTRY_PATH.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(REGISTRY_PATH)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.15)
        return sock.connect_ex(("127.0.0.1", int(port))) == 0


def used_ports(registry: dict[str, Any]) -> set[int]:
    ports: set[int] = set()
    for entry in registry.get("projects", {}).values():
        try:
            ports.add(int(entry.get("port")))
        except (TypeError, ValueError):
            continue
    for transaction in registry.get("pending_reconciles", {}).values():
        if not isinstance(transaction, dict):
            continue
        candidate = transaction.get("candidate") if isinstance(transaction.get("candidate"), dict) else {}
        try:
            ports.add(int(candidate.get("port")))
        except (TypeError, ValueError):
            continue
    return ports


def allocate_port(registry: dict[str, Any], preferred: int | None = None) -> int:
    candidates: list[int] = []
    if preferred and not port_open(preferred):
        candidates.append(preferred)
    candidates.extend(range(DEFAULT_PORT_BASE, DEFAULT_PORT_LIMIT + 1))
    reserved = used_ports(registry)
    for port in candidates:
        if port < 1:
            continue
        if port not in reserved and not port_open(port):
            return port
    raise RuntimeError(f"No free CodexPro port found in {DEFAULT_PORT_BASE}-{DEFAULT_PORT_LIMIT}.")


def app_name(slug: str, version: int) -> str:
    return f"{APP_PREFIX}-{slug}-v{version:02d}"


def normalize_public_url(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not re.match(r"^https?://", text, re.I):
        text = f"https://{text}"
    text = text.rstrip("/")
    parsed = urlparse(text)
    query = parse_qs(parsed.query, keep_blank_values=False)
    token = (query.get("codexpro_token") or [""])[0].strip()
    if parsed.path.rstrip("/") != "/mcp" or not token:
        raise ValueError("CodexPro public URL must include /mcp?codexpro_token=...")
    return text


def endpoint_collision(registry: dict[str, Any], root: str, public_url: str | None) -> dict[str, Any] | None:
    if not public_url:
        return None
    try:
        key = endpoint_key(public_url)
    except Exception as exc:
        raise ValueError(f"Invalid CodexPro endpoint: {exc}") from exc
    for other_root, entry in registry.get("projects", {}).items():
        if other_root == root or not isinstance(entry, dict):
            continue
        other_url = entry.get("public_url")
        if not other_url:
            continue
        try:
            other_key = endpoint_key(other_url)
        except Exception:
            continue
        if other_key == key:
            return {
                "root": other_root,
                "app_name": entry.get("app_name"),
                "port": entry.get("port"),
                "endpoint_key": key,
            }
    return None


@dataclass
class Decision:
    action: str
    root: str
    slug: str
    app_name: str
    version: int
    port: int
    public_url: str | None
    old_app_name: str | None
    old_public_url: str | None
    chrome_next_action: str
    transaction_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "root": self.root,
            "slug": self.slug,
            "app_name": self.app_name,
            "version": self.version,
            "port": self.port,
            "public_url": self.public_url,
            "old_app_name": self.old_app_name,
            "old_public_url": self.old_public_url,
            "chrome_next_action": self.chrome_next_action,
            "transaction_id": self.transaction_id,
        }


def _candidate_entry(decision: Decision | dict[str, Any]) -> dict[str, Any]:
    value = decision.to_dict() if isinstance(decision, Decision) else dict(decision)
    return {
        "slug": value.get("slug"),
        "app_name": value.get("app_name"),
        "version": value.get("version"),
        "port": value.get("port"),
        "public_url": value.get("public_url"),
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "candidate",
        "chrome_status": "unconfirmed",
    }


def _stage_reconcile(
    registry: dict[str, Any],
    decision: Decision,
    *,
    old_active: dict[str, Any] | None,
) -> str:
    """Persist a candidate before any account-level Developer App UI mutation."""
    pending = registry.setdefault("pending_reconciles", {})
    candidate = _candidate_entry(decision)
    for transaction_id, transaction in pending.items():
        if not isinstance(transaction, dict):
            continue
        existing_candidate = transaction.get("candidate") if isinstance(transaction.get("candidate"), dict) else {}
        if (
            transaction.get("root") == decision.root
            and existing_candidate.get("app_name") == candidate.get("app_name")
            and existing_candidate.get("public_url") == candidate.get("public_url")
            and transaction.get("phase") in {"prepared", "recovery-required"}
        ):
            transaction["candidate"] = candidate
            transaction["updated_at"] = now_iso()
            transaction["phase"] = "prepared"
            return str(transaction_id)
    transaction_id = uuid.uuid4().hex
    pending[transaction_id] = {
        "transaction_id": transaction_id,
        "root": decision.root,
        "action": decision.action,
        "old_active": dict(old_active or {}) or None,
        "candidate": candidate,
        "phase": "prepared",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    return transaction_id


def _confirmation_summary(result: dict[str, Any]) -> dict[str, Any]:
    final_url = result.get("final_url_check") if isinstance(result.get("final_url_check"), dict) else {}
    final_permission = result.get("final_permission_check") if isinstance(result.get("final_permission_check"), dict) else {}
    connect = result.get("connect_confirm") if isinstance(result.get("connect_confirm"), dict) else {}
    return {
        "ok": bool(result.get("ok")),
        "state": str(result.get("state") or ""),
        "app_name": str(result.get("app_name") or ""),
        "final_url_ok": bool(final_url.get("ok")),
        "final_permission_ok": bool(final_permission.get("ok")),
        "connect_ok": bool(connect.get("ok")) if connect else None,
        "updated_at": now_iso(),
    }


def _result_confirms_candidate(decision: dict[str, Any], result: dict[str, Any]) -> bool:
    final_url = result.get("final_url_check") if isinstance(result.get("final_url_check"), dict) else {}
    final_permission = result.get("final_permission_check") if isinstance(result.get("final_permission_check"), dict) else {}
    connect = result.get("connect_confirm") if isinstance(result.get("connect_confirm"), dict) else {}
    return bool(
        result.get("ok") is True
        and result.get("state") == "confirmed-visible"
        and str(result.get("app_name") or "") == str(decision.get("app_name") or "")
        and final_url.get("ok") is True
        and str(final_url.get("url") or "") == str(decision.get("public_url") or "")
        and connect.get("ok") is True
        and final_permission.get("ok") is True
    )


def _active_entry_matches(expected: dict[str, Any], current: dict[str, Any] | None) -> bool:
    if not isinstance(current, dict):
        return False
    return (
        str(current.get("app_name") or "") == str(expected.get("app_name") or "")
        and str(current.get("public_url") or "") == str(expected.get("public_url") or "")
    )


def _append_retired_pending_cleanup(
    registry: dict[str, Any],
    *,
    root: str,
    old_active: dict[str, Any],
    candidate: dict[str, Any],
    reason: str,
) -> None:
    old_name = str(old_active.get("app_name") or "")
    if not old_name:
        return
    registry.setdefault("retired_apps", []).append(
        {
            "root": root,
            "app_name": old_name,
            "public_url": old_active.get("public_url"),
            "status": "retire-pending",
            "retired_at": now_iso(),
            "superseded_by": candidate.get("app_name"),
            "reason": reason,
        }
    )
    registry["retired_apps"] = registry["retired_apps"][-200:]


def _append_reconcile_history(registry: dict[str, Any], transaction: dict[str, Any], *, terminal_phase: str) -> None:
    history = registry.setdefault("reconcile_history", [])
    history.append(
        {
            "transaction_id": transaction.get("transaction_id"),
            "root": transaction.get("root"),
            "action": transaction.get("action"),
            "candidate_app_name": (transaction.get("candidate") or {}).get("app_name"),
            "phase": terminal_phase,
            "created_at": transaction.get("created_at"),
            "updated_at": now_iso(),
        }
    )
    registry["reconcile_history"] = history[-200:]


def record_reconcile_failure(decision: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Keep a failed candidate journalled without changing the active pointer."""
    transaction_id = str(decision.get("transaction_id") or "")
    if not transaction_id:
        return {"ok": True, "skipped": True, "reason": "no-pending-reconcile"}
    with registry_lock():
        registry = load_registry()
        transaction = registry.get("pending_reconciles", {}).get(transaction_id)
        if not isinstance(transaction, dict):
            return {"ok": False, "reason": "pending-reconcile-not-found", "transaction_id": transaction_id}
        transaction["phase"] = "recovery-required"
        transaction["last_result"] = _confirmation_summary(result)
        transaction["last_error"] = str(result.get("error") or result.get("state") or "reconcile-failed")[:1000]
        transaction["updated_at"] = now_iso()
        write_registry(registry)
    return {"ok": True, "transaction_id": transaction_id, "phase": "recovery-required"}


def record_reconcile_started(decision: dict[str, Any]) -> dict[str, Any]:
    """Fence a staged candidate before the browser begins account UI work."""
    transaction_id = str(decision.get("transaction_id") or "")
    if not transaction_id:
        return {"ok": True, "skipped": True, "reason": "no-pending-reconcile"}
    with registry_lock():
        registry = load_registry()
        transaction = registry.get("pending_reconciles", {}).get(transaction_id)
        if not isinstance(transaction, dict):
            return {"ok": False, "reason": "pending-reconcile-not-found", "transaction_id": transaction_id}
        if transaction.get("phase") not in {"prepared", "recovery-required"}:
            return {"ok": False, "reason": "pending-reconcile-not-startable", "phase": transaction.get("phase")}
        transaction["phase"] = "ui-in-progress"
        transaction["updated_at"] = now_iso()
        write_registry(registry)
    return {"ok": True, "transaction_id": transaction_id, "phase": "ui-in-progress"}


def record_reconcile_confirmation(decision: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Commit only a fully confirmed candidate with a compare-and-swap fence."""
    if not _result_confirms_candidate(decision, result):
        return record_reconcile_failure(decision, result)
    transaction_id = str(decision.get("transaction_id") or "")
    with registry_lock():
        registry = load_registry()
        projects = registry.setdefault("projects", {})
        if not transaction_id:
            current = projects.get(str(decision.get("root") or ""))
            if not _active_entry_matches({"app_name": decision.get("app_name"), "public_url": decision.get("public_url")}, current):
                return {"ok": False, "reason": "active-entry-mismatch-on-reuse"}
            current.update(
                {
                    "chrome_status": "confirmed",
                    "chrome_confirmed_at": now_iso(),
                    "chrome_reconcile_state": result.get("state"),
                    "chrome_reconcile_route": result.get("route"),
                    "updated_at": now_iso(),
                }
            )
            write_registry(registry)
            return {"ok": True, "app_name": current.get("app_name"), "action": "reuse-confirmed"}

        transaction = registry.get("pending_reconciles", {}).get(transaction_id)
        if not isinstance(transaction, dict):
            return {"ok": False, "reason": "pending-reconcile-not-found", "transaction_id": transaction_id}
        candidate = transaction.get("candidate") if isinstance(transaction.get("candidate"), dict) else {}
        if (
            transaction.get("root") != decision.get("root")
            or str(candidate.get("app_name") or "") != str(decision.get("app_name") or "")
            or str(candidate.get("public_url") or "") != str(decision.get("public_url") or "")
        ):
            return {"ok": False, "reason": "pending-reconcile-candidate-mismatch", "transaction_id": transaction_id}
        old_active = transaction.get("old_active") if isinstance(transaction.get("old_active"), dict) else None
        current = projects.get(str(decision.get("root") or ""))
        if old_active is not None and not _active_entry_matches(old_active, current):
            transaction["phase"] = "recovery-required"
            transaction["last_error"] = "active-entry-changed-before-candidate-commit"
            transaction["updated_at"] = now_iso()
            write_registry(registry)
            return {"ok": False, "reason": "active-entry-changed-before-candidate-commit", "transaction_id": transaction_id}
        promoted = {
            **candidate,
            "status": "active",
            "chrome_status": "confirmed",
            "chrome_confirmed_at": now_iso(),
            "chrome_reconcile_state": result.get("state"),
            "chrome_reconcile_route": result.get("route"),
            "updated_at": now_iso(),
        }
        projects[str(decision.get("root") or "")] = promoted
        if old_active is not None:
            _append_retired_pending_cleanup(
                registry,
                root=str(decision.get("root") or ""),
                old_active=old_active,
                candidate=promoted,
                reason=str(transaction.get("action") or "replacement"),
            )
        transaction["phase"] = "committed"
        transaction["confirmation"] = _confirmation_summary(result)
        transaction["updated_at"] = now_iso()
        _append_reconcile_history(registry, transaction, terminal_phase="committed")
        registry["pending_reconciles"].pop(transaction_id, None)
        write_registry(registry)
        return {
            "ok": True,
            "app_name": promoted.get("app_name"),
            "root": str(decision.get("root") or ""),
            "transaction_id": transaction_id,
            "action": "candidate-committed",
            "retired_app_name": (old_active or {}).get("app_name"),
        }


def record_retired_cleanup(decision: dict[str, Any], delete_result: dict[str, Any]) -> dict[str, Any]:
    """Record old-app cleanup after the new active pointer has already committed."""
    old_name = str(decision.get("old_app_name") or "")
    new_name = str(decision.get("app_name") or "")
    root = str(decision.get("root") or "")
    if not old_name:
        return {"ok": True, "skipped": True, "reason": "no-old-app"}
    with registry_lock():
        registry = load_registry()
        for item in reversed(registry.get("retired_apps", [])):
            if not isinstance(item, dict):
                continue
            if item.get("root") != root or item.get("app_name") != old_name or item.get("superseded_by") != new_name:
                continue
            if delete_result.get("ok"):
                item["status"] = "confirmed-deleted-or-not-visible"
                item["confirmed_at"] = now_iso()
            else:
                item["status"] = "retire-pending"
                item["last_error"] = str(delete_result.get("reason") or delete_result.get("state") or "delete-failed")[:1000]
                item["updated_at"] = now_iso()
            write_registry(registry)
            return {"ok": True, "app_name": old_name, "status": item.get("status")}
    return {"ok": False, "reason": "retired-app-record-not-found", "app_name": old_name}


def _pending_reconcile_for_root(registry: dict[str, Any], root: str) -> tuple[str, dict[str, Any]] | None:
    for transaction_id, transaction in registry.get("pending_reconciles", {}).items():
        if not isinstance(transaction, dict) or transaction.get("root") != root:
            continue
        return str(transaction_id), transaction
    return None


def decide(
    *,
    root: Path,
    public_url: str | None,
    preferred_port: int | None,
    update: bool,
    force_recreate: bool = False,
    verified_open_port: bool = False,
    rebind_pending_after_app_absence: bool = False,
) -> Decision:
    registry = load_registry()
    canonical_root = stable_path(root)
    slug = unique_slug(registry, Path(canonical_root))
    projects = registry.setdefault("projects", {})
    existing = projects.get(canonical_root)
    normalized_url = normalize_public_url(public_url)
    enforce_drive_tunnel_policy(Path(canonical_root), normalized_url)
    collision = endpoint_collision(registry, canonical_root, normalized_url)
    if collision and update:
        raise RuntimeError(
            "Refusing to assign CodexPro endpoint already registered to another root: "
            + json.dumps(collision, ensure_ascii=False)
        )

    pending = _pending_reconcile_for_root(registry, canonical_root)
    if pending is not None:
        transaction_id, transaction = pending
        candidate = transaction.get("candidate") if isinstance(transaction.get("candidate"), dict) else {}
        old_active = transaction.get("old_active") if isinstance(transaction.get("old_active"), dict) else None
        # One root may have at most one candidate transaction.  Do not let a
        # still-active old app make a failed or interrupted replacement look
        # like a blank slot: that would permit a second candidate and make the
        # account/UI state impossible to reconcile safely.
        if old_active is not None and not _active_entry_matches(old_active, existing):
            raise RuntimeError(
                "RECOVERY_REQUIRED: active CodexPro app changed while a candidate replacement is pending; "
                "resolve the recorded transaction before any new app decision"
            )
        if old_active is None and existing is not None:
            raise RuntimeError(
                "RECOVERY_REQUIRED: an unexpected active CodexPro app exists beside a create candidate; "
                "resolve the recorded transaction before any new app decision"
            )
        candidate_url = normalize_public_url(candidate.get("public_url")) if candidate.get("public_url") else None
        candidate_port = int(candidate.get("port") or preferred_port or DEFAULT_PORT_BASE)
        if preferred_port and verified_open_port and candidate_port != int(preferred_port):
            candidate_port = int(preferred_port)
            if update:
                candidate["port"] = candidate_port
                candidate["updated_at"] = now_iso()
                transaction["candidate"] = candidate
                transaction["updated_at"] = now_iso()
                write_registry(registry)
        if normalized_url and candidate_url and normalized_url != candidate_url:
            phase = str(transaction.get("phase") or "")
            # A candidate whose create attempt was explicitly recovered as
            # absent may keep its app identity while its ephemeral tunnel URL
            # changes. This is deliberately opt-in: without fresh absence
            # evidence, changing the endpoint could make an existing app
            # unreachable or lead to a duplicate account-side create.
            may_rebind_recovered_absence = (
                phase == "recovery-required"
                and update
                and rebind_pending_after_app_absence
            )
            if (phase == "prepared" and update) or may_rebind_recovered_absence:
                candidate["public_url"] = normalized_url
                candidate["updated_at"] = now_iso()
                transaction["candidate"] = candidate
                transaction["updated_at"] = now_iso()
                if may_rebind_recovered_absence:
                    transaction["phase"] = "prepared"
                    transaction["recovery_rebind"] = {
                        "reason": "candidate-app-absence-confirmed",
                        "rebound_at": now_iso(),
                    }
                write_registry(registry)
                candidate_url = normalized_url
            else:
                raise RuntimeError(
                    "RECOVERY_REQUIRED: a different CodexPro candidate is already pending for this root; "
                    "resolve that transaction before creating another app"
                )
        if transaction.get("phase") not in {"prepared", "recovery-required"}:
            raise RuntimeError(
                "RECOVERY_REQUIRED: CodexPro candidate is already in account UI reconciliation; "
                "do not create or replace another app"
            )
        return Decision(
            action="resume-reconcile",
            root=canonical_root,
            slug=str(candidate.get("slug") or slug),
            app_name=str(candidate.get("app_name") or app_name(slug, 1)),
            version=int(candidate.get("version") or 1),
            port=candidate_port,
            public_url=candidate_url or normalized_url,
            old_app_name=(old_active or {}).get("app_name"),
            old_public_url=(old_active or {}).get("public_url"),
            chrome_next_action="resume-candidate-reconcile",
            transaction_id=transaction_id,
        )

    if existing:
        current_url = normalize_public_url(existing.get("public_url"))
        current_app = str(existing.get("app_name") or "").strip()
        existing_slug = str(existing.get("slug") or slug)
        current_version = int(existing.get("version") or 1)
        stored_port = existing.get("port")
        try:
            existing_port = int(stored_port) if stored_port is not None else 0
        except (TypeError, ValueError):
            existing_port = 0
        if preferred_port and verified_open_port:
            current_port = int(preferred_port)
        elif existing_port > 0:
            # A preferred port is a candidate-creation hint, never permission to
            # rewrite an active app's local endpoint during ordinary reuse.
            current_port = existing_port
        else:
            raise RuntimeError(
                "RECOVERY_REQUIRED: existing CodexPro app has no verified stored port; "
                "do not allocate a replacement port during reuse"
            )
        if existing_slug != slug and (
            existing_slug.startswith("project-")
            or ascii_slug(existing_slug) in GENERIC_ROOT_NAMES
            or current_app.startswith(f"{APP_PREFIX}-project-")
        ):
            existing_slug = slug
        if re.match(r"^[a-z0-9-]+-[0-9a-f]{8}$", existing_slug) and existing_slug.startswith(f"{slug}-"):
            existing_slug = slug
            current_app = app_name(existing_slug, current_version)
        chrome_status = str(existing.get("chrome_status") or "")
        chrome_reconcile_state = str(existing.get("chrome_reconcile_state") or "")
        repair_permission_only = bool(
            existing.get("chrome_permission_repair_only_next")
            or chrome_status == "permission-pending"
            or chrome_reconcile_state == "permission-policy-not-confirmed"
        )
        if force_recreate and repair_permission_only:
            decision = Decision(
                action="repair-permission",
                root=canonical_root,
                slug=existing_slug,
                app_name=current_app or app_name(slug, current_version),
                version=current_version,
                port=current_port,
                public_url=current_url or normalized_url,
                old_app_name=None,
                old_public_url=None,
                chrome_next_action="open-existing-app-detail-and-repair-permission",
            )
            if update:
                existing.update(
                    {
                        "slug": decision.slug,
                        "app_name": decision.app_name,
                        "version": decision.version,
                        "port": decision.port,
                        "public_url": decision.public_url,
                        "updated_at": now_iso(),
                        "status": "active",
                        "chrome_status": "permission-pending",
                        "chrome_permission_repair_only_next": True,
                        "chrome_created_app_preserved": True,
                        "chrome_app_connected_but_permission_pending": True,
                    }
                )
                write_registry(registry)
            return decision
        if force_recreate:
            next_version = current_version + 1
            # Force-recreate is still a candidate-first replacement.  Keeping
            # the old runtime alive until the new account registration is
            # committed means the candidate must not inherit its occupied
            # port.  A caller-supplied preferred port remains only a hint
            # until the bootstrap identity probe marks it verified/open.
            next_port = (
                int(preferred_port)
                if preferred_port and verified_open_port
                else allocate_port(registry, preferred_port)
            )
            next_name = app_name(existing_slug, next_version)
            decision = Decision(
                action="force-recreate",
                root=canonical_root,
                slug=existing_slug,
                app_name=next_name,
                version=next_version,
                port=next_port,
                public_url=normalized_url or current_url,
                old_app_name=current_app or app_name(slug, current_version),
                old_public_url=current_url,
                chrome_next_action="create-candidate-then-retire-old-app",
            )
            if update:
                decision.transaction_id = _stage_reconcile(registry, decision, old_active=existing)
                write_registry(registry)
            return decision

        if not normalized_url or normalized_url == current_url:
            decision = Decision(
                action="repair-permission" if repair_permission_only else "reuse",
                root=canonical_root,
                slug=existing_slug,
                app_name=current_app or app_name(slug, current_version),
                version=current_version,
                port=current_port,
                public_url=current_url or normalized_url,
                old_app_name=None,
                old_public_url=None,
                chrome_next_action=(
                    "open-existing-app-detail-and-repair-permission"
                    if repair_permission_only
                    else "select-existing-app-refresh-if-hidden"
                ),
            )
            if update:
                existing.update(
                    {
                        "slug": decision.slug,
                        "app_name": decision.app_name,
                        "version": decision.version,
                        "port": decision.port,
                        "public_url": decision.public_url,
                        "updated_at": now_iso(),
                        "status": "active",
                        "chrome_status": existing.get("chrome_status") or "unconfirmed",
                    }
                )
                write_registry(registry)
            return decision

        next_version = current_version + 1
        # URL replacement is a candidate-first transaction.  The old app and
        # its local endpoint remain live until the candidate is verified and
        # committed, so the candidate must not reuse the occupied old port.
        next_port = current_port if preferred_port and verified_open_port else allocate_port(registry, preferred_port)
        next_name = app_name(existing_slug, next_version)
        decision = Decision(
            action="replace-url",
            root=canonical_root,
            slug=existing_slug,
            app_name=next_name,
            version=next_version,
            port=next_port,
            public_url=normalized_url,
            old_app_name=current_app or app_name(slug, current_version),
            old_public_url=current_url,
            chrome_next_action="create-candidate-then-retire-old-app",
        )
        if update:
            decision.transaction_id = _stage_reconcile(registry, decision, old_active=existing)
            write_registry(registry)
        return decision

    port = int(preferred_port) if preferred_port and verified_open_port else allocate_port(registry, preferred_port)
    version = 1
    name = app_name(slug, version)
    decision = Decision(
        action="create",
        root=canonical_root,
        slug=slug,
        app_name=name,
        version=version,
        port=port,
        public_url=normalized_url,
        old_app_name=None,
        old_public_url=None,
        chrome_next_action="create-new-app",
    )
    if update:
        decision.transaction_id = _stage_reconcile(registry, decision, old_active=None)
        write_registry(registry)
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve one CodexPro ChatGPT Developer App slot per drive root.")
    parser.add_argument("--root", default="C:\\", help="CodexPro drive or project app root; use --no-git-root to preserve the exact root.")
    parser.add_argument("--public-url", help="Current Cloudflare/ngrok public MCP URL or base URL.")
    parser.add_argument("--port", type=int, help="Preferred local CodexPro port.")
    parser.add_argument("--verified-open-port", action="store_true", help="Allow --port even when already listening because MCP identity was already verified.")
    parser.add_argument(
        "--rebind-pending-after-app-absence",
        action="store_true",
        help="Allow one recovery-required candidate URL rebind only after an exact app inspection confirmed that candidate is absent.",
    )
    parser.add_argument("--no-git-root", action="store_true", help="Use the exact path instead of git rev-parse root.")
    parser.add_argument("--update", action="store_true", help="Persist the decision to the global registry.")
    parser.add_argument("--force-recreate", action="store_true", help="Create a new app version even when the public URL matches the registry.")
    parser.add_argument("--registry", action="store_true", help="Print the full registry and exit.")
    parser.add_argument("--mark-chrome-confirmed", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.registry:
        with registry_lock():
            print(json.dumps(load_registry(), ensure_ascii=False, indent=2))
        return 0

    if args.mark_chrome_confirmed:
        raise RuntimeError(
            "--mark-chrome-confirmed is disabled. Confirmation must be written only by "
            "codexpro_agbrowse_app.py after full URL and permission verification."
        )

    root = drive_app_root(project_root(Path(args.root), use_git_root=not args.no_git_root))
    with registry_lock():
        decision = decide(
            root=root,
            public_url=args.public_url,
            preferred_port=args.port,
            update=args.update,
            force_recreate=args.force_recreate,
        verified_open_port=args.verified_open_port,
        rebind_pending_after_app_absence=args.rebind_pending_after_app_absence,
        )
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
