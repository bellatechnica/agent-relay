---
name: agent-relay-message
description: Register a coding-agent session with Agent Relay and exchange durable messages by exact slug. Use when the user asks to register a Codex, Claude Code, or OpenCode session with the relay; send, read, reply to, or acknowledge relay messages; contact another agent by slug; or coordinate a handoff through Agent Relay instead of tmux.
---

# Message through Agent Relay

Use the configured `agent_relay` Model Context Protocol (MCP) tools. Do not
silently substitute tmux messaging when the relay is unavailable.

## Apply channel-independent message safety

- Address one exact recipient slug and let relay metadata identify the sender;
  do not add a tmux pane prefix to message content.
- Preserve the complete message and use `reply_to_message` to keep response
  routing attached to the received message.
- Distinguish durable server acceptance from recipient processing. A successful
  send means queued; acknowledgement means processed.
- Treat a busy or idle recipient as valid: the durable inbox holds the message
  until that session reads it.
- Do not blindly repeat a send whose result is unknown after a transport timeout;
  the first call may have committed, so retrying can create a duplicate. Report
  the ambiguous outcome and retain the exact content for the user to decide.

The pane-inspection, occupied-composer, bracketed-paste, and empty-composer
checks in `tmux-message` do not apply to Agent Relay because no keystrokes are
injected into another session.

## Establish this session's slug

- Use the slug assigned by the user or handoff prompt. Never select one active
  slug arbitrarily from `list_sessions` and claim it as this session.
- Call `register_session(slug, agent_kind)` in unauthenticated mode. Registration
  is idempotent, so repeat it when resuming a session or when registration state
  is uncertain.
- Pass the same slug as `acting_slug` on later calls. In authenticated mode,
  omit `acting_slug`; the bearer token establishes the caller.
- If no caller slug is available from the user, prompt, or acknowledged session
  context, ask for one before sending or reading.

## Send

Call `send_message` with the exact `recipient_slug`, this session's
`acting_slug`, and the user's complete message. Preserve the user's words; do
not summarize or truncate them. Treat a successful tool result as durable relay
acceptance, not proof that the recipient processed the message.

## Read and acknowledge

Call `read_inbox` with this session's `acting_slug` and handle every returned
message in send order. Acknowledge each message only after completing the action
it requests or presenting its complete content to the user. Do not acknowledge
merely because the inbox call returned it.

When a response belongs to a received message, call `reply_to_message`; the
relay derives the other participant. Use a new `send_message` only for a new
thread or when the user explicitly addresses a different slug.

## Delivery boundary

Agent Relay does not start turns in idle agent clients. Read the inbox at prompt
start, before a coordination wait, after meaningful milestones, and before
reporting completion. Do not run an unbounded polling loop.

If a relay call fails, report the operation, caller slug, recipient slug when
applicable, and the returned error. Do not claim delivery and do not switch to
tmux unless the user explicitly requests tmux mode.
