"""Validated configuration for the FADIVS full-stream processor."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}") from exc


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Crop:
    x: float
    y: float
    width: float
    height: float

    def validate(self, label: str) -> None:
        for field_name, value in (("x", self.x), ("y", self.y), ("width", self.width), ("height", self.height)):
            if not 0 <= value <= 100:
                raise ValueError(f"{label}_{field_name.upper()} must be between 0 and 100")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"{label} crop width and height must be greater than 0")
        if self.x + self.width > 100.0001 or self.y + self.height > 100.0001:
            raise ValueError(f"{label} crop must fit inside the original frame (x+width and y+height cannot exceed 100)")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_api_base_url: str
    telegram_cloud_max_upload_mb: int
    telegram_local_max_upload_mb: int
    twitch_client_id: str
    twitch_client_secret: str
    twitch_channel: str
    poll_seconds: int
    workdir: Path
    keep_files: bool
    log_level: str
    yt_dlp_cookies_file: str
    output_width: int
    output_height: int
    output_fps: int
    video_preset: str
    video_bitrate: str
    video_maxrate: str
    audio_bitrate: str
    cam_crop: Crop
    game_crop: Crop
    vod_wait_seconds: int
    min_free_disk_gb: float

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID", "@autoclipsfadivs"),
            telegram_api_base_url=_str("TELEGRAM_API_BASE_URL").rstrip("/"),
            telegram_cloud_max_upload_mb=_int("TELEGRAM_CLOUD_MAX_UPLOAD_MB", 49),
            telegram_local_max_upload_mb=_int("TELEGRAM_LOCAL_MAX_UPLOAD_MB", 1850),
            twitch_client_id=_str("TWITCH_CLIENT_ID"),
            twitch_client_secret=_str("TWITCH_CLIENT_SECRET"),
            twitch_channel=_str("TWITCH_CHANNEL", "fadivs").lstrip("@"),
            poll_seconds=_int("POLL_SECONDS", 120),
            workdir=Path(_str("WORKDIR", "/data")).expanduser(),
            keep_files=_str("KEEP_FILES", "false").lower() in {"1", "true", "yes", "on"},
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            yt_dlp_cookies_file=_str("YTDLP_COOKIES_FILE"),
            output_width=_int("OUTPUT_WIDTH", 720),
            output_height=_int("OUTPUT_HEIGHT", 1280),
            output_fps=_int("OUTPUT_FPS", 30),
            video_preset=_str("VIDEO_PRESET", "veryfast"),
            video_bitrate=_str("VIDEO_BITRATE", "1400k"),
            video_maxrate=_str("VIDEO_MAXRATE", "1800k"),
            audio_bitrate=_str("AUDIO_BITRATE", "96k"),
            cam_crop=Crop(_float("CAM_X", 0), _float("CAM_Y", 0), _float("CAM_W", 100), _float("CAM_H", 50)),
            game_crop=Crop(_float("GAME_X", 0), _float("GAME_Y", 50), _float("GAME_W", 100), _float("GAME_H", 50)),
            vod_wait_seconds=_int("VOD_WAIT_SECONDS", 180),
            min_free_disk_gb=_float("MIN_FREE_DISK_GB", 12.0),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        missing = []
        for key, value in (
            ("TELEGRAM_BOT_TOKEN", self.telegram_bot_token),
            ("TELEGRAM_CHAT_ID", self.telegram_chat_id),
            ("TWITCH_CLIENT_ID", self.twitch_client_id),
            ("TWITCH_CLIENT_SECRET", self.twitch_client_secret),
        ):
            if not value or value.lower().startswith("put_") or value.lower().startswith("your_"):
                missing.append(key)
        if missing:
            raise ValueError("Missing or placeholder environment variable(s): " + ", ".join(missing) + ". Copy .env.example to .env and fill them in locally.")
        if not self.twitch_channel:
            raise ValueError("TWITCH_CHANNEL cannot be empty")
        if self.poll_seconds < 30:
            raise ValueError("POLL_SECONDS must be at least 30 seconds")
        if self.output_width < 320 or self.output_height < 320 or self.output_width % 2 or self.output_height % 2:
            raise ValueError("OUTPUT_WIDTH and OUTPUT_HEIGHT must be even integers and at least 320")
        if self.output_fps < 1 or self.output_fps > 60:
            raise ValueError("OUTPUT_FPS must be between 1 and 60")
        if self.telegram_cloud_max_upload_mb < 1 or self.telegram_local_max_upload_mb < 1:
            raise ValueError("Telegram upload limits must be positive numbers")
        if self.min_free_disk_gb < 1:
            raise ValueError("MIN_FREE_DISK_GB must be at least 1 GB")
        self.cam_crop.validate("CAM")
        self.game_crop.validate("GAME")
        if self.yt_dlp_cookies_file and not Path(self.yt_dlp_cookies_file).is_file():
            raise ValueError("YTDLP_COOKIES_FILE is set but the file does not exist")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
