"""Application and single-instance command-line entry point."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def main() -> int:
    if sys.argv[1:2] == ["--mcp"]:
        try:
            from .mcp_server import main as serve
        except ImportError as exc:
            runtime = Path(__file__).resolve().parents[1] / ".mcp" / "runtime.json"
            try:
                interpreter = json.loads(runtime.read_text())["python"]
                if Path(interpreter).absolute() != Path(sys.executable).absolute():
                    os.execv(interpreter, [interpreter, "-m", "lumen", *sys.argv[1:]])
            except (OSError, ValueError, KeyError, TypeError):
                pass
            print(f"MCP support requires the optional SDK: run scripts/setup-mcp.py, or install lumen-recorder[mcp]. ({exc})", file=sys.stderr)
            return 1
        return serve(sys.argv[2:])
    if sys.argv[1:2] == ["--agent"]:
        if len(sys.argv) != 3:
            print("--agent requires one JSON request", file=sys.stderr)
            return 2
        from .ui import LumenApplication

        return LumenApplication().run(sys.argv)
    if "--diagnostics" in sys.argv:
        from .system import diagnostics

        print(json.dumps(diagnostics(), indent=2))
        return 0
    if "--help" in sys.argv or "-h" in sys.argv:
        print(
            "Lumen — native recording studio for Hyprland\n\n"
            "Usage: lumen [--hud | --studio | --record | --stop | --pause | --replay | --save-replay | --diagnostics | --mcp]\n\n"
            "  --hud          Open the compact recording controls (default on launch)\n"
            "  --studio       Open the full recording studio and editor\n"
            "  --record       Start with the saved capture settings\n"
            "  --stop         Save recording, or stop/discard unsaved replay\n"
            "  --pause        Toggle pause on the active recording\n"
            "  --replay       Start a replay buffer (30 seconds by default)\n"
            "  --save-replay  Save the recent buffer; keep buffering\n"
            "  --diagnostics  Print local capture diagnostics as JSON\n\n"
            "  --mcp          Run the optional local MCP server over stdio\n\n"
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
