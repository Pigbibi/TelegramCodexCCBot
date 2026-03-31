"""Tests for saved account snapshot helpers."""

from ccbot import account_manager


def test_next_account_rotates_by_name(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    current_name_file = tmp_path / "current_name"
    for name in ("plus1", "plus2", "team"):
        account_dir = snapshot_dir / name
        account_dir.mkdir(parents=True)
        (account_dir / "auth.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "CURRENT_NAME_FILE", current_name_file)

    assert account_manager.get_default_account_name() == "plus1"
    assert account_manager.get_next_account_name("plus1") == "plus2"
    assert account_manager.get_next_account_name("plus2") == "team"
    assert account_manager.get_next_account_name("team") == "plus1"

    current_name_file.write_text("plus2\n", encoding="utf-8")
    assert account_manager.get_current_account_name() == "plus2"
    assert account_manager.get_next_account_name(None) == "plus2"


def test_ensure_account_home_copies_auth_and_config(tmp_path, monkeypatch) -> None:
    snapshot_dir = tmp_path / "snapshots"
    account_home_dir = tmp_path / "homes"
    codex_dir = tmp_path / "codex"
    codex_dir.mkdir(parents=True)

    account_dir = snapshot_dir / "plus1"
    account_dir.mkdir(parents=True)
    (account_dir / "auth.json").write_text(
        '{"auth_mode":"chatgpt"}',
        encoding="utf-8",
    )
    (codex_dir / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

    monkeypatch.setattr(account_manager, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(account_manager, "ACCOUNT_HOME_DIR", account_home_dir)
    monkeypatch.setattr(account_manager, "CODEX_DIR", codex_dir)

    home = account_manager.ensure_account_home("plus1")

    assert home == account_home_dir / "plus1"
    assert (home / "auth.json").read_text(encoding="utf-8") == '{"auth_mode":"chatgpt"}'
    assert (home / "config.toml").read_text(encoding="utf-8") == 'model = "gpt-5.4"\n'
    assert (home / "memories").is_dir()
    assert (home / "tmp").is_dir()
