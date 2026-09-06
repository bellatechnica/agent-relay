"""SQLite-backed durable relay state."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .models import IssuedSession, Message, Session


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RelayStore:
    """Owns durable sessions and messages in one SQLite database."""

    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL UNIQUE,
                    agent_kind TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    registered_at TEXT NOT NULL,
                    last_authenticated_at TEXT,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS messages (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL UNIQUE,
                    sender_session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    recipient_session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    content TEXT NOT NULL,
                    in_reply_to TEXT REFERENCES messages(message_id),
                    sent_at TEXT NOT NULL,
                    first_delivery_attempt_at TEXT,
                    acknowledged_at TEXT
                );

                CREATE INDEX IF NOT EXISTS messages_recipient_unacknowledged
                    ON messages(recipient_session_id, acknowledged_at, sequence);
                """
            )

    def issue_session(self, slug: str, agent_kind: str) -> IssuedSession:
        normalized_slug = slug.strip()
        normalized_kind = agent_kind.strip()
        if not normalized_slug:
            raise ValidationError("slug must not be empty")
        if not normalized_kind:
            raise ValidationError("agent_kind must not be empty")

        session_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        registered_at = _now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        session_id, display_name, agent_kind, token_hash, registered_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        normalized_slug,
                        normalized_kind,
                        _token_hash(token),
                        registered_at,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"a session with slug {normalized_slug!r} already exists"
            ) from error

        session = Session(
            session_id=session_id,
            slug=normalized_slug,
            agent_kind=normalized_kind,
            registered_at=registered_at,
            last_authenticated_at=None,
        )
        return IssuedSession(session=session, token=token)

    def register_session(self, slug: str, agent_kind: str) -> Session:
        """Create a session by slug, or recover the active one already registered."""
        normalized_slug = slug.strip()
        if not normalized_slug:
            raise ValidationError("slug must not be empty")
        try:
            return self.issue_session(normalized_slug, agent_kind).session
        except ConflictError as conflict:
            try:
                return self.active_session_by_slug(normalized_slug)
            except NotFoundError:
                raise conflict

    def authenticate(self, token: str) -> Session:
        if not token:
            raise AuthenticationError("a bearer token is required")
        token_hash = _token_hash(token)
        authenticated_at = _now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, display_name, agent_kind, registered_at,
                       last_authenticated_at
                FROM sessions
                WHERE token_hash = ? AND revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("the bearer token is invalid or revoked")
            connection.execute(
                """
                UPDATE sessions SET last_authenticated_at = ? WHERE session_id = ?
                """,
                (authenticated_at, row["session_id"]),
            )
            connection.commit()

        return Session(
            session_id=row["session_id"],
            slug=row["display_name"],
            agent_kind=row["agent_kind"],
            registered_at=row["registered_at"],
            last_authenticated_at=authenticated_at,
        )

    def active_session(self, session_id: str) -> Session:
        """Return an active session by ID without authenticating it."""
        if not isinstance(session_id, str) or session_id == "":
            raise ValidationError("session_id must be a non-empty string")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, display_name, agent_kind, registered_at,
                       last_authenticated_at
                FROM sessions
                WHERE session_id = ? AND revoked_at IS NULL
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"active session {session_id!r} was not found")
        return self._session_from_row(row)

    def active_session_by_slug(self, slug: str) -> Session:
        """Return the one active session with an exact slug."""
        if not isinstance(slug, str) or not slug.strip():
            raise ValidationError("slug must be a non-empty string")
        normalized_slug = slug.strip()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, display_name, agent_kind, registered_at,
                       last_authenticated_at
                FROM sessions
                WHERE display_name = ? AND revoked_at IS NULL
                """,
                (normalized_slug,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"active session slug {normalized_slug!r} was not found")
        return self._session_from_row(row)

    def list_sessions(self) -> list[Session]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT session_id, display_name, agent_kind, registered_at,
                       last_authenticated_at
                FROM sessions
                WHERE revoked_at IS NULL
                ORDER BY registered_at, session_id
                """
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def revoke_session(self, session_id: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions SET revoked_at = ?
                WHERE session_id = ? AND revoked_at IS NULL
                """,
                (_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"active session {session_id!r} was not found")
            connection.commit()

    def send_message(
        self,
        sender_session_id: str,
        recipient_session_id: str,
        content: str,
        in_reply_to: str | None = None,
    ) -> Message:
        if not isinstance(content, str) or content == "":
            raise ValidationError("content must be a non-empty string")
        message_id = str(uuid.uuid4())
        sent_at = _now()

        with self._connect() as connection:
            self._require_active_session(connection, sender_session_id)
            self._require_active_session(connection, recipient_session_id)
            if in_reply_to is not None:
                original = self._require_message(connection, in_reply_to)
                expected_participants = {
                    original["sender_session_id"],
                    original["recipient_session_id"],
                }
                actual_participants = {sender_session_id, recipient_session_id}
                if actual_participants != expected_participants:
                    raise ValidationError(
                        "in_reply_to must refer to a message between the same sessions"
                    )

            connection.execute(
                """
                INSERT INTO messages (
                    message_id, sender_session_id, recipient_session_id,
                    content, in_reply_to, sent_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    sender_session_id,
                    recipient_session_id,
                    content,
                    in_reply_to,
                    sent_at,
                ),
            )
            row = self._message_by_id(connection, message_id)
            connection.commit()

        return self._message_from_row(row)

    def reply_to_message(
        self,
        replying_session_id: str,
        message_id: str,
        content: str,
        acknowledge: bool = False,
    ) -> Message:
        with self._connect() as connection:
            original = self._require_message(connection, message_id)
        participants = {
            original["sender_session_id"],
            original["recipient_session_id"],
        }
        if replying_session_id not in participants:
            raise AuthorizationError("only a participant can reply to this message")
        # Either participant may reply, but only the recipient may acknowledge.
        # Refuse before sending so a rejected acknowledgement leaves no reply
        # behind; the reverse order would deliver a reply the caller believes
        # was refused.
        if acknowledge and replying_session_id != original["recipient_session_id"]:
            raise AuthorizationError("only the recipient can acknowledge this message")
        recipient_session_id = (
            original["recipient_session_id"]
            if replying_session_id == original["sender_session_id"]
            else original["sender_session_id"]
        )
        reply = self.send_message(
            replying_session_id,
            recipient_session_id,
            content,
            in_reply_to=message_id,
        )
        # Sent first, acknowledged second: should this fail, the reply stands
        # and the answered message stays pending, so it is redelivered rather
        # than lost.
        if acknowledge:
            self.acknowledge_message(replying_session_id, message_id)
        return reply

    def unacknowledged_messages(self, recipient_session_id: str) -> list[Message]:
        with self._connect() as connection:
            rows = connection.execute(
                self._message_select()
                + """
                WHERE m.recipient_session_id = ? AND m.acknowledged_at IS NULL
                ORDER BY m.sequence
                """,
                (recipient_session_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def mark_delivery_attempt(
        self, recipient_session_id: str, message_ids: Iterable[str]
    ) -> None:
        attempted_at = _now()
        with self._connect() as connection:
            for message_id in message_ids:
                connection.execute(
                    """
                    UPDATE messages
                    SET first_delivery_attempt_at = COALESCE(
                        first_delivery_attempt_at, ?
                    )
                    WHERE message_id = ? AND recipient_session_id = ?
                    """,
                    (attempted_at, message_id, recipient_session_id),
                )
            connection.commit()

    def acknowledge_message(self, recipient_session_id: str, message_id: str) -> Message:
        acknowledged_at = _now()
        with self._connect() as connection:
            row = self._require_message(connection, message_id)
            if row["recipient_session_id"] != recipient_session_id:
                raise AuthorizationError("only the recipient can acknowledge this message")
            connection.execute(
                """
                UPDATE messages
                SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE message_id = ?
                """,
                (acknowledged_at, message_id),
            )
            updated = self._message_by_id(connection, message_id)
            connection.commit()
        return self._message_from_row(updated)

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> Session:
        return Session(
            session_id=row["session_id"],
            slug=row["display_name"],
            agent_kind=row["agent_kind"],
            registered_at=row["registered_at"],
            last_authenticated_at=row["last_authenticated_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        return Message(
            sequence=row["sequence"],
            message_id=row["message_id"],
            sender_session_id=row["sender_session_id"],
            sender_slug=row["sender_display_name"],
            recipient_session_id=row["recipient_session_id"],
            recipient_slug=row["recipient_display_name"],
            content=row["content"],
            in_reply_to=row["in_reply_to"],
            sent_at=row["sent_at"],
            first_delivery_attempt_at=row["first_delivery_attempt_at"],
            acknowledged_at=row["acknowledged_at"],
        )

    @staticmethod
    def _require_active_session(
        connection: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT session_id FROM sessions WHERE session_id = ? AND revoked_at IS NULL",
            (session_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"active session {session_id!r} was not found")
        return row

    @staticmethod
    def _message_select() -> str:
        return """
            SELECT m.sequence, m.message_id, m.sender_session_id,
                   sender.display_name AS sender_display_name,
                   m.recipient_session_id,
                   recipient.display_name AS recipient_display_name,
                   m.content, m.in_reply_to, m.sent_at,
                   m.first_delivery_attempt_at, m.acknowledged_at
            FROM messages AS m
            JOIN sessions AS sender ON sender.session_id = m.sender_session_id
            JOIN sessions AS recipient ON recipient.session_id = m.recipient_session_id
        """

    def _message_by_id(
        self, connection: sqlite3.Connection, message_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            self._message_select() + " WHERE m.message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"message {message_id!r} was not found")
        return row

    @staticmethod
    def _require_message(
        connection: sqlite3.Connection, message_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT message_id, sender_session_id, recipient_session_id
            FROM messages WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"message {message_id!r} was not found")
        return row
