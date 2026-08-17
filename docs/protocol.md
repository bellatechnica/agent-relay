# Relay protocol

A client addresses its recipient by exact slug. In the default `none` mode,
the client self-registers and supplies its own acting slug explicitly. In
explicit `token` mode, its bearer token establishes the acting session. Active
slugs are unique, and message routing never selects an arbitrary partial match.

## Message lifecycle

`sent_at` records the durable insert. `first_delivery_attempt_at` records when a
read or SSE stream first attempted to return the message; it does not claim that
the client processed it. `acknowledged_at` is set only by the recipient's
explicit acknowledgement.

Inbox reads and new SSE connections return all unacknowledged messages in
database sequence order. A reconnect replays an unacknowledged message even if a
previous connection received its SSE event, because transport receipt is not
processing proof.

## Identity modes

Start the default trusted slug mode without credentials:

```bash
agent-relay
```

`POST /v1/sessions` registers or recovers a slug without returning a token.
Other HTTP calls identify the acting session with `Agent-Relay-Slug: SLUG`.
MCP clients call `register_session(slug, agent_kind)` and pass the same slug as
`acting_slug` on later tools. Anyone who can reach the relay can supply any
active slug in this mode.

A self-registration response contains no token. Before changing an existing
database to `token` mode, revoke or replace identities created through
self-registration; they have no recoverable plaintext credential for
authenticated use. Messages remain under their original sender and recipient
IDs.

Opt into authenticated mode with an administrator token:

```bash
export AGENT_RELAY_ADMIN_TOKEN="$(openssl rand -hex 32)"
agent-relay --authentication-mode token
```

Every authenticated request uses:

```http
Authorization: Bearer SESSION_TOKEN
```

In token mode, only `GET /health` is unauthenticated. Administrative routes
require the value of `AGENT_RELAY_ADMIN_TOKEN`; ordinary routes and MCP tools
require a session token. In both modes, the server accepts the host names
`127.0.0.1`, `localhost`, and `host.docker.internal` to prevent DNS-rebinding
requests through unexpected Host headers.

### Authenticated flow recap

1. The operator starts the relay with `AGENT_RELAY_ADMIN_TOKEN`.
2. The administrator route creates one identity and bearer token per agent.
3. Each agent starts with only its own token. The relay derives the sender or
   acting session from that token; callers never submit their own slug.
4. A sender calls `list_sessions` to obtain the recipient's exact slug, then
   calls `send_message` with that slug.
5. The recipient calls `read_inbox` with its own token. A reply made with
   `reply_to_message` is routed to the original sender automatically.
6. Only the recipient token may acknowledge a message. Unacknowledged messages
   remain durable and are replayed after reconnects or server restarts.

No participant receives another participant's token. The administrator token
is used only to issue and revoke session credentials.

## HTTP API

All request and response bodies are JSON except the SSE stream.

| Method and path | Identity requirement | Result |
| --- | --- | --- |
| `GET /health` | None | Process health |
| `POST /v1/admin/sessions` | Admin in `token`; none in `none` | Issue a session and return its token once |
| `POST /v1/admin/sessions/{session_id}/revoke` | Admin in `token`; none in `none` | Revoke a session |
| `POST /v1/sessions` | `none` mode only | Self-register and return a public session record |
| `GET /v1/whoami` | Session token or acting-slug header | Acting session identity |
| `GET /v1/sessions` | Session token in `token`; none in `none` | Every active session and exact ID |
| `POST /v1/messages` | Session token or acting-slug header | Send a message |
| `GET /v1/messages` | Session token or acting-slug header | Every unacknowledged inbox message |
| `GET /v1/events` | Session token or acting-slug header | Live SSE notifications plus durable replay |
| `POST /v1/messages/{message_id}/ack` | Recipient token or acting-slug header | Acknowledge a message |
| `POST /v1/messages/{message_id}/reply` | Participant token or acting-slug header | Reply to the other participant |

Register a session in the default mode:

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -d '{"slug":"sandbox-codex","agent_kind":"codex"}' \
  http://127.0.0.1:8787/v1/sessions
```

Send a message. `content` is stored verbatim; it must be a non-empty JSON string.
`in_reply_to` is optional and must identify a message between the same two
participants.

```bash
curl --fail-with-body \
  -H 'Agent-Relay-Slug: host-coordinator' \
  -H 'Content-Type: application/json' \
  -d "{\"recipient_slug\":\"$RECIPIENT_SLUG\",\"content\":\"inspect the failing test\"}" \
  http://127.0.0.1:8787/v1/messages
```

Open the push stream from a non-browser receiver:

```bash
curl --no-buffer --fail-with-body \
  -H 'Agent-Relay-Slug: host-coordinator' \
  http://127.0.0.1:8787/v1/events
```

Each message notification has this SSE shape:

```text
event: message
id: 17
data: {"sequence":17,"message_id":"...","content":"..."}
```

The stream sends heartbeat comments at the configured interval. Heartbeats keep
intermediaries from treating an idle stream as dead; they do not poll SQLite.

## MCP tools

The Streamable HTTP endpoint is `/mcp`. It exposes:

- `register_session(slug, agent_kind)` in `none` mode
- `whoami(acting_slug?)`
- `list_sessions`
- `send_message(recipient_slug, content, in_reply_to?, acting_slug?)`
- `read_inbox(acting_slug?)`
- `acknowledge_message(message_id, acting_slug?)`
- `reply_to_message(message_id, content, acting_slug?)`

The `acting_slug` argument is omitted in `token` mode because the bearer token
supplies it. It is required in `none` mode. Supplying a slug different from the
bearer token's session is rejected in `token` mode.

The MCP transport refuses request bodies above 4 MiB with HTTP 413 before a tool
can run. No partial message is inserted. If one logical payload is larger, split
it into messages whose content identifies the part and total, for example
`part 2 of 5`, so a receiver cannot mistake one part for the whole. The HTTP API
is also available when splitting would damage the payload's semantics.

## What push means for an agent

The relay does not poll. `/v1/events` holds one HTTP connection open and wakes it
when a message is committed. MCP clients can send and read in both directions,
but the MCP protocol does not make a stopped or idle coding agent begin a new
turn when an unrelated SSE endpoint fires.

For unattended reactions, run a client-specific receiver beside the agent. It
keeps `/v1/events` open, translates each message into that client's supported
turn-start or steering API, and acknowledges only after the client accepts the
turn. A receiver crash before acknowledgement is safe: its next connection
replays the message. Do not acknowledge merely because the SSE bytes arrived.
