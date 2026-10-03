"""User-started, bounded native replay capture; nothing starts in the background."""

from __future__ import annotations

from dataclasses import asdict, replace
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

from .capture import CaptureError, CaptureOptions, LiveInputs, audio_track_map, build_gsr_command
from ._process import spawn_owned
from . import project as projects
from .system import get_monitors, parse_geometry, probe_media, select_region


def build_replay_command(
    options: CaptureOptions, directory: Path, ipc: Path, seconds: int
) -> list[str]:
    if (
        not isinstance(seconds, int)
        or isinstance(seconds, bool)
        or not 2 <= seconds <= 60
    ):
        raise ValueError("Replay duration must be between 2 and 60 seconds.")
    if options.backend not in ("auto", "gpu-screen-recorder"):
        raise ValueError("Instant replay requires GPU Screen Recorder.")
    command = build_gsr_command(options, directory, ipc)
    # Constant bitrate bounds the compressed RAM ring; quality presets select Mbps.
    bitrate = {"medium": 6000, "high": 10000, "very_high": 18000, "ultra": 30000}[
        options.quality
    ]
    command[command.index("-q") + 1] = str(bitrate)
    command[command.index("-write-first-frame-ts") + 1] = "no"
    command += [
        "-c",
        "mkv",
        "-r",
        str(seconds),
        "-replay-storage",
        "ram",
        "-bm",
        "cbr",
        "-keyint",
        "1",
        "-restart-replay-on-save",
        "no",
        "-df",
        "no",
    ]
    return command


class ReplayBuffer:
    """Blocking methods run on a GUI worker. Saved replays have no cursor telemetry."""

    def __init__(self, root: str | Path | None = None):
        self.root = root
        self.process: subprocess.Popen | None = None
        self.status = "idle"
        self.seconds = 30
        self.options: CaptureOptions | None = None
        self.staging_path: Path | None = None
        self._runtime: Path | None = None
        self._ipc: Path | None = None
        self._log = None
        self._lock = threading.RLock()
        self._preserve_staging = False
        self._import_failed = False
        self._started_at = 0.0
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
        return self._live.mic_enabled if self._live else bool(
            self.options and self.options.audio in ("mic", "both"))

    @property
    def camera_enabled(self) -> bool:
        if not self.is_running:
            return False
        return self._live.camera_enabled if self._live else bool(self.options and self.options.webcam)

    def set_microphone_enabled(self, enabled: bool) -> None:
        self._set_input("microphone", enabled)

    def set_webcam_enabled(self, enabled: bool, device: str | None = None) -> None:
        self._set_input("camera", enabled, device)

    def _set_input(self, kind: str, enabled: bool, device: str | None = None) -> None:
        with self._lock:
            if not isinstance(enabled, bool):
                raise CaptureError("Input state must be enabled or disabled.")
            if not self.live_inputs_available:
                raise CaptureError("Live controls are unavailable for this replay buffer.")
            try:
                if kind == "microphone":
                    self._live.set_microphone_enabled(enabled)
                else:
                    self._live.set_webcam_enabled(enabled, device)
            except Exception as exc:
                raise CaptureError(str(exc)) from exc

    @property
    def elapsed(self) -> float:
        return (
            max(0, time.monotonic() - self._started_at)
            if self._started_at and self.is_running
            else 0
        )

    @property
    def estimated_buffer_mib(self) -> float:
        """Compressed video budget, excluding encoder, GPU, and audio overhead."""
        quality = self.options.quality if self.options else "very_high"
        bitrate = {"medium": 6000, "high": 10000, "very_high": 18000, "ultra": 30000}[
            quality
        ]
        return bitrate * 1000 * self.seconds / 8 / (1024 * 1024)

    def start(self, options: CaptureOptions, seconds: int = 30) -> None:
        with self._lock:
            if self.is_running:
                raise CaptureError("A replay buffer is already running.")
            if self.process and self.status == "buffering":
                self._preserve_staging = True
            self._cleanup()
            self.process = None
            self.staging_path = None
            self._preserve_staging = False
            self._import_failed = False
            self.status = "starting"
            try:
                options = replace(options, cursor_telemetry=False, record_clicks=False)
                options.validate()
                if not shutil.which("gpu-screen-recorder"):
                    raise CaptureError(
                        "Install GPU Screen Recorder to use instant replay."
                    )
                if options.mode == "region" and not options.geometry:
                    options.geometry = select_region()
                    if not options.geometry:
                        raise CaptureError("Region selection cancelled.")
                monitors = get_monitors()
                if options.mode == "monitor" and not options.monitor:
                    if not monitors:
                        raise CaptureError(
                            "No monitors found. Launch Lumen in your Hyprland session."
                        )
                    options.monitor = next(
                        (m["name"] for m in monitors if m.get("focused")),
                        monitors[0]["name"],
                    )
                if options.mode in ("region", "window"):
                    if not options.geometry:
                        raise CaptureError("Select a replay capture area first.")
                    x, y, width, height = parse_geometry(options.geometry)
                    if monitors and not any(
                        x >= m["x"]
                        and y >= m["y"]
                        and x + width <= m["x"] + m["logical_width"]
                        and y + height <= m["y"] + m["logical_height"]
                        for m in monitors
                    ):
                        raise CaptureError(
                            "Choose a replay region entirely inside one monitor."
                        )
                # Validate before creating staging directories.
                build_replay_command(options, Path("unused"), Path("unused"), seconds)
                self.options, self.seconds = options, seconds
                library = projects.library_root(self.root)
                library.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.staging_path = Path(
                    tempfile.mkdtemp(prefix=".replay-", dir=library)
                )
                runtime_base = os.environ.get("XDG_RUNTIME_DIR")
                try:
                    self._runtime = Path(
                        tempfile.mkdtemp(prefix="lumen-replay-", dir=runtime_base)
                    )
                except OSError:
                    self._runtime = Path(tempfile.mkdtemp(prefix="lumen-replay-"))
                self._ipc = self._runtime / "control.sock"
                self._log = (self.staging_path / "capture.log").open("ab", buffering=0)
                backend_options = options
                if options.live_inputs:
                    self._live = LiveInputs(options)
                    backend_options = self._live.prepare()
                command = build_replay_command(
                    backend_options, self.staging_path, self._ipc, seconds
                )
                self.process = spawn_owned(
                    command,
                    stdout=self._log,
                    stderr=self._log,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
                self._started_at = time.monotonic()
                self._wait_started()
                if self._live:
                    self._live.bind(self.process.pid)
                self.status = "buffering"
            except Exception as exc:
                self.status = "failed"
                self._preserve_staging = self.staging_path is not None
                try:
                    self._end_process()
                finally:
                    self._cleanup()
                detail = (
                    f"\nReplay diagnostics: {self.staging_path / 'capture.log'}"
                    if self.staging_path
                    else ""
                )
                raise CaptureError(f"Replay could not start: {exc}{detail}") from exc

    def _wait_started(self) -> None:
        # Same liveness contract as gsr-cli status: connect to the owned IPC socket.
        # The backend queues commands sent during initialization until capture starts.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.is_running:
                raise CaptureError(
                    f"GPU Screen Recorder exited with code {self.process.returncode}."
                )
            if self._ipc.exists():
                try:
                    with socket.socket(
                        socket.AF_UNIX, socket.SOCK_STREAM
                    ) as connection:
                        connection.settimeout(0.5)
                        connection.connect(str(self._ipc))
                    return
                except OSError:
                    pass
            time.sleep(0.05)
        raise CaptureError("Replay control did not become available within 10 seconds.")

    def _request(self, name: str, data=None, timeout: float = 20):
        if not self._ipc:
            raise CaptureError("Replay control is unavailable.")
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
                    if not chunk or len(response) + len(chunk) > 65536:
                        raise CaptureError(
                            "Replay recorder closed or returned an invalid control response."
                        )
                    response += chunk
                result = json.loads(response.split(b"\n", 1)[0])
        except (OSError, ValueError) as exc:
            raise CaptureError(f"Cannot control replay: {exc}") from exc
        if not isinstance(result, dict) or result.get("id") != 1:
            raise CaptureError("Replay recorder returned an invalid control response.")
        if result.get("result") != "ok":
            raise CaptureError(str(result.get("data", "Replay command failed.")))
        return result.get("data")

    def save(self) -> dict:
        """Save the recent buffer to a normal project while capture continues."""
        with self._lock:
            if not self.is_running:
                raise CaptureError("The replay buffer is not running.")
            if self._import_failed:
                raise CaptureError(
                    f"A saved replay needs recovery in {self.staging_path}. Stop and restart the buffer before saving another clip."
                )
            try:
                saved = self._request(
                    "save-replay", {"seconds": self.seconds, "restart-replay": False}
                )
                if not isinstance(saved, str):
                    raise CaptureError(
                        "Replay recorder did not return the saved clip path."
                    )
                supplied_path = Path(saved)
                source = supplied_path.resolve()
                if (
                    supplied_path.is_symlink()
                    or source.parent != self.staging_path.resolve()
                    or not source.is_file()
                ):
                    raise CaptureError(
                        "Replay recorder returned a path outside its private staging folder."
                    )
                metadata = probe_media(source)
                project = (
                    projects.create_project(root=self.root)
                    if self.root is not None
                    else projects.create_project()
                )
                destination = Path(project["path"]) / "source.mkv"
                # Staging and the library share a filesystem: this move is atomic.
                source.replace(destination)
                project.update(metadata)
                project.update(
                    status="ready",
                    source="source.mkv",
                    source_path=str(destination),
                    name="Replay · " + project["name"].removeprefix("Recording · "),
                    fps=self.options.fps,
                    capture=asdict(self.options),
                    backend="gpu-screen-recorder",
                    replay_seconds=self.seconds,
                    cursor_telemetry=False,
                    replay_bitrate_kbps={
                        "medium": 6000,
                        "high": 10000,
                        "very_high": 18000,
                        "ultra": 30000,
                    }[self.options.quality],
                    audio_tracks=audio_track_map(self.options),
                )
                try:
                    shutil.copyfile(
                        self.staging_path / "capture.log",
                        Path(project["path"]) / "capture.log",
                    )
                except OSError:
                    pass
                try:
                    projects.save_project(project)
                except OSError as exc:
                    project["metadata_error"] = str(exc)
                    project["capture_warning"] = (
                        f"Replay video was saved at {destination}, but project metadata could not be saved: {exc}"
                    )
                return project
            except Exception as exc:
                # Retain any acknowledged file, and prevent another native save from
                # reusing its timestamp-based filename while recovery is pending.
                self._preserve_staging = True
                self._import_failed = True
                raise CaptureError(
                    f"Replay could not be imported: {exc}\nSaved data and diagnostics are preserved in {self.staging_path}."
                ) from exc

    def stop(self) -> None:
        """Discard unsaved RAM frames; previously saved projects remain intact."""
        with self._lock:
            try:
                if self.is_running:
                    try:
                        self._request("stop", timeout=8)
                        self.process.wait(timeout=8)
                    except (CaptureError, subprocess.TimeoutExpired):
                        self._end_process()
                elif self.process and self.process.returncode not in (None, 0):
                    self._preserve_staging = True
                self.status = "idle"
            except Exception:
                self.status = "failed"
                self._preserve_staging = True
                raise
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
                f"Replay process {self.process.pid} did not exit; its control socket was preserved."
            )

    def _cleanup(self) -> None:
        try:
            if self._live and not self.is_running:
                self._live.close()
                self._live = None
        finally:
            if self._log:
                self._log.close()
                self._log = None
            if not self.is_running:
                if self._runtime:
                    shutil.rmtree(self._runtime, ignore_errors=True)
                    self._runtime = None
                    self._ipc = None
                if self.staging_path and not self._preserve_staging:
                    # A file may have been saved just before a control timeout. Never
                    # delete staged media merely because its acknowledgment was lost.
                    if not any(self.staging_path.glob("*.mkv")):
                        shutil.rmtree(self.staging_path, ignore_errors=True)
