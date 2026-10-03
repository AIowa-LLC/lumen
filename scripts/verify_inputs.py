#!/usr/bin/python
"""Opt-in native HUD/input proof using synthetic devices and an owned test window.

Never opens a physical microphone/camera or plays sound to hardware. A private
Pulse sink supplies a tone; the camera source is replaced with a lavfi color.
Only this process's static pattern window is recorded. Artifacts stay in /tmp.
"""
# ruff: noqa: E402

import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(tempfile.mkdtemp(prefix="lumen-inputs-ui-"))
os.environ["LUMEN_LIBRARY"] = str(root / "library")
os.environ["XDG_CONFIG_HOME"] = str(root / "config")

from lumen import ui
from lumen.camera import CameraBubble, bubble_geometry
from lumen.editor import probe
from lumen.system import get_monitors, get_windows, parse_geometry


sink = "lumen_input_test_" + uuid.uuid4().hex
source = sink + ".monitor"
app = ui.LumenApplication()
app.set_application_id("io.github.lumen.Recorder.SyntheticInputsVerify")
state = {
    "stage": "ready",
    "since": time.monotonic(),
    "deadline": time.monotonic() + 100,
    "errors": [],
    "marks": {},
}
fixture = None
module = None
tone = None
tone_log = None


def advance(stage):
    state.update(stage=stage, since=time.monotonic())
    print(stage, flush=True)


def owned_windows():
    clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
    owned = [client for client in clients if client.get("pid") == os.getpid()]
    pattern = next(
        (
            client
            for client in owned
            if client.get("title") == "Lumen synthetic input fixture"
        ),
        None,
    )
    hud = next(
        (client for client in owned if client.get("title") == "Lumen — Recorder"), None
    )
    return pattern, hud


def dispatch(dispatcher, address, **values):
    args = {"window": "address:" + address, **values}
    body = ", ".join(f"{key}={json.dumps(value)}" for key, value in args.items())
    subprocess.run(
        ["hyprctl", "eval", f"hl.dispatch(hl.dsp.window.{dispatcher}({{{body}}}))"],
        check=True,
        capture_output=True,
    )


def make_fixture():
    global fixture
    fixture = ui.Gtk.ApplicationWindow(
        application=app,
        title="Lumen synthetic input fixture",
        default_width=960,
        default_height=540,
        resizable=False,
    )
    fixture.set_size_request(960, 540)
    area = ui.Gtk.DrawingArea()

    def draw(_, context, width, height):
        context.set_source_rgb(0.065, 0.09, 0.16)
        context.paint()
        context.set_source_rgb(0.11, 0.16, 0.25)
        context.set_line_width(1)
        for x in range(0, width, 60):
            context.move_to(x, 0)
            context.line_to(x, height)
        for y in range(0, height, 60):
            context.move_to(0, y)
            context.line_to(width, y)
        context.stroke()
        context.set_source_rgb(0.75, 0.68, 0.95)
        context.set_font_size(28)
        context.move_to(48, 76)
        context.show_text("Lumen · synthetic input verification")
        context.set_font_size(16)
        context.move_to(48, 112)
        context.show_text("No physical microphone or camera is used.")

    area.set_draw_func(draw)
    fixture.set_child(area)
    fixture.present()
    app.hud.present()


def safe_options(original):
    options = original()
    pattern, hud = owned_windows()
    assert pattern and hud, "Owned pattern/HUD must both be visible"
    geometry = parse_geometry(options.geometry)
    assert geometry == (*pattern["at"], *pattern["size"]), (
        "Capture must match only the owned pattern window"
    )
    x, y, width, height = geometry
    hx, hy = hud["at"]
    hw, hh = hud["size"]
    assert x + width <= hx or hx + hw <= x or y + height <= hy or hy + hh <= y, (
        "HUD must be outside the captured region"
    )
    assert options.audio == "none" and options.mic_source == source
    assert options.webcam is None and options.live_inputs
    assert options.record_clicks is False and options.cursor_telemetry is False
    options.backend = "gpu-screen-recorder"
    state["bounds"] = geometry
    return options


def mark(name):
    state["marks"][name] = app.window.recorder.elapsed


def tick():
    if not app.window or not app.hud or not app.window.devices_ready:
        return True
    studio, hud = app.window, app.hud
    if not state.get("hooked"):
        studio.error = lambda exc: state["errors"].append(str(exc))
        state["hooked"] = True
    try:
        if state["errors"]:
            raise AssertionError(state["errors"])
        if time.monotonic() > state["deadline"]:
            raise AssertionError("Timed out at " + state["stage"])
        stage, age = state["stage"], time.monotonic() - state["since"]
        engine = studio.recorder
        if stage == "ready":
            make_fixture()
            advance("positioning")
        elif stage == "positioning" and age > 0.5:
            pattern, compact = owned_windows()
            assert pattern and compact
            monitor = next(m for m in get_monitors() if m["id"] == pattern["monitor"])
            assert (
                monitor["logical_width"] >= 1620 and monitor["logical_height"] >= 600
            ), "Synthetic capture needs a 1620×600 logical desktop"
            x, y = monitor["x"] + 20, monitor["y"] + 35
            for client, width, height, left in (
                (pattern, 960, 540, x),
                (compact, 540, 180, x + 1020),
            ):
                dispatch("float", client["address"], action="set")
                dispatch("resize", client["address"], x=width, y=height, relative=False)
                dispatch("move", client["address"], x=left, y=y, relative=False)
            advance("starting")
        elif stage == "starting" and age > 0.5:
            pattern, _ = owned_windows()
            target = next(
                window
                for window in get_windows()
                if window["address"] == pattern["address"]
            )
            studio.windows = [target]
            studio.window_picker.set_model(ui.Gtk.StringList.new([target["label"]]))
            studio.window_picker.set_selected(0)
            studio.mode.set_selected(2)
            studio.monitor.set_selected(
                next(
                    i
                    for i, item in enumerate(studio.monitors)
                    if item["id"] == pattern["monitor"]
                )
            )
            studio.audio.set_selected(0)
            studio.mic.set_selected(1)
            studio.webcam.set_selected(0)
            studio.delay.set_selected(0)
            studio.fps.set_selected(0)
            studio.cursor.set_active(False)
            studio.telemetry.set_active(False)
            studio.record_clicks.set_active(False)
            studio.hide_recording.set_active(False)
            original = studio.capture_options
            studio.capture_options = lambda: safe_options(original)
            hud.primary_action()
            advance("wait-recording")
        elif (
            stage == "wait-recording" and engine.is_running and not studio.capture_busy
        ):
            assert engine.live_inputs_available
            assert not engine.mic_enabled and not engine.camera_enabled
            state["owned_microphone_source"] = engine._live.microphone.source_name
            mark("initial_off")
            advance("initial-off")
        elif stage == "initial-off" and age > 1.1:
            hud.mic_action()
            advance("wait-mic-on")
        elif stage == "wait-mic-on" and not studio.capture_busy:
            assert engine.mic_enabled and studio.audio.get_selected() == 2
            assert hud.mic_label.get_text() == "Mic on"
            mark("mic_on")
            advance("mic-on")
        elif stage == "mic-on" and age > 1.1:
            hud.mic_action()
            advance("wait-mic-off")
        elif stage == "wait-mic-off" and not studio.capture_busy:
            assert not engine.mic_enabled and studio.audio.get_selected() == 0
            mark("mic_off")
            advance("mic-off")
        elif stage == "mic-off" and age > 1.1:
            hud.camera_action()
            advance("wait-camera-on")
        elif stage == "wait-camera-on" and not studio.capture_busy:
            assert engine.camera_enabled and studio.webcam.get_selected() == 1
            assert hud.camera_label.get_text() == "Camera on"
            mark("camera_on")
            advance("camera-on")
        elif stage == "camera-on" and age > 1.1:
            hud.camera_action()
            advance("wait-camera-off")
        elif stage == "wait-camera-off" and not studio.capture_busy:
            assert not engine.camera_enabled and studio.webcam.get_selected() == 0
            mark("camera_off")
            advance("camera-off")
        elif stage == "camera-off" and age > 1.1:
            hud.mic_action()
            advance("wait-second-mic")
        elif stage == "wait-second-mic" and not studio.capture_busy:
            assert engine.mic_enabled
            mark("second_mic_on")
            advance("second-mic")
        elif stage == "second-mic" and age > 1.1:
            hud.mic_action()
            advance("wait-final-off")
        elif stage == "wait-final-off" and not studio.capture_busy:
            assert not engine.mic_enabled
            mark("final_off")
            advance("final-off")
        elif stage == "final-off" and age > 1.1:
            hud.primary_action()
            advance("stopping")
        elif stage == "stopping" and not studio.capture_busy and not engine.is_running:
            state["project"] = engine.project
            state["completed"] = True
            app.quit()
            return False
    except Exception:
        state["failure"] = traceback.format_exc()
        print(state["failure"], file=sys.stderr, flush=True)
        app.quit()
        return False
    return True


def audio_measure(path, moment):
    data = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(moment + 0.35),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-t",
            "0.4",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "f32le",
            "-",
        ]
    )
    samples = struct.unpack("<" + "f" * (len(data) // 4), data)
    assert samples
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    real = sum(
        value * math.cos(2 * math.pi * 997 * i / 48000)
        for i, value in enumerate(samples)
    )
    imag = sum(
        value * math.sin(2 * math.pi * 997 * i / 48000)
        for i, value in enumerate(samples)
    )
    return {"rms": rms, "tone_997hz": math.hypot(real, imag) / len(samples)}


def video_pixel(path, media, moment):
    pixels = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-ss",
            str(moment + 0.35),
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    x, y, width, height = state["bounds"]
    bx, by, bw, bh = bubble_geometry(state["bounds"])
    px = round((bx + bw / 2 - x) * media["width"] / width)
    py = round((by + bh / 2 - y) * media["height"] / height)
    values = [
        pixels[(row * media["width"] + col) * 3 : (row * media["width"] + col) * 3 + 3]
        for row in range(py - 5, py + 5)
        for col in range(px - 5, px + 5)
    ]
    return [
        sum(value[channel] for value in values) / len(values) for channel in range(3)
    ]


try:
    defaults = {
        name: subprocess.check_output(
            ["pactl", "get-default-" + name], text=True
        ).strip()
        for name in ("sink", "source")
    }
    module = int(
        subprocess.check_output(
            [
                "pactl",
                "load-module",
                "module-null-sink",
                "sink_name=" + sink,
                "rate=48000",
                "channels=2",
                "sink_properties=device.description=LumenSyntheticMicrophone",
            ],
            text=True,
        ).strip()
    )
    tone_log = (root / "tone.log").open("wb")
    tone = subprocess.Popen(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-re",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000",
            "-ac",
            "2",
            "-f",
            "pulse",
            "-device",
            sink,
            "Lumen synthetic microphone",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=tone_log,
    )
    assert tone.poll() is None
    with (
        patch.object(
            ui,
            "get_audio_sources",
            return_value=[
                {
                    "name": source,
                    "description": "Synthetic tone only",
                    "is_monitor": False,
                }
            ],
        ),
        patch.object(
            ui,
            "get_cameras",
            return_value=[
                {
                    "id": "lumen-synthetic-camera",
                    "path": "/dev/video999999",
                    "label": "Synthetic green camera",
                }
            ],
        ),
        patch.object(
            CameraBubble,
            "_source",
            return_value=("av://lavfi:color=c=0x20e040:size=640x360:rate=30", {}),
        ),
    ):
        ui.GLib.timeout_add(80, tick)
        app.run(["lumen"])
    assert state.get("completed"), state.get("failure", state)
    project = state["project"]
    path = Path(project["path"]) / project["source"]
    (root / "capture.json").write_text(
        json.dumps(
            {"project": project, "marks": state["marks"], "bounds": state["bounds"]},
            indent=2,
        )
        + "\n"
    )
    media = probe(path)
    audio = {key: audio_measure(path, moment) for key, moment in state["marks"].items()}
    for key in ("initial_off", "mic_off", "camera_on", "camera_off", "final_off"):
        assert audio[key]["rms"] < 0.002, (key, audio)
    for key in ("mic_on", "second_mic_on"):
        assert audio[key]["rms"] > 0.015 and audio[key]["tone_997hz"] > 0.01, (
            key,
            audio,
        )
    video = {
        key: video_pixel(path, media, state["marks"][key])
        for key in ("initial_off", "camera_on", "camera_off")
    }
    assert (
        video["camera_on"][1] > 170
        and video["camera_on"][1] > video["initial_off"][1] + 90
    ), video
    assert abs(video["camera_off"][1] - video["initial_off"][1]) < 20, video
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], check=True
    )
    result = {
        "ok": True,
        "root": str(root),
        "source": str(path),
        "media": media,
        "marks": state["marks"],
        "audio": audio,
        "video_rgb": video,
        "physical_inputs_opened": False,
    }
finally:
    if app.window:
        app.window.shutdown_capture()
    if tone and tone.poll() is None:
        tone.terminate()
        try:
            tone.wait(timeout=4)
        except subprocess.TimeoutExpired:
            tone.kill()
            tone.wait(timeout=4)
    if tone_log:
        tone_log.close()
    if module is not None:
        subprocess.run(["pactl", "unload-module", str(module)], check=True)
    if "defaults" in globals():
        after = {
            name: subprocess.check_output(
                ["pactl", "get-default-" + name], text=True
            ).strip()
            for name in defaults
        }
        assert after == defaults, "Default devices changed during the synthetic check"
    remaining_sources = {
        item["name"]
        for item in json.loads(
            subprocess.check_output(["pactl", "-f", "json", "list", "sources"])
        )
    }
    assert source not in remaining_sources, "Synthetic tone sink leaked"
    assert state.get("owned_microphone_source") not in remaining_sources, (
        "Recording microphone route leaked"
    )

result["cleanup"] = {
    "audio_defaults_unchanged": True,
    "private_audio_sources_removed": True,
    "tone_process_stopped": tone.poll() is not None,
}
(root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2), flush=True)
