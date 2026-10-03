from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lumen.capture import CaptureError, CaptureOptions, LiveInputs, Recorder, audio_track_map, build_gsr_command, build_wf_command
from lumen.replay import ReplayBuffer


class LiveCaptureTests(unittest.TestCase):
    def test_prepare_reserves_only_private_source_and_opens_no_camera_when_initially_off(self):
        options = CaptureOptions(mode="region", geometry="20,40 800x450", audio="desktop", live_inputs=True)
        with (patch("lumen.microphone.LiveMicrophone") as microphone,
              patch("lumen.capture.CameraBubble") as camera):
            microphone.return_value.start.return_value = "lumen_private.monitor"
            inputs = LiveInputs(options)
            effective = inputs.prepare()
            microphone.assert_called_once_with(mic_source=None, enabled=False)
            camera.return_value.start.assert_not_called()
            self.assertEqual(effective.audio, "both")
            self.assertEqual(effective.mic_source, "lumen_private.monitor")
            self.assertIsNone(effective.webcam)
            self.assertEqual(options.audio, "desktop")
            inputs.bind(4321)
            microphone.return_value.bind.assert_called_once_with(4321)

    def test_initial_camera_opens_before_recorder_command_and_closes_on_failure(self):
        options = CaptureOptions(mode="region", geometry="20,40 800x450", webcam="/dev/video0", live_inputs=True)
        with (patch("lumen.microphone.LiveMicrophone") as microphone,
              patch("lumen.capture.CameraBubble") as camera):
            microphone.return_value.start.return_value = "private.monitor"
            inputs = LiveInputs(options)
            effective = inputs.prepare()
            camera.return_value.start.assert_called_once()
            self.assertIsNone(effective.webcam)
            camera.return_value.stop.side_effect = RuntimeError("camera stuck")
            with self.assertRaisesRegex(CaptureError, "camera stuck"):
                inputs.close()
            microphone.return_value.close.assert_called_once()

    def test_silent_reserved_mic_track_is_mapped_in_each_audio_mode(self):
        for mode in ("none", "mic"):
            self.assertEqual(audio_track_map(CaptureOptions(audio=mode, live_inputs=True)), {"mic": 0})
        for mode in ("desktop", "both"):
            self.assertEqual(audio_track_map(CaptureOptions(audio=mode, live_inputs=True)), {"desktop": 0, "mic": 1})
        self.assertEqual(audio_track_map(CaptureOptions(audio="none")), {})

    def test_live_camera_is_not_opened_by_encoder_and_wf_cannot_silently_drop_controls(self):
        options = CaptureOptions(monitor="DP-1", live_inputs=True, webcam="/dev/video0")
        command = build_gsr_command(options, Path("/tmp/a.mkv"), Path("/tmp/a.sock"))
        self.assertEqual(command[command.index("-w") + 1], "DP-1")
        with self.assertRaisesRegex(CaptureError, "Live microphone"):
            build_wf_command(options, Path("/tmp/a.mkv"))

    def test_toggle_ack_updates_metadata_only_on_success_and_uses_pause_adjusted_clock(self):
        recorder = Recorder()
        recorder.process = MagicMock()
        recorder.process.poll.return_value = None
        recorder._live = MagicMock()
        recorder.project = {}
        recorder.status = "paused"
        recorder._started_at, recorder._paused_at, recorder._pause_duration = 10, 14, 1
        recorder._live.set_microphone_enabled.side_effect = RuntimeError("cannot enable")
        with self.assertRaisesRegex(CaptureError, "cannot enable"):
            recorder.set_microphone_enabled(True)
        self.assertNotIn("input_events", recorder.project)
        recorder._live.set_microphone_enabled.side_effect = None
        recorder.project = {"name": "test"}
        with patch.object(recorder, "_persist_best_effort"):
            recorder.set_microphone_enabled(True)
        self.assertEqual(recorder.project["input_events"], [{"t": 3, "input": "microphone", "enabled": True}])

    def test_no_live_toggle_after_capture_or_replay_process_exits(self):
        for capture in (Recorder(), ReplayBuffer()):
            capture.process = MagicMock()
            capture.process.poll.return_value = 0
            capture._live = MagicMock()
            self.assertFalse(capture.live_inputs_available)
            self.assertFalse(capture.mic_enabled)
            self.assertFalse(capture.camera_enabled)
            with self.assertRaises(CaptureError):
                capture.set_microphone_enabled(True)
            capture._live.set_microphone_enabled.assert_not_called()

    def test_failed_live_start_stops_encoder_before_releasing_routes(self):
        with tempfile.TemporaryDirectory() as library:
            recorder = Recorder(root=library)
            process = MagicMock()
            process.pid = 123
            process.poll.return_value = None
            order = []
            def finish(timeout):
                order.append("encoder stopped")
                process.poll.return_value = 0
                process.returncode = 0
            process.wait.side_effect = finish
            live = MagicMock()
            live.prepare.return_value = CaptureOptions(monitor="DP-1", audio="mic", mic_source="private.monitor", live_inputs=True)
            live.bind.side_effect = RuntimeError("mic route failed")
            live.close.side_effect = lambda: order.append("inputs closed")
            with (
                patch("lumen.capture.LiveInputs", return_value=live),
                patch("lumen.capture.shutil.which", return_value="installed"),
                patch("lumen.capture.spawn_owned", return_value=process),
                patch.object(recorder, "_wait_started"),
            ):
                with self.assertRaisesRegex(CaptureError, "mic route failed"):
                    recorder.start(CaptureOptions(monitor="DP-1", live_inputs=True, backend="gpu-screen-recorder"))
            self.assertEqual(order, ["encoder stopped", "inputs closed"])
            self.assertEqual(recorder.project["audio_tracks"], {"mic": 0})

    def test_recording_and_replay_stop_encoder_before_inputs(self):
        from lumen import project

        with tempfile.TemporaryDirectory() as library:
            for capture in (Recorder(root=library), ReplayBuffer(root=library)):
                capture.process = MagicMock()
                capture.process.poll.return_value = None
                capture.process.returncode = None
                capture._live = MagicMock()
                order = []
                def finish(timeout):
                    order.append("encoder stopped")
                    capture.process.poll.return_value = 0
                    capture.process.returncode = 0
                capture.process.wait.side_effect = finish
                capture._live.close.side_effect = lambda: order.append("inputs closed")
                if isinstance(capture, Recorder):
                    capture.project = project.create_project(root=library)
                    capture.backend = "gpu-screen-recorder"
                    with (patch.object(capture, "_command"),
                          patch("lumen.capture.probe_media", return_value={"duration": 1, "width": 100, "height": 100})):
                        capture.stop()
                else:
                    with patch.object(capture, "_request"):
                        capture.stop()
                self.assertEqual(order, ["encoder stopped", "inputs closed"])
                self.assertIsNone(capture._live)

    def test_live_resources_are_retained_if_encoder_still_alive(self):
        for capture in (Recorder(), ReplayBuffer()):
            capture.process = MagicMock()
            capture.process.poll.return_value = None
            capture._live = MagicMock()
            capture._cleanup()
            capture._live.close.assert_not_called()

    def test_automatic_basic_fallback_preserves_tracks_and_disables_only_live_controls(self):
        with tempfile.TemporaryDirectory() as library:
            recorder = Recorder(root=library)
            process = MagicMock()
            process.pid = 123
            process.poll.return_value = None
            live = MagicMock()
            live.prepare.side_effect = RuntimeError("GPU route unavailable")
            with (
                patch("lumen.capture.LiveInputs", return_value=live),
                patch("lumen.capture.shutil.which", return_value="installed"),
                patch("lumen.capture.spawn_owned", return_value=process) as spawn,
                patch.object(recorder, "_wait_started"),
                patch("lumen.capture._run", return_value="selected-mic"),
            ):
                project = recorder.start(CaptureOptions(monitor="DP-1", live_inputs=True, audio="mic",
                                                        cursor_telemetry=False, record_clicks=False))
            self.assertEqual(spawn.call_args.args[0][0], "wf-recorder")
            self.assertEqual(project["audio_tracks"], {"mic": 0})
            self.assertFalse(project["capture"]["live_inputs"])
            self.assertFalse(recorder.live_inputs_available)
            self.assertTrue(recorder.mic_enabled)
            self.assertIn("Live microphone", project["capture_warning"])
            process.poll.return_value = 0
            recorder._cleanup()

    def test_static_replay_inputs_show_truthful_on_state(self):
        replay = ReplayBuffer()
        replay.process = MagicMock()
        replay.process.poll.return_value = None
        replay.options = CaptureOptions(audio="mic", webcam="/dev/video0")
        self.assertFalse(replay.live_inputs_available)
        self.assertTrue(replay.mic_enabled)
        self.assertTrue(replay.camera_enabled)


if __name__ == "__main__":
    unittest.main()
