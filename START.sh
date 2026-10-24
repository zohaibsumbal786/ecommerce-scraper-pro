#!/bin/bash
# eCommerce Scraper Pro - Mac/Linux Launcher
cd "$(dirname "$0")"

clear
echo ""
echo "  ====================================================="
echo "    eCommerce Scraper Pro  -  Starting Up"
echo "  ====================================================="
echo ""

# Find Python
PYTHON=""
for CMD in python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$CMD" &>/dev/null; then
        VER=$("$CMD" -c "import sys; print(sys.version_info >= (3,8))" 2>/dev/null)
        if [ "$VER" = "True" ]; then
            PYTHON="$CMD"
            break
        fi
    fi
done

# Check common Mac paths
if [ -z "$PYTHON" ]; then
    for P in "/opt/homebrew/bin/python3" "/usr/local/bin/python3" "/usr/bin/python3"; do
        if [ -x "$P" ]; then
            VER=$("$P" -c "import sys; print(sys.version_info>=(3,8))" 2>/dev/null)
            if [ "$VER" = "True" ]; then PYTHON="$P"; break; fi
        fi
    done
fi

if [ -z "$PYTHON" ]; then
    echo "  [!] Python 3.8+ not found."
    echo ""
    echo "  On Mac:   brew install python3"
    echo "  Or visit: https://www.python.org/downloads/"
    echo ""
    exit 1
fi

echo "  [OK] $($PYTHON --version 2>&1)"
echo ""
echo "  [..] Installing packages (first run takes ~2 min)..."

"$PYTHON" -m pip install \
    "fastapi==0.115.6" \
    "uvicorn[standard]==0.34.0" \
    "httpx==0.28.1" \
    "sqlalchemy==2.0.36" \
    "aiofiles==24.1.0" \
    "python-multipart==0.0.12" \
    "beautifulsoup4==4.12.2" \
    --quiet --disable-pip-version-check 2>/dev/null || \
"$PYTHON" -m pip install \
    "fastapi==0.115.6" \
    "uvicorn[standard]==0.34.0" \
    "httpx==0.28.1" \
    "sqlalchemy==2.0.36" \
    "aiofiles==24.1.0" \
    "python-multipart==0.0.12" \
    "beautifulsoup4==4.12.2" \
    --user --quiet --disable-pip-version-check

echo "  [OK] Packages ready."
echo ""

mkdir -p images static

PORT=8000
if command -v lsof &>/dev/null; then
    lsof -i :8000 &>/dev/null && PORT=8001
    lsof -i :8001 &>/dev/null && PORT=8002
fi

# Open browser
if [[ "$OSTYPE" == "darwin"* ]]; then
    (sleep 4 && open "http://localhost:$PORT") &
elif command -v xdg-open &>/dev/null; then
    (sleep 4 && xdg-open "http://localhost:$PORT") &
fi

echo "  ====================================================="
echo "    URL: http://localhost:$PORT"
echo "    Browser opens in 4 seconds"
echo "    Press Ctrl+C to stop"
echo "  ====================================================="
echo ""

"$PYTHON" -m uvicorn main:app --host 0.0.0.0 --port "$PORT"
