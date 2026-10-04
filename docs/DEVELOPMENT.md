# Development and contributing

Lumen targets Omarchy's Hyprland/Wayland desktop. Read the [README](../README.md) for system dependencies and the [user guide](USAGE.md) for expected behavior before changing a workflow.

## Start with a focused change

- Work on a feature branch and submit a pull request; `main` requires passing CI. See [repository protection](REPOSITORY.md).
- Check [open issues](https://github.com/AIowa-LLC/lumen/issues) and [pull requests](https://github.com/AIowa-LLC/lumen/pulls) for related work.
- Keep documentation-only changes separate from application behavior changes.
- Describe the problem, the expected behavior, and the checks you actually ran. A focused test result is not a full hardware validation.
- Keep recordings, private logs, local speech runtimes, and generated test artifacts out of commits. Use synthetic content for shared examples.
- Preserve original media and document any new files, dependencies, settings, or capture boundaries.

For bug reports, include reproduction steps, GPU/driver and relevant display scale, capture mode, codec, frame rate, audio selection, and whether the native or compatibility backend ran. `./scripts/lumen --diagnostics` and a take's `capture.log` can help. Remove private paths, device details, window titles, or other sensitive content before posting them.

## Source map

| Area | Files |
| --- | --- |
| Application and native interface | `lumen/__main__.py`, `ui.py`, `hud.py`, `hud.css`, `style.css` |
| Desktop and device discovery | `lumen/system.py` |
| Recording and replay | `lumen/capture.py`, `replay.py` |
| Live inputs | `lumen/camera.py`, `microphone.py`, `microphone_worker.py` |
| Project storage and recovery | `lumen/project.py`, `recovery.py` |
| Preview and export | `lumen/editor.py`, `overlays.py`, `click_zoom.py` |
| Timed layers and speech | `lumen/captions.py`, `layer_ui.py`, `speech.py`, `speech_worker.py`, `clicks.py` |
| Wallpaper gallery | `lumen/wallpapers.py`, `wallpaper_ui.py`, `wallpapers/` |
| Local MCP and agent operations | `lumen/mcp_server.py`, `agent.py`, `scripts/setup-mcp.py`; see [MCP](MCP.md) |
| Launch, installation, and live checks | `scripts/` |
| Unit and integration tests | `tests/` |

Capture and export are separate. GTK authors an edit recipe and timed layers; rendering applies that recipe to a new output. Subprocesses use explicit argument lists, and Lumen owns and signals its capture and transcription processes.

## Tests

From the repository root:

```sh
/usr/bin/python -m unittest discover -s tests -v
```

The suite covers project storage and recovery, capture/replay arguments and lifecycle, HUD and editor state, real FFmpeg rendering, source preservation, timed layers, caption retiming, speech-cue grouping, and wallpaper handling. Some coverage depends on system multimedia or GTK components. Report failures and skips alongside the command and environment.

The [verification record](VERIFICATION.md) contains historical results from the development Omarchy desktop. It does not replace rerunning relevant checks for a new change. Use the [manual QA checklist](QA.md) for broader coverage.

## Opt-in live verification

Inspect a script before running it. Run desktop checks only inside the target Hyprland session, with visible content and selected audio suitable for capture. These are separate from the unit-test command.

| Script | Scope |
| --- | --- |
| `scripts/verify_ui.py` | Records its own Studio window; checks playback, preview, export, and replay |
| `scripts/verify_workflow.py` | Records a small region around the pointer with desktop audio; checks pause/resume, edit persistence, rendering, and source integrity |
| `scripts/verify_hud.py` | Checks launch, floating behavior, navigation, shared settings, countdown cancellation, and guarded close; records its own Studio window |
| `scripts/verify_inputs.py` | Uses a generated pattern window, private test tone, and synthetic camera image to check HUD input toggles and cleanup; no physical microphone/camera or speaker playback |
| `scripts/verify_replay_inputs.py` | Checks replay input behavior with generated media |
| `scripts/verify_quit.py` | Checks quit guards and invalid-edit recovery with synthetic footage |
| `scripts/verify_layers.py` | Generates footage and exercises layer controls, preview, sidecars, and export; screenshots only its own window |
| `scripts/verify_click_zoom.py` | Uses synthetic footage to check click-driven zoom, saved controls, preview, and export |
| `scripts/verify_wallpapers.py` | Uses synthetic footage and an image fixture to check gallery selection/import, persistence, preview, and export |
| `scripts/verify_mcp.py` | Uses the official MCP client, synthetic media, and an owned test window to check recording/replay, editing, export, cancellation, and reconnecting |

The scripts keep test artifacts in private temporary directories. `verify_layers.py` accepts `--speech-fixture /path/to/test-speech.wav` for an explicitly chosen local speech fixture; it does not download one automatically. Wallpaper import verification supplies its fixture to the chooser callback, so the real system file-dialog interaction still needs manual QA.

Before sharing results, distinguish synthetic inputs from physical hardware and short smoke tests from sustained recording or performance measurements.
