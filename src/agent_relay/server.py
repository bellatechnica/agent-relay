"""HTTP, Server-Sent Events, and Model Context Protocol transports."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.subscriptions import ListenHandler
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp_types.jsonrpc import INVALID_REQUEST
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from sse_starlette import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message as ASGIMessage, Receive, Scope, Send

from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    RelayError,
    RelayUnavailableError,
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


def _is_jsonrpc_notification(body: bytes) -> bool:
    """Report whether a request body is one well-formed JSON-RPC notification."""
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(decoded, dict)
        and decoded.get("jsonrpc") == "2.0"
        and isinstance(decoded.get("method"), str)
        and "id" not in decoded
    )


async def _read_body_for_inspection(
    receive: Receive, limit: int
) -> tuple[bytes | None, Receive]:
    """Buffer up to `limit` body bytes and return a receive that replays them.

    The body is `None` when the request is larger than the limit or is not an
    ordinary request body; the returned receive still replays every message
    already taken, so the wrapped application sees the request unchanged.
    """
    taken: list[ASGIMessage] = []
    body = bytearray()
    complete = True
    while True:
        message = await receive()
        taken.append(message)
        if message["type"] != "http.request":
            complete = False
            break
        body.extend(message.get("body", b""))
        if len(body) > limit:
            complete = False
            break
        if not message.get("more_body", False):
            break

    replayed = iter(taken)

    async def replay() -> ASGIMessage:
        message = next(replayed, None)
        return await receive() if message is None else message

    return (bytes(body) if complete else None), replay


def _is_invalid_request_rejection(status: int, body: bytes) -> bool:
    """Report whether a response is the transport's invalid-request rejection.

    The status alone does not identify it. The transport also answers 400 with
    a plain-text body when the `Content-Type` header is unusable, which it
    decides before reading any body, so the JSON-RPC error code is what
    separates the two.
    """
    if status != 400:
        return False
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        return False
    return (
        isinstance(decoded, dict)
        and isinstance(decoded.get("error"), dict)
        and decoded["error"].get("code") == INVALID_REQUEST
    )


def _accept_invalid_request_rejection(send: Send) -> Send:
    """Replace the invalid-request rejection with an empty 202, forwarding all else.

    The response is held only until its status and body are both known, which
    for a rejection is one small message. Every other response, including a
    streamed one, is forwarded as it arrives.
    """
    state: dict[str, Any] = {"held": None, "body": bytearray()}

    async def forward(message: ASGIMessage) -> None:
        held = state["held"]
        if message["type"] == "http.response.start":
            if message["status"] == 400:
                state["held"] = message
                return
            await send(message)
            return
        if message["type"] != "http.response.body" or held is None:
            await send(message)
            return
        state["body"].extend(message.get("body", b""))
        if message.get("more_body", False):
            return
        body = bytes(state["body"])
        state["held"] = None
        if _is_invalid_request_rejection(held["status"], body):
            await send(
                {
                    "type": "http.response.start",
                    "status": 202,
                    "headers": [(b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await send(held)
        await send({"type": "http.response.body", "body": body})

    return forward


class AcceptModernNotifications:
    """Answer 202 to a JSON-RPC notification the modern MCP entry would reject.

    MCP revision 2026-07-28 carries no session identifier and expresses
    cancellation as the close of the cancelled call's own HTTP stream, so the
    SDK's per-request entry rejects every notification body with HTTP 400.
    The Streamable HTTP transport also lets a server accept a notification POST
    with 202, and a client may send one after closing the stream: Antigravity
    CLI 1.1.25 posts `notifications/cancelled` on a fresh connection once its
    per-call deadline has already closed the wait. Accepting is the honest
    answer there — the notification carries no id to reply to, and the wait it
    names is gone by the time it arrives — where a 400 tells that client its
    cancellation failed when the relay had already performed it.

    The request is always passed to the transport, and only its invalid-request
    rejection is rewritten, so every other check keeps its own answer: an
    unusable host, origin, accept header or content type, and a body above the
    approved size, each still reach the client unchanged. For a body that
    parsed as JSON and carries no id, that rejection is the only one the
    transport can still reach — the parse error needs a body that did not
    parse, and every later rung needs a body that validated as a request, which
    a notification cannot.

    Bodies reaching the handshake-era paths are untouched, because those route
    a notification to its session's transport and answering here would swallow
    it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        # Era routing reads the first header of that name, as the SDK does;
        # a duplicate is the SDK's rejection to make, not this wrapper's.
        wanted = MCP_PROTOCOL_VERSION_HEADER.encode("ascii")
        version = next(
            (
                value.decode("latin-1")
                for key, value in scope["headers"]
                if key == wanted
            ),
            None,
        )
        if version is None or version in HANDSHAKE_PROTOCOL_VERSIONS:
            await self._app(scope, receive, send)
            return
        # Reading the body to classify it holds no more of it than the
        # transport already accepts and buffers itself; a body past that size
        # goes through unread, and the transport refuses it as it does today.
        body, replay = await _read_body_for_inspection(
            receive, MCP_MAX_REQUEST_BODY_BYTES
        )
        if body is not None and _is_jsonrpc_notification(body):
            await self._app(
                scope, replay, _accept_invalid_request_rejection(send)
            )
            return
        await self._app(scope, replay, send)


class RelayShutdown:
    """End Relay listeners and MCP transports before HTTP connection drain."""

    def __init__(
        self,
        hub: NotificationHub,
        mcp_session_manager: StreamableHTTPSessionManager,
        subscription_listener: ListenHandler,
    ) -> None:
        self._hub = hub
        self._mcp_session_manager = mcp_session_manager
        self._subscription_listener = subscription_listener
        self._requested = False

    @classmethod
    def for_mcp_server(
        cls, hub: NotificationHub, mcp: MCPServer
    ) -> RelayShutdown:
        # MCP SDK 2.0 registers the modern subscription handler internally and
        # does not expose a high-level shutdown method. The dependency is pinned.
        handler_entry = mcp._lowlevel_server._request_handlers.get(
            "subscriptions/listen"
        )
        if handler_entry is None or not isinstance(
            handler_entry.handler, ListenHandler
        ):
            raise ConfigurationError(
                "the MCP subscriptions/listen shutdown handler is unavailable"
            )
        return cls(hub, mcp.session_manager, handler_entry.handler)

    def request(self) -> None:
        if self._requested:
            return
        self._requested = True
        self._hub.request_shutdown()
        self._subscription_listener.close()

    async def finish(self) -> None:
        self.request()
        # MCP SDK 2.0 exposes per-transport termination but no manager-wide
        # pre-lifespan shutdown operation. The dependency is pinned, and this
        # snapshot is intentionally every active transport rather than one.
        active_transports = tuple(
            self._mcp_session_manager._server_instances.values()
        )
        await asyncio.gather(
            *(transport.terminate() for transport in active_transports)
        )


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


def _optional_flag(body: dict[str, Any], name: str) -> bool:
    value = body.get(name)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _message_payload(message: Message) -> dict[str, object]:
    """Return the whole stored message, for the paths that deliver one.

    Inbox reads, blocking waits and event-stream frames use this. Handing over
    the content is what those calls are for, so they carry it verbatim.
    """
    return message.as_dict()


def _sent_message_payload(
    message: Message, recipient_waiting_at_send: bool
) -> dict[str, object]:
    """Report what a send or reply established, without quoting it back.

    The caller wrote this content a moment ago, and a tool result is resent to
    the model on every later turn of its session, so echoing the body charges
    for it again on each of those turns. Internal session identifiers are left
    out too: callers address each other by slug.
    """
    return {
        "message_id": message.message_id,
        "recipient_slug": message.recipient_slug,
        "in_reply_to": message.in_reply_to,
        "sent_at": message.sent_at,
        "recipient_waiting_at_send": recipient_waiting_at_send,
    }


def _reply_payload(
    message: Message,
    recipient_waiting_at_send: bool,
    acknowledged_message_id: str | None,
) -> dict[str, object]:
    payload = _sent_message_payload(message, recipient_waiting_at_send)
    # Always present, so a caller reads the outcome rather than assuming its
    # request took effect. Null means no acknowledgement was requested.
    payload["acknowledged_message_id"] = acknowledged_message_id
    return payload


def _acknowledged_payload(message: Message) -> dict[str, object]:
    """Report which message an acknowledgement settled, and when.

    The acknowledging caller has just finished processing this message, so its
    body is the one thing it does not need returned.
    """
    return {
        "message_id": message.message_id,
        "acknowledged_at": message.acknowledged_at,
    }


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
            if hub.is_shutting_down:
                raise RelayUnavailableError(
                    "the relay is shutting down; reconnect and read the inbox"
                )
            wake_up.clear()
            pending = store.unacknowledged_messages(recipient_session_id)
            if pending:
                return _record_delivery_attempts(
                    store, recipient_session_id, pending
                )
            await wake_up.wait()


async def _event_stream(
    store: RelayStore, hub: NotificationHub, recipient_session_id: str
) -> AsyncIterator[dict[str, str]]:
    wake_up = await hub.event_for(recipient_session_id)
    last_emitted_sequence = 0
    while not hub.is_shutting_down:
        wake_up.clear()
        pending = store.unacknowledged_messages(recipient_session_id)
        fresh = [
            message
            for message in pending
            if message.sequence > last_emitted_sequence
        ]
        if fresh:
            store.mark_delivery_attempt(
                recipient_session_id,
                (message.message_id for message in fresh),
            )
            refreshed_by_id = {
                message.message_id: message
                for message in store.unacknowledged_messages(
                    recipient_session_id
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
    shutdown = RelayShutdown.for_mcp_server(hub, mcp)

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
        return JSONResponse(_acknowledged_payload(message))

    async def reply(request: Request) -> JSONResponse:
        sender = _session_for_request(request, store, authentication_mode)
        body = await _json_object(request)
        answered_message_id = request.path_params["message_id"]
        acknowledge = _optional_flag(body, "acknowledge")
        message = store.reply_to_message(
            sender.session_id,
            answered_message_id,
            _required_string(body, "content"),
            acknowledge=acknowledge,
        )
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return JSONResponse(
            _reply_payload(
                message,
                recipient_waiting_at_send,
                answered_message_id if acknowledge else None,
            ),
            status_code=201,
        )

    async def events(request: Request) -> EventSourceResponse:
        recipient = _session_for_request(request, store, authentication_mode)

        return EventSourceResponse(
            _event_stream(store, hub, recipient.session_id),
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
            Mount("/", app=AcceptModernNotifications(mcp_app)),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)],
        exception_handlers={RelayError: relay_error_handler},
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.hub = hub
    app.state.mcp = mcp
    app.state.shutdown = shutdown
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
            "until explicitly acknowledged. A reply can carry that "
            "acknowledgement with acknowledge=true, but only when it completes "
            "the work the answered message asked for. Acknowledge a processed "
            "batch in one turn rather than one turn per message."
        )
    else:
        instructions = (
            "Use this durable mailbox to communicate with other coding-agent "
            "sessions. The bearer token establishes your identity; omit "
            "acting_slug. Address recipients by slug. Read messages remain "
            "pending until explicitly acknowledged. A reply can carry that "
            "acknowledgement with acknowledge=true, but only when it completes "
            "the work the answered message asked for. Acknowledge a processed "
            "batch in one turn rather than one turn per message."
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
        return _acknowledged_payload(
            store.acknowledge_message(recipient.session_id, message_id)
        )

    @mcp.tool()
    async def reply_to_message(
        message_id: str,
        content: str,
        context: Context,
        acknowledge: bool = False,
        acting_slug: str | None = None,
    ) -> dict[str, object]:
        """Reply durably, acknowledging the answered message when asked to."""
        sender = acting_session(context, acting_slug)
        message = store.reply_to_message(
            sender.session_id, message_id, content, acknowledge=acknowledge
        )
        recipient_waiting_at_send = await hub.notify(
            message.recipient_session_id
        )
        return _reply_payload(
            message,
            recipient_waiting_at_send,
            message_id if acknowledge else None,
        )

    return mcp
