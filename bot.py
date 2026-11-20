"""Точка входа: запуск Telegram-бота в режиме long polling."""
#ffff
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections import defaultdict

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError
from aiogram.types import BotCommand, ErrorEvent
from aiogram.utils.token import TokenValidationError

from soundcloud_bot.config import ConfigError, Settings, load_settings
from soundcloud_bot.downloader import SoundCloudDownloader, cleanup_old_workdirs
from soundcloud_bot.handlers import DownloadRegistry, router

log = logging.getLogger(__name__)


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # События aiogram (каждый апдейт) — только в DEBUG.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def _make_session(settings: Settings) -> AiohttpSession | None:
    if not settings.telegram_api_url:
        return None
    log.info("Использую локальный Telegram Bot API: %s", settings.telegram_api_url)
    return AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_url))


def _register_error_handler(dp: Dispatcher) -> None:
    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.error(
            "Необработанная ошибка при апдейте %s",
            event.update.update_id,
            exc_info=event.exception,
        )
        return True


async def _start_dummy_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", 10000))).start()
    return runner


async def main() -> None:
    settings = load_settings()
    _setup_logging(settings)

    if shutil.which("ffmpeg") is None:
        raise SystemExit(
            "ffmpeg не найден в PATH. Установите его:\n"
            "  Ubuntu/Debian: sudo apt install ffmpeg\n"
            "  macOS:         brew install ffmpeg\n"
            "  Windows:       winget install Gyan.FFmpeg"
        )

    if settings.max_file_mb > 50 and not settings.telegram_api_url:
        log.warning(
            "MAX_FILE_MB=%s больше лимита облачного Telegram Bot API (50 МБ). "
            "Файлы крупнее 50 МБ не отправятся без TELEGRAM_API_URL.",
            settings.max_file_mb,
        )

    cleanup_old_workdirs(settings.download_dir)

    session = _make_session(settings)
    try:
        bot = Bot(
            token=settings.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
        )
    except TokenValidationError as exc:
        raise SystemExit("BOT_TOKEN не похож на токен Telegram. Скопируйте его из @BotFather целиком.") from exc
    dp = Dispatcher()
    dp["settings"] = settings
    dp["downloader"] = SoundCloudDownloader(settings)
    dp["user_locks"] = defaultdict(asyncio.Lock)
    dp["stops"] = DownloadRegistry()
    dp.include_router(router)
    _register_error_handler(dp)

    web_runner = await _start_dummy_server()

    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Приветствие"),
                BotCommand(command="help", description="Как пользоваться"),
                BotCommand(command="stop", description="Остановить загрузку плейлиста"),
            ]
        )
        await bot.delete_webhook(drop_pending_updates=False)
        me = await bot.get_me()
        log.info("Бот @%s запущен, жду ссылки на SoundCloud", me.username)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError as exc:
        raise SystemExit("Telegram отклонил BOT_TOKEN. Проверьте токен от @BotFather.") from exc
    except TelegramAPIError as exc:
        raise SystemExit(f"Не удалось подключиться к Telegram: {exc}") from exc
    finally:
        await web_runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
