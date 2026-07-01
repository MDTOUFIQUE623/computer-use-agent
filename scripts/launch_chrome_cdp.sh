#!/usr/bin/env bash
# =============================================================================
#  launch_chrome_cdp.sh  —  Phase 3: Start Chrome with the CDP debug port open
# =============================================================================
#
#  Run this script ONCE before starting the computer-use agent in attach mode.
#  It launches Chrome with --remote-debugging-port=9222 so Playwright can
#  connect to your real, logged-in session via CDP.
#
#  Usage:
#    chmod +x scripts/launch_chrome_cdp.sh
#    ./scripts/launch_chrome_cdp.sh
#    # Chrome opens — log in to Gmail, Google Docs, etc.
#    # Set BROWSER_MODE=attach in your .env, then: python main.py
#
#  Notes:
#    • This must be the ONLY Chrome/Chromium instance running. If Chrome is
#      already open without the debug port, quit it first.
#    • A dedicated profile directory is used (~/chrome_cdp_profile) so this
#      debug session is independent of your normal Chrome profile.
#    • The debug port (9222) matches CDP_URL=http://localhost:9222 (default).
#      If you change --remote-debugging-port here, update CDP_URL in .env.
#    • On macOS: the script looks for Chrome in the standard Applications path.
#      On Linux: it tries google-chrome, google-chrome-stable, chromium-browser,
#      and chromium in PATH order.
#
# =============================================================================

set -euo pipefail

CDP_PORT="${CDP_PORT:-9222}"
CDP_PROFILE_DIR="${CDP_PROFILE_DIR:-$HOME/chrome_cdp_profile}"

# ---------------------------------------------------------------------------
# Locate the Chrome binary
# ---------------------------------------------------------------------------
CHROME_BIN=""

if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    for candidate in \
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
        "/Applications/Chromium.app/Contents/MacOS/Chromium"
    do
        if [[ -x "$candidate" ]]; then
            CHROME_BIN="$candidate"
            break
        fi
    done
else
    # Linux
    for candidate in google-chrome google-chrome-stable chromium-browser chromium; do
        if command -v "$candidate" &>/dev/null; then
            CHROME_BIN="$(command -v "$candidate")"
            break
        fi
    done
fi

if [[ -z "$CHROME_BIN" ]]; then
    echo "[ERROR] Chrome/Chromium not found."
    echo "        Install Google Chrome or set CHROME_BIN in your environment."
    exit 1
fi

echo "[INFO] Found Chrome at: $CHROME_BIN"
echo "[INFO] Profile directory: $CDP_PROFILE_DIR"
echo "[INFO] Starting Chrome on port $CDP_PORT ..."
echo ""
echo "  > Log in to the sites you need (Gmail, Notion, etc.)"
echo "  > Then run: python main.py"
echo "  > Make sure BROWSER_MODE=attach is set in your .env"
echo ""

mkdir -p "$CDP_PROFILE_DIR"

"$CHROME_BIN" \
    --remote-debugging-port="$CDP_PORT" \
    --user-data-dir="$CDP_PROFILE_DIR" \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-timer-throttling \
    --disable-renderer-backgrounding \
    --disable-backgrounding-occluded-windows \
    &

CHROME_PID=$!
echo "[INFO] Chrome PID: $CHROME_PID"

# Give Chrome time to start
sleep 2

# Quick connectivity check
if curl -sf --max-time 3 "http://localhost:$CDP_PORT/json/version" > /dev/null 2>&1; then
    echo "[OK]   Chrome CDP endpoint is ready at http://localhost:$CDP_PORT"
    echo "[OK]   You can now run: python main.py"
else
    echo "[WARN] Chrome started (PID $CHROME_PID) but CDP endpoint not responding yet."
    echo "       Give it a few more seconds, then verify:"
    echo "       curl http://localhost:$CDP_PORT/json/version"
fi