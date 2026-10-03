import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from lumen.camera import CameraBubble, CameraError, bubble_geometry, camera_mode


class CameraTests(unittest.TestCase):
    def test_bubble_fits_negative_origin_and_small_region(self):
        for bounds in ((-1920, 0, 1920, 1080), (20, 50, 160, 100), (0, 0, 800, 450)):
            x, y, width, height = bubble_geometry(bounds)
            self.assertGreaterEqual(x, bounds[0])
            self.assertGreaterEqual(y, bounds[1])
            self.assertLessEqual(x + width, bounds[0] + bounds[2])
            self.assertLessEqual(y + height, bounds[1] + bounds[3])
        with self.assertRaises(CameraError):
            bubble_geometry((0, 0, 90, 80))

    def test_mode_is_advertised_and_prefers_small_thirty_fps_mjpeg(self):
        inventory = "\n".join((
            "/dev/video0|1920x1080@60hz|mjpeg", "/dev/video0|640x480@30hz|yuyv",
            "/dev/video0|640x480@30hz|mjpeg", "/dev/video2|320x180@30hz|mjpeg",
        ))
        self.assertEqual(camera_mode("/dev/video0", inventory), {
            "video_size": "640x480", "framerate": "30", "input_format": "mjpeg"})
        with self.assertRaises(CameraError):
            camera_mode("/dev/video9", inventory)

    def test_live_feed_is_opened_only_after_owned_window_is_positioned(self):
        camera = CameraBubble((0, 0, 800, 450), "/dev/video0")
        process = MagicMock()
        process.poll.return_value = None
        events = []
        def request(command, **kwargs):
            events.append(command[0])
            return {"w": 640} if command[:2] == ["get_property", "video-params"] else None
        with (
            patch.object(camera, "_install_rule", side_effect=lambda: events.append("rule")),
            patch.object(camera, "_wait_window", side_effect=lambda: events.append("positioned")),
            patch.object(camera, "_source", return_value=("synthetic", {})),
            patch.object(camera, "_request", side_effect=request),
            patch("lumen.camera.spawn_owned", return_value=process),
            patch("lumen.camera.shutil.which", return_value="/usr/bin/mpv"),
            patch("lumen.camera.threading.Thread"),
            patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/tmp"}),
        ):
            try:
                camera.start()
                self.assertTrue(camera.enabled)
                self.assertLess(events.index("positioned"), events.index("loadfile"))
            finally:
                process.poll.return_value = 0
                camera.stop()

    def test_failed_positioning_never_opens_camera_and_stops_owned_player(self):
        camera = CameraBubble((0, 0, 800, 450), "/dev/video0")
        process = MagicMock()
        process.poll.return_value = None
        def stopped(*args, **kwargs):
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = stopped
        with (
            patch.object(camera, "_install_rule"),
            patch.object(camera, "_wait_window", side_effect=CameraError("outside bounds")),
            patch.object(camera, "_source") as source,
            patch.object(camera, "_request") as request,
            patch("lumen.camera.spawn_owned", return_value=process),
            patch("lumen.camera.shutil.which", return_value="mpv"),
            patch("lumen.camera.threading.Thread"),
            patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/tmp"}),
        ):
            with self.assertRaisesRegex(CameraError, "outside bounds"):
                camera.start()
            source.assert_not_called()
            request.assert_called_once_with(["quit"], timeout=1)
            self.assertFalse(camera.enabled)
            self.assertIsNone(camera._runtime)

    def test_off_waits_for_process_exit_and_removes_only_own_rule(self):
        camera = CameraBubble((0, 0, 800, 450), "/dev/video0")
        camera.process = MagicMock()
        camera.process.poll.return_value = None
        camera._enabled = True
        camera._rule_installed = True
        with patch.object(camera, "_request") as request, patch.object(camera, "_hypr") as hypr:
            camera.stop()
        request.assert_called_once_with(["quit"], timeout=1)
        camera.process.wait.assert_called()
        self.assertIn(camera.token, hypr.call_args.args[0])
        self.assertFalse(camera.enabled)

    def test_mpv_ipc_ignores_events_and_checks_acknowledgment(self):
        camera = CameraBubble((0, 0, 800, 450))
        camera._ipc = Path("/tmp/lumen-test.sock")
        connection = MagicMock()
        connection.recv.return_value = b'{"event":"idle"}\n{"request_id":1,"error":"success","data":true}\n'
        with patch("lumen.camera.socket.socket") as socket:
            socket.return_value.__enter__.return_value = connection
            self.assertTrue(camera._request(["get_property", "idle-active"]))
        self.assertEqual(json.loads(connection.sendall.call_args.args[0])["command"],
                         ["get_property", "idle-active"])

    def test_disconnected_feed_becomes_off_and_closes_player(self):
        camera = CameraBubble((0, 0, 800, 450))
        camera.process = MagicMock()
        camera.process.poll.return_value = None
        camera._enabled = True
        camera._stop_event = MagicMock()
        camera._stop_event.wait.return_value = False
        camera._stop_event.is_set.return_value = False
        with (patch.object(camera, "_hypr", return_value="alive"),
              patch.object(camera, "_request", return_value=True),
              patch.object(camera, "_remove_rule")):
            camera._heartbeat()
        self.assertFalse(camera.enabled)
        self.assertIn("disconnected", camera.error)
        camera.process.send_signal.assert_called_once()

    def test_rule_is_scoped_leased_and_uses_monitor_local_coordinates(self):
        camera = CameraBubble((-1920, 0, 1920, 1080))
        monitor = {"name": "DP-2", "x": -1920, "y": 0,
                   "logical_width": 1920, "logical_height": 1080}
        with (patch("lumen.camera.get_monitors", return_value=[monitor]),
              patch.object(camera, "_hypr", return_value="ready") as hypr):
            camera._install_rule()
        script = hypr.call_args.args[0]
        self.assertIn("S.age >= 5", script)
        self.assertIn("pin = true", script)
        self.assertIn(camera.token, script)
        self.assertIn("set_enabled(false)", script)


if __name__ == "__main__":
    unittest.main()
