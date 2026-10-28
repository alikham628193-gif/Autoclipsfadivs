"""FADIVS Twitch full-stream automation worker.

Detects a live-to-offline transition, waits for the matching archive, processes it whole,
then uploads a single MP4 to the configured Telegram channel.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from config import Settings
from editor import make_vertical_video
from state import StateStore
from telegram_upload import make_bot, send_full_video
from twitch import TwitchClient, parse_twitch_time


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def check_disk_space(workdir: Path, min_gb: float) -> None:
    free_gb = shutil.disk_usage(workdir).free / (1024 ** 3)
    if free_gb < min_gb:
        raise RuntimeError(f"Not enough free disk space: {free_gb:.1f} GB available, {min_gb:.1f} GB required. Free space and retry.")


async def run() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    log = logging.getLogger("fadivs")
    settings.workdir.mkdir(parents=True, exist_ok=True)
    state = StateStore(settings.workdir / "state.json")
    twitch = TwitchClient(settings.twitch_client_id, settings.twitch_client_secret, settings.twitch_channel)
    bot = make_bot(settings)

    log.info("FADIVS full-stream worker started | Twitch=%s | Telegram=%s", settings.twitch_channel, settings.telegram_chat_id)
    if settings.telegram_api_base_url:
        log.info("Large-upload mode enabled through custom Telegram Bot API base URL: %s", settings.telegram_api_base_url)
    else:
        log.warning("Using official Telegram cloud API; large video uploads are limited (configured %s MB).", settings.telegram_cloud_max_upload_mb)

    async with bot:
        # Fail early on invalid bot token/channel connection instead of restarting silently forever.
        me = await bot.get_me()
        log.info("Telegram bot authenticated as @%s", me.username or me.id)
        while True:
            try:
                await one_poll_cycle(settings, twitch, bot, state, log)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling/processing cycle failed. Pending work will be retried.")
            await asyncio.sleep(settings.poll_seconds)


async def one_poll_cycle(settings: Settings, twitch: TwitchClient, bot, state: StateStore, log: logging.Logger) -> None:
    current = await asyncio.to_thread(twitch.current_stream)
    is_live = current is not None

    # On first startup, do not accidentally post a months-old VOD. Only track a live stream
    # already in progress, or a live->offline transition observed by this worker.
    if not state.data.get("initialized"):
        if is_live:
            started_at = current.get("started_at") or utc_now_iso()
            state.update(initialized=True, was_live=True, active_started_at=started_at)
            log.info("Worker started while channel is live; tracking stream started at %s", started_at)
        else:
            state.update(initialized=True, was_live=False, active_started_at=None)
            log.info("Worker initialized while channel is offline; waiting for the next stream.")
        return

    if is_live:
        if not state.data.get("was_live"):
            started_at = current.get("started_at") or utc_now_iso()
            state.update(was_live=True, active_started_at=started_at)
            log.info("Live stream detected: %s", started_at)
        elif not state.data.get("active_started_at"):
            state.update(active_started_at=current.get("started_at") or utc_now_iso())
        return

    if state.data.get("was_live"):
        pending_start = state.data.get("active_started_at") or utc_now_iso()
        state.update(was_live=False, active_started_at=None, pending_started_at=pending_start)
        log.info("Stream went offline; waiting for Twitch to publish the full VOD (started %s).", pending_start)

    pending = parse_iso(state.data.get("pending_started_at"))
    if not pending:
        return

    # VOD publication may lag the stream ending. Wait a little before querying the archive list.
    await asyncio.sleep(settings.vod_wait_seconds)
    vod = await asyncio.to_thread(twitch.latest_vod_started_after, pending)
    if not vod:
        log.info("Matching VOD is not visible yet; will retry in %s seconds.", settings.poll_seconds)
        return
    if vod["id"] == state.data.get("last_processed_vod_id"):
        log.info("VOD %s was already processed; clearing pending stream state.", vod["id"])
        state.update(pending_started_at=None)
        return

    workdir = settings.workdir
    raw_path = workdir / f"vod-{vod['id']}.mp4"
    output_path = workdir / f"FADIVS-{vod['id']}-full-vertical.mp4"
    check_disk_space(workdir, settings.min_free_disk_gb)
    log.info("Downloading complete VOD %s | %s | %s", vod["id"], vod["title"], vod.get("duration", "duration unavailable"))
    await asyncio.to_thread(twitch.download_vod, vod["url"], raw_path, settings.yt_dlp_cookies_file)
    log.info("Downloaded source: %.1f MB", raw_path.stat().st_size / (1024 * 1024))
    log.info("Rendering a single continuous vertical MP4; no highlight detection or clip splitting is used.")
    await asyncio.to_thread(make_vertical_video, raw_path, output_path, settings)
    log.info("Rendered output: %.1f MB", output_path.stat().st_size / (1024 * 1024))
    caption = f"FADIVS | Толық стрим\n{vod['title']}\nБір тұтас видео — бөліктерге бөлінбеген."
    await send_full_video(bot, settings, output_path, caption)
    state.update(last_processed_vod_id=vod["id"], pending_started_at=None)
    log.info("Successfully uploaded full VOD %s to Telegram channel %s", vod["id"], settings.telegram_chat_id)
    if not settings.keep_files:
        raw_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
        log.info("Temporary media files removed; state database retained.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Stopped by user.")
