import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lumen.capture import CaptureError, CaptureOptions
from lumen.replay import ReplayBuffer, build_replay_command


class ReplayTests(unittest.TestCase):
    def test_ram_buffer_has_bounded_duration_and_bitrate(self):
        command = build_replay_command(
            CaptureOptions(monitor="DP-1"),
            Path("/tmp/private"),
            Path("/tmp/socket"),
            30,
        )
        for flag, value in (
            ("-r", "30"),
            ("-q", "18000"),
            ("-replay-storage", "ram"),
            ("-bm", "cbr"),
            ("-o", "/tmp/private"),
            ("-restart-replay-on-save", "no"),
        ):
            self.assertEqual(command[command.index(flag) + 1], value)
        for seconds in (0, 1, 61, True):
            with self.assertRaises(ValueError):
                build_replay_command(
                    CaptureOptions(), Path("unused"), Path("unused"), seconds
                )

    def test_compatibility_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            build_replay_command(
                CaptureOptions(backend="wf-recorder"),
                Path("unused"),
                Path("unused"),
                30,
            )

    def make_buffer(self, directory):
        buffer = ReplayBuffer(root=directory)
        buffer.staging_path = Path(directory) / ".staging"
        buffer.staging_path.mkdir()
        (buffer.staging_path / "capture.log").write_text("replay log")
        buffer.options = CaptureOptions(
            monitor="DP-1", audio="both", cursor_telemetry=False
        )
        buffer.process = MagicMock()
        buffer.process.poll.return_value = None
        buffer.status = "buffering"
        return buffer

    def test_save_imports_media_without_stopping_or_clearing_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = self.make_buffer(directory)
            clip = buffer.staging_path / "Replay.mkv"
            clip.write_bytes(b"raw recording")
            with (
                patch.object(buffer, "_request", return_value=str(clip)) as request,
                patch(
                    "lumen.replay.probe_media",
                    return_value={"duration": 2, "width": 800, "height": 450},
                ),
            ):
                project = buffer.save()
            self.assertTrue(buffer.is_running)
            request.assert_called_once_with(
                "save-replay", {"seconds": 30, "restart-replay": False}
            )
            self.assertEqual(
                (Path(project["path"]) / "source.mkv").read_bytes(), b"raw recording"
            )
            self.assertFalse(clip.exists())
            self.assertFalse(project["cursor_telemetry"])
            self.assertEqual(project["audio_tracks"], {"desktop": 0, "mic": 1})
            buffer.process.send_signal.assert_not_called()

    def test_import_failure_keeps_staged_media_and_blocks_filename_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = self.make_buffer(directory)
            clip = buffer.staging_path / "Replay.mkv"
            clip.write_bytes(b"raw recording")
            with (
                patch.object(buffer, "_request", return_value=str(clip)),
                patch("lumen.replay.probe_media", side_effect=ValueError("unreadable")),
            ):
                with self.assertRaises(CaptureError):
                    buffer.save()
            self.assertTrue(clip.exists())
            with self.assertRaisesRegex(CaptureError, "needs recovery"):
                buffer.save()
            buffer.process.poll.return_value = 0
            buffer.stop()
            self.assertTrue(clip.exists())

    def test_saved_path_must_belong_to_this_buffer(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = self.make_buffer(directory)
            outside = Path(directory) / "private-data.mkv"
            outside.write_text("unrelated")
            with patch.object(buffer, "_request", return_value=str(outside)):
                with self.assertRaisesRegex(CaptureError, "outside its private"):
                    buffer.save()
            self.assertEqual(outside.read_text(), "unrelated")

    def test_stop_targets_owned_process_and_preserves_unknown_saved_clip(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = self.make_buffer(directory)
            clip = buffer.staging_path / "unacknowledged.mkv"
            clip.write_bytes(b"keep")

            def exited(timeout):
                buffer.process.poll.return_value = 0
                return 0

            buffer.process.wait.side_effect = exited
            with patch.object(
                buffer, "_request", side_effect=CaptureError("socket closed")
            ):
                buffer.stop()
            buffer.process.send_signal.assert_called_once_with(signal.SIGINT)
            self.assertTrue(clip.exists())
            self.assertFalse(buffer.is_running)

    def test_protocol_waits_for_complete_acknowledgment(self):
        buffer = ReplayBuffer()
        buffer._ipc = Path("/tmp/owned.sock")
        connection = MagicMock()
        connection.recv.side_effect = [
            b'{"id":1,"result":"ok",',
            b'"data":"/tmp/saved.mkv"}\n',
        ]
        factory = MagicMock()
        factory.return_value.__enter__.return_value = connection
        with patch("lumen.replay.socket.socket", factory):
            result = buffer._request("save-replay", {"seconds": 30})
        self.assertEqual(result, "/tmp/saved.mkv")
        self.assertEqual(
            json.loads(connection.sendall.call_args.args[0])["name"], "save-replay"
        )

    def test_start_failure_does_not_leave_child_running(self):
        with tempfile.TemporaryDirectory() as directory:
            buffer = ReplayBuffer(root=directory)
            process = MagicMock()
            process.poll.return_value = None

            def exited(timeout):
                process.poll.return_value = 0
                return 0

            process.wait.side_effect = exited
            with (
                patch("lumen.replay.shutil.which", return_value="gpu-screen-recorder"),
                patch("lumen.replay.get_monitors", return_value=[]),
                patch("lumen.replay.subprocess.Popen", return_value=process),
                patch.object(
                    buffer, "_wait_started", side_effect=CaptureError("startup failed")
                ),
            ):
                with self.assertRaisesRegex(CaptureError, "startup failed"):
                    buffer.start(CaptureOptions(monitor="DP-1"))
            self.assertFalse(buffer.is_running)
            self.assertIsNone(buffer._runtime)
            self.assertIsNone(buffer._log)
            process.send_signal.assert_called_once_with(signal.SIGINT)


if __name__ == "__main__":
    unittest.main()
