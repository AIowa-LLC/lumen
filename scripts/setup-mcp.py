#!/usr/bin/python
"""Install the optional MCP SDK into a private runtime, leaving system Python intact."""

import argparse
import json
from pathlib import Path
import subprocess
import shutil
import sys

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--python", help="Python 3.11+ with venv/ensurepip support (GTK is not needed here)")
args = parser.parse_args()
runtime = root / ".mcp"
runtime.mkdir(mode=0o700, exist_ok=True)
venv = runtime / "venv"
try:
    interpreter_choice = args.python or sys.executable
    if not args.python:
        check = subprocess.run([interpreter_choice, "-c", "import ensurepip"], capture_output=True)
        if check.returncode and shutil.which("uv"):
            found = subprocess.run(["uv", "python", "find", "--managed-python", "--no-python-downloads"], capture_output=True, text=True)
            if found.returncode == 0:
                interpreter_choice = found.stdout.strip()
    subprocess.run([interpreter_choice, "-m", "venv", str(venv)], check=True)
    interpreter = venv / "bin/python"
    subprocess.run([str(interpreter), "-m", "pip", "install", "mcp>=2.3,<3"], check=True)
    version = subprocess.check_output([str(interpreter), "-c", "from importlib.metadata import version; print(version('mcp'))"], text=True).strip()
    config = runtime / "runtime.json"
    config.write_text(json.dumps({"python": str(interpreter), "sdk_version": version}, indent=2) + "\n")
    config.chmod(0o600)
    print(f"MCP {version} ready. Connect using lumen --mcp (installed) or ./scripts/lumen --mcp (checkout).")
except (OSError, subprocess.CalledProcessError) as exc:
    print(f"MCP setup failed: {exc}\nUse --python with a Python 3.11+ interpreter that includes venv/ensurepip, or install the mcp extra in your own virtual environment.", file=sys.stderr)
    raise SystemExit(1)
