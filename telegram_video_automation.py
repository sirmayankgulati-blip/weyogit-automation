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

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import edge_tts
import requests
from google import genai
from instagrapi import Client as InstagramClient


LOGGER = logging.getLogger("telegram-video-automation")


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
                os.getenv("INSTAGRAM_SESSION_FILE", "state/instagram_session.json")
            ),
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


def generate_hindi_script(settings: Settings, summary: str) -> str:
    client = genai.Client(api_key=settings.gemini_api_key)
    prompt = f"""You write narration for a Hindi YouTube news/explainer video.

Convert the source summary below into a natural, engaging spoken Hindi script.
Use Devanagari Hindi, not transliterated Hindi. Preserve names, numbers, dates,
and facts accurately. Do not invent facts. Explain technical or English terms
briefly in Hindi when useful. Start directly with the narration, without labels,
stage directions, markdown, bullet points, or quotation marks. Aim for about
60-90 seconds of speech.

Source summary:
{summary}
"""
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
    )
    script = (response.text or "").strip()
    if not script:
        raise RuntimeError("Gemini returned an empty Hindi script.")
    return script


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
    audio_path: Path, subtitle_path: Path, output_path: Path, width: int, height: int
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_filter = (
        f"subtitles='{ffmpeg_subtitle_path(subtitle_path)}':"
        "force_style='FontName=Noto Sans Devanagari,FontSize=30,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H0010182B,"
        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=90'"
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
            subtitle_filter,
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
            "-shortest",
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


def caption_for_instagram(script: str) -> str:
    first_sentence = re.split(r"(?<=[।!?])\s+", script, maxsplit=1)[0]
    caption = f"{first_sentence}\n\n#Hindi #News #Explainer"
    return caption[:2_200]


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
    source = clean_text(summary)
    if not source:
        LOGGER.info("Skipping update %s because its summary is empty.", update_id)
        return
    paths["source"].parent.mkdir(parents=True, exist_ok=True)
    paths["source"].write_text(source, encoding="utf-8")

    LOGGER.info("Generating Hindi script for Telegram update %s.", update_id)
    script = generate_hindi_script(settings, source)
    paths["script"].write_text(script, encoding="utf-8")

    LOGGER.info("Generating Hindi speech.")
    asyncio.run(synthesize_speech(script, settings.edge_tts_voice, paths["audio"]))
    duration = audio_duration_seconds(paths["audio"])
    write_subtitles(script, duration, paths["subtitles"])

    LOGGER.info("Rendering YouTube video.")
    render_video(paths["audio"], paths["subtitles"], paths["youtube"], 1920, 1080)
    LOGGER.info("Rendering Instagram Reel.")
    render_video(paths["audio"], paths["subtitles"], paths["instagram"], 1080, 1920)

    if settings.publish_to_instagram:
        LOGGER.info("Publishing Reel to Instagram.")
        publisher = InstagramPublisher(settings)
        publisher.publish(paths["instagram"], caption_for_instagram(script))
    else:
        LOGGER.info(
            "Instagram publishing disabled. Set PUBLISH_TO_INSTAGRAM=true to enable it."
        )
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


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        run_forever(Settings.from_environment())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())