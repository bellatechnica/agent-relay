from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "agent-relay-message"
    / "scripts"
    / "send_tmux_wake.py"
)
SPEC = importlib.util.spec_from_file_location("send_tmux_wake", SCRIPT)
assert SPEC and SPEC.loader
send_tmux_wake = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = send_tmux_wake
SPEC.loader.exec_module(send_tmux_wake)


MESSAGE_ID = "073d8462-5295-4c49-91ec-42a5dbc49187"


class TmuxWakeTests(unittest.TestCase):
    def test_valid_wake_uses_tmux_sender_shell_safe_text(self) -> None:
        observed = {}

        def posix_spawn(path, arguments, environment, **kwargs):
            observed.update(
                path=path,
                arguments=arguments,
                environment=environment,
                kwargs=kwargs,
            )
            return 321

        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake.os, "posix_spawn", side_effect=posix_spawn
                ),
                mock.patch.object(
                    send_tmux_wake.os, "waitpid", return_value=(321, 2 << 8)
                ),
            ):
                code = send_tmux_wake.send_wake(
                    sender, "session:window.0", MESSAGE_ID
                )

        self.assertEqual(code, 2)
        self.assertEqual(
            observed["arguments"],
            [
                sys.executable,
                str(sender),
                "session:window.0",
                "--shell-safe-text",
                f"Relay message {MESSAGE_ID} is queued. "
                "Process the Relay inbox and restore exactly one listener.",
            ],
        )
        self.assertEqual(observed["path"], sys.executable)
        self.assertIs(observed["environment"], send_tmux_wake.os.environ)
        self.assertIn("setsigmask", observed["kwargs"])

    def test_parent_signal_during_wait_leaves_child_to_report(self) -> None:
        def waitpid(_pid, _options):
            handler = send_tmux_wake.signal.getsignal(
                send_tmux_wake.signal.SIGINT
            )
            handler(send_tmux_wake.signal.SIGINT, None)
            return 321, 4 << 8

        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake.os, "posix_spawn", return_value=321
                ),
                mock.patch.object(
                    send_tmux_wake.os, "waitpid", side_effect=waitpid
                ),
            ):
                code = send_tmux_wake.send_wake(
                    sender, "session:window.0", MESSAGE_ID
                )
        self.assertEqual(code, 4)

    def test_uppercase_uuid_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase"):
            send_tmux_wake.canonical_wake(MESSAGE_ID.upper())

    def test_extra_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            send_tmux_wake.canonical_wake(f"{MESSAGE_ID} extra")

    def test_missing_tmux_sender_returns_usage(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.py"
            with mock.patch("sys.stderr", stderr):
                code = send_tmux_wake.main(
                    [str(missing), "session:window.0", MESSAGE_ID]
                )
        self.assertEqual(code, 64)
        self.assertIn("not a regular file", stderr.getvalue())

    def test_missing_arguments_return_usage_not_dialog(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = send_tmux_wake.main([])
        self.assertEqual(code, 64)
        self.assertIn("wake error", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
