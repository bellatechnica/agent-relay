#!/usr/bin/env python3
"""Send one canonical Agent Relay wake through tmux-message."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import signal
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
    managed_signals = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    previous_handlers = {}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, managed_signals)
    mask_restored = False
    for signal_number in managed_signals:
        previous_handlers[signal_number] = signal.getsignal(signal_number)
    try:
        arguments = [
            sys.executable,
            str(tmux_sender),
            target,
            "--shell-safe-text",
            wake,
        ]
        child_pid = os.posix_spawn(
            sys.executable,
            arguments,
            os.environ,
            setsigmask=previous_mask,
        )
        for signal_number in managed_signals:
            signal.signal(signal_number, lambda _number, _frame: None)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        mask_restored = True
        _pid, status = os.waitpid(child_pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        if not mask_restored:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
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
