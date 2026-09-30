# Agent Relay

Direct messaging between local coding-agent sessions. Codex, Claude Code,
OpenCode, Antigravity CLI, or any other client that speaks the Model Context
Protocol (MCP) registers under a readable name (a slug) and can then send,
receive, and reply to messages addressed to other sessions by that name.
Messages are stored until the recipient acknowledges them, so nothing is lost
when a session is busy, restarts, or is not listening yet. When a recipient
is not listening and it runs in a tmux pane the sender can reach, the sender
also types a short wake-up notice into that pane; the message itself stays in
the relay.

Typical uses: handing a work item to a new parallel session and getting its
questions and completion report back, a reviewer session and an implementing
session exchanging verdicts, or agents from different vendors coordinating on
one repository.

## How it differs from other agent coordination

Observed as of September 2026.

- **Subagents** (in Claude Code, Codex, or OpenCode) belong to one parent session. They start for a single task, report only to
  that parent, end when they return, and you cannot talk to them directly.
  Relay participants are ordinary, independent sessions: each keeps its own
  lifetime and context, you can talk to any of them, and none commands another.
  A message is a request the recipient handles in its own session, from any
  vendor to any vendor.
- **[Grok Bot](https://x.ai/bot/guides/grok-bot-for-engineering)** is xAI's
  hosted assistant that launches, monitors, and sends follow-ups to Cursor
  cloud agents (on Cursor's cloud or on Cursor-registered private worker
  machines). Coordination runs from the bot to Cursor-managed agents. Agent
  Relay has no hosted component and connects sessions you already run on your
  own machine, whatever client they use.
- **[Superset](https://superset.sh)** is a workspace application for running
  many coding agents in parallel: a desktop app (macOS first) that gives each
  agent a Git worktree and terminal, with a dashboard, scheduling, and remote
  access. Per its own description, its agents do not message each other; you
  orchestrate them from the dashboard. Agent Relay is one small Python service
  with a SQLite file and adds only the messaging layer; it can run alongside a
  tool like Superset rather than replacing it.

### Possible integration with A2A and ACP

Neither protocol is implemented; these are open directions.

- **[A2A](https://a2a-protocol.org)** (Agent2Agent, Linux Foundation, which
  also absorbed IBM's earlier Agent Communication Protocol) standardizes calls
  between independently operated agent servers. The relay could publish each
  registered slug as an A2A agent, mapping an incoming A2A message onto a relay
  message, so A2A clients could reach local sessions. The mismatch to resolve is
  that A2A models server-side tasks with their own lifecycle, while a relay
  recipient is an interactive session that answers when it gets to the message.
- **[ACP](https://agentclientprotocol.com)** (Agent Client Protocol, from Zed)
  standardizes how an editor or other client starts and drives a coding agent.
  It is client-to-agent, not agent-to-agent. A relay-side ACP client could
  start agents and deliver wake-ups through ACP instead of tmux, for agents
  that support it.

## Architecture

The relay is one HTTP service on the host, reachable from sessions on the same
host or inside a Docker Sandbox. Each session receives a relay
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

Every send reports whether the recipient had a listener open at that moment.
When it did not, a sender that knows the recipient's tmux pane and can reach
that tmux server types a wake notice carrying only the message ID. Across a
Docker Sandbox boundary, or without tmux, the message stays queued until the
recipient next reads its inbox.

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

Configure `http://127.0.0.1:8787/mcp` as a remote MCP server named
`agent_relay` in each client, give each session a distinct slug, and tell it
the other participant's exact slug.

- The [`agent-relay-message` skill](skills/agent-relay-message/SKILL.md) holds
  the session-side rules: registering, reading and acknowledging the inbox,
  keeping one listener open per client, replying, and sending the tmux wake-up.
  Install it for skill-aware clients; for clients without it, the MCP tool
  descriptions carry the essentials. The tmux wake-up also needs the
  `tmux-message` skill, and the handoff workflow is the `handoff` skill; both
  are in [agent-skills](https://github.com/bellatechnica/agent-skills).
- [`examples/`](examples) holds ready MCP configurations for Claude Code,
  Codex, OpenCode, and Antigravity CLI. They address the relay as
  `host.docker.internal` for use inside a Docker Sandbox; on the host, use
  `127.0.0.1` as the [direct-session guide](docs/direct-sessions.md) shows.
- [The Docker Sandbox guide](docs/docker-sandbox.md) covers configuring and
  launching isolated agents; [the autostart guide](docs/autostart-wsl.md)
  installs the relay as a systemd user service on WSL.
- [The protocol reference](docs/protocol.md) is for building a receiver or
  another client, and records each client's listener behavior;
  [the requirements](docs/requirements.md) define the slug workflow and handoff
  behavior that implementations must preserve.

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

## License

Copyright 2026 Bella Technica. Licensed under the [Apache License 2.0](LICENSE).
