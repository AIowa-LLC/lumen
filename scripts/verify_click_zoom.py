#!/usr/bin/python
"""Opt-in GTK click-zoom verification using generated footage, never capture.

The test creates a private library and configuration under /tmp, opens a separate
application ID, and checks the actual controls, saved recipe, player and export.
An optional screenshot includes only this verification application's own window.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(tempfile.mkdtemp(prefix="lumen-click-zoom-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen.clicks import click_event
from lumen.editor import probe
from lumen.project import create_project, load_project, save_project
from lumen.ui import GLib, LumenApplication


project = create_project()
project.update(name="Zoom follows the story", duration=6, width=960, height=540,
               fps=30, status="ready")
project["clicks"] = [click_event(.9, .25, .3, "left"),
                     click_event(2.7, .75, .65, "right"),
                     click_event(4.5, .5, .5, "middle")]
project["clicks"][2]["enabled"] = False
source = Path(project["path"]) / "source.mkv"
subprocess.run([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
    "color=c=0x172033:s=960x540:r=30:d=6", "-vf",
    "drawgrid=width=120:height=90:thickness=2:color=0x53647e,"
    "drawbox=x=150:y=105:w=180:h=115:color=0x416ad1:t=fill,"
    "drawbox=x=630:y=295:w=180:h=115:color=0xd1554f:t=fill,"
    "drawtext=text='FIRST':fontcolor=white:fontsize=25:x=195:y=145,"
    "drawtext=text='SECOND':fontcolor=white:fontsize=25:x=664:y=337,"
    "drawtext=text='A  B  C  D  E  F  G  H':fontcolor=0xffd166:fontsize=30:x=48:y=470",
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    str(source),
], check=True)
save_project(project)
source_digest = hashlib.sha256(source.read_bytes()).hexdigest()

app = LumenApplication()
app.set_application_id("io.github.lumen.Recorder.ClickZoomVerify")
state = {"stage": "ready", "since": time.monotonic(),
         "deadline": time.monotonic() + 180, "errors": []}


def advance(stage):
    state.update(stage=stage, since=time.monotonic())
    print(stage, flush=True)


def snapshot():
    active = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"]))
    if active.get("pid") != os.getpid():
        return False
    x, y = active["at"]
    width, height = active["size"]
    subprocess.run(["grim", "-g", f"{x},{y} {width}x{height}",
                    str(root / "click-zoom.png")], check=True)
    return True


def sample_frame(path, seconds):
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path),
        "-frames:v", "1", "-vf", "scale=240:135", "-f", "rawvideo",
        "-pix_fmt", "rgb24", "pipe:1",
    ])


def assert_settings(window):
    options = window.edit_options()
    assert options.click_zoom is True
    assert options.clicks is False, "Hidden click rings should remain hidden"
    assert abs(options.click_zoom_amount - 2.1) < 1e-6
    assert abs(options.click_zoom_hold - 1.35) < 1e-6
    assert abs(options.click_zoom_transition - .4) < 1e-6
    assert abs(options.zoom - 1.6) < 1e-6
    assert abs(options.zoom_start - .7) < 1e-6
    assert abs(options.zoom_end - 4.9) < 1e-6
    assert window.click_zoom_controls.get_sensitive()
    assert not window.manual_zoom.get_sensitive()
    return options


def tick():
    window = app.window
    if not window:
        return True
    if not state.get("hooked"):
        window.error = lambda exc: state["errors"].append(str(exc))
        state["hooked"] = True
    try:
        if state["errors"]:
            raise AssertionError(state["errors"])
        if time.monotonic() > state["deadline"]:
            raise AssertionError("Timed out at " + state["stage"])
        stage = state["stage"]
        if stage == "ready" and window.devices_ready:
            window.open_project(project)
            advance("opening")
        elif stage == "opening" and window.project:
            layers = window.layers
            window.inspector.set_visible_child_name("layers")
            layers.kind.set_selected(2)
            layers.select_at("clicks", window.project["clicks"][0]["id"], .9)
            # Use the editing controls to change the event that drives the camera.
            layers.x.set_value(28)
            layers.y.set_value(32)
            layers.apply()
            layers.flags["clicks"].set_active(False)
            window.zoom.set_value(1.6)
            window.timed_zoom.set_active(True)
            window.zoom_start.set_value(.7)
            window.zoom_end.set_value(4.9)
            window.click_zoom.set_active(False)
            layers.zoom_clicks_button.emit("clicked")
            assert window.click_zoom.get_active(), "Layers action did not enable zoom"
            assert window.inspector.get_visible_child_name() == "style", (
                "Layers action did not open the Style inspector"
            )
            window.click_zoom_amount.set_value(2.1)
            window.click_zoom_hold.set_value(1.35)
            window.click_zoom_transition.set_value(.4)
            window.click_zoom.set_active(False)
            assert not window.click_zoom_controls.get_sensitive()
            assert window.manual_zoom.get_sensitive()
            assert window.timed_zoom.get_active()
            assert abs(window.zoom.get_value() - 1.6) < 1e-6
            assert abs(window.zoom_start.get_value() - .7) < 1e-6
            assert abs(window.zoom_end.get_value() - 4.9) < 1e-6
            window.click_zoom.set_active(True)
            window.background.set_selected(3)
            window.padding.set_value(0)
            window.export_size.set_selected(2)
            window.trim_start.set_value(.2)
            window.trim_end.set_value(5.8)
            window.speed.set_selected(3)
            assert_settings(window)
            assert window.save_edits(False)
            saved = load_project(project["path"])
            assert saved["edits"]["click_zoom"] is True
            assert saved["edits"]["clicks"] is False
            assert saved["clicks"][0]["x"] == .28
            assert saved["clicks"][0]["y"] == .32
            assert saved["clicks"][2]["enabled"] is False
            assert len(saved["clicks"]) == 3
            state["before_reload"] = window.project
            window.open_project(saved)
            advance("reloading")
        elif stage == "reloading" and window.project is not state["before_reload"]:
            assert_settings(window)
            assert window.click_zoom.get_active()
            assert abs(window.click_zoom_amount.get_value() - 2.1) < 1e-6
            assert abs(window.click_zoom_hold.get_value() - 1.35) < 1e-6
            assert abs(window.click_zoom_transition.get_value() - .4) < 1e-6
            window.inspector.set_visible_child_name("style")
            window.render_preview()
            advance("rendering-preview")
        elif stage == "rendering-preview" and not window.export_busy:
            assert window.preview_options is not None, "No edited preview was created"
            assert Path(window.preview_path).is_file()
            stream = window.video.get_media_stream()
            assert stream is not None
            stream.play()
            advance("playing-preview")
        elif stage == "playing-preview" and time.monotonic() - state["since"] > .9:
            stream = window.video.get_media_stream()
            assert stream.is_prepared(), "Preview player was not prepared"
            assert stream.get_error() is None, stream.get_error()
            assert stream.get_timestamp() > 0, "Preview did not play"
            stream.pause()
            state["screenshot"] = snapshot()
            window.render_export(assert_settings(window), root / "click-zoom.mp4")
            advance("exporting")
        elif stage == "exporting" and not window.export_busy:
            output = root / "click-zoom.mp4"
            metadata = probe(output)
            assert metadata["width"] == 960
            assert abs(metadata["duration"] - (5.8-.2)/1.5) < .1
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(output),
                            "-f", "null", "-"], check=True, capture_output=True)
            # Static synthetic source: a material frame change must come from
            # camera motion because the recipe hides every click ring.
            before = sample_frame(output, .05)
            zoomed = sample_frame(output, (1.4-.2)/1.5)
            returned = sample_frame(output, (5.4-.2)/1.5)
            assert before and len(before) == len(zoomed)
            difference = sum(abs(a-b) for a, b in zip(before, zoomed)) / len(before)
            assert difference > 5, f"Hidden-ring click zoom did not visibly change frame: {difference}"
            assert len(returned) == len(before)
            return_difference = sum(abs(a-b) for a, b in zip(before, returned)) / len(before)
            assert return_difference < 2, (
                f"The disabled third click moved the camera or zoom failed to return: {return_difference}"
            )
            assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
            result = {"ok": True, "root": str(root), "output": metadata,
                      "source_unchanged": True, "click_events": len(window.project["clicks"]),
                      "click_rings_hidden": True, "zoom_enabled_after_reload": True,
                      "mean_frame_difference": round(difference, 3),
                      "returned_frame_difference": round(return_difference, 3),
                      "own_window_screenshot": state["screenshot"]}
            (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(json.dumps(result, indent=2), flush=True)
            state["ok"] = True
            app.quit()
            return False
    except Exception as exc:
        state["errors"].append(str(exc))
        print(json.dumps({"ok": False, "stage": state["stage"],
                          "errors": state["errors"], "root": str(root)}, indent=2), flush=True)
        app.quit()
        return False
    return True


GLib.timeout_add(150, tick)
app.run([sys.argv[0], "--studio"])
raise SystemExit(0 if state.get("ok") else 1)
