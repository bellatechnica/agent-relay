# Autostart in WSL

Install Agent Relay as a systemd user service when the repository and Python
environment already exist in Windows Subsystem for Linux (WSL). The service
binds only to `127.0.0.1:8787` and stores its database under the user's XDG state
directory.

Preview every machine-local target before writing it:

```bash
scripts/install-user-service
```

Install the service in the default trusted slug mode, enable user linger, and
install a Windows Startup keepalive:

```bash
scripts/install-user-service --execute
```

The installer creates `~/.config/agent-relay/service.env` with mode `0600` for
optional machine-local settings. The installed unit is
`~/.config/systemd/user/agent-relay.service`.

To opt into token authentication, add both settings below to that environment
file, substitute a newly generated administrator token, and restart the
service:

```bash
AGENT_RELAY_AUTHENTICATION_MODE=token
AGENT_RELAY_ADMIN_TOKEN=GENERATED_ADMIN_TOKEN
```

Keep the administrator token only in machine-local configuration. An ordinary
installation does not generate or require one.

## Activate systemd after changing WSL configuration

`/etc/wsl.conf` must contain:

```ini
[boot]
systemd=true
```

When that setting was added after the current WSL VM started, run these commands
from Windows PowerShell. The first command terminates every process and terminal
in all WSL distributions, so save work first.

```powershell
wsl.exe --shutdown
wscript.exe "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\wsl-keepalive.vbs"
```

The hidden `wsl.exe --exec sleep infinity` process keeps the WSL VM alive after
the last terminal closes. The Startup copy recreates that keepalive at the next
Windows sign-in.

## Verify

After WSL restarts:

```bash
systemctl is-system-running
loginctl show-user "$USER" -p Linger
systemctl --user status agent-relay.service
curl --fail http://127.0.0.1:8787/health
```

Expected results are a running system state, `Linger=yes`, an active service,
and exactly `{"status":"ok"}` from the health endpoint.

Manage the process only through systemd:

```bash
systemctl --user restart agent-relay.service
journalctl --user -u agent-relay.service
```

Restart the service after updating the relay source or Python environment.
