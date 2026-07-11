#!/usr/bin/env python3
"""Minimal stdio MCP server for turn-scoped Telegram delivery metadata."""
import json
import os
import tempfile
import sys
from pathlib import Path
from typing import Any

MAX_PAYLOAD_BYTES = 64 * 1024
MAX_ATTACHMENTS = 10


def validate(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    attachments = arguments.get("attachments", [])
    confirmation = arguments.get("confirmation")
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("attachments must be a bounded array")
    clean = []
    for item in attachments:
        if not isinstance(item, dict) or item.get("kind") != "photo" or not isinstance(item.get("path"), str):
            raise ValueError("invalid attachment")
        clean.append({"kind": "photo", "path": item["path"]})
    checked_confirmation = None
    if confirmation is not None:
        summary = confirmation.get("summary") if isinstance(confirmation, dict) else None
        if not isinstance(confirmation, dict) or confirmation.get("required") is not True or not isinstance(summary, str) or not summary.strip() or len(summary) > 2000:
            raise ValueError("invalid confirmation")
        checked_confirmation = {"required": True, "summary": summary.strip()}
    return {"attachments": clean, "confirmation": checked_confirmation}


def write_payload(arguments: Any) -> None:
    turn_id = os.environ.get("TELEGRAM_BRIDGE_TURN_ID")
    filename = os.environ.get("TELEGRAM_BRIDGE_PAYLOAD_FILE")
    if not turn_id or not filename:
        raise ValueError("bridge turn environment is unavailable")
    encoded = json.dumps({"turn_id": turn_id, **validate(arguments)}, separators=(",", ":")).encode()
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError("payload is too large")
    destination = Path(filename)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".payload-", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


TOOL = {"name": "set_payload", "description": "Set Telegram photo attachments and/or external-action confirmation metadata for the current turn.", "inputSchema": {"type": "object", "properties": {"attachments": {"type": "array", "maxItems": MAX_ATTACHMENTS, "items": {"type": "object", "properties": {"kind": {"const": "photo"}, "path": {"type": "string"}}, "required": ["kind", "path"], "additionalProperties": False}}, "confirmation": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"required": {"const": True}, "summary": {"type": "string", "minLength": 1, "maxLength": 2000}}, "required": ["required", "summary"], "additionalProperties": False}]}}, "additionalProperties": False}}


def handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method, request_id = request.get("method"), request.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": (request.get("params") or {}).get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}}, "serverInfo": {"name": "telegram-bridge-payload", "version": "1.0.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [TOOL]}}
    if method == "tools/call":
        params = request.get("params") or {}
        if params.get("name") != "set_payload":
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            write_payload(params.get("arguments"))
            result = {"content": [{"type": "text", "text": "Telegram payload recorded."}]}
        except (OSError, ValueError) as exc:
            result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}}


def main() -> None:
    for line in sys.stdin:
        try:
            response = handle(json.loads(line))
        except (EOFError, json.JSONDecodeError):
            continue
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
