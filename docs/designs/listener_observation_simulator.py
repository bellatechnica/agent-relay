"""Executable state model for listener observation and durable messages."""

from dataclasses import dataclass, field


@dataclass
class RelayModel:
    active_mcp_waits: int = 0
    pending_message_ids: list[str] = field(default_factory=list)

    def start_wait(self) -> list[str] | None:
        self.active_mcp_waits += 1
        if not self.pending_message_ids:
            return None
        return self.finish_wait()

    def finish_wait(self) -> list[str]:
        if self.active_mcp_waits == 0:
            raise AssertionError("no active wait can finish")
        self.active_mcp_waits -= 1
        return list(self.pending_message_ids)

    def cancel_wait(self) -> None:
        if self.active_mcp_waits == 0:
            raise AssertionError("no active wait can be cancelled")
        self.active_mcp_waits -= 1

    def send(self, message_id: str) -> bool:
        if message_id in self.pending_message_ids:
            raise AssertionError(f"duplicate message ID: {message_id}")
        self.pending_message_ids.append(message_id)
        return self.active_mcp_waits > 0

    def acknowledge(self, message_id: str) -> None:
        self.pending_message_ids.remove(message_id)

    def restart(self) -> None:
        self.active_mcp_waits = 0


def run_scenarios() -> None:
    send_before_wait = RelayModel()
    assert send_before_wait.send("before") is False
    assert send_before_wait.start_wait() == ["before"]
    assert send_before_wait.active_mcp_waits == 0

    active_wait = RelayModel()
    assert active_wait.start_wait() is None
    assert active_wait.send("active") is True
    assert active_wait.finish_wait() == ["active"]
    assert active_wait.active_mcp_waits == 0

    cancelled_wait = RelayModel()
    assert cancelled_wait.start_wait() is None
    cancelled_wait.cancel_wait()
    assert cancelled_wait.send("after-cancel") is False
    assert cancelled_wait.pending_message_ids == ["after-cancel"]

    concurrent_waits = RelayModel()
    assert concurrent_waits.start_wait() is None
    assert concurrent_waits.start_wait() is None
    assert concurrent_waits.send("shared") is True
    assert concurrent_waits.finish_wait() == ["shared"]
    assert concurrent_waits.finish_wait() == ["shared"]
    concurrent_waits.acknowledge("shared")
    assert concurrent_waits.pending_message_ids == []

    restarted = RelayModel()
    assert restarted.start_wait() is None
    restarted.restart()
    assert restarted.send("after-restart") is False
    assert restarted.pending_message_ids == ["after-restart"]

    replacement_gap = RelayModel()
    assert replacement_gap.start_wait() is None
    replacement_gap.cancel_wait()
    assert replacement_gap.send("during-gap") is False
    assert replacement_gap.start_wait() == ["during-gap"]


if __name__ == "__main__":
    run_scenarios()
    print("6 listener-observation scenarios passed")
