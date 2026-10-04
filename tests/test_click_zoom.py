"""Click camera timing, bounded compilation, and actual rendered geometry."""

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from lumen.click_zoom import CameraState, camera_timeline, compile_click_zoom
from lumen.editor import Exporter, ExportOptions, build_export_command, probe


def click(identifier="first", time=1, x=0.25, y=0.3, **changes):
    return {"id": identifier, "t": time, "x": x, "y": y, **changes}


def state_at(timeline, time):
    return next(
        (
            segment.at(time)
            for segment in timeline
            if segment.start <= time < segment.end
        ),
        CameraState(),
    )


class ClickCameraModelTests(unittest.TestCase):
    def test_sorted_clicks_share_zoom_and_repeated_location_extends_hold(self):
        project = {
            "clicks": [
                click("repeat", 1.8),
                click("pan", 2.5, 0.75, 0.7),
                click(),
                click("disabled", 4, 0.5, 0.5, enabled=False),
            ]
        }
        original = deepcopy(project)
        timeline = camera_timeline(project, duration=6, amount=2, transition=0.5)
        self.assertEqual(state_at(timeline, 0.25).zoom, 1)
        self.assertAlmostEqual(state_at(timeline, 0.75).zoom, 1.5)
        for time in (1.1, 1.7, 1.9, 2.25, 3.6):
            self.assertEqual(state_at(timeline, time).zoom, 2)
        self.assertEqual(state_at(timeline, 1.9).x, 0.25)
        self.assertAlmostEqual(state_at(timeline, 2.25).x, 0.5)
        self.assertEqual(state_at(timeline, 4.5).zoom, 1)
        self.assertEqual(project, original)

    def test_boundary_clamping_simultaneous_clicks_and_late_clicks(self):
        timeline = camera_timeline(
            {"clicks": [click(x=0, y=0), click("last", x=1, y=1), click("late", 9)]},
            duration=4,
            amount=2,
        )
        self.assertEqual(state_at(timeline, 1.5), CameraState(2, 0.75, 0.75))
        self.assertEqual(state_at(timeline, 3.5).zoom, 1)

    def test_ten_thousand_clicks_compile_linearly_and_capped_metadata_rejects(self):
        project = {
            "clicks": [
                click(str(index), index * 0.1, 0.25 if index % 2 else 0.75)
                for index in range(10_000)
            ]
        }
        script = compile_click_zoom(
            project, duration=1002, trim_start=0, trim_end=1002, speed=1
        )
        self.assertLess(len(script), 15_000_000)
        self.assertLessEqual(len(script.splitlines()), 20_005)
        self.assertLess(max(map(len, script.splitlines())), 2000)
        project["clicks"].append(click("too-many", 1001))
        with self.assertRaisesRegex(ValueError, "10,000"):
            camera_timeline(project, duration=1002)

    def test_malformed_clicks_and_camera_settings_reject(self):
        for changes in ({"x": float("nan")}, {"t": -1}, {"enabled": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                camera_timeline({"clicks": [click(**changes)]}, duration=4)
        for changes in (
            {"amount": 5},
            {"hold": 0},
            {"transition": float("nan")},
            {"transition": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                camera_timeline({"clicks": [click()]}, duration=4, **changes)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
)
class ClickCameraRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lumen-click-camera-test-")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "source.mkv"
        # Static spatial gradients encode source X and Y directly in color. The
        # decoded export therefore reveals both camera position and magnification.
        pixels = bytes(
            value
            for y in range(180)
            for x in range(320)
            for value in (round(x * 255 / 319), round(y * 255 / 179), 32)
        )
        image = cls.directory / "grid.ppm"
        image.write_bytes(b"P6\n320 180\n255\n" + pixels)
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-loop",
                "1",
                "-framerate",
                "20",
                "-threads",
                "1",
                "-i",
                str(image),
                "-t",
                "6",
                "-frames:v",
                "120",
                "-c:v",
                "ffv1",
                "-threads",
                "1",
                str(cls.source),
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        cls.project = {
            "path": str(cls.directory),
            "source": "source.mkv",
            "clicks": [
                click(),
                click("second", 2, 0.75, 0.7),
                click("ignored", 4.5, 0.05, 0.05, enabled=False),
            ],
        }
        cls.digest = hashlib.sha256(cls.source.read_bytes()).digest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.outputs = tempfile.TemporaryDirectory(dir=self.directory)
        self.output_dir = Path(self.outputs.name)

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), self.digest)
        self.outputs.cleanup()

    def render(self, project=None, name="zoom.mp4", **changes):
        options = {
            "output_width": 320,
            "background": "none",
            "quality": 0,
            "audio_mode": "none",
            "clicks": False,
            "click_zoom": True,
            "click_zoom_amount": 2,
            "click_zoom_hold": 1,
            "click_zoom_transition": 0.5,
        }
        options.update(changes)
        destination = self.output_dir / name
        Exporter().export(
            project or self.project, ExportOptions(**options), destination
        )
        return destination

    @staticmethod
    def pixels(path, time):
        return subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                str(time),
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-",
            ],
            check=True,
            capture_output=True,
        ).stdout

    @staticmethod
    def rgb(pixels, x, y, width=320):
        offset = (y * width + x) * 3
        return tuple(pixels[offset : offset + 3])

    def camera(self, pixels):
        center = self.rgb(pixels, 160, 90)
        span = self.rgb(pixels, 240, 90)[0] - self.rgb(pixels, 80, 90)[0]
        return 128 / span, center[0] / 255, center[1] / 255

    def assert_camera(self, path, time, zoom, x, y):
        actual = self.camera(self.pixels(path, time))
        for value, expected, tolerance in zip(
            actual, (zoom, x, y), (0.08, 0.025, 0.025)
        ):
            self.assertAlmostEqual(
                value, expected, delta=tolerance, msg=f"t={time}: {actual}"
            )

    def test_actual_pixels_ease_pan_hold_return_and_ignore_disabled_click(self):
        result = self.render()
        self.assert_camera(result, 0.25, 1, 0.5, 0.5)
        # At 1.5× the viewport clamps X/Y to its nearest legal center.
        self.assert_camera(result, 0.75, 1.5, 1 / 3, 1 / 3)
        self.assert_camera(result, 1.25, 2, 0.25, 0.3)
        self.assert_camera(result, 1.75, 2, 0.5, 0.5)
        self.assert_camera(result, 2.25, 2, 0.75, 0.7)
        self.assert_camera(result, 3.25, 1.5, 2 / 3, 2 / 3)
        self.assert_camera(result, 4.5, 1, 0.5, 0.5)

    def test_trim_inside_transition_keeps_original_phase_and_speed(self):
        result = self.render(trim_start=0.75, trim_end=3.75, speed=2)
        self.assertAlmostEqual(probe(result)["duration"], 1.5, delta=0.08)
        self.assert_camera(result, 0, 1.5, 1 / 3, 1 / 3)
        self.assert_camera(result, 0.5, 2, 0.5, 0.5)
        self.assert_camera(result, 0.75, 2, 0.75, 0.7)
        self.assert_camera(result, 1.25, 1.5, 2 / 3, 2 / 3)

    def test_no_enabled_clicks_override_manual_and_manual_still_works(self):
        empty = {**self.project, "clicks": [click(enabled=False)]}
        result = self.render(
            empty,
            trim_end=1,
            zoom=3,
            zoom_start=4,
            zoom_end=2,
        )
        self.assert_camera(result, 0.5, 1, 0.5, 0.5)
        manual = self.render(
            name="manual.mp4",
            trim_end=1,
            click_zoom=False,
            zoom=2,
            zoom_x=0.75,
            zoom_y=0.7,
        )
        self.assert_camera(manual, 0.5, 2, 0.75, 0.7)
        with self.assertRaisesRegex(ValueError, "Zoom interval"):
            self.render(name="invalid.mp4", click_zoom=False, zoom_start=4, zoom_end=2)

    def test_edges_preserve_dimensions_and_fill_entire_frame(self):
        project = {**self.project, "clicks": [click(x=0, y=1)]}
        result = self.render(project, trim_start=1, trim_end=1.5, output_width=480)
        media = probe(result)
        self.assertEqual((media["width"], media["height"]), (480, 270))
        pixels = self.pixels(result, 0.2)
        center = self.rgb(pixels, 240, 135, width=480)
        self.assertAlmostEqual(center[0] / 255, 0.25, delta=0.02)
        self.assertAlmostEqual(center[1] / 255, 0.75, delta=0.02)
        self.assertGreater(self.rgb(pixels, 479, 269, width=480)[1], 240)

    def test_annotations_follow_camera_and_captions_stay_on_canvas(self):
        project = {
            **self.project,
            "annotations": [
                {
                    "id": "box",
                    "kind": "box",
                    "start": 0,
                    "end": 6,
                    "x": 0.2,
                    "y": 0.25,
                    "x2": 0.3,
                    "y2": 0.35,
                    "color": "#0000ff",
                    "size": 0.08,
                }
            ],
            "captions": [{"id": "caption", "start": 0, "end": 6, "text": "STAYS HERE"}],
        }
        result = self.render(
            project, output_width=400, background="midnight", padding=40
        )

        def bounds(pixels, match):
            locations = [
                (index % 400, index // 400)
                for index in range(len(pixels) // 3)
                if match(*pixels[index * 3 : index * 3 + 3], index // 400)
            ]
            self.assertTrue(locations)
            return (
                min(x for x, _ in locations),
                max(x for x, _ in locations),
                min(y for _, y in locations),
                max(y for _, y in locations),
            )

        neutral, focused = self.pixels(result, 0.25), self.pixels(result, 1.25)

        def blue(r, g, b, y):
            return b > 200 and r < 80 and g < 80

        normal_box, focused_box = bounds(neutral, blue), bounds(focused, blue)
        self.assertAlmostEqual((normal_box[0] + normal_box[1]) / 2, 120, delta=3)
        self.assertAlmostEqual((focused_box[0] + focused_box[1]) / 2, 200, delta=3)
        self.assertAlmostEqual((focused_box[2] + focused_box[3]) / 2, 130, delta=3)
        self.assertGreater(
            focused_box[1] - focused_box[0], 1.7 * (normal_box[1] - normal_box[0])
        )

        def white(r, g, b, y):
            return min(r, g, b) > 225 and y > 220

        self.assertEqual(bounds(neutral, white), bounds(focused, white))

    def test_camera_script_path_escaping_and_constant_argument_size(self):
        scratch = self.output_dir / "camera's space: [x],;files"
        result = self.output_dir / "special.mp4"
        options = ExportOptions(
            click_zoom=True,
            clicks=False,
            output_width=320,
            background="none",
            trim_end=1.5,
        )
        with self.assertRaisesRegex(ValueError, "overlay_dir"):
            build_export_command(self.project, options, result)
        command = build_export_command(
            self.project, options, result, overlay_dir=scratch
        )
        subprocess.run(command, check=True, capture_output=True)
        self.assert_camera(result, 1.2, 1.8, 1 / 3.6, 0.3)
        many = {
            **self.project,
            "clicks": [
                click(str(i), i / 2000, x=0.25 if i % 2 else 0.75)
                for i in range(10_000)
            ],
        }
        options.trim_end = 6
        command_many = build_export_command(
            many,
            options,
            self.output_dir / "many.mp4",
            overlay_dir=self.output_dir / "many",
        )
        self.assertLess(sum(map(len, command_many)), 2000)
        self.assertGreater(
            (self.output_dir / "many" / "lumen-camera.cmd").stat().st_size, 3_000_000
        )
        # Even with dense events, FFmpeg parses and renders the bounded script.
        subprocess.run(command_many, check=True, capture_output=True, timeout=30)


if __name__ == "__main__":
    unittest.main()
