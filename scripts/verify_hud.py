#!/usr/bin/python
"""Opt-in native HUD lifecycle check; records only its own Studio window.

A separate application ID and private library/configuration isolate this check
from a user's running Lumen instance. No microphone, webcam, or desktop audio is
used. A final guard checks the target window and HUD bounds before native capture.
"""
# ruff: noqa: E402

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(tempfile.mkdtemp(prefix="lumen-hud-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen import ui
from lumen.editor import probe
from lumen.project import create_project, list_projects, load_project, save_project
from lumen.system import get_monitors, get_windows, parse_geometry

fixture = create_project()
fixture.update(name="A project kept while switching views", duration=2,
               width=640, height=360, fps=30, status="ready")
subprocess.run([
    "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
    "color=c=0x172033:s=640x360:r=30:d=2", "-c:v", "libx264",
    "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    str(Path(fixture["path"]) / "source.mkv"),
], check=True)
save_project(fixture)

app = ui.LumenApplication()
app.set_application_id("io.github.lumen.Recorder.HudVerify")
state = {"stage": "ready", "since": time.monotonic(),
         "deadline": time.monotonic()+100, "errors": [], "remotes": []}


def advance(stage):
    state.update(stage=stage, since=time.monotonic())
    print(stage, flush=True)


def remote(*flags):
    code = (
        "import sys; from lumen.ui import LumenApplication; app=LumenApplication(); "
        "app.set_application_id('io.github.lumen.Recorder.HudVerify'); "
        "raise SystemExit(app.run(['lumen']+sys.argv[1:]))"
    )
    child = subprocess.Popen([sys.executable, "-c", code, *flags],
                             cwd=Path(__file__).resolve().parents[1])
    state["remotes"].append(child)
    state["last_remote"] = child


def remote_done():
    child = state["last_remote"]
    if child.poll() is None:
        return False
    assert child.returncode == 0, f"Remote command failed: {child.returncode}"
    return True


def own_clients():
    clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
    return [client for client in clients if client.get("pid") == os.getpid()]


def owned_windows():
    clients = own_clients()
    studio = next((client for client in clients
                   if client.get("title") == "Lumen — Recording Studio"), None)
    hud = next((client for client in clients if client is not studio), None)
    return studio, hud


def dispatch_window(dispatcher, address, **values):
    # Hyprland 0.56 uses Lua dispatchers. Only observed, owned window addresses
    # reach this helper; no desktop configuration or persistent rules are added.
    values = {"window": "address:"+address, **values}
    arguments = ", ".join(f"{key} = {json.dumps(value)}" for key, value in values.items())
    subprocess.run(["hyprctl", "eval",
                    f"hl.dispatch(hl.dsp.window.{dispatcher}({{{arguments}}}))"],
                   check=True, capture_output=True)


def snapshot_hud():
    studio, hud = owned_windows()
    assert studio is None and hud is not None
    assert hud.get("floating") is True, "The compact HUD was tiled"
    x, y = hud["at"]
    width, height = hud["size"]
    subprocess.run(["grim", "-g", f"{x},{y} {width}x{height}",
                    str(root / "hud.png")], check=True)


def position_capture_windows():
    studio, hud = owned_windows()
    assert studio and hud, "Both owned windows must be visible for safe capture"
    monitor = next(m for m in get_monitors() if m["id"] == studio["monitor"])
    assert monitor["logical_width"] >= 1550, "This opt-in test needs a 1550px-wide desktop"
    x, y = monitor["x"]+20, monitor["y"]+35
    for client, width, height, px in ((studio, 960, 650, x),
                                    (hud, 520, 200, x+1010)):
        address = client["address"]
        dispatch_window("float", address, action="set")
        dispatch_window("resize", address, x=width, y=height, relative=False)
        dispatch_window("move", address, x=px, y=y, relative=False)


def assert_safe_capture(options):
    studio, hud = owned_windows()
    assert studio and hud, "Target Studio disappeared before recording"
    x, y, width, height = parse_geometry(options.geometry)
    assert (x, y) == tuple(studio["at"])
    assert (width, height) == tuple(studio["size"])
    hx, hy = hud["at"]
    hw, hh = hud["size"]
    assert x+width <= hx or hx+hw <= x or y+height <= hy or hy+hh <= y, (
        "HUD overlaps the test's fixed Studio capture area"
    )
    assert options.audio == "none" and options.webcam is None


def assert_same_windows():
    assert app.window is state["studio"] and app.hud is state["hud"]
    assert len(app.get_windows()) == 2, "A command created duplicate Lumen windows"


def tick():
    studio, hud = app.window, getattr(app, "hud", None)
    if not studio or not hud:
        return True
    if not state.get("hooked"):
        studio.error = lambda exc: state["errors"].append(str(exc))
        state.update(hooked=True, studio=studio, hud=hud)
    try:
        if state["errors"]:
            raise AssertionError(state["errors"])
        if time.monotonic() > state["deadline"]:
            raise AssertionError("HUD workflow timed out at "+state["stage"])
        stage, age = state["stage"], time.monotonic()-state["since"]
        if stage == "ready" and studio.devices_ready and age > 1:
            assert app.view == "hud" and hud.get_visible() and not studio.get_visible()
            assert hud.get_width() <= 800 and hud.get_height() <= 200, (
                f"HUD is not compact: {hud.get_width()}×{hud.get_height()}"
            )
            state["default_hud_size"] = [hud.get_width(), hud.get_height()]
            snapshot_hud()
            assert_same_windows()
            hud.studio_button.emit("clicked")
            assert app.view == "studio" and studio.get_visible() and not hud.get_visible()
            studio.open_project(fixture)
            advance("opening-fixture")
        elif stage == "opening-fixture" and studio.project:
            studio.project_name.set_text("Edits survive returning to HUD")
            studio.zoom.set_value(1.7)
            assert studio.save_edits(False)
            original = studio.project
            assert studio.on_close() is True
            assert app.view == "hud" and hud.get_visible() and not studio.get_visible()
            assert studio.project is original
            assert load_project(fixture["path"])["edits"]["zoom"] == 1.7
            remote("--studio")
            advance("cli-studio")
        elif stage == "cli-studio" and remote_done():
            assert app.view == "studio" and studio.get_visible() and not hud.get_visible()
            assert studio.project["path"] == fixture["path"]
            assert studio.zoom.get_value() == 1.7
            remote()
            advance("cli-current-studio")
        elif stage == "cli-current-studio" and remote_done():
            assert app.view == "studio" and studio.get_visible()
            assert_same_windows()
            remote("--hud")
            advance("cli-hud")
        elif stage == "cli-hud" and remote_done():
            assert app.view == "hud" and hud.get_visible() and not studio.get_visible()
            remote()
            advance("cli-current-hud")
        elif stage == "cli-current-hud" and remote_done():
            assert app.view == "hud" and hud.get_visible() and not studio.get_visible()
            assert_same_windows()
            # Production shows one view at a time. Here only, keep Studio mapped
            # as a safe target while controlling its existing recorder via HUD.
            studio.present()
            hud.present()
            advance("positioning")
        elif stage == "positioning" and age > .5:
            position_capture_windows()
            advance("configuring-capture")
        elif stage == "configuring-capture" and age > .5:
            studio.windows = get_windows()
            target = next(i for i, item in enumerate(studio.windows)
                          if item.get("pid") == os.getpid()
                          and "Recording Studio" in item.get("title", item.get("label", "")))
            studio.window_picker.set_model(ui.Gtk.StringList.new([
                item["label"] for item in studio.windows]))
            studio.window_picker.set_selected(target)
            studio.mode.set_selected(2)
            studio.audio.set_selected(0)
            studio.webcam.set_selected(0)
            studio.resolution.set_selected(2)
            studio.hide_recording.set_active(False)
            studio.record_clicks.set_active(False)
            studio.delay.set_selected(1)
            original_start = studio.start_backend

            def guarded_start(options, replay_seconds=None):
                assert_safe_capture(options)
                return original_start(options, replay_seconds)

            studio.start_backend = guarded_start
            assert_safe_capture(studio.capture_options())
            hud.refresh()
            hud.primary_button.emit("clicked")
            advance("countdown")
        elif stage == "countdown" and studio.countdown_source and age > .3:
            assert studio.capture_busy and not studio.recorder.is_running
            hud.refresh()
            hud.primary_button.emit("clicked")
            assert studio.countdown_source is None and not studio.capture_busy
            assert not studio.recorder.is_running
            assert len(list_projects()) == 1, "Cancelled countdown created a recording"
            studio.delay.set_selected(0)
            hud.refresh()
            hud.primary_button.emit("clicked")
            advance("recording")
        elif stage == "recording" and studio.recorder.is_running and not studio.capture_busy and age > 1:
            assert app.view == "hud" and hud.get_visible()
            hud.refresh()
            process = studio.recorder.process
            state["recording_pid"] = process.pid
            dialog = Mock()
            with patch.object(ui.Adw, "AlertDialog", return_value=dialog):
                assert hud.on_close() is True
            dialog.present.assert_called_once()
            assert process.poll() is None and not studio.closing
            hud.secondary_button.emit("clicked")
            advance("pausing")
        elif stage == "pausing" and not studio.capture_busy and age > .3:
            assert studio.recorder.status == "paused"
            hud.refresh()
            assert "resume" in hud.secondary_button.get_tooltip_text().lower()
            assert hud.status_label.get_text() == "Paused"
            assert studio.recorder.process.pid == state["recording_pid"]
            hud.secondary_button.emit("clicked")
            advance("resuming")
        elif stage == "resuming" and not studio.capture_busy and age > .7:
            assert studio.recorder.status == "recording"
            hud.refresh()
            hud.primary_button.emit("clicked")
            advance("saving")
        elif stage == "saving" and not studio.capture_busy and studio.project["path"] != fixture["path"]:
            assert not studio.recorder.is_running
            assert app.view == "studio" and studio.get_visible()
            take = load_project(studio.project["path"])
            source = Path(take["path"]) / take["source"]
            metadata = probe(source)
            assert metadata["duration"] > .5
            # Native takes reserve a silent mic track for later live enabling.
            expected_audio = 1 if studio.project.get("capture", {}).get("live_inputs") else 0
            assert len(metadata["audio_streams"]) == expected_audio
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source),
                            "-f", "null", "-"], check=True, capture_output=True)
            assert_same_windows()
            result = {"ok": True, "root": str(root), "default_hud_size": state["default_hud_size"],
                      "countdown_cancelled": True, "close_guard_kept_recording": True,
                      "single_instance_views": True, "saved_take": str(source), "media": metadata}
            (root / "result.json").write_text(json.dumps(result, indent=2)+"\n")
            print(json.dumps(result, indent=2), flush=True)
            state["ok"] = True
            app.request_quit()
            return False
    except Exception as exc:
        state["errors"].append(str(exc))
        print(json.dumps({"ok": False, "stage": state["stage"],
                          "errors": state["errors"], "root": str(root)}, indent=2), flush=True)
        app.quit()  # do_shutdown finalizes any owned capture started by this test.
        return False
    return True


ui.GLib.timeout_add(100, tick)
app.run([sys.argv[0]])
raise SystemExit(0 if state.get("ok") else 1)
