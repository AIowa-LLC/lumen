#!/usr/bin/python
"""Opt-in MCP/GTK proof using synthetic media and only an owned pattern window.

Requires the optional SDK runtime. No physical microphone/camera, desktop audio,
cursor telemetry, or mouse-button hooks are used. Artifacts stay in private /tmp.
"""
# ruff: noqa: E402

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import traceback

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))


async def client_check(root):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    launcher = os.environ.get("LUMEN_VERIFY_LAUNCHER", str(repo / "scripts/lumen"))
    params = StdioServerParameters(command=launcher, args=["--mcp"], cwd=str(repo), env=dict(os.environ))
    async with Client(params, read_timeout_seconds=35) as client:
        assert len((await client.list_tools()).tools) == 20

        async def call(tool, **args):
            result = await client.call_tool("lumen_" + tool, args)
            assert not result.is_error, result.content
            assert result.structured_content is not None, result
            return result.structured_content

        async def wait(job):
            deadline = time.monotonic() + 90
            while job["state"] == "running":
                assert time.monotonic() < deadline, job
                await asyncio.sleep(.15)
                job = await call("get_job", job_id=job["id"])
            assert job["state"] == "succeeded", job
            return job["result"]

        print("MCP handshake and 20 tools", flush=True)
        status = await call("status")
        assert status["library"] == str(root / "library")
        sources = await call("list_sources", refresh=True)
        while not sources["ready"]:
            await asyncio.sleep(.1)
            sources = await call("list_sources")
        window = next(w for w in sources["windows"] if w.get("title") == "Lumen MCP test pattern")
        assert window["pid"] == int(os.environ["LUMEN_VERIFY_OWNER"])
        options = {"mode": "window", "geometry": window["geometry"], "fps": 30,
                   "backend": "gpu-screen-recorder", "audio": "none", "webcam": None,
                   "cursor": False, "cursor_telemetry": False, "record_clicks": False}
        await wait(await call("start_recording", options=options))
        assert (await call("status"))["recording"]["active"]
        await asyncio.sleep(1)
        await wait(await call("set_paused", paused=True))
        assert (await call("status"))["recording"]["state"] == "paused"
        await wait(await call("set_paused", paused=False))
        await asyncio.sleep(.7)
        recorded = await wait(await call("stop_recording"))
        assert recorded["duration"] > 1
        assert not (await call("status"))["recording"]["active"]
        print("Native capture, pause/resume, finalization", flush=True)

        await wait(await call("start_replay", options=options, seconds=15))
        await asyncio.sleep(2.5)
        replay = await wait(await call("save_replay"))
        assert replay["duration"] > 0
        await wait(await call("stop_recording"))
        assert not (await call("status"))["replay"]["active"]
        print("Native replay save and stop", flush=True)

        imported = await wait(await call("import_video", source=str(root / "fixture.mkv"), name="Agent demo"))
        pid = imported["id"]
        project = await call("get_project", project_id=pid)
        original = Path(project["path"]) / project["source"]
        digest = hashlib.sha256(original.read_bytes()).digest()
        await call("open_project", project_id=pid)
        while True:
            status = await call("status")
            if status["project"] and status["project"]["id"] == pid and not status["opening_project_id"]:
                break
            await asyncio.sleep(.1)
        wallpaper = await wait(await call("add_wallpaper", project_id=pid, source=str(root / "wallpaper.png")))
        await call("update_edits", project_id=pid, changes={"trim_start": .2, "trim_end": 2.8,
                   "speed": 1.5, "output_width": 960, "padding": 60,
                   "background": "wallpaper", "wallpaper": wallpaper["path"]})
        await call("set_layers", project_id=pid, kind="captions", items=[{"start": .3, "end": 2.6, "text": "Lumen is agentic."}])
        await call("set_layers", project_id=pid, kind="annotations", items=[{"kind": "box", "start": .4, "end": 2, "x": .2, "y": .2, "x2": .6, "y2": .6}])
        frame = await client.call_tool("lumen_get_frame", {"project_id": pid, "seconds": 1, "width": 320})
        assert not frame.is_error and frame.content[0].type == "image", frame
        draft = await wait(await call("render_preview", project_id=pid))
        assert Path(draft["path"]).is_file()
        exported = await wait(await call("export", project_id=pid, filename="agent-demo.mp4"))
        assert Path(exported["path"]).is_file()
        assert hashlib.sha256(original.read_bytes()).digest() == digest
        invalid = await client.call_tool("lumen_export", {"project_id": pid, "filename": "../escape.mp4"})
        assert invalid.is_error and "filename" in invalid.content[0].text
        assert json.loads((await client.read_resource("lumen://projects/" + pid)).contents[0].text)["edits"]["speed"] == 1.5
        await call("update_edits", project_id=pid, changes={"speed": .5})
        cancelled = await call("render_preview", project_id=pid)
        await call("cancel_job", job_id=cancelled["id"])
        while cancelled["state"] == "running":
            await asyncio.sleep(.15)
            cancelled = await call("get_job", job_id=cancelled["id"])
        assert cancelled["state"] == "cancelled", cancelled
        await call("update_edits", project_id=pid, changes={"speed": 1.5})
        # Jobs and native app survive a client disconnect.
        persistent = await call("render_preview", project_id=pid)
    async with Client(params, read_timeout_seconds=35, mode="legacy") as client:
        result = await client.call_tool("lumen_get_job", {"job_id": persistent["id"]})
        assert not result.is_error and result.structured_content["state"] in {"running", "succeeded"}
        while result.structured_content["state"] == "running":
            await asyncio.sleep(.15)
            result = await client.call_tool("lumen_get_job", {"job_id": persistent["id"]})
        assert result.structured_content["state"] == "succeeded", result
    print(json.dumps({"ok": True, "root": str(root), "capture": recorded["id"],
                      "source_unchanged": True, "export": exported["path"],
                      "client_reconnected": True, "legacy_protocol": True}), flush=True)


if sys.argv[1:2] == ["--client"]:
    asyncio.run(client_check(Path(sys.argv[2])))
    raise SystemExit(0)

from lumen.editor import probe
from lumen.ui import GLib, Gtk, LumenApplication

root = Path(tempfile.mkdtemp(prefix="lumen-mcp-verify-"))
os.environ.update(LUMEN_LIBRARY=str(root / "library"), XDG_CONFIG_HOME=str(root / "config"),
                  LUMEN_APPLICATION_ID="io.github.lumen.Recorder.MCPVerify",
                  LUMEN_VERIFY_OWNER=str(os.getpid()))
runtime = json.loads((repo / ".mcp/runtime.json").read_text())["python"]
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3",
                "-c:v", "libx264", "-preset", "ultrafast", str(root / "fixture.mkv")], check=True, capture_output=True, timeout=30)
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=0x273a65:s=800x600",
                "-frames:v", "1", str(root / "wallpaper.png")], check=True, capture_output=True, timeout=30)
app = LumenApplication()
state = {"deadline": time.monotonic() + 180}


def tick():
    try:
        assert time.monotonic() < state["deadline"], "Native MCP verification timed out"
        if not app.window or not app.window.devices_ready:
            return True
        if "fixture" not in state:
            fixture = Gtk.ApplicationWindow(application=app, title="Lumen MCP test pattern", default_width=640, default_height=360, resizable=False)
            label = Gtk.Label(label="LUMEN\n\nMCP verification\n\nOnly this test window is recorded.")
            label.add_css_class("title-1")
            fixture.set_child(label)
            fixture.present()
            state["fixture"] = fixture
            return True
        if "client" not in state:
            clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"]))
            fixture = next((w for w in clients if w.get("pid") == os.getpid() and w.get("title") == "Lumen MCP test pattern"), None)
            if not fixture:
                return True
            if not fixture["floating"]:
                # Hyprland Lua dispatcher takes a table, not JSON.
                subprocess.run(["hyprctl", "eval", "hl.dispatch(hl.dsp.window.float({window=" + json.dumps("address:" + fixture["address"]) + "}))"], check=True, capture_output=True)
                return True
            state["log"] = (root / "client.log").open("w")
            state["client"] = subprocess.Popen([runtime, str(Path(__file__).resolve()), "--client", str(root)], stdout=state["log"], stderr=subprocess.STDOUT)
            return True
        if state["client"].poll() is None:
            return True
        state["log"].close()
        print((root / "client.log").read_text(), flush=True)
        assert state["client"].returncode == 0, "Client failed; see " + str(root)
        exports = list((root / "library").glob("*/exports/agent-demo.mp4"))
        assert len(exports) == 1
        media = probe(exports[0])
        assert media["width"] == 960 and abs(media["duration"] - 2.6 / 1.5) < .1, media
        state["ok"] = True
        app.quit()
        return False
    except Exception:
        traceback.print_exc()
        print("Artifacts: " + str(root), flush=True)
        child = state.get("client")
        if child and child.poll() is None:
            child.terminate()
        app.quit()
        return False


GLib.timeout_add(150, tick)
app.run([sys.argv[0], "--hud"])
raise SystemExit(0 if state.get("ok") else 1)
