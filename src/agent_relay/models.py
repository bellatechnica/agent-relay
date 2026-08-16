"""Public relay records."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Session:
    session_id: str
    slug: str
    agent_kind: str
    registered_at: str
    last_authenticated_at: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class IssuedSession:
    session: Session
    token: str

    def as_dict(self) -> dict[str, object]:
        return {"session": self.session.as_dict(), "token": self.token}


@dataclass(frozen=True)
class Message:
    sequence: int
    message_id: str
    sender_session_id: str
    sender_slug: str
    recipient_session_id: str
    recipient_slug: str
    content: str
    in_reply_to: str | None
    sent_at: str
    first_delivery_attempt_at: str | None
    acknowledged_at: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)
