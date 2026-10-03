"""Spawn recorders from one persistent Linux parent thread.

PR_SET_PDEATHSIG follows the *creating thread*. Spawning directly from a short
GUI worker would stop capture as soon as that worker returned. A daemon thread
that lives until application exit keeps ownership stable across GUI operations.
Only Popen runs there; process control remains with each Recorder/ReplayBuffer.
"""

from __future__ import annotations

from concurrent.futures import Future
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import threading


_requests: Queue = Queue()
_thread: threading.Thread | None = None
_guard = threading.Lock()


def _spawn_loop() -> None:
    while True:
        command, kwargs, result = _requests.get()
        try:
            result.set_result(subprocess.Popen(command, **kwargs))
        except BaseException as exc:
            # One failed spawn must never end the owner thread of existing captures.
            result.set_exception(exc)


def spawn_owned(command: list[str], **kwargs) -> subprocess.Popen:
    """Launch a capture process that receives SIGINT if this application dies."""
    global _thread
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Lumen native capture requires Linux.")
    helper = str(Path(__file__).with_name("_child.py").resolve())
    wrapped = [sys.executable, helper, str(os.getpid()), *command]
    result = Future()
    with _guard:
        if _thread is None:
            _thread = threading.Thread(
                target=_spawn_loop, name="lumen-process-owner", daemon=True
            )
            _thread.start()
        _requests.put((wrapped, kwargs, result))
    return result.result()
