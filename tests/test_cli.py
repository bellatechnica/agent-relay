import asyncio
import signal

import pytest
import uvicorn

from agent_relay import cli


class SimpleShutdown:
    def __init__(self, calls):
        self._calls = calls

    def request(self):
        self._calls.append("relay request")

    async def finish(self):
        self.request()
        self._calls.append("relay finish")


def test_default_authentication_mode_does_not_require_admin_token(
    monkeypatch, tmp_path
):
    observed = {}
    monkeypatch.delenv("AGENT_RELAY_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-relay",
            "--database-path",
            str(tmp_path / "relay.sqlite3"),
        ],
    )

    class ObservedServer:
        def __init__(self, config, relay_shutdown):
            observed.update(
                config=config,
                relay_shutdown=relay_shutdown,
            )

        def run(self):
            observed["ran"] = True

    monkeypatch.setattr(cli, "RelayServer", ObservedServer)

    cli.main()

    assert observed["config"].app.state.authentication_mode == "none"
    assert observed["config"].host == "127.0.0.1"
    assert observed["config"].port == 8787
    assert observed["ran"] is True


def test_relay_shutdown_is_requested_before_uvicorn_handles_signal(monkeypatch):
    calls = []
    relay_shutdown = SimpleShutdown(calls)
    config = uvicorn.Config(lambda scope, receive, send: None)
    server = cli.RelayServer(config, relay_shutdown)
    monkeypatch.setattr(
        uvicorn.Server,
        "handle_exit",
        lambda self, sig, frame: calls.append("uvicorn"),
    )

    server.handle_exit(signal.SIGTERM, None)

    assert calls == ["relay request", "uvicorn"]


def test_mcp_transports_finish_before_uvicorn_drains_connections(monkeypatch):
    calls = []
    relay_shutdown = SimpleShutdown(calls)
    config = uvicorn.Config(lambda scope, receive, send: None)
    server = cli.RelayServer(config, relay_shutdown)
    server.servers = []

    async def observe_shutdown(self, sockets=None):
        calls.append("uvicorn drain")

    monkeypatch.setattr(uvicorn.Server, "shutdown", observe_shutdown)

    asyncio.run(server.shutdown())

    assert calls == ["relay request", "relay finish", "uvicorn drain"]


def test_token_authentication_mode_requires_admin_token(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_RELAY_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "agent-relay",
            "--authentication-mode",
            "token",
            "--database-path",
            str(tmp_path / "relay.sqlite3"),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 2
