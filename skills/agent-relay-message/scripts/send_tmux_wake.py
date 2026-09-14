#!/usr/bin/env python3
"""Send one canonical Agent Relay wake through tmux-message stdin."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


EXIT_USAGE = 64
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


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
    completed = subprocess.run(
        [sys.executable, str(tmux_sender), target, "--shell-safe-text", wake],
        check=False,
    )
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one canonical Relay tmux wake.")
    parser.add_argument("tmux_sender", type=Path)
    parser.add_argument("target")
    parser.add_argument("message_id")
    args = parser.parse_args(argv)
    try:
        return send_wake(args.tmux_sender, args.target, args.message_id)
    except (OSError, ValueError) as error:
        print(f"wake error: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
