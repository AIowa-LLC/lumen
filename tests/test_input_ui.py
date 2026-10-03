"""Device controller regressions with no physical audio/video access."""

from copy import deepcopy
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    from lumen import ui
except (ImportError, ValueError):
    ui = None


class Choice:
    def __init__(self, selected=0, callback=None):
        self.selected = selected
        self.callback = callback
        self.model = None

    def get_selected(self):
        return self.selected

    def set_selected(self, selected):
        changed = selected != self.selected
        self.selected = selected
        if changed and self.callback:
            self.callback()

    def set_model(self, model):
        self.model = model
        self.set_selected(0)


def backend():
    engine = Mock(
        is_running=False,
        mic_enabled=False,
        camera_enabled=False,
        live_inputs_available=True,
    )
    engine.set_microphone_enabled.side_effect = lambda enabled: setattr(
        engine, "mic_enabled", enabled
    )
    engine.set_webcam_enabled.side_effect = lambda enabled, **_: setattr(
        engine, "camera_enabled", enabled
    )
    return engine


def controller():
    studio = SimpleNamespace(
        recorder=backend(),
        replay=backend(),
        capture_busy=False,
        countdown_source=None,
        shutting_down=False,
        devices_ready=True,
        refreshing_devices=False,
        record_when_ready=False,
        device_busy={"mic": False, "camera": False},
        device_errors={"mic": "", "camera": ""},
        capture_lifecycle_lock=threading.RLock(),
        settings={},
        preferred_camera_id=None,
        microphones=[{"name": "test-mic-a", "description": "Test microphone A"}],
        camera_devices=[
            {
                "id": "test-camera-a",
                "path": "/dev/not-a-real-camera-a",
                "label": "Test camera A",
            },
            {
                "id": "test-camera-b",
                "path": "/dev/not-a-real-camera-b",
                "label": "Test camera B",
            },
        ],
        monitors=[{"name": "TEST-DISPLAY", "width": 640, "height": 360}],
        windows=[],
        worker=Mock(),
        error=Mock(),
        toast=Mock(),
        refresh_input_controls=Mock(),
        update_monitor_info=Mock(),
        record_button=Mock(),
        replay_start_button=Mock(),
        start_recording=Mock(),
    )
    for method in (
        "selected_microphone",
        "selected_camera",
        "resolve_camera",
        "capture_device_changed",
        "active_input_backend",
        "device_controls",
        "toggle_microphone",
        "toggle_camera",
        "toggle_input",
        "refresh_devices",
        "capture_options",
        "shutdown_capture",
    ):
        setattr(studio, method, MethodType(getattr(ui.StudioWindow, method), studio))
    for name, selected in (("audio", 1), ("mic", 1), ("webcam", 0)):
        setattr(studio, name, Choice(selected, studio.capture_device_changed))
    for name in (
        "mode",
        "monitor",
        "window_picker",
        "fps",
        "encoder",
        "quality",
        "codec",
        "resolution",
    ):
        setattr(studio, name, Choice())
    for name in ("cursor", "telemetry", "record_clicks"):
        setattr(studio, name, Mock(get_active=Mock(return_value=True)))
    return studio


@unittest.skipIf(ui is None, "GTK Python bindings unavailable")
class InputControllerTests(unittest.TestCase):
    def setUp(self):
        self.save_settings = patch.object(ui, "save_settings").start()
        self.addCleanup(patch.stopall)
        self.studio = controller()
        self.camera_discovery = patch.object(
            ui, "get_cameras", side_effect=lambda: deepcopy(self.studio.camera_devices)
        ).start()

    def request(self, kind, replay=False):
        engine = self.studio.replay if replay else self.studio.recorder
        engine.is_running = True
        self.studio.toggle_input(kind)
        return engine, self.studio.worker.call_args.args

    def test_idle_microphone_toggle_preserves_desktop_audio_for_all_modes(self):
        for before, after in ((0, 2), (1, 3), (2, 0), (3, 1)):
            with self.subTest(before=before):
                self.studio.audio.selected = before
                self.studio.toggle_microphone()
                self.assertEqual(self.studio.audio.get_selected(), after)
                self.assertEqual(self.studio.settings["audio_index"], after)
        self.studio.worker.assert_not_called()
        self.studio.recorder.set_microphone_enabled.assert_not_called()
        self.studio.replay.set_microphone_enabled.assert_not_called()

    def test_idle_camera_off_remembers_device_identity_and_reuses_it(self):
        self.studio.webcam.set_selected(2)
        self.assertEqual(self.studio.preferred_camera_id, "test-camera-b")
        self.studio.toggle_camera()
        self.assertEqual(self.studio.webcam.get_selected(), 0)
        self.assertFalse(self.studio.settings["camera_enabled"])
        self.assertEqual(self.studio.settings["camera_id"], "test-camera-b")
        self.studio.toggle_camera()
        self.assertEqual(self.studio.webcam.get_selected(), 2)
        self.assertTrue(self.studio.settings["camera_enabled"])
        self.studio.worker.assert_not_called()
        self.studio.recorder.set_webcam_enabled.assert_not_called()

    def test_device_refresh_keeps_missing_explicit_identities_without_fallback(self):
        studio = self.studio
        studio.settings.update(
            mic_source="missing-mic",
            mic_label="Saved mic",
            camera_id="missing-camera",
            camera_label="Saved camera",
            camera_enabled=False,
        )
        studio.preferred_camera_id = "missing-camera"
        studio.refresh_devices()
        done = studio.worker.call_args.args[1]
        done(
            (
                deepcopy(studio.monitors),
                deepcopy(studio.microphones),
                [],
                deepcopy(studio.camera_devices),
            )
        )
        self.assertEqual(studio.selected_microphone()["name"], "missing-mic")
        self.assertTrue(studio.selected_microphone()["unavailable"])
        self.assertEqual(studio.selected_camera()["id"], "missing-camera")
        self.assertTrue(studio.selected_camera()["unavailable"])
        states = studio.device_controls()
        self.assertFalse(states["mic"]["available"])
        self.assertFalse(states["camera"]["available"])
        studio.toggle_microphone()
        studio.toggle_camera()
        self.assertEqual(studio.audio.get_selected(), 1)
        self.assertEqual(studio.webcam.get_selected(), 0)
        studio.audio.set_selected(3)
        with self.assertRaisesRegex(ValueError, "selected microphone is unavailable"):
            studio.capture_options()
        studio.audio.set_selected(1)
        studio.webcam.set_selected(len(studio.camera_devices))
        with self.assertRaisesRegex(ValueError, "selected camera is unavailable"):
            studio.capture_options()

    def test_startup_and_refresh_selection_signals_cannot_overwrite_saved_identity(
        self,
    ):
        for ready, refreshing in ((False, False), (True, True)):
            self.studio.devices_ready, self.studio.refreshing_devices = (
                ready,
                refreshing,
            )
            self.studio.settings = {
                "camera_id": "remember-me",
                "mic_source": "explicit-mic",
            }
            before = deepcopy(self.studio.settings)
            self.studio.capture_device_changed()
            self.assertEqual(self.studio.settings, before)
        self.save_settings.assert_not_called()

    def test_live_microphone_state_and_preferences_change_only_after_acknowledgement(
        self,
    ):
        for replay in (False, True):
            with self.subTest(replay=replay):
                self.studio = controller()
                engine, (work, done, failed) = self.request("mic", replay=replay)
                self.assertEqual(self.studio.audio.get_selected(), 1)
                self.assertFalse(self.studio.device_controls()["mic"]["enabled"])
                self.assertTrue(self.studio.capture_busy)
                work()
                self.assertTrue(engine.mic_enabled)
                self.assertEqual(self.studio.audio.get_selected(), 1)
                done(None)
                self.assertEqual(self.studio.audio.get_selected(), 3)
                self.assertFalse(self.studio.capture_busy)
                self.assertFalse(self.studio.device_busy["mic"])
                self.studio.toggle_microphone()
                work, done, failed = self.studio.worker.call_args.args
                work()
                done(None)
                self.assertFalse(engine.mic_enabled)
                self.assertEqual(self.studio.audio.get_selected(), 1)

    def test_live_camera_uses_selected_path_and_persists_only_after_success(self):
        self.studio.preferred_camera_id = "test-camera-b"
        engine, (work, done, failed) = self.request("camera")
        self.assertEqual(self.studio.webcam.get_selected(), 0)
        work()
        engine.set_webcam_enabled.assert_called_once_with(
            True, device="/dev/not-a-real-camera-b"
        )
        self.assertEqual(self.studio.webcam.get_selected(), 0)
        done(None)
        self.assertEqual(self.studio.webcam.get_selected(), 2)
        self.assertTrue(self.studio.device_controls()["camera"]["enabled"])
        self.studio.toggle_camera()
        work, done, failed = self.studio.worker.call_args.args
        work()
        done(None)
        self.assertEqual(self.studio.webcam.get_selected(), 0)
        self.assertFalse(engine.camera_enabled)
        self.assertEqual(self.studio.settings["camera_id"], "test-camera-b")

    def test_camera_resolution_rejects_missing_identity_and_uses_current_device_path(
        self,
    ):
        studio = self.studio
        studio.preferred_camera_id = "test-camera-b"
        engine, (work, done, failed) = self.request("camera")
        self.camera_discovery.side_effect = None
        self.camera_discovery.return_value = [studio.camera_devices[0]]
        with self.assertRaisesRegex(ValueError, "no longer available") as error:
            work()
        failed(error.exception)
        engine.set_webcam_enabled.assert_not_called()
        self.assertFalse(engine.camera_enabled)
        self.assertEqual(studio.webcam.get_selected(), 0)
        self.assertIn(
            "no longer available", studio.device_controls()["camera"]["detail"]
        )

        self.camera_discovery.return_value = [
            {**studio.camera_devices[1], "path": "/dev/new-camera-path"}
        ]
        studio.toggle_camera()
        work, done, failed = studio.worker.call_args.args
        work()
        done(None)
        engine.set_webcam_enabled.assert_called_once_with(
            True, device="/dev/new-camera-path"
        )
        self.camera_discovery.reset_mock()
        studio.toggle_camera()
        work, done, failed = studio.worker.call_args.args
        work()
        done(None)
        self.camera_discovery.assert_not_called()
        self.assertFalse(engine.camera_enabled)

    def test_live_failure_retains_preferences_and_confirmed_state_and_reports_reason(
        self,
    ):
        for kind, method in (
            ("mic", "set_microphone_enabled"),
            ("camera", "set_webcam_enabled"),
        ):
            with self.subTest(kind=kind):
                self.studio = controller()
                engine, (work, done, failed) = self.request(kind)
                before = deepcopy(self.studio.settings)
                getattr(engine, method).side_effect = RuntimeError(
                    "Synthetic device refused to start"
                )
                try:
                    work()
                except RuntimeError as exc:
                    failed(exc)
                else:
                    self.fail("Expected the simulated backend failure")
                state = self.studio.device_controls()[kind]
                self.assertFalse(state["enabled"])
                self.assertFalse(state["busy"])
                self.assertIn("refused", state["detail"])
                self.assertEqual(self.studio.settings, before)
                self.assertEqual(self.studio.audio.get_selected(), 1)
                self.assertEqual(self.studio.webcam.get_selected(), 0)
                self.studio.error.assert_called_once()

    def test_busy_operation_serializes_both_device_toggles_and_countdown_blocks_changes(
        self,
    ):
        self.request("mic")
        self.studio.toggle_microphone()
        self.studio.toggle_camera()
        self.assertEqual(self.studio.worker.call_count, 1)
        self.studio = controller()
        self.studio.capture_busy = True
        self.studio.countdown_source = 123
        self.studio.toggle_microphone()
        self.studio.toggle_camera()
        self.studio.worker.assert_not_called()
        self.assertEqual(self.studio.audio.get_selected(), 1)
        self.assertEqual(self.studio.webcam.get_selected(), 0)

    def test_unsupported_live_backend_and_shutdown_disable_controls(self):
        self.studio.recorder.is_running = True
        self.studio.recorder.live_inputs_available = False
        for state in self.studio.device_controls().values():
            self.assertFalse(state["available"])
            self.assertIn("native", state["detail"])
        self.studio.recorder.live_inputs_available = True
        self.studio.shutting_down = True
        self.studio.toggle_microphone()
        self.studio.toggle_camera()
        self.studio.worker.assert_not_called()

    def test_queued_change_cannot_reach_replacement_backend_or_start_during_shutdown(
        self,
    ):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                self.studio = controller()
                engine, (work, done, failed) = self.request("mic")
                if shutdown:
                    self.studio.shutting_down = True
                else:
                    engine.is_running = False
                    self.studio.replay.is_running = True
                with self.assertRaisesRegex(RuntimeError, "recording ended"):
                    work()
                engine.set_microphone_enabled.assert_not_called()
                self.studio.replay.set_microphone_enabled.assert_not_called()

    def test_shutdown_waits_for_inflight_change_under_lifecycle_lock(self):
        engine, (work, done, failed) = self.request("mic")
        entered, release = threading.Event(), threading.Event()
        order, errors = [], []

        def enable(value):
            order.append("enable entered")
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Test operation was not released")
            engine.mic_enabled = value
            order.append("enable acknowledged")

        def run_work():
            try:
                work()
            except Exception as exc:
                errors.append(exc)

        engine.set_microphone_enabled.side_effect = enable
        engine.stop.side_effect = lambda: order.append("stopped")
        mutation = threading.Thread(target=run_work)
        closer = threading.Thread(target=self.studio.shutdown_capture)
        try:
            mutation.start()
            self.assertTrue(entered.wait(2))
            closer.start()
            # Acquiring this lock is impossible until shutdown has passed its
            # initial flag assignment and is queued behind the in-flight change.
            for _ in range(1000):
                if self.studio.shutting_down:
                    break
                threading.Event().wait(0.001)
            self.assertTrue(self.studio.shutting_down)
            engine.stop.assert_not_called()
        finally:
            release.set()
            mutation.join(3)
            if closer.ident is not None:
                closer.join(3)
        self.assertFalse(mutation.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(order, ["enable entered", "enable acknowledged", "stopped"])


if __name__ == "__main__":
    unittest.main()
