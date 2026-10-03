"""Timed layer model and real libass/FFmpeg rendering regressions."""

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from lumen.editor import Exporter, ExportOptions, build_export_command, probe
from lumen.overlays import compile_overlays, escape_ass_text, validate_project_overlays


def cue(**changes):
    return {
        "id": "caption-1",
        "start": 1.0,
        "end": 3.0,
        "text": "A useful caption",
        **changes,
    }


def annotation(**changes):
    return {
        "id": "annotation-1",
        "start": 1.0,
        "end": 3.0,
        "kind": "highlight",
        "text": "",
        "x": 0.55,
        "y": 0.4,
        "x2": 0.65,
        "y2": 0.6,
        "color": "#ff0000",
        "size": 0.045,
        "enabled": True,
        **changes,
    }


def click(**changes):
    return {
        "id": "click-1",
        "t": 1.0,
        "x": 0.5,
        "y": 0.5,
        "button": "left",
        "duration": 1.0,
        "size": 0.15,
        "color": "#ff0000",
        "enabled": True,
        **changes,
    }


def compile_project(project, **changes):
    args = {
        "source_width": 320,
        "source_height": 180,
        "canvas_width": 400,
        "canvas_height": 260,
        "trim_start": 0.5,
        "trim_end": 3.5,
        "speed": 2,
    }
    args.update(changes)
    return compile_overlays(project, **args)


class OverlayModelTests(unittest.TestCase):
    def test_times_are_clipped_shifted_and_sped_up(self):
        scripts = compile_project(
            {"captions": [cue(start=0, end=2), cue(id="outside", start=4, end=5)]}
        )
        self.assertIn("0:00:00.00,0:00:00.75", scripts.captions)
        self.assertEqual(scripts.captions.count("Dialogue:"), 1)

    def test_literal_text_cannot_insert_commands_or_events(self):
        text = "{\\pos(0,0)\\fs400}\\N\nDialogue: 0,fake\nUnicode café ✓"
        escaped = escape_ass_text(text)
        self.assertNotIn("\n", escaped)
        self.assertIn(r"\{", escaped)
        self.assertIn("\\\u2060N", escaped)
        scripts = compile_project({"captions": [cue(text=text)]})
        self.assertEqual(
            sum(line.startswith("Dialogue:") for line in scripts.captions.splitlines()),
            1,
        )
        self.assertIn("café ✓", scripts.captions)

    def test_disabled_and_offscreen_timed_layers_compile_to_no_work(self):
        project = {
            "captions": [cue(enabled=False)],
            "annotations": [annotation(start=8, end=9)],
            "clicks": [click(enabled=False)],
        }
        scripts = compile_project(project)
        self.assertIsNone(scripts.source)
        self.assertIsNone(scripts.captions)
        visible = {
            "captions": [cue()],
            "annotations": [annotation()],
            "clicks": [click()],
        }
        scripts = compile_project(
            visible, captions=False, annotations=False, clicks=False
        )
        self.assertIsNone(scripts.source)
        self.assertIsNone(scripts.captions)

    def test_click_trim_preserves_animation_phase(self):
        scripts = compile_project({"clicks": [click()]}, trim_start=1.4, trim_end=3)
        self.assertIn(r"\fscx61\fscy61", scripts.source)
        self.assertIn(r"\3a&H66&", scripts.source)
        self.assertIn("0:00:00.00,0:00:00.30", scripts.source)

    def test_invalid_models_are_actionable_and_original_is_unchanged(self):
        bad = [
            {"captions": [cue(start=3, end=2)]},
            {"captions": [cue(text="")]},
            {"captions": [cue(), cue()]},
            {"annotations": [annotation(x=float("nan"))]},
            {"annotations": [annotation(color="red,malicious")]},
            {"annotations": [annotation(x2=0.55)]},
            {"clicks": [click(enabled="false")]},
            {"clicks": [click(button="unknown")]},
            {"clicks": [click(duration=0)]},
            {"caption_style": {"font_size": 0.5}},
        ]
        for project in bad:
            with self.subTest(project=project), self.assertRaises(ValueError):
                validate_project_overlays(project)
        original = {
            "captions": [cue()],
            "annotations": [annotation()],
            "clicks": [click()],
        }
        saved = deepcopy(original)
        compile_project(original)
        self.assertEqual(original, saved)


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
)
class OverlayRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lumen-overlays-test-")
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / "source.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x101010:s=320x180:r=20:d=4",
                "-c:v",
                "ffv1",
                str(cls.source),
            ],
            check=True,
            capture_output=True,
        )
        cls.base = {
            "path": str(cls.root),
            "source": "source.mkv",
            "duration": 4.0,
            "width": 320,
            "height": 180,
            "fps": 20,
        }
        cls.original_hash = hashlib.sha256(cls.source.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temporary_output = tempfile.TemporaryDirectory(dir=self.root)
        self.output = Path(self.temporary_output.name)

    def tearDown(self):
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash
        )
        self.temporary_output.cleanup()

    def render(self, layers, name="render.mp4", **changes):
        defaults = {
            "output_width": 320,
            "background": "none",
            "audio_mode": "none",
            "quality": 0,
            "fps": 20,
        }
        defaults.update(changes)
        path = self.output / name
        Exporter().export(dict(self.base, **layers), ExportOptions(**defaults), path)
        return path

    @staticmethod
    def pixels(path, moment=0):
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(moment),
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
        )
        return result.stdout

    @staticmethod
    def colored_pixels(data, width, color="red"):
        selected = []
        for i in range(0, len(data), 3):
            r, g, b = data[i : i + 3]
            if (
                color == "red"
                and r > 40
                and r > g * 1.5
                and r > b * 1.5
                or color == "white"
                and min(r, g, b) > 100
            ):
                selected.append(((i // 3) % width, (i // 3) // width))
        return selected

    def test_caption_timing_survives_trim_and_speed(self):
        output = self.render(
            {
                "captions": [cue(text="Caption with {braces} and \\N")],
                "caption_style": {"font_size": 0.1},
            },
            trim_start=0.5,
            trim_end=3.5,
            speed=2,
        )
        self.assertFalse(self.colored_pixels(self.pixels(output, 0.1), 320, "white"))
        self.assertGreater(
            len(self.colored_pixels(self.pixels(output, 0.6), 320, "white")), 80
        )
        self.assertFalse(self.colored_pixels(self.pixels(output, 1.4), 320, "white"))
        self.assertAlmostEqual(probe(output)["duration"], 1.5, delta=0.05)

    def test_caption_is_on_final_frame_and_text_cannot_reposition_it(self):
        layers = {
            "captions": [cue(text=r"{\pos(0,0)\fs400}Literal text")],
            "caption_style": {"font_size": 0.07, "background": True},
        }
        plain = self.render(
            {}, "plain.mp4", output_width=400, background="midnight", padding=40, zoom=2
        )
        captioned = self.render(
            layers,
            "captioned.mp4",
            output_width=400,
            background="midnight",
            padding=40,
            zoom=2,
        )
        a, b = self.pixels(plain, 2), self.pixels(captioned, 2)
        changed = [
            ((i // 3) % 400, (i // 3) // 400)
            for i in range(0, len(a), 3)
            if max(abs(a[i + j] - b[i + j]) for j in range(3)) > 20
        ]
        self.assertGreater(len(changed), 100)
        self.assertGreater(min(y for x, y in changed), 180)
        self.assertGreater(max(y for x, y in changed), 220)

    def test_annotations_follow_zoom_in_source_coordinates(self):
        layers = {"annotations": [annotation()]}
        normal = self.render(layers, "normal.mp4")
        zoomed = self.render(layers, "zoomed.mp4", zoom=2)
        a = self.colored_pixels(self.pixels(normal, 2), 320)
        b = self.colored_pixels(self.pixels(zoomed, 2), 320)
        self.assertGreater(len(a), 100)
        self.assertGreater(len(b), len(a) * 2)
        self.assertAlmostEqual(sum(x for x, y in a) / len(a), 192, delta=3)
        self.assertAlmostEqual(sum(x for x, y in b) / len(b), 224, delta=4)
        self.assertFalse(self.colored_pixels(self.pixels(normal, 0.3), 320))

    def test_arrow_box_and_text_all_render(self):
        items = [
            annotation(kind="arrow", x=0.1, y=0.7, x2=0.7, y2=0.7),
            annotation(id="box", kind="box", x=0.1, y=0.1, x2=0.3, y2=0.3),
            annotation(id="text", kind="text", text="Hi", x=0.7, y=0.1, size=0.15),
        ]
        output = self.render({"annotations": items})
        colored = self.colored_pixels(self.pixels(output, 2), 320)
        self.assertTrue(any(x > 210 and y > 115 for x, y in colored))
        self.assertTrue(any(x < 100 and y < 60 for x, y in colored))
        self.assertTrue(any(x > 225 and y < 60 for x, y in colored))

    def test_click_ring_expands_fades_and_ends(self):
        output = self.render({"clicks": [click()]})
        early = self.colored_pixels(self.pixels(output, 1.1), 320)
        later = self.colored_pixels(self.pixels(output, 1.6), 320)
        self.assertGreater(len(early), 10)
        self.assertGreater(len(later), 10)
        early_radius = max(abs(x - 160) for x, y in early)
        late_radius = max(abs(x - 160) for x, y in later)
        self.assertGreater(late_radius, early_radius + 4)
        self.assertFalse(self.colored_pixels(self.pixels(output, 2.2), 320))
        trimmed = self.render(
            {"clicks": [click()]}, "trimmed.mp4", trim_start=1.4, trim_end=3, speed=2
        )
        active = self.colored_pixels(self.pixels(trimmed, 0), 320)
        self.assertGreater(max(abs(x - 160) for x, y in active), 14)

    def test_disabled_layers_are_absent_and_gif_supports_layers(self):
        layers = {
            "annotations": [annotation()],
            "captions": [cue()],
            "clicks": [click()],
        }
        disabled = self.render(layers, captions=False, annotations=False, clicks=False)
        self.assertFalse(self.colored_pixels(self.pixels(disabled, 1.3), 320))
        gif = self.render(
            {"annotations": [annotation()]}, "layer.gif", format="gif", trim_end=2
        )
        self.assertEqual(gif.read_bytes()[:6], b"GIF89a")
        self.assertGreater(len(self.colored_pixels(self.pixels(gif, 1.5), 320)), 100)

    def test_build_command_requires_explicit_scratch_and_escapes_special_paths(self):
        project = dict(self.base, captions=[cue()])
        destination = self.output / "out.mp4"
        with self.assertRaisesRegex(ValueError, "overlay_dir"):
            build_export_command(project, ExportOptions(), destination)
        scratch = self.output / "colon: comma, quote' [brackets];"
        command = build_export_command(
            project,
            ExportOptions(output_width=320, background="none"),
            destination,
            overlay_dir=scratch,
        )
        self.assertTrue(list(scratch.glob("*.ass")))
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        self.assertEqual(probe(destination)["width"], 320)


if __name__ == "__main__":
    unittest.main()
