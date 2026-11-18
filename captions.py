"""Transcribe speech and build readable, word-highlighted reel captions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_MODEL_CACHE: dict[str, object] = {}


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{milliseconds:03}"


def ass_timestamp(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360_000)
    minutes, centiseconds = divmod(centiseconds, 6_000)
    secs, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02}:{secs:02}.{centiseconds:02}"


def group_words(words: list[Word]) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current and (len(current) >= 5 or word.start - current[-1].end > 0.55
                        or word.end - current[0].start > 2.8):
            groups.append(current)
            current = []
        current.append(word)
        if word.text.rstrip().endswith((".", "!", "?", "؟")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def ass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def write_captions(words: list[Word], base: Path) -> tuple[Path, Path, Path]:
    groups = group_words(words)
    srt = base.with_suffix(".srt")
    ass = base.with_suffix(".ass")
    transcript = base.with_suffix(".txt")
    srt_lines: list[str] = []
    events: list[str] = []
    for number, group in enumerate(groups, 1):
        plain = " ".join(word.text.strip() for word in group)
        srt_lines.extend([str(number),
                          f"{timestamp(group[0].start)} --> {timestamp(group[-1].end)}",
                          plain, ""])
        for index, word in enumerate(group):
            start = word.start
            end = group[index + 1].start if index + 1 < len(group) else word.end
            if end <= start:
                end = start + 0.08
            pieces = []
            for j, item in enumerate(group):
                escaped = ass_escape(item.text.strip())
                if j == index:
                    pieces.append("{\\c&H00D7FF&}" + escaped + "{\\c&H00FFFFFF&}")
                else:
                    pieces.append(escaped)
            if len(pieces) > 3:
                text = " ".join(pieces[:3]) + "\\N" + " ".join(pieces[3:])
            else:
                text = " ".join(pieces)
            events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Default,,0,0,0,,{text}")
    srt.write_text("\n".join(srt_lines), encoding="utf-8")
    transcript.write_text(" ".join(word.text.strip() for word in words) + "\n", encoding="utf-8")
    ass_header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        "BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        "BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        "Style: Default,Arial,76,&H00FFFFFF,&H0000D7FF,&H00000000,&H80000000,"
        "-1,0,0,0,100,100,0,0,1,5,1,2,50,50,62,1\n\n"
        "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )
    ass.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8")
    return srt, ass, transcript


def transcribe_clip(clip: Path, model_name: str = "small", language: str | None = None) -> tuple[Path, Path, Path]:
    from faster_whisper import WhisperModel

    if model_name not in _MODEL_CACHE:
        _MODEL_CACHE[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    model = _MODEL_CACHE[model_name]
    segments, _ = model.transcribe(
        str(clip), beam_size=5, language=language, word_timestamps=True,
        vad_filter=True, condition_on_previous_text=False,
    )
    words = [Word(word.start, word.end, word.word)
             for segment in segments for word in (segment.words or [])
             if word.start is not None and word.end is not None and word.word.strip()]
    return write_captions(words, clip)
