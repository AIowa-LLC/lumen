"""Native wallpaper gallery and asynchronous custom image authoring."""

from __future__ import annotations

from copy import deepcopy

from gi.repository import Gio, GLib, Gtk, Pango

from .project import load_project, save_project
from .wallpapers import (
    BACKGROUND_STYLES,
    BUILTIN_WALLPAPERS,
    DEFAULT_WALLPAPER,
    custom_wallpapers,
    import_wallpaper,
    wallpaper_path,
)


class WallpaperPanel(Gtk.Box):
    def __init__(self, studio):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.studio = studio
        self.selected = DEFAULT_WALLPAPER
        self.tiles = {}
        self.importing = False
        self.gallery = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            min_children_per_line=2, max_children_per_line=2,
            column_spacing=8, row_spacing=8, homogeneous=True,
        )
        self.gallery.add_css_class("wallpaper-gallery")
        self.append(self.gallery)
        self.add_button = Gtk.Button(label="Add your own wallpaper")
        self.add_button.connect("clicked", lambda *_: self.add_image())
        self.append(self.add_button)
        self.note = Gtk.Label(xalign=0, wrap=True)
        self.note.add_css_class("small")
        self.note.add_css_class("muted")
        self.append(self.note)
        self.css = Gtk.CssProvider()
        studio.frame.get_style_context().add_provider(self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)

    def load(self, edits):
        selection = edits.get("wallpaper", DEFAULT_WALLPAPER)
        self.selected = selection if isinstance(selection, str) and selection else DEFAULT_WALLPAPER
        self.gallery.remove_all()
        self.tiles.clear()
        choices = [(f"builtin:{key}", name) for key, name in BUILTIN_WALLPAPERS.items()]
        choices += [(item["path"], item["name"]) for item in custom_wallpapers(self.studio.project or {})]
        if self.selected not in dict(choices):
            choices.append((self.selected, "Saved wallpaper"))
        for selection, name in choices:
            self._tile(selection, name)
        self.update_frame()

    def _tile(self, selection, name):
        tile = Gtk.Button()
        tile.add_css_class("wallpaper-tile")
        tile.set_hexpand(True)
        tile.set_tooltip_text(name)
        tile.update_property([Gtk.AccessibleProperty.LABEL], [name + " wallpaper"])
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        try:
            path = wallpaper_path(self.studio.project or {}, selection)
            picture = Gtk.Picture.new_for_filename(str(path))
            picture.set_content_fit(Gtk.ContentFit.COVER)
            picture.set_can_shrink(True)
            picture.set_size_request(100, 58)
        except ValueError:
            picture = Gtk.Image.new_from_icon_name("image-missing-symbolic")
            picture.set_size_request(100, 58)
        content.append(picture)
        title = Gtk.Label(label=name, ellipsize=Pango.EllipsizeMode.END, max_width_chars=16)
        title.add_css_class("small")
        content.append(title)
        tile.set_child(content)
        tile.connect("clicked", lambda *_: self.select(selection))
        self.gallery.append(tile)
        self.tiles[selection] = tile

    def select(self, selection):
        self.selected = selection
        self.studio.background.set_selected(BACKGROUND_STYLES.index("wallpaper"))
        self.studio.update_frame()

    def update_frame(self):
        index = self.studio.background.get_selected()
        enabled = index == BACKGROUND_STYLES.index("wallpaper")
        for selection, tile in self.tiles.items():
            if enabled and selection == self.selected:
                tile.add_css_class("selected")
            else:
                tile.remove_css_class("selected")
        self.note.remove_css_class("error")
        self.note.set_text("Images fill the backdrop. Increase frame padding to show more; Preview edits renders the result.")
        image = "none"
        if enabled:
            try:
                path = wallpaper_path(self.studio.project or {}, self.selected)
                if not self.studio.preview_options:
                    image = f'url("{path.as_uri()}")'
            except (ValueError, KeyError) as exc:
                self.note.set_text(str(exc))
                self.note.add_css_class("error")
        self.css.load_from_data(
            f".preview-frame {{ background-image: {image}; background-size: cover; background-position: center; }}".encode()
        )

    def add_image(self):
        studio = self.studio
        if not studio.project or self.importing:
            return
        project = deepcopy(studio.project)
        generation = studio.open_generation
        chooser = Gtk.FileDialog(title="Add a wallpaper")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter(name="Images · PNG, JPEG, WebP")
        for mime in ("image/png", "image/jpeg", "image/webp"):
            image_filter.add_mime_type(mime)
        filters.append(image_filter)
        chooser.set_filters(filters)

        def chosen(dialog, result):
            try:
                source = dialog.open_finish(result).get_path()
            except GLib.Error:
                return
            if not source:
                return
            if not studio.project or generation != studio.open_generation:
                studio.toast("The open project changed. Add the wallpaper to the current take.")
                return
            self.importing = True
            self.add_button.set_sensitive(False)
            self.add_button.set_label("Adding wallpaper…")

            def finish():
                self.importing = False
                self.add_button.set_sensitive(True)
                self.add_button.set_label("Add your own wallpaper")

            def failed(exc):
                finish()
                studio.error(exc)

            def imported(item):
                finish()
                try:
                    # Publish on the UI thread, using the latest recipe; the
                    # import must never overwrite edits made while decoding.
                    latest = load_project(project["path"])
                    entries = custom_wallpapers(latest)
                    if item["path"] not in {entry["path"] for entry in entries}:
                        entries.append(item)
                    latest["wallpapers"] = entries
                    save_project(latest)
                    if studio.project and generation == studio.open_generation:
                        studio.project["wallpapers"] = entries
                        self.load({"wallpaper": item["path"]})
                        self.select(item["path"])
                        if studio.save_edits(False):
                            studio.toast("Wallpaper added to this take.")
                except Exception as exc:
                    studio.error(exc)

            studio.worker(lambda: import_wallpaper(project, source), imported, failed)

        chooser.open(studio, None, chosen)
