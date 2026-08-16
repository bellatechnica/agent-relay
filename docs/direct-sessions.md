# Direct host sessions

Use this procedure when Agent Relay and every coding-agent process run in the
same host network namespace. Each client connects to
`http://127.0.0.1:8787/mcp`; `host.docker.internal` is needed only inside a
Docker Sandbox.

Give every independently acting agent its own relay identity and token. The
token determines the sender identity, so sharing one token makes messages from
different agents indistinguishable. A resumed coding-agent conversation may
reuse its existing relay identity; a new independent conversation needs a new
one.

The commands below assume the relay is installed and running as described in
the [README](../README.md), and that `curl` and `jq` are available.

## 1. Verify the relay

```bash
curl --fail http://127.0.0.1:8787/health
```

Continue only when the response is exactly `{"status":"ok"}`. On Windows
Subsystem for Linux (WSL), use the [autostart guide](autostart-wsl.md) if the
service should survive closed terminals and Windows restarts.

### Optional mode without authentication

On a trusted host, start the relay with:

```bash
agent-relay --authentication-mode none
```

Then skip credential issuance below, omit `bearer_token_env_var` from the Codex
configuration, omit the `Authorization` header from Claude Code and OpenCode,
and launch the clients without `AGENT_RELAY_TOKEN`. Each agent must first call
`register_session(slug, agent_kind)` and pass that same slug as `acting_slug` to
identity-dependent relay tools. Agents address one another by exact
`recipient_slug`.

This mode trusts every process that can reach port 8787. Slugs identify callers
but do not authenticate them.

## 2. Issue one identity per agent

The administrator token is the value used to start the relay. Keep it in the
host's machine-local configuration. Repeat the administrative request for each
agent process; the example creates a coordinator and an implementer:

If the WSL autostart installer created the service, load its generated
machine-local environment into the registration shell first:

```bash
. "${XDG_CONFIG_HOME:-$HOME/.config}/agent-relay/service.env"
```

```bash
COORDINATOR_ISSUED="$(curl --fail-with-body -sS \
  -H "Authorization: Bearer $AGENT_RELAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"host-coordinator","agent_kind":"codex"}' \
  http://127.0.0.1:8787/v1/admin/sessions)"

IMPLEMENTER_ISSUED="$(curl --fail-with-body -sS \
  -H "Authorization: Bearer $AGENT_RELAY_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"slug":"host-implementer","agent_kind":"codex"}' \
  http://127.0.0.1:8787/v1/admin/sessions)"

COORDINATOR_RELAY_TOKEN="$(jq -r .token <<<"$COORDINATOR_ISSUED")"
COORDINATOR_SLUG="$(jq -r .session.slug <<<"$COORDINATOR_ISSUED")"
IMPLEMENTER_RELAY_TOKEN="$(jq -r .token <<<"$IMPLEMENTER_ISSUED")"
IMPLEMENTER_SLUG="$(jq -r .session.slug <<<"$IMPLEMENTER_ISSUED")"
```

The issue response is the only place the plaintext session token appears. Keep
tokens in a secret manager or a mode-`0600` machine-local file, never in the
repository. Do not export one `AGENT_RELAY_TOKEN` globally when concurrent
agents need different identities; set it separately on each launch command.

## 3. Configure a client

The configuration is reusable because it contains only the environment
variable name. The value comes from the environment of each agent process.

### Codex

Add this table to `~/.codex/config.toml`:

```toml
[mcp_servers.agent_relay]
url = "http://127.0.0.1:8787/mcp"
bearer_token_env_var = "AGENT_RELAY_TOKEN"
required = true
```

Codex supports Streamable HTTP bearer authentication and reads user MCP
configuration when a session starts. Run `codex mcp list` before launch to
confirm that `agent_relay` is configured. In a running session, `/mcp` shows
active servers. See the official [Codex MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

Add one user-scoped configuration. Single quotes prevent the shell from
expanding the token reference while writing the configuration:

```bash
claude mcp add-json --scope user agent-relay \
  '{"type":"http","url":"http://127.0.0.1:8787/mcp","headers":{"Authorization":"Bearer ${AGENT_RELAY_TOKEN}"}}'
```

Run `claude mcp list` to verify the connection, or use `/mcp` inside Claude
Code. Claude documents both HTTP MCP configuration and environment expansion in
its [MCP guide](https://code.claude.com/docs/en/mcp).

### OpenCode

Merge this entry into `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "agent-relay": {
      "type": "remote",
      "url": "http://127.0.0.1:8787/mcp",
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:AGENT_RELAY_TOKEN}"
      }
    }
  }
}
```

The format follows OpenCode's [remote MCP server reference](https://opencode.ai/docs/mcp-servers).

## 4. Launch each process with its identity

Open separate terminals or terminal-multiplexer panes. Substitute each process's
own token in its launch environment. These examples start two Codex sessions:

```bash
AGENT_RELAY_TOKEN="$COORDINATOR_RELAY_TOKEN" \
  codex --approve-for-me --search

AGENT_RELAY_TOKEN="$IMPLEMENTER_RELAY_TOKEN" \
  codex --approve-for-me --search
```

`--approve-for-me` keeps Codex's workspace sandbox and sends approval requests
through automatic review. `--search` enables live web search. Do not use
`--dangerously-bypass-approvals-and-sandbox` merely to obtain unattended host
operation: unlike a Docker Sandbox, a direct session has no outer isolation
boundary.

For Claude Code, classifier-backed auto mode is the direct-host option. It is
available only when the installed version, account, provider, and selected model
meet Claude Code's requirements:

```bash
AGENT_RELAY_TOKEN="$IMPLEMENTER_RELAY_TOKEN" \
  claude --permission-mode auto
```

See Claude Code's [permission-mode guide](https://code.claude.com/docs/en/permission-modes)
for the current eligibility and safety behavior. Avoid
`--dangerously-skip-permissions` on an unsandboxed host.

For OpenCode, auto mode approves requests that are not explicitly denied by its
permission configuration:

```bash
AGENT_RELAY_TOKEN="$IMPLEMENTER_RELAY_TOKEN" opencode --auto
```

OpenCode documents that behavior in its [permissions reference](https://opencode.ai/docs/permissions).

## 5. Establish the two-way workflow

Give each agent its assigned slug and the other participant's exact slug. A
useful first prompt is:

```text
Use agent-relay for coordination. In none mode, register your assigned slug and
pass it as acting_slug. Call whoami to verify your relay identity, then call
list_sessions and confirm the other participant's exact slug.
Read every pending inbox message before starting work and after each milestone.
Acknowledge a message only after processing it. Use reply_to_message when a
response belongs to a received message.
```

MCP makes six coordination tools available to an active agent; `none` mode also
adds the one-time `register_session` tool. MCP does not start a new turn when
another agent sends a message. An interactive agent must call `read_inbox` at
agreed checkpoints. For immediate notification or an adapter that starts turns,
use the Server-Sent Events (SSE) behavior described in the
[protocol reference](protocol.md#what-push-means-for-an-agent).

## 6. Verify two-way communication

Use fresh identities whose inboxes are initially empty:

1. In the coordinator, call `whoami`; its slug must be
   `host-coordinator`.
2. In the implementer, call `whoami`; its slug must be
   `host-implementer`.
3. Have the coordinator send one probe to `IMPLEMENTER_SLUG`.
4. Have the implementer read that probe and reply with `reply_to_message`.
5. Have the coordinator read the reply.
6. Acknowledge each processed message. Both fresh inboxes must then be empty.

Configuration listing alone is not a connectivity test. Successful `whoami`,
`send_message`, `reply_to_message`, and `acknowledge_message` calls prove MCP
initialization, authentication, routing in both directions, and durable inbox
state.
