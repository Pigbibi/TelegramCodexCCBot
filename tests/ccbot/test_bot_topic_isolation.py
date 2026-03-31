"""Regression tests for keeping Telegram topics isolated by Codex session."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_DIR_CONFIRM, CB_SESSION_SELECT
from ccbot.handlers.directory_browser import BROWSE_PATH_KEY, SESSIONS_KEY
from ccbot.session import CodexSession


def _make_callback_update(data: str, thread_id: int = 42, user_id: int = 12345):
    """Build a minimal callback-query update in a forum topic."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
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
    """Build a minimal callback context."""
    context = MagicMock()
    context.bot = AsyncMock()
    context.user_data = {}
    return context


class TestSessionPickerIsolation:
    @pytest.mark.asyncio
    async def test_dir_confirm_skips_active_sessions_and_creates_new_window(self):
        """When only already-active sessions exist, directory confirm starts fresh."""
        update, query = _make_callback_update(CB_DIR_CONFIRM)
        context = _make_context()
        context.user_data = {
            BROWSE_PATH_KEY: "/tmp/project",
            "_pending_thread_id": 42,
            "_pending_thread_text": "hello",
        }
        active_session = CodexSession(
            session_id="session-a",
            summary="Existing chat",
            message_count=12,
            file_path="/tmp/project/session-a.jsonl",
        )

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as create,
            patch("ccbot.bot.build_session_picker") as build_picker,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock),
        ):
            mock_sm.list_sessions_for_directory = AsyncMock(
                return_value=[active_session]
            )
            mock_sm.has_bound_thread_for_session.return_value = True

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        create.assert_called_once_with(
            query,
            context,
            update.effective_user,
            "/tmp/project",
            42,
        )
        build_picker.assert_not_called()

    @pytest.mark.asyncio
    async def test_session_select_rejects_session_already_active_elsewhere(self):
        """Session picker must reject selecting a session already bound to a topic."""
        update, query = _make_callback_update(f"{CB_SESSION_SELECT}0")
        context = _make_context()
        context.user_data = {
            SESSIONS_KEY: [
                CodexSession(
                    session_id="session-a",
                    summary="Existing chat",
                    message_count=12,
                    file_path="/tmp/project/session-a.jsonl",
                )
            ],
            "_selected_path": "/tmp/project",
            "_pending_thread_id": 42,
        }

        with (
            patch("ccbot.bot.is_user_allowed", return_value=True),
            patch("ccbot.bot._get_thread_id", return_value=42),
            patch("ccbot.bot.session_manager") as mock_sm,
            patch(
                "ccbot.bot._create_and_bind_window", new_callable=AsyncMock
            ) as create,
            patch("ccbot.bot.safe_edit", new_callable=AsyncMock) as safe_edit,
        ):
            mock_sm.has_bound_thread_for_session.return_value = True

            from ccbot.bot import callback_handler

            await callback_handler(update, context)

        create.assert_not_called()
        safe_edit.assert_called_once()
        assert "already active" in safe_edit.await_args.args[1]
