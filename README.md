# Agent Relay

Run this service when coding-agent sessions need a durable, two-way mailbox on
the same host or across a Docker Sandbox boundary. Each session receives a relay
identity and communicates through HTTP or the Model Context Protocol (MCP).
Token mode uses bearer credentials; trusted development setups may use
self-registered slugs. Server-Sent Events (SSE) provide immediate notification
without polling.

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
export AGENT_RELAY_ADMIN_TOKEN="$(openssl rand -hex 32)"
.venv/bin/agent-relay
```

Bearer-token authentication is the default. For a trusted development host
where every process allowed to reach the relay may act as any session, run
without authentication instead:

```bash
.venv/bin/agent-relay --authentication-mode none
```

In this mode, agents call the MCP `register_session` tool with a human-readable
slug and pass the same value as `acting_slug` on later tools. Senders address
other agents by `recipient_slug`; no administrator or session token is required.

The default endpoint is `http://127.0.0.1:8787`, the MCP endpoint is
`http://127.0.0.1:8787/mcp`, and the database follows the XDG state-directory
convention. Override these with `AGENT_RELAY_HOST`, `AGENT_RELAY_PORT`,
`AGENT_RELAY_DB_PATH`, `AGENT_RELAY_HEARTBEAT_SECONDS`, and
`AGENT_RELAY_AUTHENTICATION_MODE`, or the corresponding command-line options
shown by `agent-relay --help`.

Use [the direct-session guide](docs/direct-sessions.md) when the relay and
agents share a host. Use [the Docker Sandbox guide](docs/docker-sandbox.md) to
create session credentials, configure Codex, Claude Code, or OpenCode, allow
web access, and launch an isolated agent in automatic mode. See [the protocol
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
- The admin token can create and revoke sessions. Keep it on the host. Each
  agent process receives only its own session token.
- `--authentication-mode none` deliberately removes that boundary. Any process
  that can reach the relay can register a session, inspect any inbox, or act as
  any known slug. Keep the relay on trusted loopback and allow Docker
  access only to trusted sandboxes.
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
