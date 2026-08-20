---
name: agent-relay-message
description: Register a coding-agent session with Agent Relay and exchange durable messages by exact slug. Use when the user asks to register a Codex, Claude Code, or OpenCode session with the relay; send, read, reply to, or acknowledge relay messages; contact another agent by slug; or coordinate a handoff through Agent Relay instead of tmux.
---

# Message through Agent Relay

## Require native MCP tools

Use only the configured `agent_relay` Model Context Protocol (MCP) tools for
Relay operations. Finding this `SKILL.md` on disk does not prove that the MCP
server or its tools loaded into the current agent process.

If the native tools are absent, stop and report the missing MCP setup. Do not
generate a Python client, call the Relay REST API with `curl`, or use shell HTTP
as a substitute. Do not silently substitute tmux messaging when Relay is
unavailable.

For Codex running in Docker Sandbox, the repeatable `codex-sbx` launcher
supplies the `agent_relay` MCP URL and 86,400-second tool timeout as command-line
configuration on every run. It also links this curated skill into the
sandbox-local `$CODEX_HOME/skills/` directory. Do not copy host Codex
credentials into the sandbox; authenticate inside its first session. Confirm
that the native Relay tools are present before depending on them.

For Claude Code running in Docker Sandbox, the host must allow the sandbox
network destination `localhost:8787`. The repeatable launcher workflow mounts
one key-free MCP configuration from the host and passes its in-sandbox path to
every model profile:

```bash
claude --mcp-config /path/in/sandbox/agent-relay.mcp.json
```

That JSON file defines `agent_relay` as an HTTP server at
`http://host.docker.internal:8787/mcp` with an 86,400,000-millisecond timeout.
The host copy survives sandbox deletion and supplies native tools to the first
Claude process without mutable container setup.

For an interactively maintained sandbox, this user-scoped command is an
alternative:

```bash
claude mcp add --transport http --scope user \
  agent_relay http://host.docker.internal:8787/mcp
```

Start a new Claude process after adding it. User-scoped configuration survives
sandbox stops but is deleted with the sandbox. In both workflows, confirm
through `/mcp` that `agent_relay` is connected before using this skill.

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
checks in `tmux-message` do not apply to the Relay payload call because it
injects no keystrokes. They do apply when a handoff sends a conditional tmux
wake notice.

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

Inspect `recipient_waiting_at_send` in every successful `send_message` or
`reply_to_message` result. `true` means the relay observed an active recipient
MCP wait when it notified the recipient; it does not prove processing or later
listener replacement. `false` means the message is durable but no MCP wait was
observed. Outside a handoff with a known tmux recovery address, report the false
observation without inventing another delivery channel.

In a Relay-based handoff, follow the `handoff` skill's conditional wake rule. A
false observation permits one `tmux-message` wake notice containing the Relay
message ID and an instruction to process the Relay inbox and restore exactly
one listener. Never copy the actionable payload into that notice. A true
observation permits no tmux notice.

## Read and acknowledge

Call `read_inbox` with this session's `acting_slug` and handle every returned
message in send order. Acknowledge each message only after completing the action
it requests or presenting its complete content to the user. Do not acknowledge
merely because the inbox call returned it.

When a response belongs to a received message, call `reply_to_message`; the
relay derives the other participant. Use a new `send_message` only for a new
thread or when the user explicitly addresses a different slug.

## Maintain one listener

After registration, call `read_inbox` once and process any pending messages.
Then keep exactly one listener for this slug:

1. Issue one `wait_for_messages` call with this session's identity, through the
   client mechanism below.
2. Deliver its complete result to the agent that handles it, and acknowledge
   nothing while the wait is in flight.
3. When the wait returns, handle every returned message in send order.
4. Acknowledge each message only after its requested work or presentation is
   complete. Use `reply_to_message` when the response belongs to that message.
5. Start one replacement listener after all returned messages are handled.

Do not start a second listener while one is active. Do not poll `read_inbox` or
repeat short waits; the MCP wait remains blocked until a message arrives or the
configured client deadline expires. A listener reports durable delivery, not
completed processing.

A blocked `wait_for_messages` sends no progress notification, so a client's
silence limit is the only clock running on it. Raise that limit per server in
every client before relying on a listener; a default measured in minutes ends
the wait while the mailbox is merely quiet.

Configure Codex's `agent_relay` MCP server with
`tool_timeout_sec = 86400`. If an unchanged Codex wait reaches that 24-hour
client deadline, report the failed operation, caller slug, and timeout, then
start exactly one replacement listener. Do not acknowledge anything because a
timeout delivers no message. A message committed between cancellation and
replacement remains pending and returns when the replacement wait begins. This
once-per-day replacement is recovery from the configured client deadline, not
short-wait polling; any other repeated failure requires diagnosis instead of a
retry loop.

Configure Claude Code's `agent_relay` MCP server with a per-server
`timeout = 86400000` milliseconds. Claude Code v2.1.203 and later use that
wall-clock limit as the minimum idle window for the server. Without it the call
aborts after the client default of 300 seconds of silence, reporting that the
server sent no response or progress. If an unchanged Claude wait reaches the
24-hour deadline, apply the same reporting and single-replacement behavior as
for Codex without acknowledging a message.

Use the control behavior supported by the current client:

- **Codex:** spawn the listener with `gpt-5.6-luna` and low reasoning when that
  model is available. Keep the parent turn active with the collaboration wait
  until the child completes; repeat the wait if it returns while the child is
  still running. A user prompt may steer the active turn: handle it, then
  continue waiting for the same child. Do not return the parent to an idle
  prompt while its listener is active because Codex 0.147.0 does not start a
  parent turn when an already-detached child later finishes.
- **OpenCode:** require
  `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the environment inherited
  by the OpenCode process, then call `task` with `background: true`. Keep the
  session's current model unless the user requests another. Do not use manual
  `Ctrl+B` detachment as the listener mechanism.
- **Claude Code:** where the client detaches long-running MCP calls, call
  `wait_for_messages` from the session's own turn and let it detach. An
  interactive Claude Code moves an MCP call that has not returned within 120
  seconds into a background task, reports that task's identifier, and returns
  control to the session; the call keeps running, and its completion arrives as
  a task notification whose body carries the tool result verbatim. A message
  arriving before that cutoff returns inline in the same turn. Prefer this to a
  subagent listener, whose result reaches the parent as child-written text
  rather than the relay's own payload. Do not shorten the cutoff:
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` governs every MCP server in the session,
  not `agent_relay` alone, and it bounds only how long the turn blocks before
  detaching, never whether the wait survives.

  Treat detachment as a capability to confirm, not a version to assume: a
  feature gate can switch it off in any release. It is confirmed once this
  session has seen the notice that a call moved to the background. It is absent
  in a non-interactive run (`claude -p`) without `CLAUDE_AUTO_BACKGROUND_TASKS`
  set, and in a session with background tasks disabled.

  Use a background subagent listener whenever detachment is unconfirmed, and on
  any older or unknown client. Without it the in-turn call blocks the session's
  turn until a message arrives or the 24-hour deadline expires, and no queued
  user prompt is handled meanwhile; a blocked call cannot switch mechanisms by
  itself, so a user interrupt is what ends it. A notice that has not appeared
  roughly two minutes into the call means this client does not detach. Such a
  subagent's only relay operation is one `wait_for_messages` call, and it
  returns the complete result without acknowledging anything.

A finished listener carrying an empty result is not an empty mailbox. A wait
whose client connection closes — an exiting session, a restarted relay — can
report completion with no payload while every pending message stays pending.
Read the inbox rather than treating that as nothing to handle.

An OpenCode parent may return to the prompt only after that client has
demonstrated that background completion starts the handling turn; otherwise use
the Codex active-parent pattern. A Claude Code parent may return to the prompt
once the client has confirmed detachment of its listener, because a completed
background MCP task starts a turn carrying the result. At prompt start and
before reporting completion, read the inbox if listener state is absent or
uncertain.

If a relay call fails, report the operation, caller slug, recipient slug when
applicable, and the returned error. Do not claim delivery. An ambiguous send
failure permits neither a tmux payload resend nor a conditional wake notice
because no authoritative `recipient_waiting_at_send` result was returned. A
preflight failure may select the `handoff` skill's announced tmux fallback; an
ordinary Relay operation does not switch channels silently.
