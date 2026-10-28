"""Pure helper functions (no network, no Telegram) so they can be unit-tested anywhere."""
from __future__ import annotations

import re

SIZE_SAFETY = 0.92  # aim ~8 % below the upload limit (container overhead + ABR drift)
MB = 1024 * 1024
GB = 1024 ** 3


class PermanentError(RuntimeError):
    """Retrying will not help (e.g. the stream is too long for the upload limit)."""


_DURATION_RE = re.compile(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


def parse_twitch_duration(value: str | None) -> int | None:
    """Convert Twitch durations such as '3h8m33s' or '45m10s' to seconds."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip().lower())
    if not match or not any(match.groups()):
        return None
    d, h, m, s = (int(x) if x else 0 for x in match.groups())
    return d * 86400 + h * 3600 + m * 60 + s


def bitrate_to_bps(value: str) -> int:
    """Convert ffmpeg bitrate strings such as 1800k, 2M or 96000 into bit/s."""
    raw = value.strip().lower()
    if raw.endswith("k"):
        return int(float(raw[:-1]) * 1000)
    if raw.endswith("m"):
        return int(float(raw[:-1]) * 1_000_000)
    return int(float(raw))


def fit_video_kbps(duration_s: float, limit_mb: float, audio_bitrate: str, safety: float = SIZE_SAFETY) -> int:
    """Highest video bitrate (kbit/s) that still lets `duration_s` fit under `limit_mb`."""
    if duration_s <= 0:
        raise ValueError("duration must be positive")
    total_bps = limit_mb * MB * 8 * safety / duration_s
    return int((total_bps - bitrate_to_bps(audio_bitrate)) / 1000)


def max_duration_hours(limit_mb: float, audio_bitrate: str, min_video_kbps: int, safety: float = SIZE_SAFETY) -> float:
    """Longest video (hours) that fits under `limit_mb` at the minimum allowed quality."""
    total_bps = bitrate_to_bps(audio_bitrate) + min_video_kbps * 1000
    return limit_mb * MB * 8 * safety / total_bps / 3600


def estimate_disk_needed_gb(
    duration_s: float,
    assumed_source_mbps: float,
    upload_limit_mb: float,
    local_server: bool,
    headroom_gb: float = 3.0,
) -> float:
    """Rough peak disk need: source VOD + rendered output (+ a second copy kept by a local Bot API server)."""
    source_gb = duration_s * assumed_source_mbps * 1_000_000 / 8 / GB
    output_gb = upload_limit_mb / 1024
    copies = 2 if local_server else 1
    return source_gb + output_gb * copies + headroom_gb


def redact(text: str, secrets: list[str] | tuple[str, ...]) -> str:
    """Replace every secret value in `text` with ***."""
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "***")
    return text
