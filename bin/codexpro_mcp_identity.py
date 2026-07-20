#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


TOKEN_RE = re.compile(r"(codexpro_token=)[^&\s\"']+", re.I)


def redact_secret_text(value: Any) -> str:
    return TOKEN_RE.sub(r"\1<redacted>", str(value))


def redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(v) for v in value]
    if isinstance(value, str):
        return redact_secret_text(value)
    return value


def endpoint_key(public_url: str | None) -> str:
    text = str(public_url or "").strip()
    if not text:
        raise ValueError("missing public URL")
    if not re.match(r"^https?://", text, re.I):
        text = f"https://{text}"
    parsed = urlparse(text)
    path = "/" + (parsed.path.strip("/") or "mcp")
    if path.rstrip("/") != "/mcp":
        raise ValueError("CodexPro endpoint must use /mcp path")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("CodexPro endpoint host missing")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{(parsed.scheme or 'https').lower()}://{host}{port}/mcp"


def token_from_url(public_url: str | None) -> str:
    parsed = urlparse(str(public_url or ""))
    return (parse_qs(parsed.query, keep_blank_values=False).get("codexpro_token") or [""])[0].strip()


def same_windows_path(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return os.path.normcase(os.path.normpath(str(Path(left)))) == os.path.normcase(os.path.normpath(str(Path(right))))
    except Exception:
        return os.path.normcase(os.path.normpath(str(left))) == os.path.normcase(os.path.normpath(str(right)))


def parse_mcp_response(raw: bytes | str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                data_lines.append(payload)
    if data_lines:
        return json.loads(data_lines[-1])
    raise ValueError("no JSON-RPC payload in MCP response")


def _request_json(public_url: str, payload: dict[str, Any], *, session_id: str | None = None, timeout: int = 12) -> tuple[dict[str, Any], dict[str, Any]]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["mcp-session-id"] = session_id
    request = urllib.request.Request(
        public_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = parse_mcp_response(response.read(65536))
        meta = {
            "status_code": response.status,
            "content_type": response.headers.get("Content-Type", ""),
            "session_id": response.headers.get("mcp-session-id", ""),
        }
        return body, meta


def extract_server_config(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    texts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        return dict(result["structuredContent"])
    joined = "\n".join(texts).strip()
    start = joined.find("{")
    end = joined.rfind("}")
    if start >= 0 and end > start:
        return json.loads(joined[start : end + 1])
    if isinstance(result, dict):
        return result
    raise ValueError("server_config response did not contain JSON config")


def probe_codexpro_identity(
    public_url: str,
    expected_root: str,
    expected_port: int | None = None,
    *,
    timeout: int = 12,
    scope_mode: str = "legacy-drive",
    topology_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    started = time.time()
    identity: dict[str, Any] = {
        "ok": False,
        "endpoint_key": None,
        "expected_root": expected_root,
        "expected_port": expected_port,
        "scope_mode": scope_mode,
        "topology_receipt_sha256": topology_receipt_sha256,
        "default_root": None,
        "allowed_roots": None,
        "port": None,
        "root_ok": False,
        "allowed_roots_ok": scope_mode == "legacy-drive",
        "port_ok": False,
        "server_contract_ok": scope_mode == "legacy-drive",
        "tools_ok": scope_mode == "legacy-drive",
        "reason": None,
    }
    try:
        if scope_mode not in {"legacy-drive", "parallel-exact-unit"}:
            identity["reason"] = "scope-mode-invalid"
            return identity
        if scope_mode == "parallel-exact-unit" and re.fullmatch(r"[0-9a-f]{64}", str(topology_receipt_sha256 or "")) is None:
            identity["reason"] = "topology-receipt-invalid"
            return identity
        identity["endpoint_key"] = endpoint_key(public_url)
        if not token_from_url(public_url):
            identity["reason"] = "missing-token"
            return identity
        init_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "codexpro-identity-preflight", "version": "1"},
            },
        }
        init, meta = _request_json(public_url, init_payload, timeout=timeout)
        session_id = str(meta.get("session_id") or "")
        if "error" in init:
            identity["reason"] = f"initialize-error:{init['error']}"
            return identity
        identity["server_info"] = (init.get("result") or {}).get("serverInfo") if isinstance(init.get("result"), dict) else None
        try:
            _request_json(public_url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=session_id, timeout=timeout)
        except Exception:
            pass
        cfg_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "server_config", "arguments": {}},
        }
        cfg_response, _ = _request_json(public_url, cfg_payload, session_id=session_id, timeout=timeout)
        config = extract_server_config(cfg_response)
        identity["default_root"] = config.get("defaultRoot") or config.get("root")
        identity["allowed_roots"] = config.get("allowedRoots") or config.get("allowed_roots") or []
        identity["port"] = config.get("port")
        identity["root_ok"] = same_windows_path(str(identity["default_root"] or ""), expected_root)
        try:
            identity["port_ok"] = expected_port is None or int(identity["port"]) == int(expected_port)
        except Exception:
            identity["port_ok"] = False
        if scope_mode == "parallel-exact-unit":
            roots = identity["allowed_roots"] if isinstance(identity["allowed_roots"], list) else []
            identity["allowed_roots_ok"] = len(roots) == 1 and same_windows_path(str(roots[0]), expected_root)
            actual_topology = config.get("topologyReceiptSha256") or config.get("topology_receipt_sha256")
            bash_mode = str(config.get("bashMode") or config.get("bash_mode") or "").casefold()
            write_mode = str(config.get("writeMode") or config.get("write_mode") or "").casefold()
            tool_mode = str(config.get("toolMode") or config.get("tool_mode") or "").casefold()
            profile_enabled = config.get("profileEnabled") if "profileEnabled" in config else config.get("profile_enabled")
            identity["server_contract_ok"] = bool(
                str(actual_topology or "") == str(topology_receipt_sha256)
                and bash_mode in {"off", "disabled", "false"}
                and write_mode == "workspace"
                and tool_mode == "full"
                and profile_enabled in {False, 0, "false", "off", None}
            )
            tools_response, _ = _request_json(
                public_url,
                {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                session_id=session_id,
                timeout=timeout,
            )
            listed = ((tools_response.get("result") or {}).get("tools") if isinstance(tools_response.get("result"), dict) else None) or []
            tool_names = sorted(str(item.get("name") or "") for item in listed if isinstance(item, dict))
            identity["tool_names"] = tool_names
            identity["tools_ok"] = bool("server_config" in tool_names and "bash" not in tool_names)
        identity["ok"] = bool(identity["root_ok"] and identity["port_ok"] and identity["allowed_roots_ok"] and identity["server_contract_ok"] and identity["tools_ok"])
        if not identity["root_ok"]:
            identity["reason"] = "root-mismatch"
        elif not identity["port_ok"]:
            identity["reason"] = "port-mismatch"
        elif not identity["allowed_roots_ok"]:
            identity["reason"] = "allowed-roots-mismatch"
        elif not identity["server_contract_ok"]:
            identity["reason"] = "server-contract-mismatch"
        elif not identity["tools_ok"]:
            identity["reason"] = "tools-mismatch"
        else:
            identity["reason"] = "identity-ok"
        return identity
    except urllib.error.URLError as exc:
        identity["reason"] = f"url-error:{exc.reason}"
        return identity
    except Exception as exc:
        identity["reason"] = f"{type(exc).__name__}:{exc}"
        return identity
    finally:
        identity["duration_ms"] = int((time.time() - started) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-root", required=True)
    parser.add_argument("--expected-port", type=int)
    parser.add_argument("--scope-mode", choices=("legacy-drive", "parallel-exact-unit"), default="legacy-drive")
    parser.add_argument("--topology-receipt-sha256")
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--endpoint-key-only", action="store_true")
    args = parser.parse_args()
    if args.endpoint_key_only:
        print(endpoint_key(args.url))
        return 0
    result = probe_codexpro_identity(
        args.url,
        args.expected_root,
        args.expected_port,
        timeout=args.timeout,
        scope_mode=args.scope_mode,
        topology_receipt_sha256=args.topology_receipt_sha256,
    )
    print(json.dumps(redact_json(result), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
