"""Render a 9:16 reel with facecam above gameplay."""

from __future__ import annotations

import json
import subprocess
import tempfile
import textwrap
from pathlib import Path


CANVAS_WIDTH = 1080
CAM_HEIGHT = 576
GAME_HEIGHT = 1344


def write_reel_caption(text: str, destination: Path) -> None:
    """Create one static caption centered on the seam between camera and game."""
    words = " ".join(text.split()).upper()
    if not words:
        raise ValueError("Reel caption cannot be empty")
    lines = textwrap.wrap(words, width=20, break_long_words=False)
    if len(lines) > 3:
        raise ValueError("Reel caption is too long; use at most three short lines")
    escaped = r"\N".join(
        line.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        for line in lines
    )
    destination.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Caption,Arial,76,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "-1,0,0,0,100,100,0,0,1,6,1,5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
        f"Dialogue: 0,0:00:00.00,3:00:00.00,Caption,,0,0,0,,"
        f"{{\\an5\\pos(540,{CAM_HEIGHT})}}{escaped}\n",
        encoding="utf-8",
    )


def video_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(result.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def layout_crops(width: int, height: int,
                 facecam: tuple[float, float, float, float],
                 game_focus_x: float = 0.5) -> tuple[tuple[int, int, int, int],
                                                     tuple[int, int, int, int]]:
    """Fill the lower panel with a centered gameplay crop and the upper with facecam."""
    x, y, w, h = facecam
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x and 0 < h <= 1 - y):
        raise ValueError("Facecam must be normalized x,y,width,height inside the source frame")
    if not 0 <= game_focus_x <= 1:
        raise ValueError("Game focus must be between 0 and 1")
    cam_w = max(2, min(width, round(w * width) // 2 * 2))
    cam_h = max(2, min(height, round(h * height) // 2 * 2))
    cam_x = max(0, min(width - cam_w, round(x * width) // 2 * 2))
    cam_y = max(0, min(height - cam_h, round(y * height) // 2 * 2))
    game_w = min(width, round(height * CANVAS_WIDTH / GAME_HEIGHT) // 2 * 2)
    game_x = round((width - game_w) * game_focus_x) // 2 * 2
    return (game_w, height, game_x, 0), (cam_w, cam_h, cam_x, cam_y)


def choose_layout(width: int, height: int, requested: str = "auto") -> str:
    if requested not in {"auto", "preserve", "split"}:
        raise ValueError(f"Unknown layout: {requested}")
    return ("preserve" if height >= width else "split") if requested == "auto" else requested


def render_reel(clip: Path, destination: Path,
                facecam: tuple[float, float, float, float] | None = None,
                layout: str = "auto", game_focus_x: float = 0.5,
                caption: str = "GTA 6 MOMENT") -> None:
    """Preserve a composed frame; split a landscape stream only when needed."""
    clip = clip.resolve()
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    width, height = video_size(clip)
    selected = choose_layout(width, height, layout)
    if selected == "preserve":
        filters = "[0:v]null[canvas]"
    else:
        if facecam is None:
            raise ValueError("A facecam crop is required for split layout")
        (game_w, game_h, game_x, game_y), (cam_w, cam_h, cam_x, cam_y) = layout_crops(
            width, height, facecam, game_focus_x)
        filters = (
            "[0:v]split=2[action][context];"
            f"[action]crop={game_w}:{game_h}:{game_x}:{game_y},"
            f"scale={CANVAS_WIDTH}:{GAME_HEIGHT}:flags=lanczos,setsar=1[bottom];"
            f"[context]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},"
            "hqdn3d=1.2:1.2:3:3,"
            f"scale={CANVAS_WIDTH}:{CAM_HEIGHT}:flags=lanczos:force_original_aspect_ratio=increase,"
            f"crop={CANVAS_WIDTH}:{CAM_HEIGHT},"
            "unsharp=5:5:0.35:5:5:0,setsar=1[top];"
            "[top][bottom]vstack=inputs=2[canvas]"
        )
    with tempfile.TemporaryDirectory() as temporary:
        write_reel_caption(caption, Path(temporary) / "reel_caption.ass")
        filters += ";[canvas]subtitles=reel_caption.ass[out]"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clip)]
        command.extend(["-filter_complex", filters, "-map", "[out]", "-map", "0:a:0?",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", "-shortest", str(destination)])
        subprocess.run(command, cwd=temporary, check=True)
