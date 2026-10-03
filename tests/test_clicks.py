import tempfile
import unittest
from unittest.mock import patch

from lumen.clicks import ClickCapture, available_buttons, click_event, install_lua, read_click_sidecar


class ClickMetadataTests(unittest.TestCase):
    def test_existing_plain_mouse_shortcuts_are_skipped(self):
        bindings = [{"key": "mouse:272", "modmask": 64},
                    {"key": "mouse:273", "modmask": 0},
                    {"key": "mouse:274", "modmask": 4, "ignore_mods": True}]
        self.assertEqual(available_buttons(bindings), [272])

    def test_scoped_lua_never_unbinds_user_shortcuts_or_launches_processes(self):
        lua = install_lua("a" * 32, [272, 273, 274])
        self.assertIn("non_consuming = true", lua)
        self.assertIn('type = "oneshot"', lua)
        self.assertIn("S.age >= 5", lua)
        self.assertIn("b:remove()", lua)
        for forbidden in ("hl.unbind", "exec_cmd", "io.", "os.", "ignore_mods", "locked = true", "input.keyboard"):
            self.assertNotIn(forbidden, lua)

    def test_validates_generated_lua_inputs(self):
        with self.assertRaises(ValueError):
            install_lua('"; os.execute("bad")', [272])
        with self.assertRaises(ValueError):
            install_lua("a" * 32, [30])

    def test_native_event_records_exact_normalized_coordinates_and_style(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = ClickCapture(folder, (200, 100, 800, 600), lambda: 2.25, lambda: True)
            capture._accept_line(f"custom>>lumen_click {capture.token} left 600 250")
            self.assertEqual(len(capture.events), 1)
            event = capture.events[0]
            self.assertEqual((event["t"], event["x"], event["y"]), (2.25, .5, .25))
            self.assertEqual(event["button"], "left")
            self.assertEqual((event["duration"], event["size"], event["color"], event["enabled"]), (.6, .04, "#78c8ff", True))
            self.assertEqual(len(event["id"]), 32)

    def test_paused_unrelated_and_outside_events_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            active = [False]
            capture = ClickCapture(folder, (0, 0, 100, 100), lambda: 1, lambda: active[0])
            capture._accept_line(f"custom>>lumen_click {capture.token} left 50 50")
            active[0] = True
            for line in ("activewindow>>something", "custom>>lumen_click wrong-token left 50 50",
                         f"custom>>lumen_click {capture.token} left 500 50",
                         f"custom>>lumen_click {capture.token} left nan 50",
                         f"custom>>lumen_click {capture.token} keyboard 50 50"):
                capture._accept_line(line)
            self.assertEqual(capture.events, [])

    def test_sidecar_preserves_events_before_project_manifest_save(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = ClickCapture(folder, (0, 0, 100, 100), lambda: 1, lambda: True)
            capture.events.append(click_event(1, .5, .5, "right"))
            capture._save()
            sidecar = read_click_sidecar(folder)
            self.assertEqual(sidecar["clicks"], capture.events)
            capture._installed = True
            with patch.object(capture, "_query") as query:
                capture.stop()
            self.assertIn(f'_lumen_click_{capture.token}', query.call_args.args[0])
            self.assertNotIn("hl.unbind", query.call_args.args[0])

    def test_event_limit_preserves_editable_events_and_reports_dropped_clicks(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = ClickCapture(folder, (0, 0, 100, 100), lambda: 1, lambda: True)
            with patch("lumen.clicks.MAX_CAPTURE_CLICKS", 2):
                for button in ("left", "right", "middle"):
                    capture._accept_line(f"custom>>lumen_click {capture.token} {button} 50 50")
            self.assertEqual([event["button"] for event in capture.events], ["left", "right"])
            self.assertIn("2-click limit", capture.status)
            capture._save()
            self.assertEqual(read_click_sidecar(folder)["click_capture_status"], capture.status)

    def test_outside_or_nonfinite_manual_events_are_rejected(self):
        for t, x, y in ((-1, .5, .5), (1, 1.1, .5), (1, float("nan"), .5)):
            with self.assertRaises(ValueError):
                click_event(t, x, y, "left")


if __name__ == "__main__":
    unittest.main()
