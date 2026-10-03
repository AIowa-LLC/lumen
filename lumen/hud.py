"""Compact capture controls for the Studio's existing recording session."""

from __future__ import annotations

from dataclasses import asdict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Pango  # noqa: E402


def _box(vertical=False, spacing=8):
    return Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL
        if vertical
        else Gtk.Orientation.HORIZONTAL,
        spacing=spacing,
    )


def _label(text, css=None):
    widget = Gtk.Label(label=text, xalign=0)
    if css:
        widget.add_css_class(css)
    return widget


def _icon_button(icon, description, callback):
    widget = Gtk.Button(icon_name=icon)
    widget.set_tooltip_text(description)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [description])
    widget.connect("clicked", lambda *_: callback())
    return widget


def _selection(widget):
    item = widget.get_selected_item()
    return item.get_string() if item is not None else "None available"


def _device_button(icon, text, callback):
    button = Gtk.Button()
    button.add_css_class("hud-device")
    content = _box(False, 6)
    image = Gtk.Image.new_from_icon_name(icon)
    label = _label(text)
    spinner = Gtk.Spinner()
    spinner.set_visible(False)
    content.append(image)
    content.append(label)
    content.append(spinner)
    button.set_child(content)
    button.connect("clicked", lambda *_: callback())
    return button, image, label, spinner


def capture_state(studio) -> str:
    """Derive presentation state without owning a second recording lifecycle."""
    if studio.countdown_source:
        return "countdown"
    if studio.recorder.is_running:
        return "paused" if studio.recorder.status == "paused" else "recording"
    if studio.replay.is_running:
        return "replay"
    if studio.capture_busy:
        return "busy"
    return "idle" if studio.devices_ready else "detecting"


class HudWindow(Adw.ApplicationWindow):
    def __init__(self, application, studio):
        super().__init__(
            application=application,
            title="Lumen — Recorder",
            default_width=540,
            default_height=180,
            resizable=False,
        )
        # AdwWindow initializes a 360×200 minimum separately from default_size.
        self.set_size_request(540, 180)
        self.studio = studio
        self.add_css_class("lumen-hud")
        self.set_icon_name("io.github.lumen.Recorder")
        self.connect("close-request", self.on_close)
        self._syncing = False
        self.controls = {}
        self.setting_rows = {}
        self._connections = []
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        shell = _box(True, 10)
        shell.add_css_class("hud-shell")
        for edge in ("top", "bottom"):
            getattr(shell, "set_margin_" + edge)(12)
        for edge in ("start", "end"):
            getattr(shell, "set_margin_" + edge)(16)
        self.toast_overlay.set_child(shell)

        header_handle = Gtk.WindowHandle()
        header = _box(False, 8)
        header_handle.set_child(header)
        shell.append(header_handle)
        brand = _label("lumen", "hud-brand")
        brand.set_tooltip_text("Drag here to move the recorder")
        header.append(brand)
        self.status_dot = _label("●", "hud-dot")
        header.append(self.status_dot)
        self.status_label = _label("Ready", "hud-status")
        self.status_label.set_hexpand(True)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_max_width_chars(14)
        header.append(self.status_label)
        self.time_label = _label("00:00:00", "hud-time")
        header.append(self.time_label)
        self.studio_button = Gtk.Button(label="Open Studio")
        self.studio_button.add_css_class("hud-studio")
        self.studio_button.set_tooltip_text(
            "Open the full editor and recording library"
        )
        self.studio_button.connect("clicked", lambda *_: self.open_studio())
        header.append(self.studio_button)
        self.settings_button = Gtk.MenuButton(icon_name="emblem-system-symbolic")
        self.settings_button.set_tooltip_text("Recording settings")
        self.settings_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ["Recording settings"]
        )
        self.settings_button.add_css_class("hud-icon")
        header.append(self.settings_button)
        self.close_button = _icon_button(
            "window-close-symbolic", "Quit Lumen", lambda: self.on_close()
        )
        self.close_button.add_css_class("hud-icon")
        header.append(self.close_button)

        body = _box(False, 12)
        body.set_valign(Gtk.Align.CENTER)
        shell.append(body)
        source = _box(True, 4)
        source.set_hexpand(True)
        self.source_label = _label("Detecting displays…", "hud-source")
        self.source_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.source_label.set_max_width_chars(27)
        source.append(self.source_label)
        self.audio_label = _label("Silent", "hud-audio")
        self.audio_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.audio_label.set_max_width_chars(30)
        source.append(self.audio_label)
        body.append(source)
        self.secondary_button = _icon_button(
            "media-playlist-repeat-symbolic",
            "Start replay buffer",
            self.secondary_action,
        )
        self.secondary_button.add_css_class("hud-secondary")
        self.secondary_button.set_valign(Gtk.Align.CENTER)
        body.append(self.secondary_button)
        self.primary_button = Gtk.Button(label="Record")
        self.primary_button.add_css_class("hud-primary")
        self.primary_button.set_size_request(142, 44)
        self.primary_button.connect("clicked", lambda *_: self.primary_action())
        body.append(self.primary_button)
        device_row = _box(False, 8)
        self.mic_button, self.mic_icon, self.mic_label, self.mic_spinner = (
            _device_button(
                "microphone-sensitivity-muted-symbolic", "Mic muted", self.mic_action
            )
        )
        self.camera_button, self.camera_icon, self.camera_label, self.camera_spinner = (
            _device_button("camera-disabled-symbolic", "Camera off", self.camera_action)
        )
        device_row.append(self.mic_button)
        device_row.append(self.camera_button)
        self.detail_label = _label(
            "Ctrl R to record  ·  Ctrl ⇧ R to stop", "hud-detail"
        )
        self.detail_label.set_hexpand(True)
        self.detail_label.set_xalign(1)
        self.detail_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail_label.set_max_width_chars(38)
        device_row.append(self.detail_label)
        shell.append(device_row)
        self._build_settings()
        self.refresh()

    def _build_settings(self):
        self.settings_popover = Gtk.Popover()
        self.settings_popover.add_css_class("hud-settings")
        self.settings_popover.set_position(Gtk.PositionType.BOTTOM)
        content = _box(True, 12)
        content.set_size_request(300, -1)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, "set_margin_" + edge)(14)
        content.append(_label("RECORDING SETTINGS", "hud-settings-title"))
        self.settings_fields = _box(True, 10)
        content.append(self.settings_fields)
        for name, title in (
            ("mode", "Capture"),
            ("monitor", "Display"),
            ("window_picker", "Window"),
            ("audio", "Audio"),
            ("mic", "Microphone"),
            ("webcam", "Camera"),
            ("delay", "Countdown"),
            ("replay_duration", "Replay duration"),
        ):
            original = getattr(self.studio, name)
            widget = Gtk.DropDown(model=original.get_model())
            widget.set_hexpand(True)
            widget.set_enable_search(
                name in {"monitor", "window_picker", "mic", "webcam"}
            )
            widget.update_property([Gtk.AccessibleProperty.LABEL], [title])
            factory = Gtk.SignalListItemFactory()

            def setup(_, item):
                item.set_child(
                    Gtk.Label(
                        xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=27
                    )
                )

            def bind(_, item):
                item.get_child().set_text(item.get_item().get_string())

            factory.connect("setup", setup)
            factory.connect("bind", bind)
            widget.set_factory(factory)
            self.controls[name] = widget
            row = _box(True, 4)
            row.append(_label(title, "hud-setting-label"))
            row.append(widget)
            self.setting_rows[name] = row
            self.settings_fields.append(row)
            self.sync_setting(name)
            widget.connect(
                "notify::selected", lambda *_, key=name: self.setting_changed(key)
            )
            for signal in ("notify::model", "notify::selected"):
                connection = original.connect(
                    signal, lambda *_, key=name: self.sync_setting(key)
                )
                self._connections.append((original, connection))

        self.hide_check = Gtk.CheckButton(label="Hide controls during capture")
        self.hide_check.set_tooltip_text(
            "Controls inside the capture area appear in recordings. "
            "Open Lumen again to show the controls and stop."
        )
        self.hide_check.set_active(self.studio.hide_recording.get_active())
        self.hide_check.connect("toggled", self.hide_changed)
        self.settings_fields.append(self.hide_check)
        connection = self.studio.hide_recording.connect(
            "toggled", lambda *_: self.sync_hide_setting()
        )
        self._connections.append((self.studio.hide_recording, connection))
        hint = _label(
            "Device choices apply to your next recording.", "hud-setting-label"
        )
        hint.set_wrap(True)
        content.append(hint)
        advanced = Gtk.Button(label="More settings in Studio")
        advanced.connect("clicked", lambda *_: self.open_studio("capture"))
        content.append(advanced)
        self.settings_popover.set_child(content)
        self.settings_button.set_popover(self.settings_popover)
        self._update_setting_rows()

    def sync_setting(self, name):
        original, widget = getattr(self.studio, name), self.controls[name]
        self._syncing = True
        try:
            if widget.get_model() is not original.get_model():
                widget.set_model(original.get_model())
            if widget.get_selected() != original.get_selected():
                widget.set_selected(original.get_selected())
        finally:
            self._syncing = False
        self._update_setting_rows()

    def setting_changed(self, name):
        if self._syncing:
            return
        if (
            self.studio.capture_busy
            or self.studio.recorder.is_running
            or self.studio.replay.is_running
        ):
            self.sync_setting(name)
            return
        getattr(self.studio, name).set_selected(self.controls[name].get_selected())
        self._update_setting_rows()
        self.refresh()

    def _update_setting_rows(self):
        mode = self.studio.mode.get_selected()
        for name, visible in (
            ("monitor", mode == 0),
            ("window_picker", mode == 2),
        ):
            if name in self.setting_rows:
                self.setting_rows[name].set_visible(visible)

    def sync_hide_setting(self):
        self._syncing = True
        try:
            self.hide_check.set_active(self.studio.hide_recording.get_active())
        finally:
            self._syncing = False

    def hide_changed(self, *_):
        if self._syncing:
            return
        if (
            self.studio.capture_busy
            or self.studio.recorder.is_running
            or self.studio.replay.is_running
        ):
            self.sync_hide_setting()
            return
        self.studio.hide_recording.set_active(self.hide_check.get_active())

    def source_summary(self):
        studio = self.studio
        active = None
        if studio.recorder.is_running and isinstance(studio.recorder.project, dict):
            active = studio.recorder.project.get("capture")
        elif studio.replay.is_running and studio.replay.options is not None:
            active = asdict(studio.replay.options)
        if isinstance(active, dict):
            mode = {
                "monitor": "Display",
                "region": "Region",
                "window": "Window area",
            }.get(active.get("mode"), "Display")
            source = f"{mode} · {active.get('monitor', 'Selected display')}"
            audio_mode = active.get("audio", "none")
            microphone = self.studio.device_controls()["mic"]["enabled"]
            desktop = audio_mode in {"desktop", "both"}
            audio_mode = (
                "both"
                if desktop and microphone
                else "desktop"
                if desktop
                else "mic"
                if microphone
                else "none"
            )
            audio = {
                "none": "Silent",
                "desktop": "Desktop audio",
                "mic": "Microphone",
                "both": "Desktop + microphone",
            }[audio_mode]
            return source, f"{audio} · {active.get('fps', 60)} fps"
        mode = studio.mode.get_selected()
        if mode == 2:
            source = "Window · " + _selection(studio.window_picker)
        elif mode == 1:
            source = "Region · select an area to record"
        else:
            source = "Display · " + _selection(studio.monitor).split("  ·  ")[0]
        return source, f"{_selection(studio.audio)} · {_selection(studio.fps)}"

    def refresh(self):
        studio = self.studio
        state = capture_state(studio)
        busy = studio.capture_busy
        active = state in {"recording", "paused", "replay"}
        available = studio.devices_ready and not busy and not active
        names = {
            "idle": "Ready",
            "detecting": "Connecting",
            "countdown": "Countdown",
            "busy": "Working",
            "recording": "Recording",
            "paused": "Paused",
            "replay": "Buffering",
        }
        self.status_label.set_text(names[state])
        self.time_label.set_text(studio.timer.get_text() or "00:00:00")
        for style, enabled in (("hud-live", active), ("hud-paused", state == "paused")):
            (self.add_css_class if enabled else self.remove_css_class)(style)
        source, audio = self.source_summary()
        self.source_label.set_text(source)
        self.source_label.set_tooltip_text(source)
        self.audio_label.set_text(audio)
        self.audio_label.set_tooltip_text(audio)
        status = studio.session_status.get_text()
        detail = (
            status
            if busy or active or state == "countdown"
            else "Ctrl R to record  ·  Ctrl ⇧ R to stop"
        )
        if state == "detecting":
            detail = "Connecting to your displays and audio devices…"
        self.detail_label.set_text(detail)
        self.detail_label.set_tooltip_text(detail)
        self.settings_fields.set_sensitive(available)
        self.refresh_devices()

        if state == "countdown":
            title, tooltip, sensitive = (
                "Cancel countdown",
                "Cancel this recording countdown",
                True,
            )
        elif state in {"recording", "paused"}:
            title, tooltip, sensitive = (
                "Stop & save",
                "Stop and save this recording · Ctrl Shift R",
                not busy,
            )
        elif state == "replay":
            title, tooltip, sensitive = (
                "Save replay",
                "Save recent footage and keep buffering · Ctrl Shift S",
                not busy,
            )
        elif state == "busy":
            title = "Saving…" if "Saving" in status else "Starting…"
            tooltip, sensitive = (
                "Please wait for the capture operation to finish",
                False,
            )
        else:
            title, tooltip = "Record", "Start recording · Ctrl R"
            sensitive = available and studio.record_button.get_sensitive()
        self.primary_button.set_label(title)
        self.primary_button.set_tooltip_text(tooltip)
        self.primary_button.set_sensitive(sensitive)
        (
            self.primary_button.add_css_class
            if state in {"recording", "paused"}
            else self.primary_button.remove_css_class
        )("hud-stop")

        self.secondary_button.set_visible(state not in {"countdown", "busy"})
        if state in {"recording", "paused"}:
            paused = state == "paused"
            icon = (
                "media-playback-start-symbolic"
                if paused
                else "media-playback-pause-symbolic"
            )
            description = (
                "Resume recording · Ctrl P" if paused else "Pause recording · Ctrl P"
            )
            sensitive = not busy and studio.pause_button.get_sensitive()
        elif state == "replay":
            icon, description, sensitive = (
                "media-playback-stop-symbolic",
                "Stop buffer · unsaved footage is discarded",
                not busy,
            )
        else:
            icon, description = "media-playlist-repeat-symbolic", "Start replay buffer"
            sensitive = available and studio.replay_start_button.get_sensitive()
        self.secondary_button.set_icon_name(icon)
        self.secondary_button.set_tooltip_text(description)
        self.secondary_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [description]
        )
        self.secondary_button.set_sensitive(sensitive)

    def refresh_devices(self):
        """Display confirmed state; a pending operation never predicts success."""
        states = self.studio.device_controls()
        for key, name, off, on_icon, off_icon in (
            (
                "mic",
                "Mic",
                "muted",
                "audio-input-microphone-symbolic",
                "microphone-sensitivity-muted-symbolic",
            ),
            (
                "camera",
                "Camera",
                "off",
                "camera-web-symbolic",
                "camera-disabled-symbolic",
            ),
        ):
            state = states[key]
            enabled, busy = state["enabled"], state["busy"]
            button = getattr(self, key + "_button")
            text = f"{name} {'on' if enabled else off}"
            getattr(self, key + "_label").set_text(text)
            getattr(self, key + "_icon").set_from_icon_name(
                on_icon if enabled else off_icon
            )
            spinner = getattr(self, key + "_spinner")
            spinner.set_visible(busy)
            spinner.set_spinning(busy)
            (button.add_css_class if enabled else button.remove_css_class)(
                "hud-device-on"
            )
            (button.add_css_class if busy else button.remove_css_class)(
                "hud-device-busy"
            )
            button.set_sensitive(state["available"] and not busy)
            if key == "mic":
                action = "Mute microphone" if enabled else "Unmute microphone"
                description = f"Microphone {'on' if enabled else 'muted'}. {action}."
            else:
                action = "Turn camera off" if enabled else "Turn camera on"
                description = f"{text}. {action}."
            if busy:
                description += " Change in progress."
            detail = state.get("detail", "")
            if detail:
                description += " " + detail
            button.set_tooltip_text(description)
            button.update_property([Gtk.AccessibleProperty.LABEL], [description])

    def device_action(self, kind):
        state = self.studio.device_controls()[kind]
        if state["busy"] or not state["available"]:
            return
        try:
            method = "toggle_microphone" if kind == "mic" else "toggle_camera"
            getattr(self.studio, method)()
        except Exception as exc:
            self.studio.error(exc)
        self.refresh()

    def mic_action(self):
        self.device_action("mic")

    def camera_action(self):
        self.device_action("camera")

    def primary_action(self):
        studio = self.studio
        if studio.countdown_source:
            studio.cancel_start()
        elif studio.capture_busy:
            return
        elif studio.recorder.is_running:
            studio.stop_recording()
        elif studio.replay.is_running:
            studio.save_replay(show=False)
        elif studio.devices_ready and studio.record_button.get_sensitive():
            self.settings_popover.popdown()
            studio.start_recording()
        self.refresh()

    def secondary_action(self):
        studio = self.studio
        if studio.capture_busy:
            return
        if studio.recorder.is_running:
            if studio.pause_button.get_sensitive():
                studio.pause_recording()
        elif studio.replay.is_running:
            studio.stop_recording()
        elif studio.devices_ready and studio.replay_start_button.get_sensitive():
            self.settings_popover.popdown()
            studio.start_recording(replay=True)
        self.refresh()

    def open_studio(self, page=None):
        self.settings_popover.popdown()
        self.get_application().show_studio(page)

    def toast(self, text):
        self.toast_overlay.add_toast(Adw.Toast(title=str(text), timeout=5))

    def on_close(self, *_):
        self.get_application().request_quit()
        return True
