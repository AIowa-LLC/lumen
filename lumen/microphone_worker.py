"""Private PulseAudio loopback owner, run in a disposable guardian process.

GSR always records our silent monitor. A separately owned loopback exists only
while the microphone is enabled. Its endpoints cannot move to fallback devices.
The guardian outlives application shutdown long enough to close that loopback.
It never changes global defaults, device mute/volume, or another app's streams.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import sys
import time
import uuid


class AudioError(RuntimeError):
    pass


def _run(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["pactl", *arguments], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AudioError(f"Cannot control the microphone: {exc}") from exc
    if result.returncode:
        raise AudioError((result.stderr or result.stdout).strip()[:1000]
                         or "The audio server rejected this operation.")
    return result.stdout.strip()


def _pid_identity(pid: int) -> str:
    try:
        # Fields after the final ')' begin with field 3; starttime is field 22.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError) as exc:
        raise AudioError("The recording process is no longer available.") from exc


class AudioSession:
    """Single-threaded module ownership and acknowledged routing operations."""

    def __init__(self, mic_source: str | None = None, enabled: bool = False):
        self.token = uuid.uuid4().hex
        self.sink_name = "lumen_mic_" + self.token
        self.source_name = self.sink_name + ".monitor"
        self.tag = "LumenMicrophone-" + self.token
        self.mic_source = mic_source
        self.initial_enabled = bool(enabled)
        self.sink_module: int | None = None
        self.loop_module: int | None = None
        self.source_index: int | None = None
        self.sink_index: int | None = None
        self.selected_source: str | None = None
        self.selected_index: int | None = None
        self.bound_pid: int | None = None
        self.bound_identity: str | None = None
        self.bound_stream: int | None = None
        self.enabled = False
        self.error: str | None = None
        self.closed = False

    def _list(self, kind: str) -> list[dict]:
        try:
            result = json.loads(_run("-f", "json", "list", kind))
        except (ValueError, TypeError) as exc:
            raise AudioError("The audio server returned invalid device information.") from exc
        if not isinstance(result, list) or not all(isinstance(x, dict) for x in result):
            raise AudioError("The audio server returned invalid device information.")
        return result

    def _modules(self, kind: str) -> list[int]:
        # Some pactl versions omit module indices from JSON. Short output keeps
        # numeric IDs and our deliberately single-line argument strings intact.
        found = []
        for line in _run("list", "short", "modules").splitlines():
            fields = line.split("\t")
            if len(fields) < 3 or not fields[0].isdigit() or fields[1] != kind:
                continue
            try:
                args = dict(part.split("=", 1) for part in shlex.split(fields[2]) if "=" in part)
            except ValueError:
                continue
            own = (args.get("sink_name") == self.sink_name if kind == "module-null-sink"
                   else args.get("sink") == self.sink_name
                   and args.get("source_output_properties") == "application.name=" + self.tag)
            if own:
                found.append(int(fields[0]))
        return found

    def _unload(self, kind: str) -> None:
        for module in self._modules(kind):
            _run("unload-module", str(module))
        if self._modules(kind):
            raise AudioError("The microphone module did not close; retry turning it off.")

    def _private_source(self) -> dict:
        matches = [source for source in self._list("sources")
                   if source.get("name") == self.source_name
                   and source.get("owner_module") == self.sink_module]
        if len(matches) != 1:
            raise AudioError("Lumen's private microphone track is unavailable.")
        return matches[0]

    def start(self) -> str:
        if self.sink_module is not None:
            return self.source_name
        if self.closed:
            raise AudioError("This microphone session has already closed.")
        try:
            module = _run(
                "load-module", "module-null-sink", "sink_name=" + self.sink_name,
                "rate=48000", "channels=1", "channel_map=mono",
                'sink_properties="device.description=Lumen-Microphone priority.session=0 '
                'priority.driver=0 node.virtual=true"',
            )
            self.sink_module = int(module)
            source = self._private_source()
            self.source_index = int(source["index"])
            sinks = [s for s in self._list("sinks") if s.get("name") == self.sink_name
                     and s.get("owner_module") == self.sink_module]
            if len(sinks) != 1:
                raise AudioError("Lumen's private microphone sink is unavailable.")
            self.sink_index = int(sinks[0]["index"])
            return self.source_name
        except (AudioError, ValueError, KeyError) as exc:
            self._unload("module-null-sink")
            self.sink_module = None
            raise AudioError(str(exc)) from exc

    def _reader(self) -> dict | None:
        if self.bound_pid is None:
            return None
        matches = [s for s in self._list("source-outputs")
                   if s.get("source") == self.source_index
                   and str(s.get("properties", {}).get("application.process.id")) == str(self.bound_pid)
                   and (self.bound_stream is None or s.get("index") == self.bound_stream)]
        if len(matches) > 1:
            raise AudioError("Cannot identify one private recording stream.")
        return matches[0] if matches else None

    def bind(self, pid: int) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            raise AudioError("Invalid recording process.")
        if self.bound_pid is not None and pid != self.bound_pid:
            raise AudioError("This microphone belongs to another recording.")
        self._private_source()
        self.bound_pid = pid
        self.bound_identity = _pid_identity(pid)
        deadline = time.monotonic() + 3
        while True:
            reader = self._reader()
            if reader:
                self.bound_stream = int(reader["index"])
                break
            if time.monotonic() >= deadline:
                raise AudioError("The recorder did not open its private microphone track.")
            time.sleep(.05)
        if self.initial_enabled:
            self.set_enabled(True)
        self.initial_enabled = False

    def _check_route(self) -> None:
        if self.loop_module not in self._modules("module-loopback"):
            raise AudioError("The microphone feed stopped.")
        source = [s for s in self._list("sources") if s.get("name") == self.selected_source
                  and s.get("index") == self.selected_index]
        if len(source) != 1:
            raise AudioError("The selected microphone disconnected. It was turned off.")
        outputs = [s for s in self._list("source-outputs")
                   if s.get("properties", {}).get("application.name") == self.tag]
        inputs = [s for s in self._list("sink-inputs")
                  if s.get("properties", {}).get("application.name") == self.tag]
        if (len(outputs) != 1 or outputs[0].get("source") != self.selected_index
                or len(inputs) != 1 or inputs[0].get("sink") != self.sink_index):
            raise AudioError("The private microphone route changed. It was turned off.")

    def set_enabled(self, enabled: bool) -> None:
        if not enabled:
            # Keep the state truthful if an unload fails; the caller must see the
            # failure, not an acknowledged off state while a source stays open.
            self._unload("module-loopback")
            self.loop_module = None
            self.enabled = False
            self.selected_source = None
            self.selected_index = None
            self.error = None
            return
        if self.closed or self.sink_module is None or self.bound_pid is None:
            raise AudioError("Start recording before enabling the microphone.")
        if self.enabled:
            self._check_route()
            return
        if _pid_identity(self.bound_pid) != self.bound_identity or self._reader() is None:
            raise AudioError("The recording process is no longer available.")
        self._private_source()
        source_name = self.mic_source or _run("get-default-source")
        # These values become Pulse module arguments (not shell text). Reject
        # delimiters rather than allowing a device name to inject extra options.
        if not isinstance(source_name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", source_name):
            raise AudioError("The selected microphone has an unsupported device name.")
        if source_name == self.source_name:
            raise AudioError("Select a microphone rather than Lumen's silent track.")
        sources = [s for s in self._list("sources") if s.get("name") == source_name]
        if len(sources) != 1:
            raise AudioError("The selected microphone is unavailable. Refresh devices and choose it again.")
        self.selected_source = source_name
        self.selected_index = int(sources[0]["index"])
        try:
            self.loop_module = int(_run(
                "load-module", "module-loopback", "source=" + source_name,
                "sink=" + self.sink_name, "source_dont_move=true", "sink_dont_move=true",
                "latency_msec=25", "rate=48000", "channels=1", "channel_map=mono",
                "source_output_properties=application.name=" + self.tag,
                "sink_input_properties=application.name=" + self.tag,
            ))
            deadline = time.monotonic() + 2
            while True:
                try:
                    self._check_route()
                    break
                except AudioError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(.05)
            self.enabled = True
            self.error = None
        except (AudioError, ValueError, KeyError) as exc:
            # Even if pactl timed out after creating the module, its unguessable
            # sink/tag lets cleanup find it without trusting a stale numeric ID.
            try:
                self.set_enabled(False)
            except AudioError as cleanup:
                self.enabled = True
                self.error = f"{exc}; could not close microphone: {cleanup}"
                raise AudioError(self.error) from exc
            raise AudioError(str(exc)) from exc

    def refresh(self) -> None:
        if not self.enabled:
            return
        try:
            if _pid_identity(self.bound_pid) != self.bound_identity or self._reader() is None:
                raise AudioError("The recording ended; the microphone was turned off.")
            self._check_route()
        except AudioError as exc:
            reason = str(exc)
            try:
                self.set_enabled(False)
            except AudioError as cleanup:
                reason += f" Cannot close microphone: {cleanup}"
            self.error = reason

    def close(self) -> None:
        self.set_enabled(False)
        if self.sink_module is not None:
            # Unloading a source with live readers can make Pulse choose a real
            # microphone as fallback. Keep the silent source until all leave.
            if any(s.get("source") == self.source_index for s in self._list("source-outputs")):
                raise AudioError("Waiting for the recorder to release its private microphone track.")
            self._unload("module-null-sink")
            self.sink_module = None
        self.closed = True

    def state(self) -> dict:
        return {"enabled": self.enabled, "error": self.error,
                "source": self.source_name, "closed": self.closed}


def main() -> int:
    session = AudioSession()
    quitting = False
    closing = False
    cleanup_deadline: float | None = None

    def interrupted(*_args):
        nonlocal quitting
        quitting = True

    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
    buffer = b""
    previous = None

    def emit(message):
        nonlocal quitting
        try:
            sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            quitting = True

    try:
        while not session.closed:
            if quitting or closing:
                if cleanup_deadline is None:
                    cleanup_deadline = time.monotonic() + 12
                try:
                    session.close()
                except AudioError as exc:
                    session.error = str(exc)
                    # Never force-unload a live monitor. The loopback was closed
                    # first; a leftover silent virtual source is safer.
                    if time.monotonic() >= cleanup_deadline and not session.enabled:
                        print(str(exc), file=sys.stderr)
                        return 1
                if session.closed:
                    emit({"state": session.state()})
                    break
                # If the server temporarily refuses to close a live feed, do
                # not abandon it after our normal cleanup deadline. Remain its
                # owner and retry; callers still get a bounded error response.
                time.sleep(1 if time.monotonic() >= cleanup_deadline else .15)
                continue
            for _key, _events in selector.select(timeout=.5):
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    quitting = True
                    break
                buffer += chunk
                if len(buffer) > 65536:
                    raise AudioError("Oversized microphone control request.")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    request = {}
                    try:
                        request = json.loads(line)
                        action = request["action"]
                        if action == "start":
                            if session.sink_module is not None:
                                raise AudioError("The microphone session has already started.")
                            session.mic_source = request.get("source")
                            session.initial_enabled = bool(request.get("enabled", False))
                            result = session.start()
                        elif action == "bind":
                            session.bind(request["pid"])
                            result = None
                        elif action == "set_enabled":
                            session.set_enabled(bool(request["enabled"]))
                            result = None
                        elif action == "close":
                            closing = True
                            session.close()
                            result = None
                        elif action == "status":
                            session.refresh()
                            result = None
                        else:
                            raise AudioError("Unknown microphone operation.")
                        emit({"id": request.get("id"), "result": result, "state": session.state()})
                    except (AudioError, ValueError, KeyError, TypeError) as exc:
                        session.error = str(exc)
                        emit({"id": request.get("id") if isinstance(request, dict) else None,
                              "error": str(exc), "state": session.state()})
                    if session.closed or closing or quitting:
                        break
            session.refresh()
            state = session.state()
            if state != previous:
                emit({"state": state})
                previous = state
    finally:
        selector.close()
        if not session.closed:
            while True:
                try:
                    session.close()
                    break
                except AudioError as exc:
                    print(str(exc), file=sys.stderr)
                    if not session.enabled:
                        break
                    time.sleep(1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
