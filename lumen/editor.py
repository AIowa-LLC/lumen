"""Nondestructive, cancellable FFmpeg editing for Lumen.

Times in options and cursor telemetry refer to the original recording. Exported
files are published atomically; recording sources and existing exports are never
overwritten. No shell is involved in media processing.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from typing import Callable

from .click_zoom import (
    camera_filter,
    compile_click_zoom,
    validate_settings as validate_click_zoom,
    write_camera_script,
)
from .overlays import ass_filter, compile_overlays
from .wallpapers import BACKGROUND_STYLES, DEFAULT_WALLPAPER, wallpaper_path


class ExportError(RuntimeError):
    """An edit could not be rendered."""


class ExportCancelled(ExportError):
    """The user cancelled an export; no partial destination was published."""


@dataclass
class ExportOptions:
    trim_start: float = 0.0
    trim_end: float | None = None
    speed: float = 1.0
    background: str = "midnight"
    padding: int = 64
    wallpaper: str = DEFAULT_WALLPAPER
    output_width: int | None = 1920
    zoom: float = 1.0
    zoom_x: float = 0.5
    zoom_y: float = 0.5
    zoom_start: float | None = None
    zoom_end: float | None = None
    audio_mode: str = "mix"
    volume: float = 1.0
    format: str = "mp4"
    fps: int | None = None
    quality: int = 20
    captions: bool = True
    annotations: bool = True
    clicks: bool = True
    click_zoom: bool = False
    click_zoom_amount: float = 1.8
    click_zoom_hold: float = 1.2
    click_zoom_transition: float = 0.35


BACKGROUNDS = {
    "midnight": ("0x101725", "0x344b60"),
    "violet": ("0x27203e", "0x8063a6"),
    "sand": ("0xd3bba0", "0xf2e7d5"),
    "none": ("black", "black"),
}


def _number(value: object, name: str) -> float:
    try:
        value = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _fmt(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def probe(path: str | Path) -> dict:
    """Read actual media dimensions, duration, and audio streams with ffprobe."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ExportError(f"Recording not found: {path}")
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = json.loads(result.stdout)
    except FileNotFoundError as exc:
        raise ExportError("FFprobe is missing. Install the ffmpeg package.") from exc
    except subprocess.CalledProcessError as exc:
        raise ExportError(
            f"Cannot read recording: {exc.stderr.strip()[-1600:]}"
        ) from exc
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ExportError(f"Cannot inspect recording: {path.name}") from exc
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ExportError("This file does not contain a video stream.")
    try:
        fps = float(Fraction(video.get("avg_frame_rate", "0/1")))
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if not fps:
        try:
            fps = float(Fraction(video.get("r_frame_rate", "30/1")))
        except (ValueError, ZeroDivisionError):
            fps = 30.0
    duration = float(
        data.get("format", {}).get("duration") or video.get("duration") or 0
    )
    return {
        "path": str(path),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "fps": fps or 30.0,
        "codec": video.get("codec_name", "unknown"),
        "audio_streams": [s for s in streams if s.get("codec_type") == "audio"],
    }


def _source(project: dict) -> Path:
    source = Path(project.get("source", "source.mkv"))
    if not source.is_absolute():
        source = Path(project["path"]) / source
    return source.expanduser().resolve()


def _safe_destination(source: Path, destination: str | Path) -> Path:
    destination = Path(destination).expanduser().absolute()
    if destination.resolve() == source.resolve():
        raise ValueError("An export cannot replace its source recording.")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(
            f"Choose a new filename; this file already exists: {destination.name}"
        )
    return destination


def _validate(options: ExportOptions, media: dict) -> tuple[float, float, float]:
    for name in ("captions", "annotations", "clicks", "click_zoom"):
        if not isinstance(getattr(options, name), bool):
            raise ValueError(f"{name.title()} visibility must be true or false.")
    start = _number(options.trim_start, "Trim start")
    end = _number(
        options.trim_end if options.trim_end is not None else media["duration"],
        "Trim end",
    )
    speed = _number(options.speed, "Speed")
    if not 0 <= start < end or end > media["duration"] + 0.1:
        raise ValueError("Trim must have a positive duration within the recording.")
    end = min(end, media["duration"])
    if not 0.25 <= speed <= 4:
        raise ValueError("Speed must be between 0.25× and 4×.")
    if options.background not in BACKGROUND_STYLES:
        raise ValueError("Unknown background style.")
    if options.audio_mode not in {"mix", "desktop", "mic", "none"}:
        raise ValueError("Unknown audio selection.")
    if options.format not in {"mp4", "gif"}:
        raise ValueError("Export format must be MP4 or GIF.")
    if options.click_zoom:
        validate_click_zoom(
            options.click_zoom_amount,
            options.click_zoom_hold,
            options.click_zoom_transition,
        )
    else:
        if not 1 <= _number(options.zoom, "Zoom") <= 4:
            raise ValueError("Zoom must be between 1× and 4×.")
        for axis in (options.zoom_x, options.zoom_y):
            if not 0 <= _number(axis, "Zoom center") <= 1:
                raise ValueError("Zoom center must be inside the frame.")
    if not 0 <= _number(options.volume, "Volume") <= 4:
        raise ValueError("Volume must be between 0 and 400%.")
    if options.output_width is not None and (
        not isinstance(options.output_width, int)
        or not 128 <= options.output_width <= 7680
    ):
        raise ValueError("Output width must be 128–7680 pixels, or original size.")
    if not isinstance(options.padding, int) or not 0 <= options.padding <= 512:
        raise ValueError("Frame padding must be 0–512 pixels.")
    if not isinstance(options.quality, int) or not 0 <= options.quality <= 40:
        raise ValueError("Quality must be between 0 and 40.")
    if options.fps is not None and (
        not isinstance(options.fps, int) or not 1 <= options.fps <= 120
    ):
        raise ValueError("Frame rate must be between 1 and 120.")
    if not options.click_zoom and (
        options.zoom_start is not None or options.zoom_end is not None
    ):
        zstart = _number(
            options.zoom_start if options.zoom_start is not None else start,
            "Zoom start",
        )
        zend = _number(
            options.zoom_end if options.zoom_end is not None else end, "Zoom end"
        )
        if not 0 <= zstart < zend <= media["duration"] + 0.1:
            raise ValueError("Zoom interval must be within the recording.")
    return start, end, (end - start) / speed


def _audio_indices(project: dict, media: dict, mode: str) -> list[int]:
    streams = media["audio_streams"]
    if mode == "none" or not streams:
        return []
    if mode == "mix":
        return list(range(len(streams)))
    tracks = project.get("audio_tracks", {})
    if isinstance(tracks, dict) and mode in tracks:
        index = tracks[mode]
        if isinstance(index, int) and 0 <= index < len(streams):
            return [index]
    if isinstance(tracks, dict) and tracks:
        return []
    for index, stream in enumerate(streams):
        title = str(stream.get("tags", {}).get("title", "")).lower()
        if mode in title or (mode == "mic" and "microphone" in title):
            return [index]
    # A tagged single stream must never be relabelled as a different device.
    if any(s.get("tags", {}).get("title") for s in streams):
        return []
    index = 0 if mode == "desktop" else 1
    return [index] if index < len(streams) else []


def _atempo(speed: float) -> str:
    factors = []
    while speed < 0.5:
        factors.append(0.5)
        speed /= 0.5
    while speed > 2:
        factors.append(2)
        speed /= 2
    factors.append(speed)
    return ",".join(f"atempo={_fmt(factor)}" for factor in factors)


def audio_modes(project: dict, media: dict | None = None) -> set[str]:
    """Return valid export audio choices; mix also supports a silent original."""
    media = probe(_source(project)) if media is None else media
    return {"mix", "none"} | {
        mode for mode in ("desktop", "mic") if _audio_indices(project, media, mode)
    }


def _command(
    project: dict,
    options: ExportOptions,
    destination: Path,
    media: dict,
    overlay_dir: str | Path | None = None,
) -> list[str]:
    start, end, duration = _validate(options, media)
    source = _source(project)
    output_width = _even(options.output_width or media["width"])
    padding = options.padding if options.background != "none" else 0
    padding = _even(padding) if padding else 0
    content_width = output_width - padding * 2
    if content_width < 64:
        raise ValueError("Frame padding leaves too little room for the recording.")
    content_height = _even(content_width * media["height"] / media["width"])
    output_height = content_height + padding * 2
    backdrop = (
        wallpaper_path(project, options.wallpaper)
        if padding and options.background == "wallpaper"
        else None
    )
    scripts = compile_overlays(
        project,
        source_width=media["width"],
        source_height=media["height"],
        canvas_width=output_width,
        canvas_height=output_height,
        trim_start=start,
        trim_end=end,
        speed=options.speed,
        captions=options.captions,
        annotations=options.annotations,
        clicks=options.clicks,
    )
    overlay_paths = {}
    if scripts.source or scripts.captions:
        if overlay_dir is None:
            raise ValueError(
                "This export has overlay layers. Supply a fresh overlay_dir to build_export_command, or use Exporter.export()."
            )
        overlay_paths = scripts.write(overlay_dir)
    fps = options.fps or min(60, max(1, round(media["fps"])))
    if options.format == "gif":
        fps = min(fps, 24)
    filters = [f"setpts=(PTS-STARTPTS)/{_fmt(options.speed)}", f"fps={fps}"]
    if "source" in overlay_paths:
        filters.append(
            ass_filter(overlay_paths["source"], media["width"], media["height"])
        )
    camera_script = (
        compile_click_zoom(
            project,
            duration=media["duration"],
            trim_start=start,
            trim_end=end,
            speed=options.speed,
            amount=options.click_zoom_amount,
            hold=options.click_zoom_hold,
            transition=options.click_zoom_transition,
        )
        if options.click_zoom
        else None
    )
    if camera_script:
        if overlay_dir is None:
            raise ValueError(
                "This export follows clicks. Supply a fresh overlay_dir to build_export_command, or use Exporter.export()."
            )
        filters.append(camera_filter(write_camera_script(overlay_dir, camera_script)))
        filters.append(f"scale={content_width}:{content_height}:flags=lanczos")
    elif not options.click_zoom and options.zoom > 1:
        zoom = _fmt(options.zoom)
        if options.zoom_start is not None or options.zoom_end is not None:
            zstart = (
                (options.zoom_start if options.zoom_start is not None else start)
                - start
            ) / options.speed
            zend = (
                (options.zoom_end if options.zoom_end is not None else end) - start
            ) / options.speed
            ramp = min(0.55, (zend - zstart) / 3)
            rise = f"(0.5-0.5*cos(PI*clip((on/{fps}-{_fmt(zstart)})/{_fmt(ramp)},0,1)))"
            fall = f"(0.5-0.5*cos(PI*clip(({_fmt(zend)}-on/{fps})/{_fmt(ramp)},0,1)))"
            zoom = f"1+{_fmt(options.zoom - 1)}*{rise}*{fall}"
        filters.append(
            f"zoompan=z='{zoom}':"
            f"x='max(0,min(iw-iw/zoom,iw*{_fmt(options.zoom_x)}-iw/zoom/2))':"
            f"y='max(0,min(ih-ih/zoom,ih*{_fmt(options.zoom_y)}-ih/zoom/2))':"
            f"d=1:s={content_width}x{content_height}:fps={fps}"
        )
    else:
        filters.append(f"scale={content_width}:{content_height}:flags=lanczos")
    filters.append("setsar=1")
    graph = [f"[0:v:0]{','.join(filters)}[content]"]
    if padding:
        if backdrop:
            background_filter = (
                f"[1:v:0]scale={output_width}:{output_height}:"
                "force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={output_width}:{output_height},setsar=1,"
                "setpts=PTS-STARTPTS,format=yuv420p"
            )
        else:
            c0, c1 = BACKGROUNDS[options.background]
            background_filter = (
                f"gradients=s={output_width}x{output_height}:r={fps}:c0={c0}:c1={c1}:"
                f"x0=0:y0=0:x1={output_width}:y1={output_height}:speed=0:seed=0:d={_fmt(duration)}"
            )
        shadow_offset = max(2, min(padding // 4, 12))
        graph.append(
            background_filter + ","
            f"drawbox=x={padding + shadow_offset}:y={padding + shadow_offset}:"
            f"w={content_width}:h={content_height}:c=black@0.22:t=fill[background]"
        )
        graph.append(
            f"[background][content]overlay=x={padding}:y={padding}:shortest=1,"
            f"drawbox=x={padding - 1}:y={padding - 1}:w={content_width + 2}:h={content_height + 2}:"
            "c=white@0.16:t=1[framed]"
        )
    else:
        graph.append("[content]null[framed]")
    final_label = "framed"
    if "captions" in overlay_paths:
        graph.append(
            f"[framed]{ass_filter(overlay_paths['captions'], output_width, output_height)}[captioned]"
        )
        final_label = "captioned"
    if options.format == "gif":
        graph += [
            f"[{final_label}]split[palette_input][gif_input]",
            # Sample every frame: diff-only palettes can omit short overlays
            # on otherwise static desktop recordings.
            "[palette_input]palettegen=stats_mode=full[palette]",
            "[gif_input][palette]paletteuse=dither=sierra2_4a:diff_mode=rectangle[vout]",
        ]
    else:
        graph.append(f"[{final_label}]format=yuv420p[vout]")
    audio = (
        _audio_indices(project, media, options.audio_mode)
        if options.format == "mp4"
        else []
    )
    if (
        options.format == "mp4"
        and options.audio_mode in {"desktop", "mic"}
        and not audio
    ):
        name = "desktop" if options.audio_mode == "desktop" else "microphone"
        raise ValueError(
            f"This recording has no separate {name} audio track. Choose Mix all tracks or Silent."
        )
    for n, index in enumerate(audio):
        graph.append(
            f"[0:a:{index}]asetpts=PTS-STARTPTS,{_atempo(options.speed)}[a{n}]"
        )
    if audio:
        if len(audio) > 1:
            graph.append(
                "".join(f"[a{n}]" for n in range(len(audio)))
                + f"amix=inputs={len(audio)}:duration=longest:dropout_transition=0:normalize=0[amixed]"
            )
        else:
            graph.append("[a0]anull[amixed]")
        graph.append(
            f"[amixed]volume={_fmt(options.volume)},alimiter=limit=0.95:level=0:latency=1[aout]"
        )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-ss",
        _fmt(start),
        "-t",
        _fmt(end - start),
        "-i",
        str(source),
    ]
    if backdrop:
        command += ["-loop", "1", "-framerate", str(fps), "-i", str(backdrop)]
    command += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
    if audio:
        command += ["-map", "[aout]", "-c:a", "aac", "-b:a", "192k"]
    else:
        command += ["-an"]
    if options.format == "mp4":
        command += [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            str(options.quality),
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
        ]
    else:
        command += ["-loop", "0", "-f", "gif"]
    command += [
        "-t",
        _fmt(duration),
        "-progress",
        "pipe:1",
        "-nostats",
        str(destination),
    ]
    return command


def build_export_command(
    project: dict,
    options: ExportOptions,
    destination: str | Path,
    *,
    overlay_dir: str | Path | None = None,
) -> list[str]:
    """Build FFmpeg argv, protecting the source and existing destinations.

    Active layers and click cameras require a fresh caller-owned overlay_dir for
    generated ASS/command files. Keep it until the command has finished. Without
    these generated scripts this function remains read-only.
    """
    source = _source(project)
    target = _safe_destination(source, destination)
    return _command(project, options, target, probe(source), overlay_dir)


class Exporter:
    """One export at a time. Run export() in a worker; cancel() is thread safe."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

    def export(
        self,
        project: dict,
        options: ExportOptions,
        destination: str | Path,
        on_progress: Callable[[float], None] | None = None,
    ) -> Path:
        if not self._busy.acquire(blocking=False):
            raise ExportError("This exporter is already rendering a recording.")
        process = None
        try:
            project = deepcopy(project)
            options = replace(options)
            source = _source(project)
            target = _safe_destination(source, destination)
            media = probe(source)
            _, _, duration = _validate(options, media)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".lumen-export-", dir=target.parent
            ) as temporary:
                partial = Path(temporary) / ("render." + options.format)
                command = _command(project, options, partial, media, temporary)
                if on_progress:
                    on_progress(0.0)
                with tempfile.TemporaryFile(mode="w+t") as errors:
                    if self._cancelled.is_set():
                        raise ExportCancelled("Export cancelled.")
                    try:
                        with self._lock:
                            process = subprocess.Popen(
                                command,
                                stdout=subprocess.PIPE,
                                stderr=errors,
                                text=True,
                                bufsize=1,
                            )
                            self._process = process
                    except FileNotFoundError as exc:
                        raise ExportError(
                            "FFmpeg is missing. Install the ffmpeg package."
                        ) from exc
                    assert process.stdout is not None
                    for line in process.stdout:
                        if self._cancelled.is_set():
                            break
                        if line.startswith("out_time_us=") and on_progress:
                            try:
                                fraction = (
                                    float(line.split("=", 1)[1]) / 1_000_000 / duration
                                )
                            except ValueError:
                                continue
                            on_progress(max(0.0, min(0.99, fraction)))
                    if self._cancelled.is_set():
                        raise ExportCancelled("Export cancelled.")
                    returncode = process.wait()
                    if returncode:
                        errors.seek(0)
                        raise ExportError(
                            "FFmpeg could not export the recording:\n"
                            + errors.read()[-4000:]
                        )
                if not partial.is_file() or partial.stat().st_size == 0:
                    raise ExportError("FFmpeg did not produce a video.")
                if self._cancelled.is_set():
                    raise ExportCancelled("Export cancelled.")
                # Hard-link publication is atomic and refuses to replace a file
                # created by another process while rendering.
                os.link(partial, target)
                if on_progress:
                    on_progress(1.0)
                return target
        finally:
            with self._lock:
                self._process = None
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdout:
                    process.stdout.close()
            self._cancelled.clear()
            self._busy.release()


def thumbnail(
    source: str | Path, dest: str | Path, time: float = 0.0, width: int = 960
) -> Path:
    """Render a preview PNG/JPEG, replacing only a separate thumbnail file."""
    source = Path(source).expanduser().resolve()
    dest = Path(dest).expanduser().absolute()
    if source == dest.resolve():
        raise ValueError("A thumbnail cannot replace the source recording.")
    if not math.isfinite(time) or time < 0 or not 16 <= width <= 3840:
        raise ValueError("Invalid thumbnail time or width.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".lumen-thumb-", dir=dest.parent
    ) as temporary:
        partial = Path(temporary) / ("thumb" + (dest.suffix or ".png"))
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-ss",
                    _fmt(time),
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    str(partial),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ExportError(f"Could not create recording preview: {exc}") from exc
        if not partial.exists():
            raise ExportError("No frame exists at this preview time.")
        os.replace(partial, dest)
    return dest


def suggest_zoom(project: dict) -> dict | None:
    """Suggest a focus interval from the longest cursor dwell, never inferred clicks.

    Cursor x/y are normalized within the captured region. Gaps over 0.75s
    terminate a dwell so paused recording telemetry cannot invent a focus event.
    """
    cursor_file = Path(project["path"]) / "cursor.json"
    try:
        if cursor_file.stat().st_size > 20_000_000:
            return None
        data = json.loads(cursor_file.read_text())
    except (OSError, ValueError):
        return None
    samples = data.get("samples", []) if isinstance(data, dict) else []
    if not isinstance(samples, list):
        return None
    try:
        duration = _number(project.get("duration", 0), "Duration")
    except ValueError:
        return None
    if duration <= 0:
        return None
    best: list[tuple[float, float, float]] = []
    current: list[tuple[float, float, float]] = []
    for sample in samples:
        if isinstance(sample, dict) and sample.get("inside") is False:
            # Leaving the captured region ends a dwell. Do not recommend a
            # zoom toward cursor coordinates clamped to a monitor edge.
            if current and current[-1][0] - current[0][0] > (
                best[-1][0] - best[0][0] if best else 0
            ):
                best = current
            current = []
            continue
        try:
            point = tuple(_number(sample[key], key) for key in ("t", "x", "y"))
        except (ValueError, TypeError, KeyError):
            continue
        t, x, y = point
        if not (0 <= t <= duration and 0 <= x <= 1 and 0 <= y <= 1):
            continue
        if current and (
            t <= current[-1][0]
            or t - current[-1][0] > 0.75
            or math.hypot(x - current[0][1], y - current[0][2]) > 0.07
        ):
            if current[-1][0] - current[0][0] > (
                best[-1][0] - best[0][0] if best else 0
            ):
                best = current
            current = []
        current.append(point)
    if current and current[-1][0] - current[0][0] > (
        best[-1][0] - best[0][0] if best else 0
    ):
        best = current
    if not best or best[-1][0] - best[0][0] < 0.7:
        return None
    return {
        "zoom": 1.8,
        "zoom_x": sum(s[1] for s in best) / len(best),
        "zoom_y": sum(s[2] for s in best) / len(best),
        "zoom_start": max(0, best[0][0] - 0.4),
        "zoom_end": min(duration, best[-1][0] + 0.5),
    }
