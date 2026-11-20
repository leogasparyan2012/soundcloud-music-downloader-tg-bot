"""Хендлеры aiogram: команды и обработка ссылок SoundCloud."""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import DefaultDict
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from .config import Settings
from .downloader import DownloadError, Media, ProbeResult, SoundCloudDownloader, TrackMeta, new_workdir
from .utils import extract_soundcloud_url, human_size, progress_bar

log = logging.getLogger(__name__)

UserLocks = DefaultDict[int, asyncio.Lock]
# Состояние прогресса: доля, байты, скорость и уточнённые метаданные из yt-dlp.
ProgressState = dict[str, float | int | str | None]
# Сколько ждать, пока yt-dlp определит настоящее название трека (плейлисты SoundCloud
# отдают его только при извлечении, обычно это 1–5 секунд).
METADATA_WAIT_SECONDS = 10.0


@dataclass(slots=True)
class ActiveDownload:
    """Загрузка одного пользователя: по этому объекту работает /stop."""

    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_track: TrackMeta | None = None
    actual_track: TrackMeta | None = None
    metadata_ready: asyncio.Event = field(default_factory=asyncio.Event)


class DownloadRegistry:
    """Активные загрузки пользователей, чтобы /stop попадал в нужную."""

    def __init__(self) -> None:
        self._sessions: dict[int, ActiveDownload] = {}

    def begin(self, user_id: int) -> ActiveDownload:
        session = ActiveDownload()
        self._sessions[user_id] = session
        return session

    def end(self, user_id: int, session: ActiveDownload) -> None:
        if self._sessions.get(user_id) is session:
            self._sessions.pop(user_id, None)

    def request_stop(self, user_id: int) -> ActiveDownload | None:
        """Просит остановиться; None, если активной загрузки нет."""
        session = self._sessions.get(user_id)
        if session is not None:
            session.stop_event.set()
        return session


def _h(text: object) -> str:
    """Экранирует текст для parse_mode=HTML."""
    return html.escape(str(text), quote=False)


def _track_title(track: TrackMeta) -> str:
    return track.title or "Трек без названия"


def _track_error_text(track: TrackMeta, details: str, index: int, total: int) -> str:
    """Сообщение об ошибке: название, а если его нет — номер трека и ссылка."""
    if track.title:
        header = f"⚠️ <b>{_h(track.title)}</b>"
    elif total > 1:
        header = f"⚠️ <b>Трек {index} из {total}</b>"
    else:
        header = "⚠️ <b>Не удалось скачать трек</b>"
    text = f"{header}\n{_h(details)}"
    if track.url:
        text += f'\n🔗 <a href="{html.escape(track.url)}">Открыть в SoundCloud</a>'
    return text


# Подписи для плейлистов и релизов SoundCloud (set_type из API).
COLLECTION_LABELS = {
    "album": ("💿", "Альбом"),
    "ep": ("💿", "EP"),
    "single": ("💿", "Сингл"),
    "compilation": ("💿", "Сборник"),
    "playlist": ("🎧", "Плейлист"),
}
# Сегменты URL, которые означают профиль/подборку, а не отдельный трек.
_PROFILE_PATHS = {"tracks", "albums", "sets", "reposts", "likes", "spotlight", "comments"}


def _probing_text(url: str) -> str:
    """Текст статуса, пока yt-dlp ещё определяет, что скачивать."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if any(part in ("sets", "albums") for part in parts):
        return "🔎 Получаю информацию о плейлисте или альбоме…"
    if len(parts) == 2 and parts[1] not in _PROFILE_PATHS:
        return "🔎 Получаю информацию о треке…"
    return "🔎 Получаю информацию…"


def _collection_label(probe: ProbeResult) -> tuple[str, str]:
    return COLLECTION_LABELS.get(probe.kind, ("🎧", "Плейлист"))


def _start_text(settings: Settings) -> str:
    return (
        "🎧 <b>Привет! Я скачиваю музыку из SoundCloud.</b>\n\n"
        "Пришли ссылку на трек, плейлист или альбом — верну MP3 с обложкой и тегами.\n\n"
        "<b>Подходящие ссылки</b>\n"
        "<code>https://soundcloud.com/artist/track</code>\n"
        "<code>https://on.soundcloud.com/XXXXXX</code>\n\n"
        f"<b>Ограничения</b>\n"
        f"• из плейлиста — до {settings.max_playlist_items} треков за раз\n"
        f"• размер файла — до {settings.max_file_mb} МБ (лимит Telegram)\n\n"
        "Остановить большой плейлист: /stop — текущий трек докачается и отправится.\n\n"
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
        "/help — эта справка\n"
        "/stop — остановить загрузку плейлиста: текущий трек докачается и отправится\n\n"
        "<b>Что может не получиться</b>\n"
        "• приватные и удалённые треки;\n"
        "• треки с DRM-защитой — SoundCloud не отдаёт их для скачивания;\n"
        f"• файлы больше {settings.max_file_mb} МБ — их Telegram не принимает;\n"
        "• треки, заблокированные в регионе сервера.\n\n"
        "<i>Скачивай только тот контент, на который у тебя есть права.</i>"
    )


async def cmd_start(message: Message, settings: Settings) -> None:
    await message.answer(_start_text(settings))


async def cmd_help(message: Message, settings: Settings) -> None:
    await message.answer(_help_text(settings))


async def cmd_stop(message: Message, stops: DownloadRegistry) -> None:
    if message.from_user is None:
        await message.answer("🛑 Сейчас нечего останавливать — активной загрузки нет.")
        return

    session = stops.request_stop(message.from_user.id)
    if session is None:
        await message.answer("🛑 Сейчас нечего останавливать — активной загрузки нет.")
        return

    if session.current_track is None:
        await message.answer(
            "🛑 Останавливаю загрузку: сейчас трек не качается — "
            "следующие качать не буду."
        )
        return

    # В flat-плейлистах название может быть пустым — ждём, пока yt-dlp его определит.
    track = session.actual_track or session.current_track
    if not track.title and not session.metadata_ready.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(session.metadata_ready.wait(), METADATA_WAIT_SECONDS)
        track = session.actual_track or session.current_track

    if track.title:
        await message.answer(
            "🛑 Останавливаю загрузку.\n"
            f"Трек «{_h(_track_title(track))}» уже качается — "
            "докачаю, отправлю и на этом закончу."
        )
        return

    await message.answer(
        "🛑 Останавливаю загрузку.\n"
        "Текущий трек докачаю, отправлю и на этом закончу."
    )


async def cmd_unknown(message: Message) -> None:
    await message.answer("Не знаю такую команду 🤔\n\n/help — что я умею.")


async def on_text(
    message: Message,
    bot: Bot,
    settings: Settings,
    downloader: SoundCloudDownloader,
    user_locks: UserLocks,
    stops: DownloadRegistry,
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
        await _process_url(message, bot, url, downloader, settings, stops)


# ------------------------------------------------------------------ роутер


def build_router() -> Router:
    """Создаёт роутер заново — удобно для тестов и переиспользования."""
    router = Router(name="soundcloud")
    router.message.register(cmd_start, Command("start"))
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_stop, Command("stop"))
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
    stops: DownloadRegistry,
) -> None:
    user_id = message.from_user.id if message.from_user else message.chat.id
    session = stops.begin(user_id)
    try:
        await _download_tracks(message, bot, url, downloader, settings, session)
    finally:
        stops.end(user_id, session)


async def _download_tracks(
    message: Message,
    bot: Bot,
    url: str,
    downloader: SoundCloudDownloader,
    settings: Settings,
    session: ActiveDownload,
) -> None:
    probing = _probing_text(url)
    try:
        status = await message.reply(probing)
    except TelegramBadRequest:
        # Исходное сообщение могли удалить — тогда просто пишем в чат.
        status = await message.answer(probing)
    try:
        probe = await downloader.probe(url)
    except DownloadError as exc:
        await _safe_edit(status, f"❌ {_h(exc)}")
        return

    tracks = probe.tracks[: settings.max_playlist_items]
    if not tracks:
        await _safe_edit(status, "😔 По ссылке не нашлось треков.")
        return

    if session.stop_event.is_set():
        await _safe_edit(status, "🛑 Остановлено — ни один трек не отправлен.")
        return

    if probe.is_playlist:
        note = ""
        if probe.total > len(tracks):
            note = f"\nСкачаю первые <b>{len(tracks)}</b> из {probe.total}."
        emoji, kind = _collection_label(probe)
        await _safe_edit(
            status,
            f"{emoji} {kind} <b>{_h(probe.title)}</b>\n"
            f"Треков к загрузке: <b>{len(tracks)}</b>.{note}",
        )
    else:
        track = probe.tracks[0]
        heading = f"🎵 <b>{_h(track.title or probe.title)}</b>"
        if track.performer:
            heading += f"\n<i>{_h(track.performer)}</i>"
        await _safe_edit(status, heading)

    sent = 0
    failed = 0
    stopped = False
    for index, track in enumerate(tracks, start=1):
        # /stop мог прийти между треками — тогда новый уже не начинаем.
        if session.stop_event.is_set():
            stopped = True
            break

        session.current_track = track
        session.actual_track = None
        session.metadata_ready.clear()
        try:
            workdir = new_workdir(settings.download_dir)
        except OSError:
            log.exception("Не удалось создать временную папку в %s", settings.download_dir)
            await message.answer("⚠️ На сервере закончилось место для временных файлов.")
            session.current_track = None
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
                stop_event=session.stop_event,
                session=session,
            )
            if media.filesize > settings.max_file_bytes:
                raise DownloadError(
                    f"Файл весит {human_size(media.filesize)}, а Telegram принимает не больше "
                    f"{settings.max_file_mb} МБ. Скачайте трек вручную по ссылке ниже."
                )
            await _send_audio(bot, message.chat.id, message.message_id, media)
            sent += 1
        except DownloadError as exc:
            failed += 1
            log.warning("Не удалось скачать %s: %s", track.url, exc)
            await message.reply(_track_error_text(track, str(exc), index, len(tracks)))
        except TelegramAPIError as exc:
            failed += 1
            log.warning("Telegram отклонил файл %s: %s", track.url, exc)
            await message.reply(
                _track_error_text(track, f"Telegram не принял файл: {exc}", index, len(tracks))
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            session.current_track = None

        # /stop во время загрузки: текущий трек уже отправлен, дальше не идём.
        if session.stop_event.is_set():
            stopped = True
            break

        if index < len(tracks):
            # Небольшая пауза, чтобы не ловить flood wait при плейлистах.
            await asyncio.sleep(0.7)

    if stopped:
        if sent:
            await _safe_edit(status, f"🛑 Остановлено: отправлено {sent} из {len(tracks)}.")
        else:
            await _safe_edit(status, "🛑 Остановлено — ни один трек не отправлен.")
        return

    if sent:
        summary = f"✅ Готово: {sent} из {len(tracks)}."
        if failed:
            summary += f" Пропущено с ошибкой: {failed}."
        await _safe_edit(status, summary)
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
    stop_event: asyncio.Event | None = None,
    session: ActiveDownload | None = None,
) -> Media:
    state: ProgressState = {
        "fraction": 0.0,
        "done": 0,
        "total": 0,
        "speed": None,
        "title": None,
        "performer": None,
    }
    meta_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_progress(fraction: float, done: int, total_bytes: int, speed: float | None) -> None:
        state["fraction"] = fraction
        state["done"] = done
        state["total"] = total_bytes
        state["speed"] = speed

    def publish_metadata() -> None:
        meta_event.set()
        if session is not None:
            session.metadata_ready.set()

    def on_metadata(meta: TrackMeta) -> None:
        # Вызывается из потока yt-dlp: название из flat-плейлиста может быть пустым.
        state["title"] = meta.title
        state["performer"] = meta.performer
        if session is not None:
            session.actual_track = meta
        try:
            loop.call_soon_threadsafe(publish_metadata)
        except RuntimeError:  # event loop уже закрыт — обновлять некуда
            pass

    progress_task = asyncio.create_task(
        _progress_loop(status, track, state, index, total, is_playlist, stop_event, meta_event)
    )
    try:
        return await downloader.download(track, workdir, on_progress, on_metadata)
    finally:
        progress_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await progress_task


async def _progress_loop(
    status: Message,
    track: TrackMeta,
    state: ProgressState,
    index: int,
    total: int,
    is_playlist: bool,
    stop_event: asyncio.Event | None = None,
    meta_event: asyncio.Event | None = None,
) -> None:
    first_edit = True
    while True:
        # Не показываем «Трек без названия» на старте: ждём метаданные от yt-dlp.
        if first_edit and meta_event is not None:
            first_edit = False
            if not track.title and not meta_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(meta_event.wait(), METADATA_WAIT_SECONDS)
                meta_event.clear()
        await _safe_edit(status, _progress_text(track, state, index, total, is_playlist, stop_event))
        if meta_event is None:
            await asyncio.sleep(2.5)
            continue
        # Обновляем сразу, как только yt-dlp узнал настоящее название трека.
        try:
            await asyncio.wait_for(meta_event.wait(), timeout=2.5)
        except TimeoutError:
            pass
        meta_event.clear()


def _progress_text(
    track: TrackMeta,
    state: ProgressState,
    index: int,
    total: int,
    is_playlist: bool,
    stop_event: asyncio.Event | None = None,
) -> str:
    raw_title = state.get("title") or track.title
    title = str(raw_title) if raw_title else "⏳ Определяю название…"
    performer = str(state.get("performer") or track.performer or "")
    if is_playlist:
        header = f"🎧 <b>{index}/{total}</b> • {_h(title)}"
    else:
        header = f"🎵 <b>{_h(title)}</b>"
    if performer:
        header += f"\n<i>{_h(performer)}</i>"

    fraction = float(state["fraction"] or 0.0)
    bar = progress_bar(fraction)
    if int(state["total"] or 0):
        details = f"\n{bar} {fraction * 100:.0f}%"
    else:
        details = f"\n{bar} 0%\n⏳ Скачиваю…"
    if stop_event is not None and stop_event.is_set():
        details += "\n\n🛑 Останавливаю после этого трека…"
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
