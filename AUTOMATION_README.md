# Telegram to Hindi video automation

`telegram_video_automation.py` watches `@weyogitforyou` using the Telegram Bot
API, sends each new text/caption summary to Gemini, creates Hindi narration
with `edge-tts`, renders both a landscape YouTube MP4 and a vertical Instagram
Reel MP4 with FFmpeg, and can publish the Reel with `instagrapi`.

## Telegram setup

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Add the bot to `@weyogitforyou` as an administrator.
3. Keep the bot's permission to read channel posts enabled.
4. Store the token as `TELEGRAM_BOT_TOKEN`.

This script uses only the Bot API `getUpdates` long-polling method. It does not
use Telethon, Pyrogram, `TELEGRAM_API_ID`, or `TELEGRAM_API_HASH`.
If the bot was previously configured with a webhook, remove it before starting
polling; Telegram does not deliver updates to polling while a webhook is set.
For example:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook?drop_pending_updates=false"
```

## Run

Install Python packages:

```bash
python3 -m pip install -r requirements.txt
python3 telegram_video_automation.py
```

The project environment already needs FFmpeg available on `PATH`.

The script saves each job under `output/<timestamp>_<telegram-update-id>/`:

- `youtube.mp4` — 1920x1080 narrated video
- `instagram_reel.mp4` — 1080x1920 vertical Reel
- `script_hi.txt` — generated Hindi narration
- `source_summary.txt` — cleaned Telegram source
- `subtitles.srt` — generated Hindi subtitles

## Instagram publishing

Publishing is intentionally off by default:

```bash
PUBLISH_TO_INSTAGRAM=true python3 telegram_video_automation.py
```

The Instagram session is cached in `state/instagram_session.json` so the
account does not need to be logged in from scratch on every processed post.
Use an Instagram account that is permitted to publish Reels, and review
Instagram's current automation and content policies before enabling uploads.

## Restart behavior

The next Telegram update offset is stored atomically in
`state/telegram_state.json`. This prevents already-processed channel updates
from being reprocessed after a restart. Output videos are not deleted.