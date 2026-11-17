"""Мелкие утилиты: разбор ссылок, имена файлов, форматирование текста."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Домены, которые считаем SoundCloud. Поддомены (m., www., on.) разрешены.
SC_HOSTS = ("soundcloud.com", "snd.sc", "soundcloud.app.goo.gl")


def is_soundcloud_url(url: str) -> bool:
    """True, если ссылка ведёт на SoundCloud (включая короткие ссылки)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in SC_HOSTS)


def extract_soundcloud_url(text: str | None) -> str | None:
    """Достаёт первую ссылку SoundCloud из произвольного текста сообщения."""
    if not text:
        return None
    for match in _URL_RE.finditer(text):
        # Хвостовая пунктуация часто прилипает к ссылке: "(...)", "...,", "»".
        candidate = match.group(0).rstrip(").,;:!?»\"'")
        if is_soundcloud_url(candidate):
            return candidate
    return None


def safe_filename(*parts: str, ext: str = "mp3", max_length: int = 110) -> str:
    """Собирает безопасное имя файла из частей вида «исполнитель — название»."""
    name = " - ".join(part.strip() for part in parts if part and part.strip())
    name = unicodedata.normalize("NFKC", name)
    name = _BAD_FILENAME_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    if not name:
        name = "soundcloud_track"
    name = name[:max_length].rstrip(" ._-") or "soundcloud_track"
    return f"{name}.{ext.lstrip('.')}"


def human_size(size: float | int | None) -> str:
    """5 242 880 -> «5.0 МБ»."""
    if not size or size <= 0:
        return "0 Б"
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def human_speed(bytes_per_second: float | None) -> str | None:
    """Скорость загрузки в человекочитаемом виде."""
    if not bytes_per_second or bytes_per_second <= 0:
        return None
    return f"{human_size(bytes_per_second)}/с"


def progress_bar(fraction: float, width: int = 12) -> str:
    """Текстовая полоса прогресса: ▰▰▰▰▱▱▱▱."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return "▰" * filled + "▱" * (width - filled)


def clean_error(message: object, limit: int = 300) -> str:
    """Убирает ANSI-коды и лишние переводы строк из текста ошибки yt-dlp."""
    text = _ANSI_RE.sub("", str(message))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
