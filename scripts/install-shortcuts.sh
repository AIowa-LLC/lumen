#!/usr/bin/env bash
set -euo pipefail

# Preview by default. Applying is a separate, explicit invocation.
exec /usr/bin/python - "$@" <<'PY'
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid


BEGIN = "-- BEGIN LUMEN SHORTCUTS (managed by scripts/install-shortcuts.sh)"
END = "-- END LUMEN SHORTCUTS"
# Hyprland modifiers: SUPER=64, ALT=8, SHIFT=1. Physical keycodes are
# checked too, so a matching code binding is not silently overridden.
BINDINGS = [
    ("SUPER + ALT + R", 72, "R", 27, "Lumen: start recording", "--record"),
    ("SUPER + ALT + SHIFT + R", 73, "R", 27, "Lumen: stop and save", "--stop"),
    ("SUPER + ALT + P", 72, "P", 33, "Lumen: pause or resume", "--pause"),
    ("SUPER + ALT + V", 72, "V", 55, "Lumen: save replay", "--save-replay"),
]


def run_hypr(*args):
    result = subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or "hyprctl failed")
    return result.stdout.strip()


def config_errors():
    errors = run_hypr("configerrors")
    return "" if errors.lower() == "ok" else errors


def atomic_write(path, content, mode):
    fd, temporary = tempfile.mkstemp(prefix=".lumen-bindings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="Preview or install Lumen's global shortcuts in Omarchy's Lua bindings.")
    parser.add_argument("--apply", action="store_true", help="Back up, append, reload, and validate; otherwise only preview")
    parser.add_argument("--launcher", type=Path, help="Path to the installed lumen launcher")
    args = parser.parse_args()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    if not config_home.is_absolute():
        raise RuntimeError("XDG_CONFIG_HOME must be an absolute path")
    target = config_home / "hypr/bindings.lua"
    main_config = config_home / "hypr/hyprland.lua"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"Expected a regular Lua bindings file: {target}; inspect your config manually")
    main_text = main_config.read_text()
    if not re.search(r'''require\s*\(\s*["']hypr\.bindings["']\s*\)''', main_text):
        raise RuntimeError(f"{main_config} does not load hypr.bindings; inspect your config manually")
    launcher = args.launcher or Path(os.environ.get("LUMEN_INSTALL_BIN", str(Path.home() / ".local/bin"))) / "lumen"
    launcher = launcher.expanduser().absolute()
    # JSON string quoting is also valid for these Lua strings. Reject control
    # characters because Lua and JSON differ in Unicode escape handling.
    if any(ord(character) < 32 for character in str(launcher)):
        raise RuntimeError("Launcher path contains control characters")
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    lines = [BEGIN]
    for keys, _, _, _, description, flag in BINDINGS:
        command = shlex.quote(str(launcher)) + " " + flag
        lines.append(f"o.bind({quote(keys)}, {quote(description)}, {quote(command)})")
    lines.append(END)
    block = "\n".join(lines) + "\n"
    original = target.read_bytes()
    text = original.decode("utf-8")
    if BEGIN in text or END in text:
        if text.count(BEGIN) == 1 and text.count(END) == 1 and block in text:
            print(f"Lumen shortcuts are already installed in {target}. No changes made.")
            return 0
        raise RuntimeError("A different or incomplete Lumen block exists. Review it manually before reinstalling; nothing changed")
    live = json.loads(run_hypr("binds", "-j"))
    conflicts = []
    for keys, mask, key, code, _, _ in BINDINGS:
        for binding in live:
            same_key = str(binding.get("key", "")).upper() == key or binding.get("keycode") == code
            if binding.get("modmask") == mask and (same_key or binding.get("catch_all")):
                conflicts.append(f"{keys}: {binding.get('description') or binding.get('dispatcher') or 'existing binding'}")
    if conflicts:
        raise RuntimeError("Existing bindings would conflict; nothing changed:\n" + "\n".join(conflicts))
    print(f"Proposed append to {target}:\n\n{block}")
    if not args.apply:
        print("No changes made. Run this script with --apply after reviewing the shortcuts.")
        return 0
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise RuntimeError(f"Install Lumen first; launcher is not executable: {launcher}")
    errors = config_errors()
    if errors:
        raise RuntimeError("Hyprland already has config errors; nothing changed:\n" + errors)
    updated = original + (b"\n" if not original.endswith(b"\n") else b"") + b"\n" + block.encode("utf-8")
    mode = stat.S_IMODE(target.stat().st_mode)
    backup = target.with_name(target.name + ".lumen-backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    shutil.copy2(target, backup)
    if target.read_bytes() != original:
        raise RuntimeError("Bindings changed during installation; nothing appended. Retry after other edits finish")
    atomic_write(target, updated, mode)
    try:
        run_hypr("reload")
        errors = config_errors()
        if errors:
            raise RuntimeError(errors)
        active = json.loads(run_hypr("binds", "-j"))
        for keys, mask, key, _, description, _ in BINDINGS:
            if not any(b.get("modmask") == mask and str(b.get("key", "")).upper() == key and b.get("description") == description for b in active):
                raise RuntimeError(f"Hyprland did not activate {keys}")
    except Exception as exc:
        if target.read_bytes() == updated:
            atomic_write(target, original, mode)
            try:
                run_hypr("reload")
                rollback_errors = config_errors()
            except Exception as rollback_exc:
                rollback_errors = str(rollback_exc)
            detail = f"\nRestored config still reports: {rollback_errors}" if rollback_errors else ""
            raise RuntimeError(f"Validation failed; original bindings restored. {exc}{detail}\nBackup: {backup}") from exc
        raise RuntimeError(f"Validation failed, but another edit arrived; left that file untouched. Restore manually from {backup}. Error: {exc}") from exc
    print(f"Lumen shortcuts installed and Hyprland validation passed. Backup: {backup}")
    return 0


try:
    raise SystemExit(main())
except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
    print(f"Lumen shortcuts: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
