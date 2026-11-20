"""Тесты конфигурации."""

from __future__ import annotations

import pytest

from soundcloud_bot import config

ENV_NAMES = (
    "BOT_TOKEN",
    "DOWNLOAD_DIR",
    "AUDIO_QUALITY",
    "MAX_FILE_MB",
    "MAX_PLAYLIST_ITEMS",
    "MAX_CONCURRENT_DOWNLOADS",
    "DOWNLOAD_TIMEOUT",
    "COOKIES_FILE",
    "PROXY",
    "TELEGRAM_API_URL",
    "LOG_LEVEL",
)


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)


def test_defaults(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "dl"))

    settings = config.load_settings()

    assert settings.audio_quality == "320"
    assert settings.max_file_mb == 50
    assert settings.max_playlist_items == 500
    assert settings.max_concurrent_downloads == 2
    assert settings.download_dir == tmp_path / "dl"
    assert settings.cookies_file is None
    assert settings.proxy is None


def test_missing_token(clean_env: None) -> None:
    with pytest.raises(config.ConfigError, match="BOT_TOKEN"):
        config.load_settings()


def test_malformed_token(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "не-токен")
    with pytest.raises(config.ConfigError, match="BOT_TOKEN"):
        config.load_settings()


@pytest.mark.parametrize("value", ["64", "999", "mp3"])
def test_invalid_quality(clean_env: None, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("AUDIO_QUALITY", value)
    with pytest.raises(config.ConfigError, match="AUDIO_QUALITY"):
        config.load_settings()


def test_invalid_max_file_mb(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("MAX_FILE_MB", "0")
    with pytest.raises(config.ConfigError, match="MAX_FILE_MB"):
        config.load_settings()


def test_max_playlist_items_accepts_500(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("MAX_PLAYLIST_ITEMS", "500")
    assert config.load_settings().max_playlist_items == 500


def test_max_playlist_items_rejects_more_than_500(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("MAX_PLAYLIST_ITEMS", "501")
    with pytest.raises(config.ConfigError, match="MAX_PLAYLIST_ITEMS"):
        config.load_settings()


def test_missing_cookies_file(clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("COOKIES_FILE", str(tmp_path / "nope.txt"))
    with pytest.raises(config.ConfigError, match="COOKIES_FILE"):
        config.load_settings()


def test_relative_paths_are_resolved_from_project_root(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456789:AAHtest-token")
    monkeypatch.setenv("DOWNLOAD_DIR", "downloads")
    settings = config.load_settings()
    assert settings.download_dir == config.BASE_DIR / "downloads"
