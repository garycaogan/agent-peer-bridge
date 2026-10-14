"""Codex event regressions and process failures; no model requests or CLI installation needed."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from apb.backends.codex import CodexBackend
from apb.executor import Executor
from apb.relay import Relay


class CodexEventsTest(unittest.TestCase):
    def test_thread_and_completed_items(self):
        sid = "0199a213-81c0-7800-8aa1-bbab2a035a53"
        self.assertEqual(CodexBackend.native_id_from({"type": "thread.started", "thread_id": sid}), sid)
        message = {"type": "item.completed", "item": {"type": "agent_message", "text": "ready"}}
        self.assertEqual(CodexBackend.events(message), [{"role": "assistant", "text": "ready"}])
        command = {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd",
                                                       "aggregated_output": "/tmp/work\n", "exit_code": 0,
                                                       "status": "completed"}}
        self.assertEqual(CodexBackend.events(command), [{"role": "tool", "tool": "shell", "hint": "pwd"}])

    def test_failure_message_shapes(self):
        for record in [{"type": "error", "message": "top-level failure"},
                       {"type": "turn.failed", "error": {"message": "nested failure"}},
                       {"type": "turn.failed", "error": "string failure"}]:
            with self.subTest(record=record):
                event = CodexBackend.events(record)[0]
                self.assertTrue(event["is_error"])
                self.assertIn("failure", event["text"])
                self.assertNotEqual(event["text"], "turn failed")

    def test_failed_command_without_exit_code(self):
        event = CodexBackend.events({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pwd", "exit_code": None, "status": "failed"}})[0]
        self.assertTrue(event["error"])

    def test_nested_agent_environment_is_removed(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "parent", "CODEX_SESSION_ID": "parent",
                                     "CLAUDECODE": "1", "ANTHROPIC_API_KEY": "parent-secret",
                                     "APB_ENV_TEST": "keep"}):
            env = CodexBackend.clean_env()
        self.assertEqual(env["APB_ENV_TEST"], "keep")
        for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "ANTHROPIC_API_KEY"):
            self.assertNotIn(key, env)


class ExecutorFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="apb-codex-test-")
        self.root = Path(self.tmp.name)
        fake = self.root / "codex"
        fake.write_text("#!" + sys.executable + "\n" +
                        "import json, sys\n"
                        "text = sys.stdin.read()\n"
                        "if text == 'success':\n"
                        " print(json.dumps({'type':'thread.started','thread_id':'test-thread'}))\n"
                        " print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'ready'}}))\n"
                        " print('startup warning', file=sys.stderr)\n"
                        " sys.exit(0)\n"
                        "print('Error: no rollout found; ' + text, file=sys.stderr)\n"
                        "sys.exit(7 if text == 'first' else 9)\n")
        fake.chmod(0o700)
        self.executor = Executor({"state_dir": str(self.root / "state"), "workspaces": {"scratch": str(self.root)},
                                  "backend": "codex", "backend_options": {"binary": str(fake)}, "max_running": 2})

    def tearDown(self):
        for process in list(self.executor.processes.values()):
            if process.poll() is None:
                process.kill()
            process.wait()
        self.tmp.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.executor.pump()
            if not self.executor.running():
                return
            time.sleep(.01)
        self.fail("test agent did not finish")

    def test_concurrent_stderr_only_failures_keep_exit_status(self):
        first = self.executor.op_start_session(self.executor.policy(None), "first", "scratch")["session_id"]
        second = self.executor.op_start_session(self.executor.policy(None), "second", "scratch")["session_id"]
        self.wait_idle()
        for sid, code in [(first, 7), (second, 9)]:
            item = self.executor.sessions[sid]
            result = item["last_result"]
            self.assertTrue(result["is_error"])
            self.assertEqual(result["exit_code"], code)
            self.assertIn("no rollout found", result["result"])
            self.assertEqual(Path(item["stderr_log"]).stat().st_mode & 0o777, 0o600)

    def test_success_does_not_expose_stderr(self):
        sid = self.executor.op_start_session(self.executor.policy(None), "success", "scratch")["session_id"]
        self.wait_idle()
        self.assertEqual(self.executor.sessions[sid]["last_result"]["result"], "ready")
        self.assertFalse(self.executor.sessions[sid]["last_result"]["is_error"])

    def test_configured_token_is_redacted_from_failure(self):
        self.executor.backend.token = "test-private-token"
        sid = self.executor.op_start_session(self.executor.policy(None), "test-private-token", "scratch")["session_id"]
        self.wait_idle()
        result = self.executor.sessions[sid]["last_result"]["result"]
        self.assertNotIn("test-private-token", result)
        self.assertIn("[REDACTED]", result)


class RelayConnectionTest(unittest.TestCase):
    def test_connection_closes_and_transaction_rolls_back(self):
        with tempfile.TemporaryDirectory(prefix="apb-db-test-") as tmp:
            relay = Relay(str(Path(tmp) / "relay.db"))
            with self.assertRaises(RuntimeError):
                with relay.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("INSERT INTO principals(name,kind,token_hash,created_at) VALUES ('p','peer','hash',0)")
                    raise RuntimeError("rollback")
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
            with relay.db() as reopened:
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM principals").fetchone()[0], 0)




class CodexPerCallerTest(unittest.TestCase):
    def test_rules_lead_the_prompt_and_sandbox_follows_the_caller(self):
        backend = CodexBackend({"binary": "/usr/bin/true"})
        self.assertEqual(backend.prompt("do it", None), "do it")
        wrapped = backend.prompt("do it", "Only touch the docs folder.")
        self.assertTrue(wrapped.index("Only touch the docs folder.") < wrapped.index("do it"))
        argv, _ = backend.command(native_id=None, first_turn=True, fork_native=None, model=None, effort=None,
                                  title=None, permission={"sandbox": "read-only"}, rules="x")
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
        argv, _ = backend.command(native_id="t1", first_turn=False, fork_native=None, model=None, effort=None, title=None)
        self.assertEqual(argv[argv.index("--sandbox") + 1], "workspace-write")


if __name__ == "__main__":
    unittest.main()
