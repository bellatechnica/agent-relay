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
```

Run `codex mcp list` before launch to confirm that `agent_relay` is configured.
In a running session, `/mcp` shows active servers. See the official
[Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

Add one user-scoped remote server:

```bash
claude mcp add --transport http --scope user \
  agent-relay http://127.0.0.1:8787/mcp
```

Run `claude mcp list` to verify the connection, or use `/mcp` inside Claude
Code. See Claude Code's [MCP guide](https://code.claude.com/docs/en/mcp).

### OpenCode

Merge this entry into `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "agent-relay": {
      "type": "remote",
      "url": "http://127.0.0.1:8787/mcp",
      "oauth": false
    }
  }
}
```

The format follows OpenCode's
[remote MCP server reference](https://opencode.ai/docs/mcp-servers).

Configuration listing proves only that the entry exists. Make a live
`register_session` or `whoami` call before relying on the relay for a handoff.

## 3. Launch normally

These examples enable automatic operation and web search where the client
provides corresponding flags:

```bash
codex --approve-for-me --search
claude --permission-mode auto
opencode --auto
```

`--approve-for-me` retains Codex's workspace sandbox and routes approval
requests through automatic review. Claude Code auto mode depends on the
installed version, account, provider, and selected model. Avoid permission
bypass flags on a host that has no outer sandbox.

## 4. Establish the two-way workflow

Give each agent its assigned slug and the other participant's exact slug. A
useful first prompt is:

```text
Use agent-relay for coordination. Register as host-implementer with agent kind
codex and pass host-implementer as acting_slug. The other participant is
host-coordinator. Read every pending inbox message before work and after each
meaningful milestone. Acknowledge a message only after processing it. Use
reply_to_message when a response belongs to a received message.
```

The relay queues messages durably but does not start a new turn in an idle
Codex, Claude Code, or OpenCode session. Interactive agents must call
`read_inbox` at agreed checkpoints. For automatic turn injection, use a
client-specific receiver as described in the
[protocol reference](protocol.md#what-push-means-for-an-agent).

The `agent-relay-message` skill adds this discipline for skill-aware clients.
The companion `handoff` workflow uses Agent Relay by default and includes both
slugs in the child prompt; tmux messaging is an explicit alternative rather
than a silent fallback.

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
