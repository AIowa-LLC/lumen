#!/usr/bin/python
"""Opt-in GTK wallpaper verification using synthetic footage, never capture.

The file chooser callback receives a local generated image; the gallery, saved
recipe, actual player, preview, and export run in an isolated application/library.
Screenshots include only this verification application's own window.
"""
# ruff: noqa: E402

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(tempfile.mkdtemp(prefix="lumen-wallpapers-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen.editor import probe
from lumen.project import create_project, load_project, save_project
from lumen.ui import GLib, Gtk, LumenApplication
from lumen.wallpapers import wallpaper_path


project = create_project()
project.update(name="Wallpaper studio", duration=3, width=960, height=540, fps=30, status="ready")
source = Path(project["path"]) / "source.mkv"
subprocess.run([
    "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x172033:s=960x540:r=30:d=3",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-vf",
    "drawbox=x=40:y=40:w=880:h=54:color=0x2b3854:t=fill,"
    "drawbox=x=40:y=120:w=260:h=380:color=0x26314a:t=fill,"
    "drawbox=x=325:y=120:w=595:h=380:color=0x202b40:t=fill,"
    "drawtext=text='LUMEN':fontcolor=white:fontsize=26:x=65:y=52,"
    "drawtext=text='Make it yours.':fontcolor=0xbba5ff:fontsize=34:x=365:y=190",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
], check=True, capture_output=True)
save_project(project)
source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
image = root / "My custom wallpaper.png"
subprocess.run([
    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
    "gradients=s=1200x800:c0=0x187f8c:c1=0x492969:x0=0:y0=0:x1=1200:y1=800:speed=0:seed=0",
    "-frames:v", "1", str(image),
], check=True, capture_output=True)

app = LumenApplication()
app.set_application_id("io.github.lumen.Recorder.WallpapersVerify")
state = {"stage": "ready", "since": time.monotonic(), "deadline": time.monotonic() + 120, "errors": []}


def advance(stage):
    state.update(stage=stage, since=time.monotonic())
    print(stage, flush=True)


def snapshot(name):
    clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
    client = next((c for c in clients if c.get("pid") == os.getpid()), None)
    if client:
        x, y = client["at"]
        width, height = client["size"]
        subprocess.run(["grim", "-g", f"{x},{y} {width}x{height}", str(root / name)], check=True)
        return True
    return False


def tick():
    window = app.window
    if not window:
        return True
    if not state.get("hooked"):
        window.error = lambda exc: state["errors"].append(str(exc))
        state["hooked"] = True
    try:
        assert not state["errors"], state["errors"]
        assert time.monotonic() < state["deadline"], "Timed out: " + state["stage"]
        stage = state["stage"]
        if stage == "ready" and window.devices_ready:
            window.open_project(project)
            advance("opening")
        elif stage == "opening" and window.project:
            panel = window.wallpapers
            panel.tiles["builtin:aurora"].emit("clicked")
            assert window.background.get_selected() == 4
            assert window.edit_options().wallpaper == "builtin:aurora"
            assert panel.tiles["builtin:aurora"].has_css_class("selected")
            window.export_size.set_selected(2)
            window.padding.set_value(80)
            assert window.save_edits(False)
            advance("builtin-selected")
        elif stage == "builtin-selected" and time.monotonic() - state["since"] > .6:
            snapshot("builtin-gallery.png")
            chooser = Mock()
            chooser.open_finish.return_value.get_path.return_value = str(image)
            with patch.object(Gtk, "FileDialog", return_value=chooser):
                window.wallpapers.add_button.emit("clicked")
            chooser.open.call_args.args[-1](chooser, object())
            advance("importing")
        elif stage == "importing" and not window.wallpapers.importing:
            assert window.wallpapers.selected.startswith("wallpapers/")
            assert len(window.project["wallpapers"]) == 1
            state["custom"] = window.wallpapers.selected
            assert wallpaper_path(window.project, state["custom"]).is_file()
            image.unlink()
            saved = load_project(project["path"])
            assert saved["edits"]["wallpaper"] == state["custom"]
            assert saved["edits"]["background"] == "wallpaper"
            state["previous"] = window.project
            window.open_project(saved)
            advance("reopening")
        elif stage == "reopening" and window.project is not state["previous"]:
            assert window.wallpapers.selected == state["custom"]
            assert window.wallpapers.tiles[state["custom"]].has_css_class("selected")
            assert window.edit_options().background == "wallpaper"
            window.trim_start.set_value(.2)
            window.trim_end.set_value(2.8)
            window.speed.set_selected(3)
            window.layers.kind.set_selected(0)
            window.layers.add()
            window.layers.text.get_buffer().set_text("Your wallpaper, your story.")
            window.layers.start.set_value(.3)
            window.layers.end.set_value(2.6)
            window.layers.apply()
            window.render_preview()
            advance("preview")
        elif stage == "preview" and not window.export_busy:
            assert window.preview_options is not None
            assert Path(window.preview_path).is_file()
            stream = window.video.get_media_stream()
            assert stream is not None
            stream.play()
            advance("playing")
        elif stage == "playing" and time.monotonic() - state["since"] > .8:
            stream = window.video.get_media_stream()
            assert stream.is_prepared() and stream.get_error() is None
            assert stream.get_timestamp() > 0
            stream.pause()
            snapshot("custom-preview.png")
            window.show_original()
            assert window.preview_options is None
            window.render_export(window.edit_options(), root / "wallpaper.mp4")
            advance("exporting")
        elif stage == "exporting" and not window.export_busy:
            output = root / "wallpaper.mp4"
            media = probe(output)
            assert media["width"] == 960 and media["height"] == 610
            assert abs(media["duration"] - (2.8-.2)/1.5) < .1
            assert len(media["audio_streams"]) == 1
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], check=True, capture_output=True)
            assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
            state["ok"] = True
            result = {"ok": True, "root": str(root), "source_unchanged": True,
                      "custom_reopened": True, "preview_played": True, "captions_rendered": True, "output": media}
            (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)
            app.quit()
            return False
    except Exception as exc:
        print(json.dumps({"ok": False, "stage": state["stage"], "error": str(exc), "root": str(root)}, indent=2), flush=True)
        app.quit()
        return False
    return True


GLib.timeout_add(150, tick)
app.run([sys.argv[0], "--studio"])
raise SystemExit(0 if state.get("ok") else 1)
