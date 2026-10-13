"""End-to-end and contract tests: real relay over HTTP, executor with a fake agent CLI, MCP adapter."""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apb import relay as relay_mod  # noqa: E402
from apb.backends import claude_code  # noqa: E402
from apb.executor import Executor  # noqa: E402
from apb.http import RelayClient, RelayError  # noqa: E402
from apb.mcp_server import Adapter, serve  # noqa: E402

FAKE_AGENT = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
assert args[args.index("--permission-mode") + 1] == "auto", "permission mode must come from local config"
sid = args[args.index("--session-id") + 1] if "--session-id" in args else args[args.index("--resume") + 1]
state = os.path.join({state!r}, sid)
turn = (int(open(state).read()) if os.path.exists(state) else 0) + 1
open(state, "w").write(str(turn))
prompt = sys.stdin.read()
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid}}))
print(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "tool_use", "name": "Bash", "input": {{"command": "pwd"}}}},
    {{"type": "text", "text": "turn %d: %s" % (turn, prompt)}}]}}}}))
print(json.dumps({{"type": "result", "is_error": False, "result": "done %d" % turn}}))
'''


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apb-test-"))
        self.relay = relay_mod.Relay(str(self.tmp / "relay.db"))
        self.peer_token = self.relay.add("mini", "peer")
        self.caller_token = self.relay.add("main", "caller", ["mini"])
        self.server = relay_mod.serve(self.relay, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

        claude_code.HOME = self.tmp / "home"
        workspace = self.tmp / "ws"
        workspace.mkdir()
        native_dir = claude_code.HOME / ".claude" / "projects" / claude_code.re.sub(r"[^A-Za-z0-9]", "-", str(workspace.resolve()))
        native_dir.mkdir(parents=True)
        (native_dir / "user-own.jsonl").write_text(json.dumps({"type": "user", "message": {"content": "my own work"}}) + "\n")
        fake = self.tmp / "claude"
        fake.write_text(FAKE_AGENT.format(python=sys.executable, state=str(self.tmp)))
        fake.chmod(0o700)
        config = {"state_dir": str(self.tmp / "state"), "workspaces": {"main": str(workspace)},
                  "backend": "claude-code", "backend_options": {"binary": str(fake)}}
        self.executor = Executor(config, RelayClient(self.url, self.peer_token))
        self.stop = threading.Event()
        self.worker = threading.Thread(target=self.run_executor, daemon=True)
        self.worker.start()
        self.adapter = Adapter(RelayClient(self.url, self.caller_token))

    def run_executor(self):
        import apb.executor as executor_mod
        executor_mod.POLL_SECONDS = 1
        while not self.stop.is_set():
            self.executor.pump()
            self.executor.serve_once()

    def tearDown(self):
        self.stop.set()
        self.worker.join(timeout=3)
        for process in list(self.executor.processes.values()):
            process.wait(timeout=5)
        self.executor.pump()
        self.server.shutdown()
        self.server.server_close()

    def call(self, name, **args):
        return self.adapter.call(name, args)

    def wait_idle(self, sid, turns):
        for _ in range(100):
            status = self.call("read_session", peer="mini", session_id=sid)["result"]
            if status["turns"] >= turns and status["status"] == "idle":
                return status
            time.sleep(0.2)
        self.fail("session never went idle")

    def test_full_flow(self):
        started = self.call("start_session", peer="mini", workspace="main", text="first", client_id="start-0001")
        self.assertEqual(started["status"], "completed", started)
        sid = started["result"]["session_id"]
        again = self.call("start_session", peer="mini", workspace="main", text="first", client_id="start-0001")
        self.assertEqual(again["id"], started["id"], "same client_id must not start a second session")
        self.call("send_message", peer="mini", session_id=sid, text="second", client_id="send-0001")
        done = self.wait_idle(sid, 2)
        texts = [e.get("text") for e in done["events"]]
        self.assertIn("turn 1: first", texts)
        self.assertIn("turn 2: second", texts, "the follow-up must resume the same session")
        self.assertEqual(done["last_result"]["result"], "done 2")
        listing = self.call("list_sessions", peer="mini")["result"]
        kinds = {row["kind"] for row in listing["sessions"]}
        self.assertEqual(kinds, {"bridge", "native_read_only"})
        native = next(r["session_id"] for r in listing["sessions"] if r["kind"] == "native_read_only")
        read = self.call("read_session", peer="mini", session_id=native)["result"]
        self.assertEqual(read["events"][0]["text"], "my own work")
        refused = self.call("send_message", peer="mini", session_id=native, text="x", client_id="send-0002")
        self.assertTrue(refused["is_error"] and "read-only" in refused["result"]["reason"], refused)
        forked = self.call("start_session", peer="mini", workspace="main", text="branch", fork_from=native, client_id="fork-0001")
        self.assertFalse(forked["is_error"], forked)
        stopped = self.call("stop_session", peer="mini", session_id=sid, client_id="stop-0001")
        self.assertTrue(stopped["result"]["stop_requested"])

    def test_executor_rejects_what_config_does_not_allow(self):
        bad_ws = self.call("start_session", peer="mini", workspace="elsewhere", text="x", client_id="bad-00001")
        self.assertTrue(bad_ws["is_error"] and "unknown workspace" in bad_ws["result"]["reason"])
        with self.assertRaises(RelayError) as traversal:  # path tricks never reach the executor
            self.call("read_session", peer="mini", session_id="native:main:../../etc")
        self.assertEqual(traversal.exception.code, "http_400")
        unknown = self.call("read_session", peer="mini", session_id="native:main:not-a-session")
        self.assertTrue(unknown["is_error"] and unknown["result"]["reason"] == "unknown session_id", unknown)

    def test_relay_contract(self):
        caller = RelayClient(self.url, self.caller_token)
        with self.assertRaises(RelayError) as denied:
            caller.call("POST", "/v1/requests", {"peer": "mini", "operation": "start_session", "client_id": "perm-0001",
                                                 "args": {"text": "x", "workspace": "main", "permission_mode": "bypass"}})
        self.assertEqual(denied.exception.code, "http_400", "callers cannot smuggle extra arguments")
        with self.assertRaises(RelayError) as other_peer:
            caller.call("POST", "/v1/requests", {"peer": "elsewhere", "operation": "list_sessions", "args": {}, "client_id": "peer-0001"})
        self.assertEqual(other_peer.exception.code, "http_403")
        with self.assertRaises(RelayError) as as_peer:
            RelayClient(self.url, self.peer_token).call("GET", "/v1/peers")
        self.assertEqual(as_peer.exception.code, "http_401", "a peer token is not a caller token")
        caller.call("POST", "/v1/requests", {"peer": "mini", "operation": "list_sessions", "args": {}, "client_id": "dupe-0001"})
        with self.assertRaises(RelayError) as reused:
            caller.call("POST", "/v1/requests", {"peer": "mini", "operation": "list_sessions", "args": {"limit": 3},
                                                 "client_id": "dupe-0001"})
        self.assertEqual(reused.exception.code, "http_409")


class RelayStateTest(unittest.TestCase):
    def setUp(self):
        self.relay = relay_mod.Relay(str(Path(tempfile.mkdtemp()) / "relay.db"))
        self.peer = self.relay.add("p", "peer")
        self.caller = self.relay.add("c", "caller", ["p"])

    def enqueue(self, client_id="client-01"):
        return self.relay.enqueue(self.caller, {"peer": "p", "operation": "list_sessions", "args": {}, "client_id": client_id})

    def test_lease_delivery_and_deadline(self):
        request = self.enqueue()
        with self.assertRaises(relay_mod.HTTPError):
            self.relay.claim(self.peer, "rx:one") and self.relay.claim(self.peer, "rx:two")
        claimed = self.relay.receive(self.peer, {"receiver_id": "rx:one", "wait_seconds": 0})["request"]
        self.assertIsNone(claimed, "the first claim above already took the only request")
        with self.relay.db() as db:  # force the delivery deadline into the past
            db.execute("UPDATE requests SET delivery_deadline=0 WHERE id=?", (request["id"],))
        self.assertEqual(self.relay.status(self.caller, request["id"])["status"], "uncertain")

    def test_wrong_delivery_token_and_cancel(self):
        request = self.enqueue()
        claimed = self.relay.claim(self.peer, "rx:one")
        with self.assertRaises(relay_mod.HTTPError):
            self.relay.reply(self.peer, {"request_id": request["id"], "delivery_token": "apb_delivery_wrong",
                                         "result": {}, "is_error": False})
        self.assertEqual(self.relay.cancel(self.caller, request["id"])["status"], "uncertain",
                         "a claimed request may be running; cancelling must not claim it stopped")
        queued = self.enqueue("client-02")
        self.assertEqual(self.relay.cancel(self.caller, queued["id"])["status"], "cancelled")
        self.assertTrue(claimed["delivery_token"].startswith("apb_delivery_"))

    def test_revoke(self):
        self.enqueue()
        self.relay.revoke("p")
        with self.assertRaises(relay_mod.HTTPError):
            self.relay.claim(self.peer, "rx:one")


class McpFramingTest(unittest.TestCase):
    def test_peer_rejection_sets_mcp_is_error(self):
        class Stub:
            def call(self, name, args):
                return {"status": "failed", "is_error": True, "result": {"outcome": "rejected"}}
        frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "start_session", "arguments": {
                "peer": "p", "workspace": "scratch", "text": "x", "client_id": "test-0001"}}}
        out = io.StringIO()
        serve(Stub(), io.StringIO(json.dumps(frame) + "\n"), out)
        self.assertTrue(json.loads(out.getvalue())["result"]["isError"])

    def test_meta_and_unknown_fields_are_tolerated(self):
        class Stub:
            def call(self, name, args):
                return {"ok": name}
        frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                  {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"_meta": {}}},
                  {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "list_peers", "arguments": {}, "_meta": {"progressToken": 3}}}]
        out = io.StringIO()
        serve(Stub(), io.StringIO("".join(json.dumps(f) + "\n" for f in frames)), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertNotIn("error", replies[2], "a _meta field must not break tool calls")


if __name__ == "__main__":
    unittest.main()
