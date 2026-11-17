"""Тесты для вспомогательных функций."""

from __future__ import annotations

import pytest

from soundcloud_bot.utils import (
    clean_error,
    extract_soundcloud_url,
    human_size,
    human_speed,
    is_soundcloud_url,
    progress_bar,
    safe_filename,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://soundcloud.com/artist/track", "https://soundcloud.com/artist/track"),
        ("Слушай https://on.soundcloud.com/abc123, это круто", "https://on.soundcloud.com/abc123"),
        ("(https://soundcloud.com/a/b)", "https://soundcloud.com/a/b"),
        ("https://m.soundcloud.com/a/b?utm_source=clipboard", "https://m.soundcloud.com/a/b?utm_source=clipboard"),
        ("текст без ссылки", None),
        ("https://example.com/artist/track", None),
        ("https://evil-soundcloud.com/a/b", None),
        ("https://soundcloud.com.evil.com/a/b", None),
        ("ftp://soundcloud.com/a/b", None),
        ("http://soundcloud.com/a/b", "http://soundcloud.com/a/b"),
    ],
)
def test_extract_soundcloud_url(text: str, expected: str | None) -> None:
    assert extract_soundcloud_url(text) == expected


def test_is_soundcloud_url_accepts_subdomains() -> None:
    assert is_soundcloud_url("https://www.soundcloud.com/a/b")
    assert is_soundcloud_url("https://soundcloud.com")
    assert not is_soundcloud_url("https://soundcloud.com.example.org/a")


def test_safe_filename_strips_forbidden_characters() -> None:
    name = safe_filename('AC/DC', 'Back: In "Black"?')
    assert "/" not in name
    assert ":" not in name
    assert '"' not in name
    assert name.endswith(".mp3")


def test_safe_filename_truncates_and_falls_back() -> None:
    name = safe_filename("A" * 500, "B" * 500)
    assert len(name) <= 115
    assert name.endswith(".mp3")
    assert safe_filename("", "   ") == "soundcloud_track.mp3"


def test_human_size() -> None:
    assert human_size(0) == "0 Б"
    assert human_size(512) == "512 Б"
    assert human_size(1024) == "1.0 КБ"
    assert human_size(5 * 1024 * 1024) == "5.0 МБ"


def test_human_speed() -> None:
    assert human_speed(None) is None
    assert human_speed(0) is None
    assert human_speed(1024 * 1024) == "1.0 МБ/с"


def test_progress_bar() -> None:
    assert progress_bar(0, width=10) == "▱" * 10
    assert progress_bar(1, width=10) == "▰" * 10
    assert progress_bar(0.5, width=10) == "▰" * 5 + "▱" * 5
    assert len(progress_bar(0.42, width=12)) == 12


def test_clean_error_strips_ansi_and_truncates() -> None:
    assert clean_error("\x1b[31mERROR:\x1b[0m  boom") == "ERROR: boom"
    long_error = "x" * 500
    result = clean_error(long_error, limit=50)
    assert len(result) == 50
    assert result.endswith("…")
