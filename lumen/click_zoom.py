"""A source-timeline camera driven by editable click events.

The camera is a linear-size sequence of eased intervals, not a nested FFmpeg
expression. A private sendcmd file drives crop's runtime options; each command
has constant expression depth even for the maximum supported click list.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from .overlays import validate_project_overlays


def validate_settings(amount: float, hold: float, transition: float) -> None:
    for value, label, low, high in (
        (amount, "Click zoom amount", 1, 4),
        (hold, "Click zoom hold", 0.1, 10),
        (transition, "Click zoom transition", 0.05, 2),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            raise ValueError(f"{label} must be between {low:g} and {high:g}.")


@dataclass(frozen=True)
class CameraState:
    zoom: float = 1.0
    x: float = 0.5
    y: float = 0.5


@dataclass(frozen=True)
class CameraSegment:
    start: float
    end: float
    first: CameraState
    last: CameraState

    def at(self, time: float) -> CameraState:
        """Evaluate the same cubic easing used by the FFmpeg commands."""
        phase = min(1, max(0, (time - self.start) / (self.end - self.start)))
        eased = phase * phase * (3 - 2 * phase)
        return CameraState(
            self.first.zoom + (self.last.zoom - self.first.zoom) * eased,
            self.first.x + (self.last.x - self.first.x) * eased,
            self.first.y + (self.last.y - self.first.y) * eased,
        )


def camera_timeline(
    project: dict,
    *,
    duration: float,
    amount: float = 1.8,
    hold: float = 1.2,
    transition: float = 0.35,
) -> list[CameraSegment]:
    """Compile enabled source clicks into continuous camera movements.

    Overlapping focus windows share one zoom envelope. Their focus pans finish
    at each click; repeated clicks in the same place extend the hold. Simultaneous
    clicks use the last event in stored order. Building this before trimming
    preserves a camera movement already in progress at the trim boundary.
    """
    validate_settings(amount, hold, transition)
    validate_project_overlays({"clicks": project.get("clicks", [])})
    clicks = sorted(
        (
            item
            for item in project.get("clicks", [])
            if item.get("enabled", True) and item["t"] < duration
        ),
        key=lambda item: item["t"],
    )
    if not clicks or amount == 1:
        return []
    unique = []
    for item in clicks:
        if unique and item["t"] == unique[-1]["t"]:
            unique[-1] = item
        else:
            unique.append(item)
    groups: list[list[dict]] = []
    for item in unique:
        if not groups or item["t"] > groups[-1][-1]["t"] + hold + 2 * transition:
            groups.append([])
        groups[-1].append(item)

    segments: list[CameraSegment] = []

    def append(start, end, first, last):
        if end <= start or start >= duration or end <= 0:
            return
        if (
            segments
            and segments[-1].end == start
            and segments[-1].first == segments[-1].last == first == last
        ):
            previous = segments.pop()
            start = previous.start
        segments.append(CameraSegment(start, end, first, last))

    cursor = 0.0
    neutral = CameraState()

    def target(item):
        half = 0.5 / amount
        return CameraState(
            amount,
            min(1 - half, max(half, item["x"])),
            min(1 - half, max(half, item["y"])),
        )

    for group in groups:
        first = group[0]
        focus = target(first)
        start = first["t"] - transition
        append(cursor, start, neutral, neutral)
        append(start, first["t"], CameraState(1, focus.x, focus.y), focus)
        cursor = first["t"]
        for item in group[1:]:
            next_focus = target(item)
            pan_start = max(cursor, item["t"] - transition)
            append(cursor, pan_start, focus, focus)
            append(pan_start, item["t"], focus, next_focus)
            cursor, focus = item["t"], next_focus
        hold_end = cursor + hold
        append(cursor, hold_end, focus, focus)
        cursor = hold_end + transition
        append(hold_end, cursor, focus, CameraState(1, focus.x, focus.y))
    append(cursor, duration, neutral, neutral)
    return segments


def _f(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def compile_click_zoom(
    project: dict,
    *,
    duration: float,
    trim_start: float,
    trim_end: float,
    speed: float,
    amount: float = 1.8,
    hold: float = 1.2,
    transition: float = 0.35,
) -> str | None:
    """Return a sendcmd script, or None when retained footage stays unzoomed.

    A clipped interval still evaluates its original source-time phase. Constant
    intervals send once on entry; moving intervals evaluate at the output frame
    rate. Every interval supplies complete state, so sub-frame intervals can be
    skipped without leaving stale crop dimensions or position.
    """
    timeline = camera_timeline(
        project, duration=duration, amount=amount, hold=hold, transition=transition
    )
    retained = [
        part for part in timeline if part.end > trim_start and part.start < trim_end
    ]
    if not any(part.first.zoom > 1 or part.last.zoom > 1 for part in retained):
        return None
    lines = []
    for part in retained:
        start = (max(trim_start, part.start) - trim_start) / speed
        end = (min(trim_end, part.end) - trim_start) / speed
        if _f(start) == _f(end):
            continue
        # st/ld keeps every expression small, with no dependence on click count.
        phase = (
            f"clip((T*{_f(speed)}+{_f(trim_start - part.start)})/"
            f"{_f(part.end - part.start)},0,1)"
        )
        ease = f"(st(0,{phase});ld(0)*ld(0)*(3-2*ld(0)))"

        def lerp(first, last):
            if first == last:
                return _f(first)
            return f"({_f(first)}+{_f(last - first)}*{ease})"

        zoom = lerp(part.first.zoom, part.last.zoom)
        x = lerp(part.first.x, part.last.x)
        y = lerp(part.first.y, part.last.y)
        width, height = f"round(W/{zoom})", f"round(H/{zoom})"
        moving_zoom = part.first.zoom != part.last.zoom
        commands = []
        for name, value, moving in (
            ("w", width, moving_zoom),
            ("h", height, moving_zoom),
            (
                "x",
                f"max(0,min(W-{width},W*{x}-{width}/2))",
                moving_zoom or part.first.x != part.last.x,
            ),
            (
                "y",
                f"max(0,min(H-{height},H*{y}-{height}/2))",
                moving_zoom or part.first.y != part.last.y,
            ),
        ):
            flags = "expr" if moving else "enter"
            if not moving:
                value = value.replace("W", "iw").replace("H", "ih")
            commands.append(f"[{flags}] crop@camera {name} '{value}'")
        lines.append(f"{_f(start)}-{_f(end)} " + ", ".join(commands) + ";\n")
    return "".join(lines)


def write_camera_script(directory: str | Path, script: str) -> Path:
    path = Path(directory) / "lumen-camera.cmd"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(script)
    return path


def camera_filter(path: str | Path) -> str:
    # Escape both AVOption and filtergraph parsers; no user metadata is code.
    value = str(Path(path).resolve())
    value = "".join("\\" + char if char in "\\':" else char for char in value)
    value = "".join("\\" + char if char in "\\'[],;" else char for char in value)
    return f"sendcmd=filename={value},crop@camera=w=iw:h=ih:x=0:y=0:exact=1"
