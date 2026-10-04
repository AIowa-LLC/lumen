"""Agent state, project boundaries, validated edits, and native job ownership."""

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock, patch

from lumen.agent import AgentController, edit_options
from lumen.editor import ExportCancelled
from lumen.project import create_project, load_project, save_project


class AgentTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lumen-agent-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"LUMEN_LIBRARY": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.project = create_project()
        self.project.update(status="ready", duration=4, width=320, height=180, fps=30, name="Original")
        save_project(self.project)
        (Path(self.project["path"]) / "source.mkv").write_bytes(b"Original source")
        self.studio = Mock(project=None, export_busy=False, devices_ready=True, shutting_down=False,
                           capture_busy=False, layers=Mock(transcribing=False),
                           recorder=Mock(is_running=False, project=None, status="idle", elapsed=0),
                           replay=Mock(is_running=False, status="idle", elapsed=0),
                           monitors=[], windows=[], microphones=[], camera_devices=[])
        self.studio.get_application.return_value.view = "hud"
        self.studio.export_progress.get_fraction.return_value = .3
        self.agent = AgentController(self.studio)
        self.project_id = Path(self.project["path"]).name

    def call(self, operation, **args):
        return self.agent.request(json.dumps({"library": str(self.root), "operation": operation, "arguments": args}))

    def test_rpc_rejects_unknown_operations_arguments_and_mismatched_library(self):
        for request in (
            {"library": str(self.root), "operation": "_saved", "arguments": {}},
            {"library": str(self.root), "operation": "status", "arguments": {"extra": 1}},
            {"library": "/other/library", "operation": "status"},
        ):
            with self.assertRaises((ValueError, TypeError)):
                self.agent.request(json.dumps(request))

    def test_status_and_sources_use_shared_native_controller(self):
        self.studio.recorder.status = "paused"
        self.studio.recorder.is_running = True
        self.assertEqual(self.call("status")["recording"]["state"], "paused")
        self.assertTrue(self.call("sources")["ready"])
        self.assertFalse(self.call("sources", refresh=True)["ready"])
        self.studio.refresh_devices.assert_called_once()

    def test_project_ids_cannot_escape_library_or_follow_symlinks(self):
        for project_id in ("../other", ".", "..", "/tmp", "x/y"):
            with self.assertRaises(ValueError):
                self.agent.project(project_id)
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / "outside").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.agent.project("outside")

    def test_valid_edits_preserve_extensions_and_original_source(self):
        self.project["edits"] = {"extension": {"keep": True}}
        save_project(self.project)
        result = self.call("update_edits", project_id=self.project_id,
                           changes={"background": "wallpaper", "wallpaper": "builtin:dusk", "trim_start": .5, "trim_end": 3.5, "output_width": 960}, name="New title")
        saved = load_project(self.project["path"])
        self.assertEqual(saved["edits"]["extension"], {"keep": True})
        self.assertEqual(saved["name"], "New title")
        self.assertEqual(result["edits"]["wallpaper"], "builtin:dusk")
        self.assertEqual((Path(saved["path"]) / "source.mkv").read_bytes(), b"Original source")

    def test_invalid_edits_do_not_mutate_manifest(self):
        before = load_project(self.project["path"])
        for changes in ({"trim_start": 8}, {"speed": True}, {"padding": "16"}, {"captions": 1}, {"unknown": 4}, {"speed": 4}, {"padding": 400}, {"output_width": 640}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.call("update_edits", project_id=self.project_id, changes=changes)
        self.assertEqual(load_project(self.project["path"]), before)

    def test_invalid_pending_native_fields_prevent_agent_overwrite(self):
        self.studio.project = deepcopy(self.project)
        self.studio._save_edits.side_effect = ValueError("Invalid unfinished layer")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.agent.update_edits(self.project_id, {"speed": 2})
        self.studio.load_edits.assert_not_called()
        self.assertNotIn("edits", load_project(self.project["path"]))

    def test_pending_native_open_cannot_be_overwritten_by_an_agent(self):
        self.studio.opening_project = self.project["path"]
        with self.assertRaisesRegex(ValueError, "finish opening"):
            self.agent.update_edits(self.project_id, {"speed": 2})
        self.assertNotIn("edits", load_project(self.project["path"]))

    def test_layers_append_by_default_and_replacement_is_explicit(self):
        one = {"start": 0, "end": 1, "text": "One"}
        two = {"start": 1, "end": 2, "text": "Two"}
        self.agent.set_layers(self.project_id, "captions", [one])
        self.agent.set_layers(self.project_id, "captions", [two])
        saved = load_project(self.project["path"])
        self.assertEqual(len(saved["captions"]), 2)
        self.assertTrue(all(c["id"] for c in saved["captions"]))
        self.agent.set_layers(self.project_id, "captions", [two], append=False)
        self.assertEqual(len(load_project(self.project["path"])["captions"]), 1)
        with self.assertRaises(ValueError):
            self.agent.set_layers(self.project_id, "annotations", [{"kind": "box", "start": 0, "end": 1, "x": 0, "y": 0, "x2": 0, "y2": 0}])
        self.assertEqual(len(load_project(self.project["path"])["captions"]), 1)

    def test_capture_uses_existing_backend_and_requires_explicit_geometry(self):
        with self.assertRaisesRegex(ValueError, "explicit geometry"):
            self.agent.start_recording({"mode": "region"})
        self.studio.recorder.is_running = True
        with self.assertRaisesRegex(ValueError, "already active"):
            self.agent.start_recording()
        self.studio.recorder.is_running = False
        job = self.agent.start_recording({"mode": "region", "geometry": "0,0 320x180"})
        work, done, _ = self.studio.worker.call_args.args
        work()
        options = self.studio.start_backend.call_args.args[0]
        self.assertTrue(options.live_inputs)
        self.assertEqual(options.audio, "none")
        done(self.project)
        self.studio.capture_started.assert_called_once_with(self.project)
        self.assertEqual(self.agent.get_job(job["id"])["state"], "succeeded")

    def test_pause_is_explicit_idempotent_and_replay_cannot_pause(self):
        self.studio.recorder.is_running = True
        self.studio.recorder.status = "paused"
        self.assertEqual(self.agent.set_paused(True), {"paused": True})
        self.studio.worker.assert_not_called()
        job = self.agent.set_paused(False)
        work, done, _ = self.studio.worker.call_args.args
        work()
        done(None)
        self.studio.recorder.resume.assert_called_once()
        self.assertEqual(self.agent.get_job(job["id"])["result"], {"paused": False})

    def test_render_progress_completion_and_cancellation_share_native_ui(self):
        job = self.agent.render(self.project_id, preview=True)
        options, destination, project, preview = self.studio.render_export.call_args.args
        self.assertEqual(options.output_width, 960)
        self.assertTrue(preview)
        self.assertEqual(self.agent.get_job(job["id"])["progress"], .3)
        self.agent.cancel_job(job["id"])
        self.studio.cancel_export.assert_called_once()
        completed = self.studio.render_export.call_args.kwargs["completed"]
        completed(None, ExportCancelled("Cancelled"))
        self.assertEqual(self.agent.get_job(job["id"])["state"], "cancelled")

    def test_render_cannot_write_outside_take(self):
        for filename in ("../other.mp4", "/tmp/other.mp4", "bad.gif"):
            with self.assertRaises(ValueError):
                self.agent.render(self.project_id, filename=filename)
        folder = Path(self.project["path"]) / "exports"
        folder.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "inside"):
            self.agent.render(self.project_id)

    def test_frame_cannot_write_through_external_preview_symlink(self):
        folder = Path(self.project["path"]) / "previews"
        folder.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "inside"):
            self.agent.frame(self.project_id)

    def test_render_start_exception_finishes_job_as_failed(self):
        self.studio.render_export.side_effect = RuntimeError("Cannot start renderer")
        job = self.agent.render(self.project_id)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"], "Cannot start renderer")

    def test_jobs_report_failures_and_bound_completed_history(self):
        job = self.agent._job("fixture", lambda: None)
        fail = self.studio.worker.call_args.args[2]
        fail(ValueError("Fixture failed"))
        self.assertEqual(self.agent.get_job(job["id"])["state"], "failed")
        self.assertFalse(self.agent.get_job(job["id"])["cancellable"])
        for number in range(105):
            job = self.agent._job("fixture", lambda: None)
            self.studio.worker.call_args.args[1](number)
        self.assertEqual(len(self.agent.jobs), 100)

    def test_saved_custom_source_outside_take_is_rejected(self):
        self.project["source"] = "/tmp/external.mkv"
        save_project(self.project)
        with self.assertRaisesRegex(ValueError, "source"):
            self.agent.project(self.project_id)

    def test_engine_recipe_rejects_wrong_types(self):
        for patch_values in ({"trim_end": "3"}, {"zoom_start": True}, {"fps": 1.5}, {"quality": False}):
            with self.assertRaises(ValueError):
                edit_options(self.project, patch_values)
