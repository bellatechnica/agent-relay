# Agent Relay requirements

This document defines the user-facing behavior Agent Relay must provide. The
reader is implementing or reviewing the relay, its Model Context Protocol (MCP)
tools, or the handoff workflow. Transport and storage details that do not change
these requirements belong in the [protocol reference](protocol.md).

## Immediate outcome

A user must be able to open Codex, Claude Code, or OpenCode normally, ask each
session to register a human-readable slug, and then ask either session to send a
message to the other by slug. Starting an ordinary session in this workflow must
not require the user to create, copy, or assign an administrator or session
token.

The same workflow must operate between direct host sessions and sessions inside
a Docker Sandbox. Docker sessions must retain the web-search and automatic-mode
setup described in the [Docker Sandbox guide](docker-sandbox.md).

An ordinary Codex or OpenCode session must not depend on a separately launched
Codex App Server or OpenCode HTTP server to receive messages. A background
listener subagent inside the session provides wake-up behavior through the
relay's MCP tools.

## Identity and registration

- The server defaults to unauthenticated slug mode, so the immediate workflow
  requires no authentication flag, administrator token, or session token.
  Token-authenticated operation remains available through an explicit server
  option.
- A session registers through MCP using a non-empty slug and its agent kind,
  such as `codex`, `claude`, or `opencode`.
- The slug is the only identity the user or agent needs for routine messaging.
  Internal session IDs may remain in storage and diagnostic responses, but an
  agent must not need one to address another agent.
- Active slugs are unique. Exact slug lookup must return one active session or a
  visible not-found error; routing must never select an arbitrary partial or
  duplicate match.
- Registering an already-active slug in unauthenticated mode is idempotent and
  returns the existing identity. This permits resumed sessions and retried MCP
  calls to recover without creating duplicate identities.
- No claim of ownership accompanies a slug in unauthenticated mode. Any process
  that can reach the relay can register an unused slug or act as an existing
  slug. This is an accepted limitation of the immediate trusted-development
  workflow.

## Messaging behavior

- An agent sends a complete message by exact recipient slug.
- Every successful send or reply reports whether the relay observed at least one
  active MCP `wait_for_messages` call for the recipient when it notified that
  recipient. The observation is named `recipient_waiting_at_send`; it describes
  that instant only and does not claim that the recipient processed the message
  or later replaced its listener.
- An agent reads every pending message for its own slug in send order.
- A reply to a received message is routed to the other participant without the
  replying agent supplying that participant's slug or internal session ID.
- Only an explicit acknowledgement removes a message from subsequent inbox
  reads. Reading alone records a delivery attempt, not successful processing.
- Unacknowledged messages and replies survive relay restarts.
- Message content is stored verbatim. The relay must not silently truncate
  content, select only one item from a multi-message inbox, or acknowledge work
  merely because bytes reached a client.
- Unknown, inactive, or revoked slugs produce a visible error and do not create
  a partial message.

The MCP surface must make the common requests direct and self-explanatory:
register this session by slug, list active slugs, send to a slug, read this
slug's inbox, reply to a message, and acknowledge a processed message.

## Ordinary-session usability

- The remote MCP server is configured once under the name `agent_relay` for
  each supported client. Opening a new client session must not require a
  relay-specific wrapper or per-session environment variable in
  unauthenticated mode.
- A user instruction such as “register with agent-relay as `api-review`” must
  give the agent enough information, through MCP tool names, descriptions, and
  server instructions, to perform the registration without consulting source
  code.
- A user instruction such as “send `api-review` the test failure through
  agent-relay” must be sufficient for the agent to route the message by slug.
- Provide an `agent-relay-message` skill for environments that support these
  skills. It must cover registration, sending, inbox reads, replies, and
  acknowledgements with the same safety discipline as the MCP tools. After
  registration, it must start one background listener subagent when the client
  supports background subagents.
- A listener subagent waits for messages through MCP and finishes when messages
  arrive. The parent reads and handles every pending message, acknowledges each
  message only after processing it, and then starts one replacement listener.
- Codex must configure the `agent_relay` MCP server with
  `tool_timeout_sec = 86400`. This client-side deadline ends an unchanged wait
  after 24 hours; it does not remove or acknowledge a relay message. The parent
  reports the timeout and starts exactly one replacement listener, which
  receives any message that became pending between the cancelled wait and its
  replacement. Changing this deadline is a product decision because it trades
  reconnect frequency against the lifetime of an open request, listener
  subagent, and active parent turn.
- Codex must keep the parent turn active on its collaboration wait while the
  listener is blocked. Codex 0.147.0 records a child that finishes after the
  parent has returned to the prompt, but does not start a new parent turn to
  handle that completion. The active parent wait must accept a user prompt
  steered into the running turn and then continue waiting for the same listener.
- OpenCode and Claude Code may return the parent to the prompt after launching
  the listener when their background-task completion starts a parent turn. The
  skill and handoff prompt must use the behavior verified for the running
  client, rather than claiming idle-parent wake-up for every client.
- A Codex listener should use a cheaper available model because waiting does not
  require the parent session's model capability. An OpenCode listener uses the
  session's configured model unless the user requests a different one.
- OpenCode must expose the background form of its `task` tool. While that form
  remains experimental, installation instructions must persistently set
  `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the environment inherited
  by ordinary OpenCode launches. A manual `Ctrl+B` detachment does not satisfy
  unattended listener startup.
- Starting a listener must not require a relay-specific wrapper, Codex App
  Server, OpenCode HTTP server, or access to a Docker or tmux control socket.
- Client-specific documentation must explain how to confirm that `agent_relay`
  appears in the client's MCP server list and how to distinguish configuration
  from a successful live tool call.

## Handoff integration

The `handoff` skill must use Agent Relay as its default durable message channel.
Tmux has two narrower roles: it may host the new local agent window, and it may
wake a local recipient when a successful Relay send reports
`recipient_waiting_at_send = false`. The Relay message remains the only copy of
the actionable payload in this recovery path.

When the relay channel is selected, the spawning session must:

1. Choose distinct, human-readable slugs for itself and the new session.
2. Register or recover its own slug before launching the new session.
3. Include the new session's slug and the spawning session's slug in the initial
   prompt.
4. Instruct the new session to register its slug, read pending messages at the
   start, start its background listener, acknowledge only after processing, use
   replies when responding to a received message, and replace the listener
   after handling messages.
5. Use the two slugs for clarifications, blocked-state notices, coordination
   signals, and completion reports that would otherwise travel through
   `tmux-message`.
6. Inspect `recipient_waiting_at_send` after every successful send or reply. If
   it is `true`, do not inject a tmux message. If it is `false` and the recipient
   is reachable through the same tmux server, invoke the `tmux-message` skill to
   send only a wake notice containing the Relay message ID and an instruction to
   process the Relay inbox and restore exactly one listener. Do not copy the
   actionable message content into the wake notice.

The existing `tmux-message` path remains available as an explicit user-selected
mode. When Relay registration or its live preflight fails before launch, a
handoff may use tmux as the exclusive message channel only when both sessions
are reachable through the same tmux server. It must report that fallback to the
user and give both sessions their pane addresses; if tmux is unavailable too,
it reports the blocker. A handoff must not silently change channels.

A successful Relay send with `recipient_waiting_at_send = false` remains
durably queued when no tmux path exists, including across a Docker Sandbox
boundary. The sender reports that delivery is queued but active wake-up was not
verified. An ambiguous Relay send failure must never cause the payload to be
resent through tmux because the first call may already have committed it.

## Delivery and wake-up boundary

The relay server must not poll its SQLite database for new messages. Server-Sent
Events (SSE) and MCP must offer blocking waits that use in-process notification
when a message commits. If messages are already pending, a wait returns every
pending message immediately in send order. Otherwise it remains blocked until a
message arrives or the caller cancels it.

Returning messages from a wait records a delivery attempt but does not
acknowledge them. Cancelling a wait before delivery must not change stored
message state. The server must not impose a timeout or message-count limit on a
wait. Concurrent listeners for one slug may receive the same unacknowledged
messages; a listener is notification, not an exclusive queue claim.

The relay tracks active MCP waits in memory. A wait becomes observable before
it checks the durable inbox and ceases to be observable whenever it returns,
fails, or is cancelled. A server restart therefore begins with no observed
waits while retaining every durable message. `send_message` and
`reply_to_message` return `recipient_waiting_at_send = true` exactly when one or
more waits were observable for the recipient at notification time; otherwise
they return `false`. Server-Sent Event connections are not MCP listener
subagents and do not affect this field.

On `SIGTERM` or `SIGINT`, the command-line server must mark the notification hub
as shutting down before Uvicorn waits for open connections. That transition
wakes every blocking MCP wait, `/v1/events` SSE stream, and MCP transport-owned
notification stream. That transport stream is a standalone GET in legacy MCP
revisions and a `subscriptions/listen` POST in the modern revision. MCP waits
finish with a visible temporary-unavailability error, every stream ends, and
none of those paths records a delivery attempt or acknowledgement merely
because shutdown began. A send or reply that reaches notification after the
transition reports
`recipient_waiting_at_send = false`. The managed service's stop timeout remains
a last-resort process guard, not the ordinary way long-lived requests end.

An MCP response does not itself start a new model turn. The supported immediate
wake mechanism is completion of a background listener subagent that made the
blocking MCP call. A client that does not start a parent turn on background
completion must keep the parent turn active on its own subagent wait. The parent
session remains responsible for reading the full inbox, processing it,
acknowledging processed messages, and maintaining exactly one listener after
handling completes.

## Deployment and security boundary

- The relay must work on host loopback and through Docker's
  `host.docker.internal` route without exposing a Docker or tmux control socket
  to the sandbox.
- Docker Sandbox agents must use the configured `agent_relay` MCP server for
  Relay operations. A missing native MCP tool is a visible setup failure; the
  agent must not replace it with a generated Python script, direct REST calls,
  or shell HTTP requests that bypass the client's MCP lifecycle.
- A sandbox may receive a curated skill subset through a read-only additional
  workspace. The documented Claude Code workflow must load a root containing
  `.claude/skills/` with `--add-dir`, must expose only intentionally selected
  skill copies, and must not require Docker's global read-write shared skill
  store. Refreshing a selected skill from its authoritative source is an
  explicit deployment action rather than implicit live access to every host
  skill.
- The machine-local Claude Code sandbox launcher must select model settings
  from `~/.agents/models/<profile>/settings.json` and pass one shared, key-free
  `~/.agents/sbx/agent-relay.mcp.json` file on every launch. A newly created
  sandbox must therefore receive native `agent_relay` tools in its first agent
  process without a prior `claude mcp add` bootstrap or dependence on mutable
  container state. The shared `agent_relay` entry must set its MCP tool timeout
  to 86,400,000 milliseconds (24 hours), matching the direct-client listener
  policy and preventing Claude's five-minute MCP tool idle timeout from
  cancelling an otherwise healthy long poll.
- Unauthenticated slug mode must be the default and visibly documented as
  trusted-network operation. Token-authenticated mode must be an explicit
  opt-in and must retain its existing credential checks when selected.
- The server must continue to support durable storage, health checking, and
  managed autostart documented in the [README](../README.md).
- Web access and automatic agent execution are properties of the sandbox/client
  launch configuration; the relay must coexist with them without requiring a
  broad network allowlist.

## Deferred public-key identity (P2)

Ed25519 self-registration is deferred. Its purpose is to prevent impersonation
and remove manual administrator provisioning without changing the slug-based
user experience.

A later public-key mode should let a launch helper generate or load a private
key, register the public key, prove possession using a one-use server challenge,
and receive credentials usable by existing MCP clients. The model must not be
asked to handle private-key material directly. Replay handling, key rotation,
credential lifetime, and admission control require separate design decisions
before implementation.

## Deferred client-control receivers (P2)

Direct prompt injection through Codex App Server or the OpenCode HTTP API is an
optional later optimization, not part of ordinary-session delivery. Such a
receiver would need an explicit, exact binding between a relay slug and a client
thread or session. It must not claim that MCP registration can discover or
control an arbitrary client session.

A Codex App Server receiver may start a turn while its bound thread is idle and
steer the active turn while it is busy. An OpenCode receiver may submit an
asynchronous prompt to its bound session. Neither receiver may become a
prerequisite for direct-host or Docker messaging until its installation and
session-binding workflow meets the ordinary-session requirements above.

## Acceptance checks

The immediate implementation is acceptable only when all of these observations
hold:

- Two independent MCP clients can register distinct slugs without administrator
  or session tokens.
- Starting the server without an authentication option selects unauthenticated
  slug mode; selecting token mode without an administrator token fails visibly.
- Repeating registration for an active slug returns the same relay identity and
  does not add another active session.
- The first client can send one message using only the second client's slug; the
  second client reads exactly that message and replies without supplying the
  first client's identity.
- After both clients acknowledge their processed messages, both inboxes are
  empty.
- Restarting the relay before acknowledgement preserves the pending message.
- Sending to an unknown slug fails visibly and leaves the message count
  unchanged.
- Token-authenticated clients retain their existing sender authentication and
  cannot override it with a different acting identity.
- A relay-based handoff prompt contains both assigned slugs, registration
  instructions, listener startup and replacement instructions,
  acknowledgement discipline, and the reply instruction.
- A send with no active recipient MCP wait returns
  `recipient_waiting_at_send = false`; the same send while one or more recipient
  waits are active returns `true`. After every wait has returned or been
  cancelled, a later send returns `false`. Restarting the server also resets the
  observation to `false` without removing pending messages.
- A relay-based handoff uses no tmux wake notice when a send reports
  `recipient_waiting_at_send = true`. When it reports `false` and both sessions
  share a reachable tmux server, the sender delivers one verified tmux wake
  notice containing the Relay message ID but none of its actionable content.
- In an ordinary Codex session configured with
  `tool_timeout_sec = 86400`, a cheaper-model background listener remains
  blocked while its inbox is empty for up to 24 hours. The parent remains in
  its collaboration wait, accepts a second user prompt during that wait, and
  handles the exact relay message after the listener completes without
  terminal-based message injection or a client-control API. If the 24-hour
  client deadline expires first, the parent reports the timeout and starts one
  replacement listener without acknowledging any message.
- In an ordinary OpenCode session with background tasks enabled, a listener
  using the session's current model remains blocked while its inbox is empty,
  completes after a relay message arrives, and starts the parent handling turn
  without terminal-based message injection or a client-control API.
- A blocking MCP wait returns every message already pending, or blocks without
  database polling until a send commits. Returned messages have a recorded
  delivery attempt and remain unacknowledged.
- Cancelling a blocking MCP wait before delivery leaves the inbox unchanged.
- With an MCP wait, a `/v1/events` SSE listener, and an MCP transport-owned
  notification stream open, one `SIGTERM` makes all three requests finish and
  lets the server stop without reaching the managed service's stop timeout. A
  subsequent process reads every message that was unacknowledged before
  shutdown.
- Direct-host and Docker documentation show the slug workflow without requiring
  a token in unauthenticated mode.
