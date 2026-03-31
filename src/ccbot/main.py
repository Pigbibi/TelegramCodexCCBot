"""Application entry point — CLI dispatcher and bot bootstrap.

Handles two execution modes:
  1. `ccbot hook` — delegates to hook.hook_main() for Codex hook processing.
  2. Default — configures logging, initializes tmux session, and starts the
     Telegram bot polling loop via bot.create_bot().
"""

import atexit
import fcntl
import logging
import os
import sys
from pathlib import Path
from typing import Literal, Sequence, TextIO


class AlreadyRunningError(RuntimeError):
    """Raised when another local ccbot instance already holds the lock."""


_INSTANCE_LOCK_HANDLE = None
_USAGE = """Usage:
  ccbot               Start the Telegram bot
  ccbot hook [args]   Process or install the Codex hook
  ccbot --help        Show this help message
"""


def acquire_instance_lock(lock_path: Path):
    """Acquire a non-blocking singleton lock for the current machine."""
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise AlreadyRunningError(f"ccbot is already running: {lock_path}") from exc

    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


def _release_instance_lock() -> None:
    """Release the singleton lock on shutdown."""
    global _INSTANCE_LOCK_HANDLE
    if _INSTANCE_LOCK_HANDLE is None:
        return
    try:
        fcntl.flock(_INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        _INSTANCE_LOCK_HANDLE.close()
    except OSError:
        pass
    _INSTANCE_LOCK_HANDLE = None


def _print_usage(stream: TextIO) -> None:
    """Print top-level CLI usage text."""
    print(_USAGE, file=stream, end="")


def _parse_cli_mode(argv: Sequence[str]) -> Literal["bot", "hook", "exit"]:
    """Parse top-level CLI arguments for ccbot."""
    if not argv:
        return "bot"

    command = argv[0]
    if command == "hook":
        return "hook"

    if command in {"-h", "--help", "help"}:
        _print_usage(sys.stdout)
        return "exit"

    print(f"Unknown arguments: {' '.join(argv)}", file=sys.stderr)
    _print_usage(sys.stderr)
    raise SystemExit(2)


def main() -> None:
    """Main entry point."""
    global _INSTANCE_LOCK_HANDLE

    mode = _parse_cli_mode(sys.argv[1:])

    if mode == "hook":
        from .hook import hook_main

        hook_main()
        return
    if mode == "exit":
        return

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    # Import config before enabling DEBUG — avoid leaking debug logs on config errors
    try:
        from .config import config
    except ValueError as e:
        from .utils import ccbot_dir

        config_dir = ccbot_dir()
        env_path = config_dir / ".env"
        print(f"Error: {e}\n")
        print(f"Create {env_path} with the following content:\n")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id")
        print()
        print("Get your bot token from @BotFather on Telegram.")
        print("Get your user ID from @userinfobot on Telegram.")
        sys.exit(1)

    logging.getLogger("ccbot").setLevel(logging.DEBUG)
    # AIORateLimiter (max_retries=5) handles retries itself; keep INFO for visibility
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    try:
        _INSTANCE_LOCK_HANDLE = acquire_instance_lock(config.config_dir / "ccbot.lock")
        atexit.register(_release_instance_lock)
    except AlreadyRunningError:
        logger.error("Another local ccbot instance is already running; exiting.")
        return

    from .tmux_manager import tmux_manager

    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Claude projects path: %s", config.claude_projects_path)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot...")
    from .bot import POLL_TIMEOUT_SECONDS, create_bot

    application = create_bot()
    application.run_polling(
        timeout=POLL_TIMEOUT_SECONDS,
        bootstrap_retries=-1,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()
