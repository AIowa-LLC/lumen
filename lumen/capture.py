"""Own one recorder process; never signal another recorder on the desktop."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import re

from . import project as projects
from ._process import spawn_owned
from .clicks import ClickCapture, ClickCaptureError
from .camera import CameraBubble, CameraError
from .system import (
    DesktopError,
    get_monitors,
    parse_geometry,
    probe_media,
    select_region,
    _run,
)


class CaptureError(RuntimeError):
    pass


@dataclass
class CaptureOptions:
    monitor: str = ""
    mode: str = "monitor"
    geometry: str | None = None
    fps: int = 60
    encoder: str = "auto"
    audio: str = "none"
    mic_source: str | None = None
    cursor: bool = True
    codec: str = "h264"
    quality: str = "very_high"
    backend: str = "auto"
    webcam: str | None = None
    resolution: str | None = None
    cursor_telemetry: bool = True
    record_clicks: bool = True
    live_inputs: bool = False

    def validate(self) -> None:
        if self.mode not in ("monitor", "region", "window"):
            raise ValueError("Choose a monitor, region, or fixed window area.")
        if self.audio not in ("none", "desktop", "mic", "both"):
            raise ValueError("Unknown audio mode.")
        if self.encoder not in ("auto", "gpu", "cpu"):
            raise ValueError("Unknown encoder.")
        if self.backend not in ("auto", "gpu-screen-recorder", "wf-recorder"):
            raise ValueError("Unknown capture backend.")
        if self.codec not in ("h264", "hevc", "av1"):
            raise ValueError("Unsupported capture codec.")
        if self.encoder == "cpu" and self.codec != "h264":
            raise ValueError("CPU capture supports H.264 only.")
        if self.quality not in ("medium", "high", "very_high", "ultra"):
            raise ValueError("Unknown quality preset.")
        if (
            not isinstance(self.fps, int)
            or isinstance(self.fps, bool)
            or not 1 <= self.fps <= 240
        ):
            raise ValueError("Frame rate must be between 1 and 240.")
        if self.geometry:
            parse_geometry(self.geometry)
        if not isinstance(self.live_inputs, bool):
            raise ValueError("Live inputs must be enabled or disabled.")
        if self.webcam and not re.fullmatch(r"/dev/video\d+", self.webcam):
            raise ValueError("Choose a valid webcam device.")
        if self.resolution:
            if not re.fullmatch(r"\d+x\d+", self.resolution):
                raise ValueError("Resolution must use WIDTHxHEIGHT.")
            width, height = map(int, self.resolution.split("x"))
            if (width, height) != (0, 0) and not (
                2 <= width <= 16384 and 2 <= height <= 16384
            ):
                raise ValueError(
                    "Resolution must be 0x0 (native), or between 2 and 16384 pixels per side."
                )


def build_gsr_command(options: CaptureOptions, output: Path, ipc: Path) -> list[str]:
    options.validate()
    target = options.monitor
    command = ["gpu-screen-recorder"]
    if options.mode in ("region", "window"):
        if not options.geometry:
            raise ValueError("Select a recording area first.")
        x, y, width, height = parse_geometry(options.geometry)
        target = f"{width}x{height}+{x}+{y}"
    if options.webcam and not options.live_inputs:
        target += (
            f"|{options.webcam};halign=end;valign=end;hflip=true;width=25%;height=25%"
        )
    command += [
        "-w",
        target,
        "-k",
        options.codec,
        "-f",
        str(options.fps),
        "-fm",
        "vfr",
        "-q",
        options.quality,
        "-cursor",
        "yes" if options.cursor else "no",
        "-encoder",
        "cpu" if options.encoder == "cpu" else "gpu",
        "-fallback-cpu-encoding",
        "yes" if options.encoder == "auto" and options.codec == "h264" else "no",
        "-ac",
        "aac",
        "-exclude-metadata",
        "yes",
        "-write-first-frame-ts",
        "yes",
        "-o",
        str(output),
        "-ipc",
        str(ipc),
    ]
    audio = []
    if options.audio in ("desktop", "both"):
        audio.append("default_output")
    if options.audio in ("mic", "both"):
        audio.append(
            "device:" + options.mic_source if options.mic_source else "default_input"
        )
    for source in audio:
        command += ["-a", source]
    if options.resolution:
        command += ["-s", options.resolution]
    return command


def build_wf_command(options: CaptureOptions, output: Path) -> list[str]:
    options.validate()
    if options.live_inputs:
        raise CaptureError("Live microphone and camera controls require GPU Screen Recorder.")
    if options.audio == "both":
        raise CaptureError(
            "The compatibility backend cannot mix desktop and microphone audio. Choose one audio source or use GPU Screen Recorder."
        )
    if options.webcam or not options.cursor or options.codec != "h264":
        raise CaptureError(
            "The compatibility backend supports H.264 with the captured cursor, without webcam composition."
        )
    if options.resolution:
        raise CaptureError("The compatibility backend records at native resolution.")
    crf = {"medium": 28, "high": 23, "very_high": 20, "ultra": 16}[options.quality]
    command = [
        "wf-recorder",
        "-f",
        str(output),
        "-r",
        str(options.fps),
        "--no-dmabuf",
        "-c",
        "libx264",
        "-p",
        "preset=ultrafast",
        "-p",
        f"crf={crf}",
        "-x",
        "yuv420p",
        "-F",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
    ]
    if options.mode in ("region", "window"):
        if not options.geometry:
            raise ValueError("Select a recording area first.")
        command += ["-g", options.geometry]
    elif options.monitor:
        command += ["-o", options.monitor]
    if options.audio != "none":
        if options.audio == "desktop":
            source = _run(["pactl", "get-default-sink"]) + ".monitor"
        else:
            source = options.mic_source or _run(["pactl", "get-default-source"])
        command += ["--audio=" + source, "-C", "aac"]
    return command


def capture_bounds(options: CaptureOptions) -> tuple:
    if options.mode in ("region", "window") and options.geometry:
        return parse_geometry(options.geometry)
    monitor = next((m for m in get_monitors() if m["name"] == options.monitor), None)
    if not monitor:
        raise CaptureError("Could not determine the recorded monitor bounds.")
    return monitor["x"], monitor["y"], monitor["logical_width"], monitor["logical_height"]


def audio_track_map(options: CaptureOptions) -> dict:
    desktop = options.audio in ("desktop", "both")
    mic = options.live_inputs or options.audio in ("mic", "both")
    return ({"desktop": 0, "mic": 1} if desktop and mic else
            {"desktop": 0} if desktop else {"mic": 0} if mic else {})


class LiveInputs:
    """Shared capture/replay ownership of the private mic route and camera bubble."""

    def __init__(self, options: CaptureOptions):
        self.options = replace(options)
        self.microphone = None
        self.camera: CameraBubble | None = None

    def prepare(self) -> CaptureOptions:
        from .microphone import LiveMicrophone

        self.microphone = LiveMicrophone(
            mic_source=self.options.mic_source,
            enabled=self.options.audio in ("mic", "both"),
        )
        source = self.microphone.start()
        self.camera = CameraBubble(capture_bounds(self.options), self.options.webcam)
        if self.options.webcam:
            self.camera.start()
        # A silent private mic track exists from the first frame. Enabling the
        # microphone later never needs to reopen or restart the video encoder.
        return replace(self.options, audio="both" if self.options.audio in ("desktop", "both") else "mic",
                       mic_source=source, webcam=None)

    def bind(self, pid: int) -> None:
        self.microphone.bind(pid)

    @property
    def mic_enabled(self) -> bool:
        return bool(self.microphone and self.microphone.enabled)

    @property
    def camera_enabled(self) -> bool:
        return bool(self.camera and self.camera.enabled)

    def set_microphone_enabled(self, enabled: bool) -> None:
        if not self.microphone:
            raise CaptureError("Live microphone controls are unavailable.")
        self.microphone.set_enabled(enabled)

    def set_webcam_enabled(self, enabled: bool, device: str | None = None) -> None:
        if not self.camera:
            raise CaptureError("Live camera controls are unavailable.")
        if enabled:
            self.camera.start(device)
        else:
            self.camera.stop()

    def close(self) -> None:
        errors = []
        for resource in (self.camera, self.microphone):
            if resource:
                try:
                    (resource.stop if resource is self.camera else resource.close)()
                except Exception as exc:
                    errors.append(str(exc))
        if errors:
            raise CaptureError("Live input cleanup failed: " + "; ".join(errors))


class Recorder:
    """Blocking control methods belong on the GUI worker thread."""

    def __init__(self, root: str | Path | None = None):
        self.root = root
        self.process: subprocess.Popen | None = None
        self.project: dict | None = None
        self.status = "idle"
        self.backend: str | None = None
        self._ipc: Path | None = None
        self._runtime: Path | None = None
        self._log = None
        self._lock = threading.RLock()
        self._started_at = 0.0
        self._paused_at = 0.0
        self._pause_duration = 0.0
        self._telemetry_stop = threading.Event()
        self._telemetry_thread: threading.Thread | None = None
        self._cursor_samples: list[dict] = []
        self._capture_bounds: tuple[float, float, float, float] | None = None
        self._clicks: ClickCapture | None = None
        self._live: LiveInputs | None = None

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def live_inputs_available(self) -> bool:
        return self.is_running and self._live is not None

    @property
    def mic_enabled(self) -> bool:
        if not self.is_running:
            return False
        if self._live:
            return self._live.mic_enabled
        return bool(self.project and self.project.get("capture", {}).get("audio") in ("mic", "both"))

    @property
    def camera_enabled(self) -> bool:
        if not self.is_running:
            return False
        if self._live:
            return self._live.camera_enabled
        return bool(self.project and self.project.get("capture", {}).get("webcam"))

    def set_microphone_enabled(self, enabled: bool) -> None:
        self._set_input("microphone", enabled)

    def set_webcam_enabled(self, enabled: bool, device: str | None = None) -> None:
        self._set_input("camera", enabled, device)

    def _set_input(self, kind: str, enabled: bool, device: str | None = None) -> None:
        with self._lock:
            if not isinstance(enabled, bool):
                raise CaptureError("Input state must be enabled or disabled.")
            if not self.live_inputs_available:
                raise CaptureError("Live controls are unavailable for this recording.")
            try:
                if kind == "microphone":
                    self._live.set_microphone_enabled(enabled)
                else:
                    self._live.set_webcam_enabled(enabled, device)
            except Exception as exc:
                raise CaptureError(str(exc)) from exc
            if self.project:
                events = self.project.setdefault("input_events", [])
                if len(events) < 10000:
                    events.append({"t": round(self.elapsed, 4), "input": kind, "enabled": enabled})
                self._persist_best_effort()

    @property
    def elapsed(self) -> float:
        if not self._started_at:
            return 0.0
        now = self._paused_at if self.status == "paused" else time.monotonic()
        return max(0, now - self._started_at - self._pause_duration)

    def start(self, options: CaptureOptions) -> dict:
        """All failed starts release resources, including failures while saving metadata."""
        with self._lock:
            if self.is_running:
                raise CaptureError("A recording is already active.")
            if self.status in ("recording", "paused"):
                # A child can die between UI ticks. Finalize that take before replacing it.
                self.stop()
            self._cleanup()
            self.process = None
            self.project = None
            self._started_at = 0
            self.status = "starting"
            try:
                return self._start(options)
            except Exception as exc:
                stop_error = None
                try:
                    self._end_process()
                except CaptureError as shutdown_error:
                    stop_error = str(shutdown_error)
                self.status = "failed"
                message = str(exc) + (f"\n{stop_error}" if stop_error else "")
                if self.project:
                    self.project.update(status="failed", error=message)
                    self.project.pop("capture_pid", None)
                    self._persist_best_effort()
                self._cleanup()
                if isinstance(exc, (CaptureError, DesktopError, OSError, ValueError)):
                    raise CaptureError(message) from exc
                raise

    def _start(self, options: CaptureOptions) -> dict:
        with self._lock:
            if self.is_running:
                raise CaptureError("A recording is already active.")
            options.validate()
            if options.mode == "region" and not options.geometry:
                options.geometry = select_region()
                if not options.geometry:
                    raise CaptureError("Region selection cancelled.")
            if options.mode == "monitor" and not options.monitor:
                monitors = get_monitors()
                if not monitors:
                    raise CaptureError(
                        "No monitors found. Launch Lumen in your Hyprland session."
                    )
                options.monitor = next(
                    (m["name"] for m in monitors if m.get("focused")),
                    monitors[0]["name"],
                )
            if options.mode == "window" and not options.geometry:
                raise CaptureError("Choose a window area first.")
            if options.mode in ("region", "window"):
                x, y, width, height = parse_geometry(options.geometry)
                monitors = get_monitors()
                if monitors and not any(
                    x >= m["x"]
                    and y >= m["y"]
                    and x + width <= m["x"] + m["logical_width"]
                    and y + height <= m["y"] + m["logical_height"]
                    for m in monitors
                ):
                    raise CaptureError(
                        "Choose a region entirely inside one monitor. Areas across monitors are not supported."
                    )
            self.project = (
                projects.create_project(root=self.root)
                if self.root is not None
                else projects.create_project()
            )
            folder = Path(self.project["path"])
            folder.mkdir(parents=True, exist_ok=True)
            audio_tracks = audio_track_map(options)
            self.project.update(
                status="starting",
                capture=asdict(options),
                fps=options.fps,
                source="source.mkv",
                source_path=str(folder / "source.mkv"),
                audio_tracks=audio_tracks,
            )
            projects.save_project(self.project)
            runtime_base = os.environ.get("XDG_RUNTIME_DIR")
            try:
                self._runtime = Path(
                    tempfile.mkdtemp(
                        prefix="lumen-",
                        dir=runtime_base
                        if runtime_base and Path(runtime_base).is_dir()
                        else None,
                    )
                )
            except OSError:
                # Headless/sandboxed tests may inherit a read-only runtime directory.
                self._runtime = Path(tempfile.mkdtemp(prefix="lumen-"))
            self._ipc = self._runtime / "capture.sock"
            self._log = open(folder / "capture.log", "ab", buffering=0)
            backends = (
                [options.backend]
                if options.backend != "auto"
                else ["gpu-screen-recorder", "wf-recorder"]
            )
            failures = []
            for backend in backends:
                if not shutil.which(backend):
                    failures.append(f"{backend} is not installed")
                    continue
                # An explicitly requested GPU path must never silently become software.
                if backend == "wf-recorder" and options.encoder == "gpu":
                    failures.append(
                        "wf-recorder requires CPU encoding; GPU-only capture was requested"
                    )
                    continue
                self.backend = backend
                try:
                    backend_options = options
                    if options.live_inputs:
                        if backend != "gpu-screen-recorder":
                            if options.backend != "auto":
                                raise CaptureError("Live input controls require GPU Screen Recorder.")
                            backend_options = replace(options, live_inputs=False)
                        else:
                            self._live = LiveInputs(options)
                            backend_options = self._live.prepare()
                    command = (
                        build_gsr_command(backend_options, folder / "source.mkv", self._ipc)
                        if backend == "gpu-screen-recorder"
                        else build_wf_command(backend_options, folder / "source.mkv")
                    )
                    self._log.write(("\nLumen backend: " + backend + "\n").encode())
                    self.process = spawn_owned(
                        command,
                        stdout=self._log,
                        stderr=self._log,
                        stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                    self._started_at = time.monotonic()
                    self._wait_started(folder / "source.mkv")
                    if self._live:
                        self._live.bind(self.process.pid)
                    timestamp = folder / "source.mkv.ts"
                    if backend == "gpu-screen-recorder" and timestamp.exists():
                        try:
                            self._started_at = (
                                int(timestamp.read_text().splitlines()[1].split()[0])
                                / 1_000_000
                            )
                        except (ValueError, IndexError, OSError):
                            pass
                    self._pause_duration = 0
                    self._paused_at = 0
                    self.status = "recording"
                    self.project.update(
                        status="recording",
                        backend=backend,
                        capture_pid=self.process.pid,
                    )
                    if backend == "wf-recorder":
                        self.project.update(capture=asdict(backend_options),
                                            audio_tracks=audio_track_map(backend_options))
                    if self._live:
                        self.project["input_events"] = [
                            {"t": 0, "input": "microphone", "enabled": self._live.mic_enabled},
                            {"t": 0, "input": "camera", "enabled": self._live.camera_enabled},
                        ]
                    if failures:
                        self.project["capture_warning"] = (
                            "GPU capture failed; compatibility recording uses CPU encoding."
                            + (" Live microphone and camera controls are unavailable for this take."
                               if options.live_inputs else "")
                        )
                    projects.save_project(self.project)
                    if options.cursor_telemetry:
                        self._start_telemetry(options)
                    if options.record_clicks:
                        self._start_clicks(options)
                    else:
                        self.project["click_capture_status"] = "Click capture is disabled for this take."
                    self._persist_best_effort()
                    return self.project
                except (CaptureError, CameraError, OSError, ValueError, DesktopError, RuntimeError) as exc:
                    failures.append(f"{backend}: {exc}")
                    self._end_process()
                    self._close_live_inputs()
                    if isinstance(exc, OSError) and exc.errno in (
                        errno.ENOSPC,
                        errno.EDQUOT,
                        errno.EROFS,
                        errno.EACCES,
                    ):
                        # Another backend cannot repair unavailable storage. Never overwrite
                        # an already captured take while trying to recover from a disk error.
                        raise
                    raw = folder / "source.mkv"
                    if raw.exists():
                        raw.rename(folder / f"failed-{backend}-{time.time_ns()}.mkv")
            self.status = "failed"
            self.project.update(status="failed", error="\n".join(failures))
            self._persist_best_effort()
            self._cleanup()
            raise CaptureError(
                "Recording could not start. "
                + "\n".join(failures)
                + f"\nDetails: {folder / 'capture.log'}"
            )

    def _wait_started(self, output: Path) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.is_running:
                detail = self._log_tail()
                raise CaptureError(
                    f"Recorder exited with code {self.process.returncode} before it was ready."
                    + (f"\n{detail}" if detail else "")
                )
            ready = output.exists() and output.stat().st_size > 0
            if self.backend == "gpu-screen-recorder":
                # First-frame timestamp is emitted after a frame has entered the encoder.
                # Matroska buffers its first cluster for seconds, so nonzero file size
                # would delay the UI's recording state even while capture is running.
                ready = Path(str(output) + ".ts").exists() and self._ipc.exists()
            if ready:
                return
            time.sleep(0.05)
        raise CaptureError("Recorder did not produce a frame within 10 seconds.")

    def _command(self, name: str, data=None, timeout: float = 15):
        if not self._ipc:
            raise CaptureError("Capture control socket is unavailable.")
        request = {"id": 1, "name": name}
        if data is not None:
            request["data"] = data
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self._ipc))
                connection.sendall((json.dumps(request) + "\n").encode())
                response = b""
                while b"\n" not in response:
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise CaptureError("Recorder closed its control connection.")
                    response += chunk
                    if len(response) > 65536:
                        raise CaptureError(
                            "Recorder returned an invalid control response."
                        )
                result = json.loads(response.split(b"\n", 1)[0])
        except (OSError, ValueError) as exc:
            raise CaptureError(f"Cannot control recorder: {exc}") from exc
        if not isinstance(result, dict) or result.get("id") != 1:
            raise CaptureError("Recorder returned an invalid control response.")
        if result.get("result") != "ok":
            raise CaptureError(
                str(result.get("data", "Recorder rejected the command."))
            )
        return result.get("data")

    def pause(self) -> None:
        with self._lock:
            if not self.is_running:
                raise CaptureError("No recording is active.")
            if self.backend != "gpu-screen-recorder":
                raise CaptureError(
                    "Pause requires GPU Screen Recorder; compatibility recording cannot pause safely."
                )
            if self.status == "paused":
                return
            self._command("set-paused", True)
            self._paused_at = time.monotonic()
            self.status = "paused"

    def resume(self) -> None:
        with self._lock:
            if self.status != "paused":
                return
            self._command("set-paused", False)
            self._pause_duration += time.monotonic() - self._paused_at
            self._paused_at = 0
            self.status = "recording"

    def stop(self) -> dict:
        with self._lock:
            if not self.project or not self.process:
                raise CaptureError("No recording is active.")
            if self.status == "idle" and self.project.get("status") == "ready":
                self._cleanup()
                return self.project
            self.status = "finalizing"
            self._stop_clicks()
            self._stop_telemetry()
            interrupted = self.process.poll() is not None
            source = Path(self.project["path"]) / "source.mkv"
            try:
                if self.is_running:
                    try:
                        if self.backend == "gpu-screen-recorder":
                            self._command("stop", timeout=8)
                        else:
                            self.process.send_signal(signal.SIGINT)
                    except CaptureError:
                        if self.is_running:
                            self.process.send_signal(signal.SIGINT)
                    try:
                        self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self._end_process()
                        interrupted = True
                interrupted = interrupted or self.process.returncode not in (None, 0)
                self.project.update(probe_media(source))
                self.project.update(
                    status="ready",
                    interrupted=interrupted,
                    capture_exit_code=self.process.returncode,
                )
                if interrupted:
                    self.project["capture_warning"] = (
                        "The recorder exited unexpectedly. The readable part of this take was preserved. "
                        "Check the end of the recording and capture.log before exporting."
                    )
                self.project.pop("capture_pid", None)
                self._persist_best_effort()
                self.status = "idle"
                return self.project
            except (CaptureError, DesktopError, OSError, ValueError) as exc:
                self.status = "failed"
                self.project.update(status="failed", error=str(exc))
                self.project.pop("capture_pid", None)
                self._persist_best_effort()
                raise CaptureError(
                    f"Recording stopped, but its media could not be read. Raw capture is preserved at {source}. {exc}"
                ) from exc
            finally:
                self._cleanup()

    def _end_process(self) -> None:
        for sig, timeout in (
            (signal.SIGINT, 4),
            (signal.SIGTERM, 2),
            (signal.SIGKILL, 2),
        ):
            if not self.is_running:
                return
            try:
                self.process.send_signal(sig)
                self.process.wait(timeout=timeout)
                return
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                continue
        if self.is_running:
            raise CaptureError(
                f"Recorder process {self.process.pid} did not exit after shutdown signals; its control socket and raw data were preserved."
            )

    def _persist_best_effort(self) -> bool:
        """A full disk must not turn a successful media save into lost cleanup."""
        if not self.project:
            return False
        try:
            projects.save_project(self.project)
            return True
        except (OSError, ValueError) as exc:
            self.project["metadata_error"] = str(exc)
            self.project["capture_warning"] = (
                f"The raw recording is preserved, but project metadata could not be saved: {exc}. "
                f"Recording folder: {self.project['path']}"
            )
            return False

    def _log_tail(self) -> str:
        if not self.project:
            return ""
        try:
            with (Path(self.project["path"]) / "capture.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 2500))
                return "\n".join(
                    stream.read().decode("utf-8", errors="replace").splitlines()[-8:]
                )
        except OSError:
            return ""

    def _cleanup(self) -> None:
        self._stop_clicks()
        self._stop_telemetry()
        try:
            if not self.is_running:
                self._close_live_inputs()
        finally:
            if self._log:
                self._log.close()
                self._log = None
            if self._runtime and not self.is_running:
                shutil.rmtree(self._runtime, ignore_errors=True)
                self._runtime = None
                self._ipc = None

    def _close_live_inputs(self) -> None:
        if self._live:
            self._live.close()
            self._live = None

    def _start_clicks(self, options: CaptureOptions) -> None:
        try:
            if options.geometry and options.mode in ("region", "window"):
                bounds = parse_geometry(options.geometry)
            else:
                monitor = next((m for m in get_monitors() if m["name"] == options.monitor), None)
                if not monitor:
                    raise ClickCaptureError("Could not determine the recorded monitor bounds.")
                bounds = (monitor["x"], monitor["y"], monitor["logical_width"], monitor["logical_height"])
            self._clicks = ClickCapture(self.project["path"], bounds,
                                       clock=lambda: self.elapsed,
                                       active=lambda: self.status == "recording" and self.is_running)
            self.project["clicks"] = []
            self.project["click_capture_status"] = self._clicks.start()
        except ClickCaptureError as exc:
            self.project["click_capture_status"] = str(exc)
            self._clicks = None

    def _stop_clicks(self) -> None:
        if self._clicks:
            capture, self._clicks = self._clicks, None
            events = capture.stop()
            if self.project:
                self.project["clicks"] = events
                self.project["click_capture_status"] = capture.status

    def _start_telemetry(self, options: CaptureOptions) -> None:
        """Sample positions only via the compositor, with no keyboard/input hooks."""
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        hypr_socket = Path(runtime) / "hypr" / instance / ".socket.sock"
        if not instance or not hypr_socket.exists():
            return
        if options.geometry and options.mode in ("region", "window"):
            self._capture_bounds = parse_geometry(options.geometry)
        else:
            monitor = next(
                (m for m in get_monitors() if m["name"] == options.monitor), None
            )
            if not monitor:
                return
            self._capture_bounds = (
                monitor["x"],
                monitor["y"],
                monitor["logical_width"],
                monitor["logical_height"],
            )
        self._cursor_samples = []
        # Each worker owns its stop event, path, samples and bounds. A slow final
        # disk flush can outlive join's timeout without touching the next take.
        self._telemetry_stop = threading.Event()
        self._telemetry_thread = threading.Thread(
            target=self._sample_cursor,
            args=(
                hypr_socket,
                self._telemetry_stop,
                self._cursor_samples,
                self._capture_bounds,
                Path(self.project["path"]),
                self.process,
                self._started_at,
            ),
            daemon=True,
            name="lumen-cursor",
        )
        self._telemetry_thread.start()

    def _sample_cursor(
        self,
        hypr_socket: Path,
        stop: threading.Event,
        samples: list,
        bounds: tuple,
        folder: Path,
        process: subprocess.Popen,
        started_at: float,
    ) -> None:
        x0, y0, width, height = bounds
        last_flush = time.monotonic()
        consecutive_errors = 0
        while not stop.is_set():
            if process.poll() is not None:
                break
            if self.status == "recording":
                try:
                    with socket.socket(
                        socket.AF_UNIX, socket.SOCK_STREAM
                    ) as connection:
                        connection.settimeout(0.3)
                        connection.connect(str(hypr_socket))
                        connection.sendall(b"j/cursorpos")
                        # cursorpos is a tiny JSON object (<100 bytes).
                        response = connection.recv(4096)
                    position = json.loads(response)
                    x = (position["x"] - x0) / width
                    y = (position["y"] - y0) / height
                    if not stop.is_set() and self.status == "recording":
                        samples.append(
                            {
                                "t": round(
                                    max(
                                        0,
                                        time.monotonic()
                                        - started_at
                                        - self._pause_duration,
                                    ),
                                    4,
                                ),
                                "x": round(max(0, min(1, x)), 5),
                                "y": round(max(0, min(1, y)), 5),
                                "inside": 0 <= x <= 1 and 0 <= y <= 1,
                            }
                        )
                    consecutive_errors = 0
                except (OSError, ValueError, KeyError, TypeError):
                    consecutive_errors += 1
                    if consecutive_errors >= 5:
                        break
                if time.monotonic() - last_flush > 10:
                    self._save_cursor(folder, bounds, samples)
                    last_flush = time.monotonic()
            stop.wait(0.05)
        self._save_cursor(folder, bounds, samples)

    @staticmethod
    def _save_cursor(folder: Path, bounds: tuple, samples: list) -> None:
        if not samples:
            return
        path = folder / "cursor.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "coordinate_system": "normalized",
                        "logical_bounds": bounds,
                        "samples": samples,
                    },
                    separators=(",", ":"),
                )
            )
            temporary.replace(path)
        except OSError:
            pass  # Telemetry is optional and must never break the screen recording.

    def _stop_telemetry(self) -> None:
        self._telemetry_stop.set()
        if (
            self._telemetry_thread
            and self._telemetry_thread is not threading.current_thread()
        ):
            self._telemetry_thread.join(timeout=1)
        self._telemetry_thread = None
