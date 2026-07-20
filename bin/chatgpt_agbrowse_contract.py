from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


SCHEMA = "codex.chatgpt.agbrowse-contract/v1"
PACKAGE_NAME = "agbrowse"
EXPECTED_VERSION = "0.1.18"
EXPECTED_NPM_INTEGRITY = "sha512-vO2E1XrqTAvkWeSyV1xzsONz+OBB3aDKbxIGVS7Z4pH42Hxg/mlcteIAzM+EuD4hnp6Tt5IJu/X2fjMOiftBCA=="
DEFAULT_VENDOR = "chatgpt"
DEFAULT_RENDER_PROMPT = "hello"
DEFAULT_MISSING_SESSION_ID = "missing-session"
DEFAULT_TIMEOUT_SECONDS = 20

ALLOWED_PROBE_KINDS = {"help", "missing-session", "render", "status"}
FORBIDDEN_EXECUTED_TOKENS = {"send", "query", "poll", "watch", "resume", "reattach"}
REQUIRED_LIFECYCLE_COMMANDS = ("start", "status", "doctor", "tabs")
REQUIRED_WEB_AI_COMMANDS = ("render", "status", "send", "poll", "query", "watch")
REQUIRED_SESSION_RECOVERY_COMMANDS = ("show", "resume", "reattach", "doctor")
NO_SESSION_RE = re.compile(r"^no session record for\b", re.IGNORECASE)
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

ERROR_RULES: list[dict[str, Any]] = [
    {
        "id": "capability-unsupported-provider-surface-preflight",
        "classification": "PREFLIGHT_BLOCKED",
        "match": {
            "errorCode": "capability.unsupported",
            "stage": "provider-surface-preflight",
            "mutationAllowed": False,
        },
    },
    {
        "id": "generic-no-session-record",
        "classification": "PREFLIGHT_BLOCKED",
        "match": {
            "messagePrefix": "no session record for ",
            "mutationAllowed": False,
        },
    },
    {
        "id": "cdp-unreachable-before-provider-mutation",
        "classification": "PREFLIGHT_BLOCKED",
        "match": {
            "errorCode": "cdp.unreachable",
            "stage": "connect",
            "mutationAllowed": False,
        },
    },
]


class ContractError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def envelope(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": str(self),
                "details": self.details,
            },
        }


@dataclass(frozen=True)
class InstallationInfo:
    executable_path: Path
    executable_sha256: str
    package_path: Path
    package_sha256: str
    package_file_count: int
    package_entrypoint_path: Path
    package_entrypoint_sha256: str
    version: str
    npm_integrity: str


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    kind: str
    command: tuple[str, ...]
    mode: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_package_tree_hash(root: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if "node_modules" in path.relative_to(root).parts:
            continue
        relative = path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(bytes.fromhex(sha256_file(path)))
        hasher.update(b"\0")
        count += 1
    if count == 0:
        raise ContractError("PACKAGE_EMPTY", f"package tree is empty: {root}")
    return hasher.hexdigest(), count


def canonical_json_sha256(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(canonical)


def default_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env["AGBROWSE_WEB_AI_AUTO_START"] = "0"
    env["AGBROWSE_JSON_ERRORS"] = "1"
    env.setdefault("AGBROWSE_UPDATE_CHECK", "0")
    return env


def default_run_command(
    args: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
    )


def find_first_executable(which_runner: Callable[[str], str | None], *candidates: str) -> Path:
    for candidate in candidates:
        located = which_runner(candidate)
        if located:
            return Path(located).expanduser().resolve()
    raise ContractError("EXECUTABLE_NOT_FOUND", f"could not locate any of: {', '.join(candidates)}")


def parse_json_stream(stdout: str, stderr: str, probe_id: str) -> Any:
    candidate = stdout.strip() or stderr.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "PROBE_NON_JSON",
            f"probe {probe_id} did not return JSON",
            {
                "probe_id": probe_id,
                "stdout": stdout[:400],
                "stderr": stderr[:400],
            },
        ) from exc


def compact_json_error(error: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "name": error.get("name"),
        "errorCode": error.get("errorCode"),
        "stage": error.get("stage"),
        "message": error.get("message"),
        "retryHint": error.get("retryHint"),
        "mutationAllowed": error.get("mutationAllowed"),
    }
    if "vendor" in error:
        compact["vendor"] = error.get("vendor")
    return compact


def classify_error(error: dict[str, Any]) -> str | None:
    error_code = str(error.get("errorCode") or "")
    stage = str(error.get("stage") or "")
    mutation_allowed = error.get("mutationAllowed")
    message = str(error.get("message") or "").strip()

    if (
        error_code == "capability.unsupported"
        and stage == "provider-surface-preflight"
        and mutation_allowed is False
    ):
        return "PREFLIGHT_BLOCKED"

    if mutation_allowed is False and NO_SESSION_RE.match(message):
        return "PREFLIGHT_BLOCKED"

    if error_code == "cdp.unreachable" and stage == "connect" and mutation_allowed is False:
        return "PREFLIGHT_BLOCKED"

    return None


def extract_root_commands(help_text: str) -> list[str]:
    commands = set(re.findall(r"(?m)^\s{2,}([a-z][a-z0-9-]*)\b", help_text))
    return sorted(commands)


def extract_web_ai_commands(help_text: str) -> list[str]:
    commands = set(re.findall(r"(?m)^\s{2}([a-z][a-z0-9-]*)\b", help_text))
    commands.update(re.findall(r"(?m)^\s*agbrowse web-ai sessions ([a-z-]+)\b", help_text))
    return sorted(commands)


def summarize_root_help(help_text: str) -> dict[str, Any]:
    commands = extract_root_commands(help_text)
    present = {name: name in commands for name in REQUIRED_LIFECYCLE_COMMANDS}
    return {
        "format": "text",
        "stdoutSha256": sha256_text(help_text),
        "lineCount": len(help_text.splitlines()),
        "commands": commands,
        "requiredLifecycle": present,
    }


def summarize_web_ai_help(help_text: str) -> dict[str, Any]:
    commands = extract_web_ai_commands(help_text)
    present = {name: name in commands for name in REQUIRED_WEB_AI_COMMANDS}
    recovery = {name: name in commands for name in REQUIRED_SESSION_RECOVERY_COMMANDS}
    return {
        "format": "text",
        "stdoutSha256": sha256_text(help_text),
        "lineCount": len(help_text.splitlines()),
        "commands": commands,
        "requiredWebAi": present,
        "requiredSessionRecovery": recovery,
    }


def summarize_text_status(stdout: str) -> dict[str, Any]:
    pairs: dict[str, str] = {}
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        pairs[key.strip()] = value.strip()
    return {
        "format": "text",
        "stdoutSha256": sha256_text(stdout),
        "lineCount": len(stdout.splitlines()),
        "fields": pairs,
    }


def summarize_json_probe(probe_id: str, payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "format": "json",
        "jsonSha256": canonical_json_sha256(payload),
    }
    if isinstance(payload, dict):
        summary["ok"] = payload.get("ok")
        summary["status"] = payload.get("status")
    if probe_id == "root-doctor" and isinstance(payload, dict):
        summary["checkCount"] = len(payload.get("checks") or [])
        summary["port"] = payload.get("port")
    elif probe_id == "root-tabs" and isinstance(payload, list):
        summary["tabCount"] = len(payload)
    elif probe_id == "web-ai-render" and isinstance(payload, dict):
        rendered = payload.get("rendered") or {}
        summary["vendor"] = payload.get("vendor")
        summary["estimatedChars"] = rendered.get("estimatedChars")
        summary["warningCount"] = len(payload.get("warnings") or [])
    elif isinstance(payload, dict) and payload.get("ok") is False and isinstance(payload.get("error"), dict):
        compact = compact_json_error(payload["error"])
        summary["error"] = compact
        classification = classify_error(compact)
        if classification:
            summary["bridgeClassification"] = classification
    return summary


def resolve_installation(
    *,
    executable_path: str | os.PathLike[str] | None = None,
    package_path: str | os.PathLike[str] | None = None,
    npm_executable_path: str | os.PathLike[str] | None = None,
    expected_version: str = EXPECTED_VERSION,
    expected_npm_integrity: str = EXPECTED_NPM_INTEGRITY,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = default_run_command,
    which_runner: Callable[[str], str | None] = shutil.which,
) -> InstallationInfo:
    executable = Path(executable_path).expanduser().resolve() if executable_path else find_first_executable(
        which_runner, "agbrowse.cmd", "agbrowse", "agbrowse.ps1"
    )
    if not executable.is_file():
        raise ContractError("EXECUTABLE_NOT_FOUND", f"agbrowse executable not found: {executable}")

    package_dir: Path
    if package_path is not None:
        package_dir = Path(package_path).expanduser().resolve()
    else:
        sibling_candidate = executable.parent / "node_modules" / PACKAGE_NAME
        if sibling_candidate.is_dir():
            package_dir = sibling_candidate.resolve()
        else:
            npm_executable = (
                Path(npm_executable_path).expanduser().resolve()
                if npm_executable_path
                else find_first_executable(which_runner, "npm.cmd", "npm")
            )
            npm_root = run_command(
                [str(npm_executable), "root", "-g"],
                env=default_env(),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            npm_root_path = Path(npm_root.stdout.strip()).expanduser().resolve()
            package_dir = npm_root_path / PACKAGE_NAME
    if not package_dir.is_dir():
        raise ContractError("PACKAGE_NOT_FOUND", f"agbrowse package not found: {package_dir}")

    package_json_path = package_dir / "package.json"
    if not package_json_path.is_file():
        raise ContractError("PACKAGE_METADATA_MISSING", f"package.json not found: {package_json_path}")
    package_json = json.loads(package_json_path.read_text(encoding="utf-8"))
    version = str(package_json.get("version") or "")
    if version != expected_version:
        raise ContractError(
            "VERSION_MISMATCH",
            f"expected {PACKAGE_NAME}@{expected_version}, found {version or '<missing>'}",
            {"expectedVersion": expected_version, "actualVersion": version},
        )

    npm_executable = (
        Path(npm_executable_path).expanduser().resolve()
        if npm_executable_path
        else find_first_executable(which_runner, "npm.cmd", "npm")
    )
    integrity_probe = run_command(
        [str(npm_executable), "view", f"{PACKAGE_NAME}@{expected_version}", "dist.integrity", "--json", "--offline"],
        env=default_env(),
        timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    npm_integrity = json.loads(integrity_probe.stdout.strip())
    if npm_integrity != expected_npm_integrity:
        raise ContractError(
            "NPM_INTEGRITY_MISMATCH",
            f"expected npm integrity {expected_npm_integrity}, found {npm_integrity}",
            {"expectedIntegrity": expected_npm_integrity, "actualIntegrity": npm_integrity},
        )

    package_entrypoint = package_dir / "bin" / "agbrowse.mjs"
    if not package_entrypoint.is_file():
        raise ContractError("PACKAGE_ENTRYPOINT_MISSING", f"package entrypoint not found: {package_entrypoint}")

    package_sha256, package_file_count = stable_package_tree_hash(package_dir)
    return InstallationInfo(
        executable_path=executable,
        executable_sha256=sha256_file(executable),
        package_path=package_dir,
        package_sha256=package_sha256,
        package_file_count=package_file_count,
        package_entrypoint_path=package_entrypoint,
        package_entrypoint_sha256=sha256_file(package_entrypoint),
        version=version,
        npm_integrity=npm_integrity,
    )


def probe_specs(vendor: str, prompt: str, missing_session_id: str) -> list[ProbeSpec]:
    return [
        ProbeSpec("root-help", "help", ("--help",), "help-root"),
        ProbeSpec("web-ai-help", "help", ("web-ai", "--help"), "help-web-ai"),
        ProbeSpec("root-status", "status", ("status", "--json"), "status-text-or-json"),
        ProbeSpec("root-doctor", "status", ("doctor", "--json"), "json"),
        ProbeSpec("root-tabs", "status", ("tabs", "--json"), "json"),
        ProbeSpec(
            "web-ai-render",
            "render",
            ("web-ai", "render", "--vendor", vendor, "--inline-only", "--prompt", prompt, "--json"),
            "json",
        ),
        ProbeSpec("web-ai-status", "status", ("web-ai", "status", "--vendor", vendor, "--json"), "json"),
        ProbeSpec(
            "web-ai-sessions-show-missing",
            "missing-session",
            ("web-ai", "sessions", "show", missing_session_id, "--json"),
            "json",
        ),
    ]


def execute_probe(
    installation: InstallationInfo,
    spec: ProbeSpec,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = default_run_command,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    env = default_env()
    args = [str(installation.executable_path), *spec.command]
    result = run_command(args, env=env, timeout=timeout)
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    probe: dict[str, Any] = {
        "id": spec.probe_id,
        "kind": spec.kind,
        "invocation": list(spec.command),
        "returnCode": result.returncode,
    }
    if stderr.strip():
        probe["stderrSha256"] = sha256_text(stderr)

    if spec.probe_id == "root-help":
        probe["summary"] = summarize_root_help(stdout)
        return probe
    if spec.probe_id == "web-ai-help":
        probe["summary"] = summarize_web_ai_help(stdout)
        return probe
    if spec.probe_id == "root-status":
        parsed = None
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
        probe["summary"] = summarize_json_probe(spec.probe_id, parsed) if parsed is not None else summarize_text_status(stdout)
        return probe

    payload = parse_json_stream(stdout, stderr, spec.probe_id)
    probe["summary"] = summarize_json_probe(spec.probe_id, payload)
    return probe


def build_manifest(
    installation: InstallationInfo,
    probes: list[dict[str, Any]],
    *,
    expected_version: str = EXPECTED_VERSION,
    expected_npm_integrity: str = EXPECTED_NPM_INTEGRITY,
) -> dict[str, Any]:
    by_id = {probe["id"]: probe for probe in probes}
    root_help = by_id["root-help"]["summary"]
    web_ai_help = by_id["web-ai-help"]["summary"]
    observed_errors = []
    for probe in probes:
        summary = probe.get("summary") or {}
        error = summary.get("error")
        if error:
            observed_errors.append(
                {
                    "probeId": probe["id"],
                    "error": error,
                    "classification": summary.get("bridgeClassification"),
                }
            )
    return {
        "schema": SCHEMA,
        "capturedAt": utc_now(),
        "agbrowse": {
            "packageName": PACKAGE_NAME,
            "version": installation.version,
            "expectedVersion": expected_version,
            "npmIntegrity": installation.npm_integrity,
            "expectedNpmIntegrity": expected_npm_integrity,
            "packagePath": str(installation.package_path),
            "packageSha256": installation.package_sha256,
            "packageFileCount": installation.package_file_count,
            "executablePath": str(installation.executable_path),
            "executableSha256": installation.executable_sha256,
            "packageEntrypointPath": str(installation.package_entrypoint_path),
            "packageEntrypointSha256": installation.package_entrypoint_sha256,
        },
        "policy": {
            "autoStartDisabled": True,
            "jsonErrorsForced": True,
            "allowedProbeKinds": sorted(ALLOWED_PROBE_KINDS),
            "executedProbeIds": [probe["id"] for probe in probes],
            "executedInvocations": [probe["invocation"] for probe in probes],
            "sendAuthorized": False,
            "sendProbeExecuted": False,
        },
        "allowedCommandManifest": {
            "rootHelpSha256": root_help["stdoutSha256"],
            "webAiHelpSha256": web_ai_help["stdoutSha256"],
            "lifecycle": root_help["requiredLifecycle"],
            "webAi": web_ai_help["requiredWebAi"],
            "sessionRecovery": web_ai_help["requiredSessionRecovery"],
            "sendCommandPublic": bool(web_ai_help["requiredWebAi"].get("send")),
            "sendAuthorized": False,
        },
        "errorManifest": {
            "rules": ERROR_RULES,
            "observed": observed_errors,
        },
        "probes": probes,
    }


def capture_contract(
    *,
    vendor: str = DEFAULT_VENDOR,
    prompt: str = DEFAULT_RENDER_PROMPT,
    missing_session_id: str = DEFAULT_MISSING_SESSION_ID,
    executable_path: str | os.PathLike[str] | None = None,
    package_path: str | os.PathLike[str] | None = None,
    npm_executable_path: str | os.PathLike[str] | None = None,
    expected_version: str = EXPECTED_VERSION,
    expected_npm_integrity: str = EXPECTED_NPM_INTEGRITY,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = default_run_command,
    which_runner: Callable[[str], str | None] = shutil.which,
) -> dict[str, Any]:
    installation = resolve_installation(
        executable_path=executable_path,
        package_path=package_path,
        npm_executable_path=npm_executable_path,
        expected_version=expected_version,
        expected_npm_integrity=expected_npm_integrity,
        run_command=run_command,
        which_runner=which_runner,
    )
    probes = [
        execute_probe(installation, spec, run_command=run_command)
        for spec in probe_specs(vendor, prompt, missing_session_id)
    ]
    return build_manifest(
        installation,
        probes,
        expected_version=expected_version,
        expected_npm_integrity=expected_npm_integrity,
    )


def validate_manifest(
    manifest: dict[str, Any],
    *,
    expected_version: str | None = None,
    expected_npm_integrity: str | None = None,
) -> dict[str, Any]:
    errors: list[str] = []

    if manifest.get("schema") != SCHEMA:
        errors.append(f"schema must equal {SCHEMA}")

    agbrowse = manifest.get("agbrowse") or {}
    declared_version = str(agbrowse.get("expectedVersion") or "")
    declared_integrity = str(agbrowse.get("expectedNpmIntegrity") or "")
    selected_version = str(expected_version or declared_version or EXPECTED_VERSION)
    selected_integrity = str(expected_npm_integrity or declared_integrity or EXPECTED_NPM_INTEGRITY)
    if declared_version != selected_version:
        errors.append("expectedVersion does not match the selected version")
    if declared_integrity != selected_integrity:
        errors.append("expectedNpmIntegrity does not match the selected integrity")
    if agbrowse.get("version") != selected_version:
        errors.append(f"version must equal {selected_version}")
    if agbrowse.get("npmIntegrity") != selected_integrity:
        errors.append("npmIntegrity does not match the selected value")
    for key in ("packageSha256", "executableSha256", "packageEntrypointSha256"):
        if not HEX64_RE.fullmatch(str(agbrowse.get(key) or "")):
            errors.append(f"{key} must be a 64-character lowercase sha256 hex digest")

    policy = manifest.get("policy") or {}
    if policy.get("autoStartDisabled") is not True:
        errors.append("autoStartDisabled must be true")
    if policy.get("jsonErrorsForced") is not True:
        errors.append("jsonErrorsForced must be true")
    if policy.get("sendAuthorized") is not False:
        errors.append("sendAuthorized must be false")
    if policy.get("sendProbeExecuted") is not False:
        errors.append("sendProbeExecuted must be false")
    for invocation in policy.get("executedInvocations") or []:
        lowered = {str(part).strip().casefold() for part in invocation}
        if lowered & FORBIDDEN_EXECUTED_TOKENS:
            errors.append(f"executed invocation contains forbidden mutating token: {sorted(lowered & FORBIDDEN_EXECUTED_TOKENS)}")

    allowed = manifest.get("allowedCommandManifest") or {}
    for name in REQUIRED_LIFECYCLE_COMMANDS:
        if (allowed.get("lifecycle") or {}).get(name) is not True:
            errors.append(f"root help must expose lifecycle command: {name}")
    for name in REQUIRED_WEB_AI_COMMANDS:
        if (allowed.get("webAi") or {}).get(name) is not True:
            errors.append(f"web-ai help must expose command: {name}")
    for name in REQUIRED_SESSION_RECOVERY_COMMANDS:
        if (allowed.get("sessionRecovery") or {}).get(name) is not True:
            errors.append(f"web-ai help must expose session recovery command: {name}")
    if allowed.get("sendCommandPublic") is not True:
        errors.append("sendCommandPublic must be true")
    if allowed.get("sendAuthorized") is not False:
        errors.append("allowedCommandManifest.sendAuthorized must be false")

    error_manifest = manifest.get("errorManifest") or {}
    rule_ids = {str(rule.get("id") or "") for rule in error_manifest.get("rules") or []}
    for rule in ERROR_RULES:
        if rule["id"] not in rule_ids:
            errors.append(f"missing error classification rule: {rule['id']}")

    probes = manifest.get("probes")
    if not isinstance(probes, list) or not probes:
        errors.append("probes must be a non-empty list")
    else:
        for probe in probes:
            if str(probe.get("kind") or "") not in ALLOWED_PROBE_KINDS:
                errors.append(f"probe kind is not allowed: {probe.get('kind')}")
            summary = probe.get("summary") or {}
            if probe.get("id") in {"root-help", "web-ai-help", "root-status"}:
                if summary.get("format") not in {"text", "json"}:
                    errors.append(f"probe {probe.get('id')} has invalid summary format")
            else:
                if summary.get("format") != "json":
                    errors.append(f"probe {probe.get('id')} must summarize JSON")

    if errors:
        raise ContractError("MANIFEST_INVALID", "manifest validation failed", {"errors": errors})

    return {
        "ok": True,
        "schema": SCHEMA,
        "version": selected_version,
        "sendAuthorized": False,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture and validate an exact versioned Gate-0 agbrowse contract.")
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture")
    capture.add_argument("--vendor", default=DEFAULT_VENDOR)
    capture.add_argument("--prompt", default=DEFAULT_RENDER_PROMPT)
    capture.add_argument("--missing-session-id", default=DEFAULT_MISSING_SESSION_ID)
    capture.add_argument("--expected-version", default=EXPECTED_VERSION)
    capture.add_argument("--expected-integrity", default=EXPECTED_NPM_INTEGRITY)
    capture.add_argument("--output")

    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--expected-version")
    validate.add_argument("--expected-integrity")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "capture":
            manifest = capture_contract(
                vendor=args.vendor,
                prompt=args.prompt,
                missing_session_id=args.missing_session_id,
                expected_version=args.expected_version,
                expected_npm_integrity=args.expected_integrity,
            )
            validate_manifest(
                manifest,
                expected_version=args.expected_version,
                expected_npm_integrity=args.expected_integrity,
            )
            encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
            if args.output:
                output_path = Path(args.output).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(encoded + "\n", encoding="utf-8")
            else:
                print(encoded)
            return 0

        manifest_path = Path(args.manifest).expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        print(json.dumps(validate_manifest(
            manifest,
            expected_version=args.expected_version,
            expected_npm_integrity=args.expected_integrity,
        ), ensure_ascii=False, indent=2))
        return 0
    except ContractError as exc:
        print(json.dumps(exc.envelope(), ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
