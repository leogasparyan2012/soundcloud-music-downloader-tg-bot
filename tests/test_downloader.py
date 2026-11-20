"""Тесты текстов ошибок и служебных проверок загрузчика."""

from __future__ import annotations

import pytest

from soundcloud_bot.downloader import SoundCloudDownloader


def test_hook_reports_real_metadata_once() -> None:
    metadata = []
    progress = []
    hook = SoundCloudDownloader._make_hook(
        lambda fraction, done, total, speed: progress.append((fraction, done, total, speed)),
        metadata.append,
    )
    data = {
        "status": "downloading",
        "info_dict": {
            "track": "Настоящее имя",
            "uploader": "Артист",
            "duration": 123,
            "webpage_url": "https://soundcloud.com/artist/song",
        },
        "downloaded_bytes": 5,
        "total_bytes": 10,
        "speed": 1000,
    }

    hook(data)
    hook(data)

    # Метаданные отдаём один раз, прогресс — на каждый вызов.
    assert [(m.title, m.performer, m.duration) for m in metadata] == [
        ("Настоящее имя", "Артист", 123)
    ]
    assert progress == [(0.5, 5, 10, 1000), (0.5, 5, 10, 1000)]


def test_hook_skips_metadata_without_title() -> None:
    metadata = []
    hook = SoundCloudDownloader._make_hook(None, metadata.append)

    hook({"status": "downloading", "info_dict": {"id": "1"}})
    hook({"status": "downloading"})

    assert metadata == []


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


def test_humanize_drm() -> None:
    message = SoundCloudDownloader._humanize(
        Exception("ERROR: [soundcloud] 1642806132: This video is DRM protected")
    )
    assert "DRM" in message
    assert "скачать нельзя" in message


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
