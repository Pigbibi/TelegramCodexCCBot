"""Tests for binding existing tmux windows to topics."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_WIN_BIND


def _make_text_update(text: str, user_id: int = 12345, thread_id: int = 42):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.message = MagicMock()
    update.message.text = text
    update.message.message_thread_id = thread_id
    update.message.chat = MagicMock()
    update.message.chat.type = "supergroup"
    update.message.chat.send_action = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    return update


def _make_callback_update(data: str, thread_id: int = 42, user_id: int = 12345):
    update = MagicMock()
    update.effective_user = MagicMock(id=user_id)
    update.effective_chat = MagicMock()
    update.effective_chat.type = "supergroup"
    update.effective_chat.id = -1001234567890
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = MagicMock()
    query.message.message_thread_id = thread_id
    update.callback_query = query
    return update, query


def _make_context():
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestExistingWindowBinding:
    @pytest.mark.asyncio
    async def test_untracked_unbound_windows_fall_back_to_directory_browser(self):
        update = _make_text_update("hi")
        context = _make_context()

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"
        fake_window.cwd = "/tmp/project"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.build_directory_browser") as build_directory_browser,
            patch("ccbot.bot.safe_reply", new_callable=AsyncMock) as safe_reply,
        ):
            mock_tmux.list_windows = AsyncMock(return_value=[fake_window])
            mock_sm.iter_thread_bindings.return_value = []
            mock_sm.get_window_for_thread.return_value = None
            mock_sm.window_states = {}
            build_directory_browser.return_value = ("pick dir", "kbd", ["src"])

            from ccbot.bot import text_handler

            await text_handler(update, context)

        safe_reply.assert_awaited_once_with(
            update.message, "pick dir", reply_markup="kbd"
        )
        assert context.user_data["_pending_thread_id"] == 42
        assert context.user_data["_pending_thread_text"] == "hi"

    @pytest.mark.asyncio
    async def test_window_picker_rejects_untracked_window(self):
        update, query = _make_callback_update(f"{CB_WIN_BIND}0")
        context = _make_context()
        context.user_data = {
            "unbound_windows": ["@1"],
            "_pending_thread_id": 42,
            "_pending_thread_text": "hi",
        }

        fake_window = MagicMock()
        fake_window.window_id = "@1"
        fake_window.window_name = "Projects"

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch("ccbot.bot.tmux_manager") as mock_tmux,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as safe_edit,
        ):
            mock_tmux.find_window_by_id = AsyncMock(return_value=fake_window)
            mock_sm.window_states = {}

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        safe_edit.assert_not_called()
        query.answer.assert_awaited_once()
        assert query.answer.await_args.args == (
            "This window has no tracked Codex session yet. Please choose New Session instead.",
        )
        assert query.answer.await_args.kwargs["show_alert"] is True
