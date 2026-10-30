#!/usr/bin/env bash
# Starts OpenNeoUA Studio from source on Linux (and macOS).
# First run: creates a private virtual environment (.venv) and installs requirements.txt.
# Later runs: only reinstall when requirements.txt changed.
#
# Usage:   ./run_OpenNeoUAStudio.sh [main.py arguments]
#          bash run_OpenNeoUAStudio.sh --map-editor path/to/level.ldf
# If the file is not executable:  chmod +x run_OpenNeoUAStudio.sh

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

VENV_DIR=".venv"
STAMP="$VENV_DIR/.requirements.stamp"

# Git tracks requirements.txt, but a Windows checkout may show Requirements.txt: accept both spellings.
REQUIREMENTS=""
for candidate in requirements.txt Requirements.txt; do
    if [ -f "$candidate" ]; then
        REQUIREMENTS="$candidate"
        break
    fi
done
if [ -z "$REQUIREMENTS" ]; then
    echo "[ERROR] requirements.txt was not found next to this script." >&2
    exit 1
fi

# Pick a Python 3.10+ interpreter.
PYTHON_CMD=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        PYTHON_CMD="$candidate"
        break
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] Python 3.10 or newer was not found in PATH." >&2
    echo "        Install it from your distribution (for example: sudo apt install python3 python3-venv python3-pip)." >&2
    exit 1
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[INFO] Creating virtual environment in $VENV_DIR ..."
    if ! "$PYTHON_CMD" -m venv "$VENV_DIR"; then
        rm -rf "$VENV_DIR"
        echo "[ERROR] Could not create the virtual environment." >&2
        echo "        On Debian/Ubuntu install the venv module: sudo apt install python3-venv" >&2
        exit 1
    fi
fi

VENV_PYTHON="$VENV_DIR/bin/python"

if [ ! -f "$STAMP" ] || [ "$REQUIREMENTS" -nt "$STAMP" ]; then
    echo "[INFO] Installing dependencies from $REQUIREMENTS ..."
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"
    touch "$STAMP"
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(uname -s)" = "Linux" ] &&
   [ -z "${QT_QPA_PLATFORM:-}" ]; then
    echo "[WARNING] No graphical session found (DISPLAY and WAYLAND_DISPLAY are empty)." >&2
    echo "          The Studio needs a desktop session; set QT_QPA_PLATFORM=offscreen only for tests." >&2
fi

status=0
"$VENV_PYTHON" main.py "$@" || status=$?
if [ "$status" -ne 0 ]; then
    echo >&2
    echo "[ERROR] OpenNeoUA Studio exited with code $status." >&2
    echo "        If Qt reports that the 'xcb' platform plugin could not be loaded, install the XCB cursor library:" >&2
    echo "        Debian/Ubuntu: sudo apt install libxcb-cursor0     Fedora: sudo dnf install xcb-util-cursor" >&2
    echo "        Music and sounds need GStreamer or FFmpeg (for example: sudo apt install gstreamer1.0-plugins-good)." >&2
fi
exit "$status"
