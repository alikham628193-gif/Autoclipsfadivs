"""Twitch Helix API client and full VOD download helper."""
from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def parse_twitch_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result.astimezone(timezone.utc)
    except ValueError:
        return None


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, channel: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.channel = channel.lstrip("@")
        self.token: str | None = None
        self.token_expires_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"Client-ID": self.client_id})
        self.user_id: str | None = None

    def _authenticate(self) -> None:
        response = self.session.post(
            TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "client_credentials"},
            timeout=(10, 30),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Twitch token request failed ({response.status_code}): {response.text[:300]}")
        body = response.json()
        self.token = body["access_token"]
        self.token_expires_at = time.time() + int(body.get("expires_in", 3600)) - 60

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.token or time.time() >= self.token_expires_at:
            self._authenticate()
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{HELIX}/{endpoint.lstrip('/')}"
        for attempt in range(4):
            response = self.session.get(url, params=params, headers=headers, timeout=(10, 45))
            if response.status_code == 401:
                self.token = None
                self._authenticate()
                headers["Authorization"] = f"Bearer {self.token}"
                response = self.session.get(url, params=params, headers=headers, timeout=(10, 45))
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(min(2 ** attempt, 8))
                    continue
            if response.status_code >= 400:
                raise RuntimeError(f"Twitch API {endpoint} failed ({response.status_code}): {response.text[:300]}")
            return response.json()
        raise RuntimeError(f"Twitch API {endpoint} did not respond after retries")

    def get_user_id(self) -> str:
        if self.user_id:
            return self.user_id
        users = self._get("users", {"login": self.channel}).get("data", [])
        if not users:
            raise RuntimeError(f"Twitch channel not found: {self.channel}")
        self.user_id = str(users[0]["id"])
        return self.user_id

    def current_stream(self) -> dict[str, Any] | None:
        items = self._get("streams", {"user_id": self.get_user_id(), "first": 1}).get("data", [])
        if not items:
            return None
        stream = items[0]
        return {
            "id": str(stream["id"]),
            "started_at": stream.get("started_at"),
            "title": stream.get("title", "FADIVS stream"),
        }

    def latest_vod_started_after(self, started_after: datetime) -> dict[str, Any] | None:
        user_id = self.get_user_id()
        videos = self._get("videos", {"user_id": user_id, "type": "archive", "first": 20}).get("data", [])
        threshold = started_after - timedelta(minutes=5)
        candidates = []
        for item in videos:
            created_at = parse_twitch_time(item.get("created_at"))
            if created_at and created_at >= threshold:
                candidates.append(item)
        if not candidates:
            return None
        candidates.sort(key=lambda item: parse_twitch_time(item.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        item = candidates[0]
        duration = item.get("duration", "")
        return {
            "id": str(item["id"]),
            "title": item.get("title") or "FADIVS full stream",
            "url": item.get("url") or f"https://www.twitch.tv/videos/{item['id']}",
            "created_at": item.get("created_at"),
            "duration": duration,
        }

    @staticmethod
    def download_vod(url: str, destination: Path, cookies_file: str = "") -> Path:
        """Download the complete VOD and remux it to one MP4 file."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        for old in destination.parent.glob(destination.stem + ".*" ):
            if old.is_file() and (old.suffix.lower() in {".mp4", ".mkv", ".webm", ".part"}):
                old.unlink(missing_ok=True)
        output_template = str(destination.with_suffix("")) + ".%(ext)s"
        cmd = [
            "yt-dlp", "--no-warnings", "--newline", "--retries", "10",
            "--fragment-retries", "10", "--retry-sleep", "3",
            "--merge-output-format", "mp4", "--remux-video", "mp4",
            "-o", output_template, url,
        ]
        if cookies_file:
            cmd[1:1] = ["--cookies", cookies_file]
        result = subprocess.run(cmd, check=False, timeout=None)
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp failed with exit code {result.returncode}; check that the VOD is available and you are authorized to download it.")
        candidates = [p for p in destination.parent.glob(destination.stem + ".*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"} and p.stat().st_size > 0]
        if not candidates:
            raise RuntimeError("yt-dlp completed but no video file was found. Check the download log above.")
        mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
        chosen = max(mp4s or candidates, key=lambda p: p.stat().st_size)
        if chosen != destination:
            chosen.replace(destination)
        if not destination.exists() or destination.stat().st_size == 0:
            raise RuntimeError("Downloaded VOD file is missing or empty.")
        return destination
