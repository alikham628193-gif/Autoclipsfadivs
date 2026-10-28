"""Render one complete VOD as a single vertically stacked MP4 using FFmpeg."""
from __future__ import annotations
import json
import shutil
import subprocess
from pathlib import Path
from config import Crop, Settings
from planner import bitrate_to_bps


def _crop_expr(crop: Crop) -> str:
    # Crop dimensions and offsets are rounded down to even pixels for yuv420p compatibility.
    w = crop.width / 100.0
    h = crop.height / 100.0
    x = crop.x / 100.0
    y = crop.y / 100.0
    return (
        f"crop=w='trunc(iw*{w:.6f}/2)*2':h='trunc(ih*{h:.6f}/2)*2':"
        f"x='trunc(iw*{x:.6f}/2)*2':y='trunc(ih*{y:.6f}/2)*2'"
    )


def probe_duration(path: Path) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found. Install FFmpeg (including ffprobe).")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)
    return float(data["format"]["duration"])


def make_vertical_video(source: Path, destination: Path, settings: Settings, video_kbps: int | None = None) -> None:
    """Stack the configured camera crop above gameplay/content, with no intentional time cuts.

    `video_kbps` lets the caller lower the video bitrate so a long stream still fits the upload limit.
    """
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError(f"Source video does not exist or is empty: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    top_height = settings.output_height // 2
    bottom_height = settings.output_height - top_height
    width = settings.output_width
    fps = settings.output_fps
    if video_kbps:
        video_bitrate = f"{int(video_kbps)}k"
        video_maxrate = f"{int(video_kbps * 1.3)}k"
    else:
        video_bitrate, video_maxrate = settings.video_bitrate, settings.video_maxrate
    top_crop = _crop_expr(settings.cam_crop)
    game_crop = _crop_expr(settings.game_crop)
    filters = (
        f"[0:v]split=2[top_in][game_in];"
        f"[top_in]{top_crop},scale={width}:{top_height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{top_height},setsar=1,fps={fps}[top];"
        f"[game_in]{game_crop},scale={width}:{bottom_height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{bottom_height},setsar=1,fps={fps}[game];"
        f"[top][game]vstack=inputs=2[outv]"
    )
    command = [
        "ffmpeg", "-hide_banner", "-y", "-i", str(source),
        "-filter_complex", filters,
        "-map", "[outv]", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", settings.video_preset,
        "-b:v", video_bitrate, "-maxrate", video_maxrate,
        "-bufsize", str(_bitrate_number(video_maxrate) * 2),
        "-pix_fmt", "yuv420p", "-r", str(fps),
        "-c:a", "aac", "-b:a", settings.audio_bitrate,
        "-af", "aresample=async=1:first_pts=0",
        "-movflags", "+faststart", "-max_muxing_queue_size", "2048",
        str(destination),
    ]
    proc = subprocess.run(command, check=False, timeout=None)
    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"FFmpeg failed with exit code {proc.returncode}; check the FFmpeg log above.")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("FFmpeg did not create the processed video.")
    source_duration = probe_duration(source)
    output_duration = probe_duration(destination)
    # Permit small container/timebase rounding differences, but do not accept a materially shortened video.
    if source_duration > 0 and output_duration < source_duration * 0.99:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"Output appears shorter than the source ({output_duration:.1f}s vs {source_duration:.1f}s). "
            "The file was rejected to avoid silently dropping part of the stream."
        )


def _bitrate_number(value: str) -> int:
    """Convert ffmpeg bitrate strings such as 1800k or 2M into a numeric bit/s value."""
    return bitrate_to_bps(value)
