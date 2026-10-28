"""Telegram upload with explicit size-limit checks and no automatic splitting."""
from __future__ import annotations
from pathlib import Path
from telegram import Bot
from config import Settings
from planner import PermanentError


def make_bot(settings: Settings) -> Bot:
    kwargs = {}
    if settings.telegram_api_base_url:
        root = settings.telegram_api_base_url.rstrip("/")
        kwargs["base_url"] = root + "/bot"
        kwargs["base_file_url"] = root + "/file/bot"
        kwargs["local_mode"] = True
    return Bot(token=settings.telegram_bot_token, **kwargs)


async def send_full_video(bot: Bot, settings: Settings, video_path: Path, caption: str, duration_s: int | None = None) -> None:
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise RuntimeError("The output video is missing or empty; refusing to upload it.")
    size_mb = video_path.stat().st_size / (1024 * 1024)
    if settings.telegram_api_base_url:
        limit_mb = settings.telegram_local_max_upload_mb
        mode = "local Bot API server"
    else:
        limit_mb = settings.telegram_cloud_max_upload_mb
        mode = "official Telegram Bot API"
    if size_mb > limit_mb:
        raise PermanentError(
            f"Output is {size_mb:.1f} MB, above the configured {limit_mb} MB limit for the {mode}. "
            "The video is never split. Use a local Telegram Bot API server for large uploads."
        )
    extra = {"duration": int(duration_s)} if duration_s else {}
    with video_path.open("rb") as stream:
        await bot.send_video(
            chat_id=settings.telegram_chat_id,
            video=stream,
            filename=video_path.name,
            caption=caption[:1024],
            supports_streaming=True,
            width=settings.output_width,
            height=settings.output_height,
            read_timeout=3600,
            write_timeout=3600,
            connect_timeout=60,
            pool_timeout=60,
            **extra,
        )
