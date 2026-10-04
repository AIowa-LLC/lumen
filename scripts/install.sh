#!/usr/bin/env bash
set -euo pipefail

LUMEN_SOURCE="$(dirname -- "$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")")"
/usr/bin/python - "$LUMEN_SOURCE" <<'PY'
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

source = Path(sys.argv[1])
data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser()
bin_home = Path(os.environ.get("LUMEN_INSTALL_BIN", str(Path.home() / ".local/bin"))).expanduser()
if not data_home.is_absolute() or not bin_home.is_absolute():
    raise SystemExit("XDG_DATA_HOME and LUMEN_INSTALL_BIN must be absolute paths")
app = data_home / "lumen"
applications = data_home / "applications"
icons = data_home / "icons/hicolor/scalable/apps"
for folder in (app, bin_home, applications, icons):
    folder.mkdir(parents=True, exist_ok=True)
shutil.copytree(source / "lumen", app / "lumen", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
for filename in ("README.md", "LICENSE"):
    shutil.copy2(source / filename, app / filename)
shutil.copytree(source / "docs", app / "docs", dirs_exist_ok=True)
if (source / ".speech/runtime.json").is_file():
    shutil.copytree(source / ".speech", app / ".speech", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (app / "scripts").mkdir(exist_ok=True)
    shutil.copy2(source / "scripts/setup-speech.py", app / "scripts/setup-speech.py")
if (source / ".mcp/runtime.json").is_file():
    import json

    shutil.copytree(source / ".mcp", app / ".mcp", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    runtime_path = app / ".mcp/runtime.json"
    runtime = json.loads(runtime_path.read_text())
    runtime["python"] = str(app / ".mcp/venv/bin/python")
    runtime_path.write_text(json.dumps(runtime, indent=2) + "\n")
    runtime_path.chmod(0o600)
(app / "scripts").mkdir(exist_ok=True)
shutil.copy2(source / "scripts/setup-mcp.py", app / "scripts/setup-mcp.py")
launcher = bin_home / "lumen"
launcher.write_text(
    "#!/usr/bin/env bash\nset -euo pipefail\n"
    f"export PYTHONPATH={shlex.quote(str(app))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
    'exec /usr/bin/python -P -m lumen "$@"\n'
)
launcher.chmod(0o755)
desktop = (source / "data/io.github.lumen.Recorder.desktop").read_text()
# Desktop entries have their own quoting rules, distinct from shell quoting.
escaped = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%")
desktop = desktop.replace("Exec=lumen", f'Exec="{escaped}"')
(applications / "io.github.lumen.Recorder.desktop").write_text(desktop)
shutil.copy2(source / "data/icon.svg", icons / "io.github.lumen.Recorder.svg")
if shutil.which("update-desktop-database"):
    subprocess.run(["update-desktop-database", str(applications)], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(f"Installed Lumen in {app}")
print(f"Launch it from your application menu or run {launcher}")
PY
