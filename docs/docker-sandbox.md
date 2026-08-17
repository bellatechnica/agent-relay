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

Docker Sandbox must be able to create its microVM. Check KVM access before
downloading agent images:

```bash
test -r /dev/kvm && test -w /dev/kvm
```

If that command fails, enable hardware virtualization and nested virtualization
for the Linux environment, then make `/dev/kvm` accessible to the account that
runs Docker Sandbox. `sbx diagnose` can report a healthy daemon even when this
microVM prerequisite is absent; the sandbox container then fails at startup.

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

OpenCode also needs its background-task feature enabled in every ordinary
launch. Apply the checked-in Docker Sandbox kit when creating that sandbox:

```bash
sbx create --clone --name relay-opencode \
  --kit /path/to/agent-relay/examples/opencode-background-subagents-kit \
  opencode /path/to/project
sbx policy allow network --sandbox relay-opencode localhost:8787
```

The kit sets `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true` in the sandbox
environment. It is a client feature flag, not a relay credential.

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
  agent_relay http://host.docker.internal:8787/mcp
```

See Claude Code's
[MCP configuration guide](https://code.claude.com/docs/en/mcp).

### OpenCode

Merge [the OpenCode example](../examples/opencode.json) into the sandbox's
`~/.config/opencode/opencode.json`. The format follows OpenCode's
[remote MCP server reference](https://opencode.ai/docs/mcp-servers).
The MCP entry must be named `agent_relay`. Confirm it with `opencode mcp list`;
the result must show `agent_relay` connected.

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
Use the agent-relay-message skill. Read every pending inbox message, then keep
exactly one background listener blocked in wait_for_messages. Acknowledge a
message only after processing it, replace the listener after handling its
complete result, and use reply_to_message for responses.
```

The outside client uses the same configuration with
`http://127.0.0.1:8787/mcp` and its own slug.

## Background delivery

The listener subagent calls MCP `wait_for_messages`; the server holds that call
without polling SQLite and returns every pending message when a send commits.
The child returns the complete result without acknowledging it. The parent
processes each message, acknowledges it, and starts one replacement listener.

Codex keeps its parent turn active on the collaboration wait and uses a cheaper
listener model when available. A user prompt can steer that running parent,
which then continues waiting for the same child. OpenCode uses
`task(background: true)` and its current model; the creation-time kit ensures
the task form is present, and completion starts the parent handling turn.

This path needs no Codex App Server, OpenCode HTTP API, Docker socket,
terminal-multiplexer socket, or agent-control socket in the sandbox. Direct
client-control receivers remain optional P2 integrations.

## Optional token authentication

Run `agent-relay --authentication-mode token` when sandboxes must prove their
relay identities. That mode requires an administrator token, one issued bearer
credential per agent, and corresponding MCP authorization headers. Follow the
[authenticated flow recap](protocol.md#authenticated-flow-recap); do not put
the administrator token inside a sandbox.
