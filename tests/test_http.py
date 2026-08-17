import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from agent_relay.server import MCP_MAX_REQUEST_BODY_BYTES, create_app


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
    assert [message["message_id"] for message in inbox] == [sent["message_id"]]
    assert reply["recipient_slug"] == "outside"
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
    assert [message["message_id"] for message in inbox] == [sent["message_id"]]
    assert reply["recipient_slug"] == "outside"
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
            original_event_for = app.state.hub.event_for
            wait_entered = asyncio.Event()

            async def observed_event_for(session_id):
                event = await original_event_for(session_id)
                if session_id == recipient["session_id"]:
                    wait_entered.set()
                return event

            app.state.hub.event_for = observed_event_for
            wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "recipient"}
                )
            )
            await wait_entered.wait()
            await asyncio.sleep(0)
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
            return sent_result.structured_content, waited_result.structured_content

        sent, waited = asyncio.run(scenario())

    assert sent is not None
    assert waited is not None
    assert [message["message_id"] for message in waited["messages"]] == [
        sent["message_id"]
    ]
    assert waited["messages"][0]["acknowledged_at"] is None


def test_cancelling_mcp_wait_does_not_change_inbox(tmp_path):
    app = create_app(tmp_path / "relay.sqlite3", None)
    with TestClient(app):
        recipient = call_mcp(
            app,
            "register_session",
            {"slug": "recipient", "agent_kind": "claude"},
        )

        async def scenario():
            original_event_for = app.state.hub.event_for
            wait_entered = asyncio.Event()

            async def observed_event_for(session_id):
                event = await original_event_for(session_id)
                if session_id == recipient["session_id"]:
                    wait_entered.set()
                return event

            app.state.hub.event_for = observed_event_for
            wait_task = asyncio.create_task(
                app.state.mcp.call_tool(
                    "wait_for_messages", {"acting_slug": "recipient"}
                )
            )
            await wait_entered.wait()
            await asyncio.sleep(0)
            assert not wait_task.done()
            wait_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await wait_task

        asyncio.run(scenario())

    assert app.state.store.unacknowledged_messages(recipient["session_id"]) == []


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
    assert inbox[0]["first_delivery_attempt_at"] is not None
    assert acknowledged["acknowledged_at"] is not None
    assert [message["message_id"] for message in outside_inbox] == [
        reply["message_id"]
    ]
    assert reply["recipient_slug"] == outside["session"]["slug"]


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
