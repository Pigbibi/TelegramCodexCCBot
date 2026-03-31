"""Unit tests for SessionMonitor JSONL reading and offset handling."""

import json
from unittest.mock import AsyncMock

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
