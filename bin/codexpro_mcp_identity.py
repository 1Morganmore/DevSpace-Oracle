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


def probe_codexpro_identity(public_url: str, expected_root: str, expected_port: int | None = None, *, timeout: int = 12) -> dict[str, Any]:
    started = time.time()
    identity: dict[str, Any] = {
        "ok": False,
        "endpoint_key": None,
        "expected_root": expected_root,
        "expected_port": expected_port,
        "default_root": None,
        "port": None,
        "root_ok": False,
        "port_ok": False,
        "reason": None,
    }
    try:
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
        identity["port"] = config.get("port")
        identity["root_ok"] = same_windows_path(str(identity["default_root"] or ""), expected_root)
        try:
            identity["port_ok"] = expected_port is None or int(identity["port"]) == int(expected_port)
        except Exception:
            identity["port_ok"] = False
        identity["ok"] = bool(identity["root_ok"] and identity["port_ok"])
        if not identity["root_ok"]:
            identity["reason"] = "root-mismatch"
        elif not identity["port_ok"]:
            identity["reason"] = "port-mismatch"
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
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--endpoint-key-only", action="store_true")
    args = parser.parse_args()
    if args.endpoint_key_only:
        print(endpoint_key(args.url))
        return 0
    result = probe_codexpro_identity(args.url, args.expected_root, args.expected_port, timeout=args.timeout)
    print(json.dumps(redact_json(result), ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
