"""Сценарные тесты обработчика без Telegram и без сети."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest

from soundcloud_bot import handlers as handlers_module
from soundcloud_bot.config import Settings
from soundcloud_bot.downloader import DownloadError, Media, ProbeResult, TrackMeta
from soundcloud_bot.handlers import DownloadRegistry, on_text


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        bot_token="123456789:AAHtest",
        download_dir=tmp_path / "downloads",
        audio_quality="320",
        max_file_mb=50,
        max_playlist_items=10,
        max_concurrent_downloads=1,
        download_timeout=60,
        cookies_file=None,
        proxy=None,
        telegram_api_url=None,
        log_level="INFO",
    )
    values.update(overrides)
    return Settings(**values)


def track(title: str = "Song", performer: str = "Artist") -> TrackMeta:
    return TrackMeta(url="https://soundcloud.com/artist/song", title=title, performer=performer, duration=200)


class FakeStatus:
    def __init__(self, text: str) -> None:
        self.text = text
        self.history = [text]
        self.deleted = False

    async def edit_text(self, text: str, **kwargs) -> None:
        self.text = text
        self.history.append(text)

    async def delete(self) -> None:
        self.deleted = True


class FakeMessage:
    def __init__(self, text: str, chat_type: str = "private") -> None:
        self.text = text
        self.message_id = 10
        self.from_user = type("User", (), {"id": 42})()
        self.chat = type("Chat", (), {"id": 7, "type": chat_type})()
        self.statuses: list[FakeStatus] = []
        self.replies: list[str] = []

    async def reply(self, text: str, **kwargs) -> FakeStatus:
        status = FakeStatus(text)
        self.statuses.append(status)
        self.replies.append(text)
        return status

    async def answer(self, text: str, **kwargs) -> FakeStatus:
        return await self.reply(text)


class FakeBot:
    def __init__(self) -> None:
        self.audio_calls: list[dict] = []
        self.actions: list[tuple] = []

    async def send_chat_action(self, *args, **kwargs) -> None:
        self.actions.append(args)

    async def send_audio(self, **kwargs) -> None:
        self.audio_calls.append(kwargs)


class FakeDownloader:
    def __init__(self, probe: ProbeResult | Exception, filesize: int | None = None) -> None:
        self._probe = probe
        self._filesize = filesize

    async def probe(self, url: str) -> ProbeResult:
        if isinstance(self._probe, Exception):
            raise self._probe
        return self._probe

    async def download(self, track: TrackMeta, workdir: Path, on_progress=None, on_metadata=None) -> Media:
        if on_progress:
            on_progress(0.5, 5, 10, 1000)
        path = Path(workdir) / "track.mp3"
        path.write_bytes(b"ID3" + b"0" * 64)
        return Media(
            track=track,
            file_path=path,
            thumbnail_path=None,
            filesize=self._filesize or path.stat().st_size,
        )


class FailingDownloader(FakeDownloader):
    """Скачивание всегда падает — для проверки текста ошибки."""

    def __init__(self, probe: ProbeResult, error: str) -> None:
        super().__init__(probe)
        self._error = error

    async def download(self, track: TrackMeta, workdir: Path, on_progress=None, on_metadata=None) -> Media:
        raise DownloadError(self._error)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(handlers_module.asyncio, "sleep", no_sleep)


def run(coro):
    return asyncio.run(coro)


def test_single_track(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("Слушай https://soundcloud.com/artist/song")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("Song", False, (track(),), 1))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert len(bot.audio_calls) == 1
    call = bot.audio_calls[0]
    assert call["title"] == "Song"
    assert call["performer"] == "Artist"
    assert call["duration"] == 200
    status = message.statuses[0]
    assert status.deleted
    assert "Готово" in status.history[-1]
    # временная папка удалена
    assert list(settings.download_dir.glob("sc-*")) == []


def test_playlist_is_capped(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_playlist_items=2)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    tracks = (track("One"), track("Two"), track("Three"))
    downloader = FakeDownloader(ProbeResult("Mix", True, tracks, 3))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert [call["title"] for call in bot.audio_calls] == ["One", "Two"]
    status = message.statuses[0]
    assert status.deleted
    assert "Готово: 2 из 2" in status.history[-1]


def test_single_track_heading(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/song")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("Song", False, (track(),), 1))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert any("🎵 <b>Song</b>" in text for text in message.statuses[0].history)


def test_playlist_heading(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("Mix", True, (track("One"),), 1))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert any("🎧 Плейлист <b>Mix</b>" in text for text in message.statuses[0].history)


def test_album_heading(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/album")
    bot = FakeBot()
    downloader = FakeDownloader(
        ProbeResult("My Album", True, (track("One"),), 1, album_type="album")
    )

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert any("💿 Альбом <b>My Album</b>" in text for text in message.statuses[0].history)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://soundcloud.com/artist/track", "🔎 Получаю информацию о треке…"),
        ("https://soundcloud.com/artist/sets/mix", "🔎 Получаю информацию о плейлисте или альбоме…"),
        ("https://soundcloud.com/artist/likes", "🔎 Получаю информацию…"),
        ("https://on.soundcloud.com/abc", "🔎 Получаю информацию…"),
    ],
)
def test_probing_text_matches_url_type(url: str, expected: str) -> None:
    assert handlers_module._probing_text(url) == expected


def test_probe_error_is_shown(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/private")
    bot = FakeBot()
    downloader = FakeDownloader(DownloadError("Трек приватный или требует входа в аккаунт."))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert bot.audio_calls == []
    assert "❌" in message.statuses[0].history[-1]
    assert "приватный" in message.statuses[0].history[-1]


def test_too_large_file_is_not_sent(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_file_mb=1)
    message = FakeMessage("https://soundcloud.com/artist/long-set")
    bot = FakeBot()
    downloader = FakeDownloader(
        ProbeResult("Long", False, (track("Long Set"),), 1),
        filesize=5 * 1024 * 1024,
    )

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert bot.audio_calls == []
    assert any("Telegram принимает" in reply for reply in message.replies)


def test_download_error_without_title_shows_position_and_link(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, max_playlist_items=500)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    broken = TrackMeta(url="https://soundcloud.com/artist/broken", title="", performer="")
    probe = ProbeResult("Mix", True, (broken, track("Two")), 2)
    downloader = FailingDownloader(
        probe, "Трек защищён DRM — SoundCloud не отдаёт его для скачивания."
    )

    run(
        on_text(
            message,
            bot,
            settings,
            downloader,
            defaultdict(asyncio.Lock),
            DownloadRegistry(),
        )
    )

    warnings = [reply for reply in message.replies if reply.startswith("⚠️")]
    assert len(warnings) == 2
    assert "Трек 1 из 2" in warnings[0]
    assert "DRM" in warnings[0]
    assert 'href="https://soundcloud.com/artist/broken"' in warnings[0]
    assert "Трек без названия" not in warnings[0]


def test_foreign_link_gets_hint(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://example.com/track")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("x", False, (track(),), 1))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert bot.audio_calls == []
    assert "не похоже на ссылку SoundCloud" in message.replies[0]


def test_second_download_while_first_runs_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/song")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("Song", False, (track(),), 1))
    locks = defaultdict(asyncio.Lock)
    lock = locks[message.from_user.id]
    run(lock.acquire())

    try:
        run(on_text(message, bot, settings, downloader, locks, DownloadRegistry()))
    finally:
        lock.release()

    assert bot.audio_calls == []
    assert "предыдущая загрузка" in message.replies[0]


def test_group_chat_ignores_non_soundcloud_text(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("просто болтаем", chat_type="group")
    bot = FakeBot()
    downloader = FakeDownloader(ProbeResult("x", False, (track(),), 1))

    run(on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), DownloadRegistry()))

    assert message.replies == []
