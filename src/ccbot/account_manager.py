"""Helpers for managing multiple Codex auth snapshots for ccbot."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

CCBOT_ACCOUNTS_DIR = Path.home() / ".ccbot" / "accounts"
SNAPSHOT_DIR = CCBOT_ACCOUNTS_DIR / "snapshots"
CURRENT_NAME_FILE = CCBOT_ACCOUNTS_DIR / "current_name"
ACCOUNT_HOME_DIR = CCBOT_ACCOUNTS_DIR / "homes"
CODEX_DIR = Path.home() / ".codex"


def list_account_names() -> list[str]:
    """List saved account snapshot names in stable order."""
    if not SNAPSHOT_DIR.exists():
        return []
    names = [
        path.name
        for path in SNAPSHOT_DIR.iterdir()
        if path.is_dir() and (path / "auth.json").is_file()
    ]
    return sorted(names)


def get_current_account_name() -> str | None:
    """Return the currently selected snapshot name, if any."""
    if not CURRENT_NAME_FILE.is_file():
        return None
    try:
        name = CURRENT_NAME_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return name if name in list_account_names() else None


def get_default_account_name() -> str | None:
    """Return the preferred account for new sessions."""
    names = list_account_names()
    if not names:
        return None
    current = get_current_account_name()
    if current in names:
        return current
    return names[0]


def get_next_account_name(current_name: str | None) -> str | None:
    """Return the next snapshot name for quota rotation."""
    names = list_account_names()
    if not names:
        return None
    if current_name in names:
        idx = names.index(current_name)
        if len(names) == 1:
            return None
        return names[(idx + 1) % len(names)]
    fallback_current = get_current_account_name()
    if fallback_current in names:
        return fallback_current
    return get_default_account_name()


def remember_current_account(name: str) -> None:
    """Persist the snapshot name currently used for new sessions."""
    if not name:
        return
    CCBOT_ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    CURRENT_NAME_FILE.write_text(f"{name}\n", encoding="utf-8")


def _copy_if_different(source: Path, dest: Path) -> None:
    """Copy a file when it does not exist or content changed."""
    if not source.is_file():
        return
    if dest.is_file() and source.read_bytes() == dest.read_bytes():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)


def ensure_account_home(name: str) -> Path:
    """Ensure a dedicated CODEX_HOME exists for one saved snapshot."""
    snapshot_dir = SNAPSHOT_DIR / name
    snapshot_auth = snapshot_dir / "auth.json"
    account_home = ACCOUNT_HOME_DIR / name
    account_home.mkdir(parents=True, exist_ok=True)

    if not (account_home / "auth.json").is_file():
        if not snapshot_auth.is_file():
            raise FileNotFoundError(f"Account snapshot not found: {name}")
        _copy_if_different(snapshot_auth, account_home / "auth.json")
    _copy_if_different(CODEX_DIR / "config.toml", account_home / "config.toml")

    for child in ("memories", "tmp"):
        (account_home / child).mkdir(parents=True, exist_ok=True)

    logger.debug("Prepared CODEX_HOME for %s at %s", name, account_home)
    return account_home
