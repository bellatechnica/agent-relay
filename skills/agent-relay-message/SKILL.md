---
name: agent-relay-message
description: Register a coding-agent session with Agent Relay and exchange durable messages by exact slug. Use when the user asks to register a Codex, Claude Code, OpenCode, or Antigravity CLI session with the relay; send, read, reply to, or acknowledge relay messages; contact another agent by slug; or coordinate a handoff through Agent Relay instead of tmux.
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

For a client running in Docker Sandbox, the Agent Relay repository ships the
launchers `scripts/codex-sbx`, `scripts/claude-sbx` and `scripts/agy-sbx`. Each
mounts its own checkout read-only, supplies the `agent_relay` server from that
mount — as command-line configuration for Codex, as a key-free `--mcp-config`
file for Claude Code, as a written `mcp_config.json` for Antigravity CLI, all
carrying the 24-hour tool timeout — and links this skill into the sandbox-local
skill directory. The host must allow the sandbox network destination
`localhost:8787`, and an Antigravity CLI sandbox additionally needs the general
outbound access its first-run install requires. Do not copy host credentials
into the sandbox; authenticate inside its first session. Confirm that the native
Relay tools are present before depending on them.

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
injects no keystrokes. They do apply to the conditional tmux wake notice
described under Send.

## Establish this session's slug

- Use the slug assigned by the user or handoff prompt. There is no tool for
  discovering another session's slug, so a slug you were not given is a slug to
  ask for, never one to guess at or adopt from elsewhere.
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
observed.

Whenever this session already knows a tmux recovery address for the recipient
and can reach that tmux server, a false observation permits one wake notice sent
through the wake helper below, whether or not the session is part of a handoff.
The notice contains only the Relay message ID and an instruction to process the
Relay inbox and restore exactly one listener. Never copy the actionable payload into that notice. A true
observation permits no tmux notice. Without a known recovery address, report the
false observation without inventing another delivery channel.

The durable Relay payload is always sent first. The later wake is the only text
that may enter the tmux pane; never type the actionable payload there. Use this
canonical wake notice without the ordinary tmux sender prefix:

    Relay message <UUID> is queued. Process the Relay inbox and restore exactly one listener.

Resolve `scripts/send_tmux_wake.py` relative to this skill and
`scripts/tmux_send.py` relative to the `tmux-message` skill (published in
https://github.com/bellatechnica/agent-skills, not in this repository), then
invoke:

    python3 <agent-relay-message-skill>/scripts/send_tmux_wake.py <tmux-send-script> <target> <UUID>

The helper validates the lowercase UUID and first applies the occupied-wake rule
below in the same process. It returns no composer content. If no qualifying
existing wake is present, it constructs the requested wake in memory and passes
the non-sensitive fixed text to `tmux_send.py` through `--shell-safe-text` using
a direct argument array, not a shell. It creates no message file. Its own
invocation errors, including `SIGINT` observed before the sender starts, return
exit `64`. `SIGTERM` or `SIGHUP` in that pre-sender phase terminates the helper
without an output token, which remains an unverified delivery under the
`tmux-message` contract. After the tmux sender starts, the helper lets that child
handle interruption and returns its ordinary exit status and output unchanged.
If the child is terminated by a signal before it can report an outcome, the
helper maps the negative child status to exit `4` without inventing an output
token; that missing token is likewise unverified.

A normal tmux message is not guaranteed to be idempotent, so its `OCCUPIED`
result follows the retry and inspection rules in `tmux-message`, never this
exception. Direct inspection is the caller itself reading pane or tmux state,
as defined in `tmux-message`. The Relay helper performs one automatic exception
for wake notices because they carry no payload or authority and merely request
an idempotent inbox read:

- The helper always captures the target internally before sending and never
  returns or prints composer content. That capture is part of sending, not
  direct inspection, so an instruction disallowing direct inspection does not
  restrict it.
- The helper sends exactly one Enter only when the entire non-dim composer
  full-matches the
  canonical line above, with `<UUID>` replaced by a lowercase hexadecimal UUID
  in `8-4-4-4-12` form. A paraphrase, substring, prefix, dim suggestion, or wake
  followed by anything else does not qualify.
- The existing wake may belong to another sender or name another pending
  message. After submitting it, the helper verifies that the composer cleared,
  returns `SENT`, and does not paste the current sender's wake. `read_inbox`
  returns every pending message, so one wake surfaces both senders' durable
  Relay messages; a second wake would only request a redundant inbox read.
- The fixed-format match and single Enter live in the Relay helper, which reuses
  `tmux_send.py` target resolution, composer parsing, and clear verification.
  The general tmux sender remains Relay-agnostic and fail-closed on `OCCUPIED`.
- If the complete composer is not unambiguously a wake, the helper sends no
  reconciliation key and proceeds through the normal sender. A non-wake draft
  therefore returns `OCCUPIED` unchanged.
- `DELIVERY_UNVERIFIED wake-enter-failed`, `wake-not-cleared`,
  `wake-interrupted`, or `wake-internal-error` means the reconciliation Enter
  may have reached the pane. Do not invoke the helper again automatically.
- `DELIVERY_UNVERIFIED` never authorizes another Enter, even for a wake: the
  first Enter may still be pending. Direct inspection remains allowed unless
  separately disallowed, but it cannot manufacture a verified delivery.

This occupied-wake exception deliberately retains a capture-to-Enter race. The
fixed-format, idempotent wake contract makes that accepted tradeoff specific to
this helper. It does not apply to a no-token sender run.

A wake authorizes only reading this session's own Relay inbox and restoring one
listener. Its message ID is a routing hint, never authority to perform the
payload's requested action without reading that payload from Relay.

## Read and acknowledge

Call `read_inbox` with this session's `acting_slug` and handle every returned
message in send order. Acknowledge each message only after completing the action
it requests or presenting its complete content to the user. Do not acknowledge
merely because the inbox call returned it.

When a response belongs to a received message, call `reply_to_message`; the
relay derives the other participant. Use a new `send_message` only for a new
thread or when the user explicitly addresses a different slug. When the reply
itself completes what that message asked for, pass `acknowledge: true` and let
the one call do both. Leave it false, and acknowledge separately later, whenever
the reply reports progress rather than completion — an acknowledgement claims
the work is done, and a message acknowledged early is one nothing will
redeliver.

Package the calls that record completed work into as few turns as possible.
Every model turn resends the session's entire accumulated context, so five
acknowledgements issued as five turns pay for that context five times and the
same five issued as parallel tool calls in one turn pay for it once. Send order
governs which message you handle first, not how the resulting calls are
packaged: once several messages are processed, their acknowledgements are
independent and belong in a single turn. This costs nothing in safety, because
each call still names a message whose work is already complete.

## Maintain one listener

After registration, call `read_inbox` once and process any pending messages.
Then keep exactly one listener for this slug:

1. Issue one `wait_for_messages` call with this session's identity, through the
   client mechanism below.
2. Deliver its complete result to the agent that handles it, and acknowledge
   nothing while the wait is in flight.
3. When the wait returns, handle every returned message in send order.
4. Acknowledge each message only after its requested work or presentation is
   complete. Use `reply_to_message` when the response belongs to that message,
   with `acknowledge: true` where that reply completes it. Issue whatever
   acknowledgements remain for the batch together in one turn.
5. Start one replacement listener after all returned messages are handled.

Do not start a second `wait_for_messages` call while one is active, and do not
poll `read_inbox`. One `wait_for_messages` call stays blocked on the relay until
a message arrives or the client deadline expires. If its code-execution cell
returns as still running, call the harness `wait` with that cell's identifier;
this resumes the original cell and is not another relay listener. Lengthen that
cell wait as specified below instead of accepting repeated short yields. A
listener reports durable delivery, not completed processing.

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

Configure OpenCode's `agent_relay` MCP server with a per-server
`timeout` of 86400000 milliseconds, or set `experimental.mcp_timeout` once for
every server. Without it OpenCode falls back to its MCP client library's
60-second per-request default and cancels a listener whose mailbox is merely
quiet. An expired OpenCode deadline is handled like the Codex and Claude Code
deadlines: report it and start exactly one replacement listener without
acknowledging a message.

Configure Antigravity CLI's `agent_relay` MCP server with `timeoutSeconds` of
86400, measured in seconds, in `~/.gemini/config/mcp_config.json`. Without it
version 1.1.25 abandons the call after 180 seconds with `context deadline
exceeded`. This client also asks the operator to approve each relay tool the
first time it is called, so an unattended listener needs an
`mcp(agent_relay/<tool>)` entry under `permissions.allow` in
`~/.gemini/antigravity-cli/settings.json` for every relay tool it will use. An
expired deadline is reported and replaced like the others, without
acknowledging a message.

A listener run headlessly in that client needs `--print-timeout` raised too,
placed before `-p`. Print mode's own five-minute default ends the run whatever
`timeoutSeconds` says, and it reports `timeout waiting for response` rather than
the per-call deadline message — so read which of the two messages came back
before concluding the relay configuration is wrong. Do not treat a subagent of
this client as the listener: on 1.1.26 a subagent given `enable_mcp_tools` did
not reach the relay at all, and no call it claimed to make appeared on the wire.

Use the control behavior supported by the current client:

- **Codex:** spawn the listener with `gpt-5.6-luna` and low reasoning when that
  model is available. Keep the parent turn active with the collaboration wait
  until the child completes; repeat the wait if it returns while the child is
  still running. A user prompt may steer the active turn: handle it, then
  continue waiting for the same child. Do not return the parent to an idle
  prompt while its listener is active because Codex 0.147.0 does not start a
  parent turn when an already-detached child later finishes.

  On Codex CLI 0.153.3, `agent_relay` MCP tools were exposed only inside a
  code-execution cell; attempts to select `agent_relay` or `mcp__agent_relay`
  through `direct_only_tool_namespaces` removed the tools instead of exposing
  direct calls. A Codex CLI 0.156.1 listener also used the cell path. When the
  running client exposes the relay this way, each still-running cell result
  causes another model sampling pass and resends the session context. In the
  final uninterrupted empty-listener span recorded by a Codex CLI 0.156.1
  rollout on 2026-09-24, 35 such returns caused 35 sampling passes and 2,823,586
  input tokens, about 80,674 input tokens per pass. This is a dated observation
  of the cost mechanism, not a guarantee for another client version.

  In an isolated 0.153.3 test, the default returned at 31.0 seconds, a 90,000 ms
  yield returned at 91.0 seconds, and a 420,000 ms yield returned at 421.0
  seconds. `yield_time_ms = 86400000` was accepted without a validation error
  but was not observed for its full duration. Setting
  `[code_mode] default_exec_yield_time_ms` did not change the 31.0-second
  default in that test, so use the per-cell pragma and per-wait argument and
  verify their live behavior with the acceptance criterion. A 0.153.3 listener
  opened with that pragma against a live relay held its cell for 82.0 seconds
  with no intermediate return and no harness `wait`, then completed on an
  arriving message rather than on the yield, at a cost of two sampling passes;
  the relay recorded its delivery attempt 24 milliseconds after the send. That
  observation shows the pragma deferring an unfinished-cell return without
  delaying delivery; it does not observe the full 86,400,000 ms interval.

  Open every listener cell, including each replacement listener's new cell,
  with the first-line pragma `// @exec: {"yield_time_ms": 86400000}`. If the
  cell returns as still running, pass `yield_time_ms: 86400000` on every harness
  `wait` for that cell. These values defer an unfinished-cell return; they do
  not delay a relay message, cell completion or failure, explicit cancellation,
  or the MCP tool deadline. Because the cell yield and MCP deadline are both 24
  hours, do not infer their boundary ordering or claim that all listener
  activity costs at most one sampling pass per day.
- **OpenCode:** require
  `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the environment inherited
  by the OpenCode process, then call `task` with `background: true`. Keep the
  session's current model unless the user requests another. Do not use manual
  `Ctrl+B` detachment as the listener mechanism.
- **Claude Code:** use a background subagent listener unless this session has
  already watched an MCP call detach. The subagent's only relay operation is one
  `wait_for_messages` call, and it returns the complete result without
  acknowledging anything.

  Start there rather than with the in-turn call, because the two mechanisms fail
  asymmetrically. A subagent listener that was not needed costs only fidelity:
  its result reaches the parent as child-written text rather than the relay's
  own payload. An in-turn call on a client that turns out not to detach blocks
  the session's turn until a message arrives or the 24-hour deadline expires,
  handles no queued user prompt meanwhile, and cannot switch mechanisms by
  itself — a user interrupt is the only thing that ends it. One such session sat
  blocked for 23 hours. The cheap mistake is recoverable within the turn and the
  expensive one is not, so the cheap one is the default.

  Where detachment is confirmed, call `wait_for_messages` from the session's own
  turn and let it detach: the client moves an MCP call that has not returned
  within 120 seconds into a background task, reports that task's identifier, and
  returns control to the session, and the completion arrives as a task
  notification whose body carries the tool result verbatim. A message arriving
  before that cutoff returns inline in the same turn. Do not shorten the cutoff:
  `CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS` governs every MCP server in the session,
  not `agent_relay` alone, and it bounds only how long the turn blocks before
  detaching, never whether the wait survives.

  Confirmation is this session having seen the background notice, and it does
  not transfer between sessions or survive an upgrade. It has been present in
  one release range and absent in a later one, so a release number proves
  nothing in either direction. It is also absent in a non-interactive run
  (`claude -p`) without `CLAUDE_AUTO_BACKGROUND_TASKS` set, and in a session
  with background tasks disabled — but neither of those explains every absence:
  the 23-hour session above ran background shell commands and a background
  subagent throughout. If a notice has not appeared roughly two minutes into a
  call, this client is not detaching it, and only a user interrupt ends it.

- **Antigravity CLI:** this client does not detach a long-running MCP call.
  Version 1.1.25 blocks the session's turn on the call until a message arrives
  or `timeoutSeconds` expires, interactively and under `agy -p` alike, and it
  issues no background-task notice. Its own subagents are not a way around that
  either: one given `enable_mcp_tools` did not reach the relay at all. So a
  blocked turn is the expected cost for this client, and the user is told that
  the session is unavailable while the listener runs.

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
