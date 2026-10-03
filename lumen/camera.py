"""An owned, silent camera bubble composited by Hyprland into the captured area.

The camera is opened only after its window is positioned. Turning it off exits
the player, releasing V4L2 rather than merely hiding a still-running camera.
Window rules exist only in compositor memory and expire after missed heartbeats.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from ._process import spawn_owned
from .system import get_monitors


class CameraError(RuntimeError):
    pass


def bubble_geometry(bounds: tuple) -> tuple[int, int, int, int]:
    if len(bounds) != 4 or not all(math.isfinite(float(v)) for v in bounds):
        raise CameraError("Cannot determine the recorded area for the camera.")
    x, y, width, height = map(round, bounds)
    if width < 160 or height < 100:
        raise CameraError("The recorded area is too small for a camera bubble.")
    margin = max(6, min(24, round(min(width, height) * .035)))
    w = min(320, max(128, round(width * .25)), width - margin * 2)
    h = round(w * 9 / 16)
    if h > height - margin * 2:
        h = height - margin * 2
        w = round(h * 16 / 9)
    return x + width - w - margin, y + height - h - margin, w, h


def camera_mode(device: str, inventory: str) -> dict:
    """Choose an advertised inexpensive mode; inventory never starts streaming."""
    modes = []
    for line in inventory.splitlines():
        match = re.fullmatch(r"(/dev/video\d+)\|(\d+)x(\d+)@([\d.]+)hz\|(mjpeg|yuyv)", line.strip())
        if not match or match[1] != device:
            continue
        width, height, fps = int(match[2]), int(match[3]), float(match[4])
        if width > 0 and height > 0 and fps > 0:
            modes.append((width, height, fps, match[5]))
    if not modes:
        raise CameraError("No supported webcam mode was found. Reconnect or choose another camera.")
    width, height, fps, pixel_format = min(modes, key=lambda m: (
        abs(m[2] - 30), abs(m[0] * m[1] - 640 * 360),
        0 if m[3] == "mjpeg" else 1))
    return {"video_size": f"{width}x{height}", "framerate": f"{fps:g}",
            "input_format": "mjpeg" if pixel_format == "mjpeg" else "yuyv422"}


class CameraBubble:
    def __init__(self, bounds: tuple, device: str | None = None):
        self.bounds = bounds
        self.device = device
        self.token = uuid.uuid4().hex
        self.app_id = "io.github.lumen.Camera." + self.token
        self.title = "Lumen camera " + self.token
        self.process: subprocess.Popen | None = None
        self._runtime: Path | None = None
        self._ipc: Path | None = None
        self._log = None
        self._enabled = False
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._rule_installed = False
        self.error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self.process is not None and self.process.poll() is None

    def _hypr(self, command: str) -> str:
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        if not runtime or not instance:
            raise CameraError("Live camera requires a Hyprland session.")
        path = Path(runtime) / "hypr" / instance / ".socket.sock"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(str(path))
                connection.sendall(command.encode())
                response = b""
                while chunk := connection.recv(4096):
                    response += chunk
                    if len(response) > 2 * 1024 * 1024:
                        raise CameraError("Compositor returned an oversized response.")
            return response.decode(errors="replace").strip()
        except OSError as exc:
            raise CameraError(f"Cannot position camera: {exc}") from exc

    def _install_rule(self) -> None:
        x, y, width, height = bubble_geometry(self.bounds)
        monitor = next((m for m in get_monitors()
                        if x >= m["x"] and y >= m["y"]
                        and x + width <= m["x"] + m["logical_width"]
                        and y + height <= m["y"] + m["logical_height"]), None)
        if not monitor:
            raise CameraError("The camera bubble must fit inside one active monitor.")
        key = "_lumen_camera_" + self.token
        # Named rules are replaced on another enable; disabled rules cannot match
        # another application. No user configuration or rule is changed.
        lua = f'''
local key = "{key}"
if _G[key] then _G[key].cleanup() end
local S = {{alive = true, age = 0}}
_G[key] = S
S.cleanup = function()
  S.alive = false
  if S.rule then S.rule:set_enabled(false) end
  _G[key] = nil
end
S.rule = hl.window_rule({{
  name = "lumen-camera-{self.token}",
  match = {{initial_class = {json.dumps('^' + re.escape(self.app_id) + '$')}}},
  float = true, pin = true, no_initial_focus = true, no_focus = true,
  no_anim = true, no_blur = true, no_shadow = true, border_size = 0,
  rounding = 16, opacity = "1.0 override", decorate = false,
  monitor = {json.dumps(monitor['name'])},
  move = {{{x - monitor['x']}, {y - monitor['y']}}},
  size = {{{width}, {height}}}, min_size = {{{width}, {height}}},
  max_size = {{{width}, {height}}}, suppress_event = "maximize fullscreen activate activatefocus"
}})
S.arm = function()
  hl.timer(function()
    if not S.alive then return end
    S.age = S.age + 1
    if S.age >= 5 then S.cleanup() else S.arm() end
  end, {{timeout = 1000, type = "oneshot"}})
end
S.arm()
return "ready"
'''
        self._rule_installed = True
        if self._hypr("/repl " + lua) != "ready":
            raise CameraError("The compositor could not prepare the camera bubble.")

    def _remove_rule(self) -> None:
        if self._rule_installed:
            try:
                key = "_lumen_camera_" + self.token
                self._hypr(f'/repl if _G["{key}"] then _G["{key}"].cleanup() end')
            except CameraError:
                pass  # The compositor-side lease also disables it after five seconds.
            self._rule_installed = False

    def _request(self, command: list, timeout: float = 3):
        if not self._ipc:
            raise CameraError("Camera control is unavailable.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self._ipc))
                connection.sendall((json.dumps({"command": command, "request_id": 1}) + "\n").encode())
                buffer = b""
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        raise CameraError("Camera closed its control connection.")
                    buffer += chunk
                    if len(buffer) > 65536:
                        raise CameraError("Camera returned an invalid control response.")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        reply = json.loads(line)
                        if reply.get("request_id") != 1:
                            continue
                        if reply.get("error") != "success":
                            raise CameraError(str(reply.get("error", "Camera command failed.")))
                        return reply.get("data")
        except (OSError, ValueError) as exc:
            raise CameraError(f"Cannot control camera: {exc}") from exc

    def _source(self, device: str) -> tuple[str, dict]:
        try:
            result = subprocess.run(["gpu-screen-recorder", "--list-v4l2-devices"],
                                    capture_output=True, text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CameraError(f"Cannot inspect webcam modes: {exc}") from exc
        mode = camera_mode(device, result.stdout)
        return "av://v4l2:" + device, {
            "demuxer-lavf-o": ",".join(f"{key}={value}" for key, value in mode.items()),
            "vf": "hflip",
        }

    def start(self, device: str | None = None) -> None:
        with self._lock:
            if self.enabled:
                return
            self.stop()
            self.device = device or self.device
            if not self.device or not re.fullmatch(r"/dev/video\d+", self.device):
                raise CameraError("Select a webcam before turning it on.")
            if not shutil.which("mpv"):
                raise CameraError("Install mpv to use the live camera bubble.")
            self.error = None
            try:
                self._install_rule()
                runtime = os.environ.get("XDG_RUNTIME_DIR")
                self._runtime = Path(tempfile.mkdtemp(prefix="lumen-camera-", dir=runtime))
                self._ipc = self._runtime / "control.sock"
                self._log = (self._runtime / "camera.log").open("ab", buffering=0)
                width, height = bubble_geometry(self.bounds)[2:]
                self.process = spawn_owned([
                    "mpv", "--no-config", "--load-scripts=no", "--idle=yes",
                    "--force-window=immediate", "--no-audio", "--osc=no",
                    "--osd-level=0", "--border=no", "--title-bar=no",
                    "--focus-on=never", "--input-default-bindings=no",
                    "--input-builtin-bindings=no", "--input-vo-keyboard=no",
                    "--input-terminal=no", "--input-cursor-passthrough=yes",
                    "--cursor-autohide=always", "--keepaspect-window=no",
                    "--cache=no", "--demuxer-readahead-secs=0",
                    "--vo=gpu", "--gpu-context=wayland",
                    f"--autofit={width}x{height}", f"--title={self.title}",
                    f"--wayland-app-id={self.app_id}", f"--input-ipc-server={self._ipc}",
                ], stdout=self._log, stderr=self._log, stdin=subprocess.DEVNULL,
                    start_new_session=True)
                self._stop_event = threading.Event()
                self._thread = threading.Thread(target=self._heartbeat,
                                                name="lumen-camera", daemon=True)
                self._thread.start()
                self._wait_window()
                source, options = self._source(self.device)
                self._request(["loadfile", source, "replace", -1, options])
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if self.process.poll() is not None:
                        break
                    try:
                        if self._request(["get_property", "video-params"]):
                            self._enabled = True
                            return
                    except CameraError:
                        pass
                    time.sleep(.05)
                raise CameraError("The camera produced no video. It may be busy or disconnected.")
            except Exception as exc:
                self.error = str(exc)
                self.stop()
                raise CameraError(self.error) from exc

    def _wait_window(self) -> None:
        deadline = time.monotonic() + 8
        expected = bubble_geometry(self.bounds)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise CameraError("Camera player exited before its window was ready.")
            clients = json.loads(self._hypr("j/clients"))
            own = next((c for c in clients if c.get("pid") == self.process.pid
                        and c.get("class") == self.app_id and c.get("title") == self.title), None)
            if own and self._ipc.exists():
                actual = (*own["at"], *own["size"])
                if own.get("floating") and own.get("pinned") and all(
                        abs(a - b) <= 2 for a, b in zip(actual, expected)):
                    return
            time.sleep(.05)
        raise CameraError("The camera could not be positioned safely inside the recording area.")

    def _heartbeat(self) -> None:
        event = self._stop_event
        process = self.process
        while not event.wait(1):
            if not process or process.poll() is not None:
                if event.is_set():
                    return
                self._enabled = False
                self._remove_rule()
                return
            key = "_lumen_camera_" + self.token
            try:
                result = self._hypr(f'/repl local S=_G["{key}"]; if S then S.age=0; return "alive" end')
                if event.is_set():
                    return
                if result != "alive":
                    raise CameraError("Camera positioning rules were reloaded.")
                if self._enabled and (self._request(["get_property", "idle-active"])
                                      or self._request(["get_property", "eof-reached"])):
                    raise CameraError("The camera disconnected or stopped producing video.")
            except CameraError as exc:
                if event.is_set():
                    return
                self.error = str(exc)
                # A lost compositor lease must never leave a live camera elsewhere.
                self._enabled = False
                try:
                    process.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self._remove_rule()
                return

    def stop(self) -> None:
        with self._lock:
            self._stop_event.set()
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=2.5)
            self._thread = None
            if self.process and self.process.poll() is None:
                try:
                    self._request(["quit"], timeout=1)
                except CameraError:
                    pass  # mpv may close IPC before sending the quit acknowledgment.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        self.process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        self.process.send_signal(sig)
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    raise CameraError("Camera process did not close; its control socket was preserved.") from exc
            self._enabled = False
            self._remove_rule()
            if self._log:
                self._log.close()
                self._log = None
            if self._runtime:
                shutil.rmtree(self._runtime, ignore_errors=True)
                self._runtime = None
                self._ipc = None
