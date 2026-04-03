"""Tests for topic-close cleanup behavior."""

import os
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault(
    "CCBOT_CODEX_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-")
)

import ccbot.bot as bot_module
from ccbot.handlers.cleanup import clear_topic_state
from ccbot.session import SessionManager, WindowState, session_manager


class SessionManagerCleanupTests(unittest.TestCase):
    def test_remove_window_state_clears_related_persisted_state(self) -> None:
        manager = SessionManager()
        manager.window_states = {
            "@1": WindowState(session_id="sid-1", cwd="/tmp", window_name="Projects"),
            "@2": WindowState(
                session_id="sid-2",
                cwd="/tmp/other",
                window_name="Other",
            ),
        }
        manager.window_display_names = {"@1": "Projects", "@2": "Other"}
        manager.user_window_offsets = {1: {"@1": 10, "@2": 20}, 2: {"@1": 30}}
        manager.thread_bindings = {1: {106: "@1", 107: "@2"}, 2: {206: "@1"}}

        with patch.object(manager, "_save_state") as save_mock:
            manager.remove_window_state("@1")

        assert "@1" not in manager.window_states
        assert "@1" not in manager.window_display_names
        assert manager.user_window_offsets == {1: {"@2": 20}}
        assert manager.thread_bindings == {1: {107: "@2"}}
        save_mock.assert_called_once()


class TopicStateCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_clear_topic_state_clears_group_chat_id_and_picker_state(
        self,
    ) -> None:
        original_group_chat_ids = dict(session_manager.group_chat_ids)
        session_manager.group_chat_ids = {"1:106": -100123}
        user_data = {
            "state": "selecting_window",
            "_pending_thread_id": 106,
            "_pending_thread_text": "hello",
            "_selected_path": "/tmp",
        }

        try:
            await clear_topic_state(1, 106, None, user_data)
        finally:
            session_manager.group_chat_ids = original_group_chat_ids

        assert "1:106" not in session_manager.group_chat_ids
        assert "state" not in user_data
        assert "_pending_thread_id" not in user_data
        assert "_pending_thread_text" not in user_data
        assert "_selected_path" not in user_data

    async def test_topic_closed_handler_deletes_topic_and_clears_window_state(
        self,
    ) -> None:
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=1),
            effective_chat=types.SimpleNamespace(id=-100123),
            message=types.SimpleNamespace(message_thread_id=106),
            callback_query=None,
        )
        context = types.SimpleNamespace(
            bot=types.SimpleNamespace(delete_forum_topic=AsyncMock()),
            user_data={},
        )
        fake_monitor_state = types.SimpleNamespace(
            remove_session=Mock(),
            save_if_dirty=Mock(),
        )

        with (
            patch.object(bot_module, "is_user_allowed", return_value=True),
            patch.dict(
                bot_module.session_manager.window_states,
                {"@1": types.SimpleNamespace(session_id="sid-1")},
                clear=True,
            ),
            patch.object(
                bot_module.session_manager,
                "get_window_for_thread",
                return_value="@1",
            ),
            patch.object(
                bot_module.session_manager,
                "get_display_name",
                return_value="Projects",
            ),
            patch.object(bot_module.session_manager, "unbind_thread") as unbind_mock,
            patch.object(
                bot_module.session_manager,
                "remove_session_map_entry",
                AsyncMock(),
            ) as remove_session_map_entry_mock,
            patch.object(
                bot_module.session_manager,
                "remove_window_state",
            ) as remove_window_state_mock,
            patch.object(
                bot_module,
                "clear_topic_state",
                AsyncMock(),
            ) as clear_topic_state_mock,
            patch.object(
                bot_module.tmux_manager,
                "find_window_by_id",
                AsyncMock(return_value=types.SimpleNamespace(window_id="@1")),
            ),
            patch.object(
                bot_module.tmux_manager,
                "kill_window",
                AsyncMock(),
            ) as kill_window_mock,
            patch.object(
                bot_module,
                "session_monitor",
                types.SimpleNamespace(state=fake_monitor_state),
            ),
        ):
            await bot_module.topic_closed_handler(update, context)

        kill_window_mock.assert_awaited_once_with("@1")
        unbind_mock.assert_called_once_with(1, 106)
        remove_session_map_entry_mock.assert_awaited_once_with("@1")
        remove_window_state_mock.assert_called_once_with("@1")
        clear_topic_state_mock.assert_awaited_once_with(
            1, 106, context.bot, context.user_data
        )
        context.bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=-100123,
            message_thread_id=106,
        )
        fake_monitor_state.remove_session.assert_called_once_with("sid-1")
        fake_monitor_state.save_if_dirty.assert_called_once()

    async def test_topic_closed_handler_deletes_unbound_topic(self) -> None:
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=1),
            effective_chat=types.SimpleNamespace(id=-100123),
            message=types.SimpleNamespace(message_thread_id=106),
            callback_query=None,
        )
        context = types.SimpleNamespace(
            bot=types.SimpleNamespace(delete_forum_topic=AsyncMock()),
            user_data={},
        )

        with (
            patch.object(bot_module, "is_user_allowed", return_value=True),
            patch.object(
                bot_module.session_manager,
                "get_window_for_thread",
                return_value=None,
            ),
            patch.object(
                bot_module,
                "clear_topic_state",
                AsyncMock(),
            ) as clear_topic_state_mock,
        ):
            await bot_module.topic_closed_handler(update, context)

        clear_topic_state_mock.assert_awaited_once_with(
            1, 106, context.bot, context.user_data
        )
        context.bot.delete_forum_topic.assert_awaited_once_with(
            chat_id=-100123,
            message_thread_id=106,
        )


if __name__ == "__main__":
    unittest.main()
