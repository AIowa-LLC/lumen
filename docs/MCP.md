# Agent control with MCP

Lumen includes a local **Model Context Protocol server**. Connect a client that
supports stdio MCP servers and let its agent record, inspect, edit, and export
through Lumen's native app. Lumen does not require an API key or embed a model.
The connected client supplies the agent.

## Setup

Run inside your Omarchy desktop session, from the checkout:

```sh
/usr/bin/python scripts/setup-mcp.py
./scripts/install.sh
```

Setup installs the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)
into a private, ignored `.mcp` virtual environment. It uses system Python when
that interpreter includes `ensurepip`, otherwise an already-installed uv-managed
Python. It does not download Python. If neither is available, provide a Python
3.11+ interpreter with venv/ensurepip:

```sh
/usr/bin/python scripts/setup-mcp.py --python /absolute/path/to/python3.11
```

The installer copies the configured runtime into the installed app. Alternatively,
install `lumen-recorder[mcp]` in your own virtual environment. GTK and capture still
use the system interpreter and the dependencies listed in the README.

Add this entry to your client's MCP configuration, replacing the command with
your actual absolute launcher path:

```json
{
  "mcpServers": {
    "lumen": {
      "command": "/home/YOUR_USER/.local/bin/lumen",
      "args": ["--mcp"]
    }
  }
}
```

For source development, use the absolute path to `scripts/lumen`. For a custom
library, add `"--library", "/absolute/path/to/library"` to `args`. The library
must match the already-running app. Close an idle app before switching libraries.
The client must inherit the desktop's Wayland/D-Bus session environment; a remote
shell without that session cannot reach the native app.

The transport is stdio only; no network listener is started. SDK dependencies are
optional, so the normal GTK application works without them.

## Tools and workflow

Start with `lumen_status` and `lumen_list_sources`. Discovery runs asynchronously;
wait for `ready: true`. Call sources with `refresh: true` after windows or devices
change, then poll without refresh. Window entries include fixed screen geometries.
Keep the selected window visible and stationary throughout capture.

| Tools | Purpose |
| --- | --- |
| `lumen_status`, `lumen_list_sources` | Inspect shared app state and capture inputs |
| `lumen_list_projects`, `lumen_get_project`, `lumen_open_project` | Find, read, and open takes in Studio |
| `lumen_import_video` | Copy a local video into a new take |
| `lumen_start_recording`, `lumen_stop_recording`, `lumen_set_paused` | Record, finalize, and explicitly pause/resume |
| `lumen_start_replay`, `lumen_save_replay` | Buffer and save recent footage; stop with `lumen_stop_recording` |
| `lumen_update_edits`, `lumen_set_layers` | Change the saved recipe and timed layers |
| `lumen_list_wallpapers`, `lumen_add_wallpaper` | Choose a built-in or import a custom backdrop |
| `lumen_get_frame` | Return an original recording frame as an MCP image |
| `lumen_render_preview`, `lumen_export` | Render a draft or final MP4/GIF |
| `lumen_get_job`, `lumen_cancel_job` | Poll operation results and cancel renders |

Startup, finalization, import, wallpaper import, replay save, and rendering return
job objects. Poll `lumen_get_job` until `state` is `succeeded`, `failed`, or
`cancelled`. Only a successful result provides completed media or an export path.
Jobs belong to the native app session and survive client disconnects. The app
retains at most 100 jobs, removing completed entries first; IDs expire when the
app exits. Opening a project returns `opening`; wait until status identifies that
project and `opening_project_id` is `null` before making changes in its editor.

Recording defaults to **no audio**, with live inputs enabled. Region/window modes
require explicit geometry; they do not open an interactive region picker. Controls
are hidden by default during agent capture. Launch Lumen again to show the HUD and
stop recording yourself. A client disconnect does not stop an active recording.

Edits are sparse patches. Use Studio's output widths (1920/1280/960 or `null` for
original), speeds (0.5/0.75/1/1.5/2/3), and padding up to 300 pixels. For example,
call `lumen_update_edits` with:

```json
{
  "project_id": "ID_FROM_LIST_PROJECTS",
  "changes": {
    "trim_start": 0.5,
    "trim_end": 8,
    "speed": 1.5,
    "output_width": 1280,
    "padding": 80,
    "background": "wallpaper",
    "wallpaper": "builtin:aurora"
  }
}
```

Caption and annotation times use **original source seconds**, before trim/speed.
Coordinates use normalized 0–1 source positions. Layers append by default;
`append: false` explicitly replaces that layer kind. See
[layer formats](CAPTIONS.md) and each tool's input schema. Invalid edits or pending
invalid native form fields reject the command without overwriting the saved recipe.
Wait for an open take's render/transcription before changing that take.

Render a preview before final export. Exports use fresh filenames inside the
take's `exports` folder; previews live in `previews`. Originals and existing output
files are preserved. Frame inspection creates a project-local JPEG.

Resources expose JSON at `lumen://status` and `lumen://projects/{project_id}`. The
`make_demo` prompt provides a recording-to-export workflow for a supplied goal.

## Access and verification

All clients reach the same single-instance GTK app through same-user GApplication
IPC. The HUD and Studio remain the owners of capture, progress, and cancellation.
There are no arbitrary shell-execution or project-deletion tools. Imports require
explicit local paths; project IDs and output paths stay within the selected library.

Only connect clients you trust with your recordings and desktop access. Captured
screen/audio, window titles, project metadata, and requested frame images are
available to that client. Lumen processes locally; the client's model/provider may
receive the tool results or images according to its settings.

Run SDK protocol tests with `.mcp/venv/bin/python -m unittest discover -s tests -p
test_mcp.py -v`. The opt-in `scripts/verify_mcp.py` desktop check creates synthetic
footage and an owned test window. It verifies stdio discovery, recording,
pause/resume, replay, custom wallpapers, captions/annotations, images, preview,
export, cancellation, source preservation, and reconnection with a legacy client.
It uses no physical microphone/camera, desktop audio, or pointer telemetry.
