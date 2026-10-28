"""FADIVS Twitch full-stream automation worker.

Watches one Twitch channel, downloads each finished public VOD, renders one continuous
vertical MP4 with FFmpeg and uploads it to the configured Telegram channel.
The real work lives in pipeline.py; this file only wires everything together.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from config import Settings
from pipeline import Pipeline
from planner import redact
from state import StateStore
from telegram_upload import make_bot, send_full_video
from twitch import TwitchClient


class RedactingFormatter(logging.Formatter):
    """Never let tokens/secrets reach the logs (you may need to copy logs from a phone)."""

    def __init__(self, fmt: str, secrets: list[str]) -> None:
        super().__init__(fmt)
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)


async def startup_report(settings: Settings, twitch: TwitchClient, bot, me) -> str:
    lines = [f"✅ FADIVS бот қосылды (@{me.username or me.id})."]
    try:
        member = await bot.get_chat_member(chat_id=settings.telegram_chat_id, user_id=me.id)
        status = getattr(member, "status", "?")
        can_post = getattr(member, "can_post_messages", None)
        if status in ("administrator", "creator") and can_post is not False:
            lines.append(f"✅ {settings.telegram_chat_id}: әкімші, пост жариялай алады.")
        else:
            lines.append(f"⚠️ {settings.telegram_chat_id}: бот әкімші емес немесе пост жариялау құқығы жоқ (статус: {status}).")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Канал құқығын тексере алмадым: {type(exc).__name__}")
    try:
        await asyncio.to_thread(twitch.get_user_id)
        lines.append(f"✅ Twitch кілттері дұрыс, арна табылды: {settings.twitch_channel}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"⚠️ Twitch қатесі: {exc}")
    if settings.telegram_api_base_url:
        lines.append(f"ℹ️ Үлкен файл режимі қосулы: ≤ {settings.telegram_local_max_upload_mb} МБ.")
    else:
        lines.append(
            f"⚠️ Telegram бұлты: файл ≤ {settings.telegram_cloud_max_upload_mb} МБ. "
            "Толық стрим үшін жергілікті Bot API сервері керек."
        )
    return "\n".join(lines)


async def run() -> None:
    settings = Settings.from_env()
    secrets = [settings.telegram_bot_token, settings.twitch_client_secret, os.getenv("TELEGRAM_API_HASH", "").strip()]
    logging.basicConfig(level=getattr(logging, settings.log_level), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(RedactingFormatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s", secrets))
    log = logging.getLogger("fadivs")

    settings.workdir.mkdir(parents=True, exist_ok=True)
    state = StateStore(settings.workdir / "state.json")
    twitch = TwitchClient(settings.twitch_client_id, settings.twitch_client_secret, settings.twitch_channel)
    bot = make_bot(settings)

    async def notify(text: str) -> None:
        if not settings.telegram_admin_chat_id:
            return
        try:
            await bot.send_message(chat_id=settings.telegram_admin_chat_id, text=redact(text, secrets)[:4000])
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not send admin notification: %s", redact(str(exc), secrets))

    async def send(path: Path, caption: str, duration: int | None) -> None:
        await send_full_video(bot, settings, path, caption, duration_s=duration)

    pipeline = Pipeline(settings, twitch, state, send, notify)
    pipeline.apply_retry_ids()
    pipeline.cleanup_stale_files()

    log.info("FADIVS worker started | Twitch=%s | Telegram=%s", settings.twitch_channel, settings.telegram_chat_id)
    async with bot:
        me = await bot.get_me()  # fails early (and clearly) on a wrong bot token
        report = await startup_report(settings, twitch, bot, me)
        for line in report.splitlines():
            log.info(line)
        await notify(report)

        failures = 0
        while True:
            try:
                await pipeline.poll_once()
                if failures >= 5:
                    await notify("✅ Қате жойылды, бот қалыпты жұмыс істеп тұр.")
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.exception("Polling cycle failed (%d in a row). Will retry.", failures)
                if failures == 5:
                    await notify(f"⚠️ Бот 5 рет қатарынан қате берді: {type(exc).__name__}: {exc}")
            await asyncio.sleep(settings.poll_seconds)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped by user.")
