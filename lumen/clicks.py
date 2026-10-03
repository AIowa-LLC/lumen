"""Scoped Hyprland click metadata, without input-device or keyboard access.

Plain mouse-button binds are non-consuming and last only for a recording. Native
Lua callbacks emit button/position through the compositor's event socket. A lease
expires after five missed heartbeats, including if Lumen is killed. Reloading
Hyprland also removes the Lua state. Existing bindings are never unbound.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Callable
import uuid


BUTTONS = {272: "left", 273: "right", 274: "middle"}
MAX_CAPTURE_CLICKS = 10_000  # Matches the editable layer renderer's event limit.


class ClickCaptureError(RuntimeError):
    pass


def available_buttons(binds: list[dict]) -> list[int]:
    """Skip any button with an existing plain or explicitly modifier-agnostic bind."""
    blocked = {b.get("key") for b in binds
               if b.get("modmask", 0) == 0 or b.get("ignore_mods", False)}
    return [code for code in BUTTONS if f"mouse:{code}" not in blocked]


def click_event(t: float, x: float, y: float, button: str) -> dict:
    if button not in BUTTONS.values() or not all(math.isfinite(v) for v in (t, x, y)):
        raise ValueError("Invalid click event.")
    if t < 0 or not (0 <= x <= 1 and 0 <= y <= 1):
        raise ValueError("Click is outside the recorded timeline or area.")
    return {"id": uuid.uuid4().hex, "t": round(t, 4), "x": round(x, 6), "y": round(y, 6),
            "button": button, "duration": .6, "size": .04, "color": "#78c8ff", "enabled": True}


def install_lua(token: str, buttons: list[int]) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", token) or not buttons or any(code not in BUTTONS for code in buttons):
        raise ValueError("Invalid click capture token or buttons.")
    key = "_lumen_click_" + token
    register = []
    for code in buttons:
        button = BUTTONS[code]
        register.append(f'''
S.callbacks["{button}"] = function()
  if not S.alive then return end
  local p = hl.get_cursor_pos()
  if p then hl.dispatch(hl.dsp.event(string.format("lumen_click {token} {button} %.6f %.6f", p.x, p.y))) end
end
table.insert(S.binds, hl.bind("mouse:{code}", S.callbacks["{button}"], {{
  non_consuming = true, description = "Lumen click capture {token} {button}"
}}))''')
    # One-shot timers remove their own native references after firing. On cleanup,
    # the pending timer simply observes alive=false and returns once, then disappears.
    return f'''
local key = "{key}"
if _G[key] then error("Click capture token already exists") end
local S = {{ binds = {{}}, callbacks = {{}}, age = 0, alive = true }}
_G[key] = S
S.cleanup = function()
  S.alive = false
  for _, b in ipairs(S.binds) do pcall(function() b:remove() end) end
  S.binds = {{}}
  S.callbacks = {{}}
  _G[key] = nil
end
S.arm = function()
  hl.timer(function()
    if not S.alive then return end
    S.age = S.age + 1
    if S.age >= 5 then S.cleanup() else S.arm() end
  end, {{ timeout = 1000, type = "oneshot" }})
end
local ok, err = pcall(function()
{''.join(register)}
S.arm()
end)
if not ok then S.cleanup(); error(err) end
return "active"
'''


def read_click_sidecar(folder: str | Path) -> dict:
    path = Path(folder) / "clicks.json"
    try:
        if path.stat().st_size > 32 * 1024 * 1024:
            return {}
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get("clicks"), list):
            return data
    except (OSError, ValueError):
        pass
    return {}


class ClickCapture:
    def __init__(self, folder: str | Path, bounds: tuple[float, float, float, float],
                 clock: Callable[[], float], active: Callable[[], bool]):
        self.folder = Path(folder)
        self.bounds = bounds
        self.clock = clock
        self.active = active
        self.token = uuid.uuid4().hex
        self.events: list[dict] = []
        self.status = "Click capture is not active."
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._events_socket: socket.socket | None = None
        self._command_path: Path | None = None
        self._lock = threading.Lock()
        self._installed = False

    def _query(self, command: str) -> str:
        if self._command_path is None:
            raise ClickCaptureError("Hyprland command socket is unavailable.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1.5)
                connection.connect(str(self._command_path))
                connection.sendall(command.encode())
                response = b""
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    response += chunk
                    if len(response) > 2 * 1024 * 1024:
                        raise ClickCaptureError("Hyprland returned an oversized response.")
            return response.decode("utf-8", errors="replace").strip()
        except OSError as exc:
            raise ClickCaptureError(str(exc)) from exc

    def start(self) -> str:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        instance = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not runtime or not instance:
            raise ClickCaptureError("Click capture requires a Hyprland session.")
        hypr = Path(runtime) / "hypr" / instance
        self._command_path = hypr / ".socket.sock"
        try:
            capabilities = self._query("/repl return type(hl.get_cursor_pos),type(hl.dsp.event),type(hl.timer)")
            if capabilities.split() != ["function", "function", "function"]:
                raise ClickCaptureError("This compositor lacks the native click capture API.")
            buttons = available_buttons(json.loads(self._query("j/binds")))
            if not buttons:
                raise ClickCaptureError("Existing mouse shortcuts cover all three buttons; no shortcuts were changed.")
            self._events_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._events_socket.settimeout(.2)
            self._events_socket.connect(str(hypr / ".socket2.sock"))
            self._installed = True  # A failed eval can leave a partially created scope.
            response = self._query("/repl " + install_lua(self.token, buttons))
            if response != "active":
                raise ClickCaptureError(response or "Could not register native click capture.")
            installed = [b for b in json.loads(self._query("j/binds"))
                         if self.token in b.get("description", "")]
            if len(installed) != len(buttons) or not all(b.get("non_consuming") and b.get("modmask") == 0 for b in installed):
                raise ClickCaptureError("Non-consuming mouse bindings could not be verified.")
            names = ", ".join(BUTTONS[code] for code in buttons)
            self.status = f"Recording {names} clicks without modifier keys."
            if len(buttons) < 3:
                self.status += " Buttons with existing shortcuts were skipped."
            self._thread = threading.Thread(target=self._listen, name="lumen-clicks", daemon=True)
            self._thread.start()
            return self.status
        except Exception as exc:
            self.status = f"Click capture unavailable: {exc}"
            self.stop()
            raise ClickCaptureError(self.status) from exc

    def _accept_line(self, line: str) -> None:
        prefix = f"custom>>lumen_click {self.token} "
        if not line.startswith(prefix) or not self.active():
            return
        parts = line[len(prefix):].split()
        if len(parts) != 3:
            return
        try:
            button, raw_x, raw_y = parts
            x0, y0, width, height = self.bounds
            x, y = (float(raw_x) - x0) / width, (float(raw_y) - y0) / height
            event = click_event(self.clock(), x, y, button)
        except (ValueError, TypeError, ZeroDivisionError):
            return
        with self._lock:
            if len(self.events) < MAX_CAPTURE_CLICKS:
                self.events.append(event)
                if len(self.events) == MAX_CAPTURE_CLICKS:
                    self.status = (
                        f"The {MAX_CAPTURE_CLICKS:,}-click limit was reached. "
                        "Captured clicks were retained; later clicks were not recorded."
                    )

    def _listen(self) -> None:
        connection = self._events_socket
        pending = b""
        heartbeat_at = saved_at = time.monotonic()
        try:
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(16384)
                    if not chunk:
                        if not self._stop.is_set():
                            self.status = "Click capture ended when the compositor connection closed."
                        break
                    pending += chunk
                    if len(pending) > 65536:
                        pending = b""  # Discard unrelated oversized compositor events.
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        self._accept_line(line.decode("utf-8", errors="replace"))
                except socket.timeout:
                    pass
                now = time.monotonic()
                if now - heartbeat_at >= 1:
                    response = self._query(f'/repl local s=_G["_lumen_click_{self.token}"]; if s then s.age=0; return "active" else return "missing" end')
                    if response != "active":
                        self.status = "Click capture ended after a compositor reload; recorded clicks were retained."
                        break
                    heartbeat_at = now
                if now - saved_at >= 2:
                    self._save()
                    saved_at = now
        except (OSError, ClickCaptureError) as exc:
            if not self._stop.is_set():
                self.status = f"Click capture disconnected: {exc}"
        finally:
            self._save()

    def _save(self) -> None:
        with self._lock:
            data = {"version": 1, "click_capture_status": self.status, "clicks": list(self.events)}
        temporary = self.folder / "clicks.json.tmp"
        try:
            temporary.write_text(json.dumps(data, separators=(",", ":")))
            temporary.replace(self.folder / "clicks.json")
        except OSError:
            pass  # Optional metadata must never interrupt media capture.

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._installed:
            try:
                self._query(f'/eval local s=_G["_lumen_click_{self.token}"]; if s then s.cleanup() end')
            except ClickCaptureError:
                # Lease expiry still removes only our bindings, within five seconds.
                pass
            self._installed = False
        if self._events_socket:
            try:
                self._events_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._events_socket.close()
            self._events_socket = None
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self._save()
        with self._lock:
            return list(self.events)
