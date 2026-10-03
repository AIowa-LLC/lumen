#!/usr/bin/python
"""Verify invalid click drafts survive a real quit/relaunch, using synthetic media."""
# ruff: noqa: E402

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--phase", choices=("quit", "restore"))
parser.add_argument("--root", type=Path)
args = parser.parse_args()
root = args.root or Path(tempfile.mkdtemp(prefix="lumen-quit-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen.clicks import click_event
from lumen.project import create_project, load_project, save_project
from lumen.recovery import list_recoveries, read_recovery


if args.phase is None:
    project = create_project()
    project.update(name="Quit recovery fixture", duration=3, width=640, height=360,
                   fps=30, status="ready", clicks=[click_event(1, .5, .4, "left")])
    source = Path(project["path"]) / project["source"]
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=0x172033:s=640x360:r=30:d=3", "-vf",
        "drawtext=text='Your original stays safe':fontcolor=white:fontsize=28:x=90:y=160",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source),
    ], check=True)
    save_project(project)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (root / "fixture.json").write_text(json.dumps({"path": project["path"], "sha256": digest}))
    for phase in ("quit", "restore"):
        subprocess.run([sys.executable, __file__, "--phase", phase, "--root", str(root)], check=True)
    saved = load_project(project["path"])
    assert abs(saved["clicks"][0]["duration"]-.8) < 1e-6
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    result = {"ok": True, "root": str(root), "original_unchanged": True,
              "invalid_duration_backed_up": "24.14", "restored_and_corrected_duration": .8,
              "saved_name": saved["name"], "saved_trim": [saved["edits"]["trim_start"], saved["edits"]["trim_end"]],
              "handled_recovery": saved["draft_recovery_handled_through"]}
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n")
    print(json.dumps(result, indent=2), flush=True)
    raise SystemExit(0)


from lumen.ui import GLib, LumenApplication

fixture = json.loads((root / "fixture.json").read_text())
project = load_project(fixture["path"])
app = LumenApplication()
app.set_application_id("io.github.lumen.Recorder.QuitVerify")
state = {"stage": "ready", "deadline": time.monotonic()+45, "errors": []}


def snapshot(name):
    active = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"]))
    if active.get("pid") != os.getpid():
        return
    x, y = active["at"]
    width, height = active["size"]
    subprocess.run(["grim", "-g", f"{x},{y} {width}x{height}", str(root/name)], check=True)


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
        assert time.monotonic() < state["deadline"], "Recovery workflow timed out"
        if state["stage"] == "ready" and window.devices_ready:
            window.open_project(project)
            state["stage"] = "opening"
        elif state["stage"] == "opening" and window.project:
            layers = window.layers
            window.inspector.set_visible_child_name("layers")
            layers.kind.set_selected(2)
            if args.phase == "quit":
                window.project_name.set_text("The valid title survives a bad draft")
                window.trim_start.set_value(.25)
                window.trim_end.set_value(2.75)
                assert layers.click_duration.get_adjustment().get_upper() == 10
                # Raw typing can represent an unfinished value before GTK focus
                # handling. Preserve this exact form even when it cannot commit.
                layers.click_duration.set_text("24.14")
                assert layers.click_duration.get_text() == "24.14"
                state["ok"] = True
                app.request_quit()
                return False
            assert window.recovery_banner.get_revealed(), "Recovery banner was absent after relaunch"
            state["recovery"] = window.recovery_path
            state["stage"] = "banner"
            state["since"] = time.monotonic()
        elif state["stage"] == "banner" and time.monotonic()-state["since"] > .7:
            snapshot("recovery-banner.png")
            window.recovery_banner.emit("button-clicked")
            assert window.layers.click_duration.get_text() == "24.14"
            assert window.layers.current_kind == "clicks"
            assert window.layers.click_duration_field.get_visible()
            assert not window.layers.end_field.get_visible()
            assert window.project_name.get_text() == "The valid title survives a bad draft"
            state["stage"] = "restored"
            state["since"] = time.monotonic()
        elif state["stage"] == "restored" and time.monotonic()-state["since"] > .7:
            snapshot("restored-duration.png")
            window.layers.click_duration.set_value(.8)
            window.layers.apply()
            assert not state["errors"]
            saved = load_project(project["path"])
            assert abs(saved["clicks"][0]["duration"]-.8) < 1e-6
            assert saved["draft_recovery_handled_through"] == state["recovery"].name
            assert not window.recovery_banner.get_revealed()
            state["ok"] = True
            app.request_quit()
            return False
    except Exception as exc:
        state["errors"].append(str(exc))
        print(json.dumps({"ok": False, "phase": args.phase, "stage": state["stage"],
                          "errors": state["errors"], "root": str(root)}, indent=2), flush=True)
        app.quit()
        return False
    return True


GLib.timeout_add(150, tick)
app.run([sys.argv[0], "--studio"])
assert state.get("ok"), state["errors"]
saved = load_project(project["path"])
assert hashlib.sha256((Path(project["path"])/project["source"]).read_bytes()).hexdigest() == fixture["sha256"]
if args.phase == "quit":
    backups = list_recoveries(saved)
    assert len(backups) == 1, f"Quit/shutdown wrote duplicate backups: {backups}"
    recovered = read_recovery(backups[0], saved)
    assert recovered["draft"]["layer_form"]["fields"]["click_duration"] == "24.14"
    assert abs(saved["clicks"][0]["duration"]-.6) < 1e-6
    assert saved["name"] == "The valid title survives a bad draft"
    assert saved["edits"]["trim_start"] == .25 and saved["edits"]["trim_end"] == 2.75
print(json.dumps({"phase": args.phase, "ok": True, "root": str(root)}), flush=True)
