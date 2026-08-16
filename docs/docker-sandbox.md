# Docker Sandbox setup

Use one relay identity per agent session. The host relay remains bound to
`127.0.0.1`; Docker Sandbox reaches it through `host.docker.internal`. Docker's
proxy rewrites that host name to host localhost, so the policy rule must allow
`localhost:8787`, not `host.docker.internal:8787`. This behavior is documented in
[Docker's host-service workflow](https://docs.docker.com/ai/sandboxes/workflows/#accessing-host-services-from-a-sandbox).

The commands below assume the relay is installed and running as described in the
[README](../README.md), and that `curl`, `jq`, `openssl`, and `sbx` are available
on the host.

For trusted sandboxes, the relay can instead run with
`--authentication-mode none`. In that mode, skip token issuance and injection,
remove bearer-token settings from the MCP client configuration, and have each
agent call `register_session` with its assigned slug before other relay tools.
The agent passes that slug as `acting_slug` and addresses recipients by
`recipient_slug`. Every sandbox allowed to reach port 8787 can then impersonate
or read the messages of every session.

## 1. Choose the sandbox network policy

For a new local installation, initialize Docker Sandbox with its Balanced
policy. It denies destinations by default while allowing common model-provider,
package-manager, source-hosting, registry, and cloud-service endpoints:

```bash
sbx policy init balanced
```

See [Docker's local-policy reference](https://docs.docker.com/ai/sandboxes/governance/local/)
before replacing an existing policy. Organization governance can override local
rules.

Web search and page retrieval still need the destinations used by the selected
agent or search provider. Exercise the search once, inspect denied requests with
`sbx policy log SANDBOX_NAME`, and allow the specific required domains with:

```bash
sbx policy allow network --sandbox SANDBOX_NAME "search-provider.example:443"
```

Avoid `sbx policy allow network "**"` if network isolation matters. Docker's
[network troubleshooting guide](https://docs.docker.com/ai/sandboxes/troubleshooting/#agent-cant-install-packages-or-reach-an-api)
explains how to identify blocked destinations. For an explicit search tool
instead of an agent's native web search, Docker's MCP Catalog includes a
[containerized DuckDuckGo server](https://docs.docker.com/ai/docker-agent/tools/mcp/#docker-mcp-recommended).

## 2. Create the sandbox without attaching

Clone mode keeps the agent's Git writes in an isolated clone until the host
fetches them. Replace `codex` with `claude` or `opencode` when needed.

```bash
sbx create --clone --name relay-codex codex /path/to/project
sbx policy allow network --sandbox relay-codex localhost:8787
```

Docker documents the isolation difference between direct and clone mode in its
[sandbox usage guide](https://docs.docker.com/ai/sandboxes/usage/#clone-mode).

Verify the host route before configuring an agent:

```bash
sbx exec relay-codex curl --fail http://host.docker.internal:8787/health
```

The expected response is exactly `{"status":"ok"}`.

## 3. Issue relay identities

Create one identity for the sandbox and another for the outside session. The
admin token stays on the host.

```bash
SANDBOX_ISSUED="$(curl --fail-with-body -sS \
  -H "Authorization: Bearer $AGENT_RELAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"relay-codex","agent_kind":"codex"}' \
  http://127.0.0.1:8787/v1/admin/sessions)"

OUTSIDE_ISSUED="$(curl --fail-with-body -sS \
  -H "Authorization: Bearer $AGENT_RELAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"host-coordinator","agent_kind":"codex"}' \
  http://127.0.0.1:8787/v1/admin/sessions)"

SANDBOX_RELAY_TOKEN="$(jq -r .token <<<"$SANDBOX_ISSUED")"
SANDBOX_SLUG="$(jq -r .session.slug <<<"$SANDBOX_ISSUED")"
OUTSIDE_RELAY_TOKEN="$(jq -r .token <<<"$OUTSIDE_ISSUED")"
OUTSIDE_SLUG="$(jq -r .session.slug <<<"$OUTSIDE_ISSUED")"
```

Tokens are returned only by the issue response. Keep the host token in
machine-local configuration, not in the repository.

Docker Sandbox does not forward arbitrary host environment variables. Store the
sandbox's relay token in its persistent environment file; unlike model-provider
credentials handled by Docker's credential proxy, this token is readable by the
sandbox agent:

```bash
sbx exec -e AGENT_RELAY_TOKEN="$SANDBOX_RELAY_TOKEN" relay-codex bash -c '
  umask 077
  printf "export AGENT_RELAY_TOKEN=%q\n" "$AGENT_RELAY_TOKEN" >> /etc/sandbox-persistent.sh
'
```

Run that command once for a newly created sandbox. The storage and exposure
tradeoff is described in [Docker's custom-environment-variable FAQ](https://docs.docker.com/ai/sandboxes/faq/#how-do-i-set-custom-environment-variables-inside-a-sandbox).

## 4. Configure the relay MCP server

All three clients use Streamable HTTP at
`http://host.docker.internal:8787/mcp`. Their checked-in example files keep the
token in `AGENT_RELAY_TOKEN`; they contain no credential.

### Codex

Merge [the Codex example](../examples/codex-config.toml) into the sandbox's
`~/.codex/config.toml`. The top-level web-search setting requests live retrieval,
and Codex reads the relay bearer token from the named environment variable.

```toml
web_search = "live"

[mcp_servers.agent_relay]
url = "http://host.docker.internal:8787/mcp"
bearer_token_env_var = "AGENT_RELAY_TOKEN"
required = true
```

The values and project/user config locations are defined in the official
[OpenAI Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

Run this inside the sandbox. The single quotes preserve the environment-variable
reference for expansion by Claude Code rather than the shell:

```bash
claude mcp add-json --scope user agent-relay \
  '{"type":"http","url":"http://host.docker.internal:8787/mcp","headers":{"Authorization":"Bearer ${AGENT_RELAY_TOKEN}"}}'
```

Claude Code documents runtime expansion in HTTP URLs and headers in its
[MCP configuration guide](https://code.claude.com/docs/en/mcp#environment-variable-expansion-in-mcpjson).

### OpenCode

Merge [the OpenCode example](../examples/opencode.json) into the sandbox's
`~/.config/opencode/opencode.json`. `{env:AGENT_RELAY_TOKEN}` is resolved when
OpenCode connects. The format follows OpenCode's
[remote MCP server reference](https://opencode.ai/v2/docs/mcp-servers#remote-servers).

## 5. Launch in automatic mode

Docker Sandbox starts Codex and Claude Code with their approval-bypass flags by
default. OpenCode needs its explicit auto flag:

```bash
sbx run --name relay-codex
sbx run --name relay-claude
sbx run --name relay-opencode -- --auto
```

Create and configure a distinct named sandbox before using each corresponding
command. Docker documents the default commands for
[Codex](https://docs.docker.com/ai/sandboxes/agents/codex/#default-startup-command)
and [Claude Code](https://docs.docker.com/ai/sandboxes/agents/claude-code/#default-startup-command).
Automatic mode gives the agent broad control inside its microVM and workspace;
clone mode and the network allowlist remain the containment boundaries.

In the first prompt, give each agent its assigned slug and the outside session's
slug, then require this mailbox discipline:

```text
Use agent-relay for coordination. Register as relay-codex and pass that value as
acting_slug. Read every pending inbox message before work, acknowledge it only
after processing it, and use reply_to_message for responses. The outside
session slug is host-coordinator.
```

The outside client uses the same MCP configuration with
`http://127.0.0.1:8787/mcp` and `OUTSIDE_RELAY_TOKEN` as its token.

## Push versus polling

An active agent can call `read_inbox` through MCP and respond in either direction.
For immediate notification, a receiver opens
`http://host.docker.internal:8787/v1/events`; the server wakes that SSE connection
when a message is committed and does not poll.

SSE alone does not start a new Codex, Claude Code, or OpenCode turn. Fully
unattended reactions require a client-specific adapter that converts an SSE
message into the client's supported turn-start or steering call, then
acknowledges only after the client accepts it. The relay deliberately does not
mount the host Docker socket, terminal multiplexer socket, or agent-control socket
into the sandbox.
