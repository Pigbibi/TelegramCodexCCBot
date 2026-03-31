import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("ALLOWED_USERS", "1")
os.environ.setdefault("CCBOT_DIR", tempfile.mkdtemp(prefix="ccbot-test-config-"))
os.environ.setdefault("CLAUDE_PROJECTS_PATH", tempfile.mkdtemp(prefix="ccbot-test-projects-"))

from ccbot import bot


class BrandingStringTests(unittest.TestCase):
    def test_user_visible_branding_uses_codex(self) -> None:
        self.assertEqual(bot.PRODUCT_NAME, "Codex")
        self.assertIn("Codex Monitor", bot.WELCOME_MESSAGE)
        self.assertEqual(
            bot.UNSUPPORTED_CONTENT_MESSAGE,
            "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Codex.",
        )
        self.assertEqual(bot.PHOTO_CONFIRMATION_MESSAGE, "📷 Image sent to Codex.")
        self.assertEqual(bot.SESSION_STILL_RUNNING_MESSAGE, "The Codex session is still running in tmux.")
        self.assertEqual(bot.HELP_COMMAND_DESCRIPTION, "↗ Show Codex help")
        self.assertEqual(bot.CC_COMMANDS["memory"], "↗ Edit AGENTS.md")
        self.assertEqual(bot.ESC_COMMAND_DESCRIPTION, "Send Escape to interrupt Codex")
        self.assertEqual(bot.USAGE_COMMAND_DESCRIPTION, "Show Codex usage remaining")

    def test_default_directory_browser_path_is_not_machine_specific(self) -> None:
        projects_dir = Path.home() / "Projects"
        expected = projects_dir if projects_dir.is_dir() else Path.home()
        self.assertEqual(bot._default_directory_browser_path(), str(expected))


if __name__ == "__main__":
    unittest.main()
