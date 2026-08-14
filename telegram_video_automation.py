from __future__ import annotations
import os
import imageio_ffmpeg
os.environ["FFMPEG_BINARY"] = imageio_ffmpeg.get_ffmpeg_exe()

#!/usr/bin/env python3
"""Turn new Telegram channel summaries into Hindi narrated videos.

Telegram setup:
1. Create a bot with @BotFather.
2. Add it as an administrator of the channel with permission to read messages.
3. Keep TELEGRAM_BOT_TOKEN in the environment.

The script uses Telegram's Bot API long polling only. It does not use Telethon,
Pyrogram, a Telegram API ID, or a Telegram API hash.

Run:
    python telegram_video_automation.py

By default, videos are rendered locally but not uploaded to Instagram. Set
PUBLISH_TO_INSTAGRAM=true after testing the pipeline.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import edge_tts
import requests
from flask import Flask, jsonify
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from instagrapi import Client as InstagramClient


LOGGER = logging.getLogger("telegram-video-automation")
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
SCRIPT_OPENING = "Dosto, WEYOGIT ke aaj ke market update mein aapka swagat hai."
SCRIPT_CTA = (
    "Nifty 50 Option Scalping aur PAVITRA Model updates ke liye Telegram channel "
    "@weyogitforyou aur weyogit.com join karein."
)
TELEGRAM_LINK = "https://t.me/weyogitforyou"
WEBSITE_LINK = "https://weyogit.com"
DISCLAIMER = "Educational analysis only. Not financial advice."

app = Flask(__name__)
MONITOR_STATUS: dict[str, Any] = {
    "state": "starting",
    "started_at": datetime.now(timezone.utc).isoformat(),
    "last_error": None,
}
MONITOR_STATUS_LOCK = threading.Lock()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def find_youtube_client_secret_file() -> Path | None:
    raw_secret = os.getenv("YOUTUBE_CLIENT_SECRET_JSON")
    if raw_secret:
        p = Path("/tmp/client_secret.json")
        p.write_text(raw_secret, encoding="utf-8")
        return p

    env_path = os.getenv("YOUTUBE_CLIENT_SECRET_FILE")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    for candidate in [Path("client_secret.json"), Path("attached_assets/client_secret.json")]:
        if candidate.is_file():
            return candidate

    LOGGER.warning("No YouTube client secret found. Continuing without YouTube uploads.")
    return None


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    channel_username: str
    gemini_api_key: str
    gemini_model: str
    edge_tts_voice: str
    instagram_username: str | None
    instagram_password: str | None
    publish_to_instagram: bool
    poll_seconds: int
    request_timeout: int
    output_dir: Path
    state_file: Path
    instagram_session_file: Path
    youtube_client_secret_file: Path
    youtube_token_file: Path
    upload_to_youtube: bool
    youtube_privacy_status: str
    youtube_category_id: str
    youtube_playlist_id: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        username = os.getenv("INSTAGRAM_USERNAME", "").strip() or None
        password = os.getenv("INSTAGRAM_PASSWORD", "").strip() or None
        if bool(username) != bool(password):
            raise RuntimeError(
                "Set both INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD, or neither."
            )
        publish = env_bool("PUBLISH_TO_INSTAGRAM", False)
        if publish and not username:
            raise RuntimeError(
                "PUBLISH_TO_INSTAGRAM=true requires Instagram credentials."
            )
        upload_to_youtube = env_bool("YOUTUBE_UPLOAD_ENABLED", True)
        privacy_status = os.getenv("YOUTUBE_PRIVACY_STATUS", "private").strip().lower()
        if privacy_status not in {"private", "unlisted", "public"}:
            raise RuntimeError(
                "YOUTUBE_PRIVACY_STATUS must be private, unlisted, or public."
            )

        return cls(
            telegram_token=require_env("TELEGRAM_BOT_TOKEN"),
            channel_username=os.getenv(
                "TELEGRAM_CHANNEL_USERNAME", "@weyogitforyou"
            ).strip().lower(),
            gemini_api_key=require_env("GEMINI_API_KEY"),
            gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip(),
            edge_tts_voice=os.getenv(
                "EDGE_TTS_VOICE", "hi-IN-SwaraNeural"
            ).strip(),
            instagram_username=username,
            instagram_password=password,
            publish_to_instagram=publish,
            poll_seconds=max(1, int(os.getenv("POLL_SECONDS", "5"))),
            request_timeout=max(10, int(os.getenv("REQUEST_TIMEOUT", "60"))),
            output_dir=Path(os.getenv("OUTPUT_DIR", "output")),
            state_file=Path(os.getenv("STATE_FILE", "state/telegram_state.json")),
            instagram_session_file=Path(
                os.getenv("INSTAGRAM_SESSION_FILE", "session.json")
            ),
            youtube_client_secret_file=find_youtube_client_secret_file()
            if upload_to_youtube
            else Path(
                os.getenv(
                    "YOUTUBE_CLIENT_SECRET_FILE",
                    "attached_assets/client_secret.json",
                )
            ),
            youtube_token_file=Path(
                os.getenv("YOUTUBE_TOKEN_FILE", "state/youtube_token.json")
            ),
            upload_to_youtube=upload_to_youtube,
            youtube_privacy_status=privacy_status,
            youtube_category_id=os.getenv("YOUTUBE_CATEGORY_ID", "25").strip(),
            youtube_playlist_id=os.getenv("YOUTUBE_PLAYLIST_ID", "").strip() or None,
        )


class TelegramBotApi:
    def __init__(self, token: str, timeout: int) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout
        self.session = requests.Session()

    def call(self, method: str, **params: Any) -> Any:
        response = self.session.post(
            f"{self.base_url}/{method}", json=params, timeout=self.timeout
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(
                f"Telegram API {method} failed: "
                f"{payload.get('description', 'unknown error')}"
            )
        return payload.get("result")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            # Leave room for the HTTP client timeout after Telegram's long poll.
            "timeout": max(1, self.timeout - 10),
            "allowed_updates": ["channel_post"],
        }
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)


class UpdateCheckpoint:
    """Persist the next Bot API offset atomically across process restarts."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> int | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            value = payload.get("next_update_offset")
            return int(value) if value is not None else None
        except FileNotFoundError:
            return None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid Telegram state file {self.path}: {exc}") from exc

    def save(self, next_offset: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "next_update_offset": next_offset,
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def iter_summary_updates(
    updates: list[dict[str, Any]], channel_username: str
) -> Iterator[tuple[int, dict[str, Any], str]]:
    expected = channel_username.removeprefix("@").lower()
    for update in updates:
        message = update.get("channel_post")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat") or {}
        actual = str(chat.get("username", "")).lower()
        if actual != expected:
            LOGGER.warning(
                "Ignoring channel post from %s; expected @%s",
                actual or "<private channel>",
                expected,
            )
            continue
        text = (message.get("text") or message.get("caption") or "").strip()
        if text:
            yield int(update["update_id"]), message, text


def clean_text(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def fallback_script(raw_text: str) -> str:
    """Create usable narration without depending on Gemini."""
    summary = raw_text.strip() or "Aaj ke market mein important updates saamne aaye hain."
    return (
        f"{SCRIPT_OPENING}\n\n"
        "Aaj ke market summary par ek nazar daalte hain. "
        f"{summary}\n\n"
        "Market mein trade karne se pehle apna research aur risk management zaroor karein. "
        f"{SCRIPT_CTA}"
    )


def enforce_script_structure(script: str) -> str:
    """Guarantee the exact spoken opening and closing CTA in every script."""
    body = script.strip()
    if body.startswith(SCRIPT_OPENING):
        body = body[len(SCRIPT_OPENING) :].lstrip(" \n:-")
    if body.endswith(SCRIPT_CTA):
        body = body[: -len(SCRIPT_CTA)].rstrip()
    if not body:
        body = "Aaj ke market update par ek nazar daalte hain."
    return f"{SCRIPT_OPENING}\n\n{body}\n\n{SCRIPT_CTA}"


def generate_hindi_script(settings: Settings, summary: str) -> str:
    prompt = f"""You are a conversational Indian stock-market trader speaking to
your audience in natural spoken Hinglish. Write the middle narration for a
YouTube market-update video based only on the source summary below.

Use Roman-script Hinglish as people speak it in India, with a confident,
friendly trader voice. Explain the market movement clearly, preserve names,
numbers, dates, levels, option terms, and facts accurately, and do not invent
facts. Keep it conversational and suitable for voice narration, about 60-90
seconds. Do not use markdown, bullet points, labels, stage directions, or
quotation marks. Return only the middle narration body.

The application will add these exact lines itself, so do not write them:
Opening: "{SCRIPT_OPENING}"
Closing CTA: "{SCRIPT_CTA}"

Source summary:
{summary}
"""
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
        )
        generated = (response.text or "").strip()
        if not generated:
            raise RuntimeError("Gemini returned an empty script.")
        return enforce_script_structure(generated)
    except Exception as exc:
        LOGGER.warning(
            "Gemini script generation failed; using raw-text fallback: %s",
            exc,
        )
        return fallback_script(summary)


async def synthesize_speech(text: str, voice: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    communicator = edge_tts.Communicate(text=text, voice=voice)
    await communicator.save(str(output_path))


def run_command(command: list[str]) -> None:
    LOGGER.debug("Running: %s", " ".join(command))
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"Command failed with exit code {completed.returncode}: {details[-2000:]}"
        )


def audio_duration_seconds(audio_path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not inspect audio duration: {completed.stderr.strip()}")
    try:
        return max(1.0, float(completed.stdout.strip()))
    except ValueError as exc:
        raise RuntimeError("FFprobe returned an invalid audio duration.") from exc


def wrap_for_subtitle(text: str, words_per_line: int = 8) -> list[str]:
    words = text.split()
    lines = [
        " ".join(words[index : index + words_per_line])
        for index in range(0, len(words), words_per_line)
    ]
    return [
        "\n".join(lines[index : index + 2])
        for index in range(0, len(lines), 2)
    ] or [text]


def srt_timestamp(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds_int, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds_int:02},{milliseconds:03}"


def write_subtitles(script: str, duration: float, output_path: Path) -> None:
    chunks = wrap_for_subtitle(script)
    total_words = max(1, len(script.split()))
    current = 0.0
    entries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_words = max(1, len(chunk.replace("\n", " ").split()))
        chunk_duration = duration * chunk_words / total_words
        end = duration if index == len(chunks) else min(duration, current + chunk_duration)
        entries.append(
            f"{index}\n{srt_timestamp(current)} --> {srt_timestamp(end)}\n{chunk}\n"
        )
        current = end
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(entries), encoding="utf-8")


def ffmpeg_subtitle_path(path: Path) -> str:
    # The subtitles filter uses ':' as an option separator.
    return str(path.resolve()).replace("\\", "\\\\").replace(":", "\\:")


def render_video(
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
    width: int,
    height: int,
    duration: float,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_filter = (
        f"subtitles='{ffmpeg_subtitle_path(subtitle_path)}':"
        "force_style='FontName=Noto Sans Devanagari,FontSize=30,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H0010182B,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=90'"
    )
    disclaimer_filter = (
        "drawtext="
        f"text='{DISCLAIMER}':"
        "fontcolor=white:fontsize=24:"
        "x=(w-text_w)/2:y=h-text_h-24:"
        "box=1:boxcolor=black@0.55:boxborderw=8"
    )
    run_command(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x10182b:s={width}x{height}:r=30",
            "-i",
            str(audio_path),
            "-vf",
            f"{subtitle_filter},{disclaimer_filter}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            # Bound the output to the exact duration measured from the
            # generated speech audio. The color input is intentionally
            # infinite, so -t is required instead of relying on -shortest.
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )


class InstagramPublisher:
    def __init__(self, settings: Settings) -> None:
        if not settings.instagram_username or not settings.instagram_password:
            raise RuntimeError("Instagram credentials are not configured.")
        self.settings = settings
        self.client = InstagramClient()
        session = settings.instagram_session_file
        if session.exists():
            try:
                self.client.load_settings(session)
            except Exception as exc:
                LOGGER.warning("Could not load Instagram session; logging in again: %s", exc)
        self.client.login(
            settings.instagram_username,
            settings.instagram_password,
        )
        session.parent.mkdir(parents=True, exist_ok=True)
        self.client.dump_settings(session)

    def publish(self, video_path: Path, caption: str) -> None:
        self.client.video_upload(str(video_path), caption=caption)


class YouTubePublisher:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.credentials = self._load_credentials()
        self.youtube = build("youtube", "v3", credentials=self.credentials)

    def _load_credentials(self) -> Credentials:
        token_path = self.settings.youtube_token_file
        credentials: Credentials | None = None
        if token_path.exists():
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(token_path), YOUTUBE_SCOPES
                )
            except (ValueError, OSError) as exc:
                LOGGER.warning("Ignoring invalid YouTube OAuth token: %s", exc)

        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

        if not credentials or not credentials.valid:
            LOGGER.info(
                "YouTube authorization is required. A Google authorization URL "
                "will be printed for the first upload."
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.settings.youtube_client_secret_file),
                YOUTUBE_SCOPES,
            )
            # Google installed-app clients commonly register the loopback URI
            # as http://localhost. Set it explicitly because this headless flow
            # receives the redirected URL by copy/paste instead of binding a
            # local browser callback server.
            flow.redirect_uri = "http://localhost"
            authorization_url, _state = flow.authorization_url(
                access_type="offline",
                include_granted_scopes="true",
                prompt="consent",
            )
            print(
                "\nOpen this Google authorization URL in a browser:\n"
                f"{authorization_url}\n"
                "After approving access, paste the complete redirected URL here.\n"
                "The browser may show a connection error after redirect; that is okay.\n"
                "Redirect URL: ",
                end="",
                flush=True,
            )
            redirected_url = input().strip()
            parsed = urlparse(redirected_url)
            query = parse_qs(parsed.query)
            error = query.get("error", [None])[0]
            if error:
                raise RuntimeError(f"YouTube authorization was denied: {error}")
            code = query.get("code", [None])[0] or redirected_url
            if not code:
                raise RuntimeError("No authorization code was provided.")
            flow.fetch_token(code=code)
            credentials = flow.credentials

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    @staticmethod
    def _title(summary: str) -> str:
        cleaned = clean_text(summary)
        if not cleaned:
            return "Hindi Summary"
        return cleaned[:97].rstrip() + ("..." if len(cleaned) > 97 else "")

    def upload(self, video_path: Path, summary: str, script: str) -> str:
        body: dict[str, Any] = {
            "snippet": {
                "title": self._title(summary),
                "description": (
                    f"{script.strip()}\n\n"
                    f"{SCRIPT_CTA}\n\n"
                    f"Telegram channel: {TELEGRAM_LINK}\n"
                    f"Website: {WEBSITE_LINK}\n\n"
                    f"{DISCLAIMER}\n"
                    "#Hindi #News #Explainer"
                )[:5_000],
                "categoryId": self.settings.youtube_category_id,
                "tags": ["Hindi", "News", "Explainer"],
            },
            "status": {
                "privacyStatus": self.settings.youtube_privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        request = self.youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                chunksize=8 * 1024 * 1024,
                resumable=True,
            ),
        )
        response: dict[str, Any] | None = None
        while response is None:
            progress, response = request.next_chunk()
            if progress:
                LOGGER.info(
                    "YouTube upload progress: %.0f%%",
                    progress.progress() * 100,
                )
        video_id = str(response["id"])
        if self.settings.youtube_playlist_id:
            self.youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": self.settings.youtube_playlist_id,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            ).execute()
        return video_id


def caption_for_instagram(script: str) -> str:
    first_sentence = re.split(r"(?<=[।!?])\s+", script, maxsplit=1)[0]
    caption = (
        f"{first_sentence}\n\n"
        f"{SCRIPT_CTA}\n\n"
        f"Telegram: {TELEGRAM_LINK}\n"
        f"Website: {WEBSITE_LINK}\n\n"
        f"{DISCLAIMER}\n"
        "#Hindi #News #Explainer"
    )
    return caption[:2_200]


def cleanup_media_files(paths: dict[str, Path]) -> None:
    """Remove generated media only after every enabled upload succeeds."""
    for key in ("audio", "youtube", "instagram"):
        path = paths[key]
        try:
            path.unlink(missing_ok=True)
            LOGGER.info("Deleted temporary media file: %s", path)
        except OSError as exc:
            LOGGER.warning("Could not delete temporary media file %s: %s", path, exc)


def make_job_paths(settings: Settings, update_id: int) -> dict[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = settings.output_dir / f"{stamp}_{update_id}"
    return {
        "audio": root / "narration_hi.mp3",
        "subtitles": root / "subtitles.srt",
        "youtube": root / "youtube.mp4",
        "instagram": root / "instagram_reel.mp4",
        "script": root / "script_hi.txt",
        "source": root / "source_summary.txt",
    }


def process_summary(settings: Settings, update_id: int, summary: str) -> None:
    paths = make_job_paths(settings, update_id)
    raw_summary = summary.strip()
    source = clean_text(raw_summary)
    if not source:
        LOGGER.info("Skipping update %s because its summary is empty.", update_id)
        return
    paths["source"].parent.mkdir(parents=True, exist_ok=True)
    paths["source"].write_text(source, encoding="utf-8")

    LOGGER.info("Generating Hindi script for Telegram update %s.", update_id)
    script = generate_hindi_script(settings, raw_summary)
    paths["script"].write_text(script, encoding="utf-8")

    LOGGER.info("Generating Hindi speech.")
    asyncio.run(synthesize_speech(script, settings.edge_tts_voice, paths["audio"]))
    duration = audio_duration_seconds(paths["audio"])
    write_subtitles(script, duration, paths["subtitles"])

    LOGGER.info("Rendering YouTube video.")
    render_video(
        paths["audio"],
        paths["subtitles"],
        paths["youtube"],
        1920,
        1080,
        duration,
    )
    youtube_uploaded = not settings.upload_to_youtube
    instagram_uploaded = not settings.publish_to_instagram

    if settings.upload_to_youtube:
        LOGGER.info("Uploading YouTube video.")
        youtube_publisher = YouTubePublisher(settings)
        video_id = youtube_publisher.upload(paths["youtube"], source, script)
        LOGGER.info("YouTube upload complete: https://youtu.be/%s", video_id)
        youtube_uploaded = True
    else:
        LOGGER.info("YouTube uploading disabled.")
    LOGGER.info("Rendering Instagram Reel.")
    render_video(
        paths["audio"],
        paths["subtitles"],
        paths["instagram"],
        1080,
        1920,
        duration,
    )

    if settings.publish_to_instagram:
        LOGGER.info("Publishing Reel to Instagram.")
        publisher = InstagramPublisher(settings)
        publisher.publish(paths["instagram"], caption_for_instagram(script))
        instagram_uploaded = True
    else:
        LOGGER.info(
            "Instagram publishing disabled. Set PUBLISH_TO_INSTAGRAM=true to enable it."
        )
    if youtube_uploaded and instagram_uploaded and (
        settings.upload_to_youtube or settings.publish_to_instagram
    ):
        cleanup_media_files(paths)
    LOGGER.info("Completed update %s. Files are in %s.", update_id, paths["youtube"].parent)


def run_forever(settings: Settings) -> None:
    api = TelegramBotApi(settings.telegram_token, settings.request_timeout)
    checkpoint = UpdateCheckpoint(settings.state_file)
    offset = checkpoint.read()
    LOGGER.info(
        "Watching %s with Telegram Bot API polling. Instagram publishing: %s",
        settings.channel_username,
        "enabled" if settings.publish_to_instagram else "disabled",
    )
    while True:
        try:
            updates = api.get_updates(offset)
            for update_id, _message, summary in iter_summary_updates(
                updates, settings.channel_username
            ):
                try:
                    process_summary(settings, update_id, summary)
                except Exception:
                    LOGGER.exception("Failed to process Telegram update %s.", update_id)
                offset = update_id + 1
                checkpoint.save(offset)
        except KeyboardInterrupt:
            LOGGER.info("Stopping.")
            return
        except Exception:
            LOGGER.exception("Polling failed; retrying in %s seconds.", settings.poll_seconds)
            time.sleep(settings.poll_seconds)


def public_web_url(port: int) -> str:
    """Return the Replit-routed URL when available, otherwise localhost."""
    domains = os.getenv("REPLIT_DOMAINS", "").strip()
    if domains:
        domain = domains.split(",")[0].strip()
        if domain:
            return f"https://{domain}"
    dev_domain = os.getenv("REPLIT_DEV_DOMAIN", "").strip()
    if dev_domain:
        return f"https://{dev_domain}"
    return f"http://127.0.0.1:{port}"


def monitor_worker() -> None:
    try:
        settings = Settings.from_environment()
        with MONITOR_STATUS_LOCK:
            MONITOR_STATUS["state"] = "running"
        run_forever(settings)
    except Exception as exc:
        with MONITOR_STATUS_LOCK:
            MONITOR_STATUS["state"] = "failed"
            MONITOR_STATUS["last_error"] = str(exc)
        LOGGER.exception("Telegram monitor stopped unexpectedly.")


@app.get("/")
def root_status() -> Any:
    return jsonify(
        {
            "service": "telegram-video-automation",
            "status": "ok",
            "health": "/health",
        }
    )


@app.get("/health")
def health() -> Any:
    with MONITOR_STATUS_LOCK:
        status = dict(MONITOR_STATUS)
    http_status = 200 if status["state"] in {"starting", "running"} else 503
    return jsonify(status), http_status


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    port = int(os.getenv("PORT", "8080"))
    worker = threading.Thread(
        target=monitor_worker,
        name="telegram-monitor",
        daemon=True,
    )
    worker.start()
    url = public_web_url(port)
    LOGGER.info("Health server listening on http://0.0.0.0:%s", port)
    LOGGER.info("Public web URL: %s", url)
    LOGGER.info("Health endpoint: %s/health", url)
    try:
        app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        LOGGER.info("Stopping.")
    return 0


main()
