# Telegram to Hindi video automation

`telegram_video_automation.py` watches `@weyogitforyou` using the Telegram Bot
API, sends each new text/caption summary to Gemini, creates Hindi narration
with `edge-tts`, renders both a landscape YouTube MP4 and a vertical Instagram
Reel MP4 with FFmpeg, uploads the YouTube video, and can publish the Reel with
`instagrapi`.

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

The script runs a lightweight Flask health server on port `8080` while the
Telegram monitor runs in a background thread. On startup it prints the public
Replit URL and the health endpoint:

```text
Public web URL: https://<your-replit-domain>
Health endpoint: https://<your-replit-domain>/health
```

`/health` returns the monitor state as JSON and returns HTTP 503 if the monitor
has failed. The project environment also needs FFmpeg available on `PATH`.

The script saves each job under `output/<timestamp>_<telegram-update-id>/`:

- `youtube.mp4` — 1920x1080 narrated video
- `instagram_reel.mp4` — 1080x1920 vertical Reel
- `script_hi.txt` — generated Hindi narration
- `source_summary.txt` — cleaned Telegram source
- `subtitles.srt` — generated Hindi subtitles

After every enabled upload succeeds, the generated `.mp3` and `.mp4` media
files are deleted immediately to preserve disk space. If any enabled upload
fails, the media files are retained for retry or inspection.

## YouTube uploads

YouTube upload is enabled by default. The script automatically uses the one
Google OAuth client-secret JSON file found in `attached_assets/`; the uploaded
file can keep its generated filename. To select a different file explicitly:

```bash
YOUTUBE_CLIENT_SECRET_FILE=attached_assets/client_secret.json \
python3 telegram_video_automation.py
```

On the first upload, the script prints a Google authorization URL. Open it in a
browser, approve access to your YouTube account, then paste the complete
redirected URL back into the running process. The browser may show a connection
error after redirect; the URL in its address bar is still valid. The refresh
token is cached in `state/youtube_token.json`; that runtime file is ignored by
version control. Later uploads reuse the cached authorization automatically.

Uploads are private by default. Configure the behavior with:

```bash
YOUTUBE_PRIVACY_STATUS=private   # private, unlisted, or public
YOUTUBE_CATEGORY_ID=25
YOUTUBE_PLAYLIST_ID=             # optional playlist ID
```

Set `YOUTUBE_UPLOAD_ENABLED=false` to render `youtube.mp4` without uploading.

## Instagram publishing

Publishing is intentionally off by default:

```bash
PUBLISH_TO_INSTAGRAM=true python3 telegram_video_automation.py
```

The Instagram session is cached in local `session.json` (or the path specified
by `INSTAGRAM_SESSION_FILE`) so the account does not need to repeat a full
login on every processed post. The session file is ignored by version control.
Use an Instagram account that is permitted to publish Reels, and review
Instagram's current automation and content policies before enabling uploads.

Instagram captions and YouTube descriptions include:

- Telegram: https://t.me/weyogitforyou
- Website: https://weyogit.com
- Educational analysis only. Not financial advice.

## Restart behavior

The next Telegram update offset is stored atomically in
`state/telegram_state.json`. This prevents already-processed channel updates
from being reprocessed after a restart. Output videos are not deleted.