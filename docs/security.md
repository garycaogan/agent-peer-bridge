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
