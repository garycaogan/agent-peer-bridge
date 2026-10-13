# Relay protocol

JSON over HTTPS. Every call carries `Authorization: Bearer <token>`. Tokens have a kind:
`apb_caller_…` (may enqueue for the peers it was granted) or `apb_peer_…` (may take and answer
work addressed to itself). The relay stores SHA-256 hashes only.

## Caller endpoints

| Method | Path | Body / result |
| --- | --- | --- |
| `GET` | `/v1/peers` | `{peers: [{peer, online, revoked, last_seen}]}` for the peers this caller may address |
| `POST` | `/v1/requests` | `{peer, operation, args, client_id}` → request view |
| `GET` | `/v1/requests/{id}` | request view (own requests only) |
| `POST` | `/v1/requests/{id}/cancel` | `queued → cancelled`; `claimed → uncertain` (it may already be running) |

`client_id` is the idempotency key: the same `(caller, peer, client_id)` returns the original request;
reusing it for different content is a `409`.

## Peer endpoints

| Method | Path | Body / result |
| --- | --- | --- |
| `POST` | `/v1/peer/receive` | `{receiver_id, wait_seconds ≤ 25}` → `{request: {id, operation, args, delivery_token} \| null}` |
| `POST` | `/v1/peer/reply` | `{request_id, delivery_token, result, is_error}` → request view |

A peer has one live receiver at a time (`receiver_id`, 90 s lease renewed by each poll), so two
executors cannot both drain the same queue. The `delivery_token` is minted per claim; only its
holder can post the result, once. Posting the identical result again is a no-op.

## Request states

```
queued ──claim──▶ claimed ──reply──▶ completed | failed
  │                  │
  │ 1 h, no claim    │ 15 min, no reply / cancel
  ▼                  ▼
expired           uncertain        (never retried automatically)
  ▲
cancel ─▶ cancelled
```

`uncertain` is a statement of fact: the peer took the request and the relay does not know the
outcome. Inspect the session (`read_session`) before doing anything again.

## Operations and arguments

| Operation | Required | Optional |
| --- | --- | --- |
| `list_sessions` | — | `limit` (1–100), `cursor` |
| `read_session` | `session_id` | `limit`, `cursor` |
| `start_session` | `text`, `workspace` | `title`, `model`, `effort` (`low…max`), `fork_from` |
| `send_message` | `session_id`, `text` | — |
| `stop_session` | `session_id` | — |

Identifiers match `[A-Za-z0-9_.:-]{1,100}`; text fields are at most 16 000 characters. Anything else
is rejected by the relay before a peer sees it, and validated again by the executor.

Session ids are executor UUIDs for sessions started through the bridge, and
`native:<workspace>:<id>` for the owner's own sessions (read-only).
