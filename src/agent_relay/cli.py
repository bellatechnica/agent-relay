"""Command-line entry point for the relay server."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from .server import AUTHENTICATION_MODES, create_app


def _default_database_path() -> Path:
    configured = os.environ.get("AGENT_RELAY_DB_PATH")
    if configured:
        return Path(configured).expanduser()
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
    )
    return state_home / "agent-relay" / "relay.sqlite3"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the durable coding-agent message relay."
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=_default_database_path(),
        help="SQLite database path (default: AGENT_RELAY_DB_PATH or XDG state)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("AGENT_RELAY_HOST", "127.0.0.1"),
        help="listen address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("AGENT_RELAY_PORT", "8787"),
        help="listen port (default: 8787)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=os.environ.get("AGENT_RELAY_HEARTBEAT_SECONDS", "15"),
        help="SSE heartbeat interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--authentication-mode",
        choices=AUTHENTICATION_MODES,
        default=os.environ.get("AGENT_RELAY_AUTHENTICATION_MODE", "token"),
        help=(
            "caller identity mode: token requires bearer credentials; "
            "none trusts explicit slugs (default: token)"
        ),
    )
    return parser


def main() -> None:
    parser = _parser()
    arguments = parser.parse_args()
    admin_token = os.environ.get("AGENT_RELAY_ADMIN_TOKEN")
    if arguments.authentication_mode == "token" and not admin_token:
        parser.error("AGENT_RELAY_ADMIN_TOKEN is required")
    app = create_app(
        arguments.database_path,
        admin_token,
        authentication_mode=arguments.authentication_mode,
        heartbeat_seconds=arguments.heartbeat_seconds,
    )
    uvicorn.run(app, host=arguments.host, port=arguments.port)
