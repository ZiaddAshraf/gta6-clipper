"""Run the live clipper for several Twitch, YouTube, or Kick sources."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from clipper import parse_source


def load_channels(path: Path) -> list[tuple[str, str]]:
    channels: list[tuple[str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        source = line.split("#", 1)[0].strip()
        if not source:
            continue
        try:
            name, _, _ = parse_source(source)
        except ValueError as exc:
            raise ValueError(f"Invalid source at {path}:{number}: {exc}") from exc
        if name not in [entry[0] for entry in channels]:
            channels.append((name, source))
    if not channels:
        raise ValueError(f"No channels found in {path}")
    return channels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", type=Path, default=Path("channels.txt"))
    parser.add_argument("--output", type=Path, default=Path("output"))
    parser.add_argument("--reel-caption", default="GTA 6 MOMENT")
    parser.add_argument("--quality", default="best")
    parser.add_argument("--facecam-config", type=Path, default=Path(__file__).with_name("facecams.json"))
    parser.add_argument("--layout-config", type=Path, default=Path(__file__).with_name("layouts.json"))
    parser.add_argument("--all-games", action="store_true", help="Record even when title/category does not mention GTA")
    args = parser.parse_args()
    try:
        channels = load_channels(args.channels)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        facecams = json.loads(args.facecam_config.read_text(encoding="utf-8"))
        layouts = json.loads(args.layout_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        parser.error(f"Cannot read layout configuration: {exc}")
    processes: dict[str, subprocess.Popen] = {}
    print(f"Monitoring {len(channels)} sources: {', '.join(name for name, _ in channels)}", flush=True)
    try:
        for name, source in channels:
            command = [sys.executable, str(Path(__file__).with_name("clipper.py")), source,
                       "--output", str(args.output / name), "--quality", args.quality,
                       "--reel-caption", args.reel_caption]
            crop = facecams.get(name, facecams.get("default"))
            if crop is None:
                parser.error(f"No facecam crop for {name} in {args.facecam_config}")
            command.extend(["--facecam", ",".join(str(value) for value in crop)])
            command.extend(["--layout", layouts.get(name, layouts.get("default", "auto"))])
            if not args.all_games:
                command.append("--gta-only")
            processes[name] = subprocess.Popen(command)
        while True:
            time.sleep(3)
            failed = {name: process.returncode for name, process in processes.items()
                      if process.poll() is not None}
            if failed:
                raise RuntimeError(f"Clipper stopped for {failed}")
    except KeyboardInterrupt:
        print("Stopping all channel monitors.", flush=True)
    finally:
        for process in processes.values():
            if process.poll() is None:
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.terminate()
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
