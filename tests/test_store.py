import sqlite3

import pytest

from agent_relay.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from agent_relay.store import RelayStore


@pytest.fixture
def store(tmp_path):
    relay_store = RelayStore(tmp_path / "relay.sqlite3")
    relay_store.initialize()
    return relay_store


def issue_pair(store):
    return (
        store.issue_session("outside", "codex"),
        store.issue_session("sandbox", "claude"),
    )


def test_session_token_authenticates_but_is_not_stored_in_plaintext(store):
    issued = store.issue_session("sandbox", "codex")

    authenticated = store.authenticate(issued.token)

    assert authenticated.session_id == issued.session.session_id
    assert authenticated.last_authenticated_at is not None
    with sqlite3.connect(store.database_path) as connection:
        stored_hash, = connection.execute("SELECT token_hash FROM sessions").fetchone()
    assert issued.token not in stored_hash


def test_slugs_are_unique(store):
    store.issue_session("sandbox", "codex")

    with pytest.raises(ConflictError, match="already exists"):
        store.issue_session("sandbox", "claude")


def test_registration_recovers_the_existing_active_slug(store):
    first = store.register_session("sandbox", "codex")

    recovered = store.register_session("sandbox", "codex")

    assert recovered == first
    assert [session.slug for session in store.list_sessions()] == ["sandbox"]


def test_slug_lookup_is_exact_and_rejects_unknown_values(store):
    registered = store.register_session("api-review", "claude")

    assert store.active_session_by_slug("api-review") == registered
    with pytest.raises(NotFoundError, match="active session slug"):
        store.active_session_by_slug("api")


def test_revoked_token_no_longer_authenticates(store):
    issued = store.issue_session("sandbox", "opencode")
    store.revoke_session(issued.session.session_id)

    with pytest.raises(AuthenticationError, match="invalid or revoked"):
        store.authenticate(issued.token)


def test_send_preserves_full_content_and_inbox_order(store):
    outside, sandbox = issue_pair(store)
    first_content = "opening\n" + ("payload-" * 200_000) + "\nclosing"

    first = store.send_message(
        outside.session.session_id, sandbox.session.session_id, first_content
    )
    second = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "second"
    )
    inbox = store.unacknowledged_messages(sandbox.session.session_id)

    assert [message.message_id for message in inbox] == [
        first.message_id,
        second.message_id,
    ]
    assert inbox[0].content == first_content
    assert inbox[0].content.endswith("\nclosing")


def test_delivery_attempt_and_acknowledgement_are_distinct(store):
    outside, sandbox = issue_pair(store)
    message = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "work"
    )

    store.mark_delivery_attempt(sandbox.session.session_id, [message.message_id])
    attempted, = store.unacknowledged_messages(sandbox.session.session_id)
    acknowledged = store.acknowledge_message(
        sandbox.session.session_id, message.message_id
    )

    assert attempted.first_delivery_attempt_at is not None
    assert attempted.acknowledged_at is None
    assert acknowledged.acknowledged_at is not None
    assert store.unacknowledged_messages(sandbox.session.session_id) == []


def test_only_recipient_can_acknowledge(store):
    outside, sandbox = issue_pair(store)
    message = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "work"
    )

    with pytest.raises(AuthorizationError, match="only the recipient"):
        store.acknowledge_message(outside.session.session_id, message.message_id)


def test_reply_derives_the_other_participant(store):
    outside, sandbox = issue_pair(store)
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )

    reply = store.reply_to_message(
        sandbox.session.session_id, original.message_id, "answer"
    )

    assert reply.sender_session_id == sandbox.session.session_id
    assert reply.recipient_session_id == outside.session.session_id
    assert reply.in_reply_to == original.message_id


def test_nonparticipant_cannot_reply(store):
    outside, sandbox = issue_pair(store)
    observer = store.issue_session("observer", "opencode")
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )

    with pytest.raises(AuthorizationError, match="only a participant"):
        store.reply_to_message(
            observer.session.session_id, original.message_id, "interruption"
        )


def test_in_reply_to_requires_same_participants(store):
    outside, sandbox = issue_pair(store)
    observer = store.issue_session("observer", "opencode")
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )

    with pytest.raises(ValidationError, match="same sessions"):
        store.send_message(
            outside.session.session_id,
            observer.session.session_id,
            "misaddressed",
            in_reply_to=original.message_id,
        )


def test_unknown_recipient_is_rejected(store):
    outside = store.issue_session("outside", "codex")

    with pytest.raises(NotFoundError, match="active session"):
        store.send_message(outside.session.session_id, "missing", "hello")


def test_reply_can_carry_its_own_acknowledgement(store):
    outside, sandbox = issue_pair(store)
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )
    # Reach the branch from the state a real session is in: the message has
    # been delivered and is pending, not freshly inserted.
    store.mark_delivery_attempt(
        sandbox.session.session_id, [original.message_id]
    )
    assert len(store.unacknowledged_messages(sandbox.session.session_id)) == 1

    reply = store.reply_to_message(
        sandbox.session.session_id,
        original.message_id,
        "answer",
        acknowledge=True,
    )

    assert reply.recipient_session_id == outside.session.session_id
    assert reply.in_reply_to == original.message_id
    assert store.unacknowledged_messages(sandbox.session.session_id) == []
    # The reply itself is a new message to the other participant and must not
    # arrive pre-acknowledged in their inbox.
    pending, = store.unacknowledged_messages(outside.session.session_id)
    assert pending.message_id == reply.message_id


def test_reply_without_acknowledgement_leaves_the_message_pending(store):
    outside, sandbox = issue_pair(store)
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )

    store.reply_to_message(
        sandbox.session.session_id, original.message_id, "still working"
    )

    pending, = store.unacknowledged_messages(sandbox.session.session_id)
    assert pending.message_id == original.message_id
    assert pending.acknowledged_at is None


def test_sender_replying_cannot_acknowledge_and_sends_no_reply(store):
    outside, sandbox = issue_pair(store)
    original = store.send_message(
        outside.session.session_id, sandbox.session.session_id, "question"
    )

    with pytest.raises(AuthorizationError, match="only the recipient"):
        store.reply_to_message(
            outside.session.session_id,
            original.message_id,
            "following up",
            acknowledge=True,
        )

    # Refused before sending: the recipient's inbox holds the original alone,
    # with no reply delivered alongside it.
    pending, = store.unacknowledged_messages(sandbox.session.session_id)
    assert pending.message_id == original.message_id
    assert store.unacknowledged_messages(outside.session.session_id) == []
