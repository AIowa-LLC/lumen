"""Application and single-instance command-line entry point."""

from __future__ import annotations

import json
import sys


def main() -> int:
    if "--diagnostics" in sys.argv:
        from .system import diagnostics

        print(json.dumps(diagnostics(), indent=2))
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Lumen — native recording studio for Hyprland\n\n"
            "Usage: lumen [--hud | --studio | --record | --stop | --pause | --replay | --save-replay | --diagnostics]\n\n"
            "  --hud          Open the compact recording controls (default on launch)\n"
            "  --studio       Open the full recording studio and editor\n"
            "  --record       Start with the saved capture settings\n"
            "  --stop         Save recording, or stop/discard unsaved replay\n"
            "  --pause        Toggle pause on the active recording\n"
            "  --replay       Start a replay buffer (30 seconds by default)\n"
            "  --save-replay  Save the recent buffer; keep buffering\n"
            "  --diagnostics  Print local capture diagnostics as JSON\n\n"
            "Shortcuts: Ctrl+R record, Ctrl+Shift+R stop, Ctrl+P pause,\n"
            "           Ctrl+O import, Ctrl+E export, Ctrl+Shift+S save replay,\n"
            "           Ctrl+Shift+H HUD, Ctrl+Shift+O studio, Ctrl+Q quit.\n"
            "Global Hyprland bindings can call the same command flags."
        )
        return 0
    allowed = {
        "--record",
        "--stop",
        "--pause",
        "--replay",
        "--save-replay",
        "--smoke-test",
        "--hud",
        "--studio",
    }
    unknown = set(sys.argv[1:]) - allowed
    if unknown:
        print("Unknown option: " + ", ".join(sorted(unknown)), file=sys.stderr)
        return 2
    from .ui import LumenApplication

    return LumenApplication().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
