#!/usr/bin/env python3
"""Send one canonical Agent Relay wake through tmux-message."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import signal
import subprocess
import sys


EXIT_USAGE = 64
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


class UsageError(Exception):
    """The helper invocation is invalid before the sender starts."""


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise UsageError(message.strip() if message else "help requested")


def canonical_wake(message_id: str) -> str:
    if UUID_RE.fullmatch(message_id) is None:
        raise ValueError("message ID must be one lowercase hexadecimal UUID")
    return (
        f"Relay message {message_id} is queued. "
        "Process the Relay inbox and restore exactly one listener."
    )


def send_wake(tmux_sender: Path, target: str, message_id: str) -> int:
    if not tmux_sender.is_file():
        raise ValueError(f"tmux sender is not a regular file: {tmux_sender}")
    wake = canonical_wake(message_id)
    child_started = False

    def handle_parent_signal(signal_number: int, _frame: object) -> None:
        if not child_started:
            raise UsageError(f"interrupted before sender started ({signal_number})")

    previous_handlers = {}
    for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signal_number] = signal.getsignal(signal_number)
        signal.signal(signal_number, handle_parent_signal)
    try:
        child = subprocess.Popen(
            [sys.executable, str(tmux_sender), target, "--shell-safe-text", wake]
        )
        child_started = True
        return child.wait()
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


def main(argv: list[str] | None = None) -> int:
    try:
        parser = Parser(
            description="Send one canonical Relay tmux wake.",
            allow_abbrev=False,
        )
        parser.add_argument("tmux_sender", type=Path)
        parser.add_argument("target")
        parser.add_argument("message_id")
        args = parser.parse_args(argv)
        return send_wake(args.tmux_sender, args.target, args.message_id)
    except (OSError, UsageError, ValueError) as error:
        print(f"wake error: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
