#!/bin/bash
# eCommerce Scraper Pro — Mac Launcher
# Double-click this file in Finder to start.
# If macOS blocks it: Right-click → Open → Open

# Go to the folder where this script lives
cd "$(dirname "$0")"

clear
echo ""
echo "  ====================================================="
echo "    eCommerce Scraper Pro  |  Mac Launcher"
echo "  ====================================================="
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 1: Find Python 3.8+
# ─────────────────────────────────────────────────────────────────────────────
PYTHON=""

# Try common commands first
for CMD in python3 python3.13 python3.12 python3.11 python3.10 python3.9 python; do
    if command -v "$CMD" &>/dev/null; then
        CHECK=$("$CMD" -c "import sys; print(sys.version_info >= (3,8))" 2>/dev/null)
        if [ "$CHECK" = "True" ]; then
            PYTHON="$CMD"
            break
        fi
    fi
done

# Try known Mac paths (Homebrew Intel + Apple Silicon, pyenv, system)
if [ -z "$PYTHON" ]; then
    for TRY in \
        "/opt/homebrew/bin/python3" \
        "/usr/local/bin/python3" \
        "/usr/bin/python3" \
        "/opt/homebrew/bin/python3.13" \
        "/opt/homebrew/bin/python3.12" \
        "/opt/homebrew/bin/python3.11" \
        "/usr/local/bin/python3.11" \
        "$HOME/.pyenv/shims/python3" \
        "$HOME/Library/Python/3.11/bin/python3"
    do
        if [ -x "$TRY" ]; then
            CHECK=$("$TRY" -c "import sys; print(sys.version_info >= (3,8))" 2>/dev/null)
            if [ "$CHECK" = "True" ]; then
                PYTHON="$TRY"
                break
            fi
        fi
    done
fi

# ── Python not found — guide user to install ─────────────────────────────────
if [ -z "$PYTHON" ]; then
    echo "  [!] Python 3.8+ not found on this Mac."
    echo ""
    echo "  How to install Python:"
    echo ""
    echo "  OPTION A — Homebrew (recommended, free):"
    echo "    1. Open Terminal (cmd+space, type Terminal)"
    echo "    2. Paste and run this command:"
    echo '       /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo "    3. Then run:  brew install python3"
    echo ""
    echo "  OPTION B — Direct download:"
    echo "    Go to https://www.python.org/downloads/macos/"
    echo "    Download and install Python 3.11+"
    echo ""
    echo "  After installing, double-click START_MAC.command again."
    echo ""
    # Open Python download page in browser
    open "https://www.python.org/downloads/macos/" 2>/dev/null || true
    read -rp "  Press Enter to close..."
    exit 1
fi

PY_VER=$("$PYTHON" --version 2>&1)
echo "  [OK] Found: $PY_VER"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 2: Upgrade pip silently
# ─────────────────────────────────────────────────────────────────────────────
"$PYTHON" -m pip install --upgrade pip --quiet --disable-pip-version-check 2>/dev/null || true

# ─────────────────────────────────────────────────────────────────────────────
# STEP 3: Install required packages
# ─────────────────────────────────────────────────────────────────────────────
echo "  [..] Checking and installing required packages..."
echo "       (First time: ~1-2 minutes. Next time: instant.)"
echo ""

"$PYTHON" -m pip install \
    "fastapi==0.115.6" \
    "uvicorn[standard]==0.34.0" \
    "httpx==0.28.1" \
    "sqlalchemy==2.0.36" \
    "aiofiles==24.1.0" \
    "python-multipart==0.0.12" \
    "beautifulsoup4==4.12.2" \
    --quiet --disable-pip-version-check

if [ $? -ne 0 ]; then
    echo "  [WARN] Normal install failed. Trying with --user flag..."
    "$PYTHON" -m pip install \
        "fastapi==0.115.6" \
        "uvicorn[standard]==0.34.0" \
        "httpx==0.28.1" \
        "sqlalchemy==2.0.36" \
        "aiofiles==24.1.0" \
        "python-multipart==0.0.12" \
        "beautifulsoup4==4.12.2" \
        --user --quiet --disable-pip-version-check

    if [ $? -ne 0 ]; then
        echo ""
        echo "  [ERROR] Could not install packages."
        echo "  Check your internet connection and try again."
        read -rp "  Press Enter to close..."
        exit 1
    fi
fi

echo "  [OK] All packages ready."
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# STEP 4: Create required folders
# ─────────────────────────────────────────────────────────────────────────────
mkdir -p images static

# ─────────────────────────────────────────────────────────────────────────────
# STEP 5: Find a free port (try 8000, 8001, 8002)
# ─────────────────────────────────────────────────────────────────────────────
PORT=8000
if lsof -i TCP:8000 -sTCP:LISTEN &>/dev/null 2>&1; then
    PORT=8001
    if lsof -i TCP:8001 -sTCP:LISTEN &>/dev/null 2>&1; then
        PORT=8002
    fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# STEP 6: Open browser after 4 seconds
# ─────────────────────────────────────────────────────────────────────────────
(sleep 4 && open "http://localhost:$PORT") &

# ─────────────────────────────────────────────────────────────────────────────
# STEP 7: Start server
# ─────────────────────────────────────────────────────────────────────────────
echo "  ====================================================="
echo "    Ready!  Opening: http://localhost:$PORT"
echo "    Browser opens in 4 seconds automatically."
echo ""
echo "    To STOP the server: press Ctrl+C"
echo "  ====================================================="
echo ""

"$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PORT"

echo ""
echo "  Server stopped."
read -rp "  Press Enter to close..."
