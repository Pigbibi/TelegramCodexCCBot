import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault(
    "CLAUDE_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-")
)

import ccbot.session as session_module
from ccbot.session import SessionManager, WindowState


class SessionMapLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_session_map_does_not_drop_existing_window_states(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@1": WindowState(
                session_id="rollout-2026-04-03T17-59-47-019d52c8-d90d-7f72-9062-45cf0f71f97e",
                cwd="/Users/lisiyi/Projects",
                window_name="Projects",
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(json.dumps({}))

            with patch.object(
                session_module.config, "session_map_file", session_map_file
            ):
                await manager.load_session_map()

        self.assertIn("@1", manager.window_states)
        self.assertEqual(
            manager.window_states["@1"].session_id,
            "rollout-2026-04-03T17-59-47-019d52c8-d90d-7f72-9062-45cf0f71f97e",
        )

    async def test_stale_session_map_cwd_does_not_overwrite_live_window(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@7": WindowState(
                session_id="stale-session",
                cwd="/tmp/memory",
                window_name="Projects-3",
            )
        }

        with tempfile.TemporaryDirectory(prefix="ccbot-session-map-") as tmpdir:
            session_map_file = Path(tmpdir) / "session_map.json"
            session_map_file.write_text(
                json.dumps(
                    {
                        "ccbot:@7": {
                            "session_id": "stale-session",
                            "cwd": "/tmp/memory",
                            "window_name": "Projects-3",
                        }
                    }
                )
            )

            with (
                patch.object(
                    session_module.config, "session_map_file", session_map_file
                ),
                patch.object(session_module.config, "tmux_session_name", "ccbot"),
                patch.object(
                    session_module.tmux_manager,
                    "list_windows",
                    AsyncMock(
                        return_value=[
                            SimpleNamespace(
                                window_id="@7",
                                cwd="/tmp/project",
                                window_name="Projects-3",
                            )
                        ]
                    ),
                ),
            ):
                await manager.load_session_map()

        self.assertIn("@7", manager.window_states)
        self.assertEqual(manager.window_states["@7"].session_id, "")
        self.assertEqual(manager.window_states["@7"].cwd, "")


if __name__ == "__main__":
    unittest.main()
