import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types.version import (
    LATEST_HANDSHAKE_VERSION,
    LATEST_MODERN_VERSION,
)
from starlette.testclient import TestClient

from agent_relay.hub import NotificationHub
from agent_relay.server import (
    MCP_MAX_REQUEST_BODY_BYTES,
    RelayShutdown,
    _event_stream,
    create_app,
)


ADMIN_TOKEN = "test-admin-token"


def authorization(token):
    return {"Authorization": f"Bearer {token}"}


def acting_as(slug):
    return {"Agent-Relay-Slug": slug}


def call_mcp(app, name, arguments, context=None):
    result = asyncio.run(
        app.state.mcp.call_tool(name, arguments, context=context)
    )
    assert not result.is_error
    assert result.structured_content is not None
    return result.structured_content


def create_session(client, name, kind):
    response = client.post(
        "/v1/admin/sessions",
        headers=authorization(ADMIN_TOKEN),
        json={"slug": name, "agent_kind": kind},
    )
    assert response.status_code == 201
    return response.json()


def test_admin_issues_token_once_and_session_auth_works(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    with TestClient(app) as client:
        issued = create_session(client, "sandbox", "codex")
        identity = client.get(
            "/v1/whoami", headers=authorization(issued["token"])
        )
        listed = client.get(
            "/v1/sessions", headers=authorization(issued["token"])
        )

    assert identity.status_code == 200
    assert identity.json()["slug"] == "sandbox"
    assert "token" not in listed.json()["sessions"][0]


def test_invalid_credentials_return_structured_error(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    with TestClient(app) as client:
        response = client.get("/v1/whoami", headers=authorization("wrong"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"


def test_none_mode_self_registration_and_http_round_trip(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        None,
    )
    with TestClient(app) as client:
        outside = client.post(
            "/v1/sessions",
            json={"slug": "outside", "agent_kind": "codex"},
        )
        sandbox = client.post(
            "/v1/sessions",
            json={"slug": "sandbox", "agent_kind": "claude"},
        )
        recovered_outside = client.post(
            "/v1/sessions",
            json={"slug": "outside", "agent_kind": "codex"},
        )
        outside_session_id = outside.json()["session_id"]
        missing_identity = client.get("/v1/whoami")
        unknown_recipient = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "missing", "content": "must not persist"},
        )
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={
                "recipient_slug": "sandbox",
                "content": "please inspect",
            },
        ).json()
        inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]
        reply = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "inspection complete"},
        ).json()
        acknowledged = client.post(
            f"/v1/messages/{sent['message_id']}/ack",
            headers=acting_as("sandbox"),
        ).json()
        outside_inbox = client.get(
            "/v1/messages", headers=acting_as("outside")
        ).json()["messages"]
        client.post(
            f"/v1/messages/{reply['message_id']}/ack",
            headers=acting_as("outside"),
        )
        final_outside_inbox = client.get(
            "/v1/messages", headers=acting_as("outside")
        ).json()["messages"]
        final_sandbox_inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]

    with sqlite3.connect(tmp_path / "relay.sqlite3") as connection:
        stored_message_count, = connection.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()

    assert outside.status_code == 201
    assert sandbox.status_code == 201
    assert "token" not in outside.json()
    assert recovered_outside.json()["session_id"] == outside_session_id
    assert missing_identity.status_code == 422
    assert missing_identity.json()["error"]["code"] == "validation_failed"
    assert unknown_recipient.status_code == 404
    assert sent["recipient_waiting_at_send"] is False
    assert [message["message_id"] for message in inbox] == [sent["message_id"]]
    assert reply["recipient_slug"] == "outside"
    assert reply["recipient_waiting_at_send"] is False
    assert acknowledged["acknowledged_at"] is not None
    assert [message["message_id"] for message in outside_inbox] == [
        reply["message_id"]
    ]
    assert final_outside_inbox == []
    assert final_sandbox_inbox == []
    assert stored_message_count == 2


def test_none_mode_mcp_tools_self_register_and_round_trip(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        None,
        authentication_mode="none",
    )
    with TestClient(app):
        tool_names = {
            tool.name for tool in asyncio.run(app.state.mcp.list_tools())
        }
        outside = call_mcp(
            app,
            "register_session",
            {"slug": "outside", "agent_kind": "codex"},
        )
        sandbox = call_mcp(
            app,
            "register_session",
            {"slug": "sandbox", "agent_kind": "claude"},
        )
        recovered_outside = call_mcp(
            app,
            "register_session",
            {"slug": "outside", "agent_kind": "codex"},
        )
        sent = call_mcp(
            app,
            "send_message",
            {
                "acting_slug": "outside",
                "recipient_slug": "sandbox",
                "content": "please inspect",
            },
        )
        inbox = call_mcp(
            app,
            "read_inbox",
            {"acting_slug": "sandbox"},
        )["messages"]
        reply = call_mcp(
            app,
            "reply_to_message",
            {
                "acting_slug": "sandbox",
                "message_id": sent["message_id"],
                "content": "inspection complete",
            },
        )
        acknowledged = call_mcp(
            app,
            "acknowledge_message",
            {
                "acting_slug": "sandbox",
                "message_id": sent["message_id"],
            },
        )

    assert tool_names == {
        "register_session",
        "whoami",
        "list_sessions",
        "send_message",
        "read_inbox",
        "wait_for_messages",
        "acknowledge_message",
        "reply_to_message",
    }
    assert recovered_outside["session_id"] == outside["session_id"]
    assert sent["recipient_waiting_at_send"] is False
    assert [message["message_id"] for message in inbox] == [sent["message_id"]]
    assert reply["recipient_slug"] == "outside"
    assert reply["recipient_waiting_at_send"] is False
    assert acknowledged["acknowledged_at"] is not None


def test_mcp_wait_returns_every_pending_message_in_order(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        call_mcp(
            app,
            "register_session",
            {"slug": "sender", "agent_kind": "codex"},
        )
        call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "opencode"},
        )
        sent = [
            call_mcp(
                app,
                "send_message",
                {
                    "acting_slug": "sender",
                    "recipient_slug": "recipient",
                    "content": content,
                },
            )
            for content in ("first", "second")
        ]
        delivered = call_mcp(
            app,
            "wait_for_messages",
            {"acting_slug": "recipient"},
        )["messages"]

    assert [message["message_id"] for message in delivered] == [
        message["message_id"] for message in sent
    ]
    assert all(
        message["first_delivery_attempt_at"] is not None
        and message["acknowledged_at"] is None
        for message in delivered
    )


def test_mcp_wait_blocks_until_send_notifies_it(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        call_mcp(
            app,
            "register_session",
            {"slug": "sender", "agent_kind": "codex"},
        )
        recipient = call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "opencode"},
        )

        async def scenario():
            wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "recipient"}
                )
            )
            await asyncio.sleep(0)
            assert await app.state.hub.is_mcp_waiting(recipient["session_id"])
            assert not wait_task.done()

            sent_result = await app.state.mcp.call_tool(
                "send_message",
                {
                    "acting_slug": "sender",
                    "recipient_slug": "recipient",
                    "content": "wake now",
                },
            )
            waited_result = await wait_task
            reply_wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "sender"}
                )
            )
            await asyncio.sleep(0)
            reply_result = await app.state.mcp.call_tool(
                "reply_to_message",
                {
                    "acting_slug": "recipient",
                    "message_id": sent_result.structured_content["message_id"],
                    "content": "reply while waiting",
                },
            )
            reply_waited_result = await reply_wait_task
            return (
                sent_result.structured_content,
                waited_result.structured_content,
                reply_result.structured_content,
                reply_waited_result.structured_content,
            )

        sent, waited, reply, reply_waited = asyncio.run(scenario())

    assert sent is not None
    assert waited is not None
    assert reply is not None
    assert reply_waited is not None
    assert sent["recipient_waiting_at_send"] is True
    assert [message["message_id"] for message in waited["messages"]] == [
        sent["message_id"]
    ]
    assert waited["messages"][0]["acknowledged_at"] is None
    assert reply["recipient_waiting_at_send"] is True
    assert [message["message_id"] for message in reply_waited["messages"]] == [
        reply["message_id"]
    ]


def test_mcp_send_observes_multiple_active_waits_until_they_return(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        call_mcp(
            app,
            "register_session",
            {"slug": "sender", "agent_kind": "codex"},
        )
        recipient = call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "opencode"},
        )

        async def scenario():
            wait_tasks = [
                asyncio.create_task(
                    app.state.mcp.call_tool(
                        "wait_for_messages", {"acting_slug": "recipient"}
                    )
                )
                for _ in range(2)
            ]
            await asyncio.sleep(0)
            assert await app.state.hub.is_mcp_waiting(recipient["session_id"])

            sent_result = await app.state.mcp.call_tool(
                "send_message",
                {
                    "acting_slug": "sender",
                    "recipient_slug": "recipient",
                    "content": "wake every listener",
                },
            )
            waited_results = await asyncio.gather(*wait_tasks)
            assert not await app.state.hub.is_mcp_waiting(
                recipient["session_id"]
            )
            return sent_result.structured_content, [
                result.structured_content for result in waited_results
            ]

        sent, waited = asyncio.run(scenario())

    assert sent is not None
    assert sent["recipient_waiting_at_send"] is True
    message_ids = []
    for result in waited:
        assert result is not None
        [message] = result["messages"]
        message_ids.append(message["message_id"])
    assert message_ids == [sent["message_id"], sent["message_id"]]


def test_cancelling_mcp_wait_clears_observation_without_changing_inbox(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        call_mcp(
            app,
            "register_session",
            {"slug": "sender", "agent_kind": "codex"},
        )
        recipient = call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "claude"},
        )

        async def scenario():
            wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "recipient"}
                )
            )
            await asyncio.sleep(0)
            assert await app.state.hub.is_mcp_waiting(recipient["session_id"])
            assert not wait_task.done()
            wait_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wait_task
            assert not await app.state.hub.is_mcp_waiting(
                recipient["session_id"]
            )
            sent_result = await app.state.mcp.call_tool(
                "send_message",
                {
                    "acting_slug": "sender",
                    "recipient_slug": "recipient",
                    "content": "after cancellation",
                },
            )
            return sent_result.structured_content

        sent = asyncio.run(scenario())

    assert sent is not None
    assert sent["recipient_waiting_at_send"] is False
    assert [
        message.message_id
        for message in app.state.store.unacknowledged_messages(
            recipient["session_id"]
        )
    ] == [sent["message_id"]]


def test_shutdown_ends_mcp_wait_and_sse_without_delivering_messages(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        sender = call_mcp(
            app,
            "register_session",
            {"slug": "sender", "agent_kind": "codex"},
        )
        recipient = call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "claude"},
        )

        async def scenario():
            wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "recipient"}
                )
            )
            stream = _event_stream(
                app.state.store,
                app.state.hub,
                recipient["session_id"],
            )
            stream_task = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            assert await app.state.hub.is_mcp_waiting(
                recipient["session_id"]
            )
            assert not wait_task.done()
            assert not stream_task.done()

            stored_before_shutdown = app.state.store.send_message(
                sender["session_id"],
                recipient["session_id"],
                "committed before shutdown",
            )
            app.state.hub.request_shutdown()
            observed_after_shutdown = await app.state.hub.notify(
                recipient["session_id"]
            )

            with pytest.raises(ToolError, match="relay is shutting down"):
                await wait_task
            with pytest.raises(StopAsyncIteration):
                await stream_task
            assert not await app.state.hub.is_mcp_waiting(
                recipient["session_id"]
            )

            sent_after_shutdown = await app.state.mcp.call_tool(
                "send_message",
                {
                    "acting_slug": "sender",
                    "recipient_slug": "recipient",
                    "content": "sent after shutdown",
                },
            )
            return (
                stored_before_shutdown,
                observed_after_shutdown,
                sent_after_shutdown.structured_content,
            )

        stored, observed, sent = asyncio.run(scenario())

    assert observed is False
    assert sent is not None
    assert sent["recipient_waiting_at_send"] is False
    pending = app.state.store.unacknowledged_messages(recipient["session_id"])
    assert [message.message_id for message in pending] == [
        stored.message_id,
        sent["message_id"],
    ]
    assert all(message.first_delivery_attempt_at is None for message in pending)


def test_shutdown_closes_modern_subscription_and_every_legacy_transport():
    terminated = []
    subscription_closes = []

    class Transport:
        def __init__(self, name):
            self.name = name

        async def terminate(self):
            terminated.append(self.name)

    manager = SimpleNamespace(
        _server_instances={
            "first": Transport("first"),
            "second": Transport("second"),
        }
    )
    subscription_listener = SimpleNamespace(
        close=lambda: subscription_closes.append("closed")
    )
    hub = NotificationHub()
    shutdown = RelayShutdown(hub, manager, subscription_listener)

    asyncio.run(shutdown.finish())
    shutdown.request()

    assert terminated == ["first", "second"]
    assert subscription_closes == ["closed"]
    assert hub.is_shutting_down


def test_token_mode_mcp_identity_cannot_be_overridden(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    with TestClient(app) as client:
        tool_names = {
            tool.name for tool in asyncio.run(app.state.mcp.list_tools())
        }
        outside = create_session(client, "outside", "codex")
        sandbox = create_session(client, "sandbox", "claude")
        context = SimpleNamespace(
            headers={"authorization": f"Bearer {outside['token']}"}
        )
        identity = call_mcp(app, "whoami", {}, context=context)
        with pytest.raises(ToolError, match="does not match"):
            asyncio.run(
                app.state.mcp.call_tool(
                    "whoami",
                    {"acting_slug": sandbox["session"]["slug"]},
                    context=context,
                )
            )

    assert identity["session_id"] == outside["session"]["session_id"]
    assert "register_session" not in tool_names


def test_http_round_trip_is_durable_and_two_way(tmp_path):
    database_path = tmp_path / "relay.sqlite3"
    first_app = create_app(
        database_path,
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    with TestClient(first_app) as client:
        outside = create_session(client, "outside", "codex")
        sandbox = create_session(client, "sandbox", "claude")
        sent = client.post(
            "/v1/messages",
            headers=authorization(outside["token"]),
            json={
                "recipient_slug": sandbox["session"]["slug"],
                "content": "please inspect",
            },
        ).json()

    restarted_app = create_app(
        database_path,
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    with TestClient(restarted_app) as client:
        inbox = client.get(
            "/v1/messages", headers=authorization(sandbox["token"])
        ).json()["messages"]
        reply = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=authorization(sandbox["token"]),
            json={"content": "inspection complete"},
        ).json()
        acknowledged = client.post(
            f"/v1/messages/{sent['message_id']}/ack",
            headers=authorization(sandbox["token"]),
        ).json()
        outside_inbox = client.get(
            "/v1/messages", headers=authorization(outside["token"])
        ).json()["messages"]

    assert [message["message_id"] for message in inbox] == [sent["message_id"]]
    assert sent["recipient_waiting_at_send"] is False
    assert inbox[0]["first_delivery_attempt_at"] is not None
    assert acknowledged["acknowledged_at"] is not None
    assert [message["message_id"] for message in outside_inbox] == [
        reply["message_id"]
    ]
    assert reply["recipient_slug"] == outside["session"]["slug"]
    assert reply["recipient_waiting_at_send"] is False


def test_mcp_rejects_body_above_approved_boundary_without_storing_it(tmp_path):
    app = create_app(
        tmp_path / "relay.sqlite3",
        ADMIN_TOKEN,
        authentication_mode="token",
    )
    oversized_body = b"x" * (MCP_MAX_REQUEST_BODY_BYTES + 1)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer invalid",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            content=oversized_body,
        )

    assert response.status_code == 413
    assert app.state.store.list_sessions() == []


# The MCP transport allows the test host only with a port, so these raw
# POSTs address it that way rather than through the default base URL.
MCP_BASE_URL = "http://testserver:8787"


def mcp_headers(protocol_version, method):
    return {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": protocol_version,
        "Mcp-Method": method,
    }


def test_modern_notification_is_accepted_rather_than_called_a_bad_request(
    tmp_path,
):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=mcp_headers(LATEST_MODERN_VERSION, "notifications/cancelled"),
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 4, "reason": "context deadline exceeded"},
            },
        )

    assert response.status_code == 202
    assert response.content == b""


def test_modern_request_still_reaches_the_mcp_server(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=mcp_headers(LATEST_MODERN_VERSION, "tools/list"),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": (
                            LATEST_MODERN_VERSION
                        ),
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )

    assert response.status_code == 200
    assert "wait_for_messages" in response.text


def test_modern_body_that_is_not_a_notification_keeps_its_rejection(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=mcp_headers(LATEST_MODERN_VERSION, "notifications/cancelled"),
            json=["notifications/cancelled"],
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600


def test_handshake_era_notification_keeps_its_session_requirement(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=mcp_headers(
                LATEST_HANDSHAKE_VERSION, "notifications/cancelled"
            ),
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 4},
            },
        )

    assert response.status_code == 400
    assert "session" in response.json()["error"]["message"].lower()


def test_notification_with_unusable_content_type_keeps_its_rejection(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    headers = mcp_headers(LATEST_MODERN_VERSION, "notifications/cancelled")
    headers["Content-Type"] = "text/plain"
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=headers,
            content=(
                b'{"jsonrpc": "2.0", "method": "notifications/cancelled",'
                b' "params": {"requestId": 4}}'
            ),
        )

    assert response.status_code == 400
    assert "Content-Type" in response.text


def test_notification_body_above_the_boundary_is_still_refused(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    padding = "x" * MCP_MAX_REQUEST_BODY_BYTES
    with TestClient(app, base_url=MCP_BASE_URL) as client:
        response = client.post(
            "/mcp",
            headers=mcp_headers(LATEST_MODERN_VERSION, "notifications/cancelled"),
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 4, "reason": padding},
            },
        )

    assert response.status_code == 413


def test_reply_acknowledges_the_answered_message_in_one_call(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        client.post("/v1/sessions", json={"slug": "outside", "agent_kind": "codex"})
        client.post("/v1/sessions", json={"slug": "sandbox", "agent_kind": "claude"})
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": "please inspect"},
        ).json()

        combined = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "inspection complete", "acknowledge": True},
        )
        sandbox_inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]
        outside_inbox = client.get(
            "/v1/messages", headers=acting_as("outside")
        ).json()["messages"]

    assert combined.status_code == 201
    body = combined.json()
    assert body["acknowledged_message_id"] == sent["message_id"]
    assert body["recipient_slug"] == "outside"
    # One call did both: nothing left pending for the replier, and the reply
    # itself reached the other participant.
    assert sandbox_inbox == []
    assert [message["message_id"] for message in outside_inbox] == [
        body["message_id"]
    ]


def test_reply_reports_no_acknowledgement_when_none_was_requested(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        client.post("/v1/sessions", json={"slug": "outside", "agent_kind": "codex"})
        client.post("/v1/sessions", json={"slug": "sandbox", "agent_kind": "claude"})
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": "please inspect"},
        ).json()

        reply = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "still working"},
        ).json()
        sandbox_inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]

    assert reply["acknowledged_message_id"] is None
    assert [message["message_id"] for message in sandbox_inbox] == [
        sent["message_id"]
    ]


def test_mcp_reply_tool_carries_the_acknowledgement(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app):
        call_mcp(app, "register_session", {"slug": "outside", "agent_kind": "codex"})
        call_mcp(app, "register_session", {"slug": "sandbox", "agent_kind": "claude"})
        sent = call_mcp(
            app,
            "send_message",
            {
                "recipient_slug": "sandbox",
                "content": "please inspect",
                "acting_slug": "outside",
            },
        )
        reply = call_mcp(
            app,
            "reply_to_message",
            {
                "message_id": sent["message_id"],
                "content": "inspection complete",
                "acknowledge": True,
                "acting_slug": "sandbox",
            },
        )
        sandbox_inbox = call_mcp(
            app, "read_inbox", {"acting_slug": "sandbox"}
        )["messages"]

    assert reply["acknowledged_message_id"] == sent["message_id"]
    assert sandbox_inbox == []


def test_reply_acknowledgement_from_the_sender_is_refused_without_replying(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        client.post("/v1/sessions", json={"slug": "outside", "agent_kind": "codex"})
        client.post("/v1/sessions", json={"slug": "sandbox", "agent_kind": "claude"})
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": "please inspect"},
        ).json()

        refused = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("outside"),
            json={"content": "following up", "acknowledge": True},
        )
        sandbox_inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]

    assert refused.status_code == 403
    # Refused before sending: the original stands alone, with no reply beside it.
    assert [message["message_id"] for message in sandbox_inbox] == [
        sent["message_id"]
    ]


def test_reply_rejects_a_non_boolean_acknowledge(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        client.post("/v1/sessions", json={"slug": "outside", "agent_kind": "codex"})
        client.post("/v1/sessions", json={"slug": "sandbox", "agent_kind": "claude"})
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": "please inspect"},
        ).json()

        rejected = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "inspection complete", "acknowledge": "yes"},
        )
        sandbox_inbox = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]

    assert rejected.status_code == 422
    assert [message["message_id"] for message in sandbox_inbox] == [
        sent["message_id"]
    ]


CONFIRMATION_ONLY_SLUGS = ("outside", "sandbox")
DISTINCTIVE_BODY = "audit-marker-9f3c\nsecond line of the body"


def _register_pair(client):
    for slug, kind in zip(CONFIRMATION_ONLY_SLUGS, ("codex", "claude")):
        client.post("/v1/sessions", json={"slug": slug, "agent_kind": kind})


def test_confirmations_omit_the_content_that_delivery_returns_verbatim(tmp_path):
    """The body is absent from send and ack, and present in the inbox between them."""
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        _register_pair(client)
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": DISTINCTIVE_BODY},
        ).json()
        delivered, = client.get(
            "/v1/messages", headers=acting_as("sandbox")
        ).json()["messages"]
        acknowledged = client.post(
            f"/v1/messages/{sent['message_id']}/ack",
            headers=acting_as("sandbox"),
        ).json()

    # Confirmation of the send: identity and routing, no body.
    assert "content" not in sent
    assert sent["message_id"] and sent["recipient_slug"] == "sandbox"
    # Delivery of the same message: the body, byte for byte. Without this the
    # test could not tell a trimmed response from a lost message.
    assert delivered["message_id"] == sent["message_id"]
    assert delivered["content"] == DISTINCTIVE_BODY
    # Confirmation of the acknowledgement: which message, and when.
    assert "content" not in acknowledged
    assert acknowledged["message_id"] == sent["message_id"]
    assert acknowledged["acknowledged_at"] is not None


def test_confirmations_omit_internal_session_identifiers(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        _register_pair(client)
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": DISTINCTIVE_BODY},
        ).json()
        reply = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "answer", "acknowledge": True},
        ).json()
        delivered, = client.get(
            "/v1/messages", headers=acting_as("outside")
        ).json()["messages"]

    internal = ("sender_session_id", "recipient_session_id")
    assert not any(field in sent for field in internal)
    assert not any(field in reply for field in internal)
    # Delivery is unchanged, so the identifiers are still reachable there.
    assert all(field in delivered for field in internal)


def test_reply_confirmation_keeps_the_fields_agents_are_told_to_read(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app) as client:
        _register_pair(client)
        sent = client.post(
            "/v1/messages",
            headers=acting_as("outside"),
            json={"recipient_slug": "sandbox", "content": DISTINCTIVE_BODY},
        ).json()
        reply = client.post(
            f"/v1/messages/{sent['message_id']}/reply",
            headers=acting_as("sandbox"),
            json={"content": "answer", "acknowledge": True},
        ).json()

    assert reply["recipient_waiting_at_send"] is False
    assert reply["acknowledged_message_id"] == sent["message_id"]
    assert reply["in_reply_to"] == sent["message_id"]
    assert reply["recipient_slug"] == "outside"
    assert reply["sent_at"] is not None
    assert "content" not in reply


def test_mcp_wait_and_inbox_still_deliver_content_after_trimming(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None, authentication_mode="none")
    with TestClient(app):
        call_mcp(app, "register_session", {"slug": "outside", "agent_kind": "codex"})
        call_mcp(app, "register_session", {"slug": "sandbox", "agent_kind": "claude"})
        sent = call_mcp(
            app,
            "send_message",
            {
                "recipient_slug": "sandbox",
                "content": DISTINCTIVE_BODY,
                "acting_slug": "outside",
            },
        )
        waited = call_mcp(app, "wait_for_messages", {"acting_slug": "sandbox"})
        inbox = call_mcp(app, "read_inbox", {"acting_slug": "sandbox"})
        acknowledged = call_mcp(
            app,
            "acknowledge_message",
            {"message_id": sent["message_id"], "acting_slug": "sandbox"},
        )

    assert "content" not in sent
    assert waited["messages"][0]["content"] == DISTINCTIVE_BODY
    assert inbox["messages"][0]["content"] == DISTINCTIVE_BODY
    assert "content" not in acknowledged
    assert acknowledged["acknowledged_at"] is not None
