"""Authoring regressions without creating GTK widgets or touching the desktop."""

from copy import deepcopy
from types import MethodType, SimpleNamespace
from unittest import TestCase, main, skipIf
from unittest.mock import Mock, patch

from lumen.clicks import click_event
from lumen.editor import ExportOptions

try:
    from lumen import layer_ui
except (ImportError, ValueError):
    layer_ui = None


class Control:
    """The value interface used by spin, entry, dropdown and toggle controls."""

    def __init__(self, value):
        self.value = value

    def get_value(self):
        return self.value

    get_active = get_selected = get_text = get_value

    def set_value(self, value):
        self.value = value

    set_active = set_selected = set_text = set_value

    def set_range(self, low, high):
        self.range = (low, high)


class SpinControl(Control):
    def __init__(self, value, digits=2):
        super().__init__(value)
        self.digits = digits
        self.raw = None

    def get_text(self):
        return self.raw if self.raw is not None else f"{self.value:.{self.digits}f}"

    def set_text(self, text):
        self.raw = text

    def set_value(self, value):
        self.value, self.raw = value, None

    def get_digits(self):
        return self.digits


def panel_for(project):
    panel = SimpleNamespace(
        studio=Mock(project=project, preview_options=None),
        loading=False,
        current_kind="clicks",
        selected_id=project["clicks"][0]["id"],
        pick_target=None,
        transcribing=False,
        undo_stack=[],
        redo_stack=[],
        start=SpinControl(1),
        end=SpinControl(1.6),
        click_duration=SpinControl(0.6),
        x=SpinControl(50, 1),
        y=SpinControl(25, 1),
        x2=SpinControl(70, 1),
        y2=SpinControl(60, 1),
        size=SpinControl(4, 1),
        enabled=Control(True),
        button_kind=Control(0),
        custom_color=Control("#78c8ff"),
        caption_size=SpinControl(4.5, 1),
        caption_position=Control(0),
        caption_color=Control("#ffffff"),
        caption_background=Control(True),
        kind=Control(2),
        refresh=Mock(),
        update_history=Mock(),
        load_selected=Mock(),
        annotation_kind=Control(0),
        color=Control(1),
        text=Mock(),
        get_text=Mock(return_value=""),
        details=Mock(),
        update_detail_visibility=Mock(),
        flags={key: Control(True) for key in ("captions", "annotations", "clicks")},
    )
    for method in (
        "project",
        "items",
        "selected",
        "read_item",
        "snapshot",
        "remember",
        "commit",
        "guard",
        "save_layers",
        "apply",
        "history",
        "change_kind",
        "cancel_pick",
        "place",
        "playhead",
        "delete",
        "duplicate",
        "snapshot_draft",
        "restore_draft",
    ):
        setattr(panel, method, MethodType(getattr(layer_ui.LayerPanel, method), panel))
    return panel


def native_project():
    event = click_event(1, 0.5, 0.25, "left")
    event["capture_metadata"] = {"origin": "hyprland", "unknown_future_field": [1, 2]}
    return {
        "path": "/tmp/lumen-layer-ui-regression",
        "duration": 10,
        "width": 1920,
        "height": 1080,
        "source": "source.mkv",
        "capture": {"cursor": True, "record_clicks": True},
        "clicks": [event],
        "captions": [],
        "annotations": [],
        "caption_style": {
            "font_size": 0.045,
            "position": "bottom",
            "color": "#ffffff",
            "background": True,
        },
    }


@skipIf(layer_ui is None, "GTK Python bindings unavailable")
class VideoMappingTests(TestCase):
    def test_letterbox_mapping_rejects_bars_and_retains_video_edges(self):
        for actual, expected in zip(
            layer_ui.video_bounds(1000, 1000, 1920, 1080), (0, 218.75, 1000, 562.5)
        ):
            self.assertAlmostEqual(actual, expected)
        args = (1000, 1000, 1920, 1080)
        self.assertEqual(layer_ui.video_point(500, 500, *args), (0.5, 0.5))
        for point, expected in (((0, 218.75), 0), ((1000, 781.25), 1)):
            for actual in layer_ui.video_point(*point, *args):
                self.assertAlmostEqual(actual, expected)
        for point in ((500, 218.74), (500, 781.26), (-1, 500), (1001, 500)):
            self.assertIsNone(layer_ui.video_point(*point, *args))

    def test_portrait_pillarbox_maps_to_original_normalized_coordinates(self):
        args = (1000, 600, 600, 1000)
        self.assertEqual(layer_ui.video_bounds(*args), (320, 0, 360, 600))
        self.assertEqual(layer_ui.video_point(410, 450, *args), (0.25, 0.75))
        self.assertIsNone(layer_ui.video_point(319, 300, *args))
        self.assertIsNone(layer_ui.video_point(681, 300, *args))

    def test_native_pixel_scale_does_not_change_normalized_placement(self):
        logical = layer_ui.video_point(200, 400, 800, 800, 800, 450)
        native = layer_ui.video_point(200, 400, 800, 800, 1000, 562.5)
        self.assertEqual(logical, (0.25, 0.5))
        self.assertEqual(native, logical)

    def test_unallocated_widgets_and_invalid_source_dimensions_cannot_place(self):
        for dims in (
            (0, 100, 100, 100),
            (100, 0, 100, 100),
            (100, 100, 0, 100),
            (100, 100, 100, -1),
        ):
            self.assertIsNone(layer_ui.video_point(0, 0, *dims))
        self.assertIsNone(layer_ui.video_point(float("nan"), 10, 100, 100, 100, 100))


@skipIf(layer_ui is None, "GTK Python bindings unavailable")
class ClickAuthoringTests(TestCase):
    def setUp(self):
        self.project = native_project()
        self.panel = panel_for(self.project)

    def test_clean_layers_assigns_missing_ids_without_overwriting_existing_metadata(
        self,
    ):
        first = self.project["clicks"][0]
        original = deepcopy(first)
        self.project["clicks"].append({"id": None, "t": 2, "future": "kept"})
        del self.project["annotations"]
        layer_ui.clean_layers(self.project)
        self.assertEqual(first, original)
        self.assertEqual(len(self.project["clicks"][1]["id"]), 32)
        self.assertEqual(self.project["clicks"][1]["future"], "kept")
        self.assertEqual(self.project["annotations"], [])

    def test_edit_preserves_native_id_and_metadata_in_an_independent_candidate(self):
        original = deepcopy(self.project["clicks"][0])
        self.panel.start.value, self.panel.end.value = 2.25, 3
        self.panel.click_duration.value = 0.75
        self.panel.x.value, self.panel.y.value = 12.5, 87.5
        self.panel.size.value, self.panel.button_kind.value = 8, 2
        self.panel.custom_color.value, self.panel.enabled.value = "#123abc", False
        candidate = self.panel.read_item()
        self.assertEqual(candidate["id"], original["id"])
        self.assertEqual((candidate["t"], candidate["duration"]), (2.25, 0.75))
        self.assertEqual(
            (candidate["x"], candidate["y"], candidate["size"]), (0.125, 0.875, 0.08)
        )
        self.assertEqual(
            (candidate["button"], candidate["color"], candidate["enabled"]),
            ("middle", "#123abc", False),
        )
        candidate["capture_metadata"]["unknown_future_field"].append(3)
        self.assertEqual(self.project["clicks"][0], original)

    def test_commit_preserves_other_clicks_and_baked_cursor_capture_settings(self):
        other = click_event(4, 0.8, 0.3, "right")
        self.project["clicks"].append(other)
        original = deepcopy(self.project)
        self.panel.x.value = 75
        with patch.object(layer_ui, "save_project") as save:
            self.panel.commit()
        self.assertEqual(self.project["clicks"][0]["x"], 0.75)
        self.assertEqual(self.project["clicks"][0]["id"], original["clicks"][0]["id"])
        self.assertEqual(self.project["clicks"][1], other)
        self.assertEqual(
            self.project["capture"], {"cursor": True, "record_clicks": True}
        )
        self.assertEqual(self.project["source"], "source.mkv")
        self.assertEqual(self.panel.undo_stack[0]["clicks"], original["clicks"])
        save.assert_not_called()  # The studio saves the full transaction after commit.

    def test_invalid_candidate_is_discarded_without_mutating_project_or_history(self):
        original = deepcopy(self.project)
        self.panel.x.value = 75
        self.panel.custom_color.value = "not-a-color"
        self.assertFalse(self.panel.guard(self.panel.commit))
        self.assertEqual(self.project, original)
        self.assertEqual(self.panel.undo_stack, [])
        self.panel.studio.error.assert_called_once()
        self.panel.studio.layer_timeline.queue_draw.assert_not_called()

    def test_invalid_duration_stays_in_current_kind_and_retains_unsaved_controls(self):
        self.panel.kind.value = 0
        self.panel.click_duration.set_text("10.01")
        self.panel.change_kind()
        self.assertEqual(self.panel.current_kind, "clicks")
        self.assertEqual(self.panel.kind.value, 2)
        self.assertEqual(self.panel.click_duration.get_text(), "10.01")
        self.assertEqual(self.project["clicks"][0]["duration"], 0.6)
        self.panel.refresh.assert_not_called()

    def test_loaded_native_click_keeps_exact_time_and_duration_when_display_rounds(
        self,
    ):
        event = self.project["clicks"][0]
        event.update(t=8.1449, x=0.365236, y=0.192631, duration=0.6)
        layer_ui.LayerPanel.load_selected(self.panel)
        self.assertEqual(self.panel.start.get_text(), "8.14")
        self.assertEqual(self.panel.end.get_text(), "8.74")
        item = self.panel.read_item()
        self.assertEqual(item["t"], 8.1449)
        self.assertEqual(item["duration"], 0.6)
        self.assertAlmostEqual(item["x"], 0.365236)
        self.assertAlmostEqual(item["y"], 0.192631)
        self.panel.commit()
        self.assertEqual(self.project["clicks"][0]["duration"], 0.6)

    def test_moving_click_start_preserves_duration_independent_of_old_end(self):
        self.panel.start.set_text("8.25")
        self.panel.end.value = 1.6
        self.panel.commit()
        self.assertEqual(self.project["clicks"][0]["t"], 8.25)
        self.assertEqual(self.project["clicks"][0]["duration"], 0.6)

    def test_duration_bounds_survive_floating_residue_and_reject_real_overruns(self):
        for value, expected in ((0.01, 0.01), (0.01 - 1e-15, 0.01), (10 + 1e-15, 10)):
            with self.subTest(value=value):
                self.panel.click_duration.set_value(value)
                self.panel.commit()
                self.assertEqual(self.project["clicks"][0]["duration"], expected)
        original = deepcopy(self.project)
        for text in ("0", "0.009", "10.01", "nan", "still typing"):
            with self.subTest(text=text):
                self.panel.click_duration.set_text(text)
                self.assertFalse(self.panel.guard(self.panel.commit))
                self.assertEqual(self.project, original)
                self.assertEqual(self.panel.click_duration.get_text(), text)

    def test_recovery_preserves_invalid_raw_form_without_mutating_valid_layers(self):
        import json

        original = deepcopy(self.project)
        self.panel.start.set_text("8.25")
        self.panel.click_duration.set_text("10.01")
        self.panel.custom_color.set_text("#bad")
        self.panel.get_text.return_value = "Unfinished caption\nwith another line"
        self.panel.caption_size.set_text("still typing")
        self.panel.flags["clicks"].set_active(False)
        draft = self.panel.snapshot_draft()
        # Persistence accepts this raw draft even though the render model cannot.
        saved = json.loads(json.dumps(draft, allow_nan=False))
        self.panel.click_duration.set_value(0.6)
        self.panel.custom_color.set_text("#ffffff")
        self.assertTrue(self.panel.restore_draft(saved))
        self.assertEqual(self.panel.click_duration.get_text(), "10.01")
        self.assertEqual(self.panel.custom_color.get_text(), "#bad")
        self.assertEqual(self.panel.caption_size.get_text(), "still typing")
        self.assertFalse(self.panel.flags["clicks"].get_active())
        self.panel.text.get_buffer.return_value.set_text.assert_called_with(
            "Unfinished caption\nwith another line"
        )
        self.assertEqual(self.project, original)
        self.assertFalse(self.panel.guard(self.panel.commit))

    def test_recovery_rejects_other_project_unknown_layer_and_bad_shape_before_mutation(
        self,
    ):
        draft = self.panel.snapshot_draft()
        for changes in (
            {"project_path": "/some/other/project"},
            {"selected_id": "missing"},
            {"fields": {"click_duration": "0.6"}},
            {"kind": "invalid"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.panel.restore_draft({**draft, **changes}))
        self.panel.refresh.assert_not_called()
        self.assertEqual(self.panel.snapshot_draft(), draft)

    def test_recovery_keeps_high_precision_adjustments_behind_rounded_display(self):
        self.panel.start.set_value(8.2449)
        self.panel.x.set_value(36.5236)
        draft = self.panel.snapshot_draft()
        restored = panel_for(deepcopy(self.project))
        self.assertTrue(restored.restore_draft(draft))
        self.assertEqual(restored.start.get_text(), "8.24")
        self.assertEqual(restored.read_item()["t"], 8.2449)
        self.assertAlmostEqual(restored.read_item()["x"], 0.365236)
        draft.pop("numeric_values")
        self.assertTrue(layer_ui.draft_matches_project(self.project, draft))

    def test_apply_does_not_report_saved_when_studio_cannot_persist_the_edit(self):
        self.panel.x.value = 75
        self.panel.studio.save_edits.return_value = False
        self.panel.apply()
        self.panel.studio.save_edits.assert_called_once_with(False)
        self.panel.refresh.assert_not_called()
        self.panel.studio.toast.assert_not_called()

    def test_undo_redo_preserves_native_ids_and_user_changed_click_properties(self):
        original = deepcopy(self.project["clicks"])
        self.panel.custom_color.value, self.panel.enabled.value = "#ff7285", False
        self.panel.commit()
        edited = deepcopy(self.project["clicks"])
        with patch.object(layer_ui, "save_project") as save:
            self.panel.history(True)
            self.assertEqual(self.project["clicks"], original)
            self.panel.history(False)
        self.assertEqual(self.project["clicks"], edited)
        self.assertEqual(save.call_count, 2)

    def test_placement_in_letterbox_does_not_edit_or_cancel_pick(self):
        self.panel.pick_target = "start"
        self.panel.studio.video.get_width.return_value = 1000
        self.panel.studio.video.get_height.return_value = 1000
        original = deepcopy(self.project)
        self.assertTrue(self.panel.place(500, 100))
        self.assertEqual(self.project, original)
        self.assertEqual(self.panel.pick_target, "start")
        self.panel.studio.save_edits.assert_not_called()
        self.panel.cancel_pick()
        self.assertIsNone(self.panel.pick_target)
        self.assertEqual(self.project, original)

    def test_placement_edits_event_coordinates_without_touching_source(self):
        self.panel.pick_target = "start"
        self.panel.studio.video.get_width.return_value = 1000
        self.panel.studio.video.get_height.return_value = 1000
        self.assertTrue(self.panel.place(750, 500))
        event = self.project["clicks"][0]
        self.assertAlmostEqual(event["x"], 0.75)
        self.assertAlmostEqual(event["y"], 0.5)
        self.assertEqual(event["capture_metadata"]["origin"], "hyprland")
        self.assertIsNone(self.panel.pick_target)
        self.panel.studio.save_edits.assert_called_once_with(False)
        self.assertEqual(self.project["source"], "source.mkv")

    def test_preview_playhead_converts_back_to_source_timeline(self):
        self.panel.studio.video.get_media_stream.return_value.get_timestamp.return_value = 2_000_000
        self.panel.studio.preview_options = ExportOptions(trim_start=3, speed=1.5)
        self.assertEqual(self.panel.playhead(), 6)


if __name__ == "__main__":
    main()
