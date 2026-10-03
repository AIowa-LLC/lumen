"""Fresh-process Linux exec helper: stop capture if its owning app disappears.

This runs as a standalone file, never as a Popen preexec_fn in the threaded GUI.
The helper is replaced by the recorder, preserving its PID and exit status.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: _child.py EXPECTED_PARENT_PID PROGRAM [ARGUMENT ...]",
            file=sys.stderr,
        )
        return 2
    try:
        expected_parent = int(sys.argv[1])
        if expected_parent <= 1 or os.getppid() != expected_parent:
            return 125
        # If the parent dies while Python is still preparing exec, exit cleanly.
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(1, signal.SIGINT, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        # Close the race where the parent died before prctl registered the signal.
        if os.getppid() != expected_parent:
            return 125
        os.execvp(sys.argv[2], sys.argv[2:])
    except (OSError, ValueError) as exc:
        print(f"Lumen capture child could not start: {exc}", file=sys.stderr)
        return 126
    return 126


if __name__ == "__main__":
    raise SystemExit(main())
