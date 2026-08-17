"""HTTP, Server-Sent Events, and Model Context Protocol transports."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    RelayError,
    ValidationError,
)
from .hub import NotificationHub
from .models import Message, Session
from .store import RelayStore

# The MCP SDK's documented default. Requests are rejected, never truncated.
MCP_MAX_REQUEST_BODY_BYTES = 4 * 1024 * 1024
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "host.docker.internal", "testserver"]
MCP_ALLOWED_HOSTS = [
    "127.0.0.1:*",
    "localhost:*",
    "host.docker.internal:*",
    "testserver:*",
]
AUTHENTICATION_MODES = ("none", "token")


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise AuthenticationError("an Authorization: Bearer header is required")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise AuthenticationError("an Authorization: Bearer header is required")
    return token


def _session_for_request(
    request: Request, store: RelayStore, authentication_mode: str
) -> Session:
    if authentication_mode == "token":
        return store.authenticate(
            _bearer_token(request.headers.get("authorization"))
        )
    slug = request.headers.get("agent-relay-slug")
    if slug is None or slug == "":
        raise ValidationError(
            "an Agent-Relay-Slug header is required in none mode"
        )
    return store.active_session_by_slug(slug)


def _require_admin(
    request: Request, admin_token: str | None, authentication_mode: str
) -> None:
    if authentication_mode == "none":
        return
    if admin_token is None:
        raise ConfigurationError("AGENT_RELAY_ADMIN_TOKEN must not be empty")
    supplied = _bearer_token(request.headers.get("authorization"))
    if not secrets.compare_digest(supplied, admin_token):
        raise AuthenticationError("the admin bearer token is invalid")


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValidationError("the request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValidationError("the request body must be a JSON object")
    return value


def _required_string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or value == "":
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _optional_string(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} must be a string or null")
    return value


def _message_payload(message: Message) -> dict[str, object]:
    return message.as_dict()


def _sent_message_payload(
    message: Message, recipient_waiting_at_send: bool
) -> dict[str, object]:
    payload = _message_payload(message)
    payload["recipient_waiting_at_send"] = recipient_waiting_at_send
    return payload


def _record_delivery_attempts(
    store: RelayStore, recipient_session_id: str, messages: list[Message]
) -> list[Message]:
    store.mark_delivery_attempt(
        recipient_session_id, (message.message_id for message in messages)
    )
    return store.unacknowledged_messages(recipient_session_id)


async def _wait_for_messages(
    store: RelayStore, hub: NotificationHub, recipient_session_id: str
) -> list[Message]:
    async with hub.mcp_wait(recipient_session_id) as wake_up:
        while True:
            wake_up.clear()
            pending = store.unacknowledged_messages(recipient_session_id)
            if pending:
                return _record_delivery_attempts(
                    store, recipient_session_id, pending
                )
            await wake_up.wait()


def create_app(
    database_path: Path,
    admin_token: str | None,
    *,
    authentication_mode: str = "none",
    heartbeat_seconds: float = 15.0,
) -> Starlette:
    """Build one single-process relay application."""
    if authentication_mode not in AUTHENTICATION_MODES:
        raise ConfigurationError(
            "authentication_mode must be one of: "
            + ", ".join(AUTHENTICATION_MODES)
        )
    if authentication_mode == "token" and not admin_token:
        raise ConfigurationError("AGENT_RELAY_ADMIN_TOKEN must not be empty")
    if heartbeat_seconds <= 0:
        raise ConfigurationError("heartbeat_seconds must be greater than zero")

    store = RelayStore(database_path)
    hub = NotificationHub()
    mcp = _build_mcp_server(store, hub, authentication_mode)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        max_request_body_size=MCP_MAX_REQUEST_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            allowed_hosts=MCP_ALLOWED_HOSTS,
            allowed_origins=[],
        ),
    )

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def create_session(request: Request) -> JSONResponse:
        _require_admin(request, admin_token, authentication_mode)
        body = await _json_object(request)
        issued = store.issue_session(
            _required_string(body, "slug"),
            _required_string(body, "agent_kind"),
        )
        return JSONResponse(issued.as_dict(), status_code=201)

    async def revoke_session(request: Request) -> JSONResponse:
        _require_admin(request, admin_token, authentication_mode)
        store.revoke_session(request.path_params["session_id"])
        return JSONResponse({"revoked": True})

    async def sessions(request: Request) -> JSONResponse:
        if request.method == "POST":
            if authentication_mode != "none":
                raise AuthenticationError(
                    "self-registration is available only in none mode"
                )
            body = await _json_object(request)
            session = store.register_session(
                _required_string(body, "slug"),
                _required_string(body, "agent_kind"),
            )
            return JSONResponse(session.as_dict(), status_code=201)

        if authentication_mode == "token":
            _session_for_request(request, store, authentication_mode)
        return JSONResponse(
            {"sessions": [session.as_dict() for session in store.list_sessions()]}
        )

    async def whoami(request: Request) -> JSONResponse:
        return JSONResponse(
            _session_for_request(request, store, authentication_mode).as_dict()
        )

    async def send_message(request: Request) -> JSONResponse:
        sender = _session_for_request(request, store, authentication_mode)
        body = await _json_object(request)
        message = store.send_message(
            sender.session_id,
            store.active_session_by_slug(
                _required_string(body, "recipient_slug")
            ).session_id,
            _required_string(body, "content"),
            _optional_string(body, "in_reply_to"),
        )
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return JSONResponse(
            _sent_message_payload(message, recipient_waiting_at_send),
            status_code=201,
        )

    async def read_inbox(request: Request) -> JSONResponse:
        recipient = _session_for_request(request, store, authentication_mode)
        messages = store.unacknowledged_messages(recipient.session_id)
        delivered = _record_delivery_attempts(
            store, recipient.session_id, messages
        )
        return JSONResponse(
            {"messages": [_message_payload(message) for message in delivered]}
        )

    async def acknowledge(request: Request) -> JSONResponse:
        recipient = _session_for_request(request, store, authentication_mode)
        message = store.acknowledge_message(
            recipient.session_id, request.path_params["message_id"]
        )
        return JSONResponse(_message_payload(message))

    async def reply(request: Request) -> JSONResponse:
        sender = _session_for_request(request, store, authentication_mode)
        body = await _json_object(request)
        message = store.reply_to_message(
            sender.session_id,
            request.path_params["message_id"],
            _required_string(body, "content"),
        )
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return JSONResponse(
            _sent_message_payload(message, recipient_waiting_at_send),
            status_code=201,
        )

    async def events(request: Request) -> EventSourceResponse:
        recipient = _session_for_request(request, store, authentication_mode)

        async def event_stream() -> AsyncIterator[dict[str, str]]:
            wake_up = await hub.event_for(recipient.session_id)
            last_emitted_sequence = 0
            while True:
                wake_up.clear()
                pending = store.unacknowledged_messages(recipient.session_id)
                fresh = [
                    message
                    for message in pending
                    if message.sequence > last_emitted_sequence
                ]
                if fresh:
                    store.mark_delivery_attempt(
                        recipient.session_id,
                        (message.message_id for message in fresh),
                    )
                    refreshed_by_id = {
                        message.message_id: message
                        for message in store.unacknowledged_messages(
                            recipient.session_id
                        )
                    }
                    for pending_message in fresh:
                        message = refreshed_by_id.get(pending_message.message_id)
                        if message is None:
                            continue
                        last_emitted_sequence = message.sequence
                        yield {
                            "event": "message",
                            "id": str(message.sequence),
                            "data": json.dumps(_message_payload(message)),
                        }
                    continue
                await wake_up.wait()

        return EventSourceResponse(
            event_stream(),
            ping=heartbeat_seconds,
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @asynccontextmanager
    async def lifespan(_: Starlette):
        store.initialize()
        async with mcp.session_manager.run():
            yield

    async def relay_error_handler(_: Request, error: RelayError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": error.code, "message": str(error)}},
            status_code=error.status_code,
        )

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/v1/admin/sessions", create_session, methods=["POST"]),
            Route(
                "/v1/admin/sessions/{session_id:str}/revoke",
                revoke_session,
                methods=["POST"],
            ),
            Route("/v1/whoami", whoami, methods=["GET"]),
            Route("/v1/sessions", sessions, methods=["GET", "POST"]),
            Route("/v1/messages", send_message, methods=["POST"]),
            Route("/v1/messages", read_inbox, methods=["GET"]),
            Route("/v1/events", events, methods=["GET"]),
            Route(
                "/v1/messages/{message_id:str}/ack",
                acknowledge,
                methods=["POST"],
            ),
            Route(
                "/v1/messages/{message_id:str}/reply",
                reply,
                methods=["POST"],
            ),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)],
        exception_handlers={RelayError: relay_error_handler},
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.mcp = mcp
    app.state.authentication_mode = authentication_mode
    return app


def _build_mcp_server(
    store: RelayStore, hub: NotificationHub, authentication_mode: str
) -> MCPServer:
    if authentication_mode == "none":
        instructions = (
            "Use this durable mailbox to communicate with other coding-agent "
            "sessions by slug. First call register_session with your assigned "
            "slug, then pass that same slug as acting_slug on later tools. Send "
            "to another agent with recipient_slug. Read messages remain pending "
            "until explicitly acknowledged."
        )
    else:
        instructions = (
            "Use this durable mailbox to communicate with other coding-agent "
            "sessions. The bearer token establishes your identity; omit "
            "acting_slug. Address recipients by slug. Read messages remain "
            "pending until explicitly acknowledged."
        )
    mcp = MCPServer(
        "agent-relay",
        instructions=instructions,
    )

    def acting_session(context: Context, acting_slug: str | None) -> Session:
        if authentication_mode == "none":
            if acting_slug is None or acting_slug == "":
                raise ValidationError("acting_slug is required in none mode")
            return store.active_session_by_slug(acting_slug)

        authenticated = store.authenticate(
            _bearer_token(context.headers.get("authorization"))
        )
        if acting_slug is not None and acting_slug != authenticated.slug:
            raise AuthorizationError(
                "acting_slug does not match the authenticated session"
            )
        return authenticated

    if authentication_mode == "none":

        @mcp.tool()
        def register_session(slug: str, agent_kind: str) -> dict[str, object]:
            """Register or recover this agent's exact slug for later relay calls."""
            return store.register_session(slug, agent_kind).as_dict()

    @mcp.tool()
    def whoami(
        context: Context, acting_slug: str | None = None
    ) -> dict[str, object]:
        """Return the acting relay identity."""
        return acting_session(context, acting_slug).as_dict()

    @mcp.tool()
    def list_sessions(context: Context) -> dict[str, object]:
        """List every active agent slug available for exact routing."""
        if authentication_mode == "token":
            acting_session(context, None)
        return {"sessions": [session.as_dict() for session in store.list_sessions()]}

    @mcp.tool()
    async def send_message(
        recipient_slug: str,
        content: str,
        context: Context,
        in_reply_to: str | None = None,
        acting_slug: str | None = None,
    ) -> dict[str, object]:
        """Send durably and report whether the recipient had an active MCP wait."""
        sender = acting_session(context, acting_slug)
        recipient = store.active_session_by_slug(recipient_slug)
        message = store.send_message(
            sender.session_id,
            recipient.session_id,
            content,
            in_reply_to=in_reply_to,
        )
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return _sent_message_payload(message, recipient_waiting_at_send)

    @mcp.tool()
    def read_inbox(
        context: Context, acting_slug: str | None = None
    ) -> dict[str, object]:
        """Return every unacknowledged message in send order."""
        recipient = acting_session(context, acting_slug)
        messages = store.unacknowledged_messages(recipient.session_id)
        delivered = _record_delivery_attempts(
            store, recipient.session_id, messages
        )
        return {"messages": [_message_payload(message) for message in delivered]}

    @mcp.tool()
    async def wait_for_messages(
        context: Context, acting_slug: str | None = None
    ) -> dict[str, object]:
        """Wait without polling, then return every unacknowledged message."""
        recipient = acting_session(context, acting_slug)
        delivered = await _wait_for_messages(store, hub, recipient.session_id)
        return {"messages": [_message_payload(message) for message in delivered]}

    @mcp.tool()
    def acknowledge_message(
        message_id: str,
        context: Context,
        acting_slug: str | None = None,
    ) -> dict[str, object]:
        """Confirm that this session processed a received message."""
        recipient = acting_session(context, acting_slug)
        return _message_payload(
            store.acknowledge_message(recipient.session_id, message_id)
        )

    @mcp.tool()
    async def reply_to_message(
        message_id: str,
        content: str,
        context: Context,
        acting_slug: str | None = None,
    ) -> dict[str, object]:
        """Reply durably and report whether the recipient had an active MCP wait."""
        sender = acting_session(context, acting_slug)
        message = store.reply_to_message(sender.session_id, message_id, content)
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return _sent_message_payload(message, recipient_waiting_at_send)

    return mcp
