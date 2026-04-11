"""Codex session management — the core state hub.

Manages the key mappings:
  Window→Session (window_states): which Codex session_id a window holds (keyed by window_id).
  User→Thread→Window (thread_bindings): topic-to-window bindings (1 topic = 1 window_id).

Responsibilities:
  - Persist/load state to ~/.ccbot/state.json.
  - Sync window↔session bindings from session_map.json (written by hook).
  - Resolve window IDs to Codex session objects (JSONL file reading).
  - Track per-user read offsets for unread-message detection.
  - Manage thread↔window bindings for Telegram topic routing.
  - Send keystrokes to tmux windows and retrieve message history.
  - Maintain window_id→display name mapping for UI display.
  - Re-resolve stale window IDs on startup (tmux server restart recovery).

Key class: SessionManager (singleton instantiated as `session_manager`).
Key methods for thread binding access:
  - resolve_window_for_thread: Get window_id for a user's thread
  - iter_thread_bindings: Generator for iterating all (user_id, thread_id, window_id)
  - find_users_for_session: Find all users bound to a session_id
  - has_bound_thread_for_session: Check whether a session is already active in a topic
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterator
from typing import Any

import aiofiles

from .account_manager import ACCOUNT_HOME_DIR, list_account_homes
from .config import config
from .tmux_manager import tmux_manager
from .transcript_parser import TranscriptParser
from .utils import atomic_write_json, read_cwd_from_jsonl

logger = logging.getLogger(__name__)

_SHELL_PANE_COMMANDS = {"bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"}


def _is_shell_pane_command(command: str) -> bool:
    """Return True when a tmux pane is sitting at an interactive shell."""
    if not command:
        return False
    return Path(command).name in _SHELL_PANE_COMMANDS


_UUID_SUFFIX_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)


def _iter_transcript_roots(preferred_account_name: str = "") -> list[Path]:
    """Return transcript roots to search, preferring one account home when known."""
    candidates: list[Path] = []
    if preferred_account_name:
        candidates.append(ACCOUNT_HOME_DIR / preferred_account_name)
    candidates.append(config.codex_projects_path)
    candidates.extend(list_account_homes())

    seen: set[str] = set()
    roots: list[Path] = []
    for candidate in candidates:
        path = candidate.expanduser()
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)
    return roots


def _canonical_session_id(session_id: str) -> str:
    """Normalize bare UUID and rollout-prefixed ids to one comparable form."""
    if not session_id:
        return ""
    match = _UUID_SUFFIX_RE.search(session_id)
    if match:
        return match.group(1)
    return session_id


def _session_ids_match(left: str, right: str) -> bool:
    """Return whether two session ids refer to the same Codex session."""
    return bool(
        left and right and _canonical_session_id(left) == _canonical_session_id(right)
    )


def _normalize_path(path_str: str) -> str:
    """Normalize cwd values for stable comparisons."""
    try:
        return str(Path(path_str).resolve())
    except OSError:
        return path_str


def _extract_user_text(data: dict[str, Any]) -> str:
    """Extract user text from both legacy and rollout transcript entries."""
    if TranscriptParser.is_user_message(data):
        parsed = TranscriptParser.parse_message(data)
        if parsed and parsed.text.strip():
            return parsed.text.strip()

    payload = data.get("payload")
    if isinstance(payload, dict):
        if payload.get("type") == "user_message":
            message = payload.get("message", "")
            if isinstance(message, str):
                return message.strip()

        if payload.get("type") == "message" and payload.get("role") == "user":
            content = payload.get("content", [])
            texts: list[str] = []
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, str) and item.strip():
                        texts.append(item.strip())
                    elif isinstance(item, dict):
                        text = item.get("text", "")
                        if isinstance(text, str) and text.strip():
                            texts.append(text.strip())
            text = "\n".join(texts).strip()
            if text.startswith("<environment_context>") or text.startswith(
                "# AGENTS.md instructions for "
            ):
                return ""
            return text

    return ""


@dataclass
class WindowState:
    """Persistent state for a tmux window."""

    session_id: str = ""
    cwd: str = ""
    window_name: str = ""
    account_name: str = ""
    usage_limit_exceeded: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "session_id": self.session_id,
            "cwd": self.cwd,
        }
        if self.window_name:
            d["window_name"] = self.window_name
        if self.account_name:
            d["account_name"] = self.account_name
        if self.usage_limit_exceeded:
            d["usage_limit_exceeded"] = True
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowState":
        return cls(
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
            account_name=data.get("account_name", ""),
            usage_limit_exceeded=bool(data.get("usage_limit_exceeded", False)),
        )


@dataclass
class CodexSession:
    """Information about a Codex session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str


@dataclass
class SessionManager:
    """Manages session state for Codex.

    All internal keys use window_id (e.g. '@0', '@12') for uniqueness.
    Display names (window_name) are stored separately for UI presentation.

    window_states: window_id -> WindowState (session_id, cwd, window_name)
    user_window_offsets: user_id -> {window_id -> byte_offset}
    thread_bindings: user_id -> {thread_id -> window_id}
    window_display_names: window_id -> window_name (for display)
    group_chat_ids: "user_id:thread_id" -> group chat_id (for supergroup routing)
    """

    window_states: dict[str, WindowState] = field(default_factory=dict)
    user_window_offsets: dict[int, dict[str, int]] = field(default_factory=dict)
    thread_bindings: dict[int, dict[int, str]] = field(default_factory=dict)
    # window_id -> display name (window_name)
    window_display_names: dict[str, str] = field(default_factory=dict)
    # "user_id:thread_id" -> group chat_id (for supergroup forum topic routing)
    # IMPORTANT: This mapping is essential for supergroup/forum topic support.
    # Telegram Bot API requires group chat_id (negative number like -100xxx)
    # as the chat_id parameter when sending messages to forum topics.
    # Using user_id as chat_id will fail with "Message thread not found".
    # See: https://core.telegram.org/bots/api#sendmessage
    # History: originally added in 5afc111, erroneously removed in 26cb81f,
    # restored in PR #23.
    group_chat_ids: dict[str, int] = field(default_factory=dict)
    # Closed sessions that should be hidden from the resume picker by default.
    hidden_session_ids: set[str] = field(default_factory=set)
    # Sessions that were ever managed through a Telegram topic.
    topic_managed_session_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._load_state()

    def _save_state(self) -> None:
        state: dict[str, Any] = {
            "window_states": {k: v.to_dict() for k, v in self.window_states.items()},
            "user_window_offsets": {
                str(uid): offsets for uid, offsets in self.user_window_offsets.items()
            },
            "thread_bindings": {
                str(uid): {str(tid): wid for tid, wid in bindings.items()}
                for uid, bindings in self.thread_bindings.items()
            },
            "window_display_names": self.window_display_names,
            "group_chat_ids": self.group_chat_ids,
            "hidden_session_ids": sorted(self.hidden_session_ids),
            "topic_managed_session_ids": sorted(self.topic_managed_session_ids),
        }
        atomic_write_json(config.state_file, state)
        logger.debug("State saved to %s", config.state_file)

    def _is_window_id(self, key: str) -> bool:
        """Check if a key looks like a tmux window ID (e.g. '@0', '@12')."""
        return key.startswith("@") and len(key) > 1 and key[1:].isdigit()

    def _load_state(self) -> None:
        """Load state synchronously during initialization.

        Detects old-format state (window_name keys without '@' prefix) and
        marks for migration on next startup re-resolution.
        """
        if config.state_file.exists():
            try:
                state = json.loads(config.state_file.read_text())
                self.window_states = {
                    k: WindowState.from_dict(v)
                    for k, v in state.get("window_states", {}).items()
                }
                self.user_window_offsets = {
                    int(uid): offsets
                    for uid, offsets in state.get("user_window_offsets", {}).items()
                }
                self.thread_bindings = {
                    int(uid): {int(tid): wid for tid, wid in bindings.items()}
                    for uid, bindings in state.get("thread_bindings", {}).items()
                }
                self.window_display_names = state.get("window_display_names", {})
                self.group_chat_ids = {
                    k: int(v) for k, v in state.get("group_chat_ids", {}).items()
                }
                self.hidden_session_ids = {
                    _canonical_session_id(session_id)
                    for session_id in state.get("hidden_session_ids", [])
                    if isinstance(session_id, str) and session_id
                }
                self.topic_managed_session_ids = {
                    _canonical_session_id(session_id)
                    for session_id in state.get("topic_managed_session_ids", [])
                    if isinstance(session_id, str) and session_id
                }

                # Detect old format: keys that don't look like window IDs
                needs_migration = False
                for k in self.window_states:
                    if not self._is_window_id(k):
                        needs_migration = True
                        break
                if not needs_migration:
                    for bindings in self.thread_bindings.values():
                        for wid in bindings.values():
                            if not self._is_window_id(wid):
                                needs_migration = True
                                break
                        if needs_migration:
                            break

                if needs_migration:
                    logger.info(
                        "Detected old-format state (window_name keys), "
                        "will re-resolve on startup"
                    )
                    pass

            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to load state: %s", e)
                self.window_states = {}
                self.user_window_offsets = {}
                self.thread_bindings = {}
                self.window_display_names = {}
                self.group_chat_ids = {}
                self.hidden_session_ids = set()
                self.topic_managed_session_ids = set()
                pass

    async def resolve_stale_ids(self) -> None:
        """Re-resolve persisted window IDs against live tmux windows.

        Called on startup. Handles two cases:
        1. Old-format migration: window_name keys → window_id keys
        2. Stale IDs: window_id no longer exists but display name matches a live window

        Builds {window_name: window_id} from live windows, then remaps or drops entries.
        """
        windows = await tmux_manager.list_windows()
        live_by_name: dict[str, str] = {}  # window_name -> window_id
        live_ids: set[str] = set()
        for w in windows:
            live_by_name[w.window_name] = w.window_id
            live_ids.add(w.window_id)

        changed = False

        # --- Migrate window_states ---
        new_window_states: dict[str, WindowState] = {}
        for key, ws in self.window_states.items():
            if self._is_window_id(key):
                if key in live_ids:
                    new_window_states[key] = ws
                else:
                    # Stale ID — try re-resolve by display name
                    display = self.window_display_names.get(key, ws.window_name or key)
                    new_id = live_by_name.get(display)
                    if new_id:
                        logger.info(
                            "Re-resolved stale window_id %s -> %s (name=%s)",
                            key,
                            new_id,
                            display,
                        )
                        new_window_states[new_id] = ws
                        ws.window_name = display
                        self.window_display_names[new_id] = display
                        self.window_display_names.pop(key, None)
                        changed = True
                    else:
                        logger.info(
                            "Dropping stale window_state: %s (name=%s)", key, display
                        )
                        changed = True
            else:
                # Old format: key is window_name
                new_id = live_by_name.get(key)
                if new_id:
                    logger.info("Migrating window_state key %s -> %s", key, new_id)
                    ws.window_name = key
                    new_window_states[new_id] = ws
                    self.window_display_names[new_id] = key
                    changed = True
                else:
                    logger.info(
                        "Dropping old-format window_state: %s (no live window)", key
                    )
                    changed = True
        self.window_states = new_window_states

        # --- Migrate thread_bindings ---
        for uid, bindings in self.thread_bindings.items():
            new_bindings: dict[int, str] = {}
            for tid, val in bindings.items():
                if self._is_window_id(val):
                    if val in live_ids:
                        new_bindings[tid] = val
                    else:
                        display = self.window_display_names.get(val, val)
                        new_id = live_by_name.get(display)
                        if new_id:
                            logger.info(
                                "Re-resolved thread binding %s -> %s (name=%s)",
                                val,
                                new_id,
                                display,
                            )
                            new_bindings[tid] = new_id
                            self.window_display_names[new_id] = display
                            changed = True
                        else:
                            logger.info(
                                "Dropping stale thread binding: user=%d, thread=%d, wid=%s",
                                uid,
                                tid,
                                val,
                            )
                            changed = True
                else:
                    # Old format: val is window_name
                    new_id = live_by_name.get(val)
                    if new_id:
                        logger.info("Migrating thread binding %s -> %s", val, new_id)
                        new_bindings[tid] = new_id
                        self.window_display_names[new_id] = val
                        changed = True
                    else:
                        logger.info(
                            "Dropping old-format thread binding: user=%d, thread=%d, name=%s",
                            uid,
                            tid,
                            val,
                        )
                        changed = True
            self.thread_bindings[uid] = new_bindings

        # Remove empty user entries
        empty_users = [uid for uid, b in self.thread_bindings.items() if not b]
        for uid in empty_users:
            del self.thread_bindings[uid]

        # --- Prune display names and chat mappings that no longer have live state ---
        valid_window_ids = set(self.window_states)
        stale_display_ids = [
            window_id
            for window_id in self.window_display_names
            if window_id not in valid_window_ids
        ]
        for window_id in stale_display_ids:
            logger.info("Dropping stale window display name: %s", window_id)
            del self.window_display_names[window_id]
            changed = True

        valid_thread_keys = {
            f"{uid}:{tid}"
            for uid, bindings in self.thread_bindings.items()
            for tid in bindings
        }
        stale_chat_keys = [
            key for key in self.group_chat_ids if key not in valid_thread_keys
        ]
        for key in stale_chat_keys:
            logger.info("Dropping stale group chat mapping: %s", key)
            del self.group_chat_ids[key]
            changed = True

        # --- Migrate user_window_offsets ---
        for uid, offsets in self.user_window_offsets.items():
            new_offsets: dict[str, int] = {}
            for key, offset in offsets.items():
                if self._is_window_id(key):
                    if key in live_ids:
                        new_offsets[key] = offset
                    else:
                        display = self.window_display_names.get(key, key)
                        new_id = live_by_name.get(display)
                        if new_id:
                            new_offsets[new_id] = offset
                            changed = True
                        else:
                            changed = True
                else:
                    new_id = live_by_name.get(key)
                    if new_id:
                        new_offsets[new_id] = offset
                        changed = True
                    else:
                        changed = True
            self.user_window_offsets[uid] = new_offsets

        if changed:
            self._save_state()
            logger.info("Startup re-resolution complete")

        # Clean up session_map.json: stale window IDs and old-format keys
        await self._cleanup_stale_session_map_entries(live_ids)
        await self._cleanup_old_format_session_map_keys()

    async def _cleanup_old_format_session_map_keys(self) -> None:
        """Remove old-format keys (window_name instead of @window_id) from session_map.json."""
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = f"{config.tmux_session_name}:"
        old_keys = [
            key
            for key in session_map
            if key.startswith(prefix) and not self._is_window_id(key[len(prefix) :])
        ]
        if not old_keys:
            return

        for key in old_keys:
            del session_map[key]
        atomic_write_json(config.session_map_file, session_map)
        logger.info(
            "Cleaned up %d old-format session_map keys: %s", len(old_keys), old_keys
        )

    async def _cleanup_stale_session_map_entries(self, live_ids: set[str]) -> None:
        """Remove entries for tmux windows that no longer exist.

        When windows are closed externally (outside ccbot), session_map.json
        retains orphan references. This cleanup removes entries whose window_id
        is not in the current set of live tmux windows.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = f"{config.tmux_session_name}:"
        stale_keys = [
            key
            for key in session_map
            if key.startswith(prefix)
            and self._is_window_id(key[len(prefix) :])
            and key[len(prefix) :] not in live_ids
        ]
        if not stale_keys:
            return

        for key in stale_keys:
            del session_map[key]
            logger.info("Removed stale session_map entry: %s", key)

        atomic_write_json(config.session_map_file, session_map)
        logger.info(
            "Cleaned up %d stale session_map entries (windows no longer in tmux)",
            len(stale_keys),
        )

    # --- Display name management ---

    def get_display_name(self, window_id: str) -> str:
        """Get display name for a window_id, fallback to window_id itself."""
        return self.window_display_names.get(window_id, window_id)

    def update_display_name(self, window_id: str, new_name: str) -> None:
        """Update the display name for a window and persist state."""
        self.window_display_names[window_id] = new_name
        # Also update WindowState.window_name if it exists
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()
        logger.info("Updated display name: window_id %s -> '%s'", window_id, new_name)

    # --- Group chat ID management (supergroup forum topic routing) ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store the group chat_id for a user+thread combination.

        In supergroups with forum topics, messages must be sent to the group's
        chat_id (negative number like -100xxx) rather than the user's personal ID.
        Telegram's Bot API rejects message_thread_id when chat_id is a private
        user ID — the thread only exists within the group context.

        DO NOT REMOVE this method or the group_chat_ids mapping.
        Without it, all outbound messages in forum topics fail with
        "Message thread not found". See commit history: 5afc111 → 26cb81f → PR #23.
        """
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            self._save_state()
            logger.debug(
                "Stored group chat_id: user=%d, thread=%s, chat_id=%d",
                user_id,
                thread_id,
                chat_id,
            )

    def clear_group_chat_id(self, user_id: int, thread_id: int | None) -> None:
        """Remove the stored group chat mapping for a topic."""
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        if key in self.group_chat_ids:
            del self.group_chat_ids[key]
            self._save_state()
            logger.debug(
                "Cleared group chat_id: user=%d, thread=%s",
                user_id,
                thread_id,
            )

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve the correct chat_id for sending messages.

        Returns the stored group chat_id when a thread_id is present and a
        mapping exists, otherwise falls back to user_id (for private chats).

        Every outbound Telegram API call (send_message, edit_message_text,
        delete_message, send_chat_action, edit_forum_topic, etc.) MUST use
        this method instead of raw user_id. Using user_id directly breaks
        supergroup forum topic routing.
        """
        if thread_id is not None:
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
        return user_id

    async def wait_for_session_map_entry(
        self, window_id: str, timeout: float = 5.0, interval: float = 0.5
    ) -> bool:
        """Poll session_map.json until an entry for window_id appears.

        Returns True if the entry was found within timeout, False otherwise.
        """
        logger.debug(
            "Waiting for session_map entry: window_id=%s, timeout=%.1f",
            window_id,
            timeout,
        )
        key = f"{config.tmux_session_name}:{window_id}"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                if config.session_map_file.exists():
                    async with aiofiles.open(config.session_map_file, "r") as f:
                        content = await f.read()
                    session_map = json.loads(content)
                    info = session_map.get(key, {})
                    if info.get("session_id"):
                        # Found — load into window_states immediately
                        logger.debug(
                            "session_map entry found for window_id %s", window_id
                        )
                        await self.load_session_map()
                        return True
            except (json.JSONDecodeError, OSError):
                pass
            await asyncio.sleep(interval)
        logger.warning(
            "Timed out waiting for session_map entry: window_id=%s", window_id
        )
        return False

    async def load_session_map(self) -> None:
        """Read session_map.json and update window_states with new session associations.

        Keys in session_map are formatted as "tmux_session:window_id" (e.g. "ccbot:@12").
        Only entries matching our tmux_session_name are processed.
        Also cleans up window_states entries not in current session_map.
        Updates window_display_names from the "window_name" field in values.
        """
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        prefix = f"{config.tmux_session_name}:"
        valid_wids: set[str] = set()
        changed = False
        live_window_cwds: dict[str, str] = {}
        try:
            windows = await tmux_manager.list_windows()
            live_window_cwds = {
                window.window_id: _normalize_path(window.cwd) for window in windows
            }
        except Exception as exc:
            logger.debug("Unable to validate session_map cwds against tmux: %s", exc)

        for key, info in session_map.items():
            # Only process entries for our tmux session
            if not key.startswith(prefix):
                continue
            window_id = key[len(prefix) :]
            if not self._is_window_id(window_id):
                continue
            valid_wids.add(window_id)
            new_sid = info.get("session_id", "")
            new_cwd = info.get("cwd", "")
            new_wname = info.get("window_name", "")
            if not new_sid:
                continue
            state = self.get_window_state(window_id)
            live_cwd = live_window_cwds.get(window_id)
            if live_cwd and new_cwd and _normalize_path(new_cwd) != live_cwd:
                logger.info(
                    "Ignoring stale session_map entry for window_id %s: map cwd=%s, live cwd=%s",
                    window_id,
                    new_cwd,
                    live_cwd,
                )
                if state.session_id == new_sid and state.cwd == new_cwd:
                    state.session_id = ""
                    state.cwd = ""
                    state.usage_limit_exceeded = False
                    changed = True
                if new_wname:
                    state.window_name = new_wname
                    if self.window_display_names.get(window_id) != new_wname:
                        self.window_display_names[window_id] = new_wname
                        changed = True
                continue
            if state.session_id != new_sid or state.cwd != new_cwd:
                logger.info(
                    "Session map: window_id %s updated sid=%s, cwd=%s",
                    window_id,
                    new_sid,
                    new_cwd,
                )
                state.session_id = new_sid
                state.cwd = new_cwd
                changed = True
            # Update display name
            if new_wname:
                state.window_name = new_wname
                if self.window_display_names.get(window_id) != new_wname:
                    self.window_display_names[window_id] = new_wname
                    changed = True

        # Clean up window_states entries not in current session_map only when the
        # hook has actually produced entries for this tmux session. Some Codex
        # startups/resumes can keep session_map empty for a while (or forever on
        # failure), and eagerly deleting here breaks already-bound topics.
        if valid_wids:
            stale_wids = [w for w in self.window_states if w and w not in valid_wids]
            for wid in stale_wids:
                logger.info("Removing stale window_state: %s", wid)
                del self.window_states[wid]
                changed = True

        if changed:
            self._save_state()

    async def remove_session_map_entry(self, window_id: str) -> None:
        """Remove one window entry from session_map.json if present."""
        if not config.session_map_file.exists():
            return
        try:
            async with aiofiles.open(config.session_map_file, "r") as f:
                content = await f.read()
            session_map = json.loads(content)
        except (json.JSONDecodeError, OSError):
            return

        key = f"{config.tmux_session_name}:{window_id}"
        if key not in session_map:
            return

        del session_map[key]
        atomic_write_json(config.session_map_file, session_map)
        logger.info("Removed session_map entry for window_id %s", window_id)

    # --- Window state management ---

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or create window state."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState()
        return self.window_states[window_id]

    def clear_window_session(self, window_id: str) -> None:
        """Clear session association for a window (e.g., after /clear command)."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        state.cwd = ""
        state.usage_limit_exceeded = False
        self._save_state()
        logger.info("Cleared session for window_id %s", window_id)

    def prepare_window_launch(
        self,
        window_id: str,
        *,
        cwd: str,
        window_name: str = "",
        account_name: str = "",
    ) -> None:
        """Persist metadata for a freshly created tmux window."""
        state = self.get_window_state(window_id)
        state.session_id = ""
        state.cwd = cwd
        state.window_name = window_name
        state.account_name = account_name
        state.usage_limit_exceeded = False
        if window_name:
            self.window_display_names[window_id] = window_name
        self._save_state()

    def mark_window_usage_limit_exceeded(
        self,
        window_id: str,
        exceeded: bool = True,
    ) -> bool:
        """Mark whether one window has hit Codex quota exhaustion."""
        state = self.get_window_state(window_id)
        if state.usage_limit_exceeded == exceeded:
            return False
        state.usage_limit_exceeded = exceeded
        self._save_state()
        logger.info("Window %s usage_limit_exceeded=%s", window_id, exceeded)
        return True

    def hide_session(self, session_id: str) -> bool:
        """Hide one closed session from the resume picker."""
        canonical_id = _canonical_session_id(session_id)
        if not canonical_id or canonical_id in self.hidden_session_ids:
            return False
        self.hidden_session_ids.add(canonical_id)
        self._save_state()
        logger.info("Hid closed session from picker: %s", canonical_id)
        return True

    def unhide_session(self, session_id: str) -> bool:
        """Make one session visible in the resume picker again."""
        canonical_id = _canonical_session_id(session_id)
        if not canonical_id or canonical_id not in self.hidden_session_ids:
            return False
        self.hidden_session_ids.remove(canonical_id)
        self._save_state()
        logger.info("Unhid session in picker: %s", canonical_id)
        return True

    def is_session_hidden(self, session_id: str) -> bool:
        """Return whether a session is currently hidden from the resume picker."""
        canonical_id = _canonical_session_id(session_id)
        return bool(canonical_id and canonical_id in self.hidden_session_ids)

    def track_topic_managed_session(self, session_id: str) -> bool:
        """Remember that a session was once bound to a Telegram topic."""
        canonical_id = _canonical_session_id(session_id)
        if not canonical_id or canonical_id in self.topic_managed_session_ids:
            return False
        self.topic_managed_session_ids.add(canonical_id)
        self._save_state()
        logger.info("Tracked topic-managed session: %s", canonical_id)
        return True

    def is_topic_managed_session(self, session_id: str) -> bool:
        """Return whether a session was ever managed through a Telegram topic."""
        canonical_id = _canonical_session_id(session_id)
        return bool(canonical_id and canonical_id in self.topic_managed_session_ids)

    def _window_has_bound_thread(self, window_id: str) -> bool:
        """Return whether any current topic points at this tmux window."""
        return any(
            bound_window_id == window_id
            for _user_id, _thread_id, bound_window_id in self.iter_thread_bindings()
        )

    @staticmethod
    def _is_account_home_transcript(file_path: Path) -> bool:
        """Return whether a transcript lives under a ccbot per-account CODEX_HOME."""
        try:
            resolved = file_path.resolve()
        except OSError:
            resolved = file_path
        for account_home in list_account_homes():
            try:
                if resolved.is_relative_to(account_home.resolve()):
                    return True
            except OSError:
                continue
        return False

    def _is_external_resume_transcript(self, file_path: Path) -> bool:
        """Return whether a transcript comes from the user's non-ccbot Codex history."""
        if self._is_account_home_transcript(file_path):
            return False
        try:
            resolved = file_path.resolve()
            root = config.codex_projects_path.expanduser().resolve()
            return resolved.is_relative_to(root)
        except OSError:
            return False

    def remove_window_state(self, window_id: str) -> None:
        """Remove all persisted state associated with a tmux window."""
        changed = False

        if window_id in self.window_states:
            del self.window_states[window_id]
            changed = True

        if self.window_display_names.pop(window_id, None) is not None:
            changed = True

        empty_offset_users: list[int] = []
        for user_id, offsets in self.user_window_offsets.items():
            if window_id in offsets:
                offsets.pop(window_id, None)
                changed = True
            if not offsets:
                empty_offset_users.append(user_id)

        for user_id in empty_offset_users:
            del self.user_window_offsets[user_id]

        empty_binding_users: list[int] = []
        for user_id, bindings in self.thread_bindings.items():
            stale_thread_ids = [
                thread_id
                for thread_id, bound_window_id in bindings.items()
                if bound_window_id == window_id
            ]
            for thread_id in stale_thread_ids:
                del bindings[thread_id]
                changed = True
            if not bindings:
                empty_binding_users.append(user_id)

        for user_id in empty_binding_users:
            del self.thread_bindings[user_id]

        if changed:
            self._save_state()
            logger.info("Removed persisted window state for window_id %s", window_id)

    def register_session_to_window(
        self,
        window_id: str,
        session_id: str,
        cwd: str,
        window_name: str = "",
        *,
        persist_session_map: bool = False,
    ) -> None:
        """Bind a discovered transcript session to an existing tmux window."""
        state = self.get_window_state(window_id)
        state.session_id = session_id
        state.cwd = cwd
        state.usage_limit_exceeded = False
        canonical_id = _canonical_session_id(session_id)
        if canonical_id:
            self.hidden_session_ids.discard(canonical_id)
            if self._window_has_bound_thread(window_id):
                self.topic_managed_session_ids.add(canonical_id)
        if window_name:
            state.window_name = window_name
            self.window_display_names[window_id] = window_name
        self._save_state()
        if persist_session_map:
            self._save_session_map_entry(window_id, session_id, cwd, window_name)
        logger.info(
            "Registered session to window: window_id=%s session_id=%s cwd=%s",
            window_id,
            session_id,
            cwd,
        )

    def _save_session_map_entry(
        self,
        window_id: str,
        session_id: str,
        cwd: str,
        window_name: str = "",
    ) -> None:
        """Persist a corrected window->session entry back to session_map.json."""
        session_map: dict[str, Any] = {}
        if config.session_map_file.exists():
            try:
                session_map = json.loads(config.session_map_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read session_map for update: %s", exc)
                return

        key = f"{config.tmux_session_name}:{window_id}"
        session_map[key] = {
            "session_id": session_id,
            "cwd": cwd,
            "window_name": window_name or self.get_display_name(window_id),
        }
        atomic_write_json(config.session_map_file, session_map)
        logger.info("Updated session_map entry for window_id %s", window_id)

    @staticmethod
    def _encode_cwd(cwd: str) -> str:
        """Encode a cwd path to match Codex's project directory naming.

        Replaces all non-alphanumeric characters (except dash) with dashes.
        E.g. /home/user_name/Code/project -> -home-user-name-Code-project
        """
        return re.sub(r"[^a-zA-Z0-9-]", "-", cwd)

    def _build_session_file_path(
        self,
        session_id: str,
        cwd: str,
        *,
        root: Path | None = None,
    ) -> Path | None:
        """Build the direct file path for a session from session_id and cwd."""
        if not session_id or not cwd:
            return None
        encoded_cwd = self._encode_cwd(cwd)
        base_root = root if root is not None else config.codex_projects_path
        return base_root / encoded_cwd / f"{session_id}.jsonl"

    async def _get_session_direct(
        self,
        session_id: str,
        cwd: str,
        *,
        account_name: str = "",
    ) -> CodexSession | None:
        """Get a CodexSession directly from session_id and cwd (no scanning)."""
        file_path = None
        for root in _iter_transcript_roots(account_name):
            candidate = self._build_session_file_path(session_id, cwd, root=root)
            if candidate and candidate.exists():
                file_path = candidate
                break

        # Fallback: recursive search for Codex-style date-based session layout.
        if not file_path or not file_path.exists():
            for root in _iter_transcript_roots(account_name):
                matches = list(root.rglob(f"{session_id}.jsonl"))
                if not matches:
                    canonical_id = _canonical_session_id(session_id)
                    if canonical_id:
                        matches = list(root.rglob(f"*{canonical_id}.jsonl"))
                if matches:
                    file_path = matches[0]
                    logger.debug("Found session via recursive search: %s", file_path)
                    break
            else:
                return None

        return await self._read_session_from_file(file_path, session_id=session_id)

    async def _read_session_from_file(
        self,
        file_path: Path,
        *,
        session_id: str,
    ) -> CodexSession | None:
        """Read one transcript file and build a lightweight session summary."""
        # Single pass: read file once, extract summary + count messages
        summary = ""
        last_user_msg = ""
        message_count = 0
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    message_count += 1
                    try:
                        data = json.loads(line)
                        # Check for summary
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                        # Track last user message as fallback
                        else:
                            user_text = _extract_user_text(data)
                            if user_text:
                                last_user_msg = user_text
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return CodexSession(
            session_id=session_id,
            summary=summary,
            message_count=message_count,
            file_path=str(file_path),
        )

    async def _read_session_preview_from_file(
        self,
        file_path: Path,
        *,
        session_id: str,
        max_lines: int = 80,
    ) -> CodexSession | None:
        """Read enough transcript metadata for the resume picker without full scans."""
        summary = ""
        last_user_msg = ""
        scanned_lines = 0

        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    scanned_lines += 1
                    try:
                        data = json.loads(line)
                        if data.get("type") == "summary":
                            s = data.get("summary", "")
                            if s:
                                summary = s
                                break
                        else:
                            user_text = _extract_user_text(data)
                            if user_text:
                                last_user_msg = user_text
                                break
                    except json.JSONDecodeError:
                        continue

                    if scanned_lines >= max_lines:
                        break
        except OSError:
            return None

        if not summary:
            summary = last_user_msg[:50] if last_user_msg else "Untitled"

        return CodexSession(
            session_id=session_id,
            summary=summary,
            message_count=0,
            file_path=str(file_path),
        )

    # --- Directory session listing ---

    async def list_sessions_for_directory(self, cwd: str) -> list[CodexSession]:
        """List existing Codex sessions for a directory from recursive logs."""
        started_at = time.perf_counter()
        try:
            target_cwd = str(Path(cwd).resolve())
        except OSError:
            target_cwd = cwd

        deduped_files: dict[str, Path] = {}
        for root in _iter_transcript_roots():
            if not root.exists():
                continue
            for path in root.rglob("*.jsonl"):
                if not path.is_file() or path.stem == "sessions-index":
                    continue
                key = str(path.resolve())
                deduped_files.setdefault(key, path)

        jsonl_files = sorted(
            deduped_files.values(),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        sessions: list[CodexSession] = []
        for f in jsonl_files:
            session_cwd = read_cwd_from_jsonl(f)
            if not session_cwd:
                continue
            try:
                normalized_cwd = str(Path(session_cwd).resolve())
            except OSError:
                normalized_cwd = session_cwd
            if normalized_cwd != target_cwd:
                continue
            if len(sessions) >= 10:
                break
            session_id = f.stem
            if self.is_session_hidden(session_id):
                continue
            if (
                self._is_external_resume_transcript(f)
                and not config.show_external_resume_sessions
            ):
                logger.debug(
                    "Skipping external Codex transcript in resume picker: %s", f
                )
                continue
            if not self.has_bound_thread_for_session(session_id) and (
                self.is_topic_managed_session(session_id)
                or self._is_account_home_transcript(f)
            ):
                canonical_id = _canonical_session_id(session_id)
                if canonical_id:
                    self.topic_managed_session_ids.add(canonical_id)
                self.hide_session(session_id)
                continue
            session = await self._read_session_preview_from_file(
                f, session_id=session_id
            )
            if session:
                sessions.append(session)
        logger.debug(
            "Listed %d session(s) for %s in %.2fs",
            len(sessions),
            target_cwd,
            time.perf_counter() - started_at,
        )
        return sessions

    # --- Window → Session resolution ---

    async def resolve_session_for_window(self, window_id: str) -> CodexSession | None:
        """Resolve a tmux window to the best matching Codex session.

        Uses persisted session_id + cwd to construct file path directly.
        Returns None if no session is associated with this window.
        """
        state = self.get_window_state(window_id)

        if not state.session_id or not state.cwd:
            return None

        session = await self._get_session_direct(
            state.session_id,
            state.cwd,
            account_name=state.account_name,
        )
        if session:
            return session

        # File no longer exists, clear state
        logger.warning(
            "Session file no longer exists for window_id %s (sid=%s, cwd=%s)",
            window_id,
            state.session_id,
            state.cwd,
        )
        state.session_id = ""
        state.cwd = ""
        self._save_state()
        return None

    # --- User window offset management ---

    def update_user_window_offset(
        self, user_id: int, window_id: str, offset: int
    ) -> None:
        """Update the user's last read offset for a window."""
        if user_id not in self.user_window_offsets:
            self.user_window_offsets[user_id] = {}
        self.user_window_offsets[user_id][window_id] = offset
        self._save_state()

    # --- Thread binding management ---

    def bind_thread(
        self, user_id: int, thread_id: int, window_id: str, window_name: str = ""
    ) -> None:
        """Bind a Telegram topic thread to a tmux window.

        Args:
            user_id: Telegram user ID
            thread_id: Telegram topic thread ID
            window_id: Tmux window ID (e.g. '@0')
            window_name: Display name for the window (optional)
        """
        if user_id not in self.thread_bindings:
            self.thread_bindings[user_id] = {}
        self.thread_bindings[user_id][thread_id] = window_id
        state = self.window_states.get(window_id)
        if state and state.session_id:
            canonical_id = _canonical_session_id(state.session_id)
            if canonical_id:
                self.topic_managed_session_ids.add(canonical_id)
        if window_name:
            self.window_display_names[window_id] = window_name
        self._save_state()
        display = window_name or self.get_display_name(window_id)
        logger.info(
            "Bound thread %d -> window_id %s (%s) for user %d",
            thread_id,
            window_id,
            display,
            user_id,
        )

    def unbind_thread(self, user_id: int, thread_id: int) -> str | None:
        """Remove a thread binding. Returns the previously bound window_id, or None."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings or thread_id not in bindings:
            return None
        window_id = bindings.pop(thread_id)
        if not bindings:
            del self.thread_bindings[user_id]
        self._save_state()
        logger.info(
            "Unbound thread %d (was %s) for user %d",
            thread_id,
            window_id,
            user_id,
        )
        return window_id

    def get_window_for_thread(self, user_id: int, thread_id: int) -> str | None:
        """Look up the window_id bound to a thread."""
        bindings = self.thread_bindings.get(user_id)
        if not bindings:
            return None
        return bindings.get(thread_id)

    def resolve_window_for_thread(
        self,
        user_id: int,
        thread_id: int | None,
    ) -> str | None:
        """Resolve the tmux window_id for a user's thread.

        Returns None if thread_id is None or the thread is not bound.
        """
        if thread_id is None:
            return None
        return self.get_window_for_thread(user_id, thread_id)

    def iter_thread_bindings(self) -> Iterator[tuple[int, int, str]]:
        """Iterate all thread bindings as (user_id, thread_id, window_id).

        Provides encapsulated access to thread_bindings without exposing
        the internal data structure directly.
        """
        for user_id, bindings in list(self.thread_bindings.items()):
            for thread_id, window_id in list(bindings.items()):
                yield user_id, thread_id, window_id

    @staticmethod
    def _window_id_sort_key(window_id: str) -> tuple[int, str]:
        """Sort tmux window ids numerically, falling back to string order."""
        if window_id.startswith("@") and window_id[1:].isdigit():
            return (int(window_id[1:]), window_id)
        return (10**9, window_id)

    def has_bound_thread_for_session(self, session_id: str) -> bool:
        """Return True when a bound topic already points at this session."""
        if not session_id:
            return False

        for _user_id, _thread_id, window_id in self.iter_thread_bindings():
            state = self.window_states.get(window_id)
            if state and _session_ids_match(state.session_id, session_id):
                return True
        return False

    def cleanup_duplicate_window_sessions(self) -> list[str]:
        """Clear duplicate session_id assignments while keeping one canonical window."""
        duplicates_by_session: dict[str, list[str]] = {}
        for window_id, state in self.window_states.items():
            if state.session_id:
                duplicates_by_session.setdefault(
                    _canonical_session_id(state.session_id),
                    [],
                ).append(window_id)

        bound_window_ids = {
            window_id for _, _, window_id in self.iter_thread_bindings()
        }
        cleared_windows: list[str] = []

        for session_id, window_ids in duplicates_by_session.items():
            if len(window_ids) < 2:
                continue

            keep_window_id = min(
                window_ids,
                key=lambda wid: (
                    0 if wid in bound_window_ids else 1,
                    self._window_id_sort_key(wid),
                ),
            )

            for window_id in window_ids:
                if window_id == keep_window_id:
                    continue
                state = self.window_states.get(window_id)
                if not state:
                    continue
                state.session_id = ""
                state.cwd = ""
                cleared_windows.append(window_id)
                logger.warning(
                    "Cleared duplicate session binding: session_id=%s window_id=%s keep=%s",
                    session_id,
                    window_id,
                    keep_window_id,
                )

        if cleared_windows:
            for offsets in self.user_window_offsets.values():
                for window_id in cleared_windows:
                    offsets.pop(window_id, None)
            self._save_state()

        return cleared_windows

    async def find_users_for_session(
        self,
        session_id: str,
    ) -> list[tuple[int, str, int]]:
        """Find all users whose thread-bound window maps to the given session_id.

        Returns list of (user_id, window_id, thread_id) tuples.
        """
        result: list[tuple[int, str, int]] = []
        for user_id, thread_id, window_id in self.iter_thread_bindings():
            resolved = await self.resolve_session_for_window(window_id)
            if resolved and _session_ids_match(resolved.session_id, session_id):
                result.append((user_id, window_id, thread_id))
        return result

    # --- Tmux helpers ---

    async def send_to_window(self, window_id: str, text: str) -> tuple[bool, str]:
        """Send text to a tmux window by ID."""
        display = self.get_display_name(window_id)
        logger.debug(
            "send_to_window: window_id=%s (%s), text_len=%d",
            window_id,
            display,
            len(text),
        )
        window = await tmux_manager.find_window_by_id(window_id)
        if not window:
            return False, "Window not found (may have been closed)"
        pane_cmd = (window.pane_current_command or "").strip()
        if _is_shell_pane_command(pane_cmd):
            return (
                False,
                "Window is not running Codex "
                f"(current command: {pane_cmd}); please create or resume a session again",
            )
        success = await tmux_manager.send_keys(window.window_id, text)
        if success:
            return True, f"Sent to {display}"
        return False, "Failed to send keys"

    @staticmethod
    def _file_has_usage_limit_exceeded(
        file_path: Path,
        max_bytes: int = 128 * 1024,
    ) -> bool:
        """Check recent transcript tail for a usage_limit_exceeded event."""
        try:
            size = file_path.stat().st_size
            with file_path.open("rb") as handle:
                if size > max_bytes:
                    handle.seek(size - max_bytes)
                chunk = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            return False

        for line in reversed(chunk.splitlines()):
            if "usage_limit_exceeded" not in line:
                continue
            try:
                payload = json.loads(line).get("payload", {})
            except json.JSONDecodeError:
                continue
            if (
                isinstance(payload, dict)
                and payload.get("type") == "error"
                and payload.get("codex_error_info") == "usage_limit_exceeded"
            ):
                return True
        return False

    async def window_has_usage_limit_exceeded(self, window_id: str) -> bool:
        """Return True when a window's session already exhausted its quota."""
        state = self.get_window_state(window_id)
        if state.usage_limit_exceeded:
            return True

        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return False

        exceeded = await asyncio.to_thread(
            self._file_has_usage_limit_exceeded,
            Path(session.file_path),
        )
        if exceeded:
            self.mark_window_usage_limit_exceeded(window_id, True)
        return exceeded

    # --- Message history ---

    async def get_recent_messages(
        self,
        window_id: str,
        *,
        start_byte: int = 0,
        end_byte: int | None = None,
    ) -> tuple[list[dict], int]:
        """Get user/assistant messages for a window's session.

        Resolves window → session, then reads the JSONL.
        Supports byte range filtering via start_byte/end_byte.
        Returns (messages, total_count).
        """
        session = await self.resolve_session_for_window(window_id)
        if not session or not session.file_path:
            return [], 0

        file_path = Path(session.file_path)
        if not file_path.exists():
            return [], 0

        # Read JSONL entries (optionally filtered by byte range)
        entries: list[dict] = []
        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                if start_byte > 0:
                    await f.seek(start_byte)

                while True:
                    # Check byte limit before reading
                    if end_byte is not None:
                        current_pos = await f.tell()
                        if current_pos >= end_byte:
                            break

                    line = await f.readline()
                    if not line:
                        break

                    data = TranscriptParser.parse_line(line)
                    if data:
                        entries.append(data)
        except OSError as e:
            logger.error("Error reading session file %s: %s", file_path, e)
            return [], 0

        parsed_entries, _ = TranscriptParser.parse_entries(entries)
        all_messages = [
            {
                "role": e.role,
                "text": e.text,
                "content_type": e.content_type,
                "timestamp": e.timestamp,
            }
            for e in parsed_entries
        ]

        return all_messages, len(all_messages)


session_manager = SessionManager()
