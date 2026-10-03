#!/usr/bin/python
"""Native replay proof using generated pixels and a private silent audio track.

Only the interior of an owned solid-color window is captured. CameraBubble's
source is replaced with lavfi; no physical video or audio device is opened.
"""
# ruff: noqa: E402
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lumen.camera import CameraBubble, bubble_geometry
from lumen.capture import CaptureOptions
from lumen.replay import ReplayBuffer
from lumen.system import get_monitors


def main():
    artifacts = Path(tempfile.mkdtemp(prefix="lumen-replay-inputs-"))
    monitor = next(m for m in get_monitors() if m.get("focused"))
    background = CameraBubble((monitor["x"], monitor["y"],
                               monitor["logical_width"], monitor["logical_height"]), "/dev/video0")
    background._source = lambda device: ("av://lavfi:color=c=navy:s=640x360:r=30", {})
    replay = ReplayBuffer(root=artifacts / "library")
    microphone = camera = None
    result = {}
    try:
        background.start()
        # Exclude rounded corners so every captured pixel is our generated window.
        x, y, width, height = bubble_geometry(background.bounds)
        bounds = x + 20, y + 20, width - 40, height - 40
        options = CaptureOptions(mode="region", geometry=f"{bounds[0]},{bounds[1]} {bounds[2]}x{bounds[3]}",
                                 monitor=monitor["name"], audio="none", cursor=False,
                                 live_inputs=True, record_clicks=False, cursor_telemetry=False)
        with patch.object(CameraBubble, "_source", return_value=("av://lavfi:color=c=lime:s=640x360:r=30", {})):
            replay.start(options, seconds=3)
            microphone = replay._live.microphone
            camera = replay._live.camera
            assert replay.live_inputs_available and not replay.mic_enabled and not replay.camera_enabled
            time.sleep(.7)
            replay.set_webcam_enabled(True, "/dev/video0")
            assert replay.camera_enabled and not replay.mic_enabled
            time.sleep(1.3)
            saved = replay.save()
            assert replay.is_running and replay.camera_enabled, "Saving must keep the live buffer intact"
            assert saved["audio_tracks"] == {"mic": 0}
            source = saved["source_path"]
            raw = subprocess.check_output(["ffmpeg", "-v", "error", "-ss", str(max(0, saved["duration"] - .25)),
                                           "-i", source, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
            camera_x, camera_y, camera_w, camera_h = bubble_geometry(bounds)
            px = round((camera_x - bounds[0] + camera_w / 2) / bounds[2] * saved["width"])
            py = round((camera_y - bounds[1] + camera_h / 2) / bounds[3] * saved["height"])
            offset = (py * saved["width"] + px) * 3
            rgb = list(raw[offset:offset + 3])
            assert len(rgb) == 3 and rgb[1] > 190 and rgb[0] < 45 and rgb[2] < 45, rgb
            audio = subprocess.check_output(["ffmpeg", "-v", "error", "-i", source,
                                             "-map", "0:a:0", "-f", "f32le", "-"])
            peak = max((abs(sample[0]) for sample in struct.iter_unpack("<f", audio)), default=0)
            assert audio and peak < .0001, peak
            replay.set_webcam_enabled(False)
            assert not replay.camera_enabled and camera.process.poll() is not None
            result = {"project": saved["path"], "duration": saved["duration"],
                      "video_size": [saved["width"], saved["height"]],
                      "camera_center_rgb": rgb, "silent_track_peak": peak,
                      "saved_while_buffering": True, "camera_off_exits_process": True}
    finally:
        replay.stop()
        background.stop()
    assert replay.process is None or replay.process.poll() is not None
    assert not microphone or microphone.process.poll() is not None
    assert not camera or camera.process.poll() is not None
    modules = json.loads(subprocess.check_output(["pactl", "-f", "json", "list", "modules"]))
    if microphone:
        sink = microphone.source_name.removesuffix(".monitor")
        assert not any(sink in module.get("argument", "") for module in modules)
    for owned in (background, camera):
        if owned:
            assert owned._hypr(f'/repl return _G["_lumen_camera_{owned.token}"] == nil') == "true"
    result["owned_resources_cleaned"] = True
    (artifacts / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"artifacts": str(artifacts), **result}, indent=2))


if __name__ == "__main__":
    main()
