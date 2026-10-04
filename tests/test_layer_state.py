"""Display-free regressions for editable-layer interaction state."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import MethodType
import unittest
from unittest.mock import Mock, patch

from lumen.editor import ExportOptions
from lumen.project import create_project, load_project, save_project

try:
    from lumen import layer_ui, ui
except (ImportError, ValueError):
    layer_ui = ui = None


@unittest.skipIf(ui is None, "GTK Python bindings unavailable")
class LayerStateTests(unittest.TestCase):
    def test_caption_candidate_uses_fresh_ids_without_changing_inputs(self):
        original = {"id": "existing", "start": 0, "end": 1, "text": "Original"}
        project = {"duration": 4, "captions": [deepcopy(original)]}
        imported = [
            dict(original, text="Imported one"),
            dict(original, text="Imported two"),
        ]
        before_project, before_imported = deepcopy(project), deepcopy(imported)
        combined = layer_ui.caption_candidate(project, imported)
        self.assertEqual(len(combined), 3)
        self.assertEqual(combined[0], original)
        self.assertEqual(len({cue["id"] for cue in combined}), 3)
        self.assertEqual(
            [cue["text"] for cue in combined[1:]], ["Imported one", "Imported two"]
        )
        self.assertEqual(project, before_project)
        self.assertEqual(imported, before_imported)
        combined[0]["text"] = "Changed result"
        self.assertEqual(project, before_project)

    def test_caption_candidate_rejects_combined_limit_without_mutation(self):
        project = {
            "duration": 4,
            "captions": [
                {"id": f"cue-{index}", "start": 0, "end": 1, "text": "Caption"}
                for index in range(10_000)
            ],
        }
        incoming = [{"id": "new", "start": 1, "end": 2, "text": "One too many"}]
        before_project, before_incoming = deepcopy(project), deepcopy(incoming)
        with self.assertRaisesRegex(ValueError, "10,000"):
            layer_ui.caption_candidate(project, incoming)
        self.assertEqual(project, before_project)
        self.assertEqual(incoming, before_incoming)

    def test_mutation_save_failures_keep_recoverable_drafts_and_report_errors(self):
        for action in ("add", "duplicate", "delete", "undo", "redo"):
            with (
                self.subTest(action=action),
                tempfile.TemporaryDirectory(prefix="lumen-layer-save-") as directory,
            ):
                project = create_project(root=directory)
                project.update(
                    duration=4,
                    captions=[
                        {
                            "id": "existing",
                            "start": 0,
                            "end": 1,
                            "text": "Original caption",
                        }
                    ],
                    annotations=[],
                    clicks=[],
                    caption_style={},
                )
                save_project(project)
                manifest = Path(project["path"]) / "project.json"
                saved_before = manifest.read_bytes()
                source = Path(project["path"]) / project["source"]
                source.write_bytes(b"The source is never an edit destination.")
                panel = Mock(
                    current_kind="captions",
                    selected_id="existing",
                    transcribing=False,
                    undo_stack=[],
                    redo_stack=[],
                )
                panel.project.return_value = project
                panel.playhead.return_value = 0.5
                for name in (
                    "items",
                    "selected",
                    "snapshot",
                    "remember",
                    "history",
                    "save_layers",
                ):
                    setattr(
                        panel,
                        name,
                        MethodType(getattr(layer_ui.LayerPanel, name), panel),
                    )
                if action in ("undo", "redo"):
                    previous = panel.snapshot()
                    previous["captions"][0]["text"] = action.title() + " caption"
                    (panel.undo_stack if action == "undo" else panel.redo_stack).append(
                        previous
                    )
                with patch.object(
                    layer_ui,
                    "save_project",
                    side_effect=PermissionError("Read-only library"),
                ):
                    self.assertFalse(getattr(layer_ui.LayerPanel, action)(panel))
                self.assertEqual(manifest.read_bytes(), saved_before)
                self.assertEqual(
                    source.read_bytes(), b"The source is never an edit destination."
                )
                if action in ("add", "duplicate"):
                    self.assertEqual(len(project["captions"]), 2)
                elif action == "delete":
                    self.assertEqual(project["captions"], [])
                else:
                    self.assertEqual(
                        project["captions"][0]["text"], action.title() + " caption"
                    )
                panel.studio.error.assert_called_once()
                message = str(panel.studio.error.call_args.args[0])
                self.assertIn("still in memory", message)
                self.assertIn("Save edits", message)
                self.assertIn("Read-only library", message)
                panel.studio.toast.assert_not_called()
                panel.refresh.assert_called_once()
                # A later retry persists the same retained draft once writes work.
                self.assertTrue(panel.save_layers())
                self.assertEqual(
                    load_project(project["path"])["captions"], project["captions"]
                )
                self.assertEqual(
                    source.read_bytes(), b"The source is never an edit destination."
                )

    def test_caption_append_save_failure_never_announces_success(self):
        from lumen import captions

        for action in ("import", "generate"):
            with self.subTest(action=action):
                original = {"id": "existing", "start": 0, "end": 1, "text": "Original"}
                project = {
                    "path": "/tmp/caption-save-test",
                    "duration": 4,
                    "captions": [original],
                }
                incoming = {"id": "incoming", "start": 1, "end": 2, "text": "New words"}
                panel = Mock(transcribing=False, transcribe_generation=0)
                panel.project.return_value = project
                panel.selected.return_value = original
                panel.read_item.return_value = original
                panel.speech_audio.get_selected.return_value = 0
                panel.language.get_selected.return_value = 0
                panel.save_layers = MethodType(layer_ui.LayerPanel.save_layers, panel)
                panel.guard = MethodType(layer_ui.LayerPanel.guard, panel)
                if action == "import":
                    dialog = Mock()
                    dialog.open_finish.return_value.get_path.return_value = (
                        "/tmp/captions.srt"
                    )
                    with patch.object(layer_ui.Gtk, "FileDialog", return_value=dialog):
                        layer_ui.LayerPanel.import_captions(panel)
                    chosen = dialog.open.call_args.args[-1]
                    with (
                        patch.object(
                            captions, "load_captions", return_value=[incoming]
                        ),
                        patch.object(
                            layer_ui,
                            "save_project",
                            side_effect=PermissionError("Read-only library"),
                        ),
                    ):
                        chosen(dialog, object())
                else:
                    pending = []
                    panel.studio.worker.side_effect = (
                        lambda work, done, failed: pending.append(done)
                    )
                    with patch.object(captions, "Transcriber", return_value=Mock()):
                        layer_ui.LayerPanel.generate_captions(panel)
                    with patch.object(
                        layer_ui,
                        "save_project",
                        side_effect=PermissionError("Read-only library"),
                    ):
                        pending[0]([incoming])
                    self.assertIn(
                        "not saved", panel.speech_status.set_text.call_args.args[0]
                    )
                    self.assertNotIn(
                        1,
                        [
                            call.args[0]
                            for call in panel.speech_progress.set_fraction.call_args_list
                        ],
                    )
                self.assertEqual(
                    [cue["text"] for cue in project["captions"]],
                    ["Original", "New words"],
                )
                panel.studio.error.assert_called_once()
                self.assertIn(
                    "still in memory", str(panel.studio.error.call_args.args[0])
                )
                panel.studio.toast.assert_not_called()
                panel.refresh.assert_called_once()

    def test_letterbox_coordinates_are_mapped_to_raw_video(self):
        point = layer_ui.video_point(500, 500, 1000, 1000, 1920, 1080)
        self.assertEqual(point, (0.5, 0.5))
        corner = layer_ui.video_point(0, 218.75, 1000, 1000, 1920, 1080)
        self.assertAlmostEqual(corner[0], 0)
        self.assertAlmostEqual(corner[1], 0)
        self.assertIsNone(layer_ui.video_point(500, 100, 1000, 1000, 1920, 1080))
        self.assertIsNone(layer_ui.video_point(0, 0, 0, 0, 1920, 1080))

    def test_pending_open_cannot_change_project_after_transcription_starts(self):
        current = {"path": "/tmp/current-layers"}
        studio = Mock(
            project=current,
            export_busy=False,
            open_generation=0,
            layers=Mock(transcribing=False),
        )
        pending = []
        studio.worker.side_effect = lambda work, done, failed: pending.append(done)
        ui.StudioWindow.open_project(studio, {"path": "/tmp/different-layers"})
        studio.layers.transcribing = True
        # A successful stale probe must be ignored before it touches metadata.
        with patch.object(ui, "save_project"):
            pending[0](
                {
                    "duration": 3,
                    "width": 320,
                    "height": 180,
                    "fps": 30,
                    "audio_streams": [],
                }
            )
        self.assertIs(studio.project, current)
        studio.video.set_filename.assert_not_called()

    def test_placement_cannot_be_armed_while_preview_is_rendering(self):
        panel = Mock(studio=Mock(export_busy=True), pick_target=None)
        panel.selected.return_value = {"id": "note", "kind": "text"}
        layer_ui.LayerPanel.arm_pick(panel, "start")
        self.assertIsNone(panel.pick_target)
        panel.studio.show_original.assert_not_called()

    def test_export_dialog_freezes_nested_caption_content(self):
        project = {
            "path": "/tmp/layer-project",
            "source": "source.mkv",
            "captions": [{"id": "c", "start": 0, "end": 1, "text": "Before"}],
        }
        studio = Mock(project=project, export_busy=False)
        studio.edit_options.return_value = ExportOptions()
        dialog = Mock()
        dialog.save_finish.return_value.get_path.return_value = "/tmp/layer-export.mp4"
        with patch.object(ui.Gtk, "FileDialog", return_value=dialog):
            ui.StudioWindow.export_video(studio)
        chosen = dialog.save.call_args.args[-1]
        project["captions"][0]["text"] = "After"
        chosen(dialog, object())
        rendered_project = studio.render_export.call_args.args[2]
        self.assertEqual(rendered_project["captions"][0]["text"], "Before")
        self.assertEqual(project["captions"][0]["text"], "After")

    def test_malformed_incoming_layer_cannot_replace_open_project(self):
        current = {"path": "/tmp/current-layers"}
        incoming = {
            "path": "/tmp/broken-layers",
            "captions": [
                {"id": "bad", "start": "not a time", "end": 2, "text": "Text"}
            ],
        }
        studio = Mock(
            project=current,
            export_busy=False,
            open_generation=0,
            layers=Mock(transcribing=False),
        )
        pending = []
        studio.worker.side_effect = lambda work, done, failed: pending.append(work)
        ui.StudioWindow.open_project(studio, incoming)
        with (
            patch.object(ui, "probe", return_value={"duration": 3}),
            patch.object(ui, "thumbnail") as thumbnail,
        ):
            with self.assertRaisesRegex(ValueError, "start"):
                pending[0]()
        self.assertIs(studio.project, current)
        thumbnail.assert_not_called()

    def test_transcript_append_preserves_unsaved_layer_form(self):
        from lumen import captions

        original = {"id": "existing", "start": 0, "end": 1, "text": "Stored caption"}
        project = {"path": "/tmp/caption-project", "captions": [deepcopy(original)]}
        panel = Mock(transcribing=False, transcribe_generation=0)
        panel.project.return_value = project
        panel.items.side_effect = lambda kind: project[kind]
        panel.speech_audio.get_selected.return_value = 0
        panel.language.get_selected.return_value = 0
        panel.selected.return_value = original
        panel.read_item.return_value = dict(original, text="Still typing this draft")
        pending = []
        panel.studio.worker.side_effect = lambda work, done, failed: pending.append(
            done
        )
        with patch.object(captions, "Transcriber", return_value=Mock()):
            layer_ui.LayerPanel.generate_captions(panel)
        generated = {"id": "existing", "start": 1, "end": 2, "text": "Generated words"}
        with patch.object(layer_ui, "save_project"):
            pending[0]([generated])
        self.assertEqual(len(project["captions"]), 2)
        self.assertEqual(project["captions"][0]["text"], "Stored caption")
        self.assertEqual(project["captions"][1]["text"], "Generated words")
        self.assertNotEqual(project["captions"][0]["id"], project["captions"][1]["id"])
        panel.refresh.assert_not_called()
        self.assertFalse(panel.transcribing)

    def test_undo_redo_snapshots_do_not_alias_current_layers(self):
        before = {
            "captions": [{"id": "c", "start": 0, "end": 1, "text": "Before"}],
            "annotations": [],
            "clicks": [],
            "caption_style": {},
        }
        project = deepcopy(before)
        project["captions"][0]["text"] = "After"
        panel = Mock(transcribing=False, undo_stack=[deepcopy(before)], redo_stack=[])
        panel.project.return_value = project
        panel.snapshot = MethodType(layer_ui.LayerPanel.snapshot, panel)
        with patch.object(layer_ui, "save_project"):
            layer_ui.LayerPanel.history(panel, True)
            self.assertEqual(project["captions"][0]["text"], "Before")
            layer_ui.LayerPanel.history(panel, False)
        self.assertEqual(project["captions"][0]["text"], "After")
        project["captions"][0]["text"] = "Changed again"
        self.assertEqual(panel.undo_stack[-1]["captions"][0]["text"], "Before")


if __name__ == "__main__":
    unittest.main()
