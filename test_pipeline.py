"""Pipeline tests with fake Twitch/Telegram/FFmpeg: they prove the retry-loop bug is gone."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pipeline as pipeline_module
from config import Settings
from pipeline import Pipeline
from planner import PermanentError
from state import StateStore

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def make_settings(tmp: str, **extra) -> Settings:
    env = {
        "TELEGRAM_BOT_TOKEN": "dummy-token", "TELEGRAM_CHAT_ID": "@x",
        "TWITCH_CLIENT_ID": "dummy-id", "TWITCH_CLIENT_SECRET": "dummy-secret",
        "WORKDIR": tmp, "VOD_WAIT_SECONDS": "60", "RETRY_DELAY_MINUTES": "10", "MAX_ATTEMPTS": "3",
        **extra,
    }
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


class FakeTwitch:
    def __init__(self):
        self.stream = None
        self.vods = []

    def current_stream(self):
        return self.stream

    def list_archive_vods(self, limit=20):
        return list(self.vods)


def vod(vid, created, hours=1.0, stream_id=None):
    return {"id": vid, "title": f"Stream {vid}", "url": f"https://twitch.tv/videos/{vid}",
            "created_at": created, "duration_seconds": int(hours * 3600), "stream_id": stream_id}


class Harness:
    def __init__(self, tmp, **env):
        self.tmp = tmp
        self.settings = make_settings(tmp, **env)
        self.state = StateStore(Path(tmp) / "state.json")
        self.twitch = FakeTwitch()
        self.clock = T0
        self.downloads = 0
        self.renders = []
        self.sent = []
        self.notes = []
        self.send_failures = 0
        self.pipeline = self.build()

    def build(self):
        async def send(path, caption, duration):
            if self.send_failures > 0:
                self.send_failures -= 1
                raise ConnectionError("network down")
            self.sent.append((path.name, duration))

        async def notify(text):
            self.notes.append(text)

        def download(url, dest, cookies=""):
            self.downloads += 1
            dest.write_bytes(b"x" * 1000)

        def render(src, dst, settings, kbps):
            self.renders.append(kbps)
            dst.write_bytes(b"y" * 2000)

        return Pipeline(self.settings, self.twitch, self.state, send, notify,
                        download=download, render=render, now=lambda: self.clock)

    def init_offline(self):
        self.state.update(initialized=True, watermark=T0.isoformat())


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        # plenty of fake disk
        patcher = mock.patch.object(pipeline_module.shutil, "disk_usage",
                                    return_value=mock.Mock(total=500 * 1024**3, free=400 * 1024**3))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_first_start_offline_ignores_old_vods(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.twitch.vods = [vod("old", T0 - timedelta(days=2))]
        await h.pipeline.poll_once()   # initializes
        await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)
        self.assertEqual(h.sent, [])

    async def test_new_vod_is_downloaded_rendered_and_sent_once(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=2)]
        h.clock = T0 + timedelta(hours=5)
        await h.pipeline.poll_once()
        await h.pipeline.poll_once()   # must NOT process it again
        self.assertEqual((h.downloads, len(h.renders), len(h.sent)), (1, 1, 1))
        self.assertEqual(h.sent[0][1], 7200)
        self.assertEqual(list(Path(self.tmp).glob("*.mp4")), [])      # temp media removed
        self.assertIn("100", h.state.data["done_vod_ids"])

    async def test_upload_failure_does_not_redownload_or_reencode(self):
        """The original bug: a failed upload caused download+encode again and again."""
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1))]
        h.clock = T0 + timedelta(hours=5)
        h.send_failures = 2
        await h.pipeline.poll_once()                      # attempt 1 fails
        await h.pipeline.poll_once()                      # still inside retry delay: nothing happens
        self.assertEqual((h.downloads, len(h.renders)), (1, 1))
        h.clock += timedelta(minutes=11)
        await h.pipeline.poll_once()                      # attempt 2 fails
        h.clock += timedelta(minutes=21)
        await h.pipeline.poll_once()                      # attempt 3 succeeds
        self.assertEqual((h.downloads, len(h.renders), len(h.sent)), (1, 1, 1))
        self.assertEqual(len(h.notes), 3)                 # 2 warnings + 1 success

    async def test_gives_up_after_max_attempts_and_never_loops(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1))]
        h.clock = T0 + timedelta(hours=5)
        h.send_failures = 99
        for _ in range(10):
            await h.pipeline.poll_once()
            h.clock += timedelta(hours=1)
        self.assertEqual(h.downloads, 1)
        self.assertIn("100", h.state.data["failed_vod_ids"])
        self.assertEqual(list(Path(self.tmp).glob("*.mp4")), [])      # disk freed
        self.assertTrue(any(n.startswith("❌") for n in h.notes))

    async def test_stream_too_long_for_cloud_limit_fails_without_downloading(self):
        h = Harness(self.tmp)   # no local server -> 49 MB cloud limit
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=3)]
        h.clock = T0 + timedelta(hours=6)
        await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)
        self.assertIn("100", h.state.data["failed_vod_ids"])
        self.assertTrue(any("Bot API" in n for n in h.notes))

    async def test_long_stream_gets_reduced_bitrate(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=5)]
        h.clock = T0 + timedelta(hours=8)
        await h.pipeline.poll_once()
        self.assertEqual(len(h.renders), 1)
        self.assertLess(h.renders[0], 1400)
        self.assertGreaterEqual(h.renders[0], 450)

    async def test_short_stream_uses_configured_bitrate(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=1)]
        h.clock = T0 + timedelta(hours=4)
        await h.pipeline.poll_once()
        self.assertEqual(h.renders, [1400])

    async def test_vod_of_running_stream_is_skipped(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.stream = {"id": "s1", "started_at": (T0 + timedelta(hours=1)).isoformat(), "title": "live"}
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=1, stream_id="s1")]
        h.clock = T0 + timedelta(hours=5)
        await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)

    async def test_vod_waits_until_archive_is_finished(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=1)]
        h.clock = T0 + timedelta(hours=2, seconds=30)   # ended 30 s ago, wait is 60 s
        await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)
        h.clock += timedelta(seconds=60)
        await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 1)

    async def test_oldest_vod_first_and_missed_stream_is_caught(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("B", T0 + timedelta(hours=10)), vod("A", T0 + timedelta(hours=1))]
        h.clock = T0 + timedelta(hours=20)
        await h.pipeline.poll_once()
        await h.pipeline.poll_once()
        self.assertEqual([name for name, _ in h.sent], ["FADIVS-A-full-vertical.mp4", "FADIVS-B-full-vertical.mp4"])

    async def test_retry_vod_ids_processes_old_vod_once_even_after_restart(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081", RETRY_VOD_IDS="777")
        h.init_offline()
        h.twitch.vods = [vod("777", T0 - timedelta(days=3))]
        h.clock = T0 + timedelta(hours=1)
        h.pipeline.apply_retry_ids()
        await h.pipeline.poll_once()
        self.assertEqual(len(h.sent), 1)
        # "restart" the bot with the same env value
        h.pipeline = h.build()
        h.pipeline.apply_retry_ids()
        await h.pipeline.poll_once()
        self.assertEqual(len(h.sent), 1)

    async def test_disk_too_small_for_stream_is_permanent(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=6)]
        h.clock = T0 + timedelta(hours=9)
        with mock.patch.object(pipeline_module.shutil, "disk_usage",
                               return_value=mock.Mock(total=20 * 1024**3, free=19 * 1024**3)):
            await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)
        self.assertIn("100", h.state.data["failed_vod_ids"])

    async def test_temporarily_low_disk_is_retried_later(self):
        h = Harness(self.tmp, TELEGRAM_API_BASE_URL="http://local:8081")
        h.init_offline()
        h.twitch.vods = [vod("100", T0 + timedelta(hours=1), hours=1)]
        h.clock = T0 + timedelta(hours=5)
        with mock.patch.object(pipeline_module.shutil, "disk_usage",
                               return_value=mock.Mock(total=500 * 1024**3, free=2 * 1024**3)):
            await h.pipeline.poll_once()
        self.assertEqual(h.downloads, 0)
        self.assertNotIn("100", h.state.data["failed_vod_ids"])
        h.clock += timedelta(minutes=11)
        await h.pipeline.poll_once()
        self.assertEqual(len(h.sent), 1)

    async def test_stale_files_are_cleaned_on_startup(self):
        h = Harness(self.tmp)
        (Path(self.tmp) / "vod-999.mp4").write_bytes(b"x")
        (Path(self.tmp) / "FADIVS-999-full-vertical.mp4").write_bytes(b"x")
        (Path(self.tmp) / "vod-888.mp4.part").write_bytes(b"x")
        (Path(self.tmp) / "state.json").write_text("{}")
        h.state.data["jobs"]["555"] = {"attempts": 1}
        (Path(self.tmp) / "vod-555.mp4").write_bytes(b"x")   # belongs to a live job -> kept
        h.pipeline.cleanup_stale_files()
        left = sorted(p.name for p in Path(self.tmp).iterdir())
        self.assertEqual(left, ["state.json", "vod-555.mp4"])


if __name__ == "__main__":
    unittest.main()
