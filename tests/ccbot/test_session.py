"""Tests for SessionManager pure dict operations."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ccbot.config import config
from ccbot import session as session_module
from ccbot.session import SessionManager


@pytest.fixture
def mgr(monkeypatch) -> SessionManager:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
    return SessionManager()


class TestThreadBindings:
    def test_bind_and_get(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        assert mgr.get_window_for_thread(100, 1) == "@1"

    def test_bind_unbind_get_returns_none(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.unbind_thread(100, 1)
        assert mgr.get_window_for_thread(100, 1) is None

    def test_unbind_nonexistent_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.unbind_thread(100, 999) is None

    def test_iter_thread_bindings(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        mgr.bind_thread(100, 2, "@2")
        mgr.bind_thread(200, 3, "@3")
        result = set(mgr.iter_thread_bindings())
        assert result == {(100, 1, "@1"), (100, 2, "@2"), (200, 3, "@3")}


class TestGroupChatId:
    """Tests for group chat_id routing (supergroup forum topic support).

    IMPORTANT: These tests protect against regression. The group_chat_ids
    mapping is required for Telegram supergroup forum topics — without it,
    all outbound messages fail with "Message thread not found". This was
    erroneously removed once (26cb81f) and restored in PR #23. Do NOT
    delete these tests or the underlying functionality.
    """

    def test_resolve_with_stored_group_id(self, mgr: SessionManager) -> None:
        """resolve_chat_id returns stored group chat_id for known thread."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100, 1) == -1001234567890

    def test_resolve_without_group_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id falls back to user_id when no group_id stored."""
        assert mgr.resolve_chat_id(100, 1) == 100

    def test_resolve_none_thread_id_falls_back_to_user_id(
        self, mgr: SessionManager
    ) -> None:
        """resolve_chat_id returns user_id when thread_id is None (private chat)."""
        mgr.set_group_chat_id(100, 1, -1001234567890)
        assert mgr.resolve_chat_id(100) == 100

    def test_set_group_chat_id_overwrites(self, mgr: SessionManager) -> None:
        """set_group_chat_id updates the stored value on change."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.set_group_chat_id(100, 1, -888)
        assert mgr.resolve_chat_id(100, 1) == -888

    def test_multiple_threads_independent(self, mgr: SessionManager) -> None:
        """Different threads for the same user store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(100, 2, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(100, 2) == -222

    def test_multiple_users_independent(self, mgr: SessionManager) -> None:
        """Different users store independent group chat_ids."""
        mgr.set_group_chat_id(100, 1, -111)
        mgr.set_group_chat_id(200, 1, -222)
        assert mgr.resolve_chat_id(100, 1) == -111
        assert mgr.resolve_chat_id(200, 1) == -222

    def test_set_group_chat_id_with_none_thread(self, mgr: SessionManager) -> None:
        """set_group_chat_id handles None thread_id (mapped to 0)."""
        mgr.set_group_chat_id(100, None, -999)
        # thread_id=None in resolve falls back to user_id (by design)
        assert mgr.resolve_chat_id(100, None) == 100
        # The stored key is "100:0", only accessible with explicit thread_id=0
        assert mgr.group_chat_ids.get("100:0") == -999

    def test_clear_group_chat_id_removes_mapping(self, mgr: SessionManager) -> None:
        """clear_group_chat_id removes the stored topic routing override."""
        mgr.set_group_chat_id(100, 1, -999)
        mgr.clear_group_chat_id(100, 1)
        assert mgr.resolve_chat_id(100, 1) == 100
        assert "100:1" not in mgr.group_chat_ids


class TestWindowState:
    def test_get_creates_new(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@0")
        assert state.session_id == ""
        assert state.cwd == ""

    def test_get_returns_existing(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        assert mgr.get_window_state("@1").session_id == "abc"

    def test_clear_window_session(self, mgr: SessionManager) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "abc"
        mgr.clear_window_session("@1")
        assert mgr.get_window_state("@1").session_id == ""

    def test_prepare_window_launch_sets_account_and_clears_quota_flag(
        self, mgr: SessionManager
    ) -> None:
        state = mgr.get_window_state("@1")
        state.session_id = "old-session"
        state.usage_limit_exceeded = True

        mgr.prepare_window_launch(
            "@1",
            cwd="/tmp/project",
            window_name="project",
            account_name="team",
        )

        refreshed = mgr.get_window_state("@1")
        assert refreshed.session_id == ""
        assert refreshed.cwd == "/tmp/project"
        assert refreshed.window_name == "project"
        assert refreshed.account_name == "team"
        assert refreshed.usage_limit_exceeded is False

    def test_mark_window_usage_limit_exceeded_is_idempotent(
        self, mgr: SessionManager
    ) -> None:
        assert mgr.mark_window_usage_limit_exceeded("@1", True) is True
        assert mgr.get_window_state("@1").usage_limit_exceeded is True
        assert mgr.mark_window_usage_limit_exceeded("@1", True) is False
        assert mgr.mark_window_usage_limit_exceeded("@1", False) is True
        assert mgr.get_window_state("@1").usage_limit_exceeded is False

    def test_register_session_to_window_unhides_session(
        self, mgr: SessionManager
    ) -> None:
        mgr.hidden_session_ids.add("sid-1")

        mgr.register_session_to_window("@1", "sid-1", "/tmp/project")

        assert "sid-1" not in mgr.hidden_session_ids


class TestHiddenSessions:
    def test_hide_and_unhide_session(self, mgr: SessionManager) -> None:
        assert mgr.hide_session("sid-1") is True
        assert mgr.is_session_hidden("sid-1") is True
        assert mgr.hide_session("sid-1") is False

        assert mgr.unhide_session("sid-1") is True
        assert mgr.is_session_hidden("sid-1") is False
        assert mgr.unhide_session("sid-1") is False

    def test_hide_and_unhide_session_canonicalizes_rollout_ids(
        self, mgr: SessionManager
    ) -> None:
        bare_id = "019d5147-fb09-7873-8207-3209213c574b"
        rollout_id = f"rollout-2026-04-03T10-59-25-{bare_id}"

        assert mgr.hide_session(bare_id) is True
        assert bare_id in mgr.hidden_session_ids
        assert rollout_id not in mgr.hidden_session_ids
        assert mgr.is_session_hidden(rollout_id) is True

        assert mgr.unhide_session(rollout_id) is True
        assert mgr.is_session_hidden(bare_id) is False

    @pytest.mark.asyncio
    async def test_list_sessions_for_directory_skips_hidden_sessions(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        codex_root = tmp_path / "codex"
        monkeypatch.setattr(config, "codex_projects_path", codex_root)

        def write_session(session_id: str, *, summary: str) -> None:
            session_dir = codex_root / mgr._encode_cwd(str(project_dir))
            session_dir.mkdir(parents=True, exist_ok=True)
            payload = [
                {"cwd": str(project_dir)},
                {"type": "summary", "summary": summary},
            ]
            (session_dir / f"{session_id}.jsonl").write_text(
                "\n".join(json.dumps(item) for item in payload) + "\n",
                encoding="utf-8",
            )

        write_session("visible-session", summary="Visible")
        write_session("hidden-session", summary="Hidden")
        mgr.hide_session("hidden-session")

        sessions = await mgr.list_sessions_for_directory(str(project_dir))

        assert [session.session_id for session in sessions] == ["visible-session"]
        assert sessions[0].message_count == 0

    @pytest.mark.asyncio
    async def test_list_sessions_for_directory_skips_hidden_rollout_session(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        codex_root = tmp_path / "codex"
        monkeypatch.setattr(config, "codex_projects_path", codex_root)

        session_dir = codex_root / mgr._encode_cwd(str(project_dir))
        session_dir.mkdir(parents=True, exist_ok=True)

        bare_id = "019d5147-fb09-7873-8207-3209213c574b"
        rollout_id = f"rollout-2026-04-03T10-59-25-{bare_id}"

        payload = [
            {"cwd": str(project_dir)},
            {"type": "summary", "summary": "Hidden"},
        ]
        (session_dir / f"{rollout_id}.jsonl").write_text(
            "\n".join(json.dumps(item) for item in payload) + "\n",
            encoding="utf-8",
        )

        mgr.hide_session(bare_id)
        sessions = await mgr.list_sessions_for_directory(str(project_dir))

        assert sessions == []

    @pytest.mark.asyncio
    async def test_list_sessions_for_directory_uses_matched_file_directly(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        codex_root = tmp_path / "codex"
        archived_dir = codex_root / "archived_sessions"
        archived_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, "codex_projects_path", codex_root)

        session_file = archived_dir / "archived-session.jsonl"
        payload = [
            {"cwd": str(project_dir)},
            {"type": "summary", "summary": "Archived"},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "text", "text": "hi"}],
            },
        ]
        session_file.write_text(
            "\n".join(json.dumps(item) for item in payload) + "\n",
            encoding="utf-8",
        )

        get_session_direct = AsyncMock(
            side_effect=AssertionError("unexpected fallback")
        )
        monkeypatch.setattr(mgr, "_get_session_direct", get_session_direct)

        sessions = await mgr.list_sessions_for_directory(str(project_dir))

        assert [session.session_id for session in sessions] == ["archived-session"]
        assert sessions[0].message_count == 0
        get_session_direct.assert_not_called()

    @pytest.mark.asyncio
    async def test_list_sessions_for_directory_reads_rollout_user_preview(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        codex_root = tmp_path / "codex"
        monkeypatch.setattr(config, "codex_projects_path", codex_root)

        session_dir = codex_root / mgr._encode_cwd(str(project_dir))
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = (
            session_dir
            / "rollout-2026-04-03T17-59-47-019d52c8-d90d-7f72-9062-45cf0f71f97e.jsonl"
        )
        payload = [
            {
                "timestamp": "2026-04-03T09:59:52.972Z",
                "type": "session_meta",
                "payload": {
                    "id": "019d52c8-d90d-7f72-9062-45cf0f71f97e",
                    "cwd": str(project_dir),
                },
            },
            {
                "timestamp": "2026-04-03T09:59:52.974Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "<environment_context>\n  <cwd>/tmp/project</cwd>\n</environment_context>",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-04-03T09:59:53.256Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "你好 codex"},
            },
        ]
        session_file.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in payload) + "\n",
            encoding="utf-8",
        )

        sessions = await mgr.list_sessions_for_directory(str(project_dir))

        assert [session.summary for session in sessions] == ["你好 codex"]

    @pytest.mark.asyncio
    async def test_get_session_direct_searches_account_homes(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        default_root = tmp_path / "default-codex"
        account_home = tmp_path / "homes" / "plus1"
        session_dir = account_home / mgr._encode_cwd(str(project_dir))
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "sid-1.jsonl"
        payload = [
            {"cwd": str(project_dir)},
            {"type": "summary", "summary": "From account home"},
        ]
        session_file.write_text(
            "\n".join(json.dumps(item) for item in payload) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(config, "codex_projects_path", default_root)
        monkeypatch.setattr(
            session_module, "list_account_homes", lambda: [account_home]
        )

        session = await mgr._get_session_direct(
            "sid-1",
            str(project_dir),
            account_name="plus1",
        )

        assert session is not None
        assert session.file_path == str(session_file)

    @pytest.mark.asyncio
    async def test_get_session_direct_matches_rollout_filename_by_uuid_suffix(
        self, mgr: SessionManager, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        default_root = tmp_path / "default-codex"
        default_root.mkdir()
        monkeypatch.setattr(config, "codex_projects_path", default_root)
        monkeypatch.setattr(session_module, "list_account_homes", lambda: [])

        session_dir = default_root / "sessions" / "2026" / "04" / "03"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = (
            session_dir
            / "rollout-2026-04-03T10-18-03-019d5122-1b8c-7790-9525-6d21a3c5bb94.jsonl"
        )
        payload = [
            {"cwd": str(project_dir)},
            {"type": "summary", "summary": "Rollout form"},
        ]
        session_file.write_text(
            "\n".join(json.dumps(item) for item in payload) + "\n",
            encoding="utf-8",
        )

        session = await mgr._get_session_direct(
            "019d5122-1b8c-7790-9525-6d21a3c5bb94",
            str(project_dir),
        )

        assert session is not None
        assert session.file_path == str(session_file)


class TestResolveWindowForThread:
    def test_none_thread_id_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, None) is None

    def test_unbound_thread_returns_none(self, mgr: SessionManager) -> None:
        assert mgr.resolve_window_for_thread(100, 42) is None

    def test_bound_thread_returns_window(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        assert mgr.resolve_window_for_thread(100, 42) == "@3"


class TestHasBoundThreadForSession:
    def test_true_when_bound_window_has_matching_session_id(
        self, mgr: SessionManager
    ) -> None:
        mgr.bind_thread(100, 42, "@3")
        mgr.get_window_state("@3").session_id = "session-a"

        assert mgr.has_bound_thread_for_session("session-a") is True

    def test_false_when_session_id_only_exists_on_unbound_window(
        self, mgr: SessionManager
    ) -> None:
        mgr.get_window_state("@3").session_id = "session-a"

        assert mgr.has_bound_thread_for_session("session-a") is False

    def test_false_for_different_bound_session(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        mgr.get_window_state("@3").session_id = "session-a"

        assert mgr.has_bound_thread_for_session("session-b") is False

    def test_matches_rollout_and_uuid_forms(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 42, "@3")
        mgr.get_window_state("@3").session_id = "019d5122-1b8c-7790-9525-6d21a3c5bb94"

        assert (
            mgr.has_bound_thread_for_session(
                "rollout-2026-04-03T10-18-03-019d5122-1b8c-7790-9525-6d21a3c5bb94"
            )
            is True
        )


class TestDisplayNames:
    def test_get_display_name_fallback(self, mgr: SessionManager) -> None:
        """get_display_name returns window_id when no display name is set."""
        assert mgr.get_display_name("@99") == "@99"

    def test_set_and_get_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="myproject")
        assert mgr.get_display_name("@1") == "myproject"

    def test_set_display_name_update(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="old-name")
        mgr.window_display_names["@1"] = "new-name"
        assert mgr.get_display_name("@1") == "new-name"

    def test_bind_thread_sets_display_name(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1", window_name="proj")
        assert mgr.get_display_name("@1") == "proj"

    def test_bind_thread_without_name_no_display(self, mgr: SessionManager) -> None:
        mgr.bind_thread(100, 1, "@1")
        # No display name set, fallback to window_id
        assert mgr.get_display_name("@1") == "@1"


class TestIsWindowId:
    def test_valid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("@0") is True
        assert mgr._is_window_id("@12") is True
        assert mgr._is_window_id("@999") is True

    def test_invalid_ids(self, mgr: SessionManager) -> None:
        assert mgr._is_window_id("myproject") is False
        assert mgr._is_window_id("@") is False
        assert mgr._is_window_id("") is False
        assert mgr._is_window_id("@abc") is False
