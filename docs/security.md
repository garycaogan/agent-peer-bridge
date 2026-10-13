# Security model

The bridge lets one agent cause work on another person's (or your other) machine. These are the
assumptions and what each part does about them.

## What a stolen credential can do

| Credential | Worst case | Limits |
| --- | --- | --- |
| Caller token | Start/continue sessions on the peers it was granted, read their transcripts in configured workspaces | Cannot pick directories, permission modes, or peers outside its grant; cannot read other callers' requests. Revoke with `apb-relay revoke <name>`. |
| Peer token | Take and answer work addressed to that peer (it could return false results) | Cannot enqueue anything, cannot see other peers' queues. |
| Relay database | Request history (prompts and results) | Holds token hashes only. Keep it on a host you trust; back it up like any log with prompts in it. |

Token files must be regular files readable only by their owner (`chmod 600`); the clients refuse
anything else. Tokens never appear in logs or error messages; HTTP clients refuse redirects so a
bearer token is never forwarded to another host.

## The peer machine

- **Permissions are local.** `permission_mode` (Claude Code) and `sandbox` (Codex) are set in the
  executor config. Prompts that would need a human are denied (`--permission-prompts none`) and show
  up in the session's result. Use the mode you would accept for a capable colleague working
  unsupervised in those directories — `auto`/`workspace-write`, not bypass.
- **Workspaces are names.** Requests carry a workspace name; only the executor maps it to a
  directory. Session ids are validated, so `native:` lookups cannot escape a workspace.
- **The owner's sessions are read-only.** The executor never resumes or writes a session it did not
  start. Forking reads the old transcript and writes a new one. If the owner later adopts a bridge
  session into their own UI (e.g. Claude Desktop's import), stop continuing it from the bridge —
  two writers on one transcript corrupt it.
- **The spawned agent gets a clean environment.** Variables of a parent agent session
  (`CLAUDE*`, `ANTHROPIC*`) are dropped so a nested session cannot borrow its host's identity.
- **No inbound exposure.** The executor only makes outbound HTTPS calls.

## Prompt injection

A request's `text` is a prompt written by another agent, which may itself have read untrusted
content. The receiving agent treats it like any user prompt, within the permissions above. That is
why permissions are fixed locally and approvals are never relayed: the worst a malicious prompt can
do is what you allowed that machine's agent to do on its own.

## Outcomes

Network failures are ambiguous. The bridge never converts ambiguity into a retry:

- writes are idempotent by `client_id` — retrying with the same id is safe;
- a claimed request with no reply becomes `uncertain`, never re-queued;
- `accepted` (a session started, a message queued) is never reported as `completed work`.

## Not covered

- Multi-tenant hosting: one relay is meant for one owner and their machines.
- End-to-end encryption between caller and peer: the relay sees prompts and results. Run it
  yourself, behind TLS.
- Per-caller session isolation: callers granted the same peer share its bridge sessions, including
  read, continue and stop operations. Session ownership is not tracked per caller.
- Read isolation by workspace: workspace names select a working directory, not an OS security
  boundary. Codex's `workspace-write` sandbox can allow reads outside that directory; existing MCP
  integrations and local credentials also need their own access controls.
- Business approvals: a remote prompt is not the machine owner's approval for spending money,
  publishing private material or changing access. Enforce those policies separately.

## Executor reliability limits

One executor per state directory is enforced with a file lock (`executor.lock`); a second one exits.
Its stable `receiver.id` lets a restarted executor resume its own relay lease.

Each turn is journaled as `launching`, with its message already removed from the queue, before the
agent process starts, and the PID is saved right after. If the executor dies in between, the restart
marks that turn `uncertain` and drops the messages queued behind it instead of running anything
again. So a message runs at most once, and an ambiguous turn is reported, not repeated.

What this does not give you is exactly-once side effects inside a turn: an agent that was interrupted
mid-turn may have done part of its work. Read the session before resending, and keep irreversible
actions behind the owner's own approvals, which the bridge never grants.
