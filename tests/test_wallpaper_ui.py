"""Wallpaper dialogs pin their project and publish against the latest recipe."""

from pathlib import Path
import tempfile
from unittest import TestCase, skipIf
from unittest.mock import Mock, patch

from lumen.project import create_project, load_project, save_project

try:
    from lumen import ui
    from lumen.wallpaper_ui import WallpaperPanel
except (ImportError, ValueError):
    ui = None


@skipIf(ui is None, "GTK Python bindings unavailable")
class WallpaperUIStateTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lumen-wallpaper-ui-")
        self.addCleanup(temporary.cleanup)
        self.project = create_project(temporary.name)
        self.studio = Mock(project=self.project, open_generation=1)
        self.studio.save_edits.return_value = True
        self.panel = Mock(studio=self.studio, importing=False)
        self.chooser = Mock()
        self.chooser.open_finish.return_value.get_path.return_value = "/tmp/custom.png"

    def open_dialog(self):
        with patch.object(ui.Gtk, "FileDialog", return_value=self.chooser):
            WallpaperPanel.add_image(self.panel)
        return self.chooser.open.call_args.args[-1]

    def test_cancelled_or_stale_dialog_does_not_import_into_new_take(self):
        callback = self.open_dialog()
        self.studio.open_generation = 2
        callback(self.chooser, object())
        self.studio.worker.assert_not_called()
        self.studio.toast.assert_called_once()

    def test_async_import_preserves_latest_edits_and_selects_saved_copy(self):
        callback = self.open_dialog()
        callback(self.chooser, object())
        imported = self.studio.worker.call_args.args[1]
        latest = load_project(self.project["path"])
        latest["edits"] = {"zoom": 2.3}
        save_project(latest)
        item = {"path": "wallpapers/custom.jpg", "name": "My image"}
        imported(item)
        stored = load_project(self.project["path"])
        self.assertEqual(stored["edits"], {"zoom": 2.3})
        self.assertEqual(stored["wallpapers"], [item])
        self.assertEqual(self.project["wallpapers"], [item])
        self.panel.select.assert_called_once_with(item["path"])
        self.studio.save_edits.assert_called_once_with(False)
        imported(item)
        self.assertEqual(load_project(self.project["path"])["wallpapers"], [item])

    def test_import_finishing_after_project_switch_only_updates_original_gallery(self):
        callback = self.open_dialog()
        callback(self.chooser, object())
        imported = self.studio.worker.call_args.args[1]
        other = create_project(Path(self.project["path"]).parent)
        self.studio.project = other
        self.studio.open_generation = 2
        item = {"path": "wallpapers/custom.jpg", "name": "My image"}
        imported(item)
        self.assertEqual(load_project(self.project["path"])["wallpapers"], [item])
        self.assertNotIn("wallpapers", load_project(other["path"]))
        self.panel.select.assert_not_called()
        self.studio.save_edits.assert_not_called()

    def test_invalid_import_resets_pending_controls_without_changing_gallery(self):
        callback = self.open_dialog()
        callback(self.chooser, object())
        failure = self.studio.worker.call_args.args[2]
        error = ValueError("Invalid image")
        failure(error)
        self.assertFalse(self.panel.importing)
        self.panel.add_button.set_sensitive.assert_called_with(True)
        self.studio.error.assert_called_once_with(error)
        self.panel.select.assert_not_called()
