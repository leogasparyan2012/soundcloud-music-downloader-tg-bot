"""Конфигурация бота, читается из переменных окружения и файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
VALID_QUALITIES = ("128", "192", "256", "320")
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(RuntimeError):
    """Некорректная конфигурация — бот не сможет запуститься."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int_env(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидается целое число, получено {raw!r}.") from exc
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f"от {minimum} до {maximum}" if maximum is not None else f"не меньше {minimum}"
        raise ConfigError(f"{name}: значение должно быть {bounds}, получено {value}.")
    return value


def _path_env(name: str, default: Path | None = None) -> Path | None:
    raw = _env(name)
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    download_dir: Path
    audio_quality: str
    max_file_mb: int
    max_playlist_items: int
    max_concurrent_downloads: int
    download_timeout: int
    cookies_file: Path | None
    proxy: str | None
    telegram_api_url: str | None
    log_level: str

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024


def load_settings() -> Settings:
    """Читает .env и переменные окружения, валидирует значения."""
    load_dotenv(BASE_DIR / ".env")

    token = _env("BOT_TOKEN")
    if not token:
        raise ConfigError(
            "Не задан BOT_TOKEN.\n"
            "Скопируйте .env.example в .env и вставьте токен от @BotFather: https://t.me/BotFather"
        )
    if token.count(":") != 1 or not token.split(":")[0].isdigit():
        raise ConfigError("BOT_TOKEN выглядит некорректно. Скопируйте его из @BotFather целиком.")

    quality = _env("AUDIO_QUALITY", "320")
    if quality not in VALID_QUALITIES:
        raise ConfigError(
            f"AUDIO_QUALITY: допустимые значения — {', '.join(VALID_QUALITIES)}; получено {quality!r}."
        )

    log_level = _env("LOG_LEVEL", "INFO").upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ConfigError(
            f"LOG_LEVEL: допустимые значения — {', '.join(VALID_LOG_LEVELS)}; получено {log_level!r}."
        )

    cookies_file = _path_env("COOKIES_FILE")
    if cookies_file is not None and not cookies_file.is_file():
        raise ConfigError(f"COOKIES_FILE: файл не найден — {cookies_file}")

    return Settings(
        bot_token=token,
        download_dir=_path_env("DOWNLOAD_DIR", BASE_DIR / "downloads") or BASE_DIR / "downloads",
        audio_quality=quality,
        max_file_mb=_int_env("MAX_FILE_MB", 50, minimum=1, maximum=2000),
        max_playlist_items=_int_env("MAX_PLAYLIST_ITEMS", 10, minimum=1, maximum=50),
        max_concurrent_downloads=_int_env("MAX_CONCURRENT_DOWNLOADS", 2, minimum=1, maximum=10),
        download_timeout=_int_env("DOWNLOAD_TIMEOUT", 900, minimum=30, maximum=3600),
        cookies_file=cookies_file,
        proxy=_env("PROXY") or None,
        telegram_api_url=(_env("TELEGRAM_API_URL").rstrip("/") or None),
        log_level=log_level,
    )
