"""Codex backend: headless `codex exec --json` turns, resumed by the thread id Codex reports.

Verified with codex-cli 0.162.0-alpha.17.2: two real turns, stdin prompts and a shell command.
Codex picks its own session id, so the executor learns it from thread.started before resuming.
"""
from __future__ import annotations

import os
import shutil


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


class CodexBackend:
    name = "codex"
    preassigns_ids = False
    supports_fork = False
    # Per-caller permission settings the machine owner may set in the executor config.
    permission_keys = {"sandbox": {"read-only", "workspace-write", "danger-full-access"}}

    def __init__(self, options):
        self.binary = options.get("binary") or shutil.which("codex")
        if not self.binary:
            raise RuntimeError("codex not found: install the Codex CLI or set backend_options.binary")
        # Fixed by the machine owner in config; remote callers can never change these.
        self.sandbox = options.get("sandbox", "workspace-write")
        self.extra = list(options.get("extra_args", []))

    def command(self, *, native_id, first_turn, fork_native, model, effort, title, permission=None, rules=None):
        if fork_native:
            raise ValueError("the codex backend cannot fork sessions")
        sandbox = (permission or {}).get("sandbox", self.sandbox)
        argv = [self.binary, "exec", "--json", "--skip-git-repo-check", "--sandbox", sandbox,
                "-c", "approval_policy=never", *self.extra]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        if not first_turn:
            argv += ["resume", native_id]
        argv.append("-")  # read the prompt from stdin
        return argv, {}

    @staticmethod
    def prompt(text, rules):
        # The owner's rules for this caller lead every turn, so they hold on resumed threads too.
        if not rules:
            return text
        return f"[Rules from the machine owner for this requester]\n{rules}\n\n[Request]\n{text}"

    @staticmethod
    def native_id_from(record):
        if record.get("type") == "thread.started":
            return record.get("thread_id")
        message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
        if message.get("type") == "session_configured":  # older event format
            return message.get("session_id")
        return None

    @staticmethod
    def events(record):
        kind = record.get("type")
        item = record.get("item") if isinstance(record.get("item"), dict) else {}
        if kind == "item.completed":
            if item.get("type") == "agent_message":
                return [{"role": "assistant", "text": _clip(item.get("text", ""), 2000)}]
            if item.get("type") == "command_execution":
                return [{"role": "tool", "tool": "shell", "hint": _clip(str(item.get("command", "")), 200),
                         **({"error": True} if item.get("status") == "failed" or
                            item.get("exit_code") not in (0, None) else {})}]
            if item.get("type") in ("file_change", "mcp_tool_call", "web_search"):
                return [{"role": "tool", "tool": item["type"]}]
        if kind in ("turn.failed", "error"):
            error = record.get("error")
            text = (error.get("message") if isinstance(error, dict) else error) or record.get("message")
            return [{"role": "result", "is_error": True, "text": _clip(str(text or "turn failed"), 4000)}]
        message = record.get("msg") if isinstance(record.get("msg"), dict) else {}
        if message.get("type") == "agent_message":  # older event format
            return [{"role": "assistant", "text": _clip(message.get("message", ""), 2000)}]
        return []

    def native_sessions(self, cwd):
        return []  # read-only listing of the user's own Codex sessions is not implemented yet

    def native_records(self, cwd, native_id):
        return []

    @staticmethod
    def clean_env():
        return {key: value for key, value in os.environ.items()
                if not key.startswith(("CLAUDE", "ANTHROPIC")) and
                key not in {"CODEX_SESSION_ID", "CODEX_THREAD_ID"}}
