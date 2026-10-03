"""Editable source-timeline layers rendered as private libass scripts.

Annotations and click rings use normalized source coordinates and are composited
before the camera zoom. Captions are composed on the finished canvas. User text
is always literal; it cannot add ASS commands or FFmpeg filters.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re


DEFAULT_COLOR = "#78c8ff"
MAX_ITEMS = 10_000


def _num(value, label: str, low: float = 0, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"{label} must be a number.")
    try:
        finite = math.isfinite(value)
    except OverflowError:
        finite = False
    if not finite or value < low or (high is not None and value > high):
        limit = f"{low:g}–{high:g}" if high is not None else f"at least {low:g}"
        raise ValueError(f"{label} must be finite and {limit}.")
    return float(value)


def _color(value, label="Layer color") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"{label} must use a six-digit hex color, such as #78c8ff.")
    return value.lower()


def _text(value, label: str, required=False) -> str:
    if not isinstance(value, str) or len(value) > 10_000:
        raise ValueError(f"{label} must be text of at most 10,000 characters.")
    if required and not value.strip():
        raise ValueError(f"{label} cannot be empty.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError(f"{label} contains unsupported control characters.")
    return value


def _items(project: dict, key: str) -> list[dict]:
    items = project.get(key, [])
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(
            f"{key.title()} must be a list of at most {MAX_ITEMS:,} items."
        )
    return items


def validate_project_overlays(project: dict, duration: float | None = None) -> None:
    """Validate layer metadata without changing it; unknown metadata is preserved.

    Events ending beyond the source are valid and are clipped on export. This is
    normal for click rings placed during the final half-second of a recording.
    """
    if duration is not None:
        _num(duration, "Recording duration")
    for key in ("captions", "annotations", "clicks"):
        ids = set()
        for index, item in enumerate(_items(project, key)):
            label = f"{key.title()} item {index + 1}"
            if not isinstance(item, dict):
                raise ValueError(f"{label} must be an object.")
            identifier = item.get("id")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or len(identifier) > 200
            ):
                raise ValueError(f"{label} needs a stable text id.")
            if identifier in ids:
                raise ValueError(f"{key.title()} contains duplicate id {identifier!r}.")
            ids.add(identifier)
            if not isinstance(item.get("enabled", True), bool):
                raise ValueError(f"{label}: enabled must be true or false.")
            if key == "clicks":
                _num(item.get("t"), f"{label} time")
                _num(item.get("duration", 0.6), f"{label} duration", 0.01, 10)
                _num(item.get("size", 0.04), f"{label} radius", 0.005, 0.2)
                if item.get("button", "left") not in {"left", "right", "middle"}:
                    raise ValueError(f"{label} has an unknown mouse button.")
            else:
                start = _num(item.get("start"), f"{label} start")
                end = _num(item.get("end"), f"{label} end")
                if end <= start:
                    raise ValueError(f"{label} must end after it starts.")
            if key == "captions":
                _text(item.get("text"), f"{label} text", required=True)
                continue
            _num(item.get("x"), f"{label} X position", 0, 1)
            _num(item.get("y"), f"{label} Y position", 0, 1)
            _color(item.get("color", DEFAULT_COLOR), f"{label} color")
            if key == "annotations":
                kind = item.get("kind")
                if kind not in {"text", "arrow", "box", "highlight"}:
                    raise ValueError(f"{label} has an unknown annotation type.")
                _num(item.get("size", 0.045), f"{label} size", 0.01, 0.25)
                _text(item.get("text", ""), f"{label} text", required=kind == "text")
                if kind != "text":
                    _num(item.get("x2"), f"{label} end X position", 0, 1)
                    _num(item.get("y2"), f"{label} end Y position", 0, 1)
                    if kind == "arrow":
                        if item["x"] == item["x2"] and item["y"] == item["y2"]:
                            raise ValueError(f"{label} arrow endpoints must differ.")
                    elif item["x"] == item["x2"] or item["y"] == item["y2"]:
                        raise ValueError(
                            f"{label} rectangle needs positive width and height."
                        )
    style = project.get("caption_style", {})
    if not isinstance(style, dict):
        raise ValueError("Caption style must be an object.")
    _num(style.get("font_size", 0.045), "Caption font size", 0.015, 0.15)
    _color(style.get("color", "#ffffff"), "Caption text color")
    if style.get("position", "bottom") not in {"top", "bottom"}:
        raise ValueError("Caption position must be top or bottom.")
    if not isinstance(style.get("background", True), bool):
        raise ValueError("Caption background must be true or false.")


def escape_ass_text(text: str) -> str:
    """Escape libass literal braces and prevent user backslashes becoming tags.

    A zero-width word joiner after each literal backslash prevents special text
    sequences like \\N while retaining the visible slash. Braces use libass's
    documented literal-bracket escape. Actual newlines become hard line breaks.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return (
        text.replace("\\", "\\\u2060")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\t", "    ")
        .replace("\n", r"\N")
    )


def _ass_color(color: str) -> str:
    return "&H" + color[5:7] + color[3:5] + color[1:3] + "&"


def _f(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _time(value: float) -> str:
    ticks = max(0, round(value * 100))
    return f"{ticks // 360000}:{ticks // 6000 % 60:02}:{ticks // 100 % 60:02}.{ticks % 100:02}"


def _header(
    width: int, height: int, font_size: float, caption_style: dict | None = None
) -> str:
    style = caption_style or {}
    caption_color = _ass_color(style.get("color", "#ffffff"))
    border_style = 4 if style.get("background", True) else 1
    outline = max(1, height * 0.0025)
    shadow = max(2, height * 0.012) if style.get("background", True) else 0
    alignment = 8 if style.get("position", "bottom") == "top" else 2
    margin = round(height * 0.055)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
Kerning: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,{_f(font_size)},{caption_color},&H00000000,&H90000000,&H40251710,-1,0,0,0,100,100,0,0,{border_style},{_f(outline)},{_f(shadow)},{alignment},{round(width * 0.06)},{round(width * 0.06)},{margin},1
Style: Text,DejaVu Sans,{_f(font_size)},&H00FFFFFF,&H00000000,&H80000000,&H90000000,-1,0,0,0,100,100,0,0,1,{_f(outline)},1,7,0,0,0,1
Style: Shape,DejaVu Sans,16,&H00FFFFFF,&H00000000,&H00FFFFFF,&HFF000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _interval(
    start: float, end: float, trim_start: float, trim_end: float, speed: float
):
    first, last = max(start, trim_start), min(end, trim_end)
    if last <= first:
        return None
    return ((first - trim_start) / speed, (last - trim_start) / speed)


def _event(start: float, end: float, style: str, text: str, layer=0) -> str:
    if _time(start) == _time(end):
        return ""
    return f"Dialogue: {layer},{_time(start)},{_time(end)},{style},,0,0,0,,{text}\n"


def _polygon(points: list[tuple[float, float]]) -> tuple[float, float, str]:
    left, top = min(p[0] for p in points), min(p[1] for p in points)
    local = [(x - left, y - top) for x, y in points]
    drawing = f"m {_f(local[0][0])} {_f(local[0][1])} l "
    drawing += " ".join(f"{_f(x)} {_f(y)}" for x, y in local[1:] + local[:1])
    return left, top, drawing


def _annotation(item: dict, width: int, height: int) -> tuple[str, str]:
    x, y = item["x"] * width, item["y"] * height
    size = item.get("size", 0.045) * height
    color = _ass_color(item.get("color", DEFAULT_COLOR))
    if item["kind"] == "text":
        tags = rf"{{\an7\pos({_f(x)},{_f(y)})\fs{_f(size)}\1c{color}}}"
        return "Text", tags + escape_ass_text(item["text"])
    x2, y2 = item["x2"] * width, item["y2"] * height
    stroke = max(2, size * 0.15)
    if item["kind"] == "arrow":
        length = math.hypot(x2 - x, y2 - y)
        ux, uy = (x2 - x) / length, (y2 - y) / length
        px, py = -uy, ux
        head = min(length * 0.45, max(stroke * 5, height * 0.03))
        bx, by = x2 - ux * head, y2 - uy * head
        points = [
            (x + px * stroke / 2, y + py * stroke / 2),
            (bx + px * stroke / 2, by + py * stroke / 2),
            (bx + px * head * 0.5, by + py * head * 0.5),
            (x2, y2),
            (bx - px * head * 0.5, by - py * head * 0.5),
            (bx - px * stroke / 2, by - py * stroke / 2),
            (x - px * stroke / 2, y - py * stroke / 2),
        ]
        left, top, drawing = _polygon(points)
        tags = rf"{{\an7\pos({_f(left)},{_f(top)})\bord0\shad0\1c{color}\p1}}"
    else:
        left, top, right, bottom = min(x, x2), min(y, y2), max(x, x2), max(y, y2)
        _, _, drawing = _polygon(
            [(0, 0), (right - left, 0), (right - left, bottom - top), (0, bottom - top)]
        )
        fill = r"\1a&HBB&" if item["kind"] == "highlight" else r"\1a&HFF&"
        tags = rf"{{\an7\pos({_f(left)},{_f(top)})\bord{_f(stroke)}\shad0\1c{color}\3c{color}{fill}\p1}}"
    return "Shape", tags + drawing


def _click(
    item: dict,
    width: int,
    height: int,
    trim_start: float,
    trim_end: float,
    speed: float,
) -> str:
    t, duration = item["t"], item.get("duration", 0.6)
    start, end = max(t, trim_start), min(t + duration, trim_end)
    p0, p1 = (start - t) / duration, (end - t) / duration
    radius = item.get("size", 0.04) * min(width, height)
    scale0, scale1 = 35 + 65 * p0, 35 + 65 * p1
    color = _ass_color(item.get("color", DEFAULT_COLOR))
    stroke = max(1.5, min(width, height) * 0.006)
    # A cubic Bézier circle, centered by an5; transparent fill leaves a ring.
    r, k = radius, radius * 0.55228475
    drawing = (
        f"m {_f(r)} 0 b {_f(r + k)} 0 {_f(2 * r)} {_f(r - k)} {_f(2 * r)} {_f(r)} "
        f"{_f(2 * r)} {_f(r + k)} {_f(r + k)} {_f(2 * r)} {_f(r)} {_f(2 * r)} "
        f"{_f(r - k)} {_f(2 * r)} 0 {_f(r + k)} 0 {_f(r)} "
        f"0 {_f(r - k)} {_f(r - k)} 0 {_f(r)} 0"
    )
    tags = (
        rf"{{\an5\pos({_f(item['x'] * width)},{_f(item['y'] * height)})\shad0"
        rf"\bord{_f(stroke)}\1a&HFF&\3c{color}\3a&H{round(255 * p0):02X}&"
        rf"\fscx{_f(scale0)}\fscy{_f(scale0)}"
        rf"\t(0,{round((end - start) / speed * 1000)},\fscx{_f(scale1)}\fscy{_f(scale1)}\3a&H{round(255 * p1):02X}&)\p1}}"
    )
    return tags + drawing


@dataclass(frozen=True)
class OverlayScripts:
    source: str | None = None
    captions: str | None = None

    def write(self, directory: str | Path) -> dict[str, Path]:
        """Write only into a caller-owned scratch directory, never the project."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        paths = {}
        for name, script in (("source", self.source), ("captions", self.captions)):
            if script:
                path = directory / f"lumen-{name}.ass"
                # Exclusive creation prevents a caller-provided folder from
                # causing an unrelated file/symlink to be replaced.
                with path.open("x", encoding="utf-8") as stream:
                    stream.write(script)
                paths[name] = path.resolve()
        return paths


def compile_overlays(
    project: dict,
    *,
    source_width: int,
    source_height: int,
    canvas_width: int,
    canvas_height: int,
    trim_start: float,
    trim_end: float,
    speed: float,
    captions=True,
    annotations=True,
    clicks=True,
) -> OverlayScripts:
    """Compile ASS in memory. All events are clipped/remapped onto export time."""
    validate_project_overlays(project)
    source_events = []
    caption_events = []
    if annotations:
        for item in _items(project, "annotations"):
            if not item.get("enabled", True):
                continue
            interval = _interval(
                item["start"], item["end"], trim_start, trim_end, speed
            )
            if interval:
                style, text = _annotation(item, source_width, source_height)
                source_events.append(_event(*interval, style, text))
    if clicks:
        for item in _items(project, "clicks"):
            if not item.get("enabled", True):
                continue
            interval = _interval(
                item["t"],
                item["t"] + item.get("duration", 0.6),
                trim_start,
                trim_end,
                speed,
            )
            if interval:
                source_events.append(
                    _event(
                        *interval,
                        "Shape",
                        _click(
                            item,
                            source_width,
                            source_height,
                            trim_start,
                            trim_end,
                            speed,
                        ),
                        layer=1,
                    )
                )
    if captions:
        for item in _items(project, "captions"):
            if not item.get("enabled", True):
                continue
            interval = _interval(
                item["start"], item["end"], trim_start, trim_end, speed
            )
            if interval:
                caption_events.append(
                    _event(*interval, "Caption", escape_ass_text(item["text"]))
                )
    caption_style = project.get("caption_style", {})
    return OverlayScripts(
        source=(
            _header(source_width, source_height, source_height * 0.045)
            + "".join(source_events)
        )
        if any(source_events)
        else None,
        captions=(
            _header(
                canvas_width,
                canvas_height,
                canvas_height * caption_style.get("font_size", 0.045),
                caption_style,
            )
            + "".join(caption_events)
        )
        if any(caption_events)
        else None,
    )


def ass_filter(path: str | Path, width: int, height: int) -> str:
    """Escape a local filename across both FFmpeg filtergraph parsing layers."""
    # First AVOption quoting, then filtergraph quoting. A single backslash is
    # consumed at each layer. No shell parser is involved.
    escaped = str(Path(path).resolve())
    escaped = escaped.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    escaped = escaped.replace("\\", "\\\\").replace("'", "\\'")
    for char in ",;[]":
        escaped = escaped.replace(char, "\\" + char)
    return f"ass=filename={escaped}:original_size={width}x{height}"
