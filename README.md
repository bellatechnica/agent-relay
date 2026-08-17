# Agent Relay

Run this service when coding-agent sessions need a durable, two-way mailbox on
the same host or across a Docker Sandbox boundary. Each session receives a relay
identity and communicates through HTTP or the Model Context Protocol (MCP).
Sessions self-register human-readable slugs by default; deployments that need
caller authentication can opt into bearer-token mode. Server-Sent Events (SSE)
wake connected receivers without polling, but do not start a new turn in an
idle coding-agent client.

```text
agent A ── HTTP/MCP ──> host relay <── HTTP/MCP ── agent B
                            │                      (host or sandbox)
                         SQLite
```

The relay stores complete message bodies in SQLite. Reading a message records a
delivery attempt; only an explicit acknowledgement removes it from subsequent
inbox reads. A server restart therefore does not lose pending messages.

## Install and run

Python 3.12 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/agent-relay
```

The default is trusted slug mode. Agents call the MCP `register_session` tool
with a human-readable slug and pass that value as `acting_slug` on later tools.
Senders address other agents by `recipient_slug`; no administrator or session
token is required.

Token authentication is an explicit opt-in:

```bash
export AGENT_RELAY_ADMIN_TOKEN="$(openssl rand -hex 32)"
.venv/bin/agent-relay --authentication-mode token
```

The default endpoint is `http://127.0.0.1:8787`, the MCP endpoint is
`http://127.0.0.1:8787/mcp`, and the database follows the XDG state-directory
convention. Override these with `AGENT_RELAY_HOST`, `AGENT_RELAY_PORT`,
`AGENT_RELAY_DB_PATH`, `AGENT_RELAY_HEARTBEAT_SECONDS`, and
`AGENT_RELAY_AUTHENTICATION_MODE`, or the corresponding command-line options
shown by `agent-relay --help`.

## Use from an agent session

Configure `http://127.0.0.1:8787/mcp` once as the remote MCP server named
`agent_relay`. Give each session a distinct slug and the other participant's
exact slug, then instruct it to:

1. Call `register_session(slug, agent_kind)` at startup or resume.
2. Call `send_message` with its `acting_slug` and the recipient's exact slug.
3. Call `read_inbox` at prompt start and meaningful work checkpoints.
4. Use `reply_to_message` for a response, then acknowledge the received message
   only after processing it.

A queued relay message does not wake an idle Codex, Claude Code, or OpenCode
session. A client-specific receiver is required for automatic turn injection.
The `handoff` workflow therefore includes both slugs and inbox checkpoints in
the child prompt. Agent Relay is its default communication channel; tmux
messaging is used only when the user explicitly requests tmux mode.

Use [the direct-session guide](docs/direct-sessions.md) when the relay and
agents share a host. Use [the Docker Sandbox guide](docs/docker-sandbox.md) to
configure Codex, Claude Code, or OpenCode, allow web access, and launch an
isolated agent in automatic mode. See [the protocol
reference](docs/protocol.md) when building a receiver or another client. On
WSL, follow the [autostart guide](docs/autostart-wsl.md) to install a systemd
user service and keep the VM running after terminals close.

The [requirements](docs/requirements.md) define the slug workflow and handoff
behavior that implementations must preserve.

The repository includes an [`agent-relay-message`](skills/agent-relay-message)
skill for skill-aware clients. It adds registration, acknowledgement, reply,
and failure-handling discipline; the MCP tool descriptions remain sufficient
for Claude Code or OpenCode installations that do not load this skill.

## Security boundary

- The server binds to host loopback by default. Do not expose it to a LAN or the
  internet without adding TLS and a network-level access control.
- Default slug mode has no authentication boundary. Any process that can reach
  the relay can register a session, inspect any inbox, or act as any known slug.
  Keep the relay on trusted loopback and allow Docker access only to trusted
  sandboxes.
- In explicit token mode, keep the administrator token on the host and give
  each agent only its own session token.
- Session tokens are stored as SHA-256 hashes and are returned in plaintext only
  when issued.
- MCP request bodies larger than 4 MiB receive HTTP 413. The server never
  truncates or partially stores them. Split an oversized logical payload into
  explicitly labelled messages, or use the HTTP API, whose message body is not
  capped by the relay.
- Run one server process. SQLite is durable across processes, but live SSE
  notifications use an in-process wake-up hub.

## Test

```bash
pytest
```
