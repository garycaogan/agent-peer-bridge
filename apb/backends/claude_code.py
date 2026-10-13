"""Claude Code backend: headless `claude -p` turns with caller-independent permission settings."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from ..http import read_secret

HOME = Path.home()


def _binary(configured):
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    # Claude Desktop on macOS ships its own Claude Code build; use the newest one if no CLI is installed.
    bundles = HOME / "Library/Application Support/Claude/claude-code"
    candidates = []
    for path in bundles.glob("*/*/claude.app/Contents/MacOS/claude"):
        version = path.parts[-6]
        if re.fullmatch(r"\d+(\.\d+)*", version):
            candidates.append((tuple(int(part) for part in version.split(".")), str(path)))
    if not candidates:
        raise RuntimeError("Claude Code not found: install the CLI or set backend_options.binary")
    return max(candidates)[1]


def _clip(text, limit):
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


class ClaudeCodeBackend:
    name = "claude-code"
    preassigns_ids = True      # we pick the session UUID, so it is known before the first output line
    supports_fork = True

    def __init__(self, options):
        self.binary = _binary(options.get("binary"))
        # Fixed by the machine owner in config; remote callers can never change these.
        self.permission_mode = options.get("permission_mode", "auto")
        token_file = options.get("oauth_token_file")  # from `claude setup-token`, for unattended use
        self.token = read_secret(token_file) if token_file else None

    def command(self, *, native_id, first_turn, fork_native, model, effort, title):
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose",
                "--permission-mode", self.permission_mode, "--permission-prompts", "none"]
        if title:
            argv += ["-n", title]
        if model:
            argv += ["--model", model]
        if effort:
            argv += ["--effort", effort]
        if first_turn and fork_native:
            argv += ["--resume", fork_native, "--fork-session", "--session-id", native_id]
        elif first_turn:
            argv += ["--session-id", native_id]
        else:
            argv += ["--resume", native_id]
        env = {"CLAUDE_CODE_OAUTH_TOKEN": self.token} if self.token else {}
        return argv, env  # the prompt goes to stdin, so text starting with "-" is never parsed as a flag

    @staticmethod
    def native_id_from(record):
        return record.get("session_id") if record.get("type") == "system" and record.get("subtype") == "init" else None

    @staticmethod
    def events(record):
        kind = record.get("type")
        if kind in ("assistant", "user"):
            content = (record.get("message") or {}).get("content")
            if isinstance(content, str):
                return [{"role": kind, "text": _clip(content, 2000)}]
            out = []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    out.append({"role": kind, "text": _clip(block.get("text", ""), 2000)})
                elif block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    hint = data.get("description") or data.get("command") or data.get("file_path") or ""
                    out.append({"role": "tool", "tool": block.get("name"), "hint": _clip(str(hint), 200)})
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    out.append({"role": "tool", "error": True})
            return out
        if kind == "result":
            return [{"role": "result", "is_error": bool(record.get("is_error")),
                     "text": _clip(str(record.get("result", "")), 4000),
                     "permission_denials": len(record.get("permission_denials") or [])}]
        return []

    # ---- read-only view of sessions the user ran themselves (CLI or Claude Desktop) ------------
    @staticmethod
    def _project_dir(cwd):
        return HOME / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    def native_sessions(self, cwd):
        sessions = []
        for path in self._project_dir(cwd).glob("*.jsonl"):
            title = None
            try:
                with open(path, encoding="utf-8", errors="replace") as stream:
                    for _, line in zip(range(400), stream):
                        record = json.loads(line)
                        if record.get("type") == "custom-title":
                            title = record.get("customTitle") or title
                        elif title is None and record.get("type") == "user" and isinstance(
                                (record.get("message") or {}).get("content"), str):
                            title = record["message"]["content"][:80]
            except (OSError, ValueError):
                continue
            sessions.append({"native_id": path.stem, "title": title, "updated_at": path.stat().st_mtime})
        return sessions

    def native_records(self, cwd, native_id):
        path = self._project_dir(cwd) / f"{native_id}.jsonl"
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not record.get("isMeta") and not record.get("isSidechain"):
                records.append(record)
        return records

    @staticmethod
    def clean_env():
        # Never let a parent agent session's variables leak into the spawned one.
        return {key: value for key, value in os.environ.items()
                if not key.startswith(("CLAUDE", "ANTHROPIC")) and key != "CLAUDECODE"}
