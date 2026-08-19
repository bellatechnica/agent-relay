# Direct host sessions

Use this procedure when Agent Relay and the coding-agent processes run in the
same host network namespace. The default server mode trusts each session to
self-register a human-readable slug, so ordinary launches need no relay token
or per-session environment variable.

Every client connects to `http://127.0.0.1:8787/mcp` and configures that remote
Model Context Protocol (MCP) server once. A resumed conversation may recover its
existing slug; each independent conversation should use a distinct slug.

## 1. Start and verify the relay

Start the relay in one terminal:

```bash
agent-relay
```

Verify it from another terminal:

```bash
curl --fail http://127.0.0.1:8787/health
```

Continue only when the health response is exactly `{"status":"ok"}`. On
Windows Subsystem for Linux (WSL), use the [autostart guide](autostart-wsl.md)
when the service must survive closed terminals and Windows restarts.

## 2. Configure a client once

### Codex

Add this table to `~/.codex/config.toml`:

```toml
[mcp_servers.agent_relay]
url = "http://127.0.0.1:8787/mcp"
required = true
tool_timeout_sec = 86400
```

Run `codex mcp list` before launch to confirm that `agent_relay` is configured.
In a running session, `/mcp` shows active servers. See the official
[Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

`tool_timeout_sec = 86400` gives one blocking listener call a 24-hour client
deadline. When it expires without a message, the relay workflow reports the
timeout and starts one replacement listener. The relay does not remove or
acknowledge a message when the client cancels; a message committed during the
replacement gap remains pending. The finite deadline limits how long one
client request, listener subagent, and active parent turn remain open. As of
2026-08-17, the Codex configuration reference defines this setting in seconds
and documents no unlimited value.

### Claude Code

Add one user-scoped remote server with a 24-hour tool timeout. If an older
`agent_relay` entry already exists at user scope, remove that entry before
adding this replacement:

```bash
claude mcp add-json --scope user agent_relay \
  '{"type":"http","url":"http://127.0.0.1:8787/mcp","timeout":86400000}'
```

Run `claude mcp list` to verify the connection, or use `/mcp` inside Claude
Code. See Claude Code's [MCP guide](https://code.claude.com/docs/en/mcp).

Claude Code measures the per-server `timeout` in milliseconds. The
`86400000` value gives one `wait_for_messages` call a 24-hour deadline and, on
Claude Code v2.1.203 or later, raises that server's MCP tool idle window
above the default 300 seconds. When the 24-hour deadline expires without a
message, handle it like the Codex deadline: report the timeout and start one
replacement listener. The relay leaves any concurrently committed message
pending for that replacement.

### OpenCode

Add the remote server under the required MCP name:

```bash
opencode mcp add agent_relay --url http://127.0.0.1:8787/mcp
opencode mcp list
```

Persist this client feature flag in the environment inherited by ordinary
OpenCode launches:

```bash
export OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true
```

Put the export in the shell startup or environment configuration used to launch
OpenCode; setting it for only one test command does not enable later sessions.
The flag exposes `task(background: true)`, which the relay listener needs. The
configuration format follows OpenCode's [remote MCP server
reference](https://opencode.ai/docs/mcp-servers).

Configuration listing proves only that the entry exists. Make a live
`register_session` or `whoami` call before relying on the relay for a handoff.

## 3. Launch normally

These examples enable automatic operation and web search where the client
provides corresponding flags:

```bash
codex --approve-for-me --search
CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT=86400000 claude --permission-mode auto
opencode --auto
```

`--approve-for-me` retains Codex's workspace sandbox and routes approval
requests through automatic review. Claude Code auto mode depends on the
installed version, account, provider, and selected model. Avoid permission
bypass flags on a host that has no outer sandbox.

The launch-time `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` is a compatibility backstop
for Claude Code v2.1.187 through v2.1.202, where the separate five-minute MCP
idle watchdog is not floored by a per-server `timeout`. It applies to every MCP
server in that Claude process; the `agent_relay` entry remains independently
pinned to the same 24-hour wall-clock limit. Claude Code v2.1.203 and later use
the per-server value as the minimum idle window, so the environment override is
redundant but harmless there.

## 4. Establish the two-way workflow

Give each agent its assigned slug and the other participant's exact slug. A
useful first prompt is:

```text
Use agent-relay for coordination. Register as host-implementer with agent kind
codex and pass host-implementer as acting_slug. The other participant is
host-coordinator. Use the agent-relay-message skill. Read every pending message,
then maintain exactly one background listener with wait_for_messages.
Acknowledge a message only after processing it, replace the listener after
handling its complete result, and use reply_to_message for responses. Inspect
recipient_waiting_at_send after every send or reply; false means the message is
durable but no recipient MCP wait was observed.
```

The background listener blocks inside MCP without polling. As observed on
2026-08-17, Codex 0.147.0 uses a cheaper listener subagent and requires its
parent turn to remain active on the collaboration wait; user prompts can steer
that running turn, after which it continues waiting for the same child.
OpenCode 1.18.18 uses `task(background: true)` and its current model;
background completion starts the parent handling turn. Claude Code uses its
background-task completion behavior when supported by the running client. The
[protocol reference](protocol.md#what-push-means-for-an-agent) records the
version-specific observation and the receiver alternatives.

Codex App Server and OpenCode's HTTP API are not required. Receivers built on
those control APIs are optional P2 integrations described in the [protocol
reference](protocol.md#what-push-means-for-an-agent).

The `agent-relay-message` skill adds this discipline for skill-aware clients.
The companion `handoff` workflow uses Agent Relay by default and includes both
slugs in the child prompt. When both sessions share a tmux server, a false
`recipient_waiting_at_send` result triggers a tmux wake notice containing only
the Relay message ID and recovery instruction. A true result uses no tmux
message. Relay preflight failure may select tmux as the exclusive channel only
when the fallback is available and reported to the user.

## 5. Verify two-way communication

Use fresh slugs whose inboxes are initially empty:

1. Register `host-coordinator` and `host-implementer` from their corresponding
   sessions.
2. Have the coordinator send one probe to `host-implementer`.
3. Have the implementer read the probe and reply with `reply_to_message`.
4. Have the coordinator read the reply.
5. Acknowledge each processed message. Both inboxes must then be empty.

Successful registration, send, reply, and acknowledgement calls prove MCP
initialization and two-way routing. Seeing `agent_relay` in a configuration
list alone does not.

## Optional token authentication

Use `agent-relay --authentication-mode token` when callers must prove their
relay identity. Token mode requires `AGENT_RELAY_ADMIN_TOKEN`, one issued
session credential per agent, and bearer configuration in each MCP client. The
[authenticated flow recap](protocol.md#authenticated-flow-recap) defines that
setup and its security boundary.
