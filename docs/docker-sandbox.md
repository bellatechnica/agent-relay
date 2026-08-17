# Docker Sandbox setup

Use the relay's default trusted slug mode when the host and every allowed
sandbox belong to the same trusted development environment. Each agent
self-registers its assigned slug; no relay token needs to cross the sandbox
boundary.

The host relay remains bound to `127.0.0.1`. Docker Sandbox reaches it through
`host.docker.internal`. Docker's proxy rewrites that host name to host
localhost, so the policy rule must allow `localhost:8787`, not
`host.docker.internal:8787`. See Docker's
[host-service workflow](https://docs.docker.com/ai/sandboxes/workflows/#accessing-host-services-from-a-sandbox).

## 1. Choose the sandbox network policy

For a new local installation, initialize Docker Sandbox with its Balanced
policy. It denies destinations by default while allowing common model-provider,
package-manager, source-hosting, registry, and cloud-service endpoints:

```bash
sbx policy init balanced
```

See Docker's
[local-policy reference](https://docs.docker.com/ai/sandboxes/governance/local/)
before replacing an existing policy. Organization governance can override local
rules.

Web search and page retrieval still need the destinations used by the selected
agent or search provider. Exercise search once, inspect denied requests with
`sbx policy log SANDBOX_NAME`, and allow each required domain explicitly:

```bash
sbx policy allow network --sandbox SANDBOX_NAME "search-provider.example:443"
```

Avoid `sbx policy allow network "**"` when network isolation matters. Docker's
[network troubleshooting guide](https://docs.docker.com/ai/sandboxes/troubleshooting/#agent-cant-install-packages-or-reach-an-api)
explains how to identify blocked destinations.

## 2. Create the sandbox without attaching

Clone mode keeps the agent's Git writes in an isolated clone until the host
fetches them. Replace `codex` with `claude` or `opencode` when needed.

```bash
sbx create --clone --name relay-codex codex /path/to/project
sbx policy allow network --sandbox relay-codex localhost:8787
```

Docker documents the isolation difference in its
[sandbox usage guide](https://docs.docker.com/ai/sandboxes/usage/#clone-mode).

Verify the route before configuring the agent:

```bash
sbx exec relay-codex curl --fail \
  http://host.docker.internal:8787/health
```

Continue only when the response is exactly `{"status":"ok"}`.

## 3. Configure the relay MCP server

All clients use Streamable HTTP at
`http://host.docker.internal:8787/mcp`. The checked-in examples contain no
credential or machine-specific secret.

### Codex

Merge [the Codex example](../examples/codex-config.toml) into the sandbox's
`~/.codex/config.toml`:

```toml
web_search = "live"

[mcp_servers.agent_relay]
url = "http://host.docker.internal:8787/mcp"
required = true
```

The configuration locations are defined in the official
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

Run this inside the sandbox:

```bash
claude mcp add --transport http --scope user \
  agent-relay http://host.docker.internal:8787/mcp
```

See Claude Code's
[MCP configuration guide](https://code.claude.com/docs/en/mcp).

### OpenCode

Merge [the OpenCode example](../examples/opencode.json) into the sandbox's
`~/.config/opencode/opencode.json`. The format follows OpenCode's
[remote MCP server reference](https://opencode.ai/docs/mcp-servers).

## 4. Launch in automatic mode

Docker Sandbox starts Codex and Claude Code with their approval-bypass flags by
default. OpenCode needs its explicit auto flag:

```bash
sbx run --name relay-codex
sbx run --name relay-claude
sbx run --name relay-opencode -- --auto
```

Create and configure a distinct named sandbox before each corresponding run.
Docker documents the default commands for
[Codex](https://docs.docker.com/ai/sandboxes/agents/codex/#default-startup-command)
and
[Claude Code](https://docs.docker.com/ai/sandboxes/agents/claude-code/#default-startup-command).
Automatic mode gives the agent broad control inside its microVM and workspace;
clone mode and the network allowlist remain the containment boundaries.

Give the sandbox agent its assigned slug and the outside session's exact slug:

```text
Use agent-relay for coordination. Register as relay-codex with agent kind codex
and pass relay-codex as acting_slug. The outside session is host-coordinator.
Read every pending inbox message before work and after meaningful milestones.
Acknowledge a message only after processing it, and use reply_to_message for
responses.
```

The outside client uses the same configuration with
`http://127.0.0.1:8787/mcp` and its own slug.

## Push versus agent wakeup

An active agent can call `read_inbox` through MCP and respond in either
direction. A receiver may open
`http://host.docker.internal:8787/v1/events`; the server wakes that SSE
connection when a message is committed and does not poll SQLite.

SSE alone does not start a new Codex, Claude Code, or OpenCode turn. Fully
unattended reactions require a client-specific adapter that converts an SSE
message into the client's supported turn-start or steering call, then
acknowledges only after the client accepts it. The relay does not mount the host
Docker socket, terminal-multiplexer socket, or agent-control socket into the
sandbox.

## Optional token authentication

Run `agent-relay --authentication-mode token` when sandboxes must prove their
relay identities. That mode requires an administrator token, one issued bearer
credential per agent, and corresponding MCP authorization headers. Follow the
[authenticated flow recap](protocol.md#authenticated-flow-recap); do not put
the administrator token inside a sandbox.
