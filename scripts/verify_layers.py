#!/usr/bin/python
"""Opt-in native GTK authoring check using synthetic footage, not the desktop."""
# ruff: noqa: E402

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--speech-fixture",
    type=Path,
    help="Public speech media for an offline UI transcription check",
)
args = parser.parse_args()
root = Path(tempfile.mkdtemp(prefix="lumen-layer-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
from lumen.project import create_project, save_project, load_project
from lumen.ui import LumenApplication, GLib
from lumen.editor import probe
from lumen.captions import write_srt, load_captions

project = create_project()
project.update(
    name="A clearer story", duration=4, width=960, height=540, fps=30, status="ready"
)
source = Path(project["path"]) / "source.mkv"
subprocess.run(
    [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=0x172033:s=960x540:r=30:d=4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-vf",
        "drawbox=x=40:y=40:w=880:h=54:color=0x2b3854:t=fill,drawbox=x=40:y=120:w=260:h=380:color=0x26314a:t=fill,drawbox=x=325:y=120:w=595:h=380:color=0x202b40:t=fill,drawtext=text='Demo workspace':fontcolor=white:fontsize=28:x=65:y=52,drawtext=text='Your next great idea':fontcolor=0xbba5ff:fontsize=30:x=355:y=185",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
    ],
    check=True,
)
save_project(project)
digest = hashlib.sha256(source.read_bytes()).hexdigest()
app = LumenApplication()
app.set_application_id("io.github.lumen.Recorder.LayersVerify")
state = {
    "stage": "ready",
    "since": time.monotonic(),
    "deadline": time.monotonic() + 120,
    "errors": [],
}


def next_stage(name):
    state.update(stage=name, since=time.monotonic())
    print(name, flush=True)


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


def finish():
    print(json.dumps(state["result"], indent=2), flush=True)
    state["ok"] = True
    app.quit()
    return False


def tick():
    w = app.window
    if not w:
        return True
    if not state.get("hooked"):
        w.error = lambda exc: state["errors"].append(str(exc))
        state["hooked"] = True
    try:
        if state["errors"]:
            raise AssertionError(state["errors"])
        if time.monotonic() > state["deadline"]:
            raise AssertionError("Workflow timed out at " + state["stage"])
        stage = state["stage"]
        if stage == "ready" and w.devices_ready:
            w.open_project(project)
            next_stage("opening")
        elif stage == "opening" and w.project:
            p = w.layers
            w.inspector.set_visible_child_name("layers")
            p.add()
            p.text.get_buffer().set_text("Make your point — clearly.")
            p.start.set_value(0.15)
            p.end.set_value(3.8)
            p.apply()
            p.kind.set_selected(1)
            p.add()
            p.annotation_kind.set_selected(1)
            p.start.set_value(0.1)
            p.end.set_value(3.5)
            p.x.set_value(34)
            p.y.set_value(60)
            p.x2.set_value(65)
            p.y2.set_value(40)
            p.custom_color.set_text("#ffd166")
            p.apply()
            p.add()
            p.annotation_kind.set_selected(3)
            p.start.set_value(0.1)
            p.end.set_value(3.5)
            p.x.set_value(35)
            p.y.set_value(33)
            p.x2.set_value(89)
            p.y2.set_value(44)
            p.apply()
            p.kind.set_selected(2)
            p.add()
            p.start.set_value(0.6)
            p.end.set_value(1.8)
            p.x.set_value(69)
            p.y.set_value(42)
            p.size.set_value(5)
            p.apply()
            p.duplicate()
            assert len(p.items()) == 2
            p.delete()
            assert len(p.items()) == 1
            p.undo()
            assert len(p.items()) == 2
            p.redo()
            assert len(p.items()) == 1
            assert w.save_edits(False)
            reloaded = load_project(project["path"])
            assert [
                len(reloaded[k]) for k in ("captions", "annotations", "clicks")
            ] == [1, 2, 1]
            assert reloaded["annotations"][0]["color"] == "#ffd166"
            p.select_at("captions", reloaded["captions"][0]["id"], 0.6)
            w.background.set_selected(1)
            w.trim_start.set_value(0.25)
            w.trim_end.set_value(3.75)
            w.speed.set_selected(3)
            w.export_size.set_selected(2)
            w.render_preview()
            next_stage("preview")
        elif stage == "preview" and not w.export_busy and w.preview_options:
            next_stage("playing")
        elif stage == "playing" and time.monotonic() - state["since"] > 0.7:
            stream = w.video.get_media_stream()
            assert stream and stream.is_prepared() and not stream.get_error()
            stream.pause()
            snapshot("layers.png")
            options = w.edit_options()
            write_srt(
                w.project["captions"],
                root / "captions.srt",
                trim_start=options.trim_start,
                trim_end=options.trim_end,
                speed=options.speed,
            )
            cues = load_captions(root / "captions.srt")
            assert len(cues) == 1 and cues[0]["start"] == 0
            assert abs(cues[0]["end"] - 3.5 / 1.5) < 0.01
            w.render_export(options, root / "layers.mp4")
            next_stage("export")
        elif stage == "export" and not w.export_busy:
            media = probe(root / "layers.mp4")
            assert media["width"] == 960
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(root / "layers.mp4"),
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                capture_output=True,
            )
            assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
            state["result"] = {
                "ok": True,
                "root": str(root),
                "counts": {
                    k: len(w.project[k]) for k in ("captions", "annotations", "clicks")
                },
                "output": media,
            }
            if not args.speech_fixture:
                return finish()
            speech_project = create_project()
            speech_source = Path(speech_project["path"]) / "source.mkv"
            shutil.copyfile(args.speech_fixture, speech_source)
            speech_meta = probe(speech_source)
            speech_project.update(
                {k: speech_meta[k] for k in ("width", "height", "duration", "fps")}
            )
            speech_project.update(
                name="Offline captions check",
                status="ready",
                audio_tracks={"desktop": 0},
            )
            save_project(speech_project)
            state["speech_project"] = speech_project
            w.open_project(speech_project)
            next_stage("speech-opening")
        elif (
            stage == "speech-opening"
            and w.project["path"] == state["speech_project"]["path"]
        ):
            p = w.layers
            p.kind.set_selected(0)
            p.add()
            p.text.get_buffer().set_text("Original manual caption")
            p.apply()
            p.generate_captions()
            assert p.transcribing
            p.text.get_buffer().set_text("This edit survives transcription")
            next_stage("speech-running")
        elif stage == "speech-running" and not w.layers.transcribing:
            p = w.layers
            buf = p.text.get_buffer()
            assert (
                buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
                == "This edit survives transcription"
            )
            assert len(w.project["captions"]) >= 4, p.speech_status.get_text()
            assert (
                "country" in " ".join(c["text"] for c in w.project["captions"]).lower()
            )
            p.apply()
            persisted = load_project(w.project["path"])
            assert (
                persisted["captions"][0]["text"] == "This edit survives transcription"
            )
            state["result"]["automatic_captions"] = len(persisted["captions"]) - 1
            snapshot("automatic-captions.png")
            return finish()
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
