"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from ccbot.monitor_state import TrackedSession
from ccbot.session_monitor import SessionInfo, SessionMonitor


class TestReadNewLinesOffsetRecovery:
    """Tests for _read_new_lines offset corruption recovery."""

    @pytest.fixture
    def monitor(self, tmp_path):
        """Create a SessionMonitor with temp state file."""
        return SessionMonitor(
            projects_path=tmp_path / "projects",
            state_file=tmp_path / "monitor_state.json",
        )

    @pytest.mark.asyncio
    async def test_mid_line_offset_recovery(self, monitor, tmp_path, make_jsonl_entry):
        """Recover from corrupted offset pointing mid-line."""
        # Create JSONL file with two valid lines
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first message")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second message")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Calculate offset pointing into the middle of line 1
        line1_bytes = len(json.dumps(entry1).encode("utf-8")) // 2
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=line1_bytes,  # Mid-line (corrupted)
        )

        # Read should recover and return empty (offset moved to next line)
        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        # Should return empty list (recovery skips to next line, no new content yet)
        assert entries == []
        assert usage_limit_messages == []

        # Offset should now point to start of line 2
        line1_full = len(json.dumps(entry1).encode("utf-8")) + 1  # +1 for newline
        assert session.last_byte_offset == line1_full

    @pytest.mark.asyncio
    async def test_valid_offset_reads_normally(
        self, monitor, tmp_path, make_jsonl_entry
    ):
        """Normal reading when offset points to line start."""
        jsonl_file = tmp_path / "session.jsonl"
        entry1 = make_jsonl_entry(msg_type="assistant", content="first")
        entry2 = make_jsonl_entry(msg_type="assistant", content="second")
        jsonl_file.write_text(
            json.dumps(entry1) + "\n" + json.dumps(entry2) + "\n",
            encoding="utf-8",
        )

        # Offset at 0 should read both lines
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )

        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        assert len(entries) == 2
        assert usage_limit_messages == []
        assert session.last_byte_offset == jsonl_file.stat().st_size

    @pytest.mark.asyncio
    async def test_truncation_detection(self, monitor, tmp_path, make_jsonl_entry):
        """Detect file truncation and reset offset."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = make_jsonl_entry(msg_type="assistant", content="content")
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        # Set offset beyond file size (simulates truncation)
        session = TrackedSession(
            session_id="test-session",
            file_path=str(jsonl_file),
            last_byte_offset=9999,  # Beyond file size
        )

        entries, usage_limit_messages = await monitor._read_new_lines(
            session, jsonl_file
        )

        # Should reset offset to 0 and read the line
        assert session.last_byte_offset == jsonl_file.stat().st_size
        assert len(entries) == 1
        assert usage_limit_messages == []

    @pytest.mark.asyncio
    async def test_usage_limit_event_emits_notification(self, monitor, tmp_path):
        """A usage_limit_exceeded event should be surfaced as a monitor message."""
        jsonl_file = tmp_path / "session.jsonl"
        event = {
            "type": "event_msg",
            "payload": {
                "type": "error",
                "message": "You've hit your usage limit.",
                "codex_error_info": "usage_limit_exceeded",
            },
        }
        jsonl_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-4",
                    file_path=jsonl_file,
                )
            ]
        )
        tracked = TrackedSession(
            session_id="session-4",
            file_path=str(jsonl_file),
            last_byte_offset=0,
        )
        monitor.state.update_session(tracked)

        messages = await monitor.check_for_updates(set())

        assert len(messages) == 1
        assert messages[0].content_type == "usage_limit"
        assert "usage limit" in messages[0].text.lower()

    @pytest.mark.asyncio
    async def test_rebinds_bound_window_with_stale_cwd(self, monitor, tmp_path):
        """Rebind a topic window when session_map points at an old cwd."""
        jsonl_file = tmp_path / "session.jsonl"
        entry = {
            "timestamp": "2026-03-25T22:21:29.901Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi from Codex"}],
            },
        }
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-5",
                    file_path=jsonl_file,
                    cwd="/tmp/project",
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-5",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        states = {
            "@1": SimpleNamespace(
                session_id="old-session",
                cwd="/tmp/other",
                window_name="project-1",
            ),
            "@2": SimpleNamespace(
                session_id="other-session",
                cwd="/tmp/project",
                window_name="project-2",
            ),
        }

        with (
            patch("ccbot.session_monitor.list_account_homes", return_value=[]),
            patch("ccbot.session_monitor.tmux_manager") as mock_tmux,
            patch("ccbot.session.session_manager") as mock_sm,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                    ),
                    SimpleNamespace(
                        window_id="@2",
                        cwd="/tmp/project",
                        window_name="project-2",
                    ),
                ]
            )
            mock_sm.iter_thread_bindings.return_value = [
                (100, 1, "@1"),
                (100, 2, "@2"),
            ]
            mock_sm.get_window_state.side_effect = lambda wid: states[wid]
            mock_sm.has_bound_thread_for_session.return_value = False

            messages = await monitor.check_for_updates(set())

        mock_sm.register_session_to_window.assert_called_once_with(
            "@1",
            "session-5",
            "/tmp/project",
            window_name="project-1",
            persist_session_map=True,
        )
        assert [message.text for message in messages] == ["Hi from Codex"]

    @pytest.mark.asyncio
    async def test_skips_external_transcript_auto_bind_when_account_homes_exist(
        self, monitor, tmp_path
    ):
        """Do not bind unrelated ~/.codex history when ccbot account homes exist."""
        jsonl_file = tmp_path / "default-codex" / "session-6.jsonl"
        jsonl_file.parent.mkdir(parents=True)
        entry = {
            "timestamp": "2026-03-25T22:21:29.901Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "External hello"}],
            },
        }
        jsonl_file.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        account_home = tmp_path / "homes" / "plus1"
        account_home.mkdir(parents=True)

        monitor.scan_projects = AsyncMock(
            return_value=[
                SessionInfo(
                    session_id="session-6",
                    file_path=jsonl_file,
                    cwd="/tmp/project",
                )
            ]
        )
        monitor.state.update_session(
            TrackedSession(
                session_id="session-6",
                file_path=str(jsonl_file),
                last_byte_offset=0,
            )
        )

        with (
            patch(
                "ccbot.session_monitor.list_account_homes",
                return_value=[account_home],
            ),
            patch("ccbot.session.session_manager") as mock_sm,
        ):
            mock_sm.has_bound_thread_for_session.return_value = False

            messages = await monitor.check_for_updates(set())

        mock_sm.register_session_to_window.assert_not_called()
        assert [message.text for message in messages] == ["External hello"]

    @pytest.mark.asyncio
    async def test_skips_shell_windows_during_auto_bind(self, monitor, tmp_path):
        """Do not auto-bind a transcript to a tmux window that fell back to zsh."""
        jsonl_file = tmp_path / "homes" / "plus1" / "session-7.jsonl"
        jsonl_file.parent.mkdir(parents=True)
        jsonl_file.write_text("{}\n", encoding="utf-8")

        state = SimpleNamespace(
            session_id="",
            cwd="/tmp/project",
            window_name="project-1",
        )

        with (
            patch("ccbot.session_monitor.list_account_homes", return_value=[]),
            patch("ccbot.session_monitor.tmux_manager") as mock_tmux,
        ):
            mock_tmux.list_windows = AsyncMock(
                return_value=[
                    SimpleNamespace(
                        window_id="@1",
                        cwd="/tmp/project",
                        window_name="project-1",
                        pane_current_command="zsh",
                    )
                ]
            )
            mock_sm = SimpleNamespace(
                iter_thread_bindings=lambda: [(100, 1, "@1")],
                get_window_state=lambda _wid: state,
                register_session_to_window=AsyncMock(),
            )

            await monitor._auto_bind_session_to_window(
                "session-7",
                "/tmp/project",
                mock_sm,
                session_file=jsonl_file,
            )

        mock_sm.register_session_to_window.assert_not_called()
