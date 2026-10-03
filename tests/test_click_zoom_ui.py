"""Click zoom controls retain old recipes and reject invalid drafts safely."""

from unittest import TestCase, skipIf
from unittest.mock import Mock

try:
    from lumen import ui
except (ImportError, ValueError):
    ui = None


@skipIf(ui is None, "GTK Python bindings unavailable")
class ClickZoomUIStateTests(TestCase):
    def studio(self):
        studio = Mock(project={"clicks": [{"id": "one", "t": 1, "x": 0.2, "y": 0.4}]})
        studio.layers.guard.return_value = True
        studio.save_edits.return_value = True
        studio.zoom.get_value.return_value = 1.6
        studio.click_zoom.get_active.return_value = False
        return studio

    def test_old_recipe_keeps_manual_zoom_and_click_follow_off(self):
        studio = self.studio()
        ui.StudioWindow.load_edits(studio, {"zoom": 1.6}, 12)
        studio.click_zoom.set_active.assert_called_once_with(False)
        studio.zoom.set_value.assert_called_once_with(1.6)
        studio.manual_zoom_expander.set_expanded.assert_called_once_with(True)
        studio.click_zoom_amount.set_value.assert_called_once_with(1.8)

    def test_saved_follow_controls_load_and_malformed_numbers_recover(self):
        studio = self.studio()
        ui.StudioWindow.load_edits(
            studio,
            {
                "click_zoom": True,
                "click_zoom_amount": 2.5,
                "click_zoom_hold": None,
                "click_zoom_transition": float("nan"),
            },
            12,
        )
        studio.click_zoom.set_active.assert_called_once_with(True)
        studio.click_zoom_amount.set_value.assert_called_once_with(2.5)
        studio.click_zoom_hold.set_value.assert_called_once_with(1.2)
        studio.click_zoom_transition.set_value.assert_called_once_with(0.35)

    def test_shortcut_commits_draft_before_enabling_and_saving(self):
        studio = self.studio()
        ui.StudioWindow.zoom_to_clicks(studio)
        studio.layers.guard.assert_called_once_with(studio.layers.commit)
        studio.click_zoom.set_active.assert_called_once_with(True)
        studio.inspector.set_visible_child_name.assert_called_once_with("style")
        studio.save_edits.assert_called_once_with(False)

    def test_invalid_draft_or_no_enabled_clicks_cannot_activate_shortcut(self):
        studio = self.studio()
        studio.layers.guard.return_value = False
        ui.StudioWindow.zoom_to_clicks(studio)
        studio.click_zoom.set_active.assert_not_called()
        studio.save_edits.assert_not_called()
        studio.layers.guard.return_value = True
        for clicks in ([], [{"enabled": False}]):
            studio.project["clicks"] = clicks
            ui.StudioWindow.zoom_to_clicks(studio)
        studio.click_zoom.set_active.assert_not_called()
        studio.save_edits.assert_not_called()

    def test_failed_save_does_not_announce_enabled_zoom(self):
        studio = self.studio()
        studio.save_edits.return_value = False
        ui.StudioWindow.zoom_to_clicks(studio)
        studio.toast.assert_not_called()
