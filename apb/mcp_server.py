#!/usr/bin/env python3
"""agent-peer-bridge MCP adapter: gives a local agent tools to work with agents on other machines.

Register it as a stdio MCP server in Claude Code, Codex or any MCP client:

    APB_RELAY_URL=https://relay.example.com APB_CALLER_TOKEN_FILE=~/.agent-peer-bridge/caller.token \
        python3 -m apb.mcp_server

Each tool enqueues one relay request and waits briefly for the peer's reply. Writes carry a
client_id: re-sending the same client_id is safe (the relay returns the original request), and a
different client_id is a new request. A timeout never means failure; check request_status.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

from .http import RelayClient, RelayError, read_secret

WAIT_SECONDS = 20
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


PEER = {"type": "string", "description": "peer name, see list_peers"}
CLIENT_ID = {"type": "string", "minLength": 8, "maxLength": 100,
             "description": "unique per intended action; reuse the same value when retrying that action"}
SESSION = {"type": "string"}
TOOLS = [
    {"name": "list_peers", "description": "List the machines this agent may send work to, and whether each is online.",
     "inputSchema": _schema({})},
    {"name": "start_session",
     "description": "Start a new agent session on a peer machine in one of its named workspaces. Returns once the "
                    "peer accepts it; the work continues there. Poll read_session for progress and the answer.",
     "inputSchema": _schema({"peer": PEER, "workspace": {"type": "string"}, "text": {"type": "string", "maxLength": 16000},
                             "title": {"type": "string", "maxLength": 120}, "model": {"type": "string"},
                             "effort": {"enum": ["low", "medium", "high", "xhigh", "max"]},
                             "fork_from": {"type": "string", "description": "session_id to branch from (keeps its context)"},
                             "client_id": CLIENT_ID}, ("peer", "workspace", "text", "client_id"))},
    {"name": "send_message",
     "description": "Continue a session you started on a peer. Queued in order if it is still working.",
     "inputSchema": _schema({"peer": PEER, "session_id": SESSION, "text": {"type": "string", "maxLength": 16000},
                             "client_id": CLIENT_ID}, ("peer", "session_id", "text", "client_id"))},
    {"name": "read_session", "description": "Read a peer session's recent events, status and last result.",
     "inputSchema": _schema({"peer": PEER, "session_id": SESSION, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                             "cursor": {"type": "string"}}, ("peer", "session_id"))},
    {"name": "list_sessions", "description": "List sessions on a peer: ones started through the bridge, and the "
                                             "owner's own sessions (read-only) in its workspaces.",
     "inputSchema": _schema({"peer": PEER, "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                             "cursor": {"type": "string"}}, ("peer",))},
    {"name": "stop_session", "description": "Interrupt a session you started on a peer. Does not undo finished actions.",
     "inputSchema": _schema({"peer": PEER, "session_id": SESSION, "client_id": CLIENT_ID}, ("peer", "session_id", "client_id"))},
    {"name": "request_status", "description": "Check a relay request whose outcome was unknown (pending or timed out).",
     "inputSchema": _schema({"request_id": {"type": "string"}}, ("request_id",))},
]
INSTRUCTIONS = ("Work on other machines through start_session/send_message, then read_session until the answer is there. "
                "Accepted is not finished. If a call times out, use request_status or retry with the same client_id; "
                "never invent a new client_id for the same action. Peers run with their owner's fixed permissions.")


class Adapter:
    def __init__(self, relay):
        self.relay = relay

    def request(self, peer, operation, args, client_id=None):
        created = self.relay.call("POST", "/v1/requests", {
            "peer": peer, "operation": operation, "args": args, "client_id": client_id or "read_" + uuid.uuid4().hex})
        return self.wait(created)

    def wait(self, request):
        deadline = time.monotonic() + WAIT_SECONDS
        while request["status"] in ("queued", "claimed") and time.monotonic() < deadline:
            time.sleep(1)
            request = self.relay.call("GET", f"/v1/requests/{request['id']}")
        return {key: request[key] for key in ("id", "status", "result", "is_error")} | (
            {"note": "still pending; call request_status later"} if request["status"] in ("queued", "claimed") else {}) | (
            {"note": "the peer took it but did not confirm the outcome; inspect before redoing anything"}
            if request["status"] == "uncertain" else {})

    def call(self, name, args):
        if name == "list_peers":
            return self.relay.call("GET", "/v1/peers")
        if name == "request_status":
            return self.wait(self.relay.call("GET", f"/v1/requests/{args['request_id']}"))
        peer, client_id = args.pop("peer"), args.pop("client_id", None)
        return self.request(peer, name, args, client_id)


def serve(adapter, stdin=sys.stdin, stdout=sys.stdout):
    def send(message):
        stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
        stdout.flush()

    for line in stdin:
        try:
            message = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if not isinstance(message, dict) or "id" not in message:
            continue  # notifications need no answer
        request_id, method, params = message["id"], message.get("method"), message.get("params") or {}
        if method == "initialize":
            version = params.get("protocolVersion")
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": version if version in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "agent-peer-bridge", "version": "0.1.0"},
                "instructions": INSTRUCTIONS}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "tools/list":  # params may carry cursor/_meta; there is a single page
            send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            # Clients add fields such as `_meta`; read what we need and ignore the rest.
            name, args = params.get("name"), dict(params.get("arguments") or {})
            spec = next((tool["inputSchema"] for tool in TOOLS if tool["name"] == name), None)
            if spec is None or set(args) - set(spec["properties"]) or not set(spec["required"]) <= set(args):
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid tool or arguments"}})
                continue
            try:
                data = adapter.call(name, args)
                is_error = bool(data.get("is_error")) or data.get("status") in (
                    "failed", "expired", "cancelled", "uncertain")
            except RelayError as error:
                data, is_error = {"error": error.code, "message": str(error), "uncertain": error.uncertain}, True
            send({"jsonrpc": "2.0", "id": request_id, "result": {
                "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": is_error}})
        else:
            send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})


def main():
    relay = RelayClient(os.environ["APB_RELAY_URL"], read_secret(os.environ["APB_CALLER_TOKEN_FILE"]))
    serve(Adapter(relay))


if __name__ == "__main__":
    main()
