#!/usr/bin/env python3
"""Send one canonical Agent Relay wake through tmux-message."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import re
import signal
import sys
from types import ModuleType


EXIT_USAGE = 64
EXIT_DELIVERY_UNVERIFIED = 4
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
WAKE_RE = re.compile(
    rf"Relay message {UUID_RE.pattern} is queued\. "
    r"Process the Relay inbox and restore exactly one listener\."
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


def _load_tmux_sender(tmux_sender: Path) -> ModuleType:
    module_name = f"tmux_send_for_relay_wake_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(module_name, tmux_sender)
    if spec is None or spec.loader is None:
        raise ValueError(f"could not load tmux sender: {tmux_sender}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _is_complete_non_dim_wake(composer: object) -> bool:
    text = getattr(composer, "text", None)
    return (
        isinstance(text, str)
        and bool(getattr(composer, "has_non_dim_text", False))
        and not bool(getattr(composer, "has_dim_text", True))
        and WAKE_RE.fullmatch(text) is not None
    )


def _emit_reconciliation_unverified(
    stage: str, target: str, pane: object, detail: str = ""
) -> int:
    suffix = f"; detail: {detail}" if detail else ""
    print(
        f"DELIVERY_UNVERIFIED {stage}: existing wake submission is uncertain; "
        f"do not retry automatically (target {target}; pane {pane.pane_id}; "
        f"socket {pane.socket_path}){suffix}",
        file=sys.stderr,
    )
    print(f"DELIVERY_UNVERIFIED {stage}", flush=True)
    return EXIT_DELIVERY_UNVERIFIED


def submit_existing_wake(tmux_sender: Path, target: str) -> int | None:
    """Submit one already-composed canonical wake, or return None without mutation."""
    sender = _load_tmux_sender(tmux_sender)
    pane, _failure_class, _detail = sender.resolve_target(target)
    if pane is None:
        return None
    observed = sender.capture_target(pane)
    if observed.state != sender.OCCUPIED or not _is_complete_non_dim_wake(
        observed.composer
    ):
        return None

    outcome_decided = False

    def raise_before_outcome(signal_number: int, _frame: object) -> None:
        if not outcome_decided:
            raise KeyboardInterrupt(signal_number)

    previous_handlers = {}
    enter_issued = False
    try:
        for signal_number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, raise_before_outcome)
        enter_issued = True
        submit = sender._tmux("send-keys", "-t", pane.pane_id, "Enter")
        if submit.returncode != 0:
            outcome_decided = True
            return _emit_reconciliation_unverified(
                "wake-enter-failed", target, pane, submit.stderr.strip()
            )
        cleared = sender._wait_for_clear(pane, observed)
        if not sender._submit_cleared(cleared, observed):
            outcome_decided = True
            return _emit_reconciliation_unverified(
                "wake-not-cleared", target, pane, cleared.detail
            )
        outcome_decided = True
        print("SENT", flush=True)
        return 0
    except KeyboardInterrupt as error:
        outcome_decided = True
        if enter_issued:
            return _emit_reconciliation_unverified(
                "wake-interrupted", target, pane, type(error).__name__
            )
        raise
    except Exception as error:
        outcome_decided = True
        if enter_issued:
            return _emit_reconciliation_unverified(
                "wake-internal-error", target, pane, type(error).__name__
            )
        raise
    finally:
        for signal_number, handler in previous_handlers.items():
            signal.signal(signal_number, handler)


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
        if signal.sigpending() & managed_signals:
            raise InterruptedError("interrupted before sender started")
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
        exit_code = os.waitstatus_to_exitcode(status)
        return exit_code if exit_code >= 0 else EXIT_DELIVERY_UNVERIFIED
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
        if not args.tmux_sender.is_file():
            raise ValueError(
                f"tmux sender is not a regular file: {args.tmux_sender}"
            )
        canonical_wake(args.message_id)
        reconciled = submit_existing_wake(args.tmux_sender, args.target)
        if reconciled is not None:
            return reconciled
        return send_wake(args.tmux_sender, args.target, args.message_id)
    except KeyboardInterrupt:
        print("wake error: interrupted", file=sys.stderr)
        return EXIT_USAGE
    except (OSError, UsageError, ValueError) as error:
        print(f"wake error: {error}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
