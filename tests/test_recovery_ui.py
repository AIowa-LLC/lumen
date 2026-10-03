"""Persistence regressions for recovery caching and failed draft restoration."""

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import MethodType
from unittest import TestCase, skipIf
from unittest.mock import Mock

from lumen.clicks import click_event
from lumen.editor import ExportOptions
from lumen.project import create_project, load_project, save_project
from lumen.recovery import read_recovery, write_recovery

try:
    from lumen import ui
except (ImportError, ValueError):
    ui = None


class Entry:
    def __init__(self, text):
        self.text = text

    def get_text(self):
        return self.text

    def set_text(self, text):
        self.text = text


def form_for(project):
    return {
        "version": 1,
        "project_path": project["path"],
        "kind": "clicks",
        "selected_id": project["clicks"][0]["id"],
        "fields": {
            "start": "1.00", "end": "1.60", "click_duration": "0.60",
            "x": "75", "y": "40", "x2": "80", "y2": "60", "size": "4",
            "enabled": True, "text": "Current unfinished words",
            "annotation_kind": 0, "button_kind": 0, "color": "oops",
        },
        "caption_style": {"size": "4.5", "position": 0, "color": "#ffffff", "background": True},
        "visibility": {"captions": True, "annotations": True, "clicks": True},
    }


@skipIf(ui is None, "GTK Python bindings unavailable")
class RecoveryPersistenceUITests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lumen-recovery-ui-")
        self.addCleanup(temporary.cleanup)
        self.project = create_project(temporary.name)
        self.project.update(
            duration=8, width=640, height=360, status="ready", name="Current valid take",
            clicks=[click_event(1, .75, .4, "left")], captions=[], annotations=[],
        )
        self.options = ExportOptions(trim_start=.2, trim_end=7.8, padding=48)
        self.project["edits"] = asdict(self.options)
        save_project(self.project)
        self.source = Path(self.project["path"]) / self.project["source"]
        self.source.write_bytes(b"Original recording remains unchanged")
        self.form = form_for(self.project)
        self.studio = Mock(
            project=self.project, last_recovery=None, restored_recovery=None,
            project_name=Entry("Current valid take"), recovery_path=None,
        )
        self.studio.layers.snapshot_draft.side_effect = lambda: deepcopy(self.form)
        self.studio.edit_options.side_effect = lambda: self.options
        self.studio.load_edits.side_effect = self.load_options
        self.studio.layers.load.side_effect = self.load_layers
        self.studio.layers.restore_draft.side_effect = self.restore_form
        self.studio._save_edits.side_effect = ValueError("Invalid click color")
        for name in ("preserve_edits", "restore_recovery"):
            setattr(self.studio, name, MethodType(getattr(ui.StudioWindow, name), self.studio))

    def load_options(self, edits, duration):
        self.options = ExportOptions(**edits)

    def load_layers(self, project):
        self.form = form_for(project)
        self.form["selected_id"] = None
        self.form["fields"]["text"] = ""

    def restore_form(self, form):
        if not isinstance(form, dict) or not isinstance(form.get("fields"), dict):
            return False
        self.form = deepcopy(form)
        return True

    def state(self):
        return deepcopy((self.project, self.form, self.studio.project_name.get_text(), asdict(self.options)))

    def prepare_restore(self, layer_form=None):
        older = deepcopy(self.project)
        older["clicks"][0]["x"] = .2
        older["source"] = "different-source.mkv"
        older["width"] = 1280
        draft = {
            "name": "Older recoverable take",
            "edits": asdict(ExportOptions(trim_end=6, padding=120)),
            "layer_form": deepcopy(layer_form if layer_form is not None else self.form),
        }
        self.studio.recovery_path = write_recovery(older, draft)
        self.studio.preserve_edits = Mock()
        return self.studio.recovery_path

    def test_deleted_cached_snapshot_is_rewritten_before_preserve_returns(self):
        first = self.studio.preserve_edits()
        first.unlink()
        replacement = self.studio.preserve_edits()
        self.assertNotEqual(first, replacement)
        self.assertEqual(read_recovery(replacement, self.project)["draft"]["layer_form"], self.form)

    def test_corrupt_cached_snapshot_is_rewritten_before_preserve_returns(self):
        first = self.studio.preserve_edits()
        first.write_text("{")
        replacement = self.studio.preserve_edits()
        self.assertNotEqual(first, replacement)
        self.assertEqual(read_recovery(replacement, self.project)["draft"]["layer_form"], self.form)

    def test_replaced_contents_are_not_mistaken_for_current_cached_snapshot(self):
        for altered in ("draft", "project"):
            with self.subTest(altered=altered):
                first = self.studio.preserve_edits()
                changed = read_recovery(first, self.project)
                if altered == "draft":
                    changed["draft"]["layer_form"]["fields"]["text"] = "Someone replaced the backup"
                else:
                    changed["project"]["clicks"][0]["x"] = .01
                first.write_text(json.dumps(changed))
                replacement = self.studio.preserve_edits()
                self.assertNotEqual(first, replacement)
                stored = read_recovery(replacement, self.project)
                self.assertEqual(stored["draft"]["layer_form"], self.form)
                self.assertEqual(stored["project"]["clicks"], self.project["clicks"])

    def test_malformed_recovery_form_cannot_partially_replace_current_project(self):
        self.prepare_restore(layer_form={"version": 1, "fields": None})
        before = self.state()
        self.studio.restore_recovery()
        self.studio.error.assert_called_once()
        self.assertEqual(self.state(), before)
        self.studio.toast.assert_not_called()
        self.assertIsNone(self.studio.restored_recovery)
        # An ordinary save after the failed restore must keep the current take.
        self.studio.layers.commit.return_value = None
        ui.StudioWindow._save_edits(self.studio)
        saved = load_project(self.project["path"])
        self.assertEqual(saved["clicks"], before[0]["clicks"])
        self.assertEqual(saved["name"], before[2])
        self.assertEqual(saved["edits"], before[3])

    def test_unexpected_form_restore_failure_rolls_back_all_current_state(self):
        self.prepare_restore()
        before = self.state()
        calls = 0

        def fail_once(form):
            nonlocal calls
            calls += 1
            if calls == 1:
                # Simulate a widget error after recovery was partially applied.
                self.form = {"partial": True}
                raise RuntimeError("Widget restore failed")
            return self.restore_form(form)

        self.studio.layers.restore_draft.side_effect = fail_once
        self.studio.restore_recovery()
        self.studio.error.assert_called_once()
        self.assertEqual(self.state(), before)
        self.assertIsNone(self.studio.restored_recovery)

    def test_successful_restore_keeps_current_source_identity_and_does_not_commit(self):
        path = self.prepare_restore()
        original_source = self.project["source"]
        manifest = Path(self.project["path"]) / "project.json"
        before_manifest = manifest.read_bytes()
        self.studio.restore_recovery()
        self.studio.error.assert_not_called()
        self.assertEqual(self.project["clicks"][0]["x"], .2)
        self.assertEqual(self.project["source"], original_source)
        self.assertEqual(self.project["width"], 640)
        self.assertEqual(self.studio.project_name.get_text(), "Older recoverable take")
        self.assertEqual(self.options.padding, 120)
        self.assertEqual(self.form["fields"]["color"], "oops")
        self.assertEqual(self.studio.restored_recovery, path)
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(self.source.read_bytes(), b"Original recording remains unchanged")
