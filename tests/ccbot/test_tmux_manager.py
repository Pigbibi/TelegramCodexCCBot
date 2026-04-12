import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault(
    "CLAUDE_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-")
)

import ccbot.tmux_manager as tmux_manager_module


class _DummyPane:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool]] = []

    def send_keys(self, cmd: str, enter: bool = False) -> None:
        self.commands.append((cmd, enter))


class _DummyWindow:
    def __init__(self, pane: _DummyPane) -> None:
        self.window_id = "@9"
        self.active_pane = pane
        self.window_options: list[tuple[str, str]] = []

    def set_window_option(self, name: str, value: str) -> None:
        self.window_options.append((name, value))


class _DummySession:
    def __init__(self, window: _DummyWindow) -> None:
        self.window = window
        self.created: tuple[str, str] | None = None

    def new_window(self, window_name: str, start_directory: str) -> _DummyWindow:
        self.created = (window_name, start_directory)
        return self.window


class CreateWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_window_uses_resume_subcommand(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with tempfile.TemporaryDirectory(prefix="ccbot-workdir-") as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/local/bin/codex --search -s danger-full-access",
                ),
                patch(
                    "ccbot.tmux_manager.ensure_account_home",
                    return_value=Path("/tmp/ccbot-account-home"),
                ),
            ):
                ok, _msg, _window_name, window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                    resume_session_id="sid-123",
                    account_name="plus1",
                )

        self.assertTrue(ok)
        self.assertEqual(window_id, "@9")
        self.assertEqual(session.created, ("Projects", str(Path(tmpdir).resolve())))
        self.assertEqual(window.window_options, [("allow-rename", "off")])
        self.assertEqual(
            pane.commands,
            [
                (
                    "export CODEX_HOME=/tmp/ccbot-account-home; "
                    "/usr/local/bin/codex --search -s danger-full-access resume sid-123",
                    True,
                )
            ],
        )

    async def test_create_window_strips_rollout_prefix_for_resume(self) -> None:
        pane = _DummyPane()
        window = _DummyWindow(pane)
        session = _DummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with tempfile.TemporaryDirectory(prefix="ccbot-workdir-") as tmpdir:
            with (
                patch.object(
                    manager, "find_window_by_name", AsyncMock(return_value=None)
                ),
                patch.object(manager, "get_or_create_session", return_value=session),
                patch.object(
                    tmux_manager_module.config,
                    "codex_command",
                    "/usr/local/bin/codex --search -s danger-full-access",
                ),
            ):
                ok, _msg, _window_name, _window_id = await manager.create_window(
                    tmpdir,
                    window_name="Projects",
                    resume_session_id=(
                        "rollout-2026-04-03T17-59-47-"
                        "019d52c8-d90d-7f72-9062-45cf0f71f97e"
                    ),
                )

        self.assertTrue(ok)
        self.assertEqual(
            pane.commands,
            [
                (
                    "/usr/local/bin/codex --search -s danger-full-access resume "
                    "019d52c8-d90d-7f72-9062-45cf0f71f97e",
                    True,
                )
            ],
        )


class _SendKeysDummyPane:
    def __init__(self) -> None:
        self.commands: list[tuple[str, bool, bool]] = []

    def send_keys(self, cmd: str, enter: bool = False, literal: bool = False) -> None:
        self.commands.append((cmd, enter, literal))


class _SendKeysDummyWindow:
    def __init__(self, pane: _SendKeysDummyPane) -> None:
        self.window_id = "@9"
        self.active_pane = pane


class _SendKeysWindows:
    def __init__(self, window: _SendKeysDummyWindow) -> None:
        self._window = window

    def get(self, *, window_id: str) -> _SendKeysDummyWindow | None:
        if window_id == self._window.window_id:
            return self._window
        return None


class _SendKeysDummySession:
    def __init__(self, window: _SendKeysDummyWindow) -> None:
        self.windows = _SendKeysWindows(window)


class SendKeysTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_keys_uses_tmux_cli_for_enter_after_literal_text(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"], returncode=0
                ),
            ) as run_mock,
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(pane.commands, [("hello", False, True)])
        run_mock.assert_called_once_with(
            ["tmux", "send-keys", "-t", "@9", "Enter"],
            capture_output=True,
            text=True,
            check=False,
        )

    async def test_send_keys_falls_back_to_libtmux_when_cli_enter_fails(self) -> None:
        pane = _SendKeysDummyPane()
        window = _SendKeysDummyWindow(pane)
        session = _SendKeysDummySession(window)
        manager = tmux_manager_module.TmuxManager(session_name="ccbot-test")

        with (
            patch.object(manager, "get_session", return_value=session),
            patch(
                "ccbot.tmux_manager.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["tmux", "send-keys"],
                    returncode=1,
                    stderr="send failed",
                ),
            ),
        ):
            ok = await manager.send_keys("@9", "hello")

        self.assertTrue(ok)
        self.assertEqual(
            pane.commands,
            [
                ("hello", False, True),
                ("", True, False),
            ],
        )


if __name__ == "__main__":
    unittest.main()
