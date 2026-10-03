"""UI state regressions without constructing widgets or connecting to a display.

Media rendering and native widgets have their own integration checks; these tests
exercise the asynchronous project transitions that can otherwise misfile exports.
"""

from pathlib import Path
import tempfile
from unittest import TestCase, main, skipIf
from unittest.mock import Mock, patch

from lumen.editor import ExportOptions
from lumen.project import create_project, load_project, save_project

try:
    from lumen import ui
except (ImportError, ValueError):
    ui = None


@skipIf(ui is None, "GTK Python bindings unavailable")
class StudioStateTests(TestCase):
    def test_close_during_capture_and_export_does_not_offer_action_that_aborts_export(
        self,
    ):
        studio = Mock(
            recorder=Mock(is_running=True),
            replay=Mock(is_running=False),
            layers=Mock(transcribing=False),
            capture_busy=False,
            export_busy=True,
        )
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(studio))
        offered = [call.args[0] for call in dialog.add_response.call_args_list]
        self.assertNotIn("save", offered)

    def test_save_dialog_keeps_original_project_when_user_switches_projects(self):
        options = ExportOptions(trim_start=2, trim_end=4)
        original = {"path": "/tmp/project-a", "source": "source.mkv", "name": "A"}
        studio = Mock(project=original, export_busy=False)
        studio.edit_options.return_value = options
        chooser = Mock()
        chooser.save_finish.return_value.get_path.return_value = "/tmp/export-a.mp4"
        with patch.object(ui.Gtk, "FileDialog", return_value=chooser):
            ui.StudioWindow.export_video(studio)
        callback = chooser.save.call_args.args[-1]
        studio.project = {"path": "/tmp/project-b", "source": "source.mkv", "name": "B"}
        callback(chooser, object())
        passed = studio.render_export.call_args.args
        self.assertEqual(passed[0], options)
        self.assertEqual(passed[1], "/tmp/export-a.mp4")
        self.assertEqual(passed[2]["path"], original["path"])

    def test_stale_open_completion_cannot_replace_newer_selection_or_active_export(
        self,
    ):
        current = {"path": "/tmp/current"}
        studio = Mock(
            project=current,
            export_busy=False,
            open_generation=0,
            layers=Mock(transcribing=False),
        )
        pending = []
        studio.worker.side_effect = lambda work, done: pending.append(done)
        ui.StudioWindow.open_project(studio, {"path": "/tmp/project-a"})
        ui.StudioWindow.open_project(studio, {"path": "/tmp/project-b"})
        pending[0]({})
        self.assertIs(studio.project, current)
        studio.export_busy = True
        pending[1]({})
        self.assertIs(studio.project, current)
        studio.video.set_filename.assert_not_called()

    def test_export_completion_preserves_edits_saved_during_render(self):
        with tempfile.TemporaryDirectory(prefix="lumen-ui-state-") as directory:
            project = create_project(root=directory)
            project.update(name="Before render", edits={"zoom": 1.0})
            save_project(project)
            studio = Mock(project=project, export_busy=False)
            pending = []
            studio.worker.side_effect = lambda work, done, failed: pending.append(done)
            destination = Path(project["path"]) / "export.mp4"
            ui.StudioWindow.render_export(studio, ExportOptions(), destination, project)
            project.update(name="New title while rendering", edits={"zoom": 1.8})
            save_project(project)
            with patch.object(ui.Adw.Toast, "new", return_value=Mock()):
                pending[0](destination)
            saved = load_project(project["path"])
            self.assertEqual(saved["name"], "New title while rendering")
            self.assertEqual(saved["edits"], {"zoom": 1.8})
            self.assertIn(str(destination), saved["exports"])


if __name__ == "__main__":
    main()
