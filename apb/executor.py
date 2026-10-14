#!/usr/bin/env python3
"""agent-peer-bridge executor: runs on the machine whose agent should take remote work.

It long-polls the relay with this machine's peer token (outbound only), and turns each claimed
request into the local agent's own headless sessions: start, continue, read, list, stop. Every new
task can get a fresh session, so no single conversation has to grow until it is compacted.

    python3 -m apb.executor --config ~/.agent-peer-bridge/executor.json

Rules this file enforces (see docs/security.md):
  * permission/sandbox settings come from the local config, never from a request;
  * work happens only in named workspaces mapped to directories in the local config;
  * sessions the user ran themselves are read-only here (fork them to continue);
  * every claimed request gets exactly one reply, and nothing is retried automatically.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .backends.claude_code import ClaudeCodeBackend
from .backends.codex import CodexBackend
from .http import RelayClient, RelayError, read_secret

BACKENDS = {"claude-code": ClaudeCodeBackend, "codex": CodexBackend}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
NAME = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
MAX_RESULT_BYTES = 40 * 1024
POLL_SECONDS = 20


class Rejected(Exception):
    """A request this executor will not run; reported back with is_error=true."""


CALLER_SETTINGS = {"workspaces", "rules", "rules_file", "permission", "read_owner_sessions", "see_all_sessions"}


class Policy:
    """What one caller may do on this machine. It comes from the executor config, never from a request."""

    def __init__(self, name, workspaces, rules=None, rules_file=None, permission=None,
                 read_owner_sessions=False, see_all_sessions=False):
        self.name, self.workspaces, self.permission = name, workspaces, permission or {}
        self._rules, self._rules_file = rules, rules_file
        self.read_owner_sessions, self.see_all_sessions = read_owner_sessions, see_all_sessions

    def rules(self):
        parts = [self._rules] if self._rules else []
        if self._rules_file:  # read on every turn, so edits apply without restarting the executor
            parts.append(Path(self._rules_file).expanduser().read_text(encoding="utf-8").strip())
        return "\n\n".join(part for part in parts if part) or None


def log(message):
    sys.stderr.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + message + "\n")
    sys.stderr.flush()


def write_private_json(path, value):
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=1)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def alive(pid):
    if not pid:
        return False, None
    try:
        done, status = os.waitpid(pid, os.WNOHANG)  # reap our own children
        if done:
            return False, os.waitstatus_to_exitcode(status)
        return True, None
    except ChildProcessError:  # not our child (executor restarted): fall back to a liveness probe
        try:
            os.kill(pid, 0)
            return True, None
        except ProcessLookupError:
            return False, None
        except PermissionError:
            return True, None


class Executor:
    def __init__(self, config, relay=None):
        self.state = Path(config.get("state_dir", "~/.agent-peer-bridge/state")).expanduser()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.state / "turns").mkdir(exist_ok=True, mode=0o700)
        # One executor per state directory: a second one would race on the registry and the queue.
        self._lock = open(self.state / "executor.lock", "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"another executor is already using {self.state}") from None
        self.workspaces = {name: str(Path(path).expanduser().resolve(strict=True))
                           for name, path in config["workspaces"].items()}
        self.backend = BACKENDS[config.get("backend", "claude-code")](config.get("backend_options", {}))
        self.callers = self.load_callers(config.get("callers"))
        self.max_running = int(config.get("max_running", 3))
        self.relay = relay
        self.registry_path = self.state / "sessions.json"
        self.sessions = json.loads(self.registry_path.read_text()) if self.registry_path.exists() else {}
        self.processes = {}  # keep child handles so another Popen cannot reap their exit status
        self.recover_interrupted_launches()
        receiver = self.state / "receiver.id"  # stable, so a restart resumes its own relay lease
        if not receiver.exists():
            receiver.write_text("exec_" + uuid.uuid4().hex)
        self.receiver_id = receiver.read_text().strip()

    def save(self):
        write_private_json(self.registry_path, self.sessions)

    # ---- who may do what -------------------------------------------------------------------------
    def load_callers(self, raw):
        """No `callers` section is open mode: every caller the relay admits gets every workspace."""
        if raw is None:
            return None
        callers = {}
        for name, spec in raw.items():
            unknown = set(spec) - CALLER_SETTINGS
            if unknown:
                raise SystemExit(f"caller {name}: unknown settings {sorted(unknown)}")
            wanted = spec.get("workspaces", "*")
            workspaces = sorted(self.workspaces) if wanted == "*" else list(wanted)
            if set(workspaces) - set(self.workspaces):
                raise SystemExit(f"caller {name}: unknown workspaces {sorted(set(workspaces) - set(self.workspaces))}")
            permission = spec.get("permission") or {}
            for key, value in permission.items():
                if value not in self.backend.permission_keys.get(key, ()):
                    raise SystemExit(f"caller {name}: {key}={value!r} is not a {self.backend.name} permission setting")
            callers[name] = Policy(name, workspaces, spec.get("rules"), spec.get("rules_file"), permission,
                                   bool(spec.get("read_owner_sessions")), bool(spec.get("see_all_sessions")))
        return callers

    def policy(self, caller):
        if self.callers is None:
            return Policy(caller or "anyone", sorted(self.workspaces), read_owner_sessions=True, see_all_sessions=True)
        if caller not in self.callers:
            raise Rejected("this requester is not allowed on this machine")
        return self.callers[caller]

    def session(self, who, session_id):
        """A bridge session, if this caller may see it; other callers' sessions look nonexistent."""
        item = self.sessions.get(session_id)
        if item is not None and (who.see_all_sessions or item.get("caller") == who.name):
            return item
        return None

    def recover_interrupted_launches(self):
        """A turn journaled as launching but never given a pid may or may not have run: never rerun it."""
        changed = False
        for item in self.sessions.values():
            if item.pop("launching", None) is not None and not item.get("pid"):
                dropped, item["queue"] = len(item["queue"]), []  # do not build on an unknown outcome
                item.update(status="uncertain", updated_at=time.time(), last_result={
                    "is_error": True, "exit_code": None, "dropped_queued_messages": dropped,
                    "result": "the executor stopped while starting this turn; it may or may not have run. "
                              "Read the session, then send what is still needed."})
                changed = True
        if changed:
            self.save()

    # ---- local supervision -----------------------------------------------------------------------
    def running(self):
        return sum(1 for item in self.sessions.values() if item.get("pid"))

    def launch(self, sid, text):
        item = self.sessions[sid]
        first = item["turns"] == 0
        try:  # the caller's current permissions and rules apply to every turn, including follow-ups
            who = self.policy(item.get("caller"))
            rules = who.rules()
        except (Rejected, OSError) as error:
            reason = "the requester is no longer allowed" if isinstance(error, Rejected) else "its rules file is unreadable"
            item.update(queue=[], status="blocked", updated_at=time.time(),
                        last_result={"is_error": True, "exit_code": None, "result": f"not started: {reason}"})
            self.save()
            log(f"blocked session={sid}: {reason}")
            return
        argv, extra_env = self.backend.command(native_id=item["native_id"], first_turn=first,
                                               fork_native=item.get("fork_native") if first else None,
                                               model=item.get("model"), effort=item.get("effort"),
                                               title=item["title"], permission=who.permission, rules=rules)
        text = self.backend.prompt(text, rules)
        out_path = self.state / "turns" / f"{sid}.{item['turns'] + 1}.jsonl"
        err_path = out_path.with_suffix(".stderr")
        # Journal the turn (message already dequeued) before starting it, so a crash between the two
        # can only leave an "uncertain" turn behind, never a message that runs twice.
        item["turns"] += 1
        item["logs"].append(str(out_path))
        item.update(launching=time.time(), status="launching", stderr_log=str(err_path), updated_at=time.time())
        self.save()
        with tempfile.TemporaryFile() as prompt, open(out_path, "wb") as out, open(err_path, "wb") as err:
            os.chmod(out_path, 0o600)
            os.chmod(err_path, 0o600)
            prompt.write(text.encode())
            prompt.seek(0)
            process = subprocess.Popen(argv, cwd=item["cwd"], env={**self.backend.clean_env(), **extra_env},
                                       stdin=prompt, stdout=out, stderr=err, start_new_session=True)
        self.processes[sid] = process
        item.pop("launching", None)
        item.update(pid=process.pid, status="running", updated_at=time.time())
        self.save()
        log(f"launched session={sid} turn={item['turns']} pid={process.pid}")

    @staticmethod
    def records(paths):
        for path in paths:
            try:
                lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record

    def pump(self):
        changed = False
        for sid, item in self.sessions.items():
            if not item["native_id"]:  # backends that pick their own id report it in the first events
                found = next((n for n in map(self.backend.native_id_from, self.records(item["logs"])) if n), None)
                if found:
                    item["native_id"], changed = found, True
            if item.get("pid"):
                process = self.processes.get(sid)
                if process is None:  # a turn that survived an executor restart
                    running, code = alive(item["pid"])
                else:
                    code = process.poll()
                    running = code is None
                if not running:
                    self.processes.pop(sid, None)
                    item.update(pid=None, updated_at=time.time(), last_result=self.turn_result(item, code),
                                status="stopped" if item.pop("stop_requested", False) else "idle")
                    changed = True
        for sid, item in sorted(self.sessions.items(), key=lambda pair: pair[1]["updated_at"]):
            ready = item["turns"] == 0 or item["native_id"]
            if item["queue"] and not item.get("pid") and ready and self.running() < self.max_running:
                self.launch(sid, item["queue"].pop(0))
                changed = True
        if changed:
            self.save()

    def turn_result(self, item, exit_code):
        events = [event for record in self.records(item["logs"][-1:]) for event in self.backend.events(record)]
        final = next((e for e in reversed(events) if e["role"] == "result"), None)
        reply = next((e["text"] for e in reversed(events) if e["role"] == "assistant"), "")
        failed = (final or {}).get("is_error") or exit_code not in (0, None)
        result = (final or {}).get("text") or reply
        if failed and not result:
            try:
                with open(item.get("stderr_log", ""), "rb") as stream:
                    stream.seek(max(0, os.fstat(stream.fileno()).st_size - 4000))
                    result = stream.read(4000).decode("utf-8", errors="replace").strip()
            except OSError:
                pass
            secret = getattr(self.backend, "token", None)
            if secret:
                result = result.replace(secret, "[REDACTED]")
            result = result or f"agent exited with status {exit_code}"
        return {"is_error": bool(failed), "result": result, "exit_code": exit_code,
                "permission_denials": (final or {}).get("permission_denials")}

    # ---- operations ------------------------------------------------------------------------------
    def native(self, who, session_id):
        """Resolve 'native:<workspace>:<id>' to an owner session this caller may read."""
        parts = session_id.split(":")
        if (len(parts) != 3 or parts[0] != "native" or not who.read_owner_sessions
                or parts[1] not in who.workspaces):
            raise Rejected("unknown session_id")
        cwd = self.workspaces[parts[1]]
        if not any(s["native_id"] == parts[2] for s in self.backend.native_sessions(cwd)):
            raise Rejected("unknown session_id")
        return cwd, parts[2]

    def op_list_sessions(self, who, limit=20, cursor=None):
        offset = int(cursor) if cursor and cursor.isdigit() else 0
        own_native = {item["native_id"] for item in self.sessions.values()}
        rows = [{"session_id": sid, "kind": "bridge", "title": item["title"], "workspace": item["workspace"],
                 "status": item["status"], "turns": item["turns"], "queued": len(item["queue"]),
                 "updated_at": item["updated_at"]}
                for sid, item in sorted(self.sessions.items(), key=lambda pair: -pair[1]["updated_at"])
                if self.session(who, sid) is not None]
        if who.read_owner_sessions:
            for name in who.workspaces:
                for session in sorted(self.backend.native_sessions(self.workspaces[name]), key=lambda s: -s["updated_at"]):
                    if session["native_id"] not in own_native:
                        rows.append({"session_id": f"native:{name}:{session['native_id']}", "kind": "native_read_only",
                                     "title": session["title"], "workspace": name, "updated_at": session["updated_at"]})
        return {"workspaces": who.workspaces, "sessions": rows[offset:offset + limit],
                "next_cursor": str(offset + limit) if offset + limit < len(rows) else None}

    def op_start_session(self, who, text, workspace, title=None, model=None, effort=None, fork_from=None):
        if workspace not in who.workspaces:
            raise Rejected(f"unknown workspace; available to you: {who.workspaces}")
        if effort is not None and effort not in EFFORTS:
            raise Rejected("invalid effort")
        cwd, fork_native = self.workspaces[workspace], None
        if fork_from:
            if not self.backend.supports_fork:
                raise Rejected(f"the {self.backend.name} backend cannot fork sessions")
            source = self.session(who, fork_from)
            if source is not None:
                cwd, fork_native = source["cwd"], source["native_id"]
            else:
                cwd, fork_native = self.native(who, fork_from)
        sid = str(uuid.uuid4())
        now = time.time()
        self.sessions[sid] = {
            "caller": who.name, "title": (title or text.strip().splitlines()[0])[:80], "workspace": workspace,
            "cwd": cwd, "native_id": sid if self.backend.preassigns_ids else None, "fork_native": fork_native,
            "model": model, "effort": effort, "turns": 0, "logs": [], "queue": [text], "pid": None,
            "status": "queued", "created_at": now, "updated_at": now}
        self.save()
        self.pump()
        return {"session_id": sid, "status": self.sessions[sid]["status"],
                "note": "accepted, not finished: poll read_session for progress"}

    def op_send_message(self, who, session_id, text):
        item = self.session(who, session_id)
        if item is None:
            self.native(who, session_id)
            raise Rejected("sessions you did not start through the bridge are read-only; start_session with fork_from")
        if item["turns"] and not item["native_id"] and not item.get("pid"):
            raise Rejected("this session failed before the agent reported its id; start a new session")
        item["queue"].append(text)
        item["updated_at"] = time.time()
        self.save()
        self.pump()
        return {"session_id": session_id, "status": item["status"], "queued": len(item["queue"]),
                "note": "messages run in order after the current turn; delivery is not completion"}

    def op_stop_session(self, who, session_id):
        item = self.session(who, session_id)
        if item is None:
            raise Rejected("only your own bridge sessions can be stopped")
        dropped, item["queue"] = len(item["queue"]), []
        if item.get("pid"):
            item["stop_requested"] = True
            try:
                os.killpg(item["pid"], signal.SIGINT)
            except ProcessLookupError:
                pass
        self.save()
        return {"session_id": session_id, "stop_requested": True, "dropped_queued_messages": dropped,
                "note": "stopping does not undo actions already taken"}

    def op_read_session(self, who, session_id, limit=30, cursor=None):
        item = self.session(who, session_id)
        if item is not None:
            records, meta = list(self.records(item["logs"])), {
                "kind": "bridge", "status": item["status"], "title": item["title"], "workspace": item["workspace"],
                "turns": item["turns"], "queued": len(item["queue"]), "last_result": item.get("last_result")}
        else:
            cwd, native_id = self.native(who, session_id)
            records, meta = self.backend.native_records(cwd, native_id), {"kind": "native_read_only"}
        events = [event for record in records for event in self.backend.events(record)]
        start = int(cursor) if cursor and cursor.isdigit() else max(0, len(events) - limit)
        page = events[start:start + limit]
        result = {"session_id": session_id, **meta, "events": page, "next_cursor": str(start + len(page))}
        while len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT_BYTES and result["events"]:
            result["events"] = result["events"][1:]
        return result

    # ---- relay loop ------------------------------------------------------------------------------
    def handle(self, request):
        who = self.policy(request.get("caller"))
        operation, args = request.get("operation"), request.get("args")
        method = getattr(self, f"op_{operation}", None) if isinstance(operation, str) else None
        if method is None or not isinstance(args, dict):
            raise Rejected("unsupported operation")
        for key in ("session_id", "fork_from", "workspace", "model"):
            if key in args and not (isinstance(args[key], str) and NAME.fullmatch(args[key])):
                raise Rejected(f"invalid {key}")
        try:
            return method(who, **args)
        except TypeError:
            raise Rejected("unsupported arguments") from None

    def serve_once(self):
        claim = self.relay.call("POST", "/v1/peer/receive", {"receiver_id": self.receiver_id, "wait_seconds": POLL_SECONDS})
        request = claim.get("request")
        if not request:
            return False
        log(f"claimed {request['id']} op={request.get('operation')}")
        try:
            result, is_error = self.handle(request), False
        except Rejected as error:
            result, is_error = {"outcome": "rejected", "reason": str(error)}, True
        except Exception as error:  # report it; never retry native work automatically
            result, is_error = {"outcome": "executor_error", "reason": type(error).__name__}, True
        self.relay.call("POST", "/v1/peer/reply", {"request_id": request["id"], "delivery_token": request["delivery_token"],
                                                   "result": result, "is_error": is_error})
        log(f"replied {request['id']} is_error={is_error}")
        return True

    def run(self):
        stopping = []
        signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
        backoff = 2
        log(f"executor up backend={self.backend.name} workspaces={sorted(self.workspaces)}")
        while not stopping:
            try:
                self.pump()
                self.serve_once()
                backoff = 2
            except RelayError as error:
                log(f"relay error {error.code}; retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
        log("executor stopping; running turns continue in their own process groups")


def main(argv=None):
    parser = argparse.ArgumentParser(description="agent-peer-bridge executor")
    parser.add_argument("--config", default="~/.agent-peer-bridge/executor.json")
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).expanduser().read_text())
    relay = RelayClient(config["relay_url"], read_secret(config["peer_token_file"]))
    Executor(config, relay).run()


if __name__ == "__main__":
    main()
