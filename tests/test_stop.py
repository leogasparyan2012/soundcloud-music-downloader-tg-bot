"""Сценарии команды /stop: текущий трек дошлётся, остальные — нет."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path

import pytest

from soundcloud_bot import handlers as handlers_module
from soundcloud_bot.config import Settings
from soundcloud_bot.downloader import Media, ProbeResult, TrackMeta
from soundcloud_bot.handlers import DownloadRegistry, cmd_stop, on_text


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        bot_token="123456789:AAHtest",
        download_dir=tmp_path / "downloads",
        audio_quality="320",
        max_file_mb=50,
        max_playlist_items=500,
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


class BlockingDownloader:
    """Зависает внутри download(), пока тест не отпустит загрузку."""

    def __init__(
        self,
        probe: ProbeResult,
        metadata: TrackMeta | None = None,
        metadata_delay: float = 0.0,
    ) -> None:
        self._probe = probe
        self._metadata = metadata
        self._metadata_delay = metadata_delay
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.downloaded: list[str] = []

    async def probe(self, url: str) -> ProbeResult:
        return self._probe

    async def download(
        self,
        track: TrackMeta,
        workdir: Path,
        on_progress=None,
        on_metadata=None,
    ) -> Media:
        self.downloaded.append(track.title)
        self.started.set()
        if self._metadata_delay:
            await asyncio.sleep(self._metadata_delay)
        if on_metadata is not None and self._metadata is not None:
            on_metadata(self._metadata)
        await self.release.wait()
        if on_progress:
            on_progress(0.5, 5, 10, 1000)
        path = Path(workdir) / "track.mp3"
        path.write_bytes(b"ID3" + b"0" * 64)
        return Media(
            track=track,
            file_path=path,
            thumbnail_path=None,
            filesize=path.stat().st_size,
        )


class BlockingProbeDownloader:
    """Зависает на probe(), чтобы успеть нажать /stop до первого трека."""

    def __init__(self, probe: ProbeResult) -> None:
        self._probe = probe
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def probe(self, url: str) -> ProbeResult:
        self.started.set()
        await self.release.wait()
        return self._probe

    async def download(self, *args, **kwargs) -> Media:
        raise AssertionError("/stop не должен запускать скачивание")


@pytest.fixture(autouse=True)
def short_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Не ждём реальные 2.5 с прогресса, но даём другим задачам выполняться."""
    real_sleep = asyncio.sleep

    async def sleep(delay, *args, **kwargs) -> None:
        await real_sleep(0.001)

    monkeypatch.setattr(handlers_module.asyncio, "sleep", sleep)


def run(coro):
    return asyncio.run(coro)


def test_stop_without_active_download() -> None:
    message = FakeMessage("/stop")
    stops = DownloadRegistry()

    run(cmd_stop(message, stops))

    assert "нечего останавливать" in message.replies[0]


def test_stop_sends_current_track_and_skips_rest(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    stops = DownloadRegistry()
    tracks = tuple(track(f"T{i}") for i in range(5))
    probe = ProbeResult("Mix", True, tracks, len(tracks))

    async def scenario() -> BlockingDownloader:
        downloader = BlockingDownloader(probe)
        task = asyncio.create_task(
            on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), stops)
        )
        await asyncio.wait_for(downloader.started.wait(), 5)
        await cmd_stop(message, stops)
        downloader.release.set()
        await asyncio.wait_for(task, 5)
        return downloader

    downloader = run(scenario())

    # Начал скачиваться только первый трек — он и должен уйти в чат.
    assert downloader.downloaded == ["T0"]
    assert [call["title"] for call in bot.audio_calls] == ["T0"]

    status = message.statuses[0]
    assert "Остановлено: отправлено 1 из 5" in status.history[-1]
    assert any("докачаю" in reply for reply in message.replies)

    # Сессия снята с учёта, повторный /stop уже нечего останавливать.
    assert stops.request_stop(message.from_user.id) is None
    assert list(settings.download_dir.glob("sc-*")) == []


def test_stop_waits_for_real_title_before_reply(tmp_path: Path) -> None:
    """Название из flat-плейлиста пустое — /stop ждёт настоящее и пишет его в ответ."""
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    stops = DownloadRegistry()
    unknown = TrackMeta(url="https://soundcloud.com/artist/song", title="", performer="Forss", duration=None)
    real = TrackMeta(url=unknown.url, title="City Ports", performer="Forss", duration=142)
    probe = ProbeResult("Mix", True, (unknown,), 1)

    async def scenario() -> str:
        downloader = BlockingDownloader(probe, metadata=real, metadata_delay=0.05)
        task = asyncio.create_task(
            on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), stops)
        )
        await asyncio.wait_for(downloader.started.wait(), 5)
        await cmd_stop(message, stops)
        reply = message.replies[-1]
        downloader.release.set()
        await asyncio.wait_for(task, 5)
        return reply

    reply = run(scenario())

    assert "City Ports" in reply
    assert "Трек без названия" not in reply


def test_stop_before_first_track_sends_nothing(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    stops = DownloadRegistry()
    probe = ProbeResult("Mix", True, (track("One"), track("Two")), 2)

    async def scenario() -> None:
        downloader = BlockingProbeDownloader(probe)
        task = asyncio.create_task(
            on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), stops)
        )
        await asyncio.wait_for(downloader.started.wait(), 5)
        await cmd_stop(message, stops)
        downloader.release.set()
        await asyncio.wait_for(task, 5)

    run(scenario())

    assert bot.audio_calls == []
    assert any("следующие качать не буду" in reply for reply in message.replies)
    assert "Остановлено — ни один трек не отправлен" in message.statuses[0].history[-1]


def test_progress_text_marks_pending_stop() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    state: dict[str, float | int | None] = {"fraction": 0.5, "done": 5, "total": 10, "speed": None}

    text = handlers_module._progress_text(track(), state, 1, 3, True, stop_event)

    assert "Останавливаю после этого трека" in text


def test_progress_text_has_no_size_or_speed() -> None:
    state: dict[str, float | int | None] = {
        "fraction": 0.5,
        "done": 512_000,
        "total": 1_024_000,
        "speed": 123_456,
    }

    text = handlers_module._progress_text(track(), state, 1, 2, True)

    assert "50%" in text
    assert "МБ" not in text
    assert "КБ" not in text
    assert "/с" not in text


def test_progress_text_hides_unknown_title() -> None:
    state: dict[str, float | int | None] = {"fraction": 0.0, "done": 0, "total": 0, "speed": None}
    unknown = TrackMeta(url="https://soundcloud.com/artist/song", title="", performer="Forss")

    text = handlers_module._progress_text(unknown, state, 1, 2, True)

    assert "Трек без названия" not in text
    assert "Определяю название" in text


def test_progress_shows_real_title_from_downloader(tmp_path: Path) -> None:
    """Flat-метаданные плейлиста пустые — название уточняется во время загрузки."""
    settings = make_settings(tmp_path)
    message = FakeMessage("https://soundcloud.com/artist/sets/mix")
    bot = FakeBot()
    stops = DownloadRegistry()
    unknown = TrackMeta(url="https://soundcloud.com/artist/song", title="", performer="", duration=200)
    real = TrackMeta(url=unknown.url, title="Настоящее имя", performer="Настоящий артист", duration=200)
    probe = ProbeResult("Mix", True, (unknown,), 1)

    async def scenario() -> str:
        downloader = BlockingDownloader(probe, metadata=real)
        task = asyncio.create_task(
            on_text(message, bot, settings, downloader, defaultdict(asyncio.Lock), stops)
        )
        await asyncio.wait_for(downloader.started.wait(), 5)
        # Ждём, пока цикл прогресса перерисует сообщение с настоящим названием.
        for _ in range(100):
            if "Настоящее имя" in message.statuses[0].text:
                break
            await asyncio.sleep(0.01)
        text = message.statuses[0].text
        downloader.release.set()
        await asyncio.wait_for(task, 5)
        return text

    progress_text = run(scenario())

    assert "Настоящее имя" in progress_text
    assert "Настоящий артист" in progress_text
    assert "Трек без названия" not in progress_text
    # Заглушка не должна мелькать ни в одном обновлении статуса.
    assert not any(
        "Трек без названия" in text for text in message.statuses[0].history
    )
