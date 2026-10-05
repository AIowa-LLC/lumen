![Lumen: native screen recording and presentation editing for Omarchy](docs/assets/lumen-header.png)

# Lumen

**Record your screen. Shape the story. Built exclusively for Omarchy.**

[![Built for Omarchy](https://img.shields.io/badge/Built_for-Omarchy-c7b6ff?style=flat-square)](#built-for-omarchy)
[![Status: Alpha](https://img.shields.io/badge/Status-Alpha-ff837c?style=flat-square)](#project-status)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![GTK 4 + libadwaita](https://img.shields.io/badge/GTK_4-libadwaita-9075ed?style=flat-square)](#requirements)
[![License: MIT](https://img.shields.io/badge/License-MIT-a6da95?style=flat-square)](LICENSE)
[![Follow Tony on X](https://img.shields.io/badge/Follow-%40tonysimons__-111111?style=flat-square&logo=x&logoColor=white)](https://x.com/tonysimons_)

Lumen is a native screen recorder and presentation editor for **Omarchy's Hyprland/Wayland desktop**. Capture a display, region, or visible window area, then turn the take into a framed demo with zoom, captions, annotations, and a wallpaper backdrop.

The interface uses **GTK 4 and libadwaita**, capture uses **GPU Screen Recorder**, and preview/export uses **FFmpeg**. Recordings, edit recipes, and optional speech transcription stay on your computer. No cloud account is required.

[Get started](#get-started) · [User guide](docs/USAGE.md) · [MCP guide](docs/MCP.md) · [Captions & layers](docs/CAPTIONS.md) · [Contribute](docs/DEVELOPMENT.md) · [Support](#support-the-work)

## Built for Omarchy

Lumen is an **Omarchy-exclusive project**. Its capture discovery, floating controls, and optional global-shortcut helper are built around Hyprland on Wayland. Other Linux desktops, X11, macOS, and Windows are outside the supported scope.

## What you can do

- **Record from a compact HUD.** Choose a source, start a countdown, pause/resume, and stop to open the saved take in Studio. Switch between HUD and Studio without interrupting capture.
- **Control your inputs.** Select desktop audio, a microphone, or both; use live microphone and webcam toggles with the native backend. Desktop and microphone audio are stored in separate tracks.
- **Save the last moment.** Start a 15-, 30-, or 60-second replay buffer and save clips while it keeps running.
- **Give a recording a finished frame.** Trim, change playback speed, add padding, and choose gradients, ten built-in wallpapers, or a custom image.
- **Focus on the action.** Use fixed or animated zoom, cursor-based suggestions, or click-driven zoom with adjustable hold and transition timing.
- **Add context.** Author captions, text labels, arrows, boxes, highlights, and click rings. Import/export SRT or WebVTT, or generate captions with an optional local speech runtime.
- **Export locally.** Render an edited preview, export H.264 MP4 or looping GIF, and preserve the original recording alongside the saved edit recipe.
- **Let an agent run the workflow.** Connect a stdio MCP client to record, inspect frames, edit wallpapers and layers, and export through the same native app. See [MCP setup](docs/MCP.md).

These features are implemented in the current source. Hardware support and workflow limits are covered below and in the [user guide](docs/USAGE.md).

## Get started

### Requirements

Use **Python 3.11 or newer inside a running Omarchy/Hyprland Wayland session**, with the following system components available:

| Purpose | Packages or commands |
| --- | --- |
| Native interface | `python`, `python-gobject`, `gtk4`, `libadwaita` |
| Video playback | `gstreamer`, `gst-plugins-base`, `gst-plugins-good`, `gst-libav` |
| Main capture backend | `gpu-screen-recorder` |
| Compatibility capture | `wf-recorder` |
| Export and media inspection | `ffmpeg`, `ffprobe` |
| Region selection | `slurp` |
| Monitor/window discovery | `hyprctl` |
| Audio discovery and live mic control | `pactl` with PipeWire/PulseAudio compatibility |
| Optional live webcam bubble | `mpv` with V4L2 input support |
| Optional automatic captions | Local faster-whisper runtime/model or whisper.cpp CLI/model |

The development desktop's `ffmpeg-obs` package also provides `ffmpeg` and `ffprobe`. The GTK app has no PyPI runtime dependencies; optional MCP support uses the official Python SDK. Installing Lumen's Python package alone does not supply GTK, GStreamer, or the capture tools. The launcher and local installer do not install system dependencies.

### Run from source

```sh
git clone https://github.com/AIowa-LLC/lumen.git
cd lumen
./scripts/lumen
```

The launcher uses `/usr/bin/python` so Arch's PyGObject and multimedia packages remain available, including when another Python virtual environment is active.

Lumen opens its floating recording HUD. Choose **Record** to start, or **Open Studio** for the library, full capture settings, and editor. Launching Lumen again raises the existing view without starting a recording.

To inspect local tools, devices, and capabilities:

```sh
./scripts/lumen --diagnostics
```

### Add it to your application menu

```sh
./scripts/install.sh
```

The installer copies Lumen to `~/.local/share/lumen`, creates `~/.local/bin/lumen`, and installs the desktop entry and icon. Run it again after updating your checkout. `XDG_DATA_HOME` and `LUMEN_INSTALL_BIN` can override the installation directories with absolute paths.

Global keyboard shortcuts are a separate, optional step. The helper expects Omarchy's Lua-based Hyprland configuration:

```sh
./scripts/install-shortcuts.sh          # Preview the proposed bindings
./scripts/install-shortcuts.sh --apply  # Back up, apply, reload, and validate
```

It adds **Super+Alt+R** to record, **Super+Alt+Shift+R** to stop, **Super+Alt+P** to pause/resume, and **Super+Alt+V** to save a replay. It refuses conflicting bindings and rolls back its own changes if validation fails. See the [controls reference](docs/USAGE.md#commands-and-shortcuts) for all CLI commands and in-app shortcuts.

## Your first recording

1. Open **Studio → Record** or the HUD's gear menu. Choose the display, region, or visible window area and the audio inputs you want.
2. Move the HUD outside the captured area, or enable **Hide controls during capture**. Anything visible inside that area can appear in the recording, including the HUD and webcam bubble.
3. Choose **Record**, then **Stop & save** when finished. The saved take opens in Studio.
4. Set trim points and adjust **Style & export**. Add captions or annotations under **Captions & layers**.
5. Choose **Preview edits** to render a draft. After changing the recipe, render another preview. Export the finished MP4 or GIF when you're ready.

Want footage from just before you pressed save? Start the replay buffer explicitly, then choose **Save replay**. Stopping a buffer discards its unsaved history.

## Files and privacy

The default library is `$(xdg-user-dir VIDEOS)/Lumen`, falling back to `~/Videos/Lumen`. Choose another location with:

```sh
LUMEN_LIBRARY="$HOME/Videos/Product demos" ./scripts/lumen
```

Each take has its own private folder containing the source video, `project.json`, and capture log. Captures use `source.mkv`; imported MP4, MKV, WebM, MOV, and AVI files are copied into new projects. Move the **complete take folder** to keep its edit metadata and custom wallpapers together.

Processing is local. Optional cursor telemetry stores pointer positions; optional click capture stores mouse-button events and positions, not keystrokes. Captured audio and visible screen content become part of the source recording. Automatic captions use selected audio tracks and an offline local model. See [storage, settings, and recovery](docs/USAGE.md#storage-settings-and-recovery) for details.

## Know the limits

- **Window capture is a fixed screen rectangle.** Moving, covering, or minimizing that window changes what is recorded. Keep the webcam bubble visible too; a covering window also covers it in the output.
- **Backend and GPU support matter.** The compatibility backend uses software H.264 at native resolution with one audio source. It lacks native live input toggles, webcam composition, cursor hiding, and other codecs. An explicit GPU request fails visibly when that path cannot run.
- **Capture and export are separate pipelines.** Capture offers 30/60/120 fps and H.264/HEVC/AV1 where supported. Presentation export uses FFmpeg software filters and H.264 encoding; Studio currently caps MP4 at 60 fps and GIF at 24 fps.
- **Edited previews are rendered drafts.** They use up to 960px width at 30 fps and lower encoding quality than final export. They do not update live as you change controls.
- **Replay requires GPU Screen Recorder.** It cannot run beside a normal Lumen recording, cannot pause, and does not collect click or cursor metadata. Imported and replay clips can use manually added clicks for click-driven zoom.
- **Audio and captions need review.** Already mixed audio cannot be separated. Speech recognition can make mistakes and does not identify speakers or translate audio.
- **This is a single-clip editor.** Cloud sharing, collaboration, and multi-clip timeline editing are not included.

Real microphone/webcam behavior, long sessions, additional GPU/codec combinations, and broader display layouts still need more testing. No performance advantage over Recordly has been established. The [verification record](docs/VERIFICATION.md) distinguishes completed checks from remaining coverage; the [manual QA checklist](docs/QA.md) covers further testing.

## Documentation

- [User guide](docs/USAGE.md): controls, capture settings, replay, editing, audio, and recovery
- [Agent control with MCP](docs/MCP.md): setup, tool calls, job polling, shared-session safety, and troubleshooting
- [Coding agent guide](AGENTS.md): repository map, verification commands, and contribution safeguards
- [Captions and timed layers](docs/CAPTIONS.md): authoring, click-driven zoom, and local speech setup
- [Development and contributing](docs/DEVELOPMENT.md): source layout, tests, and safe live verification
- [Verification record](docs/VERIFICATION.md): recorded test results and their limits
- [Manual QA checklist](docs/QA.md): hardware and workflow checks
- [Wallpaper artwork](docs/WALLPAPERS.md): bundled assets and generation prompts

## Project status

Lumen is **alpha software under active development**. This README documents implemented behavior on the current branch. Planned and in-progress features are not included in the feature list above.

Found a problem? [Open an issue](https://github.com/AIowa-LLC/lumen/issues) with steps to reproduce, your capture settings, and relevant diagnostics. Review logs for private paths, device details, and screen/window information before posting. Documentation improvements and reproducible test reports are welcome; start with the [contributor guide](docs/DEVELOPMENT.md).

## Support the work

If Lumen is useful to you, star the repository, share a demo, or help test it on your Omarchy setup.

You can also [buy Tony a pizza 🍕](https://buymeacoffee.com/tonysimons) to support his continued open-source work.

Follow [Tony Simons on X](https://x.com/tonysimons_) for project updates.

## License

[MIT](LICENSE) · Copyright © 2026 Lumen contributors.
