"""Загрузка треков SoundCloud через yt-dlp и ffmpeg."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import (
    DownloadError as YtDlpDownloadError,
    PostProcessingError,
    UnsupportedError,
)

from .config import Settings
from .utils import clean_error, safe_filename

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
PLACEHOLDER_TITLES = {"[deleted track]", "[private track]", "[deleted]", "deleted track"}

# Колбэк прогресса: (доля 0..1, скачано байт, всего байт, скорость байт/с).
ProgressCallback = Callable[[float, int, int, float | None], None]


class DownloadError(RuntimeError):
    """Ошибка загрузки, текст которой можно показать пользователю."""


@dataclass(frozen=True, slots=True)
class TrackMeta:
    url: str
    title: str
    performer: str = ""
    duration: int | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    title: str
    is_playlist: bool
    tracks: tuple[TrackMeta, ...]
    total: int


@dataclass(frozen=True, slots=True)
class Media:
    track: TrackMeta
    file_path: Path
    thumbnail_path: Path | None
    filesize: int


class _YdlLogger:
    """Прячет вывод yt-dlp в консоль и переводит его в logging."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


def new_workdir(root: Path) -> Path:
    """Создаёт временную папку под один запрос пользователя."""
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="sc-", dir=root))


def cleanup_old_workdirs(root: Path, max_age_seconds: int = 24 * 3600) -> None:
    """Удаляет временные папки, оставшиеся после падений/перезапусков."""
    if not root.is_dir():
        return
    now = time.time()
    for path in root.iterdir():
        try:
            if not path.is_dir() or not path.name.startswith("sc-"):
                continue
            if now - path.stat().st_mtime > max_age_seconds:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:  # папку уже удалили параллельно
            continue


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class SoundCloudDownloader:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._cache_dir = settings.download_dir / ".cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        settings.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API

    async def probe(self, url: str) -> ProbeResult:
        """Определяет, трек это или плейлист, и возвращает метаданные."""
        return await self._run(self._probe_sync, url)

    async def download(
        self,
        track: TrackMeta,
        dest_dir: Path,
        on_progress: ProgressCallback | None = None,
    ) -> Media:
        """Скачивает трек в MP3, вшивает обложку и теги."""
        async with self._semaphore:
            return await self._run(self._download_sync, track, dest_dir, on_progress)

    # ------------------------------------------------------------- служебное

    async def _run(self, func: Callable[..., Media | ProbeResult], *args: object) -> Media | ProbeResult:
        try:
            async with asyncio.timeout(self._settings.download_timeout):
                return await asyncio.to_thread(func, *args)
        except TimeoutError as exc:
            raise DownloadError(
                f"Загрузка заняла больше {self._settings.download_timeout} секунд и была прервана. "
                "Попробуйте ещё раз или выберите другой трек."
            ) from exc
        except DownloadError:
            raise
        except YtDlpDownloadError as exc:
            raise DownloadError(self._humanize(exc)) from exc
        except UnsupportedError as exc:
            raise DownloadError("Это не похоже на ссылку SoundCloud.") from exc
        except PostProcessingError as exc:
            raise DownloadError(f"Не удалось собрать MP3: {clean_error(exc)}") from exc
        except FileNotFoundError as exc:
            raise DownloadError("На сервере не найден ffmpeg — без него нельзя собрать MP3.") from exc
        except Exception as exc:  # noqa: BLE001 — любую ошибку показываем пользователю
            log.exception("Непредвиденная ошибка загрузки")
            raise DownloadError(f"Внутренняя ошибка: {clean_error(exc)}") from exc

    def _common_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": _YdlLogger(),
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "extractor_retries": 3,
            "concurrent_fragment_downloads": 4,
            "cachedir": str(self._cache_dir),
            "noplaylist": True,
            "no_color": True,
        }
        if self._settings.cookies_file:
            opts["cookiefile"] = str(self._settings.cookies_file)
        if self._settings.proxy:
            opts["proxy"] = self._settings.proxy
        return opts

    # ---------------------------------------------------------------- probe

    def _probe_sync(self, url: str) -> ProbeResult:
        opts = self._common_opts() | {"extract_flat": "in_playlist", "noplaylist": False}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise DownloadError("Не удалось получить информацию по ссылке.")

        entries = [entry for entry in (info.get("entries") or []) if entry]
        if info.get("_type") in ("playlist", "multi_video") or entries:
            tracks: list[TrackMeta] = []
            for entry in entries:
                title = str(entry.get("title") or "").strip()
                entry_url = str(entry.get("url") or "")
                # В плейлистах-«профилях» могут попадаться вложенные сеты — пропускаем.
                if not entry_url or self._is_placeholder(title):
                    continue
                if "/sets/" in urlparse(entry_url).path:
                    continue
                tracks.append(
                    TrackMeta(
                        url=entry_url,
                        title=title,
                        # Исполнителя не берём у владельца плейлиста: он часто чужой.
                        performer=str(entry.get("uploader") or entry.get("channel") or "").strip(),
                        duration=_as_int(entry.get("duration")),
                    )
                )
            if not tracks:
                raise DownloadError("В плейлисте нет доступных для скачивания треков.")
            return ProbeResult(
                title=str(info.get("title") or "Плейлист"),
                is_playlist=True,
                tracks=tuple(tracks),
                total=len(tracks),
            )

        return ProbeResult(
            title=str(info.get("track") or info.get("title") or "Трек"),
            is_playlist=False,
            tracks=(self._info_to_track(info),),
            total=1,
        )

    @staticmethod
    def _info_to_track(info: dict) -> TrackMeta:
        return TrackMeta(
            url=str(info.get("webpage_url") or info.get("original_url") or ""),
            title=str(info.get("track") or info.get("title") or "Без названия").strip() or "Без названия",
            performer=str(info.get("artist") or info.get("uploader") or info.get("creator") or "").strip(),
            duration=_as_int(info.get("duration")),
        )

    @staticmethod
    def _is_placeholder(title: str) -> bool:
        lowered = title.strip().lower()
        return lowered in PLACEHOLDER_TITLES or lowered.startswith(("[deleted", "[private"))

    # ------------------------------------------------------------- download

    def _download_sync(
        self,
        track: TrackMeta,
        dest_dir: Path,
        on_progress: ProgressCallback | None,
    ) -> Media:
        dest_dir.mkdir(parents=True, exist_ok=True)
        hook = self._make_hook(on_progress) if on_progress else None
        opts = self._common_opts() | {
            "format": "bestaudio/best",
            "outtmpl": str(dest_dir / "source.%(ext)s"),
            "writethumbnail": True,
            "convertthumbnails": "jpg",
            "overwrites": True,
            "progress_hooks": [hook] if hook else [],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self._settings.audio_quality,
                }
            ],
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(track.url, download=True)
        if not info:
            raise DownloadError("Не удалось скачать трек.")

        # На случай, если по ссылке внезапно оказался плейлист.
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            info = entries[0]

        # Плоские метаданные из плейлиста могут быть неполными — дополняем из загрузки.
        if info.get("webpage_url") or info.get("title"):
            actual = self._info_to_track(info)
            track = replace(
                track,
                title=track.title or actual.title,
                performer=track.performer or actual.performer,
                duration=track.duration or actual.duration,
            )

        audio_path = self._find_audio(info, dest_dir)
        cover_src = self._find_cover(dest_dir)
        thumbnail = self._build_thumbnail(cover_src, dest_dir / "cover.jpg") if cover_src else None
        final_path = self._finalize(audio_path, track, dest_dir, thumbnail)
        if track.duration is None:
            track = replace(track, duration=self._probe_duration(final_path))
        return Media(
            track=track,
            file_path=final_path,
            thumbnail_path=thumbnail,
            filesize=final_path.stat().st_size,
        )

    @staticmethod
    def _probe_duration(path: Path) -> int | None:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1",
            str(path),
        ]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)
            return int(float(result.stdout.strip()))
        except (subprocess.SubprocessError, OSError, ValueError):
            return None

    @staticmethod
    def _make_hook(on_progress: ProgressCallback) -> Callable[[dict], None]:
        def hook(data: dict) -> None:
            try:
                status = data.get("status")
                if status == "downloading":
                    total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                    done = int(data.get("downloaded_bytes") or 0)
                    fraction = min(done / total, 0.99) if total else 0.0
                    on_progress(fraction, done, int(total), data.get("speed"))
                elif status == "finished":
                    total = int(
                        data.get("total_bytes")
                        or data.get("total_bytes_estimate")
                        or data.get("downloaded_bytes")
                        or 0
                    )
                    on_progress(1.0, total, total, None)
            except Exception:  # noqa: BLE001 — колбэк не должен ломать загрузку
                log.debug("Ошибка в колбэке прогресса", exc_info=True)

        return hook

    def _find_audio(self, info: dict, dest_dir: Path) -> Path:
        for item in info.get("requested_downloads") or []:
            filepath = item.get("filepath")
            if filepath and Path(filepath).is_file():
                return Path(filepath)

        candidates = [p for p in dest_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS]
        if candidates:
            candidates.sort(key=lambda p: (p.suffix.lower() != ".mp3", -p.stat().st_size))
            return candidates[0]
        raise DownloadError("yt-dlp не создал аудиофайл. Возможно, трек недоступен.")

    @staticmethod
    def _find_cover(dest_dir: Path) -> Path | None:
        for path in sorted(dest_dir.iterdir()):
            if path.suffix.lower() in IMAGE_EXTENSIONS and path.name != "cover.jpg":
                return path
        return None

    @staticmethod
    def _build_thumbnail(source: Path, target: Path) -> Path | None:
        """Готовит JPEG ≤320px и ≤200 КБ — требования Telegram к обложке аудио."""

        def convert(quality: int) -> bool:
            cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                "-frames:v", "1", "-q:v", str(quality),
                str(target),
            ]
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            except (subprocess.SubprocessError, OSError):
                log.warning("Не удалось подготовить обложку %s", source, exc_info=True)
                return False
            return target.is_file() and target.stat().st_size > 0

        if not convert(4):
            return None
        if target.stat().st_size > 200_000 and not (convert(10) and target.stat().st_size <= 200_000):
            log.warning("Обложка больше 200 КБ, пропускаю")
            return None
        return target

    def _finalize(self, audio: Path, track: TrackMeta, dest_dir: Path, cover: Path | None) -> Path:
        """Вшивает теги и обложку, переименовывает файл в «исполнитель - название.mp3»."""
        target = dest_dir / safe_filename(track.performer, track.title, ext=audio.suffix)
        if target == audio:
            return audio
        tagged = dest_dir / "tagged.mp3"

        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio),
        ]
        if cover:
            cmd += [
                "-i", str(cover),
                "-map", "0:a", "-map", "1:v",
                "-c:v", "copy", "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
            ]
        else:
            cmd += ["-map", "0:a"]

        cmd += [
            "-c:a", "copy",
            "-id3v2_version", "3",
            "-metadata", f"title={track.title}",
            "-metadata", f"artist={track.performer or 'SoundCloud'}",
            "-metadata", "album=SoundCloud",
            "-metadata", f"comment={track.url}",
            str(tagged),
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
        except (subprocess.SubprocessError, OSError):
            log.warning("Не удалось вшить теги/обложку, отдаю файл как есть", exc_info=True)
            audio.replace(target)
            return target

        tagged.replace(target)
        return target

    # ------------------------------------------------------------- ошибки

    @staticmethod
    def _humanize(exc: Exception) -> str:
        raw = clean_error(exc)
        lowered = raw.lower()

        if "ffmpeg" in lowered and ("not found" in lowered or "not installed" in lowered):
            return "На сервере не установлен ffmpeg — без него бот не соберёт MP3."
        if any(marker in lowered for marker in ("private", "sign in", "login required", "authentication")):
            return (
                "Трек приватный или требует входа в аккаунт. "
                "Если у вас есть доступ, настройте COOKIES_FILE (см. README)."
            )
        if any(
            marker in lowered
            for marker in (
                "available in your country",
                "available in your region",
                "geo restricted",
                "geo-restricted",
                "geo blocked",
                "geo-blocked",
                "blocked it in your country",
            )
        ):
            return "Трек недоступен в регионе, где работает сервер с ботом."
        if any(marker in lowered for marker in ("404", "not found", "does not exist", "no longer available")):
            return "Трек не найден — возможно, он удалён или ссылка неверна."
        if "unsupported url" in lowered:
            return "Это не похоже на ссылку SoundCloud."
        if "timed out" in lowered or "timeout" in lowered:
            return "SoundCloud не ответил вовремя. Попробуйте ещё раз."
        if "unable to download" in lowered or "failed to download" in lowered:
            return "SoundCloud не отдал аудиофайл. Попробуйте позже."
        return f"Не удалось скачать трек: {raw}"
