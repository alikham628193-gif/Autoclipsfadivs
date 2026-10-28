"""Polling + job pipeline for FADIVS full-stream processing.

How it works (simple version):
  * Every poll we list the channel's newest archive VODs from Twitch (public data).
  * A VOD is processed only if it is NEWER than the "watermark" (the moment the bot was
    first started), has finished, and was not already published or given up on.
  * Each VOD goes through three stages: download -> render -> upload. Every finished stage
    is remembered, so a failed upload NEVER triggers a new download or a new encode.
  * Failures are retried a few times with a growing delay, then the VOD is marked as failed
    (the bot never loops forever on the same video).

This module does not import Telegram, so it can be tested without any network or token.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from config import Settings
from planner import (
    GB,
    MB,
    PermanentError,
    bitrate_to_bps,
    estimate_disk_needed_gb,
    fit_video_kbps,
    max_duration_hours,
)

LIVE_GRACE = timedelta(minutes=5)
KEEP_HISTORY = 200

SendVideo = Callable[[Path, str, "int | None"], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]


async def _no_notify(text: str) -> None:
    return None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _non_empty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        twitch: Any,
        state: Any,
        send_video: SendVideo,
        notify: Notify = _no_notify,
        *,
        download: Callable[..., Any] | None = None,
        render: Callable[..., Any] | None = None,
        now: Callable[[], datetime] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.settings = settings
        self.twitch = twitch
        self.state = state
        self.send_video = send_video
        self.notify = notify
        if download is None:
            from twitch import TwitchClient
            download = TwitchClient.download_vod
        if render is None:
            from editor import make_vertical_video
            render = make_vertical_video
        self.download = download
        self.render = render
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.log = log or logging.getLogger("fadivs")

    # ------------------------------------------------------------------ startup helpers
    def apply_retry_ids(self) -> None:
        """RETRY_VOD_IDS: process/retry these VODs once, even if old or previously failed."""
        data = self.state.data
        consumed = list(data.get("retry_consumed", []))
        changed = False
        for vid in self.settings.retry_vod_ids:
            if vid in consumed:
                continue
            for key in ("done_vod_ids", "failed_vod_ids"):
                if vid in data[key]:
                    data[key].remove(vid)
            data["jobs"].pop(vid, None)
            if vid not in data["forced_vod_ids"]:
                data["forced_vod_ids"].append(vid)
            consumed.append(vid)
            changed = True
            self.log.info("RETRY_VOD_IDS: VOD %s will be processed once.", vid)
        if changed:
            data["retry_consumed"] = consumed[-KEEP_HISTORY:]
            self.state.save()

    def cleanup_stale_files(self) -> None:
        """Delete leftover media from jobs that no longer exist (frees disk after crashes)."""
        keep = set(self.state.data.get("jobs", {}).keys())
        workdir = self.settings.workdir
        removed = 0
        for pattern in ("vod-*.*", "FADIVS-*-full-vertical.*"):
            for path in workdir.glob(pattern):
                vid = path.name.split("-")[1] if path.name.startswith("FADIVS-") else path.name[4:].split(".")[0]
                if vid not in keep and path.is_file():
                    path.unlink(missing_ok=True)
                    removed += 1
        if removed:
            self.log.info("Removed %d stale media file(s) from the work folder.", removed)

    # ------------------------------------------------------------------ main entry
    async def poll_once(self) -> None:
        data = self.state.data
        stream = await asyncio.to_thread(self.twitch.current_stream)
        if not data.get("initialized"):
            self._initialize(stream)
            return
        vods = await asyncio.to_thread(self.twitch.list_archive_vods)
        await self._drop_vanished_jobs(vods)
        vod = self._next_eligible(vods, stream)
        if vod is not None:
            await self._process(vod)

    def _initialize(self, stream: dict[str, Any] | None) -> None:
        now = self.now()
        if stream:
            started = parse_iso(stream.get("started_at")) or now
            watermark = started - LIVE_GRACE
            self.log.info("Worker started while the channel is live; will publish this stream's VOD when it ends.")
        else:
            watermark = now
            self.log.info("Worker initialized while the channel is offline; waiting for the next stream.")
        self.state.update(initialized=True, watermark=watermark.isoformat())

    # ------------------------------------------------------------------ selection
    def _next_eligible(self, vods: list[dict[str, Any]], stream: dict[str, Any] | None) -> dict[str, Any] | None:
        data = self.state.data
        now = self.now()
        watermark = parse_iso(data.get("watermark"))
        done = set(data["done_vod_ids"])
        failed = set(data["failed_vod_ids"])
        forced = set(data["forced_vod_ids"])
        live_start = parse_iso(stream.get("started_at")) if stream else None
        for vod in sorted(vods, key=lambda v: v["created_at"]):
            vid = vod["id"]
            if vid in done or vid in failed:
                continue
            if vid not in forced and watermark and vod["created_at"] <= watermark:
                continue
            if stream and (
                (vod.get("stream_id") and vod["stream_id"] == stream.get("id"))
                or (live_start and vod["created_at"] >= live_start - LIVE_GRACE)
            ):
                continue  # this VOD belongs to the stream that is still running
            duration = vod.get("duration_seconds")
            if not duration:
                continue
            ready_at = vod["created_at"] + timedelta(seconds=duration + self.settings.vod_wait_seconds)
            if now < ready_at:
                continue  # Twitch may still be finalizing the archive
            job = data["jobs"].get(vid)
            if job and float(job.get("retry_after_ts", 0)) > now.timestamp():
                continue
            return vod
        return None

    async def _drop_vanished_jobs(self, vods: list[dict[str, Any]]) -> None:
        visible = {v["id"] for v in vods}
        for vid in list(self.state.data["jobs"].keys()):
            if vid not in visible:
                await self._fail(vid, "VOD is no longer listed on Twitch (deleted or expired).")

    # ------------------------------------------------------------------ processing
    def _limit_mb(self) -> tuple[int, bool]:
        s = self.settings
        local = bool(s.telegram_api_base_url)
        return (s.telegram_local_max_upload_mb if local else s.telegram_cloud_max_upload_mb), local

    def _paths(self, vid: str) -> tuple[Path, Path]:
        workdir = self.settings.workdir
        return workdir / f"vod-{vid}.mp4", workdir / f"FADIVS-{vid}-full-vertical.mp4"

    def _check_disk(self, duration: int, limit_mb: int, local: bool) -> None:
        s = self.settings
        need = max(s.min_free_disk_gb, estimate_disk_needed_gb(duration, s.assumed_source_mbps, limit_mb, local))
        usage = shutil.disk_usage(s.workdir)
        if need > usage.total / GB:
            raise PermanentError(
                f"This stream needs about {need:.0f} GB of free disk, but the whole disk is only "
                f"{usage.total / GB:.0f} GB. Use a bigger disk."
            )
        if usage.free / GB < need:
            raise RuntimeError(f"Not enough free disk space: {usage.free / GB:.1f} GB free, about {need:.0f} GB needed.")

    async def _process(self, vod: dict[str, Any]) -> None:
        s = self.settings
        data = self.state.data
        vid = vod["id"]
        duration = int(vod["duration_seconds"])
        job = data["jobs"].setdefault(vid, {"attempts": 0})
        raw, out = self._paths(vid)
        limit_mb, local = self._limit_mb()
        try:
            fit = fit_video_kbps(duration, limit_mb, s.audio_bitrate)
            if fit < s.min_video_kbps:
                longest = max_duration_hours(limit_mb, s.audio_bitrate, s.min_video_kbps)
                raise PermanentError(
                    f"Stream is {duration / 3600:.1f} h long, but the {limit_mb} MB upload limit fits at most "
                    f"~{longest:.1f} h at the minimum quality ({s.min_video_kbps} kbps)."
                    + ("" if local else " Set up the local Telegram Bot API server for large files.")
                )
            kbps = min(fit, bitrate_to_bps(s.video_bitrate) // 1000)

            if not (job.get("downloaded") and _non_empty(raw)):
                job["downloaded"] = False
                job["rendered"] = False
                self._check_disk(duration, limit_mb, local)
                self.log.info("Downloading VOD %s | %s | %.1f h", vid, vod["title"], duration / 3600)
                await asyncio.to_thread(self.download, vod["url"], raw, s.yt_dlp_cookies_file)
                job["downloaded"] = True
                self.state.save()
                self.log.info("Downloaded source: %.1f MB", raw.stat().st_size / MB)

            if not (job.get("rendered") and job.get("kbps") == kbps and _non_empty(out)):
                job["rendered"] = False
                self.log.info("Rendering one continuous vertical MP4 at %d kbps video.", kbps)
                await asyncio.to_thread(self.render, raw, out, s, kbps)
                size_mb = out.stat().st_size / MB
                self.log.info("Rendered output: %.1f MB", size_mb)
                if size_mb > limit_mb:
                    raise PermanentError(f"Rendered file is {size_mb:.0f} MB, above the {limit_mb} MB upload limit.")
                job["rendered"] = True
                job["kbps"] = kbps
                self.state.save()

            caption = f"FADIVS | Толық стрим\n{vod['title']}\nБір тұтас видео — бөліктерге бөлінбеген."
            await self.send_video(out, caption, duration)
        except asyncio.CancelledError:
            raise
        except PermanentError as exc:
            await self._fail(vid, str(exc))
            return
        except Exception as exc:
            job["attempts"] = int(job.get("attempts", 0)) + 1
            if job["attempts"] >= s.max_attempts:
                await self._fail(vid, f"{type(exc).__name__}: {exc} (after {job['attempts']} attempts)")
                return
            delay_min = s.retry_delay_minutes * job["attempts"]
            job["retry_after_ts"] = self.now().timestamp() + delay_min * 60
            self.state.save()
            self.log.exception("VOD %s failed (attempt %d/%d); retrying in %d min.", vid, job["attempts"], s.max_attempts, delay_min)
            await self.notify(f"⚠️ VOD {vid}: қате ({type(exc).__name__}). {delay_min} минуттан кейін қайталаймын ({job['attempts']}/{s.max_attempts}).")
            return

        # success
        self._append_limited("done_vod_ids", vid)
        data["jobs"].pop(vid, None)
        if vid in data["forced_vod_ids"]:
            data["forced_vod_ids"].remove(vid)
        self.state.save()
        self.log.info("Successfully uploaded VOD %s to Telegram channel %s", vid, s.telegram_chat_id)
        if not s.keep_files:
            raw.unlink(missing_ok=True)
            out.unlink(missing_ok=True)
        await self.notify(f"✅ VOD {vid} каналға жарияланды.")

    def _append_limited(self, key: str, value: str) -> None:
        items = self.state.data[key]
        if value not in items:
            items.append(value)
        del items[:-KEEP_HISTORY]

    async def _fail(self, vid: str, reason: str) -> None:
        data = self.state.data
        self._append_limited("failed_vod_ids", vid)
        data["jobs"].pop(vid, None)
        if vid in data["forced_vod_ids"]:
            data["forced_vod_ids"].remove(vid)
        self.state.save()
        for path in self._paths(vid):
            path.unlink(missing_ok=True)
        self.log.error("Giving up on VOD %s: %s", vid, reason)
        await self.notify(f"❌ VOD {vid} жарияланбады: {reason}")
