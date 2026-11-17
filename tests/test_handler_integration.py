"""Интеграционный тест: реальный aiogram + фейковая сессия Telegram."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import Chat, Message, Update, User

from soundcloud_bot.config import Settings
from soundcloud_bot.downloader import Media, ProbeResult, TrackMeta
from soundcloud_bot.handlers import build_router

TOKEN = "123456789:" + "A" * 35


class FakeSession(BaseSession):
    """Принимает любые вызовы Bot API и возвращает правдоподобные объекты."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,
    ) -> Any:
        self.calls.append(method)
        return Message(
            message_id=100 + len(self.calls),
            date=datetime.now(),
            chat=Chat(id=1, type="private"),
            text="ok",
        ).as_(bot)

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""


class FakeDownloader:
    async def probe(self, url: str) -> ProbeResult:
        return ProbeResult(
            title="Song",
            is_playlist=False,
            tracks=(TrackMeta(url=url, title="Song", performer="Artist", duration=200),),
            total=1,
        )

    async def download(self, track: TrackMeta, workdir: Path, on_progress=None) -> Media:
        await asyncio.sleep(0.01)
        if on_progress:
            on_progress(0.5, 5, 10, 1000)
        path = Path(workdir) / "Song.mp3"
        path.write_bytes(b"ID3" + b"0" * 128)
        return Media(track=track, file_path=path, thumbnail_path=None, filesize=path.stat().st_size)


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr("soundcloud_bot.handlers.asyncio.sleep", no_sleep)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token=TOKEN,
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


def test_full_update_flow(tmp_path: Path) -> None:
    async def scenario() -> None:
        session = FakeSession()
        bot = Bot(token=TOKEN, session=session)
        settings = make_settings(tmp_path)
        dp = Dispatcher()
        dp["settings"] = settings
        dp["downloader"] = FakeDownloader()
        dp["user_locks"] = defaultdict(asyncio.Lock)
        dp.include_router(build_router())

        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=1, type="private"),
            from_user=User(id=2, is_bot=False, first_name="Tester"),
            text="https://soundcloud.com/artist/song",
        )
        update = Update(update_id=1, message=message)

        await dp.feed_update(bot, update)
        await bot.session.close()

        methods = [type(call).__name__ for call in session.calls]
        assert "SendMessage" in methods
        assert "SendAudio" in methods
        assert "DeleteMessage" in methods

        audio = next(call for call in session.calls if type(call).__name__ == "SendAudio")
        assert audio.title == "Song"
        assert audio.performer == "Artist"
        assert audio.chat_id == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/start", "/help", "/unknown"])
def test_commands_do_not_crash(tmp_path: Path, text: str) -> None:
    async def scenario() -> None:
        session = FakeSession()
        bot = Bot(token=TOKEN, session=session)
        dp = Dispatcher()
        dp["settings"] = make_settings(tmp_path)
        dp["downloader"] = FakeDownloader()
        dp["user_locks"] = defaultdict(asyncio.Lock)
        dp.include_router(build_router())

        message = Message(
            message_id=1,
            date=datetime.now(),
            chat=Chat(id=1, type="private"),
            from_user=User(id=2, is_bot=False, first_name="Tester"),
            text=text,
        )
        await dp.feed_update(bot, Update(update_id=1, message=message))
        await bot.session.close()

        assert any(type(call).__name__ == "SendMessage" for call in session.calls)

    asyncio.run(scenario())
