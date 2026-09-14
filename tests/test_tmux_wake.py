from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
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
OTHER_MESSAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def fake_sender_module(composer, *, cleared: bool = True):
    pane = SimpleNamespace(pane_id="%9", socket_path="/tmp/tmux.sock")
    observed = SimpleNamespace(state="OCCUPIED", composer=composer, detail="")
    clear_result = SimpleNamespace(state="CLEAR", composer=None, detail="")
    return SimpleNamespace(
        OCCUPIED="OCCUPIED",
        resolve_target=mock.Mock(return_value=(pane, "", "")),
        capture_target=mock.Mock(return_value=observed),
        _tmux=mock.Mock(return_value=SimpleNamespace(returncode=0, stderr="")),
        _wait_for_clear=mock.Mock(return_value=clear_result),
        _submit_cleared=mock.Mock(return_value=cleared),
    )


class TmuxWakeTests(unittest.TestCase):
    def test_existing_canonical_wake_is_submitted_once(self) -> None:
        composer = SimpleNamespace(
            text=send_tmux_wake.canonical_wake(OTHER_MESSAGE_ID),
            has_non_dim_text=True,
            has_dim_text=False,
        )
        sender_module = fake_sender_module(composer)
        stdout = io.StringIO()
        with (
            mock.patch.object(
                send_tmux_wake, "_load_tmux_sender", return_value=sender_module
            ),
            mock.patch("sys.stdout", stdout),
        ):
            code = send_tmux_wake.submit_existing_wake(
                Path("tmux_send.py"), "session:window.0"
            )
        self.assertEqual((code, stdout.getvalue()), (0, "SENT\n"))
        sender_module._tmux.assert_called_once_with(
            "send-keys", "-t", "%9", "Enter"
        )
        sender_module._wait_for_clear.assert_called_once()

    def test_dim_or_mixed_wake_is_not_submitted(self) -> None:
        for has_dim, suffix in ((True, ""), (True, " extra"), (False, " extra")):
            with self.subTest(has_dim=has_dim, suffix=suffix):
                composer = SimpleNamespace(
                    text=send_tmux_wake.canonical_wake(MESSAGE_ID) + suffix,
                    has_non_dim_text=True,
                    has_dim_text=has_dim,
                )
                sender_module = fake_sender_module(composer)
                with mock.patch.object(
                    send_tmux_wake,
                    "_load_tmux_sender",
                    return_value=sender_module,
                ):
                    code = send_tmux_wake.submit_existing_wake(
                        Path("tmux_send.py"), "session:window.0"
                    )
                self.assertIsNone(code)
                sender_module._tmux.assert_not_called()

    def test_existing_wake_that_does_not_clear_is_unverified(self) -> None:
        secret = send_tmux_wake.canonical_wake(MESSAGE_ID)
        composer = SimpleNamespace(
            text=secret,
            has_non_dim_text=True,
            has_dim_text=False,
        )
        sender_module = fake_sender_module(composer, cleared=False)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                send_tmux_wake, "_load_tmux_sender", return_value=sender_module
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            code = send_tmux_wake.submit_existing_wake(
                Path("tmux_send.py"), "session:window.0"
            )
        self.assertEqual(
            (code, stdout.getvalue()),
            (4, "DELIVERY_UNVERIFIED wake-not-cleared\n"),
        )
        self.assertNotIn(secret, stderr.getvalue())

    def test_exception_after_reconciliation_enter_is_unverified(self) -> None:
        composer = SimpleNamespace(
            text=send_tmux_wake.canonical_wake(MESSAGE_ID),
            has_non_dim_text=True,
            has_dim_text=False,
        )
        sender_module = fake_sender_module(composer)
        sender_module._wait_for_clear.side_effect = RuntimeError("private pane data")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.object(
                send_tmux_wake, "_load_tmux_sender", return_value=sender_module
            ),
            mock.patch("sys.stdout", stdout),
            mock.patch("sys.stderr", stderr),
        ):
            code = send_tmux_wake.submit_existing_wake(
                Path("tmux_send.py"), "session:window.0"
            )
        self.assertEqual(
            (code, stdout.getvalue()),
            (4, "DELIVERY_UNVERIFIED wake-internal-error\n"),
        )
        self.assertNotIn("private pane data", stderr.getvalue())

    def test_main_reconciles_before_starting_sender(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake, "submit_existing_wake", return_value=0
                ) as reconcile,
                mock.patch.object(send_tmux_wake, "send_wake") as send,
            ):
                code = send_tmux_wake.main(
                    [str(sender), "session:window.0", MESSAGE_ID]
                )
        self.assertEqual(code, 0)
        reconcile.assert_called_once_with(sender, "session:window.0")
        send.assert_not_called()

    def test_invalid_requested_uuid_is_rejected_before_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with mock.patch.object(
                send_tmux_wake, "submit_existing_wake"
            ) as reconcile, mock.patch("sys.stderr", io.StringIO()):
                code = send_tmux_wake.main(
                    [str(sender), "session:window.0", "not-a-uuid"]
                )
        self.assertEqual(code, 64)
        reconcile.assert_not_called()

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

    def test_pending_signal_prevents_sender_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake.signal,
                    "sigpending",
                    return_value={send_tmux_wake.signal.SIGINT},
                ),
                mock.patch.object(send_tmux_wake.os, "posix_spawn") as spawn,
            ):
                with self.assertRaisesRegex(
                    InterruptedError, "before sender started"
                ):
                    send_tmux_wake.send_wake(
                        sender, "session:window.0", MESSAGE_ID
                    )
        spawn.assert_not_called()

    def test_signal_killed_sender_maps_to_delivery_unverified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake.os, "posix_spawn", return_value=321
                ),
                mock.patch.object(
                    send_tmux_wake.os, "waitpid", return_value=(321, 9)
                ),
                mock.patch.object(
                    send_tmux_wake.os,
                    "waitstatus_to_exitcode",
                    return_value=-9,
                ),
            ):
                code = send_tmux_wake.send_wake(
                    sender, "session:window.0", MESSAGE_ID
                )
        self.assertEqual(code, 4)

    def test_main_turns_keyboard_interrupt_into_usage_result(self) -> None:
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            sender = Path(directory) / "tmux_send.py"
            sender.write_text("# test sender\n", encoding="utf-8")
            with (
                mock.patch.object(
                    send_tmux_wake,
                    "submit_existing_wake",
                    side_effect=KeyboardInterrupt,
                ),
                mock.patch("sys.stderr", stderr),
            ):
                code = send_tmux_wake.main(
                    [str(sender), "session:window.0", MESSAGE_ID]
                )
        self.assertEqual(code, 64)
        self.assertEqual(stderr.getvalue(), "wake error: interrupted\n")

    def test_interrupt_during_handler_setup_restores_changed_handlers(self) -> None:
        composer = SimpleNamespace(
            text=send_tmux_wake.canonical_wake(MESSAGE_ID),
            has_non_dim_text=True,
            has_dim_text=False,
        )
        sender_module = fake_sender_module(composer)
        old_handlers = {
            send_tmux_wake.signal.SIGINT: object(),
            send_tmux_wake.signal.SIGTERM: object(),
        }
        calls = []

        def getsignal(signal_number):
            return old_handlers.get(signal_number, object())

        def set_signal(signal_number, handler):
            calls.append((signal_number, handler))
            if len(calls) == 2:
                raise KeyboardInterrupt

        with (
            mock.patch.object(
                send_tmux_wake, "_load_tmux_sender", return_value=sender_module
            ),
            mock.patch.object(
                send_tmux_wake.signal, "getsignal", side_effect=getsignal
            ),
            mock.patch.object(
                send_tmux_wake.signal, "signal", side_effect=set_signal
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                send_tmux_wake.submit_existing_wake(
                    Path("tmux_send.py"), "session:window.0"
                )

        self.assertIn(
            (send_tmux_wake.signal.SIGINT, old_handlers[send_tmux_wake.signal.SIGINT]),
            calls,
        )
        self.assertIn(
            (
                send_tmux_wake.signal.SIGTERM,
                old_handlers[send_tmux_wake.signal.SIGTERM],
            ),
            calls,
        )
        sender_module._tmux.assert_not_called()

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
