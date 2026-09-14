# Agent Relay

Run this service when coding-agent sessions need a durable, two-way mailbox on
the same host or across a Docker Sandbox boundary. Each session receives a relay
identity and communicates through HTTP or the Model Context Protocol (MCP).
Sessions self-register human-readable slugs by default; deployments that need
caller authentication can opt into bearer-token mode. Server-Sent Events (SSE)
and a blocking MCP inbox tool wake connected listeners without polling. Each
session keeps one such call open and carries its result back into the session.

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

This repository's default development setup uses the `agent-relay` conda
environment through direnv:

```bash
conda create --name agent-relay python=3.12
direnv allow
direnv exec . python -m pip install -e .
direnv exec . agent-relay
```

Alternatively, install into a conventional virtual environment:

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

With the conda/direnv setup:

```bash
export AGENT_RELAY_ADMIN_TOKEN="$(openssl rand -hex 32)"
direnv exec . agent-relay --authentication-mode token
```

With the virtual environment:

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
2. Call `read_inbox` once, handle every pending message in send order, and
   acknowledge each only after processing it.
3. Keep exactly one listener open: a single `wait_for_messages` call whose
   complete result reaches the session unacknowledged. Codex, OpenCode and
   Claude Code use a background subagent that returns the result verbatim; a
   Claude Code that has shown this session it detaches long-running MCP calls
   may issue the call in its own turn instead; Antigravity CLI blocks its turn
   on the call.
4. Handle the returned messages, acknowledge after processing, and start one
   replacement listener. Acknowledgements for a processed batch belong in one
   model turn rather than one turn each, because every turn resends the
   session's whole context.
5. Call `send_message` with its `acting_slug` and the recipient's exact slug;
   use `reply_to_message` when responding to a received message, passing
   `acknowledge: true` when that reply completes what the message asked for, and
   inspect the returned `recipient_waiting_at_send` observation.

The version-specific listener behavior of each client is recorded in the
[protocol reference](docs/protocol.md#what-push-means-for-an-agent). OpenCode
requires `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS=true`; Claude Code
detachment varies by release and must be observed in the session that relies on
it, because an in-turn call that is not detached blocks that session's turn for
as long as its mailbox stays quiet. The
[`agent-relay-message`](skills/agent-relay-message) skill contains the exact
client rules. The `handoff` workflow includes both slugs and starts listeners
on both sides. Agent Relay is its default durable message channel. When a send
reports that no recipient MCP wait was active, any local sender that already
knows the recipient's tmux recovery address, in a handoff or not, uses tmux only
to wake the recipient; the actionable content remains in Relay.

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
