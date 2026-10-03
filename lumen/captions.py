"""Plain-text captions, timed sidecars, and optional offline speech recognition.

Project captions always use seconds in the original recording. Export retiming
clips to the selected source interval before applying playback speed.
"""

from __future__ import annotations

import html
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import uuid


MAX_CAPTION_BYTES = 16 * 1024 * 1024
MAX_CAPTIONS = 10_000
__all__ = [
    "CaptionError", "normalize_captions", "parse_srt", "parse_vtt", "load_captions",
    "retime_captions", "write_srt", "write_vtt", "Transcriber",
    "TranscriptionCancelled", "TranscriptionError", "discover_transcriber",
]


class CaptionError(ValueError):
    pass


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise CaptionError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CaptionError(f"{name} must be a finite number") from exc
    if not math.isfinite(result):
        raise CaptionError(f"{name} must be a finite number")
    return result


def _caption_id(index: int, start: float, end: float, text: str) -> str:
    key = json.dumps([index, start, end, text], ensure_ascii=False)
    return "caption-" + uuid.uuid5(uuid.NAMESPACE_URL, key).hex[:16]


def normalize_captions(captions: list[dict], *, duration: float | None = None) -> list[dict]:
    """Validate and sort captions, retaining IDs and optional enabled flags.

    Overlapping intervals are allowed. Inputs are copied and never mutated.
    Missing IDs are deterministic; duplicate supplied IDs are rejected.
    """
    if not isinstance(captions, (list, tuple)) or len(captions) > MAX_CAPTIONS:
        raise CaptionError("Captions must be a list with at most 10,000 entries")
    if duration is not None:
        duration = _finite(duration, "Recording duration")
        if duration < 0:
            raise CaptionError("Recording duration cannot be negative")
    result = []
    seen = set()
    for index, caption in enumerate(captions):
        if not isinstance(caption, dict):
            raise CaptionError(f"Caption {index + 1} must be an object")
        start = _finite(caption.get("start"), f"Caption {index + 1} start")
        end = _finite(caption.get("end"), f"Caption {index + 1} end")
        if start < 0 or end <= start:
            raise CaptionError(f"Caption {index + 1} needs 0 ≤ start < end")
        if duration is not None and end > duration + .001:
            raise CaptionError(f"Caption {index + 1} extends past the recording")
        text = caption.get("text")
        if not isinstance(text, str):
            raise CaptionError(f"Caption {index + 1} text must be a string")
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or len(text) > 10_000 or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise CaptionError(f"Caption {index + 1} needs 1–10,000 text characters without control characters")
        identifier = caption.get("id")
        if identifier is None:
            identifier = _caption_id(index, start, end, text)
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200 or identifier in seen:
            raise CaptionError(f"Caption {index + 1} has an invalid or duplicate ID")
        seen.add(identifier)
        value = {"id": identifier, "start": start, "end": end, "text": text}
        if "enabled" in caption:
            if not isinstance(caption["enabled"], bool):
                raise CaptionError(f"Caption {index + 1} enabled must be true or false")
            value["enabled"] = caption["enabled"]
        result.append(value)
    return sorted(result, key=lambda c: (c["start"], c["end"]))


_STAMP = re.compile(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{1,3})")
_TIMING = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)(?:\s+.*)?$")
_SUBTITLE_TAG = re.compile(
    r"</?(?:b|i|u|ruby|rt|font(?:\s[^>]*)?|c(?:\.[^\s>]*)?|v(?:\s[^>]*)?|lang(?:\s[^>]*)?)\s*>"
    r"|<(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}>", re.IGNORECASE,
)


def _timestamp(value: str) -> float:
    match = _STAMP.fullmatch(value)
    if not match:
        raise CaptionError(f"Invalid caption timestamp: {value}")
    hours, minutes, seconds, fraction = match.groups()
    if int(minutes) > 59 or int(seconds) > 59:
        raise CaptionError(f"Invalid caption timestamp: {value}")
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(fraction.ljust(3, "0")) / 1000


def _plain_text(value: str) -> str:
    # Strip subtitle formatting before decoding entities, so escaped literal
    # text such as &lt;b&gt; survives as text rather than becoming a formatting tag.
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = _SUBTITLE_TAG.sub("", value)
    return "\n".join("" if line == "\u200b" else line for line in html.unescape(value).split("\n"))


def _parse(text: str, *, vtt: bool) -> list[dict]:
    if not isinstance(text, str):
        raise CaptionError("Subtitle contents must be text")
    if len(text.encode("utf-8")) > MAX_CAPTION_BYTES:
        raise CaptionError("Subtitle file exceeds the 16 MiB limit")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if vtt:
        first, _, remainder = text.partition("\n")
        if not re.fullmatch(r"WEBVTT(?:[ \t].*)?", first):
            raise CaptionError("WebVTT files must begin with WEBVTT")
        # Header metadata is separated from cues by the first blank line.
        if remainder.startswith("\n"):
            text = remainder[1:]
        elif "\n\n" in remainder:
            text = remainder.split("\n\n", 1)[1]
        elif remainder.strip():
            raise CaptionError("WebVTT needs a blank line after its header")
        else:
            text = ""
    if not text.strip():
        return []
    captions = []
    for number, block in enumerate(re.split(r"\n[ \t]*\n", text.strip()), 1):
        lines = block.splitlines()
        if not lines:
            continue
        if vtt and (re.match(r"^NOTE(?:\s|$)", lines[0]) or lines[0] in ("STYLE", "REGION")):
            continue
        timing_index = 0 if "-->" in lines[0] else 1
        if timing_index >= len(lines):
            raise CaptionError(f"Subtitle block {number} has no timing line")
        match = _TIMING.fullmatch(lines[timing_index])
        if not match:
            raise CaptionError(f"Subtitle block {number} has an invalid timing line")
        start, end = (_timestamp(value) for value in match.groups())
        payload = _plain_text("\n".join(lines[timing_index + 1:]))
        if not payload.strip():
            raise CaptionError(f"Subtitle block {number} has no caption text")
        captions.append({"id": _caption_id(number, start, end, payload), "start": start, "end": end, "text": payload})
    return normalize_captions(captions)


def parse_srt(text: str) -> list[dict]:
    return _parse(text, vtt=False)


def parse_vtt(text: str) -> list[dict]:
    return _parse(text, vtt=True)


def load_captions(path: str | os.PathLike[str]) -> list[dict]:
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_CAPTION_BYTES + 1)
    if len(data) > MAX_CAPTION_BYTES:
        raise CaptionError("Subtitle file exceeds the 16 MiB limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaptionError("Save the subtitle file as UTF-8 before importing") from exc
    return parse_vtt(text) if Path(path).suffix.lower() == ".vtt" or text.startswith("WEBVTT") else parse_srt(text)


def retime_captions(captions: list[dict], *, trim_start: float = 0, trim_end: float | None = None,
                    speed: float = 1, include_disabled: bool = False) -> list[dict]:
    start = _finite(trim_start, "Trim start")
    end = _finite(trim_end, "Trim end") if trim_end is not None else math.inf
    speed = _finite(speed, "Playback speed")
    if start < 0 or end <= start or speed <= 0:
        raise CaptionError("Caption retiming requires 0 ≤ trim start < trim end and positive speed")
    result = []
    for caption in normalize_captions(captions):
        if not include_disabled and not caption.get("enabled", True):
            continue
        clipped_start, clipped_end = max(start, caption["start"]), min(end, caption["end"])
        if clipped_end <= clipped_start:
            continue
        result.append({**caption, "start": (clipped_start - start) / speed, "end": (clipped_end - start) / speed})
    return result


def _format_stamp(milliseconds: int, *, vtt: bool) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{'.' if vtt else ','}{ms:03d}"


def _serialize(captions: list[dict], *, vtt: bool, **retiming) -> str:
    blocks = []
    for index, caption in enumerate(retime_captions(captions, **retiming), 1):
        start = int(math.floor(caption["start"] * 1000 + .5))
        end = max(start + 1, int(math.floor(caption["end"] * 1000 + .5)))
        # Escape literal markup and '-->' payloads. A zero-width line preserves
        # intentional blank text lines without terminating the subtitle block.
        payload = "\n".join(html.escape(line, quote=False) if line else "\u200b" for line in caption["text"].split("\n"))
        blocks.append(f"{index}\n{_format_stamp(start, vtt=vtt)} --> {_format_stamp(end, vtt=vtt)}\n{payload}")
    return ("WEBVTT\n\n" if vtt else "") + "\n\n".join(blocks) + ("\n" if blocks else "")


def _write_sidecar(path: str | os.PathLike[str], text: str, *, overwrite: bool) -> Path:
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Publish without a check-then-overwrite race; an existing sidecar
            # or source file can never be replaced accidentally.
            os.link(temporary, path)
        return path
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_srt(captions: list[dict], path: str | os.PathLike[str], *, trim_start: float = 0,
              trim_end: float | None = None, speed: float = 1, overwrite: bool = False) -> Path:
    text = _serialize(captions, vtt=False, trim_start=trim_start, trim_end=trim_end, speed=speed)
    return _write_sidecar(path, text, overwrite=overwrite)


def write_vtt(captions: list[dict], path: str | os.PathLike[str], *, trim_start: float = 0,
              trim_end: float | None = None, speed: float = 1, overwrite: bool = False) -> Path:
    text = _serialize(captions, vtt=True, trim_start=trim_start, trim_end=trim_end, speed=speed)
    return _write_sidecar(path, text, overwrite=overwrite)


# Convenient public API for the GTK worker; no speech package is imported here.
from .speech import (  # noqa: E402
    Transcriber, TranscriptionCancelled, TranscriptionError, discover_transcriber,
)
