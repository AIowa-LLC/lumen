# Agent control with MCP

[README](../README.md) · [User guide](USAGE.md) · [Coding agent guide](../AGENTS.md)

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
After updating, quit an existing idle Lumen instance and reopen it to load the new
application code.

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

The current surface has **20 tools**, **two resource endpoints** (one fixed
resource and one project resource template), and **one prompt**. The source of
truth for names and argument schemas is [`lumen/mcp_server.py`](../lumen/mcp_server.py);
[`lumen/agent.py`](../lumen/agent.py) implements native operations and job results.
Use your client's tool discovery to inspect the installed version before calling
it; repository documentation alone does not verify which version is running.

All clients and the human-facing HUD/Studio share the same app state. A connection
has no private recording ownership. Inspect status before capture, pause, stop,
opening a take, or editing shared work. Do not stop an existing recording/replay
or cancel a render unless it belongs to the requested workflow or the user has
explicitly authorized that action.

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
job objects. Pause/resume also returns a job when it changes state; if already in
the requested state, it returns `{"paused": true}` or `{"paused": false}` directly. Poll `lumen_get_job` until `state` is `succeeded`, `failed`, or
`cancelled`. Only a successful result provides completed media or an export path.
Jobs belong to the native app session and survive client disconnects. The app
retains at most 100 jobs, removing completed entries first; completed job IDs may be evicted at that limit, and all IDs expire when the
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

## Worked example: record, inspect, edit, and export

The JSON below is an MCP tool-call name and its arguments, not a shell command.
Send each call through your client, inspect its structured result, and wait at
all indicated steps. Replace uppercase placeholders with values from earlier
results. Do not send this sequence blindly against an occupied app.

1. **Inspect the shared session and discover sources.**

   ```json
   {"name": "lumen_status", "arguments": {}}
   ```

   ```json
   {"name": "lumen_list_sources", "arguments": {"refresh": true}}
   ```

   If discovery returns `ready: false`, poll `lumen_list_sources` with `{}` until
   `ready: true`. Confirm the requested window in `windows` and copy its exact
   `geometry` string (for example, `100,80 1280x720`). Check `recording.active`,
   `replay.active`, `capture_busy`, and the active `jobs` in status. If another
   workflow is active, coordinate with its owner before stopping or changing it.

2. **Start a silent window recording with explicit capture boundaries.**

   ```json
   {
     "name": "lumen_start_recording",
     "arguments": {
       "options": {
         "mode": "window",
         "geometry": "GEOMETRY_FROM_SELECTED_WINDOW",
         "fps": 30,
         "audio": "none",
         "webcam": null,
         "cursor": true,
         "cursor_telemetry": false,
         "record_clicks": false
       },
       "hide_controls": true
     }
   }
   ```

   The response is a startup job. Save its `id`, then poll:

   ```json
   {"name": "lumen_get_job", "arguments": {"job_id": "START_JOB_ID"}}
   ```

   While `state` is `running`, wait briefly and poll the same ID again. Continue
   only after `succeeded`; if `failed` or `cancelled`, inspect `error` and stop this
   sequence. A job ID alone does not mean recording started. Confirm
   `recording.active` through `lumen_status`, then perform the intended demo.
   For the edit times below, record at least five seconds of source footage.

3. **Optionally pause or resume.**

   ```json
   {"name": "lumen_set_paused", "arguments": {"paused": true}}
   ```

   If the response contains a job `id`, poll it to success before continuing.
   Resume with the same tool and `{"paused": false}`, again waiting for its job.
   If already in the requested state, the tool returns `{"paused": true}` or
   `{"paused": false}` immediately. Replay buffers cannot pause.

4. **Stop this recording and wait for finalization.** Recheck status before
   stopping if another person or client may have changed the session.

   ```json
   {"name": "lumen_stop_recording", "arguments": {}}
   ```

   ```json
   {"name": "lumen_get_job", "arguments": {"job_id": "STOP_JOB_ID"}}
   ```

   Poll to `succeeded`. Use the finalized take's `result.id` as `PROJECT_ID` below.
   Read the project to confirm its actual duration before choosing edit times:

   ```json
   {"name": "lumen_get_project", "arguments": {"project_id": "PROJECT_ID"}}
   ```

   To work on an existing take instead, call `lumen_list_projects` with
   `{"limit": 20, "offset": 0}` and use an ID from `projects`. To import a local
   video, call `lumen_import_video` with
   `{"source": "/absolute/path/to/demo.mp4", "name": "Demo"}` and poll its job;
   the new ID is also in `result.id`. Use IDs, not library paths, for `project_id`.

5. **Inspect an original frame and optionally open Studio.** The following time
   must be inside the actual source duration:

   ```json
   {"name": "lumen_get_frame", "arguments": {"project_id": "PROJECT_ID", "seconds": 1.0, "width": 960}}
   ```

   This normally returns an MCP image after internally waiting for extraction.
   It shows the original source, not the edited preview. To open the take visibly:

   ```json
   {"name": "lumen_open_project", "arguments": {"project_id": "PROJECT_ID"}}
   ```

   This returns `{"opening": "PROJECT_ID"}`, not a job. Poll `lumen_status` until
   `project.id` matches and `opening_project_id` is `null` before editing.

6. **Apply a sparse edit patch and append timed layers.** These sample times assume
   at least five seconds of source footage; shorten them for a shorter take.

   ```json
   {
     "name": "lumen_update_edits",
     "arguments": {
       "project_id": "PROJECT_ID",
       "changes": {
         "trim_start": 0.5,
         "trim_end": 4.5,
         "speed": 1.0,
         "output_width": 1280,
         "padding": 80,
         "background": "wallpaper",
         "wallpaper": "builtin:aurora",
         "format": "mp4",
         "captions": true,
         "annotations": true
       }
     }
   }
   ```

   ```json
   {
     "name": "lumen_set_layers",
     "arguments": {
       "project_id": "PROJECT_ID",
       "kind": "captions",
       "append": true,
       "items": [{"start": 0.5, "end": 2.5, "text": "Open the demo"}]
     }
   }
   ```

   ```json
   {
     "name": "lumen_set_layers",
     "arguments": {
       "project_id": "PROJECT_ID",
       "kind": "annotations",
       "append": true,
       "items": [{"kind": "box", "start": 1.0, "end": 3.0, "x": 0.2, "y": 0.2, "x2": 0.6, "y2": 0.6}]
     }
   }
   ```

   Inspect the saved recipe/layers with `lumen_get_project`. Repeating an append
   can duplicate layers; do not retry a write with an uncertain result blindly.

7. **Render and review the edited preview.**

   ```json
   {"name": "lumen_render_preview", "arguments": {"project_id": "PROJECT_ID"}}
   ```

   ```json
   {"name": "lumen_get_job", "arguments": {"job_id": "PREVIEW_JOB_ID"}}
   ```

   Wait for `succeeded` and review the file at `result.path` (or `result.uri`).
   Studio shows the draft if this take is open. The preview uses up to 960px width
   at 30 fps. After changing edits, render another preview.

8. **Export and report the completed output.** Omitting `filename` lets Lumen
   generate a fresh name with the selected format's extension.

   ```json
   {"name": "lumen_export", "arguments": {"project_id": "PROJECT_ID"}}
   ```

   ```json
   {"name": "lumen_get_job", "arguments": {"job_id": "EXPORT_JOB_ID"}}
   ```

   Report `result.path`/`result.uri` only after `state: "succeeded"`. A custom
   `filename` must be a new basename such as `demo.mp4`, match the saved format,
   and contain no directory components. To cancel a running preview/export, call
   `lumen_cancel_job` with `{"job_id": "RENDER_JOB_ID"}` and poll until terminal;
   a cancellation request is not itself a completed cancellation.

### Replay: save before stopping

Start with `lumen_start_replay`, using the same explicit `options` structure and
`seconds: 15`, `30`, or `60`. Poll the startup job. Replay requires GPU Screen
Recorder and cannot run beside a normal recording. When the wanted moment has
occurred, call `lumen_save_replay` with `{}` and poll its job to `succeeded`; its
`result.id` identifies the saved take. Buffering continues after saving.

Only then, when authorized to end that shared buffer, call
`lumen_stop_recording` with `{}` and poll its finalization job. For replay this
returns `result.buffer_stopped: true` and **discards unsaved replay history**.
It does not save a final clip. Do not use stop as a generic cleanup step.

## Troubleshooting

| Symptom | What to check or do |
| --- | --- |
| Optional SDK is missing or cannot import | Run `scripts/setup-mcp.py` from the current checkout, using `--python` with a Python 3.11+ interpreter that includes venv/ensurepip if needed. Re-run `scripts/install.sh` for the installed app after setup. Confirm the client's absolute launcher path points to that installation. |
| Cannot start/reach the native app, or no agent response | Run inside the actual Omarchy Wayland/D-Bus session and check README system dependencies with `./scripts/lumen --diagnostics`. A working SDK alone does not supply GTK or the desktop session. After an update, close and reopen an idle Lumen app. |
| The running app uses a different library | Set the client's `--library`/`LUMEN_LIBRARY` to the library used to launch the existing app. A mismatch blocks all tool calls, including `lumen_status`; confirm the active library with its owner, or coordinate closing the idle app before switching. Do not stop active work just to change libraries. |
| Device discovery is still running | Poll `lumen_list_sources` without refresh until `ready: true`. Refresh once when windows/devices change; repeatedly refreshing restarts discovery. |
| Capture transition, recording, replay, render, or transcription is busy | Inspect `lumen_status` and poll known active jobs. Wait for startup/pause/resume/save/finalization or project opening before dependent calls. Coordinate with the owner of existing work instead of stopping it automatically. For open-take render/transcription, wait or use the appropriate authorized native controls. |
| Invalid edits or pending native form fields | Correct the reported field in Studio or the tool arguments. Invalid pending fields are preserved, not silently overwritten. Use supported sizes/speeds, source-time ranges within the take, and normalized layer coordinates. |
| Request exceeds 64 KiB | The encoded native request, including its envelope and library path, must fit in 64 KiB. Split large `items` lists into smaller `lumen_set_layers` calls. Append subsequent batches with `append: true`; if intentionally replacing a kind, use `append: false` only on the first batch. There is no fixed safe item count because text sizes vary. |
| Unknown or expired job ID | Jobs belong to the native app session; completed entries can be evicted at the 100-job limit, and all IDs expire when the app exits. Inspect current status/projects and saved output before retrying. Do not assume failure or repeat a capture/import/export merely because its old job is missing. |
| Client disconnected or a response was lost | Reconnect to the same library, call `lumen_status`, and poll retained job IDs. Active recording and jobs survive a client disconnect. Review current state and saved project data before retrying a write; do not blindly start another recording or append duplicate layers. |
| Frame inspection says it is still running | The tool error includes the frame job ID. Poll `lumen_get_job` with that ID. On success, the frame job's `result` is the project-local JPEG path; it is not the export-style `{path, uri}` object. |
| Cancellation rejected | Only render jobs are cancellable. Recording/replay use `lumen_stop_recording`, with the replay data-loss warning above. Import, startup, pause/resume, replay save, and other non-cancellable jobs must finish. |

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
Run `.mcp/venv/bin/python scripts/verify_mcp_startup.py` to check startup with Lumen
closed and confirm that client disconnection leaves the isolated native app alive.
This check starts no capture and closes only its own test instance.
