"""Live microphone toggle with app-local routing and crash cleanup."""
from __future__ import annotations

from concurrent.futures import Future, TimeoutError
import json
from pathlib import Path
import subprocess
import sys
import threading

from ._process import spawn_owned


class MicrophoneError(RuntimeError):
    pass


class LiveMicrophone:
    """Keep one private recording source; open the selected mic only when on.

    Call start before spawning GSR, bind to its PID once it starts, then toggle.
    Call close after the recorder exits. The guardian also closes the live feed
    on parent death or a broken control pipe. No global audio state is modified.
    """

    def __init__(self, mic_source: str | None = None, enabled: bool = False):
        self.mic_source = mic_source
        self.initial_enabled = bool(enabled)
        self.process: subprocess.Popen | None = None
        self.source_name: str | None = None
        self.error: str | None = None
        self._enabled = False
        self._closed = False
        self._sequence = 0
        self._pending: dict[int, Future] = {}
        self._guard = threading.RLock()
        self._operation = threading.RLock()
        self._reader: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self.process is not None and self.process.poll() is None

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                if len(line) > 65536:
                    raise MicrophoneError("Invalid microphone control response.")
                message = json.loads(line)
                state = message.get("state", {})
                with self._guard:
                    self._enabled = bool(state.get("enabled", self._enabled))
                    self.error = state.get("error", self.error)
                    self._closed = bool(state.get("closed", self._closed))
                    pending = self._pending.pop(message.get("id"), None)
                if pending:
                    if message.get("error"):
                        pending.set_exception(MicrophoneError(message["error"]))
                    else:
                        pending.set_result(message.get("result"))
        except (OSError, ValueError, MicrophoneError) as exc:
            self.error = str(exc)
        finally:
            with self._guard:
                pending = list(self._pending.values())
                self._pending.clear()
                self._enabled = False
                if not self._closed and not self.error:
                    self.error = "The microphone controller stopped unexpectedly."
            for request in pending:
                request.set_exception(MicrophoneError(self.error or "Microphone control closed."))

    def _request(self, action: str, *, timeout: float = 12, **values):
        with self._operation:
            if (self.process is None or self.process.poll() is not None
                    or self.process.stdin.closed):
                raise MicrophoneError(self.error or "The microphone controller is unavailable.")
            with self._guard:
                self._sequence += 1
                sequence = self._sequence
                future = Future()
                self._pending[sequence] = future
            try:
                self.process.stdin.write(json.dumps({"id": sequence, "action": action, **values}) + "\n")
                self.process.stdin.flush()
                return future.result(timeout=timeout)
            except (OSError, ValueError, TimeoutError) as exc:
                with self._guard:
                    self._pending.pop(sequence, None)
                raise MicrophoneError(f"Microphone operation could not be confirmed: {exc}") from exc

    def start(self) -> str:
        with self._operation:
            if self.source_name and self.process and self.process.poll() is None:
                return self.source_name
            if self.process is not None:
                raise MicrophoneError("This microphone session has already ended.")
            worker = str(Path(__file__).with_name("microphone_worker.py").resolve())
            self.process = spawn_owned(
                [sys.executable, worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._reader = threading.Thread(target=self._read, name="lumen-microphone", daemon=True)
            self._reader.start()
            try:
                self.source_name = self._request("start", source=self.mic_source, enabled=self.initial_enabled)
                return self.source_name
            except Exception:
                # EOF asks the guardian to clean up even if startup's reply was
                # lost. Never SIGKILL a helper that might own a live loopback.
                self.process.stdin.close()
                raise

    def bind(self, pid: int) -> None:
        self._request("bind", pid=pid)

    def set_enabled(self, enabled: bool) -> None:
        self._request("set_enabled", enabled=bool(enabled))

    def refresh(self) -> bool:
        self._request("status")
        return self.enabled

    def close(self) -> None:
        with self._operation:
            if self.process is None:
                return
            if self.process.poll() is not None:
                self._finish_pipes()
                if not self._closed:
                    raise MicrophoneError(self.error or "Microphone cleanup could not be confirmed.")
                return
            if not self.process.stdin.closed:
                self._request("close")
            try:
                self.process.wait(timeout=4 if self._closed else 15)
            except subprocess.TimeoutExpired as exc:
                raise MicrophoneError("The microphone controller is still cleaning up.") from exc
            self._finish_pipes()
            if not self._closed:
                raise MicrophoneError(self.error or "Microphone cleanup could not be confirmed.")

    def _finish_pipes(self) -> None:
        """Drain the final acknowledgement before checking a terminated worker."""
        if self._reader:
            self._reader.join(timeout=1)
        if self.process.poll() is not None:
            self.process.stdin.close()
            self.process.stdout.close()
