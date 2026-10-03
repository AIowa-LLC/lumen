"""Timed caption, annotation, and click authoring for the native studio."""

# ruff: noqa: E402
from __future__ import annotations

from copy import deepcopy
import json
import locale
import math
from pathlib import Path
import uuid

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gio, GLib, Gtk, Pango

from .project import save_project


KINDS = ("captions", "annotations", "clicks")
COLORS = ("#ffffff", "#78c8ff", "#bba5ff", "#ffd166", "#ff7285", "#8ae2b1")
DRAFT_NUMBERS = ("start", "end", "click_duration", "x", "y", "x2", "y2", "size")


def spin_value(control, label):
    """Read pending entry text without forcing GTK to discard an invalid edit.

    An untouched rounded display keeps the original higher-precision value;
    typed text is otherwise authoritative even before the spin loses focus.
    """
    value = control.get_value()
    raw = control.get_text()
    if isinstance(raw, str):
        try:
            entered = locale.atof(raw.strip())
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label} must be a number.") from exc
        digits = control.get_digits()
        if not math.isfinite(value) or entered != round(value, digits):
            value = entered
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite.")
    return value


def click_duration(value):
    """Match the editable effect duration to its documented numeric bounds."""
    if not math.isfinite(value):
        raise ValueError(
            "Click duration must be finite and between 0.01 and 10 seconds."
        )
    # GTK increments can leave a tiny floating-point residue at either boundary.
    value = round(value, 8)
    if not 0.01 <= value <= 10:
        raise ValueError("Click duration must be between 0.01 and 10 seconds.")
    return value


def draft_matches_project(project, draft):
    """Preflight recovery structure/identity without mutating project or widgets.

    Pending form values may be invalid edits; only their types, sizes, widget
    selections and association with a saved layer are checked here.
    """
    if (
        not isinstance(project, dict)
        or not isinstance(draft, dict)
        or type(draft.get("version")) is not int
        or draft["version"] != 1
        or draft.get("project_path") != str(project.get("path", ""))
        or draft.get("kind") not in KINDS
    ):
        return False
    kind, selected_id = draft["kind"], draft.get("selected_id")
    items = project.get(kind, [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return False
    if selected_id is not None and (
        not isinstance(selected_id, str)
        or not any(item.get("id") == selected_id for item in items)
    ):
        return False
    fields, style, visibility = (
        draft.get("fields"),
        draft.get("caption_style"),
        draft.get("visibility"),
    )
    if not all(isinstance(value, dict) for value in (fields, style, visibility)):
        return False
    if "numeric_values" in draft:
        values = draft["numeric_values"]
        if not isinstance(values, dict) or any(
            type(values.get(key)) not in (int, float) or not math.isfinite(values[key])
            for key in (*DRAFT_NUMBERS, "caption_size")
        ):
            return False

    def text(mapping, key, limit=200):
        return isinstance(mapping.get(key), str) and len(mapping[key]) <= limit

    return (
        all(text(fields, key) for key in DRAFT_NUMBERS)
        and text(fields, "text", 100_000)
        and text(fields, "color")
        and isinstance(fields.get("enabled"), bool)
        and type(fields.get("annotation_kind")) is int
        and fields["annotation_kind"] in range(4)
        and type(fields.get("button_kind")) is int
        and fields["button_kind"] in range(3)
        and text(style, "size")
        and text(style, "color")
        and type(style.get("position")) is int
        and style["position"] in range(2)
        and isinstance(style.get("background"), bool)
        and all(isinstance(visibility.get(key), bool) for key in KINDS)
    )


def video_bounds(width, height, source_width, source_height):
    if min(width, height, source_width, source_height) <= 0:
        return 0, 0, 0, 0
    scale = min(width / source_width, height / source_height)
    w, h = source_width * scale, source_height * scale
    return (width - w) / 2, (height - h) / 2, w, h


def video_point(x, y, width, height, source_width, source_height):
    ox, oy, w, h = video_bounds(width, height, source_width, source_height)
    if not w or not h or not (ox <= x <= ox + w and oy <= y <= oy + h):
        return None
    return (x - ox) / w, (y - oy) / h


def clean_layers(project):
    """Keep malformed manifests visible; validation reports unsafe edit fields."""
    for kind in KINDS:
        items = project.get(kind)
        if items is None:
            project[kind] = []
        elif not isinstance(items, list) or not all(
            isinstance(item, dict) for item in items
        ):
            raise ValueError(f"The {kind} list in this project is malformed.")
        for item in project[kind]:
            if not isinstance(item.get("id"), str) or not item["id"]:
                item["id"] = uuid.uuid4().hex


def caption_candidate(project, captions):
    """Validate an append before touching the open project's metadata."""
    from .overlays import validate_project_overlays

    candidate = deepcopy(project)
    captions = deepcopy(captions)
    for cue in captions:
        cue["id"] = uuid.uuid4().hex
    candidate.setdefault("captions", []).extend(captions)
    validate_project_overlays(candidate, project.get("duration"))
    return candidate["captions"]


class LayerPanel(Gtk.Box):
    def __init__(self, studio):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        # Imported lazily to keep the GTK application and authoring module acyclic.
        from .ui import box, button, dropdown, field, label, scroll, spin

        self.studio = studio
        self.loading = False
        self.current_kind = "captions"
        self.selected_id = None
        self.pick_target = None
        self.transcriber = None
        self.transcribing = False
        self.transcribe_generation = 0
        self.undo_stack = []
        self.redo_stack = []
        self.append(label("TIMED LAYERS", "eyebrow"))
        self.kind = dropdown(["Captions", "Annotations", "Cursor clicks"])
        self.kind.connect("notify::selected", self.change_kind)
        self.append(self.kind)
        self.flags = {}
        for kind, text in zip(
            KINDS, ("Render captions", "Render annotations", "Render click effects")
        ):
            self.flags[kind] = Gtk.CheckButton(label=text, active=True)
            self.append(self.flags[kind])
        self.summary = label(
            "Open a recording to add timed layers.", "small muted", True
        )
        self.append(self.summary)
        self.auto_caption_expander = Gtk.Expander(label="Automatic captions · offline")
        self.auto_caption_box = box(True, 12)
        self.auto_caption_expander.set_child(self.auto_caption_box)
        self.append(self.auto_caption_expander)
        self.rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.rows.add_css_class("boxed-list")
        self.rows.connect("row-selected", self.select_row)
        area = scroll(self.rows)
        area.set_min_content_height(125)
        area.set_max_content_height(180)
        area.set_propagate_natural_height(True)
        area.set_vexpand(False)
        self.append(area)
        tools = box(False, 6)
        tools.append(button("Add", self.add, "suggested-action"))
        tools.append(button("Copy", self.duplicate, "flat"))
        tools.append(button("Delete", self.delete, "flat"))
        self.append(tools)
        history = box(False, 6)
        self.undo_button = button("Undo", self.undo, "flat", "edit-undo-symbolic")
        self.redo_button = button("Redo", self.redo, "flat", "edit-redo-symbolic")
        history.append(self.undo_button)
        history.append(self.redo_button)
        self.append(history)
        self.details = box(True, 12)
        self.append(self.details)
        self.enabled = Gtk.CheckButton(label="Enable this layer", active=True)
        self.details.append(self.enabled)
        times = box(False, 8)
        self.start = spin(0, 86400, 0.1, 0, 2)
        self.end = spin(0.01, 86400, 0.1, 2, 2)
        self.click_duration = spin(0.01, 10, 0.1, 0.6, 2)
        self.click_duration.set_numeric(True)
        self.click_duration.set_tooltip_text(
            "How long the click effect remains visible: 0.01 to 10 seconds. "
            "Moving the start time keeps this duration."
        )
        for name, title, control in (
            ("start_field", "Start · seconds", self.start),
            ("end_field", "End · seconds", self.end),
            ("click_duration_field", "Duration · seconds", self.click_duration),
        ):
            col = box()
            field(col, title, control)
            setattr(self, name, col)
            times.append(col)
        self.details.append(times)
        mark = box(False, 6)
        mark.append(
            button("Start here", lambda: self.start.set_value(self.playhead()), "flat")
        )
        self.end_here_button = button(
            "End here", lambda: self.end.set_value(self.playhead()), "flat"
        )
        mark.append(self.end_here_button)
        self.details.append(mark)
        self.text_box = box(True, 6)
        self.text_box.append(
            label("Text · plain text, multiple lines supported", "small muted", True)
        )
        self.text = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.text.set_top_margin(9)
        self.text.set_bottom_margin(9)
        self.text.set_left_margin(9)
        self.text.set_right_margin(9)
        text_scroll = scroll(self.text)
        text_scroll.set_min_content_height(96)
        text_scroll.set_max_content_height(150)
        text_scroll.set_vexpand(False)
        self.text_box.append(text_scroll)
        self.details.append(self.text_box)
        self.annotation_kind = dropdown(
            ["Text label", "Arrow", "Outline box", "Highlight"]
        )
        self.annotation_kind.connect("notify::selected", self.update_detail_visibility)
        self.annotation_field = box()
        field(self.annotation_field, "Annotation", self.annotation_kind)
        self.details.append(self.annotation_field)
        self.button_kind = dropdown(["Left click", "Right click", "Middle click"])
        self.button_field = box()
        field(self.button_field, "Button", self.button_kind)
        self.details.append(self.button_field)
        self.position = box(True, 10)
        coords = box(False, 8)
        self.x, self.y = spin(0, 100, 1, 50, 1), spin(0, 100, 1, 50, 1)
        for title, control in (("X · %", self.x), ("Y · %", self.y)):
            col = box()
            field(col, title, control)
            coords.append(col)
        self.position.append(coords)
        self.position.append(
            button(
                "Place on video",
                lambda: self.arm_pick("start"),
                "flat",
                "find-location-symbolic",
            )
        )
        self.details.append(self.position)
        self.endpoint = box(True, 10)
        coords2 = box(False, 8)
        self.x2, self.y2 = spin(0, 100, 1, 70, 1), spin(0, 100, 1, 60, 1)
        for title, control in (("End X · %", self.x2), ("End Y · %", self.y2)):
            col = box()
            field(col, title, control)
            coords2.append(col)
        self.endpoint.append(coords2)
        self.endpoint.append(
            button("Place endpoint", lambda: self.arm_pick("end"), "flat")
        )
        self.details.append(self.endpoint)
        self.appearance = box(True, 10)
        self.color = field(
            self.appearance,
            "Color",
            dropdown(["White", "Sky", "Violet", "Gold", "Coral", "Mint"], 1),
        )
        self.custom_color = field(
            self.appearance, "Hex color", Gtk.Entry(text="#78c8ff", max_length=7)
        )
        self.color.connect(
            "notify::selected",
            lambda *_: self.custom_color.set_text(COLORS[self.color.get_selected()])
            if not self.loading
            else None,
        )
        self.size = field(
            self.appearance, "Size · % of frame", spin(0.5, 25, 0.5, 4, 1)
        )
        self.details.append(self.appearance)
        self.details.append(button("Apply layer", self.apply, "suggested-action"))
        self.details.append(
            label(
                "Times refer to the original take. Trim and speed are applied automatically on export.",
                "small muted",
                True,
            )
        )
        self.caption_tools = box(True, 12)
        self.caption_tools.append(Gtk.Separator())
        self.caption_tools.append(label("CAPTION TOOLS", "eyebrow"))
        self.caption_tools.append(
            button(
                "Import SRT / VTT",
                self.import_captions,
                "flat",
                "document-open-symbolic",
            )
        )
        export_row = box(False, 8)
        export_row.append(
            button("Export SRT", lambda: self.export_captions("srt"), "flat")
        )
        export_row.append(
            button("Export VTT", lambda: self.export_captions("vtt"), "flat")
        )
        self.caption_tools.append(export_row)
        self.caption_tools.append(
            label(
                "Subtitle files use the current trim and playback speed.",
                "small muted",
                True,
            )
        )
        self.caption_size = field(
            self.caption_tools, "Caption size · % of canvas", spin(1.5, 15, 0.5, 4.5, 1)
        )
        self.caption_color = field(
            self.caption_tools,
            "Caption hex color",
            Gtk.Entry(text="#ffffff", max_length=7),
        )
        self.caption_position = field(
            self.caption_tools, "Position", dropdown(["Bottom", "Top"])
        )
        self.caption_background = Gtk.CheckButton(
            label="Dark caption background", active=True
        )
        self.caption_tools.append(self.caption_background)
        self.speech_audio = field(
            self.auto_caption_box,
            "Transcribe audio",
            dropdown(["Mix all tracks", "Desktop only", "Microphone only"]),
        )
        self.language = field(
            self.auto_caption_box,
            "Language",
            dropdown(
                [
                    "Auto-detect",
                    "English",
                    "Spanish",
                    "French",
                    "German",
                    "Portuguese",
                    "Japanese",
                    "Chinese",
                ]
            ),
        )
        self.transcribe_button = button(
            "Generate captions",
            self.generate_captions,
            "suggested-action",
            "audio-input-microphone-symbolic",
        )
        self.auto_caption_box.append(self.transcribe_button)
        self.speech_status = label(
            "Speech stays on this computer. Generated words remain editable.",
            "small muted",
            True,
        )
        self.auto_caption_box.append(self.speech_status)
        self.speech_progress = Gtk.ProgressBar()
        self.speech_progress.set_visible(False)
        self.auto_caption_box.append(self.speech_progress)
        self.cancel_speech_button = button(
            "Cancel transcription", self.cancel_transcription, "flat"
        )
        self.cancel_speech_button.set_visible(False)
        self.auto_caption_box.append(self.cancel_speech_button)
        self.append(self.caption_tools)
        self.zoom_clicks_button = button(
            "Zoom to clicks",
            self.studio.zoom_to_clicks,
            "suggested-action",
            "find-location-symbolic",
        )
        self.append(self.zoom_clicks_button)
        self.click_note = label(
            "Captured clicks remain editable. You can also add click effects to imported videos. A click effect does not remove a cursor already baked into the recording.",
            "small muted",
            True,
        )
        self.append(self.click_note)
        self.details.set_sensitive(False)
        self.update_detail_visibility()
        self.update_history()

    def project(self):
        return self.studio.project

    def items(self, kind=None):
        project = self.project()
        return project.get(kind or self.current_kind, []) if project else []

    def selected(self):
        return next(
            (item for item in self.items() if item.get("id") == self.selected_id), None
        )

    def playhead(self):
        stream = self.studio.video.get_media_stream()
        seconds = stream.get_timestamp() / 1_000_000 if stream else 0
        if self.studio.preview_options:
            seconds = (
                seconds * self.studio.preview_options.speed
                + self.studio.preview_options.trim_start
            )
        return (
            min(max(0, seconds), self.project().get("duration", 0))
            if self.project()
            else 0
        )

    def load(self, project):
        self.loading = True
        try:
            clean_layers(project)
            self.selected_id = None
            self.pick_target = None
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.auto_caption_expander.set_expanded(not project.get("captions"))
            for kind, toggle in self.flags.items():
                toggle.set_active(
                    project.get("edits", {}).get(kind, True)
                    if isinstance(project.get("edits"), dict)
                    else True
                )
            style = project.get("caption_style", {})
            if not isinstance(style, dict):
                style = {}
            size = style.get("font_size", 0.045)
            self.caption_size.set_value(
                size * 100
                if isinstance(size, (int, float)) and math.isfinite(size)
                else 4.5
            )
            self.caption_position.set_selected(
                1 if style.get("position") == "top" else 0
            )
            self.caption_background.set_active(
                style.get("background", True) is not False
            )
            self.caption_color.set_text(style.get("color", "#ffffff"))
            self.start.set_range(0, max(0.01, project["duration"]))
            self.end.set_range(0.01, max(0.01, project["duration"] + 10))
            self.click_note.set_text(
                "Edit captured clicks or add your own. Click effects do not remove a cursor baked into the original.\n\n"
                + project.get(
                    "click_capture_status",
                    "Imported videos and replays support manually placed click effects.",
                )
            )
            self.refresh()
        finally:
            self.loading = False
        self.update_history()

    def snapshot(self):
        return {
            key: deepcopy(self.project().get(key, [] if key in KINDS else {}))
            for key in (*KINDS, "caption_style")
        }

    def snapshot_draft(self):
        """Capture the form losslessly, including entries that cannot be committed.

        This is recovery data, not renderable layer metadata. The caller decides
        where to persist it and when it has been recovered successfully.
        """
        if not self.project():
            return None
        fields = {
            name: getattr(self, name).get_text()
            for name in ("start", "end", "click_duration", "x", "y", "x2", "y2", "size")
        }
        fields.update(
            enabled=self.enabled.get_active(),
            text=self.get_text(),
            annotation_kind=self.annotation_kind.get_selected(),
            button_kind=self.button_kind.get_selected(),
            color=self.custom_color.get_text(),
        )
        return {
            "version": 1,
            "project_path": str(self.project().get("path", "")),
            "kind": self.current_kind,
            "selected_id": self.selected_id,
            "fields": fields,
            "numeric_values": {
                name: getattr(self, name).get_value()
                for name in (*DRAFT_NUMBERS, "caption_size")
            },
            "caption_style": {
                "size": self.caption_size.get_text(),
                "position": self.caption_position.get_selected(),
                "color": self.caption_color.get_text(),
                "background": self.caption_background.get_active(),
            },
            "visibility": {
                kind: widget.get_active() for kind, widget in self.flags.items()
            },
        }

    def restore_draft(self, draft):
        """Restore a matching recovery form without validating or committing it.

        Reject structurally damaged or mismatched recovery data before modifying
        the current form. Invalid numeric/color/text entries remain visible so the
        user can correct them; the last valid project metadata remains intact.
        """
        if not draft_matches_project(self.project(), draft):
            return False
        kind, selected_id = draft["kind"], draft.get("selected_id")
        fields, style, visibility = (
            draft["fields"],
            draft["caption_style"],
            draft["visibility"],
        )
        self.loading = True
        try:
            self.current_kind, self.selected_id = kind, selected_id
            self.kind.set_selected(KINDS.index(kind))
            self.pick_target = None
            self.refresh()
            # load_selected() temporarily toggles loading, so restore the guard
            # before writing form values that have selection-change callbacks.
            self.loading = True
            for name in DRAFT_NUMBERS:
                if "numeric_values" in draft:
                    getattr(self, name).set_value(draft["numeric_values"][name])
                getattr(self, name).set_text(fields[name])
            self.enabled.set_active(fields["enabled"])
            self.text.get_buffer().set_text(fields["text"])
            self.annotation_kind.set_selected(fields["annotation_kind"])
            self.button_kind.set_selected(fields["button_kind"])
            self.custom_color.set_text(fields["color"])
            if "numeric_values" in draft:
                self.caption_size.set_value(draft["numeric_values"]["caption_size"])
            self.caption_size.set_text(style["size"])
            self.caption_position.set_selected(style["position"])
            self.caption_color.set_text(style["color"])
            self.caption_background.set_active(style["background"])
            for key in KINDS:
                self.flags[key].set_active(visibility[key])
        finally:
            self.loading = False
        self.update_detail_visibility()
        self.studio.layer_guides.queue_draw()
        return True

    def remember(self):
        self.undo_stack.append(self.snapshot())
        self.undo_stack = self.undo_stack[-50:]
        self.redo_stack.clear()
        self.update_history()

    def update_history(self):
        self.undo_button.set_sensitive(bool(self.undo_stack))
        self.redo_button.set_sensitive(bool(self.redo_stack))

    def history(self, undo=True):
        if not self.project() or self.transcribing:
            return
        source, destination = (
            (self.undo_stack, self.redo_stack)
            if undo
            else (self.redo_stack, self.undo_stack)
        )
        if not source:
            return
        destination.append(self.snapshot())
        self.project().update(source.pop())
        style = self.project().get("caption_style", {})
        self.caption_size.set_value(style.get("font_size", 0.045) * 100)
        self.caption_position.set_selected(1 if style.get("position") == "top" else 0)
        self.caption_background.set_active(style.get("background", True))
        self.caption_color.set_text(style.get("color", "#ffffff"))
        self.selected_id = None
        saved = self.save_layers()
        self.refresh()
        self.update_history()
        return saved

    def undo(self):
        return self.history(True)

    def redo(self):
        return self.history(False)

    def get_text(self):
        buffer = self.text.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)

    def read_item(self):
        item = self.selected()
        if not item:
            return None
        result = deepcopy(item)
        result["enabled"] = self.enabled.get_active()
        start = spin_value(self.start, "Layer start")
        if self.current_kind == "clicks":
            result.update(
                t=start,
                duration=click_duration(
                    spin_value(self.click_duration, "Click duration")
                ),
                x=spin_value(self.x, "X position") / 100,
                y=spin_value(self.y, "Y position") / 100,
                size=spin_value(self.size, "Layer size") / 100,
                color=self.custom_color.get_text().strip(),
                button=("left", "right", "middle")[self.button_kind.get_selected()],
            )
        else:
            end = spin_value(self.end, "Layer end")
            if end <= start:
                raise ValueError("Layer end must be later than its start.")
            result.update(start=start, end=end, text=self.get_text())
            if self.current_kind == "annotations":
                result.update(
                    kind=("text", "arrow", "box", "highlight")[
                        self.annotation_kind.get_selected()
                    ],
                    x=spin_value(self.x, "X position") / 100,
                    y=spin_value(self.y, "Y position") / 100,
                    x2=spin_value(self.x2, "End X position") / 100,
                    y2=spin_value(self.y2, "End Y position") / 100,
                    size=spin_value(self.size, "Layer size") / 100,
                    color=self.custom_color.get_text().strip(),
                )
        return result

    def commit(self):
        if self.loading or not self.project():
            return
        from .overlays import validate_project_overlays

        candidate = deepcopy(self.project())
        item = self.read_item()
        if item:
            candidate[self.current_kind] = [
                item if x.get("id") == item["id"] else x for x in self.items()
            ]
        candidate["caption_style"] = {
            "font_size": spin_value(self.caption_size, "Caption size") / 100,
            "position": ("bottom", "top")[self.caption_position.get_selected()],
            "color": self.caption_color.get_text().strip(),
            "background": self.caption_background.get_active(),
        }
        validate_project_overlays(candidate, self.project()["duration"])
        if any(
            candidate.get(k) != self.project().get(k) for k in (*KINDS, "caption_style")
        ):
            self.remember()
            for key in (*KINDS, "caption_style"):
                if key in candidate:
                    self.project()[key] = candidate[key]
        self.studio.layer_timeline.queue_draw()
        self.studio.layer_guides.queue_draw()

    def guard(self, action):
        try:
            action()
            return True
        except Exception as exc:
            self.studio.error(exc)
            return False

    def save_layers(self):
        """Keep the draft visible and recoverable if its manifest cannot be saved."""
        try:
            save_project(self.project())
        except Exception as exc:
            self.studio.error(
                RuntimeError(
                    "Layer changes are still in memory, but could not be saved. "
                    "Keep Lumen open and try Save edits again.\n\n" + str(exc)
                )
            )
            return False
        return True

    def apply(self):
        if self.guard(self.commit):
            if self.studio.save_edits(False) is False:
                return
            self.refresh()
            self.studio.toast("Layer saved. Preview edits to see the result.")

    def change_kind(self, *_):
        if self.loading:
            return
        if not self.guard(self.commit):
            self.loading = True
            self.kind.set_selected(KINDS.index(self.current_kind))
            self.loading = False
            return
        self.current_kind = KINDS[self.kind.get_selected()]
        self.selected_id = None
        self.pick_target = None
        self.refresh()

    def refresh(self):
        from .ui import box, label

        was_loading = self.loading
        self.loading = True
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        chosen = None
        for item in self.items():
            row = Gtk.ListBoxRow()
            row.layer_id = item.get("id")
            content = box(True, 4)
            for attr in ("margin_top", "margin_bottom", "margin_start", "margin_end"):
                getattr(content, "set_" + attr)(8)
            title = (
                item.get("text", "").splitlines()[0]
                if item.get("text")
                else item.get("kind", item.get("button", "Layer")).title()
            )
            text = label(title)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_max_width_chars(26)
            content.append(text)
            start = item.get("t", item.get("start", 0))
            end = item.get("end", start + item.get("duration", 0.6))
            content.append(
                label(
                    f"{start:.2f}s – {end:.2f}s"
                    + (" · hidden" if item.get("enabled") is False else ""),
                    "small muted",
                )
            )
            row.set_child(content)
            self.rows.append(row)
            if row.layer_id == self.selected_id:
                chosen = row
        if chosen:
            self.rows.select_row(chosen)
        elif self.rows.get_first_child():
            chosen = self.rows.get_first_child()
            self.rows.select_row(chosen)
            self.selected_id = chosen.layer_id
        else:
            self.selected_id = None
        self.summary.set_text(
            " · ".join(
                f"{len(self.items(k))} {name}"
                for k, name in zip(KINDS, ("captions", "annotations", "clicks"))
            )
        )
        self.loading = was_loading
        self.load_selected()
        self.update_detail_visibility()
        if hasattr(self.studio, "layer_timeline"):
            self.studio.layer_timeline.queue_draw()
            self.studio.layer_guides.queue_draw()

    def select_row(self, _, row):
        if self.loading or not row:
            return
        if not self.guard(self.commit):
            self.loading = True
            child = self.rows.get_first_child()
            while child:
                if child.layer_id == self.selected_id:
                    self.rows.select_row(child)
                    break
                child = child.get_next_sibling()
            self.loading = False
            return
        self.selected_id = row.layer_id
        self.load_selected()
        self.studio.layer_guides.queue_draw()

    def load_selected(self):
        item = self.selected()
        self.details.set_sensitive(item is not None)
        self.details.set_visible(item is not None)
        if not item:
            return
        self.loading = True
        try:
            self.enabled.set_active(item.get("enabled", True))
            limit = (
                max(
                    self.project()["duration"],
                    item.get("t", item.get("start", 0)),
                    item.get("end", 0),
                )
                + 10
            )
            self.start.set_range(0, limit)
            self.end.set_range(0.01, limit)
            self.start.set_value(item.get("t", item.get("start", 0)))
            self.end.set_value(
                item.get("end", item.get("t", 0) + item.get("duration", 0.6))
            )
            self.click_duration.set_value(item.get("duration", 0.6))
            self.text.get_buffer().set_text(item.get("text", ""))
            for axis, default in (("x", 0.5), ("y", 0.5), ("x2", 0.7), ("y2", 0.6)):
                getattr(self, axis).set_value(item.get(axis, default) * 100)
            kind = item.get("kind", "text")
            self.annotation_kind.set_selected(
                ("text", "arrow", "box", "highlight").index(kind)
                if kind in ("text", "arrow", "box", "highlight")
                else 0
            )
            btn = item.get("button", "left")
            self.button_kind.set_selected(
                ("left", "right", "middle").index(btn)
                if btn in ("left", "right", "middle")
                else 0
            )
            color = item.get("color", "#78c8ff")
            self.color.set_selected(COLORS.index(color) if color in COLORS else 1)
            self.custom_color.set_text(color)
            self.size.set_range(
                0.5 if self.current_kind == "clicks" else 1,
                20 if self.current_kind == "clicks" else 25,
            )
            self.size.set_value(item.get("size", 0.04) * 100)
        finally:
            self.loading = False
        self.update_detail_visibility()

    def update_detail_visibility(self, *_):
        kind = self.current_kind
        if not hasattr(self, "click_note"):
            return
        self.end_field.set_visible(kind != "clicks")
        self.click_duration_field.set_visible(kind == "clicks")
        self.end_here_button.set_visible(kind != "clicks")
        self.text_box.set_visible(
            kind == "captions"
            or (kind == "annotations" and self.annotation_kind.get_selected() == 0)
        )
        self.annotation_field.set_visible(kind == "annotations")
        self.button_field.set_visible(kind == "clicks")
        self.position.set_visible(kind != "captions")
        self.endpoint.set_visible(
            kind == "annotations" and self.annotation_kind.get_selected() != 0
        )
        self.appearance.set_visible(kind != "captions")
        self.caption_tools.set_visible(kind == "captions")
        self.auto_caption_expander.set_visible(kind == "captions")
        self.click_note.set_visible(kind == "clicks")
        self.zoom_clicks_button.set_visible(kind == "clicks")
        for k, toggle in self.flags.items():
            toggle.set_visible(k == kind)

    def cursor_at(self, seconds):
        try:
            data = json.loads(
                (Path(self.project()["path"]) / "cursor.json").read_text()
            )
            samples = [s for s in data.get("samples", []) if s.get("inside", True)]
            sample = min(samples, key=lambda s: abs(s["t"] - seconds))
            return sample["x"], sample["y"]
        except (OSError, ValueError, KeyError, TypeError):
            return 0.5, 0.5

    def add(self):
        if not self.project() or not self.guard(self.commit):
            return
        self.remember()
        t = min(self.playhead(), max(0, self.project()["duration"] - 0.01))
        item = {"id": uuid.uuid4().hex, "enabled": True}
        if self.current_kind == "clicks":
            x, y = self.cursor_at(t)
            item.update(
                t=t, x=x, y=y, button="left", duration=0.6, size=0.04, color="#78c8ff"
            )
        else:
            item.update(
                start=t,
                end=min(t + 2, self.project()["duration"]),
                text="Your caption" if self.current_kind == "captions" else "Look here",
            )
            if self.current_kind == "annotations":
                item.update(
                    kind="text",
                    x=0.5,
                    y=0.35,
                    x2=0.7,
                    y2=0.6,
                    color="#78c8ff",
                    size=0.045,
                )
        self.items().append(item)
        self.selected_id = item["id"]
        saved = self.save_layers()
        self.refresh()
        return saved

    def duplicate(self):
        if not self.selected() or not self.guard(self.commit):
            return
        self.remember()
        item = deepcopy(self.selected())
        item["id"] = uuid.uuid4().hex
        self.items().append(item)
        self.selected_id = item["id"]
        saved = self.save_layers()
        self.refresh()
        return saved

    def delete(self):
        if not self.selected():
            return
        self.remember()
        self.project()[self.current_kind] = [
            item for item in self.items() if item.get("id") != self.selected_id
        ]
        self.selected_id = None
        saved = self.save_layers()
        self.refresh()
        return saved

    def arm_pick(self, target):
        if not self.selected():
            return
        if self.studio.export_busy:
            self.studio.toast(
                "Wait for the preview or export to finish before placing a layer."
            )
            return
        item = self.selected()
        start = item.get("t", item.get("start", 0))
        end = item.get("end", start + item.get("duration", 0.6))
        self.studio.show_original(max(start, min(self.playhead(), end)))
        self.pick_target = target
        self.studio.video.set_cursor_from_name("crosshair")
        self.studio.toast(
            "Click a position on the original video. Escape cancels placement."
        )

    def place(self, x, y):
        if not self.pick_target or not self.selected():
            return False
        point = video_point(
            x,
            y,
            self.studio.video.get_width(),
            self.studio.video.get_height(),
            self.project()["width"],
            self.project()["height"],
        )
        if not point:
            return True
        target = self.pick_target
        self.cancel_pick()
        px, py = (self.x2, self.y2) if target == "end" else (self.x, self.y)
        px.set_value(point[0] * 100)
        py.set_value(point[1] * 100)
        self.apply()
        return True

    def cancel_pick(self):
        self.pick_target = None
        self.studio.video.set_cursor_from_name(None)

    def select_at(self, kind, identifier, seconds):
        if not self.guard(self.commit):
            return
        self.current_kind = kind
        self.loading = True
        self.kind.set_selected(KINDS.index(kind))
        self.loading = False
        self.selected_id = identifier
        self.refresh()
        self.studio.inspector.set_visible_child_name("layers")
        self.seek(seconds)

    def seek(self, seconds):
        stream = self.studio.video.get_media_stream()
        if stream:
            if self.studio.preview_options:
                seconds = (
                    seconds - self.studio.preview_options.trim_start
                ) / self.studio.preview_options.speed
            stream.seek(int(max(0, seconds) * 1_000_000))
            stream.pause()

    def import_captions(self):
        if not self.project() or not self.guard(self.commit):
            return
        project = self.project()
        chooser = Gtk.FileDialog(title="Import captions")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filt = Gtk.FileFilter(name="Subtitles (SRT / WebVTT)")
        filt.add_pattern("*.srt")
        filt.add_pattern("*.vtt")
        filters.append(filt)
        chooser.set_filters(filters)

        def chosen(dialog, result):
            try:
                path = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            if not path or self.project() is not project:
                return

            def action():
                from .captions import load_captions

                captions = load_captions(path)
                combined = caption_candidate(project, captions)
                self.remember()
                project["captions"] = combined
                saved = self.save_layers()
                self.refresh()
                if not saved:
                    return
                self.studio.toast(
                    f"Imported {len(captions)} captions. Undo reverses this import."
                )

            self.guard(action)

        chooser.open(self.studio, None, chosen)

    def export_captions(self, fmt):
        if not self.project() or not self.guard(self.commit):
            return
        captions = deepcopy(self.items("captions"))
        options = self.studio.edit_options()
        chooser = Gtk.FileDialog(
            title="Export subtitles", initial_name=f"captions.{fmt}"
        )
        chooser.set_initial_folder(Gio.File.new_for_path(self.project()["path"]))

        def chosen(dialog, result):
            try:
                path = dialog.save_finish(result).get_path()
            except GLib.Error:
                return
            if not path:
                return

            def action():
                from .captions import write_srt, write_vtt

                writer = write_srt if fmt == "srt" else write_vtt
                writer(
                    captions,
                    path,
                    trim_start=options.trim_start,
                    trim_end=options.trim_end,
                    speed=options.speed,
                )
                self.studio.toast("Subtitles exported with your trim and speed.")

            self.guard(action)

        chooser.save(self.studio, None, chosen)

    def generate_captions(self):
        if not self.project() or self.transcribing or not self.guard(self.commit):
            return
        # Existing cues stay intact: generation appends and has an Undo entry.
        from .captions import Transcriber

        project = deepcopy(self.project())
        self.transcribe_generation += 1
        generation = self.transcribe_generation
        self.transcribing = True
        self.transcriber = Transcriber()
        transcriber = self.transcriber
        self.transcribe_button.set_sensitive(False)
        self.cancel_speech_button.set_visible(True)
        self.speech_progress.set_visible(True)
        self.speech_progress.set_fraction(0)
        self.speech_status.set_text("Preparing local speech recognition…")
        mode = ("mix", "desktop", "mic")[self.speech_audio.get_selected()]
        language = (None, "en", "es", "fr", "de", "pt", "ja", "zh")[
            self.language.get_selected()
        ]

        def progress(value, message="Transcribing on this computer…"):
            def update():
                self.speech_progress.set_fraction(max(0, min(1, float(value))))
                self.speech_status.set_text(message)
                return False

            GLib.idle_add(update)

        def finished():
            self.transcribing = False
            self.transcriber = None
            self.transcribe_button.set_sensitive(True)
            self.cancel_speech_button.set_visible(False)

        def done(cues):
            finished()
            if (
                generation != self.transcribe_generation
                or not self.project()
                or self.project()["path"] != project["path"]
            ):
                return
            # A user may keep editing while speech runs. Preserve the active form;
            # do not replace its unsaved text when the transcript arrives.
            try:
                has_draft = self.read_item() != self.selected()
            except (ValueError, TypeError):
                has_draft = True
            try:
                combined = caption_candidate(self.project(), cues)
            except (ValueError, TypeError) as exc:
                self.speech_status.set_text(str(exc))
                self.studio.error(exc)
                return
            self.remember()
            self.project()["captions"] = combined
            saved = self.save_layers()
            if not has_draft:
                self.refresh()
            else:
                self.studio.layer_timeline.queue_draw()
            if not saved:
                self.speech_status.set_text(
                    "Captions generated but not saved. Keep Lumen open and retry Save edits."
                )
                return
            self.speech_progress.set_fraction(1)
            self.speech_status.set_text(
                f"Added {len(cues)} captions. Review the words and timing before sharing."
            )

        def failed(exc):
            finished()
            self.speech_status.set_text(str(exc))

        def status(message):
            GLib.idle_add(lambda: (self.speech_status.set_text(message), False)[1])

        self.studio.worker(
            lambda: transcriber.transcribe(
                project,
                audio_mode=mode,
                language=language,
                on_progress=progress,
                on_status=status,
            ),
            done,
            failed,
        )

    def cancel_transcription(self):
        if self.transcriber:
            self.transcriber.cancel()


class LayerTimeline(Gtk.DrawingArea):
    def __init__(self, studio):
        super().__init__()
        self.studio = studio
        self.set_content_height(112)
        self.set_draw_func(self.draw)
        gesture = Gtk.GestureClick()
        gesture.connect("pressed", self.clicked)
        self.add_controller(gesture)

    def draw(self, _, cr, width, height):
        project = self.studio.project
        cr.set_source_rgb(0.10, 0.105, 0.15)
        cr.paint()
        if not project:
            return
        duration = max(0.01, project.get("duration", 0.01))
        left, right = 58, width - 12
        length = right - left
        colors = ((0.73, 0.65, 1), (0.48, 0.79, 1), (1, 0.8, 0.4))
        cr.select_font_face("sans-serif", 0, 0)
        cr.set_font_size(10)
        for lane, (kind, color, title) in enumerate(
            zip(KINDS, colors, ("Captions", "Notes", "Clicks"))
        ):
            y = 12 + lane * 26
            cr.set_source_rgb(0.58, 0.59, 0.68)
            cr.move_to(6, y + 12)
            cr.show_text(title)
            cr.set_source_rgb(0.18, 0.19, 0.25)
            cr.rectangle(left, y, length, 18)
            cr.fill()
            for item in project.get(kind, []):
                if not isinstance(item, dict):
                    continue
                start = item.get("t", item.get("start", 0))
                end = item.get("end", start + item.get("duration", 0.6))
                if not all(
                    isinstance(v, (int, float)) and math.isfinite(v)
                    for v in (start, end)
                ):
                    continue
                x = left + max(0, min(1, start / duration)) * length
                w = max(3, min(right - x, (end - start) / duration * length))
                cr.set_source_rgba(*color, 0.9 if item.get("enabled", True) else 0.25)
                cr.rectangle(x, y + 2, w, 14)
                cr.fill()
                if (
                    item.get("id") == self.studio.layers.selected_id
                    and kind == self.studio.layers.current_kind
                ):
                    cr.set_source_rgb(1, 1, 1)
                    cr.set_line_width(1)
                    cr.rectangle(x, y + 2, w, 14)
                    cr.stroke()
        seconds = self.studio.layers.playhead()
        cr.set_source_rgb(0.95, 0.95, 1)
        x = left + seconds / duration * length
        cr.set_line_width(1.5)
        cr.move_to(x, 7)
        cr.line_to(x, 91)
        cr.stroke()
        cr.set_source_rgb(0.58, 0.59, 0.68)
        cr.move_to(left, 105)
        cr.show_text(f"{seconds:.2f}s / {duration:.2f}s · source timeline")

    def clicked(self, gesture, n, x, y):
        project = self.studio.project
        if not project:
            return
        duration = max(0.01, project.get("duration", 0.01))
        seconds = max(
            0, min(duration, (x - 58) / max(1, self.get_width() - 70) * duration)
        )
        lane = int((y - 12) // 26)
        if 0 <= lane < 3:
            kind = KINDS[lane]
            for item in reversed(project.get(kind, [])):
                start = item.get("t", item.get("start", 0))
                end = item.get("end", start + item.get("duration", 0.6))
                if start <= seconds <= end:
                    self.studio.layers.select_at(kind, item.get("id"), seconds)
                    return
        self.studio.layers.seek(seconds)


def draw_guides(studio, cr, width, height):
    if not studio.project or studio.preview_options or not hasattr(studio, "layers"):
        return
    item = studio.layers.selected()
    if (
        not item
        or studio.layers.current_kind == "captions"
        or studio.inspector.get_visible_child_name() != "layers"
    ):
        return
    ox, oy, w, h = video_bounds(
        width, height, studio.project["width"], studio.project["height"]
    )
    x, y = ox + item.get("x", 0.5) * w, oy + item.get("y", 0.5) * h
    cr.set_source_rgba(0.73, 0.65, 1, 0.85)
    cr.set_line_width(2)
    cr.set_dash([5, 4])
    if item.get("kind") in ("arrow", "box", "highlight"):
        x2, y2 = ox + item.get("x2", 0.7) * w, oy + item.get("y2", 0.6) * h
        if item.get("kind") == "arrow":
            cr.move_to(x, y)
            cr.line_to(x2, y2)
        else:
            cr.rectangle(min(x, x2), min(y, y2), abs(x2 - x), abs(y2 - y))
    else:
        cr.arc(x, y, 12, 0, math.tau)
        cr.move_to(x - 18, y)
        cr.line_to(x + 18, y)
        cr.move_to(x, y - 18)
        cr.line_to(x, y + 18)
    cr.stroke()
