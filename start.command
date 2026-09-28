#!/usr/bin/env bash
# sff (Standalone File Fetcher) — double-click launcher (macOS)
# First run:  detects Python, creates a venv inside app/, installs dependencies.
# Every run:  starts the FastAPI server on localhost:8899 and opens the browser.
# Auto-shutdown: closes itself when you close the browser tab (~20s idle).

cd "$(dirname "$0")"

# Set the Terminal window title
printf '\033]0;sff — Standalone File Fetcher\007'

cat <<'EOF'
========================================
 sff: Spotify -> Rekordbox sync
 http://localhost:8899
========================================

EOF

# ─── 1. Find Python (>= 3.10) ───────────────────────────────────────────────
# Search PATH and common Homebrew install prefixes (brew doesn't always put
# python3.13 on PATH, especially with user-local brew installs).
PYTHON_BIN=""
PYTHON_CANDIDATES=(
    python3.13 python3.12 python3.11 python3.10
    /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10
    /usr/local/bin/python3.13 /usr/local/bin/python3.12
    /usr/local/bin/python3.11 /usr/local/bin/python3.10
    "$HOME/.local/homebrew/bin/python3.13"
    "$HOME/.local/homebrew/bin/python3.12"
    "$HOME/.local/homebrew/bin/python3.11"
    "$HOME/.local/homebrew/bin/python3.10"
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.11/bin/python3
    /Library/Frameworks/Python.framework/Versions/3.10/bin/python3
    python3
)
for cmd in "${PYTHON_CANDIDATES[@]}"; do
    # Resolve to a usable path: either a binary that exists, or a name on PATH
    if [ -x "$cmd" ]; then
        candidate="$cmd"
    elif command -v "$cmd" >/dev/null 2>&1; then
        candidate="$cmd"
    else
        continue
    fi
    # Verify version is >= 3.10
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    cat <<'EOF'
ERROR: Python 3.10 or newer is required, but none was found.

(Found python3 but it's too old — likely macOS's bundled Python 3.9.)

Easiest install (recommended): https://www.python.org/downloads/
  Download Python 3.13, run the installer, then double-click this file again.

Or via Homebrew:
  brew install python@3.13 python-tk@3.13

EOF
    read -n 1 -s -r -p "Press any key to close."
    echo
    exit 1
fi
echo "Using Python: $PYTHON_BIN"
echo

# ─── 2. Bootstrap venv on first run ─────────────────────────────────────────
cd app

if [ ! -x ".venv/bin/python" ]; then
    echo "First-run setup: creating virtual environment and installing dependencies."
    echo "This takes ~2 minutes on the first run; subsequent launches are instant."
    echo
    "$PYTHON_BIN" -m venv .venv || {
        echo
        echo "ERROR: Failed to create virtual environment."
        read -n 1 -s -r -p "Press any key to close."
        exit 1
    }
    echo "Installing Python packages..."
    .venv/bin/pip install --quiet --upgrade pip || true
    if ! .venv/bin/pip install --quiet -r requirements.txt; then
        echo
        echo "ERROR: pip install failed. Scroll up for details."
        read -n 1 -s -r -p "Press any key to close."
        exit 1
    fi
    echo "Setup complete."
    echo
fi

# ─── 2b. Keep yt-dlp current ────────────────────────────────────────────────
# The venv bootstrap above only runs pip when it CREATES .venv, so yt-dlp would
# otherwise stay frozen at whatever shipped on first run. YouTube breaks stale
# extractors within weeks (see DEBUG_LOG section 16), so re-check every launch.
# Nightly (--pre) because YouTube fixes land there first. `yt-dlp -U` cannot be
# used: it only self-updates the standalone binary, not a pip install.
# Scoped to yt-dlp ONLY — pyrekordbox must not silently move.
echo "Checking for yt-dlp updates..."
.venv/bin/python -m pip install -U --pre -q \
    --disable-pip-version-check --timeout 10 --retries 1 \
    "yt-dlp[default]" \
  || echo "  (skipped — offline or PyPI unreachable; using installed version)"
echo

# ─── 3. Run the server ──────────────────────────────────────────────────────
echo "Server log appears below. Close the browser tab to stop the server"
echo "(or press Ctrl+C). This window closes automatically when the server exits."
echo
echo "Browser opens automatically in 3 seconds..."
echo

# Open browser after server has bound the port
( sleep 3 && open "http://localhost:8899" ) &

AUTO_SHUTDOWN_IDLE=20 .venv/bin/python main.py
EXIT_CODE=$?

# ─── 4. Exit handling ────────────────────────────────────────────────────────
if [ "$EXIT_CODE" -eq 0 ] || [ "$EXIT_CODE" -eq 130 ]; then
    # Clean exit (browser closed) or Ctrl+C — close the Terminal window automatically
    osascript -e 'tell application "Terminal" to close (front window)' &>/dev/null &
    exit 0
fi

# Non-clean exit: pause so user can read the error
echo
echo "Server exited with error $EXIT_CODE."
read -n 1 -s -r -p "Press any key to close."
echo
