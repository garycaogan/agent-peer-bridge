#!/usr/bin/env python3
"""agent-peer-bridge relay: a small durable queue between coding agents on different machines.

Callers (an agent's MCP adapter) enqueue requests for a named peer. Peers (an executor on
another machine) long-poll for work over outbound HTTPS, run it against their local agent and
post one result back. Nobody opens an inbound port except the relay itself.

Stdlib only. Tokens are stored as SHA-256 hashes and shown once at creation.

    python3 -m apb.relay --db relay.db add-peer claude-mini
    python3 -m apb.relay --db relay.db add-caller codex-main --peers claude-mini
    python3 -m apb.relay --db relay.db serve --host 127.0.0.1 --port 8787
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUEUE_TTL = 3600          # a queued request expires if no peer claims it within an hour
DELIVERY_TTL = 900        # a claimed request must be answered within 15 minutes, else "uncertain"
RECEIVER_TTL = 90         # one live receiver lease per peer; renewed by every poll
MAX_IN_FLIGHT = 4
MAX_BODY = 64 * 1024
MAX_RESULT = 48 * 1024
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,100}")

# The relay validates the shape of every operation, so a compromised caller cannot hand a peer
# anything outside this contract. The peer validates again before acting.
OPERATIONS = {
    "list_sessions": {"optional": {"limit", "cursor"}, "required": set()},
    "read_session": {"optional": {"limit", "cursor"}, "required": {"session_id"}},
    "start_session": {"optional": {"title", "model", "effort", "fork_from"}, "required": {"text", "workspace"}},
    "send_message": {"optional": set(), "required": {"session_id", "text"}},
    "stop_session": {"optional": set(), "required": {"session_id"}},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS principals (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('peer','caller')),
    token_hash TEXT NOT NULL UNIQUE, allowed_peers TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL, revoked_at REAL,
    receiver_id TEXT, receiver_until REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, caller TEXT NOT NULL, peer TEXT NOT NULL, client_id TEXT NOT NULL,
    operation TEXT NOT NULL, args_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued','claimed','completed','failed','uncertain','expired','cancelled')),
    created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL NOT NULL,
    delivery_hash TEXT, delivery_deadline REAL, result_json TEXT, is_error INTEGER,
    UNIQUE(caller, peer, client_id)
);
CREATE INDEX IF NOT EXISTS queue ON requests(peer, status, created_at);
"""


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def canonical(value, limit, field):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPError(400, f"invalid {field}") from None
    if len(text.encode()) > limit:
        raise HTTPError(413, f"{field} too large")
    return text


def validate_operation(operation, args):
    spec = OPERATIONS.get(operation)
    if spec is None or not isinstance(args, dict):
        raise HTTPError(400, "unsupported operation")
    keys = set(args)
    if not spec["required"] <= keys or keys - spec["required"] - spec["optional"]:
        raise HTTPError(400, "unsupported arguments")
    for key, value in args.items():
        if key in {"session_id", "fork_from", "workspace", "model", "effort"}:
            ok = isinstance(value, str) and NAME.fullmatch(value)
        elif key == "limit":
            ok = type(value) is int and 1 <= value <= 100
        else:  # text, title, cursor
            ok = isinstance(value, str) and 0 < len(value) <= 16000
        if not ok:
            raise HTTPError(400, f"invalid {key}")


class Relay:
    def __init__(self, path):
        self.path = path
        with self.db() as db:
            db.executescript(SCHEMA)

    def db(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    # ---- administration (local CLI only) -------------------------------------------------------
    def add(self, name, kind, peers=()):
        if not NAME.fullmatch(name):
            raise SystemExit("names use letters, digits and . _ : -")
        token = f"apb_{kind}_" + secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("INSERT INTO principals(name,kind,token_hash,allowed_peers,created_at) VALUES (?,?,?,?,?)",
                       (name, kind, digest(token), json.dumps(sorted(peers)), time.time()))
        return token

    def revoke(self, name):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE principals SET revoked_at=COALESCE(revoked_at,?) WHERE name=?", (time.time(), name))
            db.execute("UPDATE requests SET status=CASE status WHEN 'queued' THEN 'cancelled' ELSE 'uncertain' END,"
                       "updated_at=? WHERE (peer=? OR caller=?) AND status IN ('queued','claimed')",
                       (time.time(), name, name))

    # ---- authentication ------------------------------------------------------------------------
    def principal(self, db, token, kind):
        if not isinstance(token, str) or not token.startswith(f"apb_{kind}_"):
            raise HTTPError(401, "invalid credential")
        row = db.execute("SELECT * FROM principals WHERE token_hash=? AND kind=? AND revoked_at IS NULL",
                         (digest(token), kind)).fetchone()
        if row is None:
            raise HTTPError(401, "invalid credential")
        return row

    @staticmethod
    def sweep(db, now):
        db.execute("UPDATE requests SET status='expired',updated_at=? WHERE status='queued' AND expires_at<=?", (now, now))
        db.execute("UPDATE requests SET status='uncertain',updated_at=? WHERE status='claimed' AND delivery_deadline<=?",
                   (now, now))

    @staticmethod
    def view(row):
        return {"id": row["id"], "peer": row["peer"], "client_id": row["client_id"], "operation": row["operation"],
                "args": json.loads(row["args_json"]), "status": row["status"], "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
                "is_error": None if row["is_error"] is None else bool(row["is_error"])}

    # ---- caller side ---------------------------------------------------------------------------
    def enqueue(self, token, body):
        peer, operation, args, client_id = (body.get(k) for k in ("peer", "operation", "args", "client_id"))
        if not (isinstance(peer, str) and NAME.fullmatch(peer)) or not (isinstance(client_id, str) and NAME.fullmatch(client_id)):
            raise HTTPError(400, "invalid peer or client_id")
        validate_operation(operation, args)
        args_json = canonical(args, MAX_BODY, "args")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self.principal(db, token, "caller")
            if peer not in json.loads(caller["allowed_peers"]):
                raise HTTPError(403, "caller may not address this peer")
            target = db.execute("SELECT 1 FROM principals WHERE name=? AND kind='peer' AND revoked_at IS NULL",
                                (peer,)).fetchone()
            if target is None:
                raise HTTPError(404, "unknown peer")
            now = time.time()
            self.sweep(db, now)
            existing = db.execute("SELECT * FROM requests WHERE caller=? AND peer=? AND client_id=?",
                                  (caller["name"], peer, client_id)).fetchone()
            if existing is not None:  # idempotent: the same client_id never creates a second request
                if existing["operation"] != operation or existing["args_json"] != args_json:
                    raise HTTPError(409, "client_id already used for a different request")
                return self.view(existing)
            request_id = "req_" + uuid.uuid4().hex
            db.execute("INSERT INTO requests(id,caller,peer,client_id,operation,args_json,status,created_at,updated_at,"
                       "expires_at) VALUES (?,?,?,?,?,?,'queued',?,?,?)",
                       (request_id, caller["name"], peer, client_id, operation, args_json, now, now, now + QUEUE_TTL))
            return self.view(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def status(self, token, request_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self.principal(db, token, "caller")
            self.sweep(db, time.time())
            row = db.execute("SELECT * FROM requests WHERE id=? AND caller=?", (request_id, caller["name"])).fetchone()
            if row is None:
                raise HTTPError(404, "unknown request")
            return self.view(row)

    def cancel(self, token, request_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            caller = self.principal(db, token, "caller")
            row = db.execute("SELECT * FROM requests WHERE id=? AND caller=?", (request_id, caller["name"])).fetchone()
            if row is None:
                raise HTTPError(404, "unknown request")
            # A claimed request may already be running on the peer; never claim that it stopped.
            new = {"queued": "cancelled", "claimed": "uncertain"}.get(row["status"])
            if new:
                db.execute("UPDATE requests SET status=?,updated_at=? WHERE id=?", (new, time.time(), request_id))
            return self.view(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def peers(self, token):
        with self.db() as db:
            caller = self.principal(db, token, "caller")
            now, result = time.time(), []
            for name in json.loads(caller["allowed_peers"]):
                row = db.execute("SELECT * FROM principals WHERE name=? AND kind='peer'", (name,)).fetchone()
                if row is not None:
                    result.append({"peer": name, "revoked": row["revoked_at"] is not None,
                                   "online": bool(row["receiver_until"] and row["receiver_until"] > now),
                                   "last_seen": row["last_seen"]})
            return {"peers": result}

    # ---- peer side -----------------------------------------------------------------------------
    def lease(self, db, peer, receiver_id, now):
        if not (isinstance(receiver_id, str) and NAME.fullmatch(receiver_id)):
            raise HTTPError(400, "invalid receiver_id")
        if peer["receiver_id"] not in (None, receiver_id) and (peer["receiver_until"] or 0) > now:
            raise HTTPError(409, "another receiver holds this peer")
        db.execute("UPDATE principals SET receiver_id=?,receiver_until=?,last_seen=? WHERE name=?",
                   (receiver_id, now + RECEIVER_TTL, now, peer["name"]))

    def claim(self, token, receiver_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            peer = self.principal(db, token, "peer")
            now = time.time()
            self.lease(db, peer, receiver_id, now)
            self.sweep(db, now)
            busy = db.execute("SELECT COUNT(*) FROM requests WHERE peer=? AND status='claimed'", (peer["name"],)).fetchone()[0]
            if busy >= MAX_IN_FLIGHT:
                return None
            row = db.execute("SELECT * FROM requests WHERE peer=? AND status='queued' ORDER BY created_at LIMIT 1",
                             (peer["name"],)).fetchone()
            if row is None:
                return None
            delivery = "apb_delivery_" + secrets.token_urlsafe(32)
            db.execute("UPDATE requests SET status='claimed',delivery_hash=?,delivery_deadline=?,updated_at=? WHERE id=?",
                       (digest(delivery), now + DELIVERY_TTL, now, row["id"]))
            claimed = self.view(db.execute("SELECT * FROM requests WHERE id=?", (row["id"],)).fetchone())
            return {key: claimed[key] for key in ("id", "operation", "args")} | {"delivery_token": delivery}

    def receive(self, token, body):
        wait = body.get("wait_seconds", 0)
        if type(wait) is not int or not 0 <= wait <= 25:
            raise HTTPError(400, "wait_seconds must be 0..25")
        deadline = time.monotonic() + wait
        while True:
            request = self.claim(token, body.get("receiver_id"))
            if request is not None or time.monotonic() >= deadline:
                return {"request": request}
            time.sleep(0.5)

    def reply(self, token, body):
        request_id, delivery, result, is_error = (body.get(k) for k in ("request_id", "delivery_token", "result", "is_error"))
        if type(is_error) is not bool or not isinstance(delivery, str):
            raise HTTPError(400, "invalid reply")
        result_json = canonical(result, MAX_RESULT, "result")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            peer = self.principal(db, token, "peer")
            now = time.time()
            self.sweep(db, now)
            row = db.execute("SELECT * FROM requests WHERE id=? AND peer=?", (request_id, peer["name"])).fetchone()
            if row is None or not row["delivery_hash"] or not hmac.compare_digest(row["delivery_hash"], digest(delivery)):
                raise HTTPError(401, "invalid delivery credential")
            if row["status"] in ("completed", "failed"):
                if row["result_json"] == result_json and bool(row["is_error"]) == is_error:
                    return self.view(row)  # a retried identical reply is harmless
                raise HTTPError(409, "request already has a different result")
            if row["status"] != "claimed":
                raise HTTPError(409, f"request is {row['status']}, not awaiting a result")
            db.execute("UPDATE requests SET status=?,result_json=?,is_error=?,updated_at=? WHERE id=?",
                       ("failed" if is_error else "completed", result_json, int(is_error), now, request_id))
            return self.view(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())


class Handler(BaseHTTPRequestHandler):
    relay: Relay
    server_version = "agent-peer-bridge"

    def log_message(self, fmt, *args):  # keep request bodies and tokens out of logs
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def token(self):
        header = self.headers.get("Authorization", "")
        return header[7:] if header.startswith("Bearer ") else ""

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise HTTPError(413, "body too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HTTPError(400, "invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPError(400, "body must be an object")
        return value

    def route(self, method):
        relay, path = self.relay, self.path.split("?", 1)[0]
        if method == "GET" and path == "/v1/health":
            return {"ok": True}
        if method == "GET" and path == "/v1/peers":
            return relay.peers(self.token())
        if method == "POST" and path == "/v1/requests":
            return relay.enqueue(self.token(), self.body())
        match = re.fullmatch(r"/v1/requests/(req_[0-9a-f]{32})(/cancel)?", path)
        if match and method == "GET" and not match.group(2):
            return relay.status(self.token(), match.group(1))
        if match and method == "POST" and match.group(2):
            return relay.cancel(self.token(), match.group(1))
        if method == "POST" and path == "/v1/peer/receive":
            return relay.receive(self.token(), self.body())
        if method == "POST" and path == "/v1/peer/reply":
            return relay.reply(self.token(), self.body())
        raise HTTPError(404, "not found")

    def handle_method(self, method):
        try:
            self.send(200, self.route(method))
        except HTTPError as error:
            self.send(error.status, {"error": str(error)})
        except Exception:  # never leak internals
            self.send(500, {"error": "internal error"})

    def do_GET(self):
        self.handle_method("GET")

    def do_POST(self):
        self.handle_method("POST")


def serve(relay, host, port):
    handler = type("BoundHandler", (Handler,), {"relay": relay})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="relay.db")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("add-peer").add_argument("name")
    caller = sub.add_parser("add-caller")
    caller.add_argument("name")
    caller.add_argument("--peers", required=True, help="comma-separated peer names this caller may address")
    sub.add_parser("revoke").add_argument("name")
    run = sub.add_parser("serve")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8787)
    args = parser.parse_args(argv)
    relay = Relay(args.db)
    if args.command == "add-peer":
        print(relay.add(args.name, "peer"))
    elif args.command == "add-caller":
        print(relay.add(args.name, "caller", [p.strip() for p in args.peers.split(",") if p.strip()]))
    elif args.command == "revoke":
        relay.revoke(args.name)
    else:
        server = serve(relay, args.host, args.port)
        print(f"relay listening on http://{args.host}:{args.port} (put TLS in front before exposing it)", file=sys.stderr)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            server.shutdown()


if __name__ == "__main__":
    main()
