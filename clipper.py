"""Record a live Twitch stream and clip bursts of chat activity."""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import queue
import re
import select
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from highlights import HighlightDetector
from reels import render_reel


CHAT_HOST = "irc.chat.twitch.tv"
CHAT_PORT = 6697
CHANNEL_RE = re.compile(r"^[a-zA-Z0-9_]{3,25}$")


@dataclass(frozen=True)
class Settings:
    channel: str
    url: str
    platform: str
    output: Path
    quality: str
    bucket_seconds: int
    min_messages: int
    spike_factor: float
    cooldown_seconds: int
    before_seconds: int
    after_seconds: int
    poll_seconds: int
    reel_caption: str
    gta_only: bool
    facecam: tuple[float, float, float, float]
    layout: str
    chat_delay_seconds: int


@dataclass(frozen=True)
class ClipJob:
    start: int
    end: int
    signal: str


def stream_info(url: str) -> dict | None:
    command = [sys.executable, "-m", "streamlink", "--json", url]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=35, check=False)
    except subprocess.TimeoutExpired:
        print("Live check timed out; retrying.", flush=True)
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if data.get("streams") else None


def is_gta_stream(info: dict) -> bool:
    metadata = info.get("metadata") or {}
    text = f"{metadata.get('category') or ''} {metadata.get('title') or ''}".casefold()
    return "gta" in text or "grand theft auto" in text


def start_recording(settings: Settings, recording: Path) -> tuple[subprocess.Popen[str], object]:
    command = [
        sys.executable, "-m", "streamlink", "--progress", "no", "--ffmpeg-fout",
        "mpegts", "--output",
        str(recording), settings.url, settings.quality,
    ]
    log = recording.with_suffix(".streamlink.log").open("w", encoding="utf-8")
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, text=True)
    return process, log


def connect_chat(channel: str) -> socket.socket:
    token = os.getenv("TWITCH_OAUTH_TOKEN", "").strip()
    nickname = os.getenv("TWITCH_NICK", "").strip()
    if token and not nickname:
        raise ValueError("Set TWITCH_NICK alongside TWITCH_OAUTH_TOKEN.")
    if not token:
        nickname = f"justinfan{os.getpid() % 100000:05d}{int(time.time()) % 10000:04d}"
    raw = socket.create_connection((CHAT_HOST, CHAT_PORT), timeout=15)
    connection = ssl.create_default_context().wrap_socket(raw, server_hostname=CHAT_HOST)
    if token:
        connection.sendall(f"PASS oauth:{token.removeprefix('oauth:')}\r\n".encode())
    connection.sendall(f"NICK {nickname}\r\nJOIN #{channel}\r\n".encode())
    connection.setblocking(False)
    return connection


def clip_worker(settings: Settings, recording: Path, started: float,
                jobs: queue.Queue[ClipJob | None]) -> None:
    clips_dir = recording.parent / "clips"
    reels_dir = recording.parent / "reels"
    clips_dir.mkdir(exist_ok=True)
    reels_dir.mkdir(exist_ok=True)
    while True:
        job = jobs.get()
        if job is None:
            jobs.task_done()
            return
        start, end = job.start, job.end
        try:
            # Wait for the full post-roll to have reached the recording file.
            while time.monotonic() - started < end + 4:
                time.sleep(1)
            name = f"{recording.stem}_at_{start:06d}"
            clip = clips_dir / f"{name}.mp4"
            # Seek a few seconds early, then trim after demuxing so audio and video
            # share the exact cut timestamp even when live TS segments start apart.
            preseek = max(0, start - 5)
            fine_seek = start - preseek
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss",
                       str(preseek), "-i", str(recording), "-ss", str(fine_seek),
                       "-t", str(end - start),
                       "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                       "-preset", "medium", "-crf", "18", "-c:a", "aac",
                       "-b:a", "192k",
                       "-movflags", "+faststart", str(clip)]
            subprocess.run(command, check=True)
            print(f"Saved {clip}", flush=True)
            if job.signal == "audio":
                review_dir = recording.parent / "review"
                review_dir.mkdir(exist_ok=True)
                clip.replace(review_dir / clip.name)
                print(f"Audio candidate at {start}s needs review before a reel is made.", flush=True)
                continue
            reel = reels_dir / f"{name}_reel.mp4"
            render_reel(clip, reel, settings.facecam, settings.layout,
                        caption=settings.reel_caption)
            print(f"Saved {reel}", flush=True)
        except Exception as exc:
            print(f"Clip failed at {start}s: {exc}", file=sys.stderr, flush=True)
        finally:
            jobs.task_done()


def monitor_chat(settings: Settings, started: float, recording_process: subprocess.Popen[str],
                 jobs: queue.Queue[ClipJob | None], recording: Path) -> bool:
    detector = HighlightDetector(settings.bucket_seconds, settings.min_messages,
                                 settings.spike_factor, settings.cooldown_seconds)
    current_bucket = 0
    messages_seen = 0
    events_file = recording.with_suffix(".highlights.jsonl")
    while recording_process.poll() is None:
        connection = None
        try:
            connection = connect_chat(settings.channel)
            print("Chat connected.", flush=True)
            buffer = b""
            while recording_process.poll() is None:
                elapsed = max(0, int(time.monotonic() - started))
                if elapsed >= 40 and messages_seen == 0:
                    print("No chat messages received; switching to audio detection.", flush=True)
                    return False
                bucket = elapsed // settings.bucket_seconds
                while current_bucket < bucket:
                    highlight = detector.close_bucket(current_bucket)
                    if highlight:
                        clip_start = max(0, highlight.second - settings.before_seconds)
                        clip_end = highlight.second + settings.after_seconds
                        jobs.put(ClipJob(clip_start, clip_end, "chat"))
                        with events_file.open("a", encoding="utf-8") as log:
                            log.write(json.dumps(asdict(highlight), ensure_ascii=False) + "\n")
                        print(f"Highlight: {highlight.count} messages from {highlight.unique_chatters} people; "
                              f"{highlight.reason} at {highlight.second}s.", flush=True)
                    current_bucket += 1
                ready, _, _ = select.select([connection], [], [], 1)
                if not ready:
                    continue
                data = connection.recv(65536)
                if not data:
                    raise ConnectionError("Chat disconnected")
                buffer += data
                while b"\n" in buffer:
                    raw_line, buffer = buffer.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if line.startswith("PING"):
                        connection.sendall((line.replace("PING", "PONG", 1) + "\r\n").encode())
                    if "Login authentication failed" in line or "Improperly formatted auth" in line:
                        raise ConnectionError("Twitch rejected chat login. Use TWITCH_NICK and TWITCH_OAUTH_TOKEN with chat:read.")
                    if " PRIVMSG #" in line:
                        if detector.add_line(line):
                            messages_seen += 1
        except (OSError, ConnectionError) as exc:
            print(f"Chat connection: {exc}; reconnecting in 5s.", file=sys.stderr, flush=True)
            time.sleep(5)
        finally:
            if connection is not None:
                connection.close()
    return True


def monitor_youtube_chat(settings: Settings, video_id: str, started: float,
                         recording_process: subprocess.Popen[str],
                         jobs: queue.Queue[ClipJob | None], recording: Path) -> bool:
    """Use live YouTube chat bursts when the video exposes a public chat."""
    try:
        import pytchat
        chat = pytchat.create(video_id=video_id)
    except Exception as exc:
        print(f"YouTube chat unavailable: {exc}", file=sys.stderr, flush=True)
        return False
    detector = HighlightDetector(settings.bucket_seconds, settings.min_messages,
                                 settings.spike_factor, settings.cooldown_seconds)
    current_bucket = 0
    messages_seen = 0
    events_file = recording.with_suffix(".highlights.jsonl")
    print("YouTube live chat connected.", flush=True)
    try:
        while recording_process.poll() is None:
            elapsed = max(0, int(time.monotonic() - started))
            bucket = elapsed // settings.bucket_seconds
            while current_bucket < bucket:
                highlight = detector.close_bucket(current_bucket)
                if highlight:
                    clip_start = max(0, highlight.second - settings.before_seconds
                                     + settings.chat_delay_seconds)
                    clip_end = highlight.second + settings.after_seconds + settings.chat_delay_seconds
                    jobs.put(ClipJob(clip_start, clip_end, "chat"))
                    with events_file.open("a", encoding="utf-8") as log:
                        log.write(json.dumps(asdict(highlight), ensure_ascii=False) + "\n")
                    print(f"YouTube highlight: {highlight.count} messages from "
                          f"{highlight.unique_chatters} people; {highlight.reason} "
                          f"at {highlight.second}s.", flush=True)
                current_bucket += 1
            if not chat.is_alive() or (elapsed >= 60 and messages_seen == 0):
                print("YouTube live chat ended or returned no messages; switching to audio.", flush=True)
                return False
            for message in chat.get().sync_items():
                detector.add_message(message.author.name, message.message)
                messages_seen += 1
            time.sleep(1)
    except Exception as exc:
        print(f"YouTube chat error: {exc}; switching to audio.", file=sys.stderr, flush=True)
        return False
    finally:
        chat.terminate()
    return True


def audio_level(recording: Path, start: int, seconds: int) -> float | None:
    """Measure a finished audio window in dB; silence and incomplete windows are skipped."""
    command = ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start), "-i",
               str(recording), "-t", str(seconds), "-vn", "-af", "volumedetect",
               "-f", "null", "-"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    return float(match.group(1)) if result.returncode == 0 and match else None


def monitor_audio(settings: Settings, started: float, recording_process: subprocess.Popen[str],
                  recording: Path, jobs: queue.Queue[ClipJob | None]) -> None:
    """Fallback highlight detector for platforms without a usable chat connection."""
    history: collections.deque[float] = collections.deque(maxlen=6)
    bucket = 0
    last_clip_time = -settings.cooldown_seconds
    print("Detecting audio peaks (approximate highlight detection).", flush=True)
    while recording_process.poll() is None:
        end = (bucket + 1) * settings.bucket_seconds
        if time.monotonic() - started < end + 8:
            time.sleep(1)
            continue
        try:
            level = audio_level(recording, bucket * settings.bucket_seconds,
                                settings.bucket_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"Audio analysis error: {exc}", file=sys.stderr, flush=True)
            level = None
        at = bucket * settings.bucket_seconds + settings.bucket_seconds // 2
        if level is not None:
            if (len(history) >= 3 and level >= -25.0
                    and level >= statistics.median(history) + 8.0
                    and at - last_clip_time >= settings.cooldown_seconds):
                jobs.put(ClipJob(max(0, at - settings.before_seconds),
                                 at + settings.after_seconds, "audio"))
                last_clip_time = at
                print(f"Audio peak ({level:.1f} dB); clip queued at {at}s.", flush=True)
            history.append(level)
        bucket += 1


def run(settings: Settings) -> None:
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is required on PATH.")
    try:
        import streamlink  # noqa: F401
        if settings.platform == "youtube":
            import pytchat  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"Missing Python package: {exc.name}. See README.md.") from exc
    settings.output.mkdir(parents=True, exist_ok=True)
    print(f"Waiting for {settings.url} to go live. Press Ctrl+C to stop.", flush=True)
    try:
        while True:
            info = stream_info(settings.url)
            if info is None:
                time.sleep(settings.poll_seconds)
                continue
            if settings.gta_only and not is_gta_stream(info):
                time.sleep(settings.poll_seconds)
                continue
            session = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            recording = settings.output / f"{settings.channel}_{session}.ts"
            process, log = start_recording(settings, recording)
            started = time.monotonic()
            jobs: queue.Queue[ClipJob | None] = queue.Queue()
            worker = threading.Thread(target=clip_worker, args=(settings, recording, started, jobs), daemon=True)
            worker.start()
            print(f"Recording {recording}", flush=True)
            if settings.platform == "twitch":
                chat_worked = monitor_chat(settings, started, process, jobs, recording)
                if not chat_worked and process.poll() is None:
                    monitor_audio(settings, started, process, recording, jobs)
            elif settings.platform == "youtube":
                video_id = (info.get("metadata") or {}).get("id")
                chat_worked = bool(video_id) and monitor_youtube_chat(
                    settings, video_id, started, process, jobs, recording)
                if not chat_worked and process.poll() is None:
                    monitor_audio(settings, started, process, recording, jobs)
            else:
                monitor_audio(settings, started, process, recording, jobs)
            process.wait()
            log.close()
            if process.returncode:
                print(f"Streamlink ended ({process.returncode}); see {recording.with_suffix('.streamlink.log')}", file=sys.stderr, flush=True)
            jobs.put(None)
            jobs.join()
            worker.join()
            print("Stream ended; waiting for the next live session.", flush=True)
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
        if "process" in locals() and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if "log" in locals() and not log.closed:
            log.close()
        if "jobs" in locals() and "worker" in locals() and worker.is_alive():
            jobs.put(None)
            jobs.join()
            worker.join()


def parse_source(value: str) -> tuple[str, str, str]:
    if CHANNEL_RE.fullmatch(value):
        return value.lower(), f"https://www.twitch.tv/{value.lower()}", "twitch"
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if parsed.scheme != "https" or host not in {"twitch.tv", "youtube.com", "youtu.be", "kick.com"}:
        raise ValueError("Use a Twitch channel name or an HTTPS Twitch, YouTube, or Kick URL")
    parts = [part for part in parsed.path.split("/") if part]
    if host == "twitch.tv":
        if not parts or not CHANNEL_RE.fullmatch(parts[0]):
            raise ValueError("Use a Twitch channel URL, such as https://twitch.tv/channel")
        return parts[0].lower(), f"https://www.twitch.tv/{parts[0].lower()}", "twitch"
    if host == "kick.com":
        if not parts or not CHANNEL_RE.fullmatch(parts[0]):
            raise ValueError("Use a Kick channel URL, such as https://kick.com/channel")
        return f"kick_{parts[0].lower()}", f"https://kick.com/{parts[0].lower()}", "kick"
    if not parts:
        raise ValueError("Use a YouTube live or channel URL")
    label_source = "_".join(parts)
    if parts[0] == "watch":
        label_source = parse_qs(parsed.query).get("v", ["watch"])[0]
    label = re.sub(r"[^a-zA-Z0-9_]+", "_", label_source).strip("_")[:40] or "live"
    return f"youtube_{label}", value, "youtube"


def parse_args() -> Settings:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Twitch channel name or Twitch, YouTube, Kick live URL")
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--quality", default="best")
    parser.add_argument("--bucket-seconds", type=int, default=10)
    parser.add_argument("--min-messages", type=int, default=25)
    parser.add_argument("--spike-factor", type=float, default=3.0)
    parser.add_argument("--cooldown-seconds", type=int, default=60)
    parser.add_argument("--before-seconds", type=int, default=15)
    parser.add_argument("--after-seconds", type=int, default=15)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--reel-caption", default="GTA 6 MOMENT",
                        help="One short static caption centered between streamer and gameplay")
    parser.add_argument("--facecam", default="0,0.55,0.28,0.34",
                        help="Webcam crop as x,y,width,height fractions of the landscape stream")
    parser.add_argument("--layout", choices=("auto", "preserve", "split"), default="auto",
                        help="Auto keeps portrait streams intact; preserve always keeps the original frame")
    parser.add_argument("--chat-delay-seconds", type=int,
                        help="Shift chat-selected clips to match delayed live video (YouTube default: 10)")
    parser.add_argument("--gta-only", action="store_true", help="Only record streams whose category or title mentions GTA")
    args = parser.parse_args()
    try:
        channel, url, platform = parse_source(args.source)
    except ValueError as exc:
        parser.error(str(exc))
    for key in ("bucket_seconds", "min_messages", "cooldown_seconds", "before_seconds", "after_seconds", "poll_seconds"):
        if getattr(args, key) < 1:
            parser.error(f"--{key.replace('_', '-')} must be at least 1")
    if args.spike_factor <= 1:
        parser.error("--spike-factor must be greater than 1")
    if args.chat_delay_seconds is not None and args.chat_delay_seconds < 0:
        parser.error("--chat-delay-seconds cannot be negative")
    if not args.reel_caption.strip():
        parser.error("--reel-caption cannot be empty")
    try:
        facecam = tuple(float(value.strip()) for value in args.facecam.split(","))
    except ValueError:
        parser.error("--facecam must be x,y,width,height between 0 and 1")
    if len(facecam) != 4 or not (0 <= facecam[0] < 1 and 0 <= facecam[1] < 1
                                and 0 < facecam[2] <= 1 - facecam[0]
                                and 0 < facecam[3] <= 1 - facecam[1]):
        parser.error("--facecam must fit inside the source frame")
    return Settings(channel, url, platform, args.output.resolve(), args.quality, args.bucket_seconds,
                    args.min_messages, args.spike_factor, args.cooldown_seconds,
                    args.before_seconds, args.after_seconds, args.poll_seconds,
                    args.reel_caption,
                    args.gta_only, facecam, args.layout,
                    args.chat_delay_seconds if args.chat_delay_seconds is not None else
                    (10 if platform == "youtube" else 0))


if __name__ == "__main__":
    run(parse_args())
