"""Тесты текстов ошибок и служебных проверок загрузчика."""

from __future__ import annotations

import pytest

from soundcloud_bot.downloader import SoundCloudDownloader


def test_humanize_private_track() -> None:
    message = SoundCloudDownloader._humanize(Exception("ERROR: This video is private"))
    assert "приват" in message


def test_humanize_not_found() -> None:
    message = SoundCloudDownloader._humanize(Exception("ERROR: HTTP Error 404: Not Found"))
    assert "не найден" in message


def test_humanize_geo_block() -> None:
    message = SoundCloudDownloader._humanize(
        Exception("ERROR: The uploader has not made this sound available in your country")
    )
    assert "регион" in message


def test_humanize_missing_ffmpeg() -> None:
    message = SoundCloudDownloader._humanize(Exception("ffmpeg not found. Please install ffmpeg"))
    assert "ffmpeg" in message


def test_humanize_strips_ansi_and_keeps_details() -> None:
    message = SoundCloudDownloader._humanize(Exception("\x1b[31mERROR:\x1b[0m something odd happened"))
    assert "something odd happened" in message
    assert "\x1b" not in message


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[Deleted track]", True),
        ("[private track]", True),
        ("deleted track", True),
        ("Flickermood", False),
    ],
)
def test_is_placeholder(title: str, expected: bool) -> None:
    assert SoundCloudDownloader._is_placeholder(title) is expected
