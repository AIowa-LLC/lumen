#!/usr/bin/env python
"""Opt-in cold MCP startup and disconnect proof; no capture or visible windows.

Run with the optional SDK's Python inside the desktop session. Only the isolated
test app it creates is terminated; artifacts remain in a private temporary folder.
"""
# ruff: noqa: E402

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import uuid

from mcp import Client
from mcp.client.stdio import StdioServerParameters

repo = Path(__file__).resolve().parents[1]
root = Path(tempfile.mkdtemp(prefix="lumen-mcp-cold-"))
environment = {**os.environ, "LUMEN_LIBRARY": str(root / "library"),
               "XDG_CONFIG_HOME": str(root / "config"),
               "LUMEN_APPLICATION_ID": "io.github.lumen.Recorder.Cold" + uuid.uuid4().hex}
launcher = sys.argv[1] if len(sys.argv) > 1 else str(repo / "scripts/lumen")
params = StdioServerParameters(command=launcher, args=["--mcp"], env=environment)


async def main():
    pid = None
    try:
        async with Client(params, read_timeout_seconds=60) as client:
            assert len((await client.list_tools()).tools) == 20
            result = await client.call_tool("lumen_status")
            assert not result.is_error, result
            state = result.structured_content
            assert state["library"] == str(root / "library"), state
            assert state["version"] == "0.6.0", state
            assert not state["recording"]["active"] and not state["replay"]["active"]
            pid = state["pid"]
            assert pid > 1 and pid != os.getpid()
        os.kill(pid, 0)
        async with Client(params, mode="legacy", read_timeout_seconds=60) as client:
            result = await client.call_tool("lumen_status")
            assert not result.is_error and result.structured_content["pid"] == pid, result
        print(json.dumps({"ok": True, "cold_start": True, "app_survived_disconnect": True,
                          "capture_inactive": True, "version": state["version"], "root": str(root)}))
    finally:
        if pid:
            os.kill(pid, signal.SIGTERM)


asyncio.run(main())
