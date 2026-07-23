"""
main.py  —  Computer-Use Agent entry point
Phase 3:  shows active browser mode in the startup banner and runs a
          lightweight CDP pre-flight check when BROWSER_MODE=attach.
Phase 8:  when VOICE_ENABLED=true, replaces the text input() loop below
          with a global-hotkey + system-tray voice flow (src/voice.py).
          _run_task() is the shared piece both paths funnel through — a
          task string, however it was produced, runs through the exact
          same graph.invoke() pipeline either way.
Phase 10: when GUI_ENABLED=true, takes priority over both of the above
          and opens the native desktop window instead (src/desktop_app.py)
          — the window absorbs voice/wake-word listening in the
          background too (no separate tray icon in this mode).
"""

import sys
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Phase 3: CDP pre-flight check
# ---------------------------------------------------------------------------

def _cdp_preflight() -> bool:
    """
    When BROWSER_MODE=attach, verify Chrome is reachable at CDP_URL before
    we try to run any task.  Prints a clear error and returns False if not.
    Returns True if we should continue.
    """
    from src.config import BROWSER_MODE, CDP_URL

    if BROWSER_MODE.lower() != "attach":
        return True

    import urllib.request
    import urllib.error
    import json

    version_url = CDP_URL.rstrip("/") + "/json/version"
    print(f"\n[CDP] Checking Chrome at {CDP_URL} …", end=" ", flush=True)

    try:
        with urllib.request.urlopen(version_url, timeout=4) as resp:
            data    = json.loads(resp.read().decode())
            browser = data.get("Browser", "Chrome")
        print(f"OK ({browser})")
        return True

    except urllib.error.URLError:
        print("FAILED")
        print()
        print("  ✗  Chrome is not reachable at " + CDP_URL)
        print()
        print("  To use attach mode:")
        print("    1. Close any existing Chrome windows")
        print("    2. Run:  scripts\\launch_chrome_cdp.bat   (Windows)")
        print("             scripts/launch_chrome_cdp.sh    (macOS/Linux)")
        print("    3. Log in to Gmail / Google Docs / etc. in that window")
        print("    4. Run this agent again")
        print()
        print("  To use a fresh managed browser instead:")
        print("    Remove BROWSER_MODE=attach from your .env  (or set it to launch)")
        print()
        print("  Diagnostic tool:  python scripts/check_cdp.py")
        print()
        return False

    except Exception as e:
        print(f"FAILED ({e})")
        log.error("CDP pre-flight error: %s", e)
        return False


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

def _print_banner():
    from src.config import (
        BROWSER_MODE, CDP_URL, CDP_TAB_POLICY,
        VOICE_ENABLED, VOICE_HOTKEY, WAKE_WORD_ENABLED, WAKE_WORD_MODEL,
        GUI_ENABLED,
    )

    mode = BROWSER_MODE.lower()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║      Computer-Use Agent  —  Phase 10 (Desktop App)  ║")
    print("╚══════════════════════════════════════════════════════╝")
    print()

    if mode == "attach":
        print(f"  Browser mode : ATTACH  (using your real Chrome)")
        print(f"  CDP URL      : {CDP_URL}")
        print(f"  Tab policy   : {CDP_TAB_POLICY}")
        print()
        print("  Real cookies and logged-in sessions are available.")
        print("  Chrome will NOT be closed when a task finishes.")
    else:
        print(f"  Browser mode : LAUNCH  (fresh managed Chromium)")
        print()
        print("  No real cookies — clean sandbox per task.")
        print("  Set BROWSER_MODE=attach in .env to use your real browser.")

    print()

    if GUI_ENABLED:
        print("  Input mode   : DESKTOP APP")
        print()
        print("  Opening the app window — type a task there, or use voice")
        print("  if VOICE_ENABLED is set (works even while the window isn't focused).")
        print("  This terminal window can be minimized once the app opens.")
    elif VOICE_ENABLED:
        if WAKE_WORD_ENABLED:
            wake_phrase = WAKE_WORD_MODEL.replace("_", " ")
            print(f"  Input mode   : VOICE  (hotkey: {VOICE_HOTKEY}, wake word: \"{wake_phrase}\")")
            print()
            print(f"  Say \"{wake_phrase}\" any time, or press {VOICE_HOTKEY}, to start a task.")
            print("  Wake-word recordings stop automatically once you pause speaking.")
            print("  Use the system tray icon's Quit to exit.")
        else:
            print(f"  Input mode   : VOICE  (hotkey: {VOICE_HOTKEY})")
            print()
            print(f"  Press {VOICE_HOTKEY} to start/stop recording a task.")
            print("  Set WAKE_WORD_ENABLED=true in .env to add hands-free \"hey jarvis\" activation.")
            print("  Use the system tray icon's Quit to exit.")
    else:
        print("  Input mode   : TEXT")
        print()
        print("  Type 'quit' or 'exit' to stop.")
        print("  Type 'mode' to show the current browser mode.")
        print("  Set VOICE_ENABLED=true in .env to switch to voice input, or")
        print("  GUI_ENABLED=true for the desktop app.")

    print()


# ---------------------------------------------------------------------------
# Shared task runner — both the text loop and voice callback use this
# ---------------------------------------------------------------------------

def _run_task(task: str) -> None:
    """
    Run one task string through the full graph. Used by both the text
    REPL loop and (when VOICE_ENABLED=true) src.voice's on_task callback
    — a task is a task regardless of how the string was produced.

    The actual invocation logic lives in src.graph.run_task_sync() —
    shared with Phase 10's desktop app (src/desktop_app.py) so there's
    exactly one canonical way to run a task, not two copies that can
    drift apart over time.
    """
    from src.graph import run_task_sync

    print()
    run_task_sync(task)
    print()


# ---------------------------------------------------------------------------
# Text REPL (default input mode)
# ---------------------------------------------------------------------------

def _run_text_loop():
    from src.config import BROWSER_MODE

    while True:
        try:
            task = input("Enter task: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Agent] Shutting down.")
            break

        if not task:
            continue

        if task.lower() in ("quit", "exit", "q"):
            print("[Agent] Goodbye.")
            break

        # Convenience command — show active mode without running a task
        if task.lower() in ("mode", "status"):
            from src.config import CDP_URL, CDP_TAB_POLICY
            mode = BROWSER_MODE.lower()
            if mode == "attach":
                print(f"[Mode] ATTACH  (CDP: {CDP_URL}, tab: {CDP_TAB_POLICY})")
            else:
                print(f"[Mode] LAUNCH  (fresh Chromium)")
            continue

        _run_task(task)


# ---------------------------------------------------------------------------
# Voice loop (Phase 8, opt-in via VOICE_ENABLED)
# ---------------------------------------------------------------------------

def _run_voice_loop():
    from src.voice import run_voice_loop, VoiceDependencyError

    try:
        run_voice_loop(on_task=_run_task)
    except VoiceDependencyError as e:
        print(f"\n[ERROR] {e}\n")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Desktop app (Phase 10, opt-in via GUI_ENABLED)
# ---------------------------------------------------------------------------

def _run_desktop_app():
    from src.desktop_app import run_desktop_app
    run_desktop_app()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    _print_banner()

    # CDP pre-flight — abort early if Chrome isn't reachable
    if not _cdp_preflight():
        sys.exit(1)

    from src.config import GUI_ENABLED, VOICE_ENABLED

    if GUI_ENABLED:
        _run_desktop_app()
    elif VOICE_ENABLED:
        _run_voice_loop()
    else:
        _run_text_loop()


if __name__ == "__main__":
    run()