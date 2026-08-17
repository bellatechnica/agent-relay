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
  arrive. Its completion wakes the parent session. The parent reads and handles
  every pending message, acknowledges each message only after processing it,
  and then starts one replacement listener.
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

The `handoff` skill must use Agent Relay as its default and exclusive
inter-session communication channel. It uses `tmux-message` only when the user
explicitly requests tmux mode. This rule governs communication after launch;
the skill may still use tmux to create and host a local agent window.

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

The existing `tmux-message` path remains available as an explicit user-selected
mode. A handoff must not silently fall back to tmux when relay registration or
connectivity fails; it must report the relay blocker and obtain the user's
direction. Relay remains usable when the sessions do not share a reachable tmux
server, including across a Docker Sandbox boundary.

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

An MCP response does not itself start a new model turn. The supported immediate
wake mechanism is completion of a background listener subagent that made the
blocking MCP call. The parent session remains responsible for reading the full
inbox, processing it, acknowledging processed messages, and maintaining exactly
one listener after handling completes.

## Deployment and security boundary

- The relay must work on host loopback and through Docker's
  `host.docker.internal` route without exposing a Docker or tmux control socket
  to the sandbox.
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
- In an ordinary Codex session and an OpenCode session with background tasks
  enabled, a background listener remains blocked while its inbox is empty,
  completes after a relay message arrives, and wakes its parent without terminal
  input or a client-control API.
- A blocking MCP wait returns every message already pending, or blocks without
  database polling until a send commits. Returned messages have a recorded
  delivery attempt and remain unacknowledged.
- Cancelling a blocking MCP wait before delivery leaves the inbox unchanged.
- Direct-host and Docker documentation show the slug workflow without requiring
  a token in unauthenticated mode.
