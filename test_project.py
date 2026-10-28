"""Local tests; run with: python -m unittest -v   (no network, no tokens needed)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from config import Crop
from editor import _bitrate_number, _crop_expr
from planner import (
    bitrate_to_bps, estimate_disk_needed_gb, fit_video_kbps, max_duration_hours,
    parse_twitch_duration, redact,
)
from state import StateStore, DEFAULT_STATE


class CropTests(unittest.TestCase):
    def test_valid_crop(self):
        Crop(0, 0, 50, 40).validate("CAM")

    def test_crop_overflow_rejected(self):
        with self.assertRaises(ValueError):
            Crop(80, 0, 30, 50).validate("CAM")

    def test_zero_size_rejected(self):
        with self.assertRaises(ValueError):
            Crop(0, 0, 0, 50).validate("GAME")

    def test_crop_expression_contains_rounding(self):
        expr = _crop_expr(Crop(10, 20, 50, 40))
        self.assertIn("trunc(iw*0.500000/2)*2", expr)
        self.assertIn("trunc(ih*0.400000/2)*2", expr)

    def test_bitrate_parser(self):
        self.assertEqual(_bitrate_number("1800k"), 1_800_000)
        self.assertEqual(_bitrate_number("2M"), 2_000_000)
        self.assertEqual(_bitrate_number("1200000"), 1_200_000)


class PlannerTests(unittest.TestCase):
    def test_duration_parser(self):
        self.assertEqual(parse_twitch_duration("3h8m33s"), 3 * 3600 + 8 * 60 + 33)
        self.assertEqual(parse_twitch_duration("45m10s"), 2710)
        self.assertEqual(parse_twitch_duration("30s"), 30)
        self.assertIsNone(parse_twitch_duration(""))
        self.assertIsNone(parse_twitch_duration("abc"))
        self.assertIsNone(parse_twitch_duration(None))

    def test_short_stream_keeps_configured_bitrate(self):
        self.assertGreater(fit_video_kbps(3600, 1850, "96k"), 1400)

    def test_long_stream_gets_lower_bitrate_that_really_fits(self):
        seconds = 5 * 3600
        kbps = fit_video_kbps(seconds, 1850, "96k")
        self.assertLess(kbps, 1400)
        total_mb = (kbps + 96) * 1000 * seconds / 8 / 1024 / 1024
        self.assertLessEqual(total_mb, 1850)

    def test_cloud_limit_only_fits_minutes(self):
        self.assertLess(max_duration_hours(49, "96k", 450), 0.25)

    def test_disk_estimate_grows_with_duration_and_local_server(self):
        short = estimate_disk_needed_gb(3600, 8, 1850, False)
        long_ = estimate_disk_needed_gb(4 * 3600, 8, 1850, False)
        local = estimate_disk_needed_gb(4 * 3600, 8, 1850, True)
        self.assertLess(short, long_)
        self.assertLess(long_, local)

    def test_redact_hides_secrets(self):
        text = "error at https://api.telegram.org/bot123456:ABCDEF-secret/getMe"
        self.assertNotIn("ABCDEF-secret", redact(text, ["123456:ABCDEF-secret"]))
        self.assertEqual(redact("hello", [""]), "hello")


class StateTests(unittest.TestCase):
    def test_defaults_are_not_shared_between_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = StateStore(Path(tmp) / "a.json")
            a.data["done_vod_ids"].append("1")
            b = StateStore(Path(tmp) / "b.json")
            self.assertEqual(b.data["done_vod_ids"], [])
            self.assertEqual(DEFAULT_STATE["done_vod_ids"], [])

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            a = StateStore(path)
            a.update(initialized=True, watermark="2026-01-01T00:00:00+00:00")
            b = StateStore(path)
            self.assertTrue(b.data["initialized"])
            self.assertEqual(b.data["watermark"], "2026-01-01T00:00:00+00:00")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg not installed")
class RealFfmpegTest(unittest.TestCase):
    def test_render_makes_vertical_video_with_same_duration(self):
        import os
        from unittest import mock
        from config import Settings
        from editor import make_vertical_video, probe_duration

        env = {
            "TELEGRAM_BOT_TOKEN": "dummy-token", "TELEGRAM_CHAT_ID": "@x",
            "TWITCH_CLIENT_ID": "dummy-id", "TWITCH_CLIENT_SECRET": "dummy-secret",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
            src, dst = Path(tmp) / "src.mp4", Path(tmp) / "out.mp4"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                 "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)],
                check=True,
            )
            make_vertical_video(src, dst, settings, video_kbps=600)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                 "stream=width,height", "-of", "csv=p=0", str(dst)],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            self.assertEqual(probe, "720,1280")
            self.assertGreaterEqual(probe_duration(dst), probe_duration(src) * 0.99)


if __name__ == "__main__":
    unittest.main()
