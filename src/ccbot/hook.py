"""Hook subcommand for Codex session tracking.

Called by a SessionStart hook to maintain a window↔session mapping in
<CCBOT_DIR>/session_map.json. Also provides `--install` to enable Codex hooks
in `~/.codex/config.toml` and register the SessionStart hook in
`~/.codex/hooks.json`.

This module must NOT import config.py (which requires TELEGRAM_BOT_TOKEN),
since hooks run inside tmux panes where bot env vars are not set.
Config directory resolution uses utils.ccbot_dir() (shared with config.py).

Key functions: hook_main() (CLI entry), _install_hook().
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Validate session_id looks like a UUID
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CONFIG_SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
_CODEX_HOOKS_FLAG_RE = re.compile(r"^\s*codex_hooks\s*=")

_CODEX_DIR = Path.home() / ".codex"
_CODEX_CONFIG_FILE = _CODEX_DIR / "config.toml"
_CODEX_HOOKS_FILE = _CODEX_DIR / "hooks.json"
_SESSION_START_MATCHER = "startup|resume"
_HOOK_STATUS_MESSAGE = "Registering CCBot session"
_HOOK_TIMEOUT_SECONDS = 5

# The hook command suffix for detection
_HOOK_COMMAND_SUFFIX = "ccbot hook"


def _find_ccbot_path() -> str:
    """Find the full path to the ccbot executable.

    Priority:
    1. shutil.which("ccbot") - if ccbot is in PATH
    2. Same directory as the Python interpreter (for venv installs)
    """
    # Try PATH first
    ccbot_path = shutil.which("ccbot")
    if ccbot_path:
        return ccbot_path

    # Fall back to the directory containing the Python interpreter
    # This handles the case where ccbot is installed in a venv
    python_dir = Path(sys.executable).parent
    ccbot_in_venv = python_dir / "ccbot"
    if ccbot_in_venv.exists():
        return str(ccbot_in_venv)

    # Last resort: assume it will be in PATH
    return "ccbot"


def _is_hook_installed(settings: dict) -> bool:
    """Check if ccbot hook is already installed in hooks.json.

    Detects both 'ccbot hook' and full paths like '/path/to/ccbot hook'.
    """
    hooks = settings.get("hooks", {})
    session_start = hooks.get("SessionStart", [])

    for entry in session_start:
        if not isinstance(entry, dict):
            continue
        inner_hooks = entry.get("hooks", [])
        for h in inner_hooks:
            if not isinstance(h, dict):
                continue
            cmd = h.get("command", "")
            # Match 'ccbot hook' or paths ending with 'ccbot hook'
            if cmd == _HOOK_COMMAND_SUFFIX or cmd.endswith("/" + _HOOK_COMMAND_SUFFIX):
                return True
    return False


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk, returning {} when the file is absent."""
    if not path.exists():
        return {}

    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a top-level JSON object")
    return data


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON with a trailing newline for easier diffs."""
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _enable_codex_hooks_feature(config_file: Path) -> None:
    """Ensure `[features] codex_hooks = true` exists in config.toml."""
    if config_file.exists():
        text = config_file.read_text()
    else:
        text = ""

    lines = text.splitlines()

    section_starts: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        match = _CONFIG_SECTION_RE.match(line)
        if match:
            section_starts.append((idx, match.group(1).strip()))

    features_start = None
    features_end = len(lines)
    for pos, (idx, section_name) in enumerate(section_starts):
        if section_name == "features":
            features_start = idx
            if pos + 1 < len(section_starts):
                features_end = section_starts[pos + 1][0]
            break

    changed = False
    if features_start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", "codex_hooks = true"])
        changed = True
    else:
        for idx in range(features_start + 1, features_end):
            if _CODEX_HOOKS_FLAG_RE.match(lines[idx]):
                if lines[idx].strip() != "codex_hooks = true":
                    lines[idx] = "codex_hooks = true"
                    changed = True
                break
        else:
            lines.insert(features_end, "codex_hooks = true")
            changed = True

    if changed or not config_file.exists():
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text("\n".join(lines).rstrip() + "\n")


def _install_hook() -> int:
    """Install the ccbot hook into Codex's config.toml and hooks.json.

    Returns 0 on success, 1 on error.
    """
    config_file = _CODEX_CONFIG_FILE
    hooks_file = _CODEX_HOOKS_FILE

    try:
        _enable_codex_hooks_feature(config_file)
    except (OSError, ValueError) as e:
        logger.error("Error updating %s: %s", config_file, e)
        print(f"Error updating {config_file}: {e}", file=sys.stderr)
        return 1

    try:
        settings = _read_json_file(hooks_file)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        logger.error("Error reading %s: %s", hooks_file, e)
        print(f"Error reading {hooks_file}: {e}", file=sys.stderr)
        return 1

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        message = f"{hooks_file} has invalid 'hooks' shape"
        logger.error(message)
        print(message, file=sys.stderr)
        return 1

    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        message = f"{hooks_file} has invalid 'hooks.SessionStart' shape"
        logger.error(message)
        print(message, file=sys.stderr)
        return 1

    # Check if already installed
    if _is_hook_installed(settings):
        logger.info("Hook already installed in %s", hooks_file)
        print(
            f"Hook already installed in {hooks_file} (Codex hooks enabled in {config_file})"
        )
        return 0

    # Find the full path to ccbot
    ccbot_path = _find_ccbot_path()
    hook_command = f"{ccbot_path} hook"
    hook_config = {
        "type": "command",
        "command": hook_command,
        "statusMessage": _HOOK_STATUS_MESSAGE,
        "timeout": _HOOK_TIMEOUT_SECONDS,
    }
    logger.info("Installing hook command: %s", hook_command)

    session_start.append(
        {
            "matcher": _SESSION_START_MATCHER,
            "hooks": [hook_config],
        }
    )

    # Write back
    try:
        hooks_file.parent.mkdir(parents=True, exist_ok=True)
        _write_json_file(hooks_file, settings)
    except OSError as e:
        logger.error("Error writing %s: %s", hooks_file, e)
        print(f"Error writing {hooks_file}: {e}", file=sys.stderr)
        return 1

    logger.info("Hook installed successfully in %s", hooks_file)
    print(
        "Hook installed successfully in "
        f"{hooks_file} (Codex hooks enabled in {config_file})"
    )
    return 0


def hook_main() -> None:
    """Process a Codex hook event from stdin, or install the hook."""
    # Configure logging for the hook subprocess (main.py logging doesn't apply here)
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG,
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        prog="ccbot hook",
        description="Codex session tracking hook",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Enable Codex hooks and install the SessionStart hook in ~/.codex/",
    )
    # Parse only known args to avoid conflicts with stdin JSON
    args, _ = parser.parse_known_args(sys.argv[2:])

    if args.install:
        logger.info("Hook install requested")
        sys.exit(_install_hook())

    # Normal hook processing: read JSON from stdin
    logger.debug("Processing hook event from stdin")
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse stdin JSON: %s", e)
        return

    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd", "")
    event = payload.get("hook_event_name", "")

    if not session_id or not event:
        logger.debug("Empty session_id or event, ignoring")
        return

    # Validate session_id format
    if not _UUID_RE.match(session_id):
        logger.warning("Invalid session_id format: %s", session_id)
        return

    # Validate cwd is an absolute path (if provided)
    if cwd and not os.path.isabs(cwd):
        logger.warning("cwd is not absolute: %s", cwd)
        return

    if event != "SessionStart":
        logger.debug("Ignoring non-SessionStart event: %s", event)
        return

    # Get tmux session:window key for the pane running this hook.
    # TMUX_PANE is set by tmux for every process inside a pane.
    pane_id = os.environ.get("TMUX_PANE", "")
    if not pane_id:
        logger.warning("TMUX_PANE not set, cannot determine window")
        return

    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-t",
            pane_id,
            "-p",
            "#{session_name}:#{window_id}:#{window_name}",
        ],
        capture_output=True,
        text=True,
    )
    raw_output = result.stdout.strip()
    # Expected format: "session_name:@id:window_name"
    parts = raw_output.split(":", 2)
    if len(parts) < 3:
        logger.warning(
            "Failed to parse session:window_id:window_name from tmux (pane=%s, output=%s)",
            pane_id,
            raw_output,
        )
        return
    tmux_session_name, window_id, window_name = parts
    # Key uses window_id for uniqueness
    session_window_key = f"{tmux_session_name}:{window_id}"

    logger.debug(
        "tmux key=%s, window_name=%s, session_id=%s, cwd=%s",
        session_window_key,
        window_name,
        session_id,
        cwd,
    )

    # Read-modify-write with file locking to prevent concurrent hook races
    from .utils import ccbot_dir

    map_file = ccbot_dir() / "session_map.json"
    map_file.parent.mkdir(parents=True, exist_ok=True)

    lock_path = map_file.with_suffix(".lock")
    try:
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            logger.debug("Acquired lock on %s", lock_path)
            try:
                session_map: dict[str, dict[str, str]] = {}
                if map_file.exists():
                    try:
                        session_map = json.loads(map_file.read_text())
                    except (json.JSONDecodeError, OSError):
                        logger.warning(
                            "Failed to read existing session_map, starting fresh"
                        )

                session_map[session_window_key] = {
                    "session_id": session_id,
                    "cwd": cwd,
                    "window_name": window_name,
                }

                # Clean up old-format key ("session:window_name") if it exists.
                # Previous versions keyed by window_name instead of window_id.
                old_key = f"{tmux_session_name}:{window_name}"
                if old_key != session_window_key and old_key in session_map:
                    del session_map[old_key]
                    logger.info("Removed old-format session_map key: %s", old_key)

                from .utils import atomic_write_json

                atomic_write_json(map_file, session_map)
                logger.info(
                    "Updated session_map: %s -> session_id=%s, cwd=%s",
                    session_window_key,
                    session_id,
                    cwd,
                )
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to write session_map: %s", e)
