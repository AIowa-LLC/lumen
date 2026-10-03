#!/usr/bin/python
"""Opt-in live GTK workflow test. Records a short silent take in /tmp."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(tempfile.mkdtemp(prefix="lumen-ui-check-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen.ui import LumenApplication, GLib  # noqa: E402
from lumen.editor import probe  # noqa: E402
from lumen.system import get_windows  # noqa: E402
from lumen.project import list_projects  # noqa: E402

app = LumenApplication()
app.set_application_id("io.github.lumen.Recorder.Verify")
state = {
    "stage": "ready",
    "since": time.monotonic(),
    "deadline": time.monotonic() + 60,
    "errors": [],
}
commands = []


def remote(flag):
    code = "import sys; from lumen.ui import LumenApplication; app=LumenApplication(); app.set_application_id('io.github.lumen.Recorder.Verify'); raise SystemExit(app.run(['lumen']+sys.argv[1:]))"
    commands.append(
        subprocess.Popen(
            [sys.executable, "-c", code, flag], cwd=Path(__file__).resolve().parents[1]
        )
    )


def advance(stage):
    state.update(stage=stage, since=time.monotonic())
    print(stage, flush=True)


def snapshot(name):
    clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
    for client in clients:
        if client.get("pid") == os.getpid():
            x, y = client["at"]
            w, h = client["size"]
            subprocess.run(
                ["grim", "-g", f"{x},{y} {w}x{h}", str(root / name)], check=True
            )
            break


def click_bindings():
    bindings = json.loads(subprocess.check_output(["hyprctl", "binds", "-j"]))
    return [binding for binding in bindings
            if binding.get("description", "").startswith("Lumen click capture ")]


def tick():
    window = app.window
    if not window:
        return True
    if "hooked" not in state:
        window.error = lambda exc: state["errors"].append(str(exc))
        state["hooked"] = True
    try:
        if state["errors"]:
            raise AssertionError(state["errors"])
        if time.monotonic() > state["deadline"]:
            raise AssertionError("UI workflow timed out at " + state["stage"])
        stage = state["stage"]
        age = time.monotonic() - state["since"]
        if stage == "ready" and window.devices_ready:
            assert window.monitors, "No monitors found"
            window.delay.set_selected(0)
            window.audio.set_selected(0)
            window.hide_recording.set_active(False)
            window.record_clicks.set_active(True)
            window.resolution.set_selected(2)
            # Exercise fixed-window capture while keeping unrelated desktop content
            # out of this repeatable UI test.
            window.windows = get_windows()
            own = next(
                i
                for i, item in enumerate(window.windows)
                if item.get("pid") == os.getpid()
            )
            from lumen.ui import Gtk

            window.window_picker.set_model(
                Gtk.StringList.new([item["label"] for item in window.windows])
            )
            window.window_picker.set_selected(own)
            window.mode.set_selected(2)
            snapshot("capture.png")
            remote("--record")
            advance("recording")
        elif (
            stage == "recording"
            and not window.capture_busy
            and window.recorder.is_running
            and age > 1
        ):
            assert window.recorder._clicks is not None, window.recorder.project.get("click_capture_status")
            owned = [binding for binding in click_bindings()
                     if window.recorder._clicks.token in binding["description"]]
            assert owned and all(binding.get("non_consuming") for binding in owned)
            state["native_click_buttons"] = len(owned)
            remote("--pause")
            advance("pausing")
        elif stage == "pausing" and not window.capture_busy and age > 0.5:
            assert window.recorder.status == "paused"
            remote("--pause")
            advance("resuming")
        elif stage == "resuming" and not window.capture_busy and age > 1:
            assert window.recorder.status == "recording"
            remote("--stop")
            advance("opening")
        elif stage == "opening" and window.project and not window.capture_busy:
            assert not click_bindings(), "Temporary click bindings survived recording stop"
            assert window.stack.get_visible_child_name() == "editor"
            stream = window.video.get_media_stream()
            assert stream is not None, "No media stream"
            stream.play()
            advance("preview")
        elif stage == "preview" and age > 1:
            stream = window.video.get_media_stream()
            assert stream.get_error() is None, stream.get_error()
            assert stream.is_prepared(), "Video stream not prepared"
            assert stream.get_timestamp() > 0, "Preview did not play"
            stream.pause()
            window.background.set_selected(1)
            window.trim_start.set_value(0.1)
            window.speed.set_selected(3)
            window.zoom.set_value(1.3)
            window.save_edits(False)
            window.render_preview()
            advance("rendering-draft")
        elif stage == "rendering-draft" and not window.export_busy:
            assert window.preview_options is not None, "No rendered draft"
            assert Path(window.preview_path).exists()
            advance("draft-playback")
        elif stage == "draft-playback" and age > 0.7:
            stream = window.video.get_media_stream()
            assert stream.get_error() is None, stream.get_error()
            assert stream.is_prepared()
            snapshot("studio.png")
            window.render_export(window.edit_options(), root / "ui-export.mp4")
            advance("exporting")
        elif stage == "exporting" and not window.export_busy:
            output = root / "ui-export.mp4"
            assert output.is_file(), "Export did not publish"
            meta = probe(output)
            assert meta["width"] == 1920
            state["output"] = meta
            state["source_duration"] = window.project["duration"]
            state["original_project"] = window.project["path"]
            window.show_page("capture")
            remote("--replay")
            advance("buffering")
        elif stage == "buffering" and not window.capture_busy and age > 2:
            assert window.replay.is_running
            remote("--save-replay")
            advance("saving-replay")
        elif stage == "saving-replay" and not window.capture_busy and age > 0.5:
            replays = [p for p in list_projects() if p.get("replay_seconds") == 30]
            if not replays:
                return True
            state["replay_duration"] = replays[0]["duration"]
            assert window.project["path"] == state["original_project"], (
                "Remote replay should preserve the current editor"
            )
            assert window.replay.is_running, "Replay save stopped buffer"
            remote("--stop")
            advance("stopping-replay")
        elif stage == "stopping-replay" and not window.capture_busy:
            assert not window.replay.is_running
            assert not click_bindings(), "Temporary click bindings survived replay workflow"
            assert all(child.poll() == 0 for child in commands), (
                "Remote command failed or did not finish"
            )
            window.show_page("library")
            snapshot("library.png")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "root": str(root),
                        "source_duration": state["source_duration"],
                        "output": state["output"],
                        "replay_duration": state["replay_duration"],
                        "native_click_buttons": state["native_click_buttons"],
                        "remaining_click_bindings": len(click_bindings()),
                    },
                    indent=2,
                ),
                flush=True,
            )
            state["ok"] = True
            app.quit()
            return False
    except Exception as exc:
        state["errors"].append(str(exc))
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": state["stage"],
                    "errors": state["errors"],
                    "root": str(root),
                },
                indent=2,
            ),
            flush=True,
        )
        app.quit()
        return False
    return True


GLib.timeout_add(150, tick)
app.run([sys.argv[0], "--studio"])
raise SystemExit(0 if state.get("ok") else 1)
