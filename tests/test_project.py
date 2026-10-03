import json
import os
from datetime import datetime, timezone
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from lumen import project


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "LUMEN_LIBRARY": str(self.root / "library"),
                "XDG_CONFIG_HOME": str(self.root / "config"),
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_new_project_round_trip_and_private_permissions(self):
        created = project.create_project()
        path = Path(created["path"])
        self.assertTrue(path.is_absolute())
        self.assertEqual(path.parent, self.root / "library")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((path / "project.json").stat().st_mode), 0o600)
        self.assertEqual(created["source"], "source.mkv")
        self.assertEqual(created["fps"], 60)
        self.assertEqual(project.load_project(path), created)
        self.assertEqual(project.load_project(path / "project.json"), created)

    def test_collision_retries_without_overwriting(self):
        with (
            mock.patch.object(project, "datetime", wraps=datetime) as clock,
            mock.patch.object(
                project.uuid,
                "uuid4",
                side_effect=[
                    SimpleNamespace(hex="a" * 32),
                    SimpleNamespace(hex="a" * 32),
                    SimpleNamespace(hex="b" * 32),
                ],
            ) as identifiers,
        ):
            clock.now.return_value = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
            first = project.create_project()
            second = project.create_project()
            self.assertEqual(identifiers.call_count, 3)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(len(project.list_projects()), 2)

    def test_editor_metadata_survives_and_relocated_path_is_corrected(self):
        item = project.create_project()
        item.update(
            {"name": "Release demo", "editor": {"trim": [1.5, 7], "zoom": 1.25}}
        )
        project.save_project(item)
        moved = self.root / "moved"
        Path(item["path"]).rename(moved)
        loaded = project.load_project(moved)
        self.assertEqual(loaded["path"], str(moved))
        self.assertEqual(loaded["name"], "Release demo")
        self.assertEqual(loaded["editor"], item["editor"])

    def test_library_skips_broken_manifests(self):
        good = project.create_project()
        root = Path(good["path"]).parent
        for name, content in (
            ("bad-json", "{"),
            ("array", "[]"),
            ("null", "null"),
            ("unicode", "\udcff"),
        ):
            folder = root / name
            folder.mkdir()
            (folder / "project.json").write_bytes(
                content.encode("utf-8", errors="surrogateescape")
            )
        (root / "empty").mkdir()
        (root / "stray.txt").write_text("not a project")
        self.assertEqual(project.list_projects(), [good])

    def test_known_bad_metadata_types_are_safe_and_unknown_fields_preserved(self):
        folder = self.root / "weird"
        folder.mkdir()
        metadata = {
            "name": [],
            "source": None,
            "status": 42,
            "created_at": 33,
            "duration": "oops",
            "width": -10,
            "height": True,
            "fps": float("nan"),
            "future_field": {"keep": True},
        }
        (folder / "project.json").write_text(json.dumps(metadata))
        loaded = project.load_project(folder)
        self.assertEqual(loaded["name"], "weird")
        self.assertEqual(loaded["source"], "source.mkv")
        self.assertEqual(loaded["status"], "new")
        self.assertEqual(
            (loaded["duration"], loaded["width"], loaded["height"], loaded["fps"]),
            (0, 0, 0, 60),
        )
        self.assertEqual(loaded["future_field"], {"keep": True})
        self.assertIsInstance(loaded["created_at"], str)

    def test_sorting_uses_actual_time_across_timezones(self):
        older = project.create_project()
        older["created_at"] = "2026-10-02T13:00:00+03:00"
        project.save_project(older)
        newer = project.create_project()
        newer["created_at"] = "2026-10-02T11:00:00Z"
        project.save_project(newer)
        invalid = project.create_project()
        invalid["created_at"] = "not a date"
        project.save_project(invalid)
        self.assertEqual(
            [p["path"] for p in project.list_projects()],
            [newer["path"], older["path"], invalid["path"]],
        )

    def test_failed_atomic_replace_preserves_original_and_cleans_tempfile(self):
        item = project.create_project()
        manifest = Path(item["path"]) / "project.json"
        original = manifest.read_bytes()
        item["name"] = "A change that must not partially save"
        with mock.patch.object(
            project.os, "replace", side_effect=OSError("simulated disk failure")
        ):
            with self.assertRaises(OSError):
                project.save_project(item)
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual(list(manifest.parent.iterdir()), [manifest])

    def test_serialization_failure_preserves_original(self):
        item = project.create_project()
        manifest = Path(item["path"]) / "project.json"
        original = manifest.read_bytes()
        item["duration"] = float("nan")
        with self.assertRaises(ValueError):
            project.save_project(item)
        self.assertEqual(manifest.read_bytes(), original)

    def test_long_recording_clicks_and_captions_roundtrip_beyond_one_mib(self):
        item = project.create_project()
        item["clicks"] = [
            {"id": f"click-{index}", "t": index / 2, "x": .123456, "y": .654321,
             "button": "left", "duration": .6, "size": .04, "color": "#78c8ff", "enabled": True}
            for index in range(3000)
        ]
        item["captions"] = [
            {"id": f"caption-{index}", "start": index * 3, "end": index * 3 + 2.5,
             "text": "A detailed spoken explanation remains editable in this recording. " * 8}
            for index in range(1000)
        ]
        project.save_project(item)
        manifest = Path(item["path"]) / "project.json"
        self.assertGreater(manifest.stat().st_size, 1024 * 1024)
        loaded = project.load_project(manifest)
        self.assertEqual(loaded["clicks"], item["clicks"])
        self.assertEqual(loaded["captions"], item["captions"])
        self.assertEqual(project.list_projects()[0]["path"], item["path"])

    def test_settings_roundtrip_and_invalid_settings_fallback(self):
        self.assertEqual(project.load_settings(), {})
        settings = {"fps": 60, "audio": "both", "custom": {"keep": True}}
        project.save_settings(settings)
        self.assertEqual(project.load_settings(), settings)
        path = self.root / "config" / "lumen" / "settings.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        path.write_text("[]")
        self.assertEqual(project.load_settings(), {})
        with self.assertRaises(ValueError):
            project.save_settings([])

    def test_explicit_root_wins_over_environment(self):
        custom = self.root / "custom"
        created = project.create_project(custom)
        self.assertEqual(Path(created["path"]).parent, custom)
        self.assertEqual(project.list_projects(), [])
        self.assertEqual(project.list_projects(custom), [created])

    def test_xdg_video_folder_used_when_no_override(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(project.subprocess, "run") as run,
        ):
            run.return_value = SimpleNamespace(
                returncode=0, stdout=str(self.root / "Videos in XDG") + "\n"
            )
            self.assertEqual(
                project.library_root(), self.root / "Videos in XDG" / "Lumen"
            )
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
