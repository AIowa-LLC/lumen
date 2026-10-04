"""Real FFmpeg integration tests; no display server or audio devices needed."""

import hashlib
import json
import math
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from lumen.editor import (
    ExportCancelled,
    Exporter,
    ExportOptions,
    _audio_indices,
    audio_modes,
    build_export_command,
    probe,
    suggest_zoom,
    thumbnail,
)
from lumen.wallpapers import BUILTIN_WALLPAPERS, import_wallpaper


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required"
)
class EditorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lumen-editor-test-")
        cls.directory = Path(cls.temporary.name)
        cls.source = cls.directory / "source.mkv"
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
                "testsrc2=size=320x180:rate=20:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000:duration=4",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=880:sample_rate=48000:duration=4",
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-map",
                "2:a",
                "-c:v",
                "ffv1",
                "-c:a",
                "pcm_s16le",
                "-metadata:s:a:0",
                "title=desktop",
                "-metadata:s:a:1",
                "title=mic",
                str(cls.source),
            ],
            check=True,
            capture_output=True,
        )
        cls.project = {
            "path": str(cls.directory),
            "source": "source.mkv",
            "duration": 4.0,
            "width": 320,
            "height": 180,
            "fps": 20,
            "audio_tracks": {"desktop": 0, "mic": 1},
        }
        cls.source_digest = hashlib.sha256(cls.source.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.outputs = tempfile.TemporaryDirectory(dir=self.directory)
        self.output_dir = Path(self.outputs.name)

    def tearDown(self):
        self.assertEqual(
            hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_digest
        )
        self.outputs.cleanup()

    def render(self, name="test.mp4", **options):
        path = self.output_dir / name
        Exporter().export(
            self.project, ExportOptions(output_width=320, padding=16, **options), path
        )
        return path

    @staticmethod
    def pixels(path, time=0):
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
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
        )
        return result.stdout

    @staticmethod
    def frequencies(path):
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vn",
                "-t",
                "0.8",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        samples = struct.unpack("<" + "h" * (len(result.stdout) // 2), result.stdout)

        def amplitude(hz):
            real = sum(
                v * math.cos(2 * math.pi * hz * i / 8000) for i, v in enumerate(samples)
            )
            imag = sum(
                v * math.sin(2 * math.pi * hz * i / 8000) for i, v in enumerate(samples)
            )
            return math.hypot(real, imag) / len(samples)

        return amplitude(440), amplitude(880)

    def test_probe_and_thumbnail(self):
        media = probe(self.source)
        self.assertEqual(
            (media["width"], media["height"], media["fps"]), (320, 180, 20)
        )
        self.assertAlmostEqual(media["duration"], 4, places=2)
        self.assertEqual(len(media["audio_streams"]), 2)
        preview = thumbnail(
            self.source, self.output_dir / "thumb.png", time=1.5, width=160
        )
        self.assertEqual(preview.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(probe(preview)["width"], 160)

    def test_trim_speed_frame_and_progress(self):
        progress = []
        destination = self.output_dir / "framed.mp4"
        Exporter().export(
            self.project,
            ExportOptions(
                trim_start=1,
                trim_end=3,
                speed=2,
                output_width=640,
                padding=32,
                background="violet",
            ),
            destination,
            on_progress=progress.append,
        )
        media = probe(destination)
        self.assertEqual((media["width"], media["height"]), (640, 388))
        self.assertAlmostEqual(media["duration"], 1, delta=0.12)
        self.assertEqual(len(media["audio_streams"]), 1)
        self.assertEqual(progress[0], 0.0)
        self.assertEqual(progress[-1], 1.0)
        self.assertTrue(all(0 <= value <= 1 for value in progress))
        pixels = self.pixels(destination)
        first_pixel = pixels[:3]
        # Violet gradient has a purple top-left corner, outside the video.
        self.assertGreater(first_pixel[2], first_pixel[1])
        self.assertGreater(first_pixel[0], first_pixel[1])

    def test_audio_selection_and_mix(self):
        desktop = self.render(
            "desktop.mp4", audio_mode="desktop", trim_end=1, background="none"
        )
        microphone = self.render(
            "mic.mp4", audio_mode="mic", trim_end=1, background="none"
        )
        mixed = self.render("mix.mp4", audio_mode="mix", trim_end=1, background="none")
        d440, d880 = self.frequencies(desktop)
        m440, m880 = self.frequencies(microphone)
        x440, x880 = self.frequencies(mixed)
        self.assertGreater(d440, d880 * 10)
        self.assertGreater(m880, m440 * 10)
        self.assertGreater(x440, 500)
        self.assertGreater(x880, 500)
        silent = self.render(
            "silent.mp4", audio_mode="none", trim_end=1, background="none"
        )
        self.assertEqual(probe(silent)["audio_streams"], [])

    def test_bundled_wallpapers_render_in_mp4_and_gif(self):
        for name in BUILTIN_WALLPAPERS:
            path = self.render(
                name + ".mp4", background="wallpaper", wallpaper="builtin:" + name,
                trim_start=1, trim_end=3, speed=2,
            )
            media = probe(path)
            self.assertEqual((media["width"], media["height"]), (320, 194))
            self.assertAlmostEqual(media["duration"], 1, delta=.1)
            self.assertEqual(len(media["audio_streams"]), 1)
            self.assertEqual(len(self.pixels(path)), 320 * 194 * 3)
        gif = self.render(
            "wallpaper.gif", format="gif", background="wallpaper",
            wallpaper="builtin:aurora", trim_end=.5, fps=10,
        )
        self.assertEqual(probe(gif)["audio_streams"], [])
        self.assertEqual(len(self.pixels(gif)), 320 * 194 * 3)

    def test_custom_wallpapers_center_crop_landscape_and_portrait(self):
        for width, height in ((600, 100), (100, 600)):
            # Wide/portrait images with red/blue edges and a green center.
            # Cover-cropping shows green at every corner; stretching shows red.
            ppm = self.output_dir / "stripes.ppm"
            data = bytearray()
            for y in range(height):
                for x in range(width):
                    fraction = x / width if width > height else y / height
                    data.extend((240, 20, 20) if fraction < 1/3 else (20, 210, 60) if fraction < 2/3 else (20, 20, 240))
            ppm.write_bytes(f"P6\n{width} {height}\n255\n".encode() + data)
            image = self.output_dir / f"portrait {height} ' [image].png"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(ppm), str(image)], check=True, capture_output=True)
            item = import_wallpaper(self.project, image)
            image.unlink()
            output = self.render(
                f"crop-{height}.mp4", background="wallpaper", wallpaper=item["path"],
                trim_end=.4, audio_mode="none",
            )
            pixels = self.pixels(output)
            for index in (0, 319, 320 * 193, 320 * 194 - 1):
                red, green, blue = pixels[index*3:index*3+3]
                self.assertGreater(green, 180)
                self.assertLess(red, 45)
                self.assertLess(blue, 85)

    def test_missing_wallpaper_cannot_publish_export(self):
        destination = self.output_dir / "missing.mp4"
        with self.assertRaisesRegex(ValueError, "missing"):
            Exporter().export(
                self.project,
                ExportOptions(background="wallpaper", wallpaper="wallpapers/missing.jpg"),
                destination,
            )
        self.assertFalse(destination.exists())

    def test_no_frame_or_zero_padding_does_not_require_wallpaper(self):
        for background, padding in (("none", 64), ("wallpaper", 0)):
            destination = self.output_dir / (background + ".mp4")
            Exporter().export(
                self.project,
                ExportOptions(background=background, padding=padding, wallpaper="wallpapers/missing.jpg", output_width=320, trim_end=.4),
                destination,
            )
            self.assertEqual((probe(destination)["width"], probe(destination)["height"]), (320, 180))

    def test_timed_zoom_changes_only_focus_interval(self):
        baseline = self.render(
            "baseline.mp4", background="none", quality=0, audio_mode="none"
        )
        focused = self.render(
            "zoom.mp4",
            background="none",
            quality=0,
            audio_mode="none",
            zoom=2,
            zoom_x=0.7,
            zoom_y=0.4,
            zoom_start=1,
            zoom_end=3,
        )
        for moment in (0.2, 3.5):
            a, b = self.pixels(baseline, moment), self.pixels(focused, moment)
            self.assertEqual(len(a), 320 * 180 * 3)
            self.assertEqual(len(a), len(b))
            self.assertLess(sum(abs(x - y) for x, y in zip(a, b)) / len(a), 3)
        a, b = self.pixels(baseline, 2), self.pixels(focused, 2)
        self.assertGreater(sum(abs(x - y) for x, y in zip(a, b)) / len(a), 25)
        self.assertAlmostEqual(probe(focused)["duration"], 4, delta=0.1)

    def test_gif_is_animated_and_silent(self):
        gif = self.render(
            "animated.gif",
            format="gif",
            background="sand",
            trim_end=0.75,
            fps=12,
            zoom=1.3,
        )
        self.assertEqual(gif.read_bytes()[:6], b"GIF89a")
        media = probe(gif)
        self.assertEqual(media["audio_streams"], [])
        self.assertGreater(media["duration"], 0.5)
        self.assertEqual(media["width"], 320)

    def test_cancel_does_not_publish_partial_file(self):
        exporter = Exporter()
        target = self.output_dir / "cancelled.mp4"
        with self.assertRaises(ExportCancelled):
            exporter.export(
                self.project,
                ExportOptions(output_width=320),
                target,
                on_progress=lambda _: exporter.cancel(),
            )
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_cancel_before_worker_starts_is_honored(self):
        exporter = Exporter()
        exporter.cancel()
        target = self.output_dir / "cancel-before-start.mp4"
        with self.assertRaises(ExportCancelled):
            exporter.export(self.project, ExportOptions(output_width=320), target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_cancel_while_ffmpeg_is_running(self):
        exporter = Exporter()
        target = self.output_dir / "cancelled-running.mp4"
        callbacks = []

        def progress(value):
            callbacks.append(value)
            if len(callbacks) > 1:
                exporter.cancel()

        with self.assertRaises(ExportCancelled):
            exporter.export(
                self.project,
                ExportOptions(output_width=1280, zoom=1.5),
                target,
                on_progress=progress,
            )
        self.assertGreater(len(callbacks), 1)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_source_and_existing_destination_are_protected(self):
        with self.assertRaises(ValueError):
            build_export_command(self.project, ExportOptions(), self.source)
        alias = self.output_dir / "alias.mp4"
        alias.symlink_to(self.source)
        with self.assertRaises(ValueError):
            Exporter().export(self.project, ExportOptions(), alias)
        existing = self.output_dir / "existing.mp4"
        existing.write_bytes(b"keep this")
        with self.assertRaises(FileExistsError):
            Exporter().export(self.project, ExportOptions(), existing)
        self.assertEqual(existing.read_bytes(), b"keep this")

    def test_invalid_options_are_rejected(self):
        invalid = [
            ExportOptions(trim_start=3, trim_end=2),
            ExportOptions(speed=0),
            ExportOptions(zoom=float("nan")),
            ExportOptions(trim_end=8),
            ExportOptions(zoom_start=3, zoom_end=1),
            ExportOptions(background="untrusted,filter"),
        ]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ValueError):
                build_export_command(
                    self.project, options, self.output_dir / "invalid.mp4"
                )

    def test_missing_audio_track_is_reported_instead_of_silent_export(self):
        project = dict(self.project, audio_tracks={"desktop": 0})
        self.assertEqual(audio_modes(project), {"mix", "desktop", "none"})
        with self.assertRaisesRegex(ValueError, "no separate microphone"):
            build_export_command(
                project, ExportOptions(audio_mode="mic"), self.output_dir / "absent.mp4"
            )


class CursorSuggestionTests(unittest.TestCase):
    def test_explicit_mic_only_mapping_never_becomes_desktop_audio(self):
        media = {"audio_streams": [{}]}
        project = {"audio_tracks": {"mic": 0}}
        self.assertEqual(_audio_indices(project, media, "mic"), [0])
        self.assertEqual(_audio_indices(project, media, "desktop"), [])
        self.assertEqual(_audio_indices(project, media, "mix"), [0])

    def test_longest_valid_dwell_and_missing_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            project = {"path": directory, "duration": 10}
            self.assertIsNone(suggest_zoom(project))
            samples = [{"t": i / 10, "x": 0.6, "y": 0.4} for i in range(20, 41)]
            (Path(directory) / "cursor.json").write_text(
                json.dumps({"samples": samples})
            )
            result = suggest_zoom(project)
            self.assertAlmostEqual(result["zoom_x"], 0.6)
            self.assertAlmostEqual(result["zoom_y"], 0.4)
            self.assertAlmostEqual(result["zoom_start"], 1.6)
            self.assertAlmostEqual(result["zoom_end"], 4.5)


if __name__ == "__main__":
    unittest.main()
