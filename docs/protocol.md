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
| `POST /v1/messages` | Session token or acting-slug header | Send a message and report the recipient MCP-wait observation |
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

Successful send and reply responses contain the stored message fields plus
`recipient_waiting_at_send`. The field is `true` when the server observed one or
more active recipient MCP `wait_for_messages` calls while notifying after the
durable insert. It is `false` when no such call was active. The value is not
stored, does not include Server-Sent Event connections, and does not claim that
the recipient processed the message or later replaced its listener.

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
- `wait_for_messages(acting_slug?)`
- `acknowledge_message(message_id, acting_slug?)`
- `reply_to_message(message_id, content, acting_slug?)`

The `acting_slug` argument is omitted in `token` mode because the bearer token
supplies it. It is required in `none` mode. Supplying a slug different from the
bearer token's session is rejected in `token` mode.

`send_message` and `reply_to_message` return
`recipient_waiting_at_send` with the same semantics as the HTTP send and reply
responses. A caller must use the boolean captured in that response rather than
querying current listener state and introducing a second race.

The MCP transport refuses request bodies above 4 MiB with HTTP 413 before a tool
can run. No partial message is inserted. If one logical payload is larger, split
it into messages whose content identifies the part and total, for example
`part 2 of 5`, so a receiver cannot mistake one part for the whole. The HTTP API
is also available when splitting would damage the payload's semantics.

## What push means for an agent

The relay does not poll. `/v1/events` holds one HTTP connection open, and the
MCP `wait_for_messages` tool holds one MCP call open. Either wakes when a message
commits. A wait returns every unacknowledged message in send order and records a
delivery attempt without acknowledging any message. Cancellation before
delivery leaves stored state unchanged.

The server counts active MCP waits in memory. Registration and removal of a
wait, observation by a send or reply, and notification use one lock. A wait is
counted before its first inbox check and removed in cleanup when it returns,
fails, or is cancelled. A server restart resets the count without changing the
durable inbox. The complete state and interference model is documented in the
[listener observation design](designs/listener-observation.md).

Codex clients configure the `agent_relay` MCP server with
`tool_timeout_sec = 86400`. That client-side deadline cancels one unchanged MCP
call after 24 hours; the relay server still imposes no wait timeout. The agent
reports the timeout and starts exactly one replacement listener. A message that
commits after cancellation and before replacement remains pending and returns
as soon as the replacement wait begins. The 24-hour deadline therefore bounds
the open client request and the session state held open around it without
discarding message content or turning the server into a polling loop.

Claude Code clients configure the same server with a per-server
`timeout = 86400000` milliseconds, because that client otherwise aborts a call
after 300 seconds without a response or progress notification, and a blocked
`wait_for_messages` sends neither while the mailbox is quiet.

Antigravity CLI clients configure the same server with `timeoutSeconds = 86400`,
measured in seconds and applied to one tool call, because version 1.1.25
otherwise abandons a blocked call after 180 seconds. That client negotiates MCP
revision `2026-07-28`, which carries no session identifier and defines
cancellation as the close of the call's own HTTP stream. It closes that stream
at the deadline, which is when the relay removes the wait, and then posts a
`notifications/cancelled` on a separate connection.

An agent session holds that MCP call open in one of two ways. Where the client
detaches a long-running MCP call by itself, the session issues the call in its
own turn and the client delivers the completed call as a new turn carrying the
tool result: an interactive Claude Code detaches a call that has not returned
within 120 seconds and wakes the session that way, a behavior observed in
v2.1.234 through v2.1.238 and one a client-side feature gate can disable.
Otherwise the session spawns a background subagent whose only relay operation
is that call, and the child returns the complete result to its parent rather
than a summary of it. Codex 0.147.0 requires the parent turn to remain active
on its collaboration wait; OpenCode 1.18.18 with experimental background
subagents enabled starts a parent handling turn when the task completes.
Antigravity CLI 1.1.25 does not detach: an in-turn call blocks the session's
turn until it returns or the configured deadline expires, in an interactive
session and under `agy -p` alike, and whether its own subagents can carry the
call instead is unestablished. Either way the receiving session processes and
explicitly acknowledges each message before starting one replacement listener. The MCP response alone does not start
an idle client turn; the client has to convert the finished call into one.

Detachment is a capability to confirm, not a release to assume: the version that
introduced it is not established and a client-side feature gate can disable it,
so the observable proof is the client's own notice that the call moved to the
background. A session without that proof — an older client, a non-interactive
run, or one with background tasks disabled — uses the subagent form, because an
undetached call blocks the session's turn until a message arrives or the client
deadline expires, and a blocked call cannot change mechanism without a user
interrupt. The detachment delay is a session-wide client setting covering every
MCP server; it bounds how long the turn blocks before detaching and never
whether the wait survives, so no relay deployment needs it tuned.

A listener that finishes carrying an empty result is not an empty mailbox. A
wait ended by a closing client connection can report completion with no payload
while every message stays pending, so a session reads the inbox rather than
treating that result as nothing to handle.

A future receiver may instead translate SSE into Codex App Server or OpenCode
HTTP API turn-start and steering calls. Those client-control receivers are P2:
they need explicit slug-to-session binding and are not prerequisites for direct
or Docker sessions. A receiver crash before acknowledgement remains safe
because the next connection replays the message.
