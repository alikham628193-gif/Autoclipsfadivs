# FADIVS Twitch Full-Stream Bot

A Docker-based worker for the `fadivs` Twitch channel. After a live-to-offline transition, it waits for the matching archive, downloads the full VOD, renders one continuous vertical MP4 with configurable camera/content crops, and uploads it to `@autoclipsfadivs`.

**This is not a highlights generator.** It does not intentionally trim the timeline or split the output. It checks the exported duration against the source and rejects output that appears materially shorter.

## Important constraints

- Twitch Helix returns VOD metadata; `yt-dlp` is used for the actual download. Only process VODs you own or are authorized to download. Some private/subscriber-only archives may need an authorized session and may not be available.
- The worker must run continuously on a host with FFmpeg and plenty of disk space. GitHub stores the source code but does not run this worker by itself.
- Telegram's official cloud Bot API currently limits bot uploads to 50 MB. Large full VODs require a self-hosted Telegram Bot API server (documented up to 2000 MB) or another supported delivery method. The bot never splits the file automatically.
- Crop percentages must match your actual VOD layout; software cannot separate a webcam overlay from gameplay if they are not distinct source regions.

See [START_HERE_KK.md](START_HERE_KK.md) for phone-first upload instructions and the main [README](README.md) for the full setup.
