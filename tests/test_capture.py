import json
import errno
from pathlib import Path
import tempfile
import threading
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from lumen.capture import (
    CaptureError,
    CaptureOptions,
    Recorder,
    build_gsr_command,
    build_wf_command,
)
from lumen.system import DesktopError, get_windows, parse_geometry, probe_media


class CaptureCommandTests(unittest.TestCase):
    def test_region_keeps_logical_coordinates_and_negative_origin(self):
        command = build_gsr_command(
            CaptureOptions(mode="region", geometry="-120,80 801x603"),
            Path("/tmp/clip.mkv"),
            Path("/tmp/test.sock"),
        )
        self.assertEqual(command[command.index("-w") + 1], "801x603+-120+80")
        self.assertIn("vfr", command)

    def test_audio_is_separate_for_editor_and_arguments_are_not_shell_code(self):
        source = "input with spaces;$(touch /tmp/never)"
        command = build_gsr_command(
            CaptureOptions(monitor="DP-1", audio="both", mic_source=source),
            Path("/tmp/clip.mkv"),
            Path("/tmp/test.sock"),
        )
        tracks = [command[i + 1] for i, value in enumerate(command) if value == "-a"]
        self.assertEqual(tracks, ["default_output", "device:" + source])

    def test_cpu_support_is_explicit(self):
        with self.assertRaises(ValueError):
            CaptureOptions(encoder="cpu", codec="av1").validate()
        command = build_gsr_command(
            CaptureOptions(monitor="DP-1", encoder="gpu"),
            Path("/tmp/clip.mkv"),
            Path("/tmp/test.sock"),
        )
        self.assertEqual(command[command.index("-fallback-cpu-encoding") + 1], "no")

    def test_compatibility_backend_never_silently_discards_requested_features(self):
        for options in (
            CaptureOptions(audio="both"),
            CaptureOptions(cursor=False),
            CaptureOptions(webcam="/dev/video0"),
            CaptureOptions(codec="av1"),
        ):
            with self.subTest(options=options), self.assertRaises(CaptureError):
                build_wf_command(options, Path("/tmp/clip.mkv"))

    def test_invalid_geometry_and_fps_rejected(self):
        for value in ("0 0 20 20", "0,0 1x10", "0,0 1x1; rm -rf /", "0,0 -5x8"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_geometry(value)
        with self.assertRaises(ValueError):
            CaptureOptions(fps=0).validate()

    def test_compatibility_quality_and_even_frame_size_are_explicit(self):
        command = build_wf_command(
            CaptureOptions(quality="medium"), Path("/tmp/source.mkv")
        )
        self.assertIn("crf=28", command)
        self.assertIn("pad=ceil(iw/2)*2:ceil(ih/2)*2", command)

    def test_regions_crossing_monitors_are_rejected_before_project_creation(self):
        monitors = [
            {"x": 0, "y": 0, "logical_width": 100, "logical_height": 100},
            {"x": 100, "y": 0, "logical_width": 100, "logical_height": 100},
        ]
        with (
            patch("lumen.capture.get_monitors", return_value=monitors),
            patch("lumen.capture.projects.create_project") as create,
        ):
            with self.assertRaisesRegex(CaptureError, "entirely inside one monitor"):
                Recorder().start(CaptureOptions(mode="region", geometry="50,0 100x100"))
            create.assert_not_called()


class RecorderControlTests(unittest.TestCase):
    def test_ipc_uses_acknowledged_absolute_pause_state(self):
        connection = MagicMock()
        connection.recv.side_effect = [b'{"id":1,"result":', b'"ok"}\n']
        factory = MagicMock()
        factory.return_value.__enter__.return_value = connection
        recorder = Recorder()
        recorder._ipc = Path("/tmp/lumen-test.sock")
        with patch("lumen.capture.socket.socket", factory):
            recorder._command("set-paused", True)
        self.assertEqual(
            json.loads(connection.sendall.call_args.args[0]),
            {"id": 1, "name": "set-paused", "data": True},
        )
        connection.connect.assert_called_once_with("/tmp/lumen-test.sock")

    def test_failed_start_preserves_partial_capture_and_marks_project(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(root=directory)
            process = MagicMock()
            process.poll.return_value = 1
            process.returncode = 1

            def start_process(command, **kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"partial capture")
                return process

            with (
                patch(
                    "lumen.capture.shutil.which",
                    return_value="/usr/bin/gpu-screen-recorder",
                ),
                patch("lumen.capture.subprocess.Popen", side_effect=start_process),
            ):
                with self.assertRaises(CaptureError):
                    recorder.start(
                        CaptureOptions(monitor="DP-1", backend="gpu-screen-recorder")
                    )
            self.assertEqual(recorder.project["status"], "failed")
            files = list(Path(recorder.project["path"]).glob("failed-*.mkv"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_bytes(), b"partial capture")

    def test_stop_preserves_unreadable_source(self):
        from lumen import project
        from lumen.system import DesktopError

        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder()
            recorder.project = project.create_project(root=directory)
            source = Path(recorder.project["path"]) / "source.mkv"
            source.write_bytes(b"recoverable raw data")
            recorder.process = MagicMock()
            recorder.process.poll.return_value = 1
            with patch(
                "lumen.capture.probe_media", side_effect=DesktopError("incomplete")
            ):
                with self.assertRaises(CaptureError):
                    recorder.stop()
            self.assertEqual(source.read_bytes(), b"recoverable raw data")
            self.assertEqual(recorder.project["status"], "failed")

    def test_elapsed_excludes_pauses(self):
        recorder = Recorder()
        recorder._started_at = 10
        recorder._paused_at = 14
        recorder._pause_duration = 1
        recorder.status = "paused"
        self.assertEqual(recorder.elapsed, 3)
        recorder.status = "recording"
        with patch("lumen.capture.time.monotonic", return_value=17):
            self.assertEqual(recorder.elapsed, 6)

    def test_first_frame_is_ready_before_muxer_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "source.mkv"
            output.touch()
            Path(str(output) + ".ts").write_text(
                "monotonic_microsec\trealtime_microsec\n1\t1\n"
            )
            recorder = Recorder()
            recorder.backend = "gpu-screen-recorder"
            recorder._ipc = Path(directory) / "control.sock"
            recorder._ipc.touch()
            recorder.process = MagicMock()
            recorder.process.poll.return_value = None
            recorder._wait_started(output)

    def test_full_disk_after_process_start_stops_owned_process_without_fallback(self):
        from lumen import project

        original_save = project.save_project
        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder(root=directory)
            process = MagicMock()
            process.pid = 1234
            process.poll.return_value = None
            process.returncode = None
            saves = 0

            def save(value):
                nonlocal saves
                saves += 1
                if saves >= 3:
                    raise OSError(errno.ENOSPC, "No space left on device")
                original_save(value)

            def finish(timeout):
                process.poll.return_value = 0
                process.returncode = 0
                return 0

            def spawn(command, **kwargs):
                Path(command[command.index("-o") + 1]).write_bytes(b"preserve me")
                return process

            process.wait.side_effect = finish
            with (
                patch("lumen.capture.projects.save_project", side_effect=save),
                patch("lumen.capture.shutil.which", return_value="recorder"),
                patch("lumen.capture.subprocess.Popen", side_effect=spawn) as popen,
                patch.object(recorder, "_wait_started"),
            ):
                with self.assertRaisesRegex(CaptureError, "No space left"):
                    recorder.start(
                        CaptureOptions(monitor="DP-1", cursor_telemetry=False)
                    )
            self.assertEqual(popen.call_count, 1)
            process.send_signal.assert_called_with(signal.SIGINT)
            self.assertFalse(recorder.is_running)
            self.assertIsNone(recorder._log)
            self.assertIsNone(recorder._runtime)
            self.assertEqual(
                (Path(recorder.project["path"]) / "source.mkv").read_bytes(),
                b"preserve me",
            )

    def test_media_save_survives_full_disk_manifest_failure(self):
        from lumen import project

        with tempfile.TemporaryDirectory() as directory:
            recorder = Recorder()
            recorder.project = project.create_project(root=directory)
            recorder.process = MagicMock()
            recorder.process.poll.return_value = 0
            recorder.process.returncode = 0
            recorder._log = MagicMock()
            logfile = recorder._log
            with (
                patch(
                    "lumen.capture.probe_media",
                    return_value={"duration": 2, "width": 200, "height": 100},
                ),
                patch(
                    "lumen.capture.projects.save_project",
                    side_effect=OSError(errno.ENOSPC, "disk full"),
                ),
            ):
                result = recorder.stop()
            self.assertEqual(result["status"], "ready")
            self.assertIn("disk full", result["metadata_error"])
            self.assertEqual(recorder.status, "idle")
            logfile.close.assert_called_once()

    def test_shutdown_is_bounded_and_only_signals_own_process(self):
        recorder = Recorder()
        recorder.process = MagicMock()
        recorder.process.poll.return_value = None
        recorder.process.pid = 1234
        recorder.process.wait.side_effect = subprocess.TimeoutExpired("recorder", 1)
        with self.assertRaisesRegex(CaptureError, "1234 did not exit"):
            recorder._end_process()
        self.assertEqual(
            [c.args[0] for c in recorder.process.send_signal.call_args_list],
            [signal.SIGINT, signal.SIGTERM, signal.SIGKILL],
        )

    def test_telemetry_exits_with_child_and_flushes_to_original_project(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "original"
            current = Path(directory) / "current"
            original.mkdir()
            current.mkdir()
            recorder = Recorder()
            recorder.project = {"path": str(current)}
            process = MagicMock()
            process.poll.return_value = 1
            samples = [{"t": 1, "x": 0.5, "y": 0.5, "inside": True}]
            recorder._sample_cursor(
                Path("/no/socket"),
                threading.Event(),
                samples,
                (0, 0, 100, 100),
                original,
                process,
                0,
            )
            self.assertTrue((original / "cursor.json").is_file())
            self.assertFalse((current / "cursor.json").exists())


class DesktopDiscoveryTests(unittest.TestCase):
    def test_only_visible_windows_and_pinned_windows_are_selectable(self):
        clients = [
            {
                "title": "visible",
                "workspace": {"id": 1},
                "at": [0, 0],
                "size": [40, 40],
            },
            {
                "title": "hidden workspace",
                "workspace": {"id": 2},
                "at": [0, 0],
                "size": [40, 40],
            },
            {
                "title": "pinned",
                "workspace": {"id": 2},
                "pinned": True,
                "at": [0, 0],
                "size": [40, 40],
            },
        ]
        with (
            patch("lumen.system._run", return_value=json.dumps(clients)),
            patch(
                "lumen.system.get_monitors",
                return_value=[{"activeWorkspace": {"id": 1}}],
            ),
        ):
            self.assertEqual([w["title"] for w in get_windows()], ["visible", "pinned"])

    def test_header_only_recording_is_not_reported_ready(self):
        with patch(
            "lumen.system._run",
            return_value=json.dumps(
                {"streams": [{"codec_type": "video", "width": 200, "height": 100}]}
            ),
        ):
            with self.assertRaisesRegex(DesktopError, "no usable duration"):
                probe_media("/tmp/header-only.mkv")


if __name__ == "__main__":
    unittest.main()
