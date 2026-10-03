"""HUD/Studio lifecycle checks without connecting to a display or recording."""

from types import MethodType, SimpleNamespace
from pathlib import Path
from unittest import TestCase, main, skipIf
from unittest.mock import Mock, patch

try:
    from lumen import hud, ui
except (ImportError, ValueError):
    ui = None
    hud = None


def application(view="hud"):
    app = SimpleNamespace(
        window=Mock(), hud=Mock(), view=view, ensure_windows=Mock(), quit=Mock()
    )
    for method in (
        "visible_window",
        "present_current",
        "show_studio",
        "show_hud",
        "studio_action",
        "request_quit",
        "do_activate",
        "do_command_line",
    ):
        setattr(app, method, MethodType(getattr(ui.LumenApplication, method), app))
    app.activate = app.do_activate
    return app


def command(*flags):
    item = Mock()
    item.get_arguments.return_value = ["lumen", *flags]
    return item


@skipIf(ui is None, "GTK Python bindings unavailable")
class ApplicationViewTests(TestCase):
    def test_ensure_windows_creates_one_controller_and_one_hud(self):
        app = SimpleNamespace(window=None, hud=None)
        with (
            patch.object(ui, "StudioWindow") as controller,
            patch.object(hud, "HudWindow") as compact,
        ):
            ui.LumenApplication.ensure_windows(app)
            ui.LumenApplication.ensure_windows(app)
        controller.assert_called_once_with(app)
        compact.assert_called_once_with(app, controller.return_value)

    def test_activation_presents_current_view_without_replacing_controller(self):
        for view in ("hud", "studio"):
            with self.subTest(view=view):
                app = application(view)
                studio, hud = app.window, app.hud
                app.do_activate()
                self.assertIs(app.window, studio)
                self.assertIs(app.hud, hud)
                (hud if view == "hud" else studio).present.assert_called_once()
                (studio if view == "hud" else hud).present.assert_not_called()

    def test_return_to_hud_saves_edits_and_pauses_preview_without_stopping_work(self):
        app = application("studio")
        project = {"path": "/tmp/existing-project"}
        app.window.project = project
        self.assertTrue(app.show_hud())
        self.assertEqual(app.view, "hud")
        app.window.preserve_edits.assert_called_once_with()
        app.window.video.get_media_stream.return_value.pause.assert_called_once()
        app.window.set_visible.assert_called_once_with(False)
        app.hud.present.assert_called_once()
        self.assertIs(app.window.project, project)
        app.window.stop_recording.assert_not_called()
        app.window.shutdown_capture.assert_not_called()
        app.quit.assert_not_called()

    def test_failed_preservation_still_switches_view_without_losing_shared_draft(self):
        app = application("studio")
        project = app.window.project
        app.window.preserve_edits.side_effect = OSError("disk full")
        self.assertTrue(app.show_hud())
        self.assertEqual(app.view, "hud")
        app.window.set_visible.assert_called_once_with(False)
        app.hud.present.assert_called_once()
        self.assertIs(app.window.project, project)
        app.window.toast.assert_called_once()

    def test_open_studio_reuses_project_and_recorder_and_selects_requested_page(self):
        app = application()
        project, recorder = app.window.project, app.window.recorder
        app.show_studio("library")
        self.assertEqual(app.view, "studio")
        app.hud.set_visible.assert_called_once_with(False)
        app.window.show_page.assert_called_once_with("library")
        app.window.present.assert_called_once()
        self.assertIs(app.window.project, project)
        self.assertIs(app.window.recorder, recorder)

    def test_empty_cli_activation_respects_either_current_view(self):
        for view in ("hud", "studio"):
            with self.subTest(view=view):
                app = application(view)
                self.assertEqual(app.do_command_line(command()), 0)
                self.assertEqual(app.view, view)
                (app.hud if view == "hud" else app.window).present.assert_called_once()

    def test_explicit_cli_views_switch_and_conflicting_flags_do_not_change_view(self):
        app = application()
        self.assertEqual(app.do_command_line(command("--studio")), 0)
        self.assertEqual(app.view, "studio")
        self.assertEqual(app.do_command_line(command("--hud")), 0)
        self.assertEqual(app.view, "hud")
        invalid = command("--hud", "--studio")
        self.assertEqual(app.do_command_line(invalid), 2)
        self.assertEqual(app.view, "hud")
        invalid.printerr_literal.assert_called_once()

    def test_inactive_control_command_does_not_create_hidden_recording_session(self):
        for flag in ("--pause", "--stop", "--save-replay"):
            app = application()
            app.window = None
            request = command(flag)
            self.assertEqual(app.do_command_line(request), 1)
            app.ensure_windows.assert_not_called()

    def test_quit_is_guarded_by_controller_and_uses_current_visible_parent(self):
        app = application()
        app.window.confirm_quit.return_value = True
        app.request_quit()
        app.window.confirm_quit.assert_called_once_with(parent=app.hud)
        app.quit.assert_not_called()
        app.window.confirm_quit.return_value = False
        app.request_quit()
        app.quit.assert_called_once()

    def test_studio_close_returns_to_hud_even_while_work_is_active(self):
        app = application("studio")
        studio = app.window
        studio.get_application.return_value = app
        studio.recorder.is_running = True
        self.assertTrue(ui.StudioWindow.on_close(studio))
        self.assertEqual(app.view, "hud")
        studio.stop_recording.assert_not_called()
        studio.cancel_export.assert_not_called()
        app.quit.assert_not_called()

    def test_import_shortcut_opens_studio_but_pause_keeps_hud(self):
        app = application()
        app.studio_action("pause_recording")
        self.assertEqual(app.view, "hud")
        app.window.pause_recording.assert_called_once()
        app.studio_action("import_video")
        self.assertEqual(app.view, "studio")
        app.window.show_page.assert_called_once_with("editor")
        app.window.import_video.assert_called_once()

    def test_replay_save_shortcut_preserves_current_view(self):
        for view in ("hud", "studio"):
            app = application(view)
            app.studio_action("save_replay")
            app.window.save_replay.assert_called_once_with(show=view == "studio")
            self.assertEqual(app.view, view)

    def test_finished_preview_plays_only_when_studio_is_current_and_visible(self):
        for view, visible in (("hud", False), ("studio", False), ("studio", True)):
            with self.subTest(view=view, visible=visible):
                studio = Mock(
                    project={"path": "/tmp/hud-preview-project"}, export_busy=False
                )
                studio.get_application.return_value.view = view
                studio.get_visible.return_value = visible
                destination = Path("/tmp/hud-preview-result.mp4")
                options = ui.ExportOptions()
                ui.StudioWindow.render_export(
                    studio, options, destination, preview=True
                )
                done = studio.worker.call_args.args[1]
                with patch.object(
                    ui, "probe", return_value={"width": 960, "height": 540}
                ):
                    done(destination)
                self.assertEqual(studio.preview_path, destination)
                self.assertIs(studio.preview_options, options)
                media = studio.video.get_media_stream.return_value
                if view == "studio" and visible:
                    media.play.assert_called_once()
                else:
                    media.play.assert_not_called()


@skipIf(ui is None, "GTK Python bindings unavailable")
class QuitGuardTests(TestCase):
    def test_total_preservation_failure_offers_explicit_choice_without_stopping_capture(
        self,
    ):
        studio = Mock(recorder=Mock(is_running=True))
        studio.preserve_edits.side_effect = OSError("disk full")
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(studio, parent=Mock()))
        self.assertEqual(
            [call.args[0] for call in dialog.add_response.call_args_list],
            ["stay", "discard"],
        )
        studio.stop_recording.assert_not_called()
        studio.shutdown_capture.assert_not_called()

    def test_export_or_transcription_never_offers_an_action_that_aborts_work(self):
        for export, transcription in ((True, False), (False, True), (True, True)):
            studio = Mock(
                recorder=Mock(is_running=True),
                replay=Mock(is_running=False),
                capture_busy=False,
                export_busy=export,
                countdown_source=None,
                layers=Mock(transcribing=transcription),
            )
            dialog = Mock()
            with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
                self.assertTrue(ui.StudioWindow.confirm_quit(studio, parent=Mock()))
            choices = [call.args[0] for call in dialog.add_response.call_args_list]
            self.assertEqual(choices, ["stay"])
            studio.stop_recording.assert_not_called()

    def test_active_capture_quits_only_after_explicit_stop_and_save_response(self):
        studio = Mock(
            recorder=Mock(is_running=True),
            replay=Mock(is_running=False),
            capture_busy=False,
            export_busy=False,
            closing=False,
            countdown_source=None,
            layers=Mock(transcribing=False),
        )
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(studio, parent=Mock()))
        response = dialog.connect.call_args.args[1]
        response(dialog, "stay")
        self.assertFalse(studio.closing)
        studio.stop_recording.assert_not_called()
        response(dialog, "save")
        self.assertTrue(studio.closing)
        studio.stop_recording.assert_called_once()

    def test_pending_countdown_blocks_quit_without_stopping_or_starting_capture(self):
        studio = Mock(
            recorder=Mock(is_running=False),
            replay=Mock(is_running=False),
            capture_busy=True,
            export_busy=False,
            countdown_source=123,
            layers=Mock(transcribing=False),
        )
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            self.assertTrue(ui.StudioWindow.confirm_quit(studio, parent=Mock()))
        self.assertEqual(
            [call.args[0] for call in dialog.add_response.call_args_list],
            ["stay", "cancel-countdown"],
        )
        studio.stop_recording.assert_not_called()
        studio.start_backend.assert_not_called()
        studio.cancel_start.assert_not_called()
        response = dialog.connect.call_args.args[1]
        response(dialog, "cancel-countdown")
        studio.cancel_start.assert_called_once()
        studio.get_application.return_value.request_quit.assert_called_once()

    def test_stale_stop_and_quit_response_rechecks_new_export_work(self):
        studio = Mock(
            recorder=Mock(is_running=True),
            replay=Mock(is_running=False),
            capture_busy=False,
            export_busy=False,
            countdown_source=None,
            closing=False,
            layers=Mock(transcribing=False),
        )
        dialog = Mock()
        with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
            ui.StudioWindow.confirm_quit(studio, parent=Mock())
        studio.export_busy = True
        dialog.connect.call_args.args[1](dialog, "save")
        studio.stop_recording.assert_not_called()
        self.assertFalse(studio.closing)
        studio.toast.assert_called_once()


def studio_state(**changes):
    studio = Mock(
        countdown_source=None,
        capture_busy=False,
        devices_ready=True,
        recorder=Mock(is_running=False, status="idle"),
        replay=Mock(is_running=False),
    )
    studio.timer.get_text.return_value = "00:00:02"
    studio.session_status.get_text.return_value = "Ready"
    studio.device_controls.return_value = {
        "mic": {"enabled": False, "available": True, "busy": False, "detail": ""},
        "camera": {"enabled": False, "available": True, "busy": False, "detail": ""},
    }
    for name, value in changes.items():
        setattr(studio, name, value)
    return studio


@skipIf(hud is None, "GTK Python bindings unavailable")
class HudControlTests(TestCase):
    def test_state_is_derived_from_shared_controller_without_second_backend(self):
        studio = studio_state(devices_ready=False)
        self.assertEqual(hud.capture_state(studio), "detecting")
        studio.devices_ready = True
        self.assertEqual(hud.capture_state(studio), "idle")
        studio.capture_busy = True
        self.assertEqual(hud.capture_state(studio), "busy")
        studio.countdown_source = 123
        self.assertEqual(hud.capture_state(studio), "countdown")
        studio.countdown_source = None
        studio.recorder.is_running = True
        studio.recorder.status = "recording"
        self.assertEqual(hud.capture_state(studio), "recording")
        studio.recorder.status = "paused"
        self.assertEqual(hud.capture_state(studio), "paused")
        studio.recorder.is_running = False
        studio.replay.is_running = True
        self.assertEqual(hud.capture_state(studio), "replay")

    def test_countdown_primary_cancels_without_starting_or_stopping_backend(self):
        studio = studio_state(countdown_source=123, capture_busy=True)
        compact = Mock(studio=studio)
        hud.HudWindow.primary_action(compact)
        studio.cancel_start.assert_called_once()
        studio.start_recording.assert_not_called()
        studio.stop_recording.assert_not_called()

    def test_idle_primary_uses_existing_controller_and_closes_settings(self):
        studio = studio_state()
        compact = Mock(studio=studio)
        hud.HudWindow.primary_action(compact)
        compact.settings_popover.popdown.assert_called_once()
        studio.start_recording.assert_called_once_with()
        compact.refresh.assert_called_once()

    def test_active_primary_stops_same_controller(self):
        studio = studio_state()
        studio.recorder.is_running = True
        hud.HudWindow.primary_action(Mock(studio=studio))
        studio.stop_recording.assert_called_once()
        studio.start_recording.assert_not_called()

    def test_replay_primary_saves_without_opening_studio_or_stopping_buffer(self):
        studio = studio_state()
        studio.replay.is_running = True
        hud.HudWindow.primary_action(Mock(studio=studio))
        studio.save_replay.assert_called_once_with(show=False)
        studio.stop_recording.assert_not_called()

    def test_busy_controls_cannot_enqueue_duplicate_operations(self):
        studio = studio_state(capture_busy=True)
        studio.recorder.is_running = True
        compact = Mock(studio=studio)
        hud.HudWindow.primary_action(compact)
        hud.HudWindow.secondary_action(compact)
        studio.start_recording.assert_not_called()
        studio.stop_recording.assert_not_called()
        studio.pause_recording.assert_not_called()

    def test_secondary_shares_pause_and_respects_unsupported_backend(self):
        studio = studio_state()
        studio.recorder.is_running = True
        compact = Mock(studio=studio)
        studio.pause_button.get_sensitive.return_value = False
        hud.HudWindow.secondary_action(compact)
        studio.pause_recording.assert_not_called()
        studio.pause_button.get_sensitive.return_value = True
        hud.HudWindow.secondary_action(compact)
        studio.pause_recording.assert_called_once()

    def test_secondary_starts_replay_from_idle_and_stops_active_buffer(self):
        studio = studio_state()
        compact = Mock(studio=studio)
        hud.HudWindow.secondary_action(compact)
        studio.start_recording.assert_called_once_with(replay=True)
        studio.replay.is_running = True
        hud.HudWindow.secondary_action(compact)
        studio.stop_recording.assert_called_once()

    def test_paused_state_has_resume_control_and_shared_elapsed_time(self):
        studio = studio_state()
        studio.recorder.is_running = True
        studio.recorder.status = "paused"
        compact = Mock(studio=studio)
        compact.source_summary.return_value = ("Display", "Silent")
        hud.HudWindow.refresh(compact)
        compact.status_label.set_text.assert_called_once_with("Paused")
        compact.time_label.set_text.assert_called_once_with("00:00:02")
        compact.primary_button.set_label.assert_called_once_with("Stop & save")
        compact.secondary_button.set_tooltip_text.assert_called_once_with(
            "Resume recording · Ctrl P"
        )

    def test_pending_settings_change_during_capture_reverts_to_controller(self):
        studio = studio_state()
        studio.recorder.is_running = True
        compact = Mock(studio=studio, _syncing=False)
        compact.controls = {"audio": Mock()}
        hud.HudWindow.setting_changed(compact, "audio")
        compact.sync_setting.assert_called_once_with("audio")
        studio.audio.set_selected.assert_not_called()

    def test_idle_setting_change_updates_shared_controller_selection(self):
        studio = studio_state()
        compact = Mock(studio=studio, _syncing=False)
        compact.controls = {"audio": Mock()}
        compact.controls["audio"].get_selected.return_value = 3
        hud.HudWindow.setting_changed(compact, "audio")
        studio.audio.set_selected.assert_called_once_with(3)

    def test_hud_close_uses_guarded_application_quit(self):
        compact = Mock()
        self.assertTrue(hud.HudWindow.on_close(compact))
        compact.get_application.return_value.request_quit.assert_called_once()
        compact.studio.stop_recording.assert_not_called()


@skipIf(hud is None, "GTK Python bindings unavailable")
class HudDeviceTests(TestCase):
    def test_confirmed_device_state_is_explicit_and_pending_never_predicts_on(self):
        studio = studio_state()
        states = studio.device_controls.return_value
        states["mic"].update(busy=True, detail="Opening the selected microphone")
        states["camera"].update(enabled=True)
        compact = Mock(studio=studio)
        hud.HudWindow.refresh_devices(compact)
        compact.mic_label.set_text.assert_called_once_with("Mic muted")
        compact.mic_button.set_sensitive.assert_called_once_with(False)
        compact.mic_spinner.set_spinning.assert_called_once_with(True)
        self.assertIn(
            "Change in progress", compact.mic_button.set_tooltip_text.call_args.args[0]
        )
        compact.camera_label.set_text.assert_called_once_with("Camera on")
        compact.camera_button.set_sensitive.assert_called_once_with(True)
        compact.camera_icon.set_from_icon_name.assert_called_once_with(
            "camera-web-symbolic"
        )

    def test_unavailable_devices_explain_why_and_cannot_be_toggled(self):
        studio = studio_state()
        states = studio.device_controls.return_value
        states["camera"].update(available=False, detail="No camera was found.")
        compact = Mock(studio=studio)
        hud.HudWindow.refresh_devices(compact)
        compact.camera_label.set_text.assert_called_once_with("Camera off")
        self.assertIn(
            "No camera was found",
            compact.camera_button.set_tooltip_text.call_args.args[0],
        )
        hud.HudWindow.device_action(compact, "camera")
        studio.toggle_camera.assert_not_called()
        compact.refresh.assert_not_called()

    def test_device_operations_use_controller_once_without_changing_selection(self):
        for device, method in (
            ("mic", "toggle_microphone"),
            ("camera", "toggle_camera"),
        ):
            with self.subTest(device=device):
                studio = studio_state()
                studio.recorder.is_running = True
                state = studio.device_controls.return_value[device]
                getattr(studio, method).side_effect = lambda: state.update(busy=True)
                compact = Mock(studio=studio)
                hud.HudWindow.device_action(compact, device)
                hud.HudWindow.device_action(compact, device)
                getattr(studio, method).assert_called_once_with()
                studio.audio.set_selected.assert_not_called()
                studio.webcam.set_selected.assert_not_called()
                self.assertFalse(state["enabled"])
                compact.refresh.assert_called_once()

    def test_failed_device_operation_keeps_confirmed_state_and_surfaces_error(self):
        studio = studio_state()
        studio.toggle_microphone.side_effect = RuntimeError(
            "Microphone connection failed"
        )
        compact = Mock(studio=studio)
        hud.HudWindow.device_action(compact, "mic")
        hud.HudWindow.refresh_devices(compact)
        studio.error.assert_called_once()
        self.assertIn("connection failed", str(studio.error.call_args.args[0]))
        compact.mic_label.set_text.assert_called_once_with("Mic muted")
        compact.mic_button.remove_css_class.assert_any_call("hud-device-on")
        compact.refresh.assert_called_once()

    def test_buttons_delegate_to_the_matching_device_operation(self):
        compact = Mock()
        hud.HudWindow.mic_action(compact)
        hud.HudWindow.camera_action(compact)
        self.assertEqual(
            [call.args for call in compact.device_action.call_args_list],
            [("mic",), ("camera",)],
        )

    def test_active_audio_summary_reflects_live_mute_not_initial_audio_recipe(self):
        studio = studio_state()
        studio.recorder.is_running = True
        studio.recorder.project = {
            "capture": {
                "mode": "monitor",
                "monitor": "DP-1",
                "fps": 60,
                "audio": "both",
            }
        }
        compact = Mock(studio=studio)
        self.assertEqual(
            hud.HudWindow.source_summary(compact),
            ("Display · DP-1", "Desktop audio · 60 fps"),
        )
        studio.device_controls.return_value["mic"]["enabled"] = True
        self.assertEqual(
            hud.HudWindow.source_summary(compact)[1], "Desktop + microphone · 60 fps"
        )
        studio.recorder.project["capture"]["audio"] = "none"
        self.assertEqual(
            hud.HudWindow.source_summary(compact)[1], "Microphone · 60 fps"
        )
        studio.device_controls.return_value["mic"]["enabled"] = False
        self.assertEqual(hud.HudWindow.source_summary(compact)[1], "Silent · 60 fps")

    def test_camera_device_choice_changes_only_idle_controller_selection(self):
        studio = studio_state()
        compact = Mock(studio=studio, _syncing=False)
        compact.controls = {"webcam": Mock()}
        compact.controls["webcam"].get_selected.return_value = 2
        hud.HudWindow.setting_changed(compact, "webcam")
        studio.webcam.set_selected.assert_called_once_with(2)
        studio.toggle_camera.assert_not_called()
        studio.recorder.is_running = True
        compact.controls["webcam"].get_selected.return_value = 1
        hud.HudWindow.setting_changed(compact, "webcam")
        compact.sync_setting.assert_called_once_with("webcam")
        self.assertEqual(studio.webcam.set_selected.call_count, 1)


if __name__ == "__main__":
    main()
