"""Regression coverage for quitting with invalid pending layer fields."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import MethodType
from unittest import TestCase, main, skipIf
from unittest.mock import Mock, patch

from lumen.clicks import click_event
from lumen.editor import ExportOptions
from lumen.project import create_project, load_project, save_project
from lumen.recovery import list_recoveries, read_recovery

try:
    from lumen import ui
except (ImportError, ValueError):
    ui = None


@skipIf(ui is None, "GTK Python bindings unavailable")
class PendingEditRecoveryTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="lumen-quit-regression-")
        self.addCleanup(self.directory.cleanup)
        self.project = create_project(self.directory.name)
        self.project.update(duration=3, width=320, height=180, fps=30, status="ready",
                            clicks=[click_event(1, .5, .4, "left")])
        save_project(self.project)
        self.source = Path(self.project["path"]) / self.project["source"]
        self.source.write_bytes(b"original recording must stay intact")
        self.original = deepcopy(self.project)
        self.draft = {
            "version": 1, "project_path": self.project["path"], "kind": "clicks",
            "selected_id": self.project["clicks"][0]["id"],
            "fields": {"start": "1.00", "end": "24.14", "color": "oops",
                       "text": "An unfinished note — preserve literally"},
        }
        self.studio = Mock(project=self.project, last_recovery=None, restored_recovery=None,
                           discard_edits_on_quit=False, countdown_source=None,
                           capture_busy=False, export_busy=False, closing=False,
                           recorder=Mock(is_running=False), replay=Mock(is_running=False))
        self.studio.layers.transcribing = False
        self.studio.layers.snapshot_draft.return_value = self.draft
        self.studio.project_name.get_text.return_value = "Keep this valid title"
        self.studio.edit_options.return_value = ExportOptions(trim_start=.2, trim_end=2.8, padding=80)
        self.studio._save_edits.side_effect = ValueError("Click color must be hexadecimal")
        self.studio.preserve_edits = MethodType(ui.StudioWindow.preserve_edits, self.studio)

    def test_invalid_form_is_backed_up_while_valid_project_options_are_saved(self):
        path = self.studio.preserve_edits()
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        recovered = read_recovery(path, self.project)
        self.assertEqual(recovered["draft"]["layer_form"], self.draft)
        self.assertEqual(recovered["project"]["clicks"], self.original["clicks"])
        saved = load_project(self.project["path"])
        self.assertEqual(saved["clicks"], self.original["clicks"])
        self.assertEqual(saved["name"], "Keep this valid title")
        self.assertEqual(saved["edits"]["padding"], 80)
        self.assertEqual(saved["edits"]["trim_start"], .2)
        self.assertEqual(self.source.read_bytes(), b"original recording must stay intact")

    def test_idle_quit_is_allowed_after_invalid_draft_is_durably_preserved(self):
        with patch.object(ui.Adw, "AlertDialog") as dialog:
            self.assertFalse(ui.StudioWindow.confirm_quit(self.studio))
        dialog.assert_not_called()
        self.assertEqual(len(list_recoveries(self.project)), 1)
        self.assertFalse(self.studio.discard_edits_on_quit)
        self.studio.stop_recording.assert_not_called()

    def test_same_draft_is_not_duplicated_by_quit_then_shutdown(self):
        first = self.studio.preserve_edits()
        second = self.studio.preserve_edits()
        self.assertEqual(first, second)
        self.assertEqual(list_recoveries(self.project), [first])

    def test_changed_raw_draft_gets_a_new_backup_before_exit(self):
        first = self.studio.preserve_edits()
        self.draft["fields"]["text"] = "A later unsaved change"
        second = self.studio.preserve_edits()
        self.assertNotEqual(first, second)
        self.assertEqual(read_recovery(second, self.project)["draft"]["layer_form"]["fields"]["text"],
                         "A later unsaved change")

    def test_recovery_remains_usable_when_manifest_write_fails(self):
        with patch.object(ui, "save_project", side_effect=OSError("disk full")):
            path = self.studio.preserve_edits()
        self.assertTrue(path.is_file())
        self.assertEqual(load_project(self.project["path"])["name"], self.original["name"])
        self.assertEqual(read_recovery(path, self.project)["draft"]["name"], "Keep this valid title")

    def test_backed_up_invalid_form_does_not_bypass_active_recording_guard(self):
        self.studio.recorder.is_running = True
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(self.studio))
        self.assertEqual(len(list_recoveries(self.project)), 1)
        self.assertEqual([call.args[0] for call in dialog.add_response.call_args_list], ["stay", "save"])
        self.studio.stop_recording.assert_not_called()
        self.assertFalse(self.studio.closing)

    def test_total_persistence_failure_requires_explicit_discard_choice(self):
        dialog = Mock()
        with patch.object(ui, "write_recovery", side_effect=OSError("disk full")), \
             patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(self.studio))
        self.assertEqual([call.args[0] for call in dialog.add_response.call_args_list], ["stay", "discard"])
        self.studio.get_application.return_value.request_quit.assert_not_called()
        dialog.connect.call_args.args[1](dialog, "discard")
        self.studio.get_application.return_value.request_quit.assert_called_once_with(discard_edits=True)

    def test_explicit_edit_discard_still_cannot_abort_export_or_transcription(self):
        self.studio.export_busy = True
        self.studio.recorder.is_running = True
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(self.studio, discard_edits=True))
        self.assertEqual([call.args[0] for call in dialog.add_response.call_args_list], ["stay"])
        self.studio.stop_recording.assert_not_called()
        self.studio.cancel_export.assert_not_called()
        self.assertFalse(self.studio.discard_edits_on_quit)


if __name__ == "__main__":
    main()
