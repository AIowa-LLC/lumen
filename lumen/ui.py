"""Native GTK studio. All capture and encoding work stays off the UI thread."""
# ruff: noqa: E402

from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .capture import CaptureOptions, Recorder  # noqa: E402
from .editor import (
    ExportCancelled,
    Exporter,
    ExportOptions,
    audio_modes,
    probe,
    suggest_zoom,
    thumbnail,
)  # noqa: E402
from .project import (
    create_project,
    library_root,
    list_projects,
    load_project,
    load_settings,
    save_project,
    save_settings,
)  # noqa: E402
from .replay import ReplayBuffer  # noqa: E402
from .recovery import list_recoveries, read_recovery, write_recovery  # noqa: E402
from .layer_ui import (  # noqa: E402
    LayerPanel, LayerTimeline, clean_layers, draft_matches_project, draw_guides,
)
from .overlays import validate_project_overlays  # noqa: E402
from .wallpaper_ui import WallpaperPanel  # noqa: E402
from .wallpapers import BACKGROUND_STYLES  # noqa: E402
from .system import (
    diagnostics,
    get_audio_sources,
    get_cameras,
    get_monitors,
    get_windows,
    select_region,
)  # noqa: E402


def label(text, css=None, wrap=False):
    w = Gtk.Label(label=text, xalign=0)
    if css:
        for c in css.split():
            w.add_css_class(c)
    w.set_wrap(wrap)
    return w


def box(vertical=True, spacing=12, css=None):
    w = Gtk.Box(
        orientation=Gtk.Orientation.VERTICAL
        if vertical
        else Gtk.Orientation.HORIZONTAL,
        spacing=spacing,
    )
    if css:
        for c in css.split():
            w.add_css_class(c)
    return w


def button(text, callback, css=None, icon=None):
    w = Gtk.Button()
    if icon:
        content = box(False, 9)
        content.append(Gtk.Image.new_from_icon_name(icon))
        content.append(label(text))
        w.set_child(content)
    else:
        w.set_label(text)
    if css:
        for c in css.split():
            w.add_css_class(c)
    w.connect("clicked", lambda *_: callback())
    return w


def dropdown(values, selected=0):
    w = Gtk.DropDown.new_from_strings(values or ["None available"])
    selected = (
        selected if isinstance(selected, int) and 0 <= selected < len(values) else 0
    )
    w.set_selected(selected)
    factory = Gtk.SignalListItemFactory()

    def setup(_, item):
        text = Gtk.Label(
            xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=22
        )
        item.set_child(text)

    def bind(_, item):
        item.get_child().set_text(item.get_item().get_string())
        item.get_child().set_tooltip_text(item.get_item().get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    w.set_factory(factory)
    w.set_hexpand(True)
    return w


def spin(minimum, maximum, step, value, digits=0):
    w = Gtk.SpinButton.new_with_range(minimum, maximum, step)
    w.set_value(value)
    w.set_digits(digits)
    w.set_width_chars(5)
    w.set_max_width_chars(7)
    w.set_hexpand(True)
    return w


def field(parent, title, widget, hint=None):
    item = box(True, 6)
    item.append(label(title, "small muted"))
    item.append(widget)
    if hint:
        item.append(label(hint, "small muted", True))
    parent.append(item)
    return widget


def scroll(child):
    w = Gtk.ScrolledWindow()
    w.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    w.set_child(child)
    w.set_vexpand(True)
    return w


def clock_text(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class LumenApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=os.environ.get("LUMEN_APPLICATION_ID", "io.github.lumen.Recorder"),
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window = None
        self.hud = None
        self.view = "hud"
        self.agent = None

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        for name, method, shortcuts in [
            ("record", "start_recording", ["<Control>r"]),
            ("stop", "stop_recording", ["<Control><Shift>r"]),
            ("pause", "pause_recording", ["<Control>p"]),
            ("import", "import_video", ["<Control>o"]),
            ("export", "export_video", ["<Control>e"]),
            ("save-replay", "save_replay", ["<Control><Shift>s"]),
        ]:
            action = Gio.SimpleAction.new(name, None)
            action.connect(
                "activate",
                lambda a, p, m=method: self.studio_action(m),
            )
            self.add_action(action)
            self.set_accels_for_action("app." + name, shortcuts)
        for name, method, shortcut in (
            ("hud", self.show_hud, "<Control><Shift>h"),
            ("studio", self.show_studio, "<Control><Shift>o"),
            ("quit", self.request_quit, "<Control>q"),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, callback=method: callback())
            self.add_action(action)
            self.set_accels_for_action("app." + name, [shortcut])
        for filename in ("style.css", "hud.css"):
            css = Gtk.CssProvider()
            css.load_from_path(str(Path(__file__).with_name(filename)))
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        for signum in (signal.SIGTERM, signal.SIGHUP):
            GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, signum, lambda: (self.quit(), False)[1]
            )

    def do_shutdown(self):
        if self.window:
            self.window.cancel_export()
            self.window.layers.cancel_transcription()
            if not self.window.discard_edits_on_quit:
                try:
                    self.window.preserve_edits()
                except Exception as exc:
                    # Shutdown cannot present a usable dialog. Normal Quit has
                    # already checked persistence while the UI is still alive.
                    print(f"Lumen could not preserve edits during shutdown: {exc}", file=sys.stderr)
            self.window.shutdown_capture()
        Adw.Application.do_shutdown(self)

    def ensure_windows(self):
        if self.window is None:
            self.window = StudioWindow(self)
        if self.hud is None:
            from .hud import HudWindow

            self.hud = HudWindow(self, self.window)

    def visible_window(self):
        return self.hud if self.view == "hud" and self.hud is not None else self.window

    def present_current(self):
        self.ensure_windows()
        if self.view == "hud":
            self.hud.refresh()
        self.visible_window().present()

    def show_studio(self, page=None):
        self.ensure_windows()
        self.view = "studio"
        self.hud.set_visible(False)
        if page:
            self.window.show_page(page)
        self.window.present()

    def show_hud(self):
        self.ensure_windows()
        try:
            self.window.preserve_edits()
        except Exception:
            # Both windows share this form; switching views never discards it.
            self.window.toast("Edits are still in memory. Saving will be retried when you quit.")
        stream = self.window.video.get_media_stream()
        if stream:
            stream.pause()
        self.view = "hud"
        self.window.set_visible(False)
        self.hud.refresh()
        self.hud.present()
        return True

    def studio_action(self, method):
        self.ensure_windows()
        if method in {"import_video", "export_video"}:
            self.show_studio("editor")
        if method == "save_replay":
            self.window.save_replay(show=self.view == "studio")
        else:
            getattr(self.window, method)()

    def request_quit(self, discard_edits=False):
        if self.window:
            self.present_current()
            options = {"discard_edits": True} if discard_edits else {}
            if self.window.confirm_quit(parent=self.visible_window(), **options):
                return
        self.quit()

    def do_activate(self):
        self.present_current()

    def do_command_line(self, command):
        args = command.get_arguments()[1:]
        if args == ["--agent-host"]:
            self.ensure_windows()
            command.print_literal(json.dumps({"primary": not command.get_is_remote()}) + "\n")
            return 0
        if args and args[0] == "--agent":
            try:
                if len(args) != 2:
                    raise ValueError("--agent requires one JSON request.")
                self.ensure_windows()
                if self.agent is None:
                    from .agent import AgentController

                    self.agent = AgentController(self.window)
                result = self.agent.request(args[1])
                command.print_literal(json.dumps({"result": result}, allow_nan=False) + "\n")
                return 0
            except Exception as exc:
                command.print_literal(json.dumps({"error": str(exc)}) + "\n")
                return 1
        if self.window is None and any(
            a in args for a in ("--stop", "--pause", "--save-replay")
        ):
            command.printerr_literal("No Lumen recording is running.\n")
            return 1
        if "--studio" in args and "--hud" in args:
            command.printerr_literal("Choose either --hud or --studio.\n")
            return 2
        if "--studio" in args:
            self.show_studio()
        elif "--hud" in args:
            self.show_hud()
        elif (
            self.window is None
            or not args
            or any(a in args for a in ("--record", "--replay", "--smoke-test"))
        ):
            self.activate()
        if "--record" in args:
            self.window.record_when_ready = True
            if self.window.devices_ready:
                self.window.record_when_ready = False
                self.window.start_recording()
        if "--stop" in args:
            self.window.stop_recording()
        if "--pause" in args:
            self.window.pause_recording()
        if "--replay" in args:
            self.window.record_when_ready = "replay"
            if self.window.devices_ready:
                self.window.record_when_ready = False
                self.window.start_recording(replay=True)
        if "--save-replay" in args:
            self.window.save_replay(show=False)
        if "--smoke-test" in args:
            GLib.timeout_add(2500, lambda: (self.quit(), False)[1])
        return 0


class StudioWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(
            application=app,
            title="Lumen — Recording Studio",
            default_width=1280,
            default_height=860,
        )
        self.set_icon_name("io.github.lumen.Recorder")
        self.recorder = Recorder()
        self.replay = ReplayBuffer()
        self.exporter = None
        self.capture_busy = False
        self.shutting_down = False
        self.capture_lifecycle_lock = threading.RLock()
        self.export_busy = False
        self.countdown_source = None
        self.devices_ready = False
        self.record_when_ready = False
        self.project = None
        self.monitors = []
        self.windows = []
        self.microphones = []
        self.settings = load_settings()
        self.device_busy = {"mic": False, "camera": False}
        self.device_errors = {"mic": "", "camera": ""}
        self.refreshing_devices = False
        self.preferred_camera_id = self.settings.get("camera_id")
        self.capture_camera_choice = None
        self.preview_path = None
        self.preview_options = None
        self.preview_directory = tempfile.TemporaryDirectory(prefix="lumen-preview-")
        self.closing = False
        self.discard_edits_on_quit = False
        self.last_recovery = None
        self.recovery_path = None
        self.restored_recovery = None
        self.open_generation = 0
        self.opening_project = None
        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)
        layout = box(True, 0)
        self.toast_overlay.set_child(layout)
        header = Adw.HeaderBar()
        header.set_title_widget(label("LUMEN  /  RECORDING STUDIO", "eyebrow"))
        self.hud_button = button("HUD", app.show_hud, "flat", "view-restore-symbolic")
        self.hud_button.set_tooltip_text(
            "Return to the compact recorder · Ctrl+Shift+H"
        )
        header.pack_end(self.hud_button)
        self.session_status = label("Ready", "small muted")
        header.pack_start(self.session_status)
        self.timer = label("", "timer")
        header.pack_end(self.timer)
        self.stop_button = button(
            "Stop & save",
            self.stop_recording,
            "destructive-action",
            "media-playback-stop-symbolic",
        )
        self.stop_button.set_visible(False)
        header.pack_end(self.stop_button)
        self.pause_button = button(
            "Pause", self.pause_recording, icon="media-playback-pause-symbolic"
        )
        self.pause_button.set_visible(False)
        header.pack_end(self.pause_button)
        self.save_replay_button = button(
            "Save replay",
            self.save_replay,
            "suggested-action",
            "document-save-symbolic",
        )
        self.save_replay_button.set_visible(False)
        header.pack_end(self.save_replay_button)
        layout.append(header)
        body = box(False, 0)
        body.set_vexpand(True)
        layout.append(body)
        sidebar = box(True, 12, "sidebar")
        sidebar.set_size_request(150, -1)
        sidebar.append(label("◉  lumen", "brand"))
        sidebar.append(label("CAPTURE & CREATE", "eyebrow"))
        spacer = box()
        spacer.set_size_request(-1, 22)
        sidebar.append(spacer)
        self.nav = {}
        for name, title, icon in [
            ("capture", "Record", "media-record-symbolic"),
            ("library", "Library", "folder-videos-symbolic"),
            ("editor", "Studio", "document-edit-symbolic"),
            ("health", "System", "preferences-system-symbolic"),
        ]:
            self.nav[name] = button(
                title, lambda n=name: self.show_page(n), "nav", icon
            )
            sidebar.append(self.nav[name])
        stretch = box()
        stretch.set_vexpand(True)
        sidebar.append(stretch)
        sidebar.append(label("NATIVE WAYLAND", "badge"))
        sidebar.append(
            label("Private by design.\nEverything stays here.", "small muted", True)
        )
        sidebar.append(Gtk.Separator())
        sidebar.append(
            label(
                "Ctrl R       Record\nCtrl ⇧ R     Stop\nCtrl P       Pause\nCtrl O       Import",
                "key",
            )
        )
        body.append(sidebar)
        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=140
        )
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        body.append(self.stack)
        self.build_capture()
        self.build_library()
        self.build_editor()
        self.build_health()
        self.show_page("capture")
        self.connect("close-request", self.on_close)
        GLib.timeout_add(250, self.tick)
        self.refresh_devices()

    def page(self, name):
        page = box(True, 22)
        for attr in ("margin_start", "margin_end", "margin_top", "margin_bottom"):
            getattr(page, "set_" + attr)(28)
        self.stack.add_named(page, name)
        return page

    def heading(self, page, title, subtitle, action=None):
        row = box(False, 16)
        text = box(True, 5)
        text.set_hexpand(True)
        text.append(label(title, "page-title"))
        text.append(label(subtitle, "subtitle", True))
        row.append(text)
        if action:
            action.set_valign(Gtk.Align.CENTER)
            row.append(action)
        page.append(row)

    def build_capture(self):
        page = self.page("capture")
        self.heading(
            page,
            "Make a great take.",
            "Native capture. A little polish. Ready to share.",
            button(
                "Refresh devices", self.refresh_devices, "flat", "view-refresh-symbolic"
            ),
        )
        columns = box(False, 22)
        columns.set_vexpand(True)
        page.append(columns)
        left = box(True, 16)
        left.set_hexpand(True)
        columns.append(left)
        self.mode = dropdown(["Entire display", "Select a region", "Window area"])
        self.mode.connect("notify::selected", self.mode_changed)
        left.append(self.mode)
        self.monitor = dropdown(["Detecting displays…"])
        self.monitor.connect("notify::selected", lambda *_: self.update_monitor_info())
        left.append(self.monitor)
        self.window_picker = dropdown(["Detecting windows…"])
        self.window_picker.set_visible(False)
        left.append(self.window_picker)
        preview_box = box(True, 16, "preview")
        preview_box.set_vexpand(True)
        preview_box.set_size_request(320, 260)
        self.monitor_drawing = Gtk.DrawingArea()
        self.monitor_drawing.set_vexpand(True)
        self.monitor_drawing.set_draw_func(self.draw_monitors)
        preview_box.append(self.monitor_drawing)
        self.monitor_info = label("Connecting to Hyprland…", "small muted")
        self.monitor_info.set_halign(Gtk.Align.CENTER)
        self.monitor_info.set_margin_bottom(24)
        preview_box.append(self.monitor_info)
        left.append(preview_box)
        self.capture_hint = label(
            "Your original stays intact. Edit and export as many versions as you like.",
            "small muted",
            True,
        )
        left.append(self.capture_hint)
        info = box(True, 9, "card")
        info.append(label("Made for your desktop", "section-title"))
        info.append(
            label(
                "Direct Wayland capture • Hardware encoding • Local projects",
                "small accent",
                True,
            )
        )
        info.append(
            label(
                "A window area stays in one place. Keep the window visible while recording.",
                "small muted",
                True,
            )
        )
        left.append(info)
        right = box(True, 16, "card")
        right.set_size_request(290, -1)
        columns.append(scroll(right))
        right.append(label("RECORDING SETUP", "eyebrow"))
        row = box(False, 12)
        self.fps = dropdown(
            ["30 fps", "60 fps", "120 fps"], self.settings.get("fps_index", 1)
        )
        self.codec = dropdown(
            ["H.264", "HEVC", "AV1"], self.settings.get("codec_index", 0)
        )
        col1, col2 = box(), box()
        field(col1, "Frame rate", self.fps)
        field(col2, "Codec", self.codec)
        row.append(col1)
        row.append(col2)
        right.append(row)
        self.encoder = field(
            right,
            "Encoding",
            dropdown(
                ["Automatic · GPU first", "GPU only", "CPU compatibility"],
                self.settings.get("encoder_index", 0),
            ),
        )
        self.quality = field(
            right,
            "Quality",
            dropdown(
                ["High", "Very high", "Ultra"], self.settings.get("quality_index", 1)
            ),
        )
        self.resolution = field(
            right,
            "Capture resolution",
            dropdown(["Original", "1920 × 1080", "1280 × 720"]),
        )
        right.append(Gtk.Separator())
        self.audio = field(
            right,
            "Audio",
            dropdown(
                ["Silent", "Desktop audio", "Microphone", "Desktop + microphone"],
                self.settings.get("audio_index", 0),
            ),
        )
        self.mic = field(right, "Microphone device", dropdown(["System default"]))
        self.webcam = field(
            right,
            "Webcam overlay",
            dropdown(["Off"]),
            "A live camera bubble appears inside the recording area. Toggle it from the HUD.",
        )
        self.camera_devices = []
        for widget in (self.audio, self.mic, self.webcam):
            widget.connect("notify::selected", self.capture_device_changed)
        self.cursor = Gtk.Switch(
            active=self.settings.get("cursor", True), valign=Gtk.Align.CENTER
        )
        self.telemetry = Gtk.Switch(
            active=self.settings.get("telemetry", True), valign=Gtk.Align.CENTER
        )
        self.record_clicks = Gtk.Switch(
            active=self.settings.get("record_clicks", True), valign=Gtk.Align.CENTER
        )
        self.record_clicks.set_tooltip_text(
            "Records unmodified left, right, and middle clicks. Existing mouse bindings are preserved."
        )
        for title, widget in [
            ("Show cursor", self.cursor),
            ("Save cursor path for zoom", self.telemetry),
            ("Record editable clicks", self.record_clicks),
        ]:
            r = box(False, 12)
            t = label(title, "small")
            t.set_hexpand(True)
            r.append(t)
            r.append(widget)
            right.append(r)
        self.delay = field(
            right, "Countdown", dropdown(["No delay", "3 seconds", "5 seconds"], 1)
        )
        self.hide_recording = Gtk.CheckButton(
            label="Hide controls during capture", active=False
        )
        self.hide_recording.set_tooltip_text(
            "Controls inside the captured area appear in the recording. Hide them, then launch Lumen again or use your global shortcuts to control the take."
        )
        right.append(self.hide_recording)
        self.record_button = button(
            "Start recording", self.start_recording, "record", "media-record-symbolic"
        )
        self.record_button.set_sensitive(False)
        right.append(self.record_button)
        self.cancel_countdown = button("Cancel countdown", self.cancel_start, "flat")
        self.cancel_countdown.set_visible(False)
        right.append(self.cancel_countdown)
        right.append(label("Ctrl R to start  ·  Ctrl ⇧ R to stop", "key"))
        right.append(Gtk.Separator())
        right.append(label("INSTANT REPLAY", "eyebrow"))
        self.replay_duration = field(
            right,
            "Remember the last…",
            dropdown(["15 seconds", "30 seconds", "60 seconds"], 1),
        )
        self.replay_start_button = button(
            "Start replay buffer",
            lambda: self.start_recording(replay=True),
            "flat",
            "media-playlist-repeat-symbolic",
        )
        right.append(self.replay_start_button)
        right.append(
            label(
                "Keep recent frames in memory. Save a clip when something happens. Stop discards unsaved frames.",
                "small muted",
                True,
            )
        )

    def draw_monitors(self, area, cr, width, height):
        monitors = self.monitors
        if not monitors:
            return
        minx = min(m["x"] for m in monitors)
        miny = min(m["y"] for m in monitors)
        maxx = max(m["x"] + m.get("logical_width", m["width"]) for m in monitors)
        maxy = max(m["y"] + m.get("logical_height", m["height"]) for m in monitors)
        scale = min(
            (width - 70) / max(1, maxx - minx), (height - 90) / max(1, maxy - miny)
        )
        ox = (width - (maxx - minx) * scale) / 2
        oy = (height - (maxy - miny) * scale) / 2 - 8
        for i, m in enumerate(monitors):
            x, y = ox + (m["x"] - minx) * scale, oy + (m["y"] - miny) * scale
            w, h = (
                m.get("logical_width", m["width"]) * scale - 10,
                m.get("logical_height", m["height"]) * scale - 10,
            )
            chosen = i == self.monitor.get_selected()
            cr.set_source_rgb(*((0.20, 0.17, 0.30) if chosen else (0.105, 0.11, 0.16)))
            cr.rectangle(x, y, w, h)
            cr.fill_preserve()
            cr.set_source_rgb(*((0.73, 0.65, 1.0) if chosen else (0.27, 0.28, 0.36)))
            cr.set_line_width(2)
            cr.stroke()
            cr.select_font_face("sans-serif", 0, 1)
            cr.set_font_size(17)
            text = m["name"]
            extents = cr.text_extents(text)
            cr.move_to(x + (w - extents.width) / 2, y + h / 2 + 5)
            cr.show_text(text)
            cr.set_line_width(3)
            cr.move_to(x + w / 2, y + h + 2)
            cr.line_to(x + w / 2, y + h + 15)
            cr.move_to(x + w / 2 - 20, y + h + 15)
            cr.line_to(x + w / 2 + 20, y + h + 15)
            cr.stroke()

    def build_library(self):
        page = self.page("library")
        self.heading(
            page,
            "Your library.",
            "Original takes and saved edits, all on your machine.",
            button(
                "Import video",
                self.import_video,
                "suggested-action",
                "list-add-symbolic",
            ),
        )
        self.library_box = box(True, 12)
        page.append(scroll(self.library_box))
        page.append(
            button(
                "Open recordings folder",
                lambda: self.open_path(library_root()),
                "flat",
                "folder-open-symbolic",
            )
        )

    def build_editor(self):
        page = self.page("editor")
        self.editor_title = label("The finishing touches.", "page-title")
        page.append(self.editor_title)
        self.recovery_banner = Adw.Banner(title="An unfinished edit was kept when Lumen closed.")
        self.recovery_banner.set_button_label("Restore draft")
        self.recovery_banner.set_revealed(False)
        self.recovery_banner.connect("button-clicked", lambda *_: self.restore_recovery())
        page.append(self.recovery_banner)
        self.editor_empty = box(True, 20, "card")
        self.editor_empty.append(label("Start with a recording", "section-title"))
        self.editor_empty.append(
            label(
                "Record your desktop or import a video. Then trim it, add a frame, and give the important part a closer look.",
                "muted",
                True,
            )
        )
        self.editor_empty.append(
            button("Import video", self.import_video, "suggested-action")
        )
        page.append(self.editor_empty)
        self.editor_content = box(False, 20)
        self.editor_content.set_vexpand(True)
        self.editor_content.set_visible(False)
        page.append(self.editor_content)
        left = box(True, 14)
        left.set_hexpand(True)
        self.editor_content.append(left)
        self.frame = box(True, 0, "preview-frame")
        self.frame.set_size_request(260, 160)
        self.video = Gtk.Video()
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        self.video.set_autoplay(False)
        self.video_overlay = Gtk.Overlay()
        self.video_overlay.set_child(self.video)
        self.layer_guides = Gtk.DrawingArea()
        self.layer_guides.set_can_target(False)
        self.layer_guides.set_draw_func(lambda _, cr, w, h: draw_guides(self, cr, w, h))
        self.video_overlay.add_overlay(self.layer_guides)
        self.frame.append(self.video_overlay)
        placement = Gtk.GestureClick(button=1)
        placement.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def place(gesture, count, x, y):
            if hasattr(self, "layers") and self.layers.place(x, y):
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)

        placement.connect("pressed", place)
        self.video.add_controller(placement)
        keys = Gtk.EventControllerKey()

        def key_pressed(_, keyval, code, state):
            if (
                keyval == Gdk.KEY_Escape
                and hasattr(self, "layers")
                and self.layers.pick_target
            ):
                self.layers.cancel_pick()
                return True
            return False

        keys.connect("key-pressed", key_pressed)
        self.add_controller(keys)
        self.aspect = Gtk.AspectFrame(
            xalign=0.5, yalign=0.5, ratio=16 / 9, obey_child=False
        )
        self.aspect.set_child(self.frame)
        self.aspect.set_vexpand(True)
        left.append(self.aspect)
        self.layer_timeline = LayerTimeline(self)
        left.append(self.layer_timeline)
        self.preview_note = label(
            "Source preview · Export applies your trim, speed, zoom, and frame.\nWith two audio tracks, source playback uses the first track; export can mix both.",
            "small muted",
            True,
        )
        left.append(self.preview_note)
        self.playback_error = label("", "small error", True)
        left.append(self.playback_error)
        self.editor_meta = label("", "small accent", True)
        left.append(self.editor_meta)
        actions = box(False, 12)
        actions.append(
            button(
                "External player",
                self.play_external,
                "flat",
                "media-playback-start-symbolic",
            )
        )
        actions.append(
            button(
                "Project folder",
                lambda: self.open_path(self.project["path"]) if self.project else None,
                "flat",
                "folder-open-symbolic",
            )
        )
        left.append(actions)
        preview_actions = box(False, 10)
        self.preview_button = button(
            "Preview edits",
            self.render_preview,
            "suggested-action",
            "media-playback-start-symbolic",
        )
        preview_actions.append(self.preview_button)
        preview_actions.append(button("Show original", self.show_original, "flat"))
        left.append(preview_actions)
        self.export_progress = Gtk.ProgressBar(show_text=True)
        self.export_progress.set_visible(False)
        left.append(self.export_progress)
        self.export_cancel = button("Cancel export", self.cancel_export, "flat")
        self.export_cancel.set_visible(False)
        left.append(self.export_cancel)
        right = box(True, 15, "card")
        right.set_size_request(280, -1)
        inspector_column = box(True, 10)
        self.inspector = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.inspector.set_vexpand(True)
        self.inspector.add_titled(scroll(right), "style", "Style & export")
        self.layers = LayerPanel(self)
        self.layers.add_css_class("card")
        self.layers.set_size_request(280, -1)
        self.inspector.add_titled(scroll(self.layers), "layers", "Captions & layers")
        switcher = Gtk.StackSwitcher(stack=self.inspector)
        switcher.set_halign(Gtk.Align.FILL)
        inspector_column.append(switcher)
        inspector_column.append(self.inspector)
        self.editor_content.append(inspector_column)
        self.project_name = field(right, "Project name", Gtk.Entry())
        self.project_name.connect("activate", lambda *_: self.save_edits())
        right.append(label("CUT & TIMING", "eyebrow"))
        times = box(False, 10)
        self.trim_start = spin(0, 86400, 0.1, 0, 2)
        self.trim_end = spin(0.1, 86400, 0.1, 1, 2)
        for text, widget in [
            ("In · seconds", self.trim_start),
            ("Out · seconds", self.trim_end),
        ]:
            c = box()
            field(c, text, widget)
            times.append(c)
        right.append(times)
        mark_row = box(False, 8)
        mark_row.append(button("Set in here", lambda: self.mark_trim(True), "flat"))
        mark_row.append(button("Set out here", lambda: self.mark_trim(False), "flat"))
        right.append(mark_row)
        self.speed = field(
            right,
            "Playback speed",
            dropdown(["0.5×", "0.75×", "1×", "1.5×", "2×", "3×"], 2),
        )
        right.append(Gtk.Separator())
        right.append(label("COMPOSITION", "eyebrow"))
        self.background = field(
            right, "Background", dropdown(["Midnight", "Violet", "Sand", "None", "Wallpaper"])
        )
        self.wallpapers = WallpaperPanel(self)
        right.append(self.wallpapers)
        self.background.connect("notify::selected", self.update_frame)
        self.padding = field(right, "Frame padding · pixels", spin(0, 300, 8, 64))
        right.append(Gtk.Separator())
        right.append(label("FOLLOW THE ACTION", "eyebrow"))
        self.click_zoom = Gtk.CheckButton(label="Zoom to clicks")
        right.append(self.click_zoom)
        right.append(
            label(
                "Smoothly follows enabled clicks. Edit their positions and timing in Cursor clicks, then Preview edits.",
                "small muted",
                True,
            )
        )
        self.click_zoom_controls = box(True, 10)
        self.click_zoom_amount = field(
            self.click_zoom_controls, "Click zoom amount", spin(1, 4, 0.1, 1.8, 2)
        )
        self.click_zoom_hold = field(
            self.click_zoom_controls,
            "Hold after click · seconds",
            spin(0.1, 10, 0.1, 1.2, 2),
        )
        self.click_zoom_transition = field(
            self.click_zoom_controls,
            "Transition · seconds",
            spin(0.05, 2, 0.05, 0.35, 2),
        )
        self.click_zoom_controls.append(
            label(
                "Nearby clicks keep the view zoomed and pan to the next target. Timing uses the original take; playback speed applies automatically.",
                "small muted",
                True,
            )
        )
        right.append(self.click_zoom_controls)
        self.manual_zoom = box(True, 10)
        self.manual_zoom_expander = Gtk.Expander(label="Manual zoom")
        self.manual_zoom_expander.set_child(self.manual_zoom)
        right.append(self.manual_zoom_expander)
        self.zoom = field(self.manual_zoom, "Zoom amount", spin(1, 4, 0.1, 1, 2))
        center = box(False, 10)
        self.zoom_x = spin(0, 1, 0.05, 0.5, 2)
        self.zoom_y = spin(0, 1, 0.05, 0.5, 2)
        for text, widget in [
            ("Focus X · 0–1", self.zoom_x),
            ("Focus Y · 0–1", self.zoom_y),
        ]:
            c = box()
            field(c, text, widget)
            center.append(c)
        self.manual_zoom.append(center)
        self.timed_zoom = Gtk.CheckButton(label="Timed zoom animation")
        self.manual_zoom.append(self.timed_zoom)
        ztimes = box(False, 10)
        self.zoom_start = spin(0, 86400, 0.1, 0, 2)
        self.zoom_end = spin(0.1, 86400, 0.1, 2, 2)
        for text, widget in [
            ("Zoom in · sec", self.zoom_start),
            ("Zoom out · sec", self.zoom_end),
        ]:
            c = box()
            field(c, text, widget)
            ztimes.append(c)
        self.manual_zoom.append(ztimes)
        self.manual_zoom.append(
            button("Suggest a zoom", self.auto_zoom, "flat", "find-location-symbolic")
        )
        self.click_zoom.connect("toggled", self.update_zoom_controls)
        self.update_zoom_controls()
        self.wallpapers.load({})
        right.append(Gtk.Separator())
        right.append(label("EXPORT", "eyebrow"))
        self.export_format = field(
            right, "Format", dropdown(["MP4 · H.264", "GIF · looping"])
        )
        self.export_size = field(
            right,
            "Output width",
            dropdown(["1920 px", "1280 px", "960 px", "Original"], 0),
        )
        self.export_audio = field(
            right,
            "Audio tracks",
            dropdown(["Mix all tracks", "Desktop only", "Microphone only", "Silent"]),
        )
        self.volume = field(right, "Audio volume", spin(0, 3, 0.1, 1, 1))
        right.append(button("Save edits", self.save_edits, "flat"))
        self.export_button = button(
            "Export video",
            self.export_video,
            "suggested-action",
            "document-save-symbolic",
        )
        right.append(self.export_button)

    def build_health(self):
        page = self.page("health")
        self.heading(
            page,
            "Ready when you are.",
            "Real devices, real capabilities, and useful failure details.",
            button(
                "Run checks",
                self.refresh_health,
                "suggested-action",
                "view-refresh-symbolic",
            ),
        )
        self.health_text = Gtk.TextView(
            editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR
        )
        self.health_text.set_top_margin(16)
        self.health_text.set_bottom_margin(16)
        self.health_text.set_left_margin(16)
        self.health_text.set_right_margin(16)
        page.append(scroll(self.health_text))
        page.append(
            label(
                "Capture logs live beside each original recording. System checks do not start a recording.",
                "small muted",
                True,
            )
        )

    def show_page(self, name):
        if name == "library":
            self.refresh_library()
        if name == "health":
            self.refresh_health()
        self.stack.set_visible_child_name(name)
        for n, w in self.nav.items():
            (w.add_css_class if n == name else w.remove_css_class)("active")
        if name != "editor" and self.video.get_media_stream():
            self.video.get_media_stream().pause()

    def toast(self, text):
        app = self.get_application()
        if app.hud is not None and app.view == "hud":
            app.hud.toast(text)
            return
        self.toast_overlay.add_toast(Adw.Toast.new(str(text)[:240]))

    def error(self, exc):
        self.session_status.set_text("Needs attention")
        dialog = Adw.AlertDialog(heading="Lumen needs a moment", body=str(exc)[-2400:])
        dialog.add_response("ok", "Got it")
        self.get_application().present_current()
        dialog.present(self.get_application().visible_window())

    def worker(self, work, done=None, failed=None):
        def deliver(callback, value):
            try:
                callback(value)
            except Exception as exc:
                self.error(exc)
            return False

        def run():
            try:
                result = work()
            except Exception as exc:
                GLib.idle_add(deliver, failed or self.error, exc)
            else:
                if done:
                    GLib.idle_add(deliver, done, result)

        threading.Thread(target=run, daemon=True).start()

    def refresh_devices(self):
        def work():
            monitors = get_monitors()
            try:
                sources = get_audio_sources()
            except Exception:
                sources = []
            try:
                windows = get_windows()
            except Exception:
                windows = []
            cameras = get_cameras()
            return monitors, sources, windows, cameras

        def done(result):
            self.refreshing_devices = True
            self.monitors, sources, self.windows, cameras = result
            self.monitor.set_model(
                Gtk.StringList.new(
                    [
                        f"{m['name']}  ·  {m['width']} × {m['height']}"
                        for m in self.monitors
                    ]
                    or ["No monitors found"]
                )
            )
            focused = next(
                (
                    i
                    for i, m in enumerate(self.monitors)
                    if m.get("name") == self.settings.get("monitor")
                ),
                next((i for i, m in enumerate(self.monitors) if m.get("focused")), 0),
            )
            self.monitor.set_selected(focused)
            self.microphones = [s for s in sources if not s.get("is_monitor")]
            wanted_mic = self.settings.get("mic_source")
            if wanted_mic and not any(s["name"] == wanted_mic for s in self.microphones):
                self.microphones.append({
                    "name": wanted_mic,
                    "description": "Unavailable · " + self.settings.get("mic_label", wanted_mic),
                    "unavailable": True,
                })
            self.mic.set_model(
                Gtk.StringList.new(
                    ["System default"] + [s["description"] for s in self.microphones]
                )
            )
            self.mic.set_selected(next(
                (i + 1 for i, s in enumerate(self.microphones) if s["name"] == wanted_mic), 0
            ))
            self.window_picker.set_model(
                Gtk.StringList.new(
                    [w["label"][:80] for w in self.windows] or ["No windows found"]
                )
            )
            self.camera_devices = cameras
            if self.preferred_camera_id and not any(c["id"] == self.preferred_camera_id for c in cameras):
                self.camera_devices.append({
                    "id": self.preferred_camera_id, "path": None, "unavailable": True,
                    "label": "Unavailable · " + self.settings.get("camera_label", "Saved camera"),
                })
            self.webcam.set_model(Gtk.StringList.new(["Off"] + [c["label"] for c in self.camera_devices]))
            camera_index = next((i + 1 for i, c in enumerate(self.camera_devices)
                                 if c["id"] == self.preferred_camera_id), 0)
            self.webcam.set_selected(camera_index if self.settings.get("camera_enabled", False) else 0)
            self.refreshing_devices = False
            self.devices_ready = True
            available = (
                bool(self.monitors)
                and not self.capture_busy
                and not self.recorder.is_running
                and not self.replay.is_running
            )
            self.record_button.set_sensitive(available)
            self.replay_start_button.set_sensitive(available)
            self.update_monitor_info()
            if self.record_when_ready:
                replay = self.record_when_ready == "replay"
                self.record_when_ready = False
                self.start_recording(replay=replay)

        self.worker(work, done)

    def selected_microphone(self):
        index = self.mic.get_selected()
        return self.microphones[index - 1] if 0 < index <= len(self.microphones) else None

    def selected_camera(self):
        index = self.webcam.get_selected()
        if 0 < index <= len(self.camera_devices):
            return self.camera_devices[index - 1]
        if self.preferred_camera_id:
            return next((c for c in self.camera_devices if c["id"] == self.preferred_camera_id), None)
        return next((c for c in self.camera_devices if not c.get("unavailable")), None)

    def resolve_camera(self, identity):
        device = next((c for c in get_cameras() if c["id"] == identity), None)
        if not device:
            raise ValueError("The selected camera is no longer available. Refresh devices and select it again.")
        return device["path"]

    def capture_device_changed(self, *_):
        if self.refreshing_devices or not self.devices_ready:
            return
        microphone = self.selected_microphone()
        camera = self.selected_camera()
        if self.webcam.get_selected() > 0 and camera:
            self.preferred_camera_id = camera["id"]
        self.settings.update(
            audio_index=self.audio.get_selected(),
            mic_source=microphone["name"] if microphone else None,
            mic_label=microphone["description"] if microphone else "System default",
            camera_id=self.preferred_camera_id,
            camera_label=camera["label"] if camera else "Camera",
            camera_enabled=self.webcam.get_selected() > 0,
        )
        try:
            save_settings(self.settings)
        except Exception as exc:
            self.toast(f"Device choices remain in memory: {exc}")
        self.device_errors = {"mic": "", "camera": ""}
        self.refresh_input_controls()

    def active_input_backend(self):
        if self.recorder.is_running:
            return self.recorder
        if self.replay.is_running:
            return self.replay
        return None

    def device_controls(self):
        backend = self.active_input_backend()
        microphone, camera = self.selected_microphone(), self.selected_camera()
        mic_available = (
            not microphone.get("unavailable", False) if microphone
            else any(not s.get("unavailable") for s in self.microphones)
        )
        camera_available = bool(camera and not camera.get("unavailable") and camera.get("path"))
        mic_enabled = bool(backend.mic_enabled) if backend else self.audio.get_selected() in (2, 3)
        camera_enabled = bool(backend.camera_enabled) if backend else self.webcam.get_selected() > 0
        live = backend is None or backend.live_inputs_available
        ready = self.devices_ready and not self.shutting_down
        mic_detail = (
            ("Mute microphone for this recording" if mic_enabled else "Enable microphone for this recording")
            if backend else "Microphone will be recorded" if mic_enabled else "Enable microphone for the next recording"
        )
        camera_detail = (
            ("Turn off camera and release the device" if camera_enabled else "Show camera inside the recording area")
            if backend else "Camera starts with recording" if camera_enabled else "Enable camera for the next recording"
        )
        if not mic_available and not mic_enabled:
            mic_detail = "Selected microphone is unavailable. Choose a device in recording settings."
        if not camera_available and not camera_enabled:
            camera_detail = "No selected camera is available. Connect one and refresh devices in Studio."
        if not live:
            mic_detail = camera_detail = "Live input controls require the native recording backend."
        return {
            "mic": {
                "enabled": mic_enabled, "available": bool(ready and live and (mic_available or mic_enabled)),
                "busy": self.capture_busy or self.device_busy["mic"],
                "detail": self.device_errors["mic"] or mic_detail,
            },
            "camera": {
                "enabled": camera_enabled, "available": bool(ready and live and (camera_available or camera_enabled)),
                "busy": self.capture_busy or self.device_busy["camera"],
                "detail": self.device_errors["camera"] or camera_detail,
            },
        }

    def refresh_input_controls(self):
        editable = self.devices_ready and not self.capture_busy and self.active_input_backend() is None
        for widget in (self.audio, self.mic, self.webcam):
            widget.set_sensitive(editable)
        hud = self.get_application().hud
        if hud is not None:
            hud.refresh()

    def toggle_microphone(self):
        self.toggle_input("mic")

    def toggle_camera(self):
        self.toggle_input("camera")

    def toggle_input(self, kind):
        state = self.device_controls()[kind]
        if state["busy"] or not state["available"]:
            return
        enabled = not state["enabled"]
        backend = self.active_input_backend()
        if backend is None:
            if kind == "mic":
                self.audio.set_selected(self.audio.get_selected() ^ 2)
            else:
                camera = self.selected_camera()
                self.webcam.set_selected(self.camera_devices.index(camera) + 1 if enabled else 0)
            self.refresh_input_controls()
            return
        camera = self.selected_camera()
        self.device_busy[kind] = True
        self.capture_busy = True
        self.device_errors[kind] = ""
        self.refresh_input_controls()

        def work():
            with self.capture_lifecycle_lock:
                if self.shutting_down or self.active_input_backend() is not backend:
                    raise RuntimeError("The recording ended before the device could be changed.")
                if kind == "mic":
                    backend.set_microphone_enabled(enabled)
                else:
                    device = self.resolve_camera(camera["id"]) if enabled and camera else None
                    backend.set_webcam_enabled(enabled, device=device)

        def finish():
            self.device_busy[kind] = False
            self.capture_busy = False
            self.refresh_input_controls()

        def done(_):
            if kind == "mic":
                mode = self.audio.get_selected() & 1
                self.audio.set_selected(mode | (2 if enabled else 0))
            elif enabled and camera in self.camera_devices:
                self.webcam.set_selected(self.camera_devices.index(camera) + 1)
            elif not enabled:
                self.webcam.set_selected(0)
            finish()

        def failed(exc):
            self.device_errors[kind] = str(exc)
            finish()
            self.error(exc)

        self.worker(work, done, failed)

    def update_monitor_info(self):
        if not hasattr(self, "monitor_info"):
            return
        index = self.monitor.get_selected()
        if index < len(self.monitors):
            m = self.monitors[index]
            self.monitor_info.set_text(
                f"{m['width']} × {m['height']}  /  {m.get('scale', 1):g}× scale  /  {m.get('refreshRate', 60):.0f} Hz"
            )
        self.monitor_drawing.queue_draw()

    def mode_changed(self, *_):
        if not hasattr(self, "window_picker"):
            return
        mode = self.mode.get_selected()
        self.window_picker.set_visible(mode == 2)
        self.monitor.set_visible(mode == 0)
        self.capture_hint.set_text(
            [
                "Your original stays intact. Edit and export as many versions as you like.",
                "After Start, drag to select an area. Escape cancels the picker.",
                "Records a fixed area. Keep this window visible and in the same position.",
            ][mode]
        )

    def capture_options(self):
        index = self.monitor.get_selected()
        if index >= len(self.monitors):
            raise ValueError("No display is available. Refresh devices first.")
        mode = ["monitor", "region", "window"][self.mode.get_selected()]
        geometry = None
        if mode == "window":
            idx = self.window_picker.get_selected()
            if idx >= len(self.windows):
                raise ValueError("Choose a visible window first.")
            # Resolve the rectangle again so a resized window is not stale.
            address = self.windows[idx].get("address")
            current = next(
                (w for w in get_windows() if w.get("address") == address), None
            )
            if not current:
                raise ValueError("That window is no longer available. Refresh devices.")
            geometry = current["geometry"]
        mic_index = self.mic.get_selected()
        camera_index = self.webcam.get_selected()
        microphone = self.selected_microphone()
        camera = self.selected_camera()
        if self.audio.get_selected() in (2, 3) and microphone and microphone.get("unavailable"):
            raise ValueError("The selected microphone is unavailable. Choose another device in recording settings.")
        if camera_index > 0 and (not camera or camera.get("unavailable")):
            raise ValueError("The selected camera is unavailable. Choose another device in recording settings.")
        options = CaptureOptions(
            monitor=self.monitors[index]["name"],
            mode=mode,
            geometry=geometry,
            fps=[30, 60, 120][self.fps.get_selected()],
            encoder=["auto", "gpu", "cpu"][self.encoder.get_selected()],
            audio=["none", "desktop", "mic", "both"][self.audio.get_selected()],
            mic_source=self.microphones[mic_index - 1]["name"]
            if 0 < mic_index <= len(self.microphones)
            else None,
            cursor=self.cursor.get_active(),
            cursor_telemetry=self.telemetry.get_active(),
            record_clicks=self.record_clicks.get_active(),
            codec=["h264", "hevc", "av1"][self.codec.get_selected()],
            quality=["high", "very_high", "ultra"][self.quality.get_selected()],
            resolution=[None, "1920x1080", "1280x720"][self.resolution.get_selected()],
            webcam=self.camera_devices[camera_index - 1]["path"]
            if 0 < camera_index <= len(self.camera_devices)
            else None,
            live_inputs=True,
        )
        self.capture_camera_choice = (options, camera["id"] if camera_index > 0 and camera else None)
        return options

    def persist_capture_settings(self, options):
        self.settings.update(
            monitor=options.monitor,
            cursor=options.cursor,
            telemetry=options.cursor_telemetry,
            record_clicks=options.record_clicks,
        )
        for key in ("fps", "codec", "encoder", "quality", "audio"):
            self.settings[key + "_index"] = getattr(self, key).get_selected()
        save_settings(self.settings)

    def start_recording(self, replay=False):
        if (
            self.shutting_down
            or self.capture_busy
            or self.recorder.is_running
            or self.replay.is_running
            or not self.devices_ready
        ):
            return
        try:
            options = self.capture_options()
            options.validate()
            self.persist_capture_settings(options)
        except Exception as exc:
            self.error(exc)
            return
        self.capture_busy = True
        self.record_button.set_sensitive(False)
        self.replay_start_button.set_sensitive(False)

        def ready(opts):
            self.get_application().present_current()
            self.begin_countdown(opts, replay=replay)

        if options.mode == "region":
            self.get_application().visible_window().set_visible(False)

            def pick():
                options.geometry = select_region()
                if not options.geometry:
                    raise ValueError("Region selection cancelled.")
                return options

            self.worker(pick, ready, self.capture_failed)
        else:
            ready(options)

    def begin_countdown(self, options, replay=False):
        remaining = [0, 3, 5][self.delay.get_selected()]
        self.cancel_countdown.set_visible(True)

        def step():
            nonlocal remaining
            if remaining <= 0:
                self.countdown_source = None
                self.cancel_countdown.set_visible(False)
                self.session_status.set_text("Starting capture…")
                if self.hide_recording.get_active():
                    self.get_application().visible_window().set_visible(False)
                if replay:
                    seconds = [15, 30, 60][self.replay_duration.get_selected()]
                    self.worker(
                        lambda: self.start_backend(options, seconds),
                        self.replay_started,
                        self.capture_failed,
                    )
                else:
                    self.worker(
                        lambda: self.start_backend(options),
                        self.capture_started,
                        self.capture_failed,
                    )
                return False
            self.session_status.set_text(f"Recording in {remaining}…")
            self.record_button.set_label(str(remaining))
            remaining -= 1
            return True

        if step():
            self.countdown_source = GLib.timeout_add(1000, step)

    def start_backend(self, options, replay_seconds=None):
        with self.capture_lifecycle_lock:
            if self.shutting_down:
                raise RuntimeError("Studio is closing; capture was cancelled.")
            choice = self.capture_camera_choice
            if choice and choice[0] is options and choice[1] and options.webcam:
                options.webcam = self.resolve_camera(choice[1])
            return (
                self.replay.start(options, replay_seconds)
                if replay_seconds
                else self.recorder.start(options)
            )

    def shutdown_capture(self):
        self.shutting_down = True
        with self.capture_lifecycle_lock:
            # A start already in flight must finish before its process is stopped.
            for backend in (self.recorder, self.replay):
                if backend.is_running:
                    try:
                        backend.stop()
                    except Exception:
                        # Capture engines retain raw media and diagnostics on failure.
                        pass

    def cancel_start(self):
        if self.countdown_source:
            GLib.source_remove(self.countdown_source)
            self.countdown_source = None
            self.capture_busy = False
            self.cancel_countdown.set_visible(False)
            self.record_button.set_sensitive(True)
            self.replay_start_button.set_sensitive(True)
            self.record_button.set_label("Start recording")
            self.session_status.set_text("Ready")

    def capture_started(self, project):
        self.capture_busy = False
        self.record_button.set_label("Recording…")
        self.stop_button.set_visible(True)
        self.pause_button.set_visible(True)
        self.pause_button.set_sensitive(self.recorder.backend != "wf-recorder")
        self.session_status.set_text("Recording")
        if project.get("capture_warning"):
            self.toast(project["capture_warning"])

    def capture_failed(self, exc):
        self.capture_busy = False
        self.cancel_countdown.set_visible(False)
        self.record_button.set_label("Start recording")
        self.record_button.set_sensitive(True)
        self.replay_start_button.set_sensitive(True)
        self.get_application().present_current()
        self.error(exc)

    def stop_recording(self):
        if self.countdown_source:
            self.cancel_start()
            return
        if self.replay.is_running and not self.capture_busy:
            self.capture_busy = True
            self.stop_button.set_sensitive(False)
            self.save_replay_button.set_sensitive(False)

            def done(_):
                self.capture_busy = False
                self.reset_capture_controls()
                self.toast("Replay buffer stopped. Saved clips are in your library.")
                if self.closing:
                    self.get_application().quit()

            def failed(exc):
                self.capture_busy = False
                self.closing = False
                self.discard_edits_on_quit = False
                self.reset_capture_controls()
                self.error(exc)

            self.worker(self.replay.stop, done, failed)
            return
        if self.capture_busy or not self.recorder.is_running:
            return
        self.capture_busy = True
        self.stop_button.set_sensitive(False)
        self.pause_button.set_sensitive(False)
        self.session_status.set_text("Saving original…")

        def done(project):
            self.capture_busy = False
            self.reset_capture_controls()
            if self.closing:
                self.get_application().quit()
                return
            self.get_application().show_studio()
            self.open_project(project)
            self.toast("Take saved. Your original is ready.")
            if project.get("capture_warning"):
                self.error(project["capture_warning"])

        def failed(exc):
            self.capture_busy = False
            self.closing = False
            self.discard_edits_on_quit = False
            self.reset_capture_controls()
            self.get_application().present_current()
            self.error(exc)

        self.worker(self.recorder.stop, done, failed)

    def reset_capture_controls(self):
        self.stop_button.set_visible(False)
        self.stop_button.set_sensitive(True)
        self.stop_button.set_label("Stop & save")
        self.pause_button.set_visible(False)
        self.pause_button.set_sensitive(True)
        self.pause_button.set_label("Pause")
        self.record_button.set_sensitive(True)
        self.replay_start_button.set_sensitive(True)
        self.save_replay_button.set_visible(False)
        self.save_replay_button.set_sensitive(True)
        self.record_button.set_label("Start recording")
        self.session_status.set_text("Ready")
        self.timer.set_text("")

    def replay_started(self, _):
        self.capture_busy = False
        self.session_status.set_text(f"Replay buffering · last {self.replay.seconds}s")
        self.record_button.set_label("Replay active")
        self.save_replay_button.set_visible(True)
        self.stop_button.set_visible(True)
        self.stop_button.set_label("Stop buffer")
        self.pause_button.set_visible(False)
        self.toast(
            f"Replay is active. Save replay keeps the last {self.replay.seconds} seconds."
        )

    def save_replay(self, show=True):
        if not self.replay.is_running:
            self.toast("Start a replay buffer from Record first.")
            return
        if self.capture_busy:
            return
        self.capture_busy = True
        self.save_replay_button.set_sensitive(False)

        def finish():
            self.capture_busy = False
            self.save_replay_button.set_sensitive(True)

        def done(project):
            finish()
            if show:
                self.open_project(project)
                self.get_application().show_studio()
            else:
                notification = Gio.Notification.new("Replay saved")
                notification.set_body(
                    f"{project['duration']:.1f} seconds saved in your Lumen library. Buffering continues."
                )
                self.get_application().send_notification("replay-saved", notification)
                if self.stack.get_visible_child_name() == "library":
                    self.refresh_library()
            self.toast("Replay saved. The buffer is still running.")
            if project.get("capture_warning"):
                self.error(project["capture_warning"])

        def failed(exc):
            finish()
            self.error(exc)

        self.worker(self.replay.save, done, failed)

    def pause_recording(self):
        if self.capture_busy or not self.recorder.is_running:
            return
        self.capture_busy = True
        paused = self.recorder.status == "paused"

        def done(_):
            self.capture_busy = False
            self.pause_button.set_label("Pause" if paused else "Resume")
            self.session_status.set_text("Recording" if paused else "Paused")

        def failed(exc):
            self.capture_busy = False
            self.error(exc)

        self.worker(
            self.recorder.resume if paused else self.recorder.pause, done, failed
        )

    def tick(self):
        if self.project:
            self.layer_timeline.queue_draw()
        if self.recorder.is_running:
            self.timer.set_text(clock_text(self.recorder.elapsed))
        elif self.replay.is_running:
            self.timer.set_text(clock_text(self.replay.elapsed))
        elif not self.capture_busy and self.replay.status == "buffering":
            self.capture_busy = True

            def stopped(_):
                self.capture_busy = False
                self.reset_capture_controls()
                self.error(
                    "The replay buffer exited unexpectedly. Saved clips are safe. Check System and try starting it again."
                )

            self.worker(self.replay.stop, stopped, stopped)
        elif not self.capture_busy and self.recorder.status in ("recording", "paused"):
            # Finalize unexpected exits so corrupt/incomplete captures are visible.
            self.capture_busy = True

            def failed(exc):
                self.capture_busy = False
                self.reset_capture_controls()
                self.error(exc)

            def done(project):
                self.capture_busy = False
                self.reset_capture_controls()
                self.get_application().show_studio()
                self.open_project(project)
                self.toast(
                    "Capture ended unexpectedly. Check the take before exporting."
                )

            self.worker(self.recorder.stop, done, failed)
        self.refresh_input_controls()
        return True

    def refresh_library(self):
        child = self.library_box.get_first_child()
        while child:
            following = child.get_next_sibling()
            self.library_box.remove(child)
            child = following
        projects = list_projects()
        if not projects:
            empty = box(True, 14, "card")
            empty.append(label("Your first take belongs here.", "section-title"))
            empty.append(
                label(
                    "Record a display, choose an area, or import an existing video.",
                    "muted",
                    True,
                )
            )
            empty.append(
                button(
                    "Make a recording",
                    lambda: self.show_page("capture"),
                    "suggested-action",
                )
            )
            self.library_box.append(empty)
        for project in projects:
            row = box(False, 16)
            picpath = Path(project["path"]) / "thumbnail.jpg"
            if picpath.exists():
                pic = Gtk.Picture.new_for_filename(str(picpath))
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_size_request(140, 80)
                row.append(pic)
            else:
                icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
                icon.set_pixel_size(42)
                icon.set_size_request(100, 80)
                row.append(icon)
            info = box(True, 7)
            info.set_hexpand(True)
            title = label(project.get("name", "Untitled take"), "section-title")
            title.set_ellipsize(Pango.EllipsizeMode.END)
            info.append(title)
            info.append(
                label(
                    f"{clock_text(project.get('duration', 0))}   ·   {project.get('width', 0)} × {project.get('height', 0)}   ·   {project.get('status', 'saved')}",
                    "small muted",
                )
            )
            info.append(
                label(
                    project.get("created_at", "")[:16].replace("T", "  "), "small muted"
                )
            )
            row.append(info)
            row.append(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            btn = Gtk.Button(child=row)
            btn.add_css_class("library-row")
            btn.connect("clicked", lambda _, p=project: self.open_project(p))
            self.library_box.append(btn)

    def open_project(self, project):
        if self.layers.transcribing:
            self.toast("Finish or cancel transcription before opening another project.")
            return
        if self.export_busy:
            self.toast("Finish or cancel the export before opening another project.")
            return
        if self.save_edits(False) is False:
            return
        project = deepcopy(project)
        self.open_generation += 1
        generation = self.open_generation
        self.opening_project = project["path"]
        source = Path(project["path"]) / project.get("source", "source.mkv")

        def work():
            meta = probe(source)
            clean_layers(project)
            validate_project_overlays(project, meta["duration"])
            thumb = Path(project["path"]) / "thumbnail.jpg"
            if not thumb.exists():
                thumbnail(source, thumb, time=min(0.5, meta["duration"] / 2))
            return meta

        def done(meta):
            if generation == self.open_generation:
                self.opening_project = None
            if (
                generation != self.open_generation
                or self.export_busy
                or self.layers.transcribing
            ):
                return
            self.project = project
            project.update({k: meta[k] for k in ("duration", "width", "height", "fps")})
            save_project(project)
            self.project_name.set_text(project.get("name", "Untitled take"))
            self.editor_title.set_text("The finishing touches.")
            self.editor_empty.set_visible(False)
            self.editor_content.set_visible(True)
            self.video.set_filename(str(source))
            self.preview_options = None
            self.preview_note.set_text(
                "Original preview · Preview edits renders your trim, speed, zoom, frame, audio and timed layers."
            )
            self.export_progress.set_visible(False)
            stream = self.video.get_media_stream()
            self.playback_error.set_text("")
            if stream:
                stream.connect("notify::error", self.media_error)
            self.editor_meta.set_text(
                f"{meta['width']} × {meta['height']}  ·  {meta['fps']:.0f} fps  ·  {meta['duration']:.2f} seconds  ·  Original preserved"
            )
            self.load_edits(project.get("edits", {}), meta["duration"])
            self.layers.load(project)
            self.last_recovery = None
            self.restored_recovery = None
            self.update_recovery_banner()
            self.aspect.set_ratio(meta["width"] / meta["height"])
            self.update_frame()
            modes = audio_modes(project, meta)
            self.export_audio.set_model(
                Gtk.StringList.new(
                    [
                        "Mix all tracks",
                        "Desktop only"
                        if "desktop" in modes
                        else "Desktop · unavailable",
                        "Microphone only" if "mic" in modes else "Mic · unavailable",
                        "Silent",
                    ]
                )
            )
            self.export_audio.set_selected(
                ["mix", "desktop", "mic", "none"].index(
                    project.get("edits", {}).get("audio_mode", "mix")
                )
                if isinstance(project.get("edits"), dict)
                and project["edits"].get("audio_mode", "mix")
                in ("mix", "desktop", "mic", "none")
                else 0
            )
            self.show_page("editor")

        def failed(exc):
            if generation == self.open_generation:
                self.opening_project = None
            self.error(exc)

        self.worker(work, done, failed)

    def media_error(self, stream, *_):
        if stream.get_error():
            self.playback_error.set_text(
                "Preview unavailable for this codec. Use Play source externally, or export MP4."
            )

    def load_edits(self, edits, duration):
        if not isinstance(edits, dict):
            edits = {}
        edits = edits.copy()
        defaults = asdict(ExportOptions())
        for key in (
            "trim_start",
            "trim_end",
            "padding",
            "zoom",
            "zoom_x",
            "zoom_y",
            "volume",
            "zoom_start",
            "zoom_end",
            "click_zoom_amount",
            "click_zoom_hold",
            "click_zoom_transition",
        ):
            val = edits.get(key, defaults[key])
            if (val is None and defaults[key] is not None) or (
                val is not None
                and (
                    isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or not math.isfinite(val)
                )
            ):
                edits[key] = defaults[key]
        for widget in (self.trim_start, self.trim_end, self.zoom_start, self.zoom_end):
            widget.set_range(0, max(0.1, duration))
        self.trim_start.set_value(edits.get("trim_start", 0))
        self.trim_end.set_value(edits.get("trim_end") or duration)
        self.padding.set_value(edits.get("padding", 64))
        self.zoom.set_value(edits.get("zoom", 1))
        self.zoom_x.set_value(edits.get("zoom_x", 0.5))
        self.zoom_y.set_value(edits.get("zoom_y", 0.5))
        self.volume.set_value(edits.get("volume", 1))
        self.zoom_start.set_value(edits.get("zoom_start") or 0)
        self.zoom_end.set_value(edits.get("zoom_end") or min(duration, 3))
        self.timed_zoom.set_active(edits.get("zoom_start") is not None)
        self.click_zoom.set_active(edits.get("click_zoom") is True)
        for name in ("click_zoom_amount", "click_zoom_hold", "click_zoom_transition"):
            getattr(self, name).set_value(edits.get(name, defaults[name]))
        self.manual_zoom_expander.set_expanded(
            self.zoom.get_value() > 1 and not self.click_zoom.get_active()
        )
        self.update_zoom_controls()
        self.wallpapers.load(edits)
        for widget, values, value in [
            (self.speed, [0.5, 0.75, 1, 1.5, 2, 3], edits.get("speed", 1)),
            (
                self.background,
                BACKGROUND_STYLES,
                edits.get("background", "midnight"),
            ),
            (
                self.export_size,
                [1920, 1280, 960, None],
                edits.get("output_width", 1920),
            ),
            (
                self.export_audio,
                ["mix", "desktop", "mic", "none"],
                edits.get("audio_mode", "mix"),
            ),
            (self.export_format, ["mp4", "gif"], edits.get("format", "mp4")),
        ]:
            widget.set_selected(values.index(value) if value in values else 0)
        self.update_frame()

    def edit_options(self):
        return ExportOptions(
            trim_start=self.trim_start.get_value(),
            trim_end=self.trim_end.get_value(),
            speed=[0.5, 0.75, 1, 1.5, 2, 3][self.speed.get_selected()],
            background=BACKGROUND_STYLES[
                self.background.get_selected()
            ],
            wallpaper=self.wallpapers.selected,
            padding=self.padding.get_value_as_int(),
            output_width=[1920, 1280, 960, None][self.export_size.get_selected()],
            zoom=self.zoom.get_value(),
            zoom_x=self.zoom_x.get_value(),
            zoom_y=self.zoom_y.get_value(),
            zoom_start=self.zoom_start.get_value()
            if self.timed_zoom.get_active()
            else None,
            zoom_end=self.zoom_end.get_value()
            if self.timed_zoom.get_active()
            else None,
            click_zoom=self.click_zoom.get_active(),
            click_zoom_amount=self.click_zoom_amount.get_value(),
            click_zoom_hold=self.click_zoom_hold.get_value(),
            click_zoom_transition=self.click_zoom_transition.get_value(),
            audio_mode=["mix", "desktop", "mic", "none"][
                self.export_audio.get_selected()
            ],
            volume=self.volume.get_value(),
            format=["mp4", "gif"][self.export_format.get_selected()],
            captions=self.layers.flags["captions"].get_active(),
            annotations=self.layers.flags["annotations"].get_active(),
            clicks=self.layers.flags["clicks"].get_active(),
        )

    def _save_edits(self):
        if not self.project:
            return
        self.layers.commit()
        candidate = deepcopy(self.project)
        candidate["edits"] = asdict(self.edit_options())
        candidate["name"] = self.project_name.get_text().strip() or "Untitled take"
        resolved = self.restored_recovery or (self.last_recovery[1] if self.last_recovery else None)
        if resolved:
            candidate["draft_recovery_handled_through"] = resolved.name
        save_project(candidate)
        self.project.update(candidate)
        self.last_recovery = None
        if resolved:
            self.restored_recovery = None
            self.update_recovery_banner()

    def save_edits(self, notify=True):
        if self.project:
            try:
                self._save_edits()
            except Exception as exc:
                self.error(exc)
                return False
            if notify:
                self.toast("Edits saved. Original unchanged.")
        return True

    def preserve_edits(self):
        """Save valid edits or durably retain the exact unfinished form."""
        if not self.project:
            return None
        draft = {
            "layer_form": self.layers.snapshot_draft(),
            "name": self.project_name.get_text(),
            "edits": asdict(self.edit_options()),
        }
        try:
            self._save_edits()
            return None
        except Exception as exc:
            draft["error"] = str(exc)
        # Keep the committed layers separate from the raw, possibly invalid form.
        # Recovery must succeed before we treat the draft as safe to close.
        snapshot = deepcopy(self.project)
        signature = json.dumps([snapshot, draft], sort_keys=True, allow_nan=False)
        if self.last_recovery and self.last_recovery[0] == signature:
            try:
                cached = read_recovery(self.last_recovery[1], self.project)
                if cached["draft"] == draft and cached["project"] == self.last_recovery[2]:
                    return self.last_recovery[1]
            except (OSError, ValueError):
                pass
        path = write_recovery(snapshot, draft)
        candidate = deepcopy(snapshot)
        candidate.update(name=draft["name"].strip() or "Untitled take", edits=draft["edits"])
        try:
            validate_project_overlays(candidate, candidate["duration"])
            save_project(candidate)
            self.project.update(candidate)
        except Exception:
            # The durable recovery already contains these values, including when
            # the manifest itself cannot be written (e.g. disk exhaustion).
            pass
        signature = json.dumps([self.project, draft], sort_keys=True, allow_nan=False)
        self.last_recovery = (signature, path, snapshot)
        self.update_recovery_banner()
        return path

    def update_recovery_banner(self):
        try:
            handled = self.project.get("draft_recovery_handled_through", "")
            paths = [p for p in list_recoveries(self.project) if p.name > handled]
            self.recovery_path = paths[0] if paths else None
        except (OSError, ValueError):
            self.recovery_path = None
        self.recovery_banner.set_revealed(self.recovery_path is not None)

    def restore_recovery(self):
        if not self.project or not self.recovery_path:
            return
        path = self.recovery_path
        previous = None
        try:
            recovered = read_recovery(path, self.project)
            snapshot = recovered["project"]
            validate_project_overlays(snapshot, self.project["duration"])
            draft = recovered["draft"]
            if (
                not isinstance(draft.get("name"), str)
                or not isinstance(draft.get("edits"), dict)
                or not draft_matches_project(snapshot, draft.get("layer_form"))
            ):
                raise ValueError("The unfinished edit is damaged and could not be restored.")
            self.preserve_edits()
            previous = (
                deepcopy(self.project), self.project_name.get_text(),
                asdict(self.edit_options()), self.layers.snapshot_draft(),
            )
            # Source identity and media properties always come from the open take.
            for key in ("captions", "annotations", "clicks", "caption_style"):
                if key in snapshot:
                    self.project[key] = deepcopy(snapshot[key])
            self.project_name.set_text(draft.get("name", self.project.get("name", "")))
            self.load_edits(draft.get("edits", {}), self.project["duration"])
            self.layers.load(self.project)
            if not self.layers.restore_draft(draft["layer_form"]):
                raise ValueError("The unfinished layer could not be restored.")
            self.restored_recovery = path
            self.recovery_banner.set_revealed(False)
            self.toast("Draft restored. Finish the edit, then save.")
        except Exception as exc:
            if previous:
                project, name, edits, form = previous
                self.project.clear()
                self.project.update(project)
                self.project_name.set_text(name)
                self.load_edits(edits, project["duration"])
                self.layers.load(self.project)
                self.layers.restore_draft(form)
            self.error(exc)

    def mark_trim(self, start):
        stream = self.video.get_media_stream()
        if stream:
            seconds = stream.get_timestamp() / 1_000_000
            if self.preview_options:
                seconds = (
                    seconds * self.preview_options.speed
                    + self.preview_options.trim_start
                )
            (self.trim_start if start else self.trim_end).set_value(seconds)

    def update_frame(self, *_):
        for style in ("preview-violet", "preview-sand", "preview-none"):
            self.frame.remove_css_class(style)
        selected = self.background.get_selected()
        if self.preview_options:
            self.frame.add_css_class("preview-none")
        elif selected in (1, 2, 3):
            self.frame.add_css_class(
                ["", "preview-violet", "preview-sand", "preview-none"][selected]
            )
        self.wallpapers.update_frame()

    def show_original(self, source_time=None):
        if not self.project:
            return
        if source_time is None:
            source_time = self.layers.playhead()
        self.preview_options = None
        self.video.set_filename(
            str(Path(self.project["path"]) / self.project["source"])
        )
        stream = self.video.get_media_stream()
        if stream:
            target = max(0, min(source_time, self.project["duration"]))

            def ready(media, *_):
                if media.is_prepared():
                    media.seek(int(target * 1_000_000))
                    media.pause()

            if stream.is_prepared():
                ready(stream)
            else:
                stream.connect("notify::prepared", ready)
        self.preview_note.set_text(
            "Original preview · Preview edits renders your trim, speed, zoom, frame, audio and timed layers."
        )
        self.aspect.set_ratio(self.project["width"] / self.project["height"])
        self.update_frame()
        self.layer_guides.queue_draw()

    def render_preview(self):
        if not self.project or self.export_busy:
            return
        if self.save_edits(False) is False:
            return
        self.layers.cancel_pick()
        options = self.edit_options()
        requested_width = options.output_width or self.project["width"]
        width = min(960, requested_width)
        draft = replace(
            options,
            output_width=width,
            padding=round(options.padding * width / requested_width),
            format="mp4",
            fps=30,
            quality=27,
        )
        destination = (
            Path(self.preview_directory.name) / f"draft-{GLib.get_monotonic_time()}.mp4"
        )
        self.render_export(draft, destination, preview=True)

    def update_zoom_controls(self, *_):
        enabled = self.click_zoom.get_active()
        self.click_zoom_controls.set_sensitive(enabled)
        self.manual_zoom.set_sensitive(not enabled)

    def zoom_to_clicks(self):
        if not self.project or not self.layers.guard(self.layers.commit):
            return
        if not any(c.get("enabled", True) for c in self.project.get("clicks", [])):
            self.toast(
                "Add or enable a click first, or make a new recording with click capture enabled."
            )
            return
        self.click_zoom.set_active(True)
        self.inspector.set_visible_child_name("style")
        if self.save_edits(False) is not False:
            self.toast("Zoom to clicks enabled. Preview edits to see it move.")

    def auto_zoom(self):
        if not self.project:
            return
        try:
            suggestion = suggest_zoom(self.project)
            if not suggestion:
                self.toast(
                    "No clear cursor pause found. Set a zoom manually, or record with cursor-path capture enabled."
                )
                return
            for name in ("zoom", "zoom_x", "zoom_y", "zoom_start", "zoom_end"):
                if suggestion.get(name) is not None:
                    getattr(self, name).set_value(suggestion[name])
            self.timed_zoom.set_active(True)
            self.click_zoom.set_active(False)
            self.manual_zoom_expander.set_expanded(True)
            self.toast(
                "Zoom suggested around a cursor pause. Adjust the focus and timing before export."
            )
        except Exception as exc:
            self.error(exc)

    def export_video(self):
        if not self.project or self.export_busy:
            if not self.project:
                self.toast("Open a recording first.")
            return
        if self.save_edits(False) is False:
            return
        options = self.edit_options()
        project = deepcopy(self.project)
        chooser = Gtk.FileDialog(
            title="Export your video",
            initial_name=f"Lumen-{datetime.now():%Y%m%d-%H%M%S}.{options.format}",
        )
        chooser.set_initial_folder(Gio.File.new_for_path(self.project["path"]))

        def chosen(dialog, result):
            try:
                destination = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            if not destination:
                self.error(ValueError("Choose a local file destination."))
                return
            self.render_export(options, destination, project)

        chooser.save(self, None, chosen)

    def render_export(self, options, destination, project=None, preview=False, completed=None):
        if self.export_busy:
            self.toast("An export is already running.")
            return
        self.export_busy = True
        self.export_button.set_sensitive(False)
        self.preview_button.set_sensitive(False)
        self.export_progress.set_visible(True)
        self.export_progress.set_fraction(0)
        self.export_progress.set_text("Preparing export…")
        self.export_cancel.set_visible(True)
        self.exporter = Exporter()
        exporter = self.exporter
        project = deepcopy(project or self.project)

        def progress(fraction):
            GLib.idle_add(self.set_progress, fraction)

        def publish(path):
            self.finish_export()
            self.export_progress.set_fraction(1)
            if preview:
                if not self.project or self.project["path"] != project["path"]:
                    return
                self.layers.cancel_pick()
                self.preview_path = path
                self.preview_options = options
                self.video.set_filename(str(path))
                media = self.video.get_media_stream()
                if (
                    media
                    and self.get_application().view == "studio"
                    and self.get_visible()
                ):
                    media.play()
                self.update_frame()
                self.layer_guides.queue_draw()
                meta = probe(path)
                self.aspect.set_ratio(meta["width"] / meta["height"])
                self.preview_note.set_text(
                    "Rendered draft · 960px maximum / 30 fps. Preview edits again after changes. Export uses your full output settings."
                )
                self.export_progress.set_text("Preview ready")
                return
            self.export_progress.set_text("Export complete · " + Path(path).name)
            latest = load_project(project["path"])
            latest.setdefault("exports", []).append(str(path))
            save_project(latest)
            if self.project and self.project["path"] == project["path"]:
                self.project["exports"] = latest["exports"]
            toast = Adw.Toast.new("Export ready")
            toast.set_button_label("Open")
            toast.connect("button-clicked", lambda *_: self.open_path(path))
            self.toast_overlay.add_toast(toast)

        def done(path):
            try:
                publish(path)
            except Exception as exc:
                if completed:
                    completed(None, exc)
                raise
            if completed:
                completed(path)

        def failed(exc):
            self.finish_export()
            self.export_progress.set_text(
                "Export cancelled"
                if isinstance(exc, ExportCancelled)
                else "Export failed"
            )
            if not isinstance(exc, ExportCancelled):
                self.error(exc)
            if completed:
                completed(None, exc)

        self.worker(
            lambda: exporter.export(
                project, options, destination, on_progress=progress
            ),
            done,
            failed,
        )

    def set_progress(self, fraction):
        self.export_progress.set_fraction(fraction)
        self.export_progress.set_text(f"Exporting · {fraction:.0%}")
        return False

    def finish_export(self):
        self.export_busy = False
        self.export_button.set_sensitive(True)
        self.preview_button.set_sensitive(True)
        self.export_cancel.set_visible(False)
        self.exporter = None

    def cancel_export(self):
        if self.exporter:
            self.exporter.cancel()

    def import_video(self):
        if self.export_busy or self.layers.transcribing:
            self.toast("Finish or cancel the current export or transcription first.")
            return
        chooser = Gtk.FileDialog(title="Import a video")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filt = Gtk.FileFilter(name="Videos")
        for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov", "*.avi"):
            filt.add_pattern(pattern)
        filters.append(filt)
        chooser.set_filters(filters)

        def chosen(dialog, result):
            try:
                source = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            if not source:
                return

            def work():
                meta = probe(source)
                project = create_project()
                project.update(
                    name=Path(source).stem,
                    source="source" + Path(source).suffix.lower(),
                    status="ready",
                )
                project.update(
                    {k: meta[k] for k in ("duration", "width", "height", "fps")}
                )
                shutil.copy2(source, Path(project["path"]) / project["source"])
                save_project(project)
                return project

            self.worker(work, self.open_project)

        chooser.open(self, None, chosen)

    def open_path(self, path):
        path = Path(path)
        if path == library_root():
            path.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(Path(path).absolute().as_uri(), None)

    def play_external(self):
        if self.project:
            self.open_path(Path(self.project["path"]) / self.project["source"])

    def refresh_health(self):
        self.health_text.get_buffer().set_text("Checking the native capture stack…")
        self.worker(
            diagnostics,
            lambda report: self.health_text.get_buffer().set_text(
                json.dumps(report, indent=2)
            ),
        )

    def on_close(self, *_):
        # Keep one controller alive for both views, including ongoing capture.
        self.get_application().show_hud()
        return True

    def confirm_quit(self, parent=None, discard_edits=False):
        if not discard_edits:
            try:
                self.preserve_edits()
            except Exception as exc:
                dialog = Adw.AlertDialog(
                    heading="Edits could not be saved",
                    body=f"Your existing recording is safe. Lumen could not save the latest edits or a recovery copy.\n\n{exc}",
                )
                dialog.add_response("stay", "Keep working")
                dialog.add_response("discard", "Quit without saving edits")
                dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
                dialog.set_default_response("stay")
                dialog.set_close_response("stay")
                dialog.connect("response", lambda _, answer: (
                    self.get_application().request_quit(discard_edits=True)
                    if answer == "discard" else None
                ))
                dialog.present(parent or self)
                return True
        if (
            self.recorder.is_running
            or self.replay.is_running
            or self.capture_busy
            or self.export_busy
            or self.layers.transcribing
        ):
            dialog = Adw.AlertDialog(
                heading="Work is still running",
                body="Stop and save your recording, or finish the export or transcription before closing Lumen.",
            )
            dialog.add_response("stay", "Keep working")
            if self.countdown_source:
                dialog.add_response("cancel-countdown", "Cancel countdown & quit")
            if (
                (self.recorder.is_running or self.replay.is_running)
                and not self.capture_busy
                and not self.export_busy
                and not self.layers.transcribing
            ):
                dialog.add_response("save", "Stop, save & close")
                if self.replay.is_running:
                    dialog.set_response_label("save", "Stop buffer & close")
                    dialog.set_body(
                        "The replay buffer is active. Save a replay first if you want to keep recent frames. Stopping discards the unsaved buffer."
                    )
                dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

            def response(_, answer):
                if answer == "save":
                    # Work may have finished or changed while the dialog was open.
                    if (
                        self.capture_busy
                        or self.export_busy
                        or self.layers.transcribing
                    ):
                        self.toast("Finish the current operation before quitting.")
                    elif self.recorder.is_running or self.replay.is_running:
                        if not discard_edits:
                            try:
                                self.preserve_edits()
                            except Exception:
                                self.get_application().request_quit()
                                return
                        self.discard_edits_on_quit = discard_edits
                        self.closing = True
                        self.stop_recording()
                    else:
                        if discard_edits:
                            self.get_application().request_quit(discard_edits=True)
                        else:
                            self.get_application().request_quit()
                elif answer == "cancel-countdown":
                    self.cancel_start()
                    if discard_edits:
                        self.get_application().request_quit(discard_edits=True)
                    else:
                        self.get_application().request_quit()

            dialog.connect("response", response)
            dialog.present(parent or self)
            return True
        self.discard_edits_on_quit = discard_edits
        return False
