"""Real image imports, portability, and malformed gallery recovery."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from lumen.project import create_project, load_project, save_project
from lumen.wallpapers import (
    BUILTIN_WALLPAPERS, custom_wallpapers, import_wallpaper, wallpaper_path,
)


class WallpaperCatalogTests(unittest.TestCase):
    def test_all_bundled_images_are_present(self):
        for name in BUILTIN_WALLPAPERS:
            path = wallpaper_path({}, "builtin:" + name)
            self.assertGreater(path.stat().st_size, 1000)

    def test_malformed_gallery_retains_valid_and_missing_entries(self):
        valid = {"path": "wallpapers/missing.jpg", "name": "My image"}
        project = {"wallpapers": [None, {}, valid, valid, {"path": "wallpapers/../source.mkv"}]}
        self.assertEqual(custom_wallpapers(project), [valid])
        self.assertEqual(custom_wallpapers({"wallpapers": None}), [])

    def test_invalid_or_external_selections_are_rejected(self):
        for selection in (None, "", "builtin:nope", "/tmp/image.jpg", "../image.jpg", "wallpapers/../source.mkv"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                wallpaper_path({"path": "/tmp/project"}, selection)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class WallpaperImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lumen-wallpapers-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = create_project(self.root / "library")

    def fixture(self, suffix):
        image = self.root / ("My wallpaper ' [blue]" + suffix)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=120x80", "-frames:v", "1", str(image)],
            check=True, capture_output=True,
        )
        return image

    def test_png_jpeg_webp_imports_are_private_stills_and_deduplicated(self):
        for suffix in (".png", ".jpg", ".webp"):
            source = self.fixture(suffix)
            original = source.read_bytes()
            entry = import_wallpaper(self.project, source)
            path = wallpaper_path(self.project, entry["path"])
            self.assertEqual(path.suffix, ".jpg")
            self.assertEqual(entry["name"], source.stem)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(import_wallpaper(self.project, source), entry)
            self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(path.parent.glob(".import-*")))

    def test_import_survives_original_deletion_and_project_move(self):
        source = self.fixture(".png")
        item = import_wallpaper(self.project, source)
        self.project.update(wallpapers=[item], edits={"background": "wallpaper", "wallpaper": item["path"]})
        save_project(self.project)
        source.unlink()
        moved = self.root / "moved take"
        Path(self.project["path"]).rename(moved)
        project = load_project(moved)
        self.assertTrue(wallpaper_path(project, project["edits"]["wallpaper"]).is_file())
        self.assertEqual(custom_wallpapers(project), [item])

    def test_invalid_and_oversized_import_leave_gallery_and_manifest_unchanged(self):
        manifest = Path(self.project["path"]) / "project.json"
        before = manifest.read_bytes()
        corrupt = self.root / "corrupt.png"
        corrupt.write_bytes(b"Not an image")
        with self.assertRaises(ValueError):
            import_wallpaper(self.project, corrupt)
        huge = self.root / "huge.png"
        with huge.open("wb") as stream:
            stream.truncate(50 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "50 MiB"):
            import_wallpaper(self.project, huge)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse((manifest.parent / "wallpapers").exists())

    def test_symlink_outside_project_cannot_be_used_as_wallpaper(self):
        source = self.fixture(".png")
        folder = Path(self.project["path"]) / "wallpapers"
        folder.mkdir()
        os.symlink(source, folder / "outside.png")
        with self.assertRaises(ValueError):
            wallpaper_path(self.project, "wallpapers/outside.png")
