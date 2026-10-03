"""Recovery preserves rejected edits without committing or replacing media."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lumen import recovery
from lumen.project import create_project, load_project, save_project


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = create_project(temporary.name)
        self.project.update(
            duration=10,
            edits={"zoom": 1.8},
            captions=[{"id": "one", "start": 1, "end": 2, "text": "Saved caption"}],
        )
        save_project(self.project)
        self.folder = Path(self.project["path"])
        self.manifest = self.folder / "project.json"
        self.manifest_before = self.manifest.read_bytes()
        self.source = self.folder / "source.mkv"
        self.source.write_bytes(b"Immutable original media")
        self.draft = {
            "selected_id": "one",
            "raw_form": {"text": "An unfinished cue — 日本語\nSecond line", "color": "#oops"},
            "candidate_edits": {"trim_start": 8, "trim_end": 2},
            "error": "End must be later than start",
        }

    def assert_originals(self):
        self.assertEqual(self.manifest.read_bytes(), self.manifest_before)
        self.assertEqual(self.source.read_bytes(), b"Immutable original media")

    def assert_no_temporary(self):
        self.assertEqual(list(self.folder.glob(".draft-recovery-*.tmp")), [])

    def test_private_snapshot_retains_invalid_raw_fields_and_valid_project(self):
        before = deepcopy((self.project, self.draft))
        destination = recovery.write_recovery(self.project, self.draft)
        document = json.loads(destination.read_bytes())
        self.assertEqual(destination.parent, self.folder)
        self.assertRegex(destination.name, r"^draft-recovery-\d{8}T\d{12}Z-[a-f0-9]{32}\.json$")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(document["schema_version"], 1)
        self.assertIsNotNone(datetime.fromisoformat(document["created_at"]).tzinfo)
        self.assertEqual(document["project"], load_project(self.folder))
        self.assertEqual(document["draft"], self.draft)
        self.assertEqual((self.project, self.draft), before)
        self.assert_originals()
        self.assert_no_temporary()

    def test_colliding_recovery_and_source_symlink_are_never_replaced(self):
        now = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
        prefix = f"draft-recovery-{now:%Y%m%dT%H%M%S%fZ}-"
        existing = self.folder / f"{prefix}{'a' * 32}.json"
        existing.write_bytes(b"Previous recoverable draft")
        symlink = self.folder / f"{prefix}{'b' * 32}.json"
        symlink.symlink_to(self.source)
        with (
            patch.object(recovery, "datetime", wraps=datetime) as clock,
            patch.object(recovery.uuid, "uuid4", side_effect=[
                SimpleNamespace(hex=letter * 32) for letter in "abc"
            ]),
        ):
            clock.now.return_value = now
            destination = recovery.write_recovery(self.project, self.draft)
        self.assertEqual(existing.read_bytes(), b"Previous recoverable draft")
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(destination.name, f"{prefix}{'c' * 32}.json")
        self.assert_originals()
        self.assert_no_temporary()

    def test_success_fsyncs_file_then_publishes_then_fsyncs_directory(self):
        actions = []
        fsync, link = recovery.os.fsync, recovery.os.link

        def sync(fd):
            actions.append("sync")
            return fsync(fd)

        def publish(*args, **kwargs):
            actions.append("publish")
            # The temporary file already contains the complete JSON document.
            self.assertEqual(json.loads(Path(args[0]).read_bytes())["draft"], self.draft)
            return link(*args, **kwargs)

        with patch.object(recovery.os, "fsync", side_effect=sync), patch.object(
            recovery.os, "link", side_effect=publish
        ):
            recovery.write_recovery(self.project, self.draft)
        self.assertEqual(actions, ["sync", "publish", "sync"])

    def test_publish_failure_preserves_originals_and_removes_temporary(self):
        with patch.object(recovery.os, "link", side_effect=OSError("Disk failure")):
            with self.assertRaisesRegex(OSError, "Disk failure"):
                recovery.write_recovery(self.project, self.draft)
        self.assertEqual(list(self.folder.glob("draft-recovery-*.json")), [])
        self.assert_originals()
        self.assert_no_temporary()

    def test_file_sync_failure_never_publishes_a_partial_recovery(self):
        with patch.object(recovery.os, "fsync", side_effect=OSError("Cannot sync")):
            with self.assertRaisesRegex(OSError, "Cannot sync"):
                recovery.write_recovery(self.project, self.draft)
        self.assertEqual(list(self.folder.glob("draft-recovery-*.json")), [])
        self.assert_originals()
        self.assert_no_temporary()

    def test_directory_sync_failure_reports_failure_but_keeps_complete_snapshot(self):
        with patch.object(recovery.os, "fsync", side_effect=[None, OSError("Directory sync")]):
            with self.assertRaisesRegex(OSError, "Directory sync"):
                recovery.write_recovery(self.project, self.draft)
        snapshots = list(self.folder.glob("draft-recovery-*.json"))
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(json.loads(snapshots[0].read_bytes())["draft"], self.draft)
        self.assert_originals()
        self.assert_no_temporary()

    def test_invalid_json_or_size_fails_before_creating_files(self):
        for draft in ({"number": float("nan")}, {"object": object()}):
            with self.subTest(draft=draft):
                with self.assertRaises((ValueError, TypeError)):
                    recovery.write_recovery(self.project, draft)
        with patch.object(recovery, "MAX_JSON_BYTES", 512):
            with self.assertRaisesRegex(ValueError, "size limit"):
                recovery.write_recovery(self.project, {"text": "é" * 512})
        self.assertEqual(set(self.folder.iterdir()), {self.source, self.manifest})
        self.assert_originals()

    def test_recording_folder_must_already_exist(self):
        missing = self.folder / "absent"
        for project in ({}, {"path": ""}, {"path": 2}, None):
            with self.subTest(project=project), self.assertRaises(ValueError):
                recovery.write_recovery(project, self.draft)
        with self.assertRaises(FileNotFoundError):
            recovery.write_recovery({"path": str(missing)}, self.draft)
        self.assertFalse(missing.exists())
        with self.assertRaises(NotADirectoryError):
            recovery.write_recovery({"path": str(self.source)}, self.draft)
        self.assert_originals()

    def test_list_is_newest_first_and_skips_unreadable_invalid_or_temporary_files(self):
        with patch.object(recovery, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc)
            older = recovery.write_recovery(self.project, {"text": "Older"})
            clock.now.return_value = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
            newer = recovery.write_recovery(self.project, {"text": "Newer"})
        # Timestamps inside the documents determine order, not file mtime.
        os.utime(older, (2_000_000_000, 2_000_000_000))
        broken = self.folder / ("draft-recovery-20261003T140000000000Z-" + "a" * 32 + ".json")
        broken.write_text("{")
        temporary = self.folder / ".draft-recovery-unfinished.tmp"
        temporary.write_text("{}")
        self.assertEqual(recovery.list_recoveries(self.project), [newer, older])
        self.assertEqual(recovery.read_recovery(newer, self.project)["draft"], {"text": "Newer"})
        self.assertEqual(recovery.list_recoveries({"path": "/nonexistent/lumen-recovery"}), [])
        self.assert_originals()

    def test_reader_refuses_symlinks_special_files_and_other_recording_folders(self):
        destination = recovery.write_recovery(self.project, self.draft)
        outside = create_project(self.folder.parent)
        other_recovery = recovery.write_recovery(outside, self.draft)
        with self.assertRaises(ValueError):
            recovery.read_recovery(other_recovery, self.project)
        linked = self.folder / ("draft-recovery-20261003T140000000000Z-" + "b" * 32 + ".json")
        linked.symlink_to(destination)
        with self.assertRaises(OSError):
            recovery.read_recovery(linked, self.project)
        fifo = self.folder / ("draft-recovery-20261003T140000000000Z-" + "c" * 32 + ".json")
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular file"):
            recovery.read_recovery(fifo, self.project)
        self.assertEqual(recovery.list_recoveries(self.project), [destination])
        self.assert_originals()

    def test_reader_rejects_malformed_envelopes_and_saved_path_mismatch(self):
        destination = recovery.write_recovery(self.project, self.draft)
        document = recovery.read_recovery(destination, self.project)
        for updates in (
            {"schema_version": True},
            {"schema_version": 2},
            {"project": []},
            {"draft": []},
            {"created_at": "not a timestamp"},
            {"created_at": "2026-10-03T12:00:00"},
            {"project": dict(self.project, path="/a/different/project")},
        ):
            with self.subTest(updates=updates):
                destination.write_text(json.dumps(dict(document, **updates)))
                with self.assertRaises(ValueError):
                    recovery.read_recovery(destination, self.project)
                self.assertEqual(recovery.list_recoveries(self.project), [])
        self.assert_originals()

    def test_reader_bounds_input_and_rejects_non_json_numeric_constants(self):
        destination = recovery.write_recovery(self.project, self.draft)
        with patch.object(recovery, "MAX_JSON_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "size limit"):
                recovery.read_recovery(destination, self.project)
            self.assertEqual(recovery.list_recoveries(self.project), [])
        document = json.loads(destination.read_bytes())
        document["draft"]["invalid_number"] = float("nan")
        destination.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "JSON number"):
            recovery.read_recovery(destination, self.project)
        self.assert_originals()


if __name__ == "__main__":
    unittest.main()
