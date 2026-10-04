# Working on Lumen

This file is for coding agents changing the repository. To operate an installed
Lumen app through an agent, read [Agent control with MCP](docs/MCP.md), including
its shared-session, capture, and job-handling rules.

## Before changing anything

- Read [README.md](README.md), [the user guide](docs/USAGE.md), and
  [Development and contributing](docs/DEVELOPMENT.md). Check the current source
  and any more-specific repository instructions before editing.
- Inspect the branch and working tree. Preserve unrelated changes, original media,
  saved recipes, and existing exports. Do not reset, clean, or overwrite another
  person's work to make a check pass.
- Keep changes focused. Separate documentation from application behavior changes;
  do not change versions or dependencies for a documentation-only task.
- Describe implemented behavior, not planned features. A successful unit test or
  synthetic smoke test is not proof that a user's hardware or installation works.

## Supported environment

Lumen targets **Omarchy's Hyprland/Wayland desktop**, using Python 3.11+, GTK 4,
libadwaita, GStreamer, GPU Screen Recorder, and FFmpeg. Other desktops, X11,
macOS, and Windows are outside the supported scope. Use the system dependencies
listed in the README; installing the Python package alone does not supply them.

The source launcher is `./scripts/lumen`. It uses `/usr/bin/python` so Arch's GTK
and multimedia bindings remain available even when another virtual environment is
active. Run `./scripts/lumen --diagnostics` to inspect the actual local setup.
Desktop checks require the real Hyprland/Wayland and D-Bus session. Do not describe
headless tests as native capture or playback verification.

MCP is optional. Its SDK lives in a separate runtime; the native app still uses
system Python. Preserve this separation and the stdio-only transport.

## Source map

| Area | Start here |
| --- | --- |
| Entry points and single-instance app | `lumen/__main__.py`, `lumen/ui.py` |
| Recording controls | `lumen/hud.py`, `lumen/hud.css` |
| Desktop and device discovery | `lumen/system.py` |
| Capture and replay | `lumen/capture.py`, `lumen/replay.py` |
| Live microphone and webcam | `lumen/microphone.py`, `lumen/microphone_worker.py`, `lumen/camera.py` |
| Project storage and recovery | `lumen/project.py`, `lumen/recovery.py` |
| Rendered preview and export | `lumen/editor.py`, `lumen/overlays.py`, `lumen/click_zoom.py` |
| Timed layers, clicks, and speech | `lumen/layer_ui.py`, `lumen/captions.py`, `lumen/clicks.py`, `lumen/speech.py`, `lumen/speech_worker.py` |
| Wallpapers | `lumen/wallpapers.py`, `lumen/wallpaper_ui.py`, `lumen/wallpapers/` |
| Public MCP tools, schemas, resources, prompt | `lumen/mcp_server.py` |
| Native agent operations and job lifecycle | `lumen/agent.py` |
| Launch and optional MCP runtime setup | `scripts/lumen`, `scripts/install.sh`, `scripts/setup-mcp.py` |
| Automated tests and CI | `tests/`, `.github/workflows/ci.yml` |

## Behavior and privacy to preserve

- Capture and export are separate. Save edits as a recipe; render new output
  without replacing source media or existing exports.
- The HUD, Studio, CLI, and all MCP clients share one native app. Inspect its
  state before acting. Do not stop another session to clear a busy error. A client
  disconnect does not stop capture. Stopping replay discards unsaved history;
  save wanted footage and wait for success first.
- Capture only the user-authorized screen area and inputs. MCP recording defaults
  to no audio. Window capture is a fixed screen rectangle, not isolated window
  capture. Keep the selected window visible and stationary.
- Keep layer times in original source seconds and coordinates normalized to 0–1.
  Sparse edit patches preserve unspecified fields; replacing layers is explicit.
- Preserve native ownership of capture, progress, cancellation, and input state.
  Do not add arbitrary shell execution or a network listener to the MCP surface.
- Keep recordings, screenshots, private logs, secrets, local runtimes, and
  generated test artifacts out of commits. Use synthetic content in fixtures.
  Redact private paths, window titles, device details, and captured content before
  sharing diagnostics. MCP clients may forward returned metadata/images to their
  model provider; local processing does not make that client private.

## Verification

Run checks from the repository root in an environment with the documented
dependencies. The native test suite uses the system interpreter:

```sh
/usr/bin/python -m unittest discover -s tests -v
```

Optional MCP protocol tests require the configured SDK runtime:

```sh
.mcp/venv/bin/python -m unittest discover -s tests -p test_mcp.py -v
```

CI's complete validation setup and commands are in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). It installs the MCP extra
in a system-site-packages virtual environment, runs `ruff check lumen tests
scripts`, and runs the full unittest suite in a pinned Arch container. Follow
that file rather than inventing another required check. Report commands,
environment, failures, and skips; distinguish checks passed from checks not run.

For documentation changes, verify tool names, argument examples, result fields,
and asynchronous behavior against both `lumen/mcp_server.py` and `lumen/agent.py`.
Check relative links and keep the README documentation index current.

Read each `scripts/verify_*.py` script before running it. Live checks can capture
screen/audio or change app state; use only suitable, authorized inputs inside the
target desktop session. Follow [Development](docs/DEVELOPMENT.md) and
[Manual QA](docs/QA.md). The [verification record](docs/VERIFICATION.md) is
historical evidence, not a substitute for checking the current change.

## Pull requests

Work on a focused branch and open a draft PR for review. Follow the current
[repository protections](docs/REPOSITORY.md): required Validation must pass
against the latest base, review conversations must be resolved, and history stays
linear through squash or rebase. Never bypass protections. Pushing, merging, or
publishing still requires the user's authorization; a request for a draft alone
does not grant it.
