#!/usr/bin/env python3
"""Exercise native capture → pause/resume → reload → edit → decode.

Run this inside the Hyprland desktop session. It records about two seconds of a
small region around the existing pointer, including desktop audio (never the
microphone). It opens no windows and does not move the pointer. Artifacts remain
in a private /tmp/lumen-workflow-* directory, printed before capture starts.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lumen.capture import CaptureOptions, Recorder
from lumen.editor import Exporter, ExportOptions, probe, suggest_zoom
from lumen.project import load_project, save_project
from lumen.system import get_monitors


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def region_at_pointer() -> tuple[dict, str, tuple[int, int]]:
    monitors = get_monitors()
    require(bool(monitors), "No Hyprland monitors are available in this session.")
    response = subprocess.run(
        ["hyprctl", "cursorpos", "-j"],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    cursor = json.loads(response.stdout)
    monitor = next(
        (
            m
            for m in monitors
            if m["x"] <= cursor["x"] < m["x"] + m["logical_width"]
            and m["y"] <= cursor["y"] < m["y"] + m["logical_height"]
        ),
        next((m for m in monitors if m.get("focused")), monitors[0]),
    )
    # GSR interprets regions in logical coordinates and captures native pixels.
    scale = float(monitor.get("scale", 1))
    width, height = round(640 / scale), round(360 / scale)
    require(
        width <= monitor["logical_width"] and height <= monitor["logical_height"],
        "The chosen display is too small for a native 640×360 test region.",
    )
    x = int(
        max(
            monitor["x"],
            min(
                cursor["x"] - width // 2,
                monitor["x"] + monitor["logical_width"] - width,
            ),
        )
    )
    y = int(
        max(
            monitor["y"],
            min(
                cursor["y"] - height // 2,
                monitor["y"] + monitor["logical_height"] - height,
            ),
        )
    )
    expected = (round(width * scale), round(height * scale))
    return monitor, f"{x},{y} {width}x{height}", expected


def stop_owned_recorder(recorder: Recorder) -> None:
    """Always reap only this test's process, including exceptions and SIGTERM."""
    if recorder.is_running:
        try:
            recorder.stop()
        finally:
            process = recorder.process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="Artifact directory; default creates and preserves a private /tmp directory",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=2.0,
        help="Active capture time, excluding a 0.3-second pause (default: 2)",
    )
    args = parser.parse_args()
    require(
        math.isfinite(args.seconds) and 1.5 <= args.seconds <= 10,
        "Capture duration must be 1.5–10 seconds.",
    )
    root = (
        args.root.expanduser().resolve()
        if args.root
        else Path(tempfile.mkdtemp(prefix="lumen-workflow-"))
    )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    print(f"Artifact root: {root}", flush=True)
    summary_path = root / f"workflow-summary-{time.time_ns()}.json"
    recorder = Recorder(root=root / "library")
    summary = {
        "status": "running",
        "artifact_root": str(root),
        "summary_path": str(summary_path),
    }
    original_handlers = {}

    def interrupted(signum, _frame):
        raise KeyboardInterrupt(f"Interrupted by signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        original_handlers[sig] = signal.signal(sig, interrupted)
    try:
        monitor, geometry, expected_size = region_at_pointer()
        options = CaptureOptions(
            monitor=monitor["name"],
            mode="region",
            geometry=geometry,
            fps=30,
            encoder="gpu",
            audio="desktop",
            cursor=True,
            backend="gpu-screen-recorder",
            cursor_telemetry=True,
        )
        summary["capture_options"] = asdict(options)
        summary["expected_native_size"] = list(expected_size)
        started = time.monotonic()
        project = recorder.start(options)
        summary["project_path"] = project["path"]
        summary["startup_seconds"] = round(time.monotonic() - started, 3)
        time.sleep(args.seconds / 2)
        recorder.pause()
        paused_at = recorder.elapsed
        require(recorder.status == "paused", "Recorder did not enter paused state.")
        time.sleep(0.3)
        require(
            abs(recorder.elapsed - paused_at) < 0.02,
            "Recording timer advanced during pause.",
        )
        recorder.resume()
        require(recorder.status == "recording", "Recorder did not resume.")
        time.sleep(args.seconds / 2)
        project = recorder.stop()
        require(not recorder.is_running, "Recorder remains active after stop.")
        summary["capture_wall_seconds"] = round(time.monotonic() - started, 3)
        summary["pause_resume_verified"] = True
        project = load_project(project["path"])
        source = Path(project["path"]) / project["source"]
        media = probe(source)
        summary["source"] = {
            k: media[k] for k in ("path", "width", "height", "fps", "duration", "codec")
        }
        require(project["status"] == "ready", "Reloaded project is not marked ready.")
        require(
            abs(media["width"] - expected_size[0]) <= 2
            and abs(media["height"] - expected_size[1]) <= 2,
            f"Unexpected native capture dimensions: {media['width']}×{media['height']}",
        )
        require(
            args.seconds - 0.35 <= media["duration"] <= args.seconds + 1.0,
            f"Capture duration {media['duration']:.3f}s is outside expected active time.",
        )
        require(
            len(media["audio_streams"]) == 1
            and project.get("audio_tracks") == {"desktop": 0},
            "Capture did not preserve the requested desktop-only audio track.",
        )
        checksum_before = digest(source)
        suggestion = suggest_zoom(project)
        summary["cursor_suggestion"] = suggestion
        summary["cursor_suggestion_used"] = suggestion is not None
        cursor_path = Path(project["path"]) / "cursor.json"
        if cursor_path.exists():
            summary["cursor_samples"] = len(
                json.loads(cursor_path.read_text()).get("samples", [])
            )
        if suggestion is None:
            # A user moving the pointer can legitimately prevent a dwell. Still
            # test timed zoom and say explicitly that no suggestion was applied.
            suggestion = {
                "zoom": 1.6,
                "zoom_x": 0.5,
                "zoom_y": 0.5,
                "zoom_start": 0.2,
                "zoom_end": media["duration"] - 0.2,
            }
            summary["note"] = (
                "No cursor dwell was available; export used an explicit timed center zoom."
            )
        edits = ExportOptions(
            trim_start=0.15,
            trim_end=media["duration"] - 0.15,
            speed=1.5,
            background="violet",
            padding=32,
            output_width=960,
            audio_mode="desktop",
            volume=0.8,
            fps=30,
            **suggestion,
        )
        project["edits"] = asdict(edits)
        save_project(project)
        project = load_project(project["path"])
        require(
            project["edits"] == asdict(edits), "Project edits did not survive reload."
        )
        destination = Path(project["path"]) / "workflow-export.mp4"
        progress = []
        render_started = time.monotonic()
        Exporter().export(project, edits, destination, on_progress=progress.append)
        summary["export_seconds"] = round(time.monotonic() - render_started, 3)
        rendered = probe(destination)
        summary["export"] = {
            k: rendered[k]
            for k in ("path", "width", "height", "fps", "duration", "codec")
        }
        expected_duration = (edits.trim_end - edits.trim_start) / edits.speed
        require(
            rendered["width"] == 960 and rendered["height"] > 360,
            "Styled export has incorrect dimensions.",
        )
        require(
            abs(rendered["duration"] - expected_duration) < 0.15,
            "Trim/speed export duration is incorrect.",
        )
        require(len(rendered["audio_streams"]) == 1, "Export is missing desktop audio.")
        require(
            progress and progress[0] == 0 and progress[-1] == 1,
            "Export progress did not complete.",
        )
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-nostdin",
                "-i",
                str(destination),
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        checksum_after = digest(source)
        require(
            checksum_before == checksum_after, "Export changed the original recording."
        )
        summary.update(
            status="passed",
            source_sha256=checksum_after,
            source_unchanged=True,
            output_decodes=True,
            expected_export_duration=round(expected_duration, 4),
            export_progress_events=len(progress),
            edits=asdict(edits),
        )
    except BaseException as exc:
        summary.update(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(),
        )
    finally:
        try:
            stop_owned_recorder(recorder)
        except BaseException as exc:
            summary["cleanup_error"] = f"{type(exc).__name__}: {exc}"
            summary["status"] = "failed"
        summary["recorder_stopped"] = not recorder.is_running
        summary_path.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2), flush=True)
        for sig, handler in original_handlers.items():
            signal.signal(sig, handler)
    return 0 if summary["status"] == "passed" and summary["recorder_stopped"] else 1


if __name__ == "__main__":
    sys.exit(main())
