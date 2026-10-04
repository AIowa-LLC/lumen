"""Optional official-SDK stdio server; no desktop bindings in this process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .agent import MAX_REQUEST_BYTES
from .project import library_root


class CaptureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: Literal["monitor", "region", "window"] = "monitor"
    monitor: str = ""
    geometry: str | None = None
    fps: Annotated[int, Field(ge=1, le=240)] = 60
    audio: Literal["none", "desktop", "mic", "both"] = "none"
    mic_source: str | None = None
    webcam: str | None = None
    cursor: bool = True
    cursor_telemetry: bool = True
    record_clicks: bool = True
    live_inputs: bool = True
    codec: Literal["h264", "hevc", "av1"] = "h264"
    encoder: Literal["auto", "gpu", "cpu"] = "auto"
    quality: Literal["medium", "high", "very_high", "ultra"] = "very_high"
    backend: Literal["auto", "gpu-screen-recorder", "wf-recorder"] = "auto"
    resolution: str | None = None


class EditPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    trim_start: float | None = None
    trim_end: float | None = None
    speed: Literal[0.5, 0.75, 1.0, 1.5, 2.0, 3.0] | None = None
    background: Literal["midnight", "violet", "sand", "none", "wallpaper"] | None = None
    wallpaper: str | None = None
    padding: Annotated[int, Field(ge=0, le=300)] | None = None
    output_width: Literal[1920, 1280, 960] | None = None
    zoom: Annotated[float, Field(ge=1, le=4)] | None = None
    zoom_x: Annotated[float, Field(ge=0, le=1)] | None = None
    zoom_y: Annotated[float, Field(ge=0, le=1)] | None = None
    zoom_start: float | None = None
    zoom_end: float | None = None
    audio_mode: Literal["mix", "desktop", "mic", "none"] | None = None
    volume: Annotated[float, Field(ge=0, le=4)] | None = None
    format: Literal["mp4", "gif"] | None = None
    captions: bool | None = None
    annotations: bool | None = None
    clicks: bool | None = None
    click_zoom: bool | None = None
    click_zoom_amount: Annotated[float, Field(ge=1, le=4)] | None = None
    click_zoom_hold: Annotated[float, Field(ge=.1, le=10)] | None = None
    click_zoom_transition: Annotated[float, Field(ge=.05, le=2)] | None = None


class NativeBridge:
    def __init__(self):
        self.start_lock = asyncio.Lock()
        self.native_host = None

    async def ensure_app(self, env):
        """Start/confirm the GTK primary without tying its life to an RPC pipe."""
        async with self.start_lock:
            with tempfile.TemporaryFile() as ready:
                process = subprocess.Popen(
                    ["/usr/bin/python", "-P", "-m", "lumen", "--agent-host"],
                    stdin=subprocess.DEVNULL, stdout=ready, stderr=subprocess.DEVNULL,
                    env=env, start_new_session=True,
                )
                deadline = asyncio.get_running_loop().time() + 20
                while True:
                    ready.seek(0)
                    line = ready.readline()
                    if line.endswith(b"\n"):
                        response = json.loads(line)
                        if response.get("primary"):
                            self.native_host = process
                        else:
                            while process.poll() is None:
                                if asyncio.get_running_loop().time() > deadline:
                                    process.kill()
                                    process.wait()
                                    raise ValueError("Lumen's native launch did not finish.")
                                await asyncio.sleep(.02)
                        return
                    if process.poll() is not None:
                        raise ValueError("Cannot start Lumen. Check the desktop session/GTK dependencies; after an update, close and reopen an idle Lumen instance.")
                    if asyncio.get_running_loop().time() > deadline:
                        process.kill()
                        process.wait()
                        raise ValueError("Lumen's native launch timed out.")
                    await asyncio.sleep(.02)

    async def call(self, operation, **arguments):
        payload = json.dumps({"operation": operation, "arguments": arguments, "library": str(library_root())}, allow_nan=False)
        if len(payload.encode()) > MAX_REQUEST_BYTES:
            raise ValueError("Request exceeds 64 KiB. Add layers in smaller batches.")
        env = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = source + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        try:
            await self.ensure_app(env)
            process = await asyncio.create_subprocess_exec(
                "/usr/bin/python", "-P", "-m", "lumen", "--agent", payload,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
        except (OSError, TimeoutError) as exc:
            raise ValueError("Cannot reach the native Lumen app. Run this server in your Omarchy desktop session.") from exc
        try:
            response = json.loads(stdout)
        except (ValueError, TypeError) as exc:
            raise ValueError("Lumen did not return an agent response. Check the desktop session and installed GTK dependencies.") from exc
        if process.returncode or "error" in response:
            raise ValueError(response.get("error", "Native Lumen command failed."))
        return response["result"]


def create_server(bridge=None):
    bridge = bridge or NativeBridge()
    mcp = MCPServer(
        "Lumen", title="Lumen Recording Studio", version=__version__,
        website_url="https://github.com/AIowa-LLC/lumen", log_level="WARNING",
        instructions="Control the local Lumen app. Start with lumen_status and lumen_list_sources. Capture opens local devices/screen; follow the user's requested source and audio. Long operations return app-session job IDs: poll lumen_get_job and check state/error before using the result. Original media is preserved. Use source-time seconds and normalized 0–1 coordinates for layers. Preview before final export.",
    )
    async def call(operation, **arguments):
        try:
            return await bridge.call(operation, **arguments)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    read = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_status() -> dict[str, Any]:
        """Inspect the shared native app, active recording/replay, open project, and jobs."""
        return await call("status")

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_list_sources(refresh: bool = False) -> dict[str, Any]:
        """List display names, visible window geometries, microphones, and cameras. Refresh after windows/devices change, then poll without refresh until ready."""
        return await call("sources", refresh=refresh)

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_list_projects(limit: Annotated[int, Field(ge=1, le=100)] = 50, offset: Annotated[int, Field(ge=0)] = 0) -> dict[str, Any]:
        """List recordings in the app's library, newest first. Use their IDs in other tools."""
        return await call("projects", limit=limit, offset=offset)

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_get_project(project_id: str) -> dict[str, Any]:
        """Read the take's media metadata, full saved recipe, wallpaper gallery, and timed layers."""
        return await call("project", project_id=project_id)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_open_project(project_id: str) -> dict[str, Any]:
        """Open a saved take in the visible native Studio. Opening is asynchronous."""
        return await call("open_project", project_id=project_id)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_import_video(source: str, name: str | None = None) -> dict[str, Any]:
        """Copy a local video into a new take. Returns an import job; its result contains the new project ID."""
        return await call("import_video", source=source, name=name)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False), structured_output=True)
    async def lumen_update_edits(project_id: str, changes: EditPatch, name: str | None = None) -> dict[str, Any]:
        """Patch a saved recipe: trim, speed, frame, wallpaper, zoom, audio, or layer visibility. Unspecified fields persist."""
        return await call("update_edits", project_id=project_id, changes=changes.model_dump(exclude_unset=True), name=name)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False), structured_output=True)
    async def lumen_set_layers(project_id: str, kind: Literal["captions", "annotations", "clicks"], items: list[dict], append: bool = True, caption_style: dict | None = None) -> dict[str, Any]:
        """Append timed layers (default), or explicitly replace a kind with append=false. IDs are generated if absent. Captions: start/end/text. Annotations: kind/start/end/x/y/x2/y2/text/color/size. Clicks: t/x/y/button/duration/size/color. Times are source seconds; positions and sizes are fractions. Optional enabled flags hide items."""
        return await call("set_layers", project_id=project_id, kind=kind, items=items, append=append, caption_style=caption_style)

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_list_wallpapers(project_id: str | None = None) -> dict[str, Any]:
        """List built-in IDs and, optionally, the take's custom wallpaper paths for the wallpaper recipe field."""
        return await call("wallpapers", project_id=project_id)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_add_wallpaper(project_id: str, source: str) -> dict[str, Any]:
        """Copy a local PNG/JPEG/WebP into the take. The job result path is the wallpaper recipe value."""
        return await call("add_wallpaper", project_id=project_id, source=source)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_start_recording(options: CaptureSettings | None = None, hide_controls: bool = True) -> dict[str, Any]:
        """Start a screen recording through the shared HUD controller. Audio defaults to none. Region/window modes require explicit geometry from sources. Returns a startup job; stop with lumen_stop_recording."""
        return await call("start_recording", options=options.model_dump() if options else {}, hide_controls=hide_controls)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False), structured_output=True)
    async def lumen_stop_recording() -> dict[str, Any]:
        """Stop and save the active recording, or stop/discard an unsaved replay buffer. Returns a finalization job."""
        return await call("stop_recording")

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_set_paused(paused: bool) -> dict[str, Any]:
        """Explicitly pause or resume the shared recording. Replay buffers cannot pause."""
        return await call("set_paused", paused=paused)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_start_replay(options: CaptureSettings | None = None, seconds: Literal[15, 30, 60] = 30, hide_controls: bool = True) -> dict[str, Any]:
        """Start a GPU replay buffer with explicit capture/audio settings; returns a startup job."""
        return await call("start_replay", options=options.model_dump() if options else {}, seconds=seconds, hide_controls=hide_controls)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_save_replay() -> dict[str, Any]:
        """Save the recent replay as a new take while buffering continues. Returns a save job."""
        return await call("save_replay")

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_render_preview(project_id: str) -> dict[str, Any]:
        """Render the recipe to a 960px/30fps draft. Returns a cancellable job; shows the draft if its take is open in Studio."""
        return await call("render", project_id=project_id, preview=True)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_export(project_id: str, filename: str | None = None) -> dict[str, Any]:
        """Render MP4/GIF under the take's exports folder without replacing existing files or source. Returns a cancellable job with path/URI when complete."""
        return await call("render", project_id=project_id, filename=filename)

    @mcp.tool(annotations=read, structured_output=True)
    async def lumen_get_job(job_id: str) -> dict[str, Any]:
        """Poll an app-session job: running/succeeded/failed/cancelled, progress, result, and error."""
        return await call("get_job", job_id=job_id)

    @mcp.tool(annotations=write, structured_output=True)
    async def lumen_cancel_job(job_id: str) -> dict[str, Any]:
        """Cancel an in-progress render. Partial outputs are not published. Capture must be stopped explicitly."""
        return await call("cancel_job", job_id=job_id)

    @mcp.tool(annotations=write)
    async def lumen_get_frame(project_id: str, seconds: float = 0, width: Annotated[int, Field(ge=128, le=1920)] = 960) -> Image:
        """Inspect an original recording frame as an image at source-time seconds; creates a project-local preview JPEG."""
        job = await call("frame", project_id=project_id, seconds=seconds, width=width)
        for _ in range(120):
            if job["state"] != "running":
                if job["state"] != "succeeded":
                    raise ToolError(job.get("error", "Frame inspection failed."))
                return Image(data=Path(job["result"]).read_bytes(), format="jpeg")
            await asyncio.sleep(.25)
            job = await call("get_job", job_id=job["id"])
        raise ToolError("Frame inspection is still running. Poll lumen_get_job using " + job["id"])

    @mcp.resource("lumen://status", mime_type="application/json")
    async def status_resource() -> str:
        return json.dumps(await call("status"))

    @mcp.resource("lumen://projects/{project_id}", mime_type="application/json")
    async def project_resource(project_id: str) -> str:
        return json.dumps(await call("project", project_id=project_id))

    @mcp.prompt()
    async def make_demo(goal: str) -> str:
        """Plan and create a polished local screen-recording demo using Lumen tools."""
        return f"Create a Lumen demo for: {goal}\nInspect status and sources. Choose the requested source/audio, start recording, perform the demo, then stop and poll finalization for its project ID. Review frames, set trim/zoom/wallpaper, add source-time captions and annotations, render a preview and poll it, then export and wait for success. Report the final path. Follow the user's capture boundaries. Do not infer successful recording/export from a job ID alone."

    return mcp


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lumen MCP server (local stdio transport)")
    parser.add_argument("--library", help="Recording library; must match an already-running app")
    args = parser.parse_args(argv)
    if args.library:
        os.environ["LUMEN_LIBRARY"] = str(Path(args.library).expanduser().resolve())
    create_server().run(transport="stdio")
    return 0
