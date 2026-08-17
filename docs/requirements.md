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
  acknowledgements with the same safety discipline as the MCP tools.
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
   start and at meaningful checkpoints, acknowledge only after processing, and
   use replies when responding to a received message.
5. Use the two slugs for clarifications, blocked-state notices, coordination
   signals, and completion reports that would otherwise travel through
   `tmux-message`.

The existing `tmux-message` path remains available as an explicit user-selected
mode. A handoff must not silently fall back to tmux when relay registration or
connectivity fails; it must report the relay blocker and obtain the user's
direction. Relay remains usable when the sessions do not share a reachable tmux
server, including across a Docker Sandbox boundary.

## Delivery and wake-up boundary

The relay server must not poll its SQLite database for new messages. It may hold
a Server-Sent Events (SSE) connection open and wake it when a message is
committed, as specified in the [protocol reference](protocol.md#what-push-means-for-an-agent).

An MCP message does not, by itself, start a new turn in an idle Codex, Claude
Code, or OpenCode session. The immediate handoff workflow therefore requires
agents to read their inboxes at prompt start and at meaningful checkpoints.
Automatic turn injection requires a client-specific receiver and is outside the
immediate slug-routing scope.

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
  instructions, inbox checkpoints, acknowledgement discipline, and the reply
  instruction.
- Direct-host and Docker documentation show the slug workflow without requiring
  a token in unauthenticated mode.
