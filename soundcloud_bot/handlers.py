"""Хендлеры aiogram: команды и обработка ссылок SoundCloud."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import shutil
from pathlib import Path
from typing import DefaultDict

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from .config import Settings
from .downloader import DownloadError, Media, SoundCloudDownloader, TrackMeta, new_workdir
from .utils import extract_soundcloud_url, human_size, human_speed, progress_bar

log = logging.getLogger(__name__)

UserLocks = DefaultDict[int, asyncio.Lock]


def _h(text: object) -> str:
    """Экранирует текст для parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def _track_title(track: TrackMeta) -> str:
    return track.title or "Трек без названия"


def _start_text(settings: Settings) -> str:
    return (
        "🎧 <b>Привет! Я скачиваю музыку из SoundCloud.</b>\n\n"
        "Пришли ссылку на трек или плейлист — верну MP3 с обложкой и тегами.\n\n"
        "<b>Подходящие ссылки</b>\n"
        "<code>https://soundcloud.com/artist/track</code>\n"
        "<code>https://on.soundcloud.com/XXXXXX</code>\n\n"
        f"<b>Ограничения</b>\n"
        f"• из плейлиста — до {settings.max_playlist_items} треков за раз\n"
        f"• размер файла — до {settings.max_file_mb} МБ (лимит Telegram)\n\n"
        "Подробнее: /help"
    )


def _help_text(settings: Settings) -> str:
    return (
        "<b>Как пользоваться</b>\n"
        "1. Открой трек в SoundCloud → «Поделиться» → «Копировать ссылку».\n"
        "2. Отправь ссылку боту.\n"
        "3. Дождись MP3: бот сам вшьёт обложку, название и исполнителя.\n\n"
        "<b>Команды</b>\n"
        "/start — приветствие\n"
        "/help — эта справка\n\n"
        "<b>Что может не получиться</b>\n"
        "• приватные и удалённые треки;\n"
        f"• файлы больше {settings.max_file_mb} МБ — их Telegram не принимает;\n"
        "• треки, заблокированные в регионе сервера.\n\n"
        "<i>Скачивай только тот контент, на который у тебя есть права.</i>"
    )


async def cmd_start(message: Message, settings: Settings) -> None:
    await message.answer(_start_text(settings))


async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(_help_text(settings))


async def cmd_unknown(message: Message) -> None:
    await message.answer("Не знаю такую команду 🤔\n\n/help — что я умею.")


async def on_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    downloader: SoundCloudDownloader,
    user_locks: UserLocks,
) -> None:
    if message.from_user is None:
        return

    url = extract_soundcloud_url(message.text)
    if url is None:
        if message.chat.type == ChatType.PRIVATE:
            await message.reply(
                "Это не похоже на ссылку SoundCloud 🤔\n\n"
                "Пришли ссылку вида <code>https://soundcloud.com/artist/track</code>."
            )
        return

    lock = user_locks[message.from_user.id]
    if lock.locked():
        await message.reply("⏳ Твоя предыдущая загрузка ещё идёт. Дождись её окончания.")
        return

    async with lock:
        await _process_url(message, bot, url, downloader, settings)


# ------------------------------------------------------------------ роутер


def build_router() -> Router:
    """Создаёт роутер заново — удобно для тестов и переиспользования."""
    router = Router(name="soundcloud")
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_unknown, F.text.startswith("/"))
    router.message.register(on_text, F.text)
    return router


router = build_router()


# --------------------------------------------------------------------- логика


async def _process_url(
    message: Message,
    bot: Bot,
    url: str,
    downloader: SoundCloudDownloader,
    settings: Settings,
) -> None:
    try:
        status = await message.reply("🔎 Получаю информацию о треке…")
    except TelegramBadRequest:
        # Исходное сообщение могли удалить — тогда просто пишем в чат.
        status = await message.answer("🔎 Получаю информацию о треке…")
    try:
        probe = await downloader.probe(url)
    except DownloadError as exc:
        await _safe_edit(status, f"❌ {_h(exc)}")
        return

    tracks = probe.tracks[: settings.max_playlist_items]
    if not tracks:
        await _safe_edit(status, "😔 По ссылке не нашлось треков.")
        return

    if probe.is_playlist:
        note = ""
        if probe.total > len(tracks):
            note = f"\nСкачаю первые <b>{len(tracks)}</b> из {probe.total}."
        await _safe_edit(
            status,
            f"🎧 Плейлист <b>{_h(probe.title)}</b>\n"
            f"Треков к загрузке: <b>{len(tracks)}</b>.{note}",
        )

    sent = 0
    for index, track in enumerate(tracks, start=1):
        try:
            workdir = new_workdir(settings.download_dir)
        except OSError:
            log.exception("Не удалось создать временную папку в %s", settings.download_dir)
            await message.answer("⚠️ На сервере закончилось место для временных файлов.")
            break
        try:
            media = await _download_with_progress(
                downloader=downloader,
                track=track,
                workdir=workdir,
                status=status,
                index=index,
                total=len(tracks),
                is_playlist=probe.is_playlist,
            )
            if media.filesize > settings.max_file_bytes:
                raise DownloadError(
                    f"Файл весит {human_size(media.filesize)}, а Telegram принимает не больше "
                    f"{settings.max_file_mb} МБ. Ссылка на трек: {track.url}"
                )
            await _send_audio(bot, message.chat.id, message.message_id, media)
            sent += 1
        except DownloadError as exc:
            log.warning("Не удалось скачать %s: %s", track.url, exc)
            await message.reply(f"⚠️ <b>{_h(_track_title(track))}</b>\n{_h(exc)}")
        except TelegramAPIError as exc:
            log.warning("Telegram отклонил файл %s: %s", track.url, exc)
            await message.reply(f"⚠️ Telegram не принял файл «{_h(_track_title(track))}»: {_h(exc)}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        if index < len(tracks):
            # Небольшая пауза, чтобы не ловить flood wait при плейлистах.
            await asyncio.sleep(0.7)

    if sent:
        await _safe_edit(status, f"✅ Готово: {sent} из {len(tracks)}.")
        await asyncio.sleep(2.5)
        with contextlib.suppress(TelegramAPIError):
            await status.delete()
    else:
        await _safe_edit(status, "😔 Не удалось скачать ни один трек.")


async def _download_with_progress(
    downloader: SoundCloudDownloader,
    track: TrackMeta,
    workdir: Path,
    status: Message,
    index: int,
    total: int,
    is_playlist: bool,
) -> Media:
    state: dict[str, float | int | None] = {"fraction": 0.0, "done": 0, "total": 0, "speed": None}

    def on_progress(fraction: float, done: int, total_bytes: int, speed: float | None) -> None:
        state["fraction"] = fraction
        state["done"] = done
        state["total"] = total_bytes
        state["speed"] = speed

    progress_task = asyncio.create_task(
        _progress_loop(status, track, state, index, total, is_playlist)
    )
    try:
        return await downloader.download(track, workdir, on_progress)
    finally:
        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task


async def _progress_loop(
    status: Message,
    track: TrackMeta,
    state: dict[str, float | int | None],
    index: int,
    total: int,
    is_playlist: bool,
) -> None:
    while True:
        await _safe_edit(status, _progress_text(track, state, index, total, is_playlist))
        await asyncio.sleep(2.5)


def _progress_text(
    track: TrackMeta,
    state: dict[str, float | int | None],
    index: int,
    total: int,
    is_playlist: bool,
) -> str:
    if is_playlist:
        header = f"🎧 <b>{index}/{total}</b> • {_h(_track_title(track))}"
    else:
        header = f"🎵 <b>{_h(_track_title(track))}</b>"
    if track.performer:
        header += f"\n<i>{_h(track.performer)}</i>"

    fraction = float(state["fraction"] or 0.0)
    bar = progress_bar(fraction)
    total_bytes = int(state["total"] or 0)
    done_bytes = int(state["done"] or 0)
    if total_bytes:
        details = f"\n{bar} {fraction * 100:.0f}%\n{human_size(done_bytes)} из {human_size(total_bytes)}"
        speed = human_speed(state["speed"])  # type: ignore[arg-type]
        if speed:
            details += f" • {speed}"
    else:
        details = f"\n{bar} 0%\n⏳ Скачиваю…"
    return header + details


async def _send_audio(bot: Bot, chat_id: int, reply_to: int, media: Media) -> None:
    track = media.track
    kwargs: dict = {
        "chat_id": chat_id,
        "audio": FSInputFile(media.file_path),
        "title": (track.title or media.file_path.stem)[:250],
        "performer": (track.performer or "SoundCloud")[:250],
        "caption": f'🔗 <a href="{html.escape(track.url)}">SoundCloud</a>',
        "reply_to_message_id": reply_to,
    }
    if track.duration:
        kwargs["duration"] = track.duration
    if media.thumbnail_path:
        kwargs["thumbnail"] = FSInputFile(media.thumbnail_path)

    with contextlib.suppress(TelegramAPIError):
        await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)

    for attempt in range(3):
        try:
            await bot.send_audio(**kwargs)
            return
        except TelegramBadRequest as exc:
            # Исходное сообщение могли удалить, пока шла загрузка.
            if "reply" in str(exc).lower() and "reply_to_message_id" in kwargs:
                log.info("Не удалось ответить на сообщение, отправляю без reply: %s", exc)
                kwargs.pop("reply_to_message_id", None)
                await bot.send_audio(**kwargs)
                return
            raise
        except TelegramRetryAfter as exc:
            if attempt == 2:
                raise
            log.info("Flood wait %s секунд, повторяю отправку", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)


async def _safe_edit(message: Message, text: str) -> None:
    try:
        await message.edit_text(text)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("Не удалось обновить статус: %s", exc)
    except TelegramAPIError as exc:
        log.debug("Не удалось обновить статус: %s", exc)
