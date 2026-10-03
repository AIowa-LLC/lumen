"""Exercise real Linux process ownership without touching screen/audio devices."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from lumen._process import spawn_owned


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux parent-death signaling")
class OwnedProcessTests(unittest.TestCase):
    def test_helper_refuses_wrong_parent_before_executing_command(self):
        helper = Path(__file__).resolve().parent.parent / "lumen" / "_child.py"
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "should-not-exist"
            result = subprocess.run(
                [
                    sys.executable,
                    str(helper),
                    "1",
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                    str(marker),
                ]
            )
            self.assertEqual(result.returncode, 125)
            self.assertFalse(marker.exists())

    def test_exec_preserves_pid_and_exit_status(self):
        child = spawn_owned(
            [
                sys.executable,
                "-c",
                "import os; print(os.getpid(), flush=True); raise SystemExit(7)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        output, errors = child.communicate(timeout=5)
        self.assertEqual(int(output.strip()), child.pid)
        self.assertEqual(child.returncode, 7, errors)

    def test_child_survives_worker_return_but_stops_when_app_is_killed(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            received = Path(directory) / "received-sigint"
            child_code = """
import os, signal, sys, time
from pathlib import Path
def stop(signum, frame):
    Path(sys.argv[2]).write_text(str(signum))
    raise SystemExit(0)
signal.signal(signal.SIGINT, stop)
Path(sys.argv[1]).write_text(str(os.getpid()))
while True: time.sleep(.1)
"""
            parent_code = """
from lumen._process import spawn_owned
import sys, threading, time
def start():
    child = spawn_owned([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]],
                        start_new_session=True)
    print(child.pid, flush=True)
worker = threading.Thread(target=start)
worker.start()
worker.join()
print('worker-finished', flush=True)
while True: time.sleep(.1)
"""
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_code,
                    child_code,
                    str(ready),
                    str(received),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=Path(__file__).resolve().parent.parent,
            )
            child_pid = None
            try:
                deadline = time.monotonic() + 5
                while (
                    not ready.exists()
                    and time.monotonic() < deadline
                    and parent.poll() is None
                ):
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "Child did not start")
                child_pid = int(ready.read_text())
                self.assertEqual(int(parent.stdout.readline().strip()), child_pid)
                self.assertEqual(parent.stdout.readline().strip(), "worker-finished")
                time.sleep(0.15)
                self.assertFalse(
                    received.exists(),
                    "Exiting the transient worker incorrectly stopped the child",
                )
                # Kill only the dummy parent created above, never the desktop app.
                parent.kill()
                parent.wait(timeout=5)
                deadline = time.monotonic() + 5
                while not received.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(
                    received.exists(), "Child did not receive parent-death SIGINT"
                )
                self.assertEqual(received.read_text(), str(signal.SIGINT))
            finally:
                if parent.poll() is None:
                    parent.kill()
                parent.wait(timeout=5)
                if child_pid and not received.exists():
                    try:
                        os.kill(child_pid, signal.SIGINT)
                    except ProcessLookupError:
                        pass
                parent.stdout.close()
                parent.stderr.close()


if __name__ == "__main__":
    unittest.main()
