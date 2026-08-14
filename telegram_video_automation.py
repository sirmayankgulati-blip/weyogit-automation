from __future__ import annotations

import os
import sys
import time
import json
import re
import logging
import threading
import subprocess
from pathlib import Path
import requests
from flask import Flask, jsonify
import imageio_ffmpeg
from instagrapi import Client

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
LOGGER = logging.getLogger("telegram-video-automation")

# Ensure FFmpeg binary path is available
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
os.environ["FFMPEG_BINARY"] = FFMPEG_EXE

# Environment Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL = "@weyogitforyou"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "").strip()
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "").strip()
PUBLISH_TO_INSTAGRAM = os.getenv("PUBLISH_TO_INSTAGRAM", "true").lower() == "true"
PORT = int(os.getenv("PORT", 10000))

STATE_FILE = Path("state/last_offset.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Flask Health Server for UptimeRobot
app = Flask(__name__)

@app.route("/")
@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "service": "telegram-video-automation"}), 200

# YouTube Secret Resolver
def get_youtube_secret_path() -> Path | None:
    raw_secret = os.getenv("YOUTUBE_CLIENT_SECRET_JSON")
    if raw_secret and raw_secret.strip():
        tmp_p = Path("/tmp/client_secret.json")
        try:
            tmp_p.write_text(raw_secret.strip(), encoding="utf-8")
            return tmp_p
        except Exception:
            pass

    for candidate in [Path("client_secret.json"), Path("attached_assets/client_secret.json")]:
        if candidate.is_file():
            return candidate

    return None

# Dynamic Gemini Model Cascade
def generate_hindi_script(telegram_text: str) -> str:
    candidate_models = [
        "gemini-1.5-flash-002",
        "gemini-1.5-flash-001",
        "gemini-1.5-pro-002",
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-2.5-flash"
    ]
    prompt = (
        f"You are the chief financial analyst for WEYOGIT. Convert this Telegram market update into a natural, "
        f"conversational Hindi YouTube script in spoken Hinglish as used by Indian traders. "
        f"Focus on Nifty 50 and PAVITRA Model insights. Avoid bullet lists:\n\n{telegram_text}"
    )

    if GEMINI_API_KEY:
        headers = {"Content-Type": "application/json"}
        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        for model in candidate_models:
            try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
                res = requests.post(url, json=payload, headers=headers, timeout=15)
                if res.status_code == 200:
                    raw_text = res.json()["candidates"][0]["content"]["parts"][0]["text"]
                    LOGGER.info("Generated script using model: %s", model)
                    return re.sub(r"(\.|।)", r"\1 <break time='300ms'/>", raw_text)
            except Exception as e:
                LOGGER.warning("Model %s invocation error: %s", model, e)

    LOGGER.info("Using deterministic fallback script.")
    return (
        f"Dosto, WEYOGIT Market Update mein aapka swagat hai. "
        f"{telegram_text} "
        f"Live Nifty 50 aur PAVITRA Model scalping updates ke liye Telegram channel @weyogitforyou aur weyogit.com visit karein."
    )

# Audio Synthesis
def generate_speech(script_text: str, output_path: Path) -> Path:
    clean_text = re.sub(r"<[^>]+>", "", script_text)[:1500]
    cmd = f'edge-tts --text "{clean_text}" --voice hi-IN-SwaraNeural --write-media {output_path}'
    subprocess.run(cmd, shell=True, check=True)
    return output_path

# Audio Duration Measurement
def get_audio_duration(audio_path: Path) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
    return float(res.stdout.strip())

# Clean Low-Memory Video Rendering (Zero Filter Dependencies)
def render_low_memory_video(audio_path: Path, output_path: Path, is_reel: bool = False) -> Path:
    duration = get_audio_duration(audio_path)
    dimensions = "1080x1920" if is_reel else "1920x1080"
    
    cmd = [
        FFMPEG_EXE, "-y",
        "-f", "lavfi", "-i", f"color=c=0x111625:s={dimensions}:r=25",
        "-i", str(audio_path),
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-threads", "1",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        str(output_path)
    ]
    subprocess.run(cmd, check=True)
    return output_path

# Instagram Auto-Post
def post_to_instagram(video_path: Path, caption: str):
    if not (PUBLISH_TO_INSTAGRAM and INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD):
        LOGGER.info("Instagram posting skipped (credentials or flag not set).")
        return
    try:
        cl = Client()
        session_file = Path("state/instagram_session.json")
        if session_file.exists():
            cl.load_settings(session_file)
        cl.login(INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD)
        cl.dump_settings(session_file)
        media = cl.clip_upload(str(video_path), caption=caption)
        LOGGER.info("Reel posted to Instagram successfully: %s", media.pk)
    except Exception as e:
        LOGGER.error("Instagram auto-post error: %s", e)

# Telegram Background Polling Loop
def telegram_worker():
    LOGGER.info("Telegram background poller started for %s", TELEGRAM_CHANNEL)
    last_offset = 0
    if STATE_FILE.exists():
        try:
            last_offset = json.loads(STATE_FILE.read_text()).get("offset", 0)
        except Exception:
            pass

    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(10)
                continue

            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 10}
            if last_offset:
                params["offset"] = last_offset + 1

            res = requests.get(url, params=params, timeout=15).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    update_id = update["update_id"]
                    last_offset = max(last_offset, update_id)
                    STATE_FILE.write_text(json.dumps({"offset": last_offset}))

                    post = update.get("channel_post") or update.get("message")
                    if not post:
                        continue

                    text = post.get("text") or post.get("caption") or ""
                    if not text.strip():
                        continue

                    LOGGER.info("Processing Telegram post ID %s: %s", post.get("message_id"), text[:60])
                    
                    # 1. Script
                    script = generate_hindi_script(text)
                    
                    # 2. Audio
                    audio_file = OUTPUT_DIR / f"audio_{update_id}.mp3"
                    generate_speech(script, audio_file)
                    
                    # 3. Video Rendering
                    yt_video = OUTPUT_DIR / f"yt_{update_id}.mp4"
                    render_low_memory_video(audio_file, yt_video, is_reel=False)

                    reel_video = OUTPUT_DIR / f"reel_{update_id}.mp4"
                    render_low_memory_video(audio_file, reel_video, is_reel=True)

                    # 4. Instagram Posting with Disclaimer
                    caption = (
                        f"WEYOGIT Alert\n\n{text[:200]}\n\n"
                        f"Educational analysis of Nifty 50 / PAVITRA Model. Not financial advice.\n"
                        f"#WEYOGIT #Nifty50 #PAVITRAModel"
                    )
                    post_to_instagram(reel_video, caption)

                    # 5. Cleanup temp media
                    for p in [audio_file, yt_video, reel_video]:
                        if p.exists():
                            p.unlink()

                    LOGGER.info("Completed processing update %s", update_id)

        except Exception as e:
            LOGGER.error("Telegram worker loop error: %s", e)

        time.sleep(5)

# Launch Background Thread on module load
poller_thread = threading.Thread(target=telegram_worker, daemon=True)
poller_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
