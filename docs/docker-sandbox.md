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

For a new local installation where network isolation is part of the security
boundary, initialize Docker Sandbox with its Balanced policy. It denies
destinations by default while allowing common model-provider, package-manager,
source-hosting, registry, and cloud-service endpoints:

```bash
sbx policy init balanced
```

See Docker's
[local-policy reference](https://docs.docker.com/ai/sandboxes/governance/local/)
before replacing an existing policy. Organization governance can override local
rules.

Balanced does not promise unrestricted web browsing. Web search and page
retrieval can reach destinations outside its built-in set, including arbitrary
result pages. Either exercise the browsing workflow, inspect denied requests
with `sbx policy log SANDBOX_NAME`, and allow each required domain explicitly:

```bash
sbx policy allow network --sandbox SANDBOX_NAME "search-provider.example:443"
```

or initialize the global Open policy when the agent must browse arbitrary web
destinations:

```bash
sbx policy init allow-all
```

Open removes network allowlisting as a containment boundary. Avoid it when
network isolation matters. Docker's
[network troubleshooting guide](https://docs.docker.com/ai/sandboxes/troubleshooting/#agent-cant-install-packages-or-reach-an-api)
explains how to identify blocked destinations.

## 2. Stage a curated agent skill root

Use a read-only additional workspace when a sandbox should receive selected
skills instead of every skill installed on the host. Shape the mounted root the
way Claude Code discovers additional-directory skills:

```text
/path/to/sandbox-skill-root/
├── agent-relay.mcp.json
└── .claude/
    └── skills/
        └── agent-relay-message/
            ├── SKILL.md
            └── agents/
                └── openai.yaml
```

Copy each selected skill from its authoritative repository into this root.
Refresh the copy after the authoritative skill changes; the mount is not an
import or synchronization mechanism.

Docker Sandbox mounts an additional workspace at its host-derived path and
does not accept a separate container destination. Pass the mounted root to
Claude Code with `--add-dir`; Claude discovers `.claude/skills/` beneath that
root and watches an existing skill directory for changes. Append `:ro` to the
host workspace argument so the sandbox cannot modify the curated copies.

Create the sandbox with `--no-share-skills` when the curated root must be its
only non-project skill source. That flag is fixed at sandbox creation. Docker's
default shared skill store is read-write and global to participating
sandboxes, so leaving it enabled creates a wider trust boundary than a curated
read-only root. Docker documents the shared store and its trust boundary in
[Share agent skills](https://docs.docker.com/ai/sandboxes/workflows/#share-agent-skills).

On Windows, use the in-sandbox path printed while Docker resolves the
workspace. For example, a host path on `D:` is normally visible below `/d/`
inside the sandbox; a WSL path such as `/mnt/d/...` is not the path Claude sees.

A machine-local launcher may keep model profiles and the curated sandbox root
under one private directory:

```text
~/.agents/
├── claude-sbx
├── codex-sbx
├── models/
│   ├── CLAUDE_PROFILE/
│   │   └── settings.json
│   └── CODEX_PROFILE/
│       └── config.toml
└── sbx/
    ├── agent-relay.mcp.json
    └── .claude/skills/
```

Put symlinks to the launchers in a directory on `PATH`; keep the launchers
themselves beside the machine-local profiles they manage. The Codex launcher
copies its read-only host profile into the sandbox's writable `$CODEX_HOME` on
first use, then preserves that local copy so Codex can record project trust and
other interactive settings. It links selected skills from the mounted
`.claude/skills/` directory into `$CODEX_HOME/skills/`. One curated skill copy
therefore serves both clients; `.claude` is only the host-side discovery layout
required by Claude Code.

## 3. Create the sandbox without attaching

Clone mode keeps the agent's Git writes in an isolated clone until the host
fetches them. Replace `codex` with `claude` or `opencode` when needed.

```bash
sbx create --clone --name relay-codex codex /path/to/project
sbx policy allow network --sandbox relay-codex localhost:8787
```

The profile launchers use direct mode for the primary project workspace, so
agent edits appear immediately in the host checkout. The model profile and
curated skill root are separate read-only workspaces. Use clone mode instead
when the project itself must be isolated from the host checkout.

```bash
sbx create --no-share-skills --name relay-codex \
  codex /path/to/project /path/to/agents/models/CODEX_PROFILE:ro \
  /path/to/sandbox-skill-root:ro
sbx policy allow network --sandbox relay-codex localhost:8787

sbx create --no-share-skills --name relay-claude \
  claude /path/to/project /path/to/agents/models/PROFILE:ro \
  /path/to/sandbox-skill-root:ro
sbx policy allow network --sandbox relay-claude localhost:8787
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

## 4. Configure the relay MCP server

All clients use Streamable HTTP at
`http://host.docker.internal:8787/mcp`. The checked-in examples contain no
credential or machine-specific secret.

### Codex

The machine-local launcher seeds
`$CODEX_HOME/PROFILE.config.toml` from
`~/.agents/models/PROFILE/config.toml` on first use, then supplies the
Relay settings below as command-line overrides on every run. The writable
sandbox-local profile can record project trust and other interactive changes;
later launches do not overwrite it from the host template. The profile file
therefore holds model/provider preferences without duplicating the
machine-independent Relay configuration:

```toml
web_search = "live"

[mcp_servers.agent_relay]
url = "http://host.docker.internal:8787/mcp"
required = true
tool_timeout_sec = 86400
```

The launcher leaves `$CODEX_HOME` inside the sandbox writable and does not
mount or copy the host's Codex credentials. Complete ChatGPT or API-key login
inside the first session; for device-code login, run `codex login
--device-auth` inside the sandbox. The resulting authentication cache and
conversation state survive sandbox stop/start. Removing or resetting the
sandbox deletes them. OpenAI documents the local cache, device-code flow, and
Docker-container login alternatives in its
[Codex authentication guide](https://learn.chatgpt.com/docs/auth).

The configuration locations are defined in the official
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
and [MCP guide](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

### Claude Code

Put one key-free `agent-relay.mcp.json` at the root of the curated sandbox
directory. Every model profile can use the same file because the Relay endpoint
and listener timeout do not depend on the selected model:

```json
{
  "mcpServers": {
    "agent_relay": {
      "type": "http",
      "url": "http://host.docker.internal:8787/mcp",
      "timeout": 86400000
    }
  }
}
```

Mount the profile directory and curated sandbox root read-only. Pass the
profile's `settings.json` with `--settings`, the shared Relay file with
`--mcp-config`, and the curated root with `--add-dir`. This supplies native
Relay tools to the first Claude process and survives sandbox deletion because
the configuration remains on the host. Claude accepts multiple
`--mcp-config` paths, so callers may append other MCP files.

The read-only `settings.json` is a command-line override layer, not a file that
Claude must update. On Linux, Claude stores login credentials in
`~/.claude/.credentials.json`; it uses separate writable files such as
`~/.claude.json` and project-local settings for per-project trust, MCP state,
permissions, and caches. The sandbox-home files persist with the sandbox, while
project-local files persist with the directly mounted checkout. See Claude's
[settings](https://code.claude.com/docs/en/settings) and
[authentication](https://code.claude.com/docs/en/authentication) references for
the current storage locations.

Claude Code measures the per-server `timeout` in milliseconds. The
`86400000` value gives one Relay listener call a 24-hour deadline and, on
Claude Code v2.1.203 or later, raises this server's MCP tool idle window
above the default 300 seconds.

`claude mcp add --transport http --scope user agent_relay URL` remains useful
for an interactively maintained sandbox. It writes mutable container state,
requires a new Claude process, and is deleted by `sbx rm`; the launcher workflow
does not depend on it.

Use `/mcp` to confirm that `agent_relay` is connected and exposes its native
tools. If those tools are absent, stop and repair the MCP configuration; do not
generate a Python client or call the Relay REST API with `curl` as a substitute.

See Claude Code's
[MCP configuration guide](https://code.claude.com/docs/en/mcp).

### OpenCode

Merge [the OpenCode example](../examples/opencode.json) into the sandbox's
`~/.config/opencode/opencode.json`. The format follows OpenCode's
[remote MCP server reference](https://opencode.ai/docs/mcp-servers).
The MCP entry must be named `agent_relay`. Confirm it with `opencode mcp list`;
the result must show `agent_relay` connected.

## 5. Launch in automatic mode

Docker's Codex default startup command supplies
`--dangerously-bypass-approvals-and-sandbox`, making Docker Sandbox the
execution boundary; the launcher must not append another copy because Codex
rejects that flag when repeated. The launcher additionally enables live web
search. Docker Sandbox starts Claude Code with its approval-bypass flag by
default. OpenCode needs its explicit auto flag:

```bash
codex-sbx PROFILE [CODEX_ARG ...]
sbx run --name relay-claude -- \
  --settings /path/visible/in/sandbox/models/PROFILE/settings.json \
  --mcp-config /path/visible/in/sandbox/sandbox-skill-root/agent-relay.mcp.json \
  --add-dir /path/visible/in/sandbox/sandbox-skill-root
sbx run --name relay-opencode -- --auto
```

Create and configure a distinct named sandbox before using a manual `sbx run`
command. A profile launcher performs that setup on first use and reuses the
same named sandbox thereafter.
Docker documents the default commands for
[Codex](https://docs.docker.com/ai/sandboxes/agents/codex/#default-startup-command)
and
[Claude Code](https://docs.docker.com/ai/sandboxes/agents/claude-code/#default-startup-command).
Automatic mode gives the agent broad control inside its microVM and workspace.
Clone mode remains a filesystem containment boundary. The network allowlist is
also a boundary under Balanced or a restrictive custom policy, but not under
Open.

For `codex-sbx`, the first argument selects both the settings profile and the
named sandbox. Every remaining argument is forwarded to Codex in its original
order. Launching the same repository/profile pair again starts another Codex
process in the existing sandbox; it does not create another sandbox. Give each
process its own tmux window name, Codex session name, Relay slug, and editing
worktree. Read-only processes may share the mounted repo root, exactly as for
direct Codex sessions.

### Preserve OSC 52 clipboard forwarding through tmux

When `sbx` runs inside tmux, applications in an attached sandbox shell can set
the outer terminal clipboard through OSC 52 only if tmux accepts application
clipboard sequences:

```tmux
set -g set-clipboard on
```

`set-clipboard external` is insufficient for this direction: it lets tmux send
clipboard updates to the terminal but does not let an application inside a pane
set a tmux buffer. Reload the option in the current server after changing the
configuration, and verify that `tmux info` reports an `Ms` terminal capability.

Test the path from a directly attached sandbox shell, such as `sbx exec -it
SANDBOX bash`. Output from a Claude Code Bash tool is captured by Claude before
it is rendered and is not a transparent test of terminal control-sequence
forwarding.

If a named sandbox repeatedly fails to restart with an ext4 `/dev/vdb`
input/output error or a read-only sandbox filesystem, the failure is in Docker
Sandbox's private runtime storage rather than the agent command. Run
`sbx diagnose`, then `sbx daemon restart` and retry. Docker's documented next
recovery step, `sbx reset`, deletes every sandbox and shared skill state; do not
use it until the state that must survive has been identified and preserved.
See Docker's
[sandbox troubleshooting guide](https://docs.docker.com/ai/sandboxes/troubleshooting/#restart-the-sandbox-daemon).

Give the sandbox agent its assigned slug and the outside session's exact slug:

```text
Use agent-relay for coordination. Register as relay-codex with agent kind codex
and pass relay-codex as acting_slug. The outside session is host-coordinator.
Use the agent-relay-message skill. Read every pending inbox message, then keep
exactly one background listener blocked in wait_for_messages. Acknowledge a
message only after processing it, replace the listener after handling its
complete result, and use reply_to_message for responses. Inspect
recipient_waiting_at_send after every send or reply; false means the message is
durable but no recipient MCP wait was observed.
```

The outside client uses the same configuration with
`http://127.0.0.1:8787/mcp` and its own slug.

## Background delivery

The listener subagent calls MCP `wait_for_messages`; the server holds that call
without polling SQLite and returns every pending message when a send commits.
The child returns the complete result without acknowledging it. The parent
processes each message, acknowledges it, and starts one replacement listener.

Codex uses `tool_timeout_sec = 86400` for this MCP server. The client cancels an
unchanged listener after 24 hours and starts one replacement; the relay server
does not time out the wait. Messages committed during the replacement gap stay
pending and return when the new wait begins. This deadline bounds the lifetime
of one open client request, listener subagent, and active parent turn.

Claude Code uses the equivalent per-server `timeout = 86400000` milliseconds.
The same replacement-listener rule applies when that deadline expires. On
Claude Code v2.1.203 and later, keep that value so the separate five-minute MCP
idle watchdog cannot abort a healthy quiet listener.

As observed on 2026-08-17, Codex 0.147.0 keeps its parent turn active on the
collaboration wait and uses a cheaper listener model when available. A user
prompt can steer that running parent, which then continues waiting for the same
child. OpenCode 1.18.18 uses `task(background: true)` and its current model; the
creation-time kit ensures the task form is present, and completion starts the
parent handling turn. The [protocol
reference](protocol.md#what-push-means-for-an-agent) records the
version-specific observation and the receiver alternatives.

This path needs no Codex App Server, OpenCode HTTP API, Docker socket,
terminal-multiplexer socket, or agent-control socket in the sandbox. Direct
client-control receivers remain optional P2 integrations.

When a send across the sandbox boundary reports
`recipient_waiting_at_send = false`, the sender reports that the message is
queued and active wake-up is unverified. It must not expose a tmux socket to the
sandbox or copy the actionable payload into another channel. A local host-to-host
handoff may instead send the conditional tmux wake notice described in the
[direct-session guide](direct-sessions.md#4-establish-the-two-way-workflow).

## Optional token authentication

Run `agent-relay --authentication-mode token` when sandboxes must prove their
relay identities. That mode requires an administrator token, one issued bearer
credential per agent, and corresponding MCP authorization headers. Follow the
[authenticated flow recap](protocol.md#authenticated-flow-recap); do not put
the administrator token inside a sandbox.
