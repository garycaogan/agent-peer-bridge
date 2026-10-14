# agent-peer-bridge

Let coding agents on different machines hand work to each other. An agent on one computer can
**start, continue, read, list and stop real sessions** of the agent on another computer —
Claude Code ↔ Codex, or any mix — through a small relay. Neither machine opens a port, and the
caller never gets more power than the owner of the other machine allowed. The same mechanism lets a
whole team, and each teammate's own agent, use the best-equipped agent you have, each person with
their own workspaces, permissions and rules.

[中文说明](README.zh-CN.md)

```
 machine A (e.g. Codex)                    relay (HTTPS)                 machine B (e.g. Claude Code)
┌──────────────────────┐   enqueue   ┌──────────────────────┐  long-poll ┌──────────────────────────┐
│ agent ─ MCP adapter  │ ──────────▶ │ queue · tokens ·     │ ◀───────── │ executor ─ claude -p /   │
│  (caller token)      │ ◀────────── │ leases · idempotency │ ─────────▶ │ codex exec sessions      │
└──────────────────────┘   result    └──────────────────────┘  one reply └──────────────────────────┘
        Run the same pieces the other way round for a two-way bridge.
```

## Why

We had Codex on the main computer and Claude on a second Mac, and a human copying messages
between them. Two things hurt:

1. **The human was the relay.** Every handoff waited for a person to paste it across.
2. **One endless session is a bad unit of work.** A single "receiver" conversation that takes
   every task grows until it is compacted again and again, and starts to drift. Each task should
   get its own fresh session, and only reuse context when you choose to (fork).

So the bridge does not pipe chat messages into one long conversation. It drives the other
machine's agent **the way its owner would**: headless sessions, one per task, continued or forked
on purpose, each with a readable transcript.

## Design rules

| Rule | What it means |
| --- | --- |
| Outbound only | Executors long-poll the relay. No inbound port, no SSH, no tunnel into the agent machine. |
| Scoped credentials | Each caller token may address only the peers it was granted. Peer tokens can only take work for themselves. Tokens are stored hashed and shown once. |
| Owner-fixed permissions | Permission mode / sandbox come from the executor's local config. A request cannot change them; the relay rejects unknown arguments. |
| Named workspaces | Callers pick a workspace *name*; only the executor maps names to directories. No paths travel over the wire. |
| Per-person access | Each caller gets its own workspaces, permission setting and standing rules, and sees only its own sessions. |
| Your sessions stay yours | Sessions the owner ran by hand are listed and readable, never written. To build on one, fork it into a new session. |
| Honest outcomes | `client_id` makes every write idempotent. A claimed request that is not answered in time becomes `uncertain`, never silently retried. "Accepted" is never reported as "done". |

## Quick start

Install on every machine involved (Python 3.10+, no dependencies):

```bash
git clone <this repo> && cd agent-peer-bridge && python3 -m pip install -e .
```

**1. Relay** (any host both machines can reach over HTTPS — one of the two machines, or a VPS):

```bash
apb-relay --db relay.db add-peer mac-mini                       # prints the peer token once
apb-relay --db relay.db add-caller codex-main --peers mac-mini  # prints the caller token once
apb-relay --db relay.db serve --host 127.0.0.1 --port 8787
```

Put TLS in front before anything leaves localhost (Caddy, a Cloudflare Tunnel, Tailscale Serve…).
The relay itself speaks plain HTTP on loopback.

**2. Executor** on the machine that should take work. Save the peer token to a `chmod 600` file and
write a config (see [`examples/`](examples)):

```json
{
  "relay_url": "https://relay.example.com",
  "peer_token_file": "~/.agent-peer-bridge/peer.token",
  "backend": "claude-code",
  "backend_options": {"permission_mode": "auto"},
  "workspaces": {"main": "~/code/main-repo"},
  "max_running": 3
}
```

```bash
apb-executor --config ~/.agent-peer-bridge/executor.json
```

Keep it running with launchd or systemd ([`examples/`](examples)). For unattended Claude Code, create
a long-lived token with `claude setup-token` and point `backend_options.oauth_token_file` at it.

**3. MCP adapter** on the machine that sends work, registered with its agent:

```bash
# Claude Code
claude mcp add peer-bridge --env APB_RELAY_URL=https://relay.example.com \
  --env APB_CALLER_TOKEN_FILE=$HOME/.agent-peer-bridge/caller.token -- apb-mcp
```

```toml
# Codex (~/.codex/config.toml)
[mcp_servers.peer-bridge]
command = "apb-mcp"
env = { APB_RELAY_URL = "https://relay.example.com", APB_CALLER_TOKEN_FILE = "/Users/me/.agent-peer-bridge/caller.token" }
```

The agent now has `list_peers`, `start_session`, `send_message`, `read_session`, `list_sessions`,
`stop_session` and `request_status`.

**Two-way:** run an executor on both machines and give each side a caller token for the other.

## Share your agent with a team

The machine with the best-equipped agent — your projects, notes, skills and habits already loaded —
can serve everyone. Give each teammate (or their own agent) a caller token, then say in the executor
config what each of them gets:

```json
"callers": {
  "anqi":   {"workspaces": ["tools"],
             "rules": "You are working for Anqi (operations). Fix and ship reversible changes yourself; stop and say so before anything that touches pricing.",
             "permission": {"permission_mode": "auto"}},
  "bonnie": {"workspaces": ["tools"], "rules_file": "~/.agent-peer-bridge/rules/bonnie.md"},
  "intern": {"workspaces": ["docs"], "permission": {"permission_mode": "plan"}},
  "me":     {"workspaces": "*", "read_owner_sessions": true, "see_all_sessions": true}
}
```

| Setting | Effect |
| --- | --- |
| `workspaces` | Named workspaces this caller may start sessions in (`"*"` for all). |
| `rules` / `rules_file` | Your standing instructions for this caller, applied on every turn: appended to the system prompt (Claude Code) or placed before the prompt (Codex). A rules file is re-read each turn, so edits apply without a restart. |
| `permission` | This caller's own permission setting: `permission_mode` for Claude Code (e.g. `plan` for someone who only asks questions), `sandbox` for Codex (e.g. `read-only`). |
| `read_owner_sessions` | May list and read — never write — the sessions you ran yourself in its workspaces. This is how your accumulated context is shared. |
| `see_all_sessions` | May see other callers' bridge sessions. By default everyone sees only their own. |

Callers that are not listed are refused. Without a `callers` section the executor runs in open mode
(every admitted caller gets everything), which suits one person with two machines. A full example is
in [`examples/executor.team.json`](examples/executor.team.json).

## Operations

| Operation | Effect on the peer |
| --- | --- |
| `start_session` | New headless session in a named workspace (optional `fork_from` to branch an existing one). Returns once accepted. |
| `send_message` | Next turn of a bridge-started session; queued in order while one is running. |
| `read_session` | Recent events (assistant text, tool calls, errors), status and last result; cursor paging. |
| `list_sessions` | Bridge-started sessions plus the owner's own sessions in the workspaces (read-only). |
| `stop_session` | Interrupts the running turn and drops queued messages. Finished actions are not undone. |

Protocol details: [docs/protocol.md](docs/protocol.md). Threat model: [docs/security.md](docs/security.md).

## Backends

- **Claude Code** — `claude -p --output-format stream-json` with `--session-id` / `--resume` /
  `--fork-session`. Tested end to end. On macOS it falls back to the Claude Code build shipped
  inside Claude Desktop when no CLI is installed. Sessions it creates are ordinary Claude Code
  sessions; Claude Desktop can adopt them via *Help → Troubleshooting → Import Claude Code CLI
  Sessions…* — after that, treat them as yours (read/fork from the bridge, don't continue both).
- **Codex** — `codex exec --json`, resumed by the thread id Codex reports. No fork.
  Tested end to end with codex-cli 0.162.0-alpha.17.2 (new session, resume, stdin and shell execution).

## Limits

- One SQLite relay; fine for a handful of machines, not a fleet.
- No streaming: callers poll `read_session`.
- Anything that would need a human approval on the peer is denied and reported in the result,
  not escalated. Approvals stay with the machine owner.
- A request's text is a prompt to another agent. Treat a peer like a colleague with the
  permissions you configured — not like a sandbox for untrusted input.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
