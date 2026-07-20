from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import codexpro_mcp_identity as ident  # noqa: E402


def server_config_payload(root: str, port: int) -> dict:
    return {
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": "# CodexPro Server Config\n\n" + json.dumps({"defaultRoot": root, "port": port}),
                }
            ]
        }
    }


def test_endpoint_key_ignores_token_case_and_trailing_slash() -> None:
    left = ident.endpoint_key("https://Example.TryCloudflare.com/mcp?codexpro_token=aaa")
    right = ident.endpoint_key("https://example.trycloudflare.com/mcp/?codexpro_token=bbb")
    assert left == right == "https://example.trycloudflare.com/mcp"


def test_redact_json_removes_nested_codexpro_tokens() -> None:
    raw = {
        "url": "https://x.trycloudflare.com/mcp?codexpro_token=secret123",
        "nested": ["prefix codexpro_token=abc def"],
    }
    redacted = ident.redact_json(raw)
    dumped = json.dumps(redacted)
    assert "secret123" not in dumped
    assert "abc" not in dumped
    assert "codexpro_token=<redacted>" in dumped


def test_probe_rejects_reachable_wrong_root(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fake_request(url, payload, *, session_id=None, timeout=12):
        calls.append(payload["method"])
        if payload["method"] == "initialize":
            return {"result": {"serverInfo": {"name": "CodexPro"}}}, {"session_id": "sid"}
        if payload["method"] == "tools/call":
            return server_config_payload(r"C:\workspace\.codex", 8790), {"session_id": "sid"}
        return {}, {"session_id": "sid"}

    monkeypatch.setattr(ident, "_request_json", fake_request)
    result = ident.probe_codexpro_identity(
        "https://x.trycloudflare.com/mcp?codexpro_token=tok",
        r"C:\workspace\BB",
        8793,
    )
    assert result["ok"] is False
    assert result["reason"] == "root-mismatch"
    assert "tools/call" in calls


def test_probe_accepts_matching_root_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(url, payload, *, session_id=None, timeout=12):
        if payload["method"] == "initialize":
            return {"result": {"serverInfo": {"name": "CodexPro"}}}, {"session_id": "sid"}
        if payload["method"] == "tools/call":
            return server_config_payload(r"C:\workspace\BB", 8793), {"session_id": "sid"}
        return {}, {"session_id": "sid"}

    monkeypatch.setattr(ident, "_request_json", fake_request)
    result = ident.probe_codexpro_identity(
        "https://x.trycloudflare.com/mcp?codexpro_token=tok",
        r"C:\workspace\BB",
        8793,
    )
    assert result["ok"] is True
    assert result["reason"] == "identity-ok"


def test_parse_mcp_response_accepts_sse() -> None:
    payload = b'event: message\ndata: {"jsonrpc":"2.0","result":{"ok":true}}\n\n'
    assert ident.parse_mcp_response(payload)["result"]["ok"] is True
