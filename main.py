"""
main.py  —  Computer-Use Agent entry point
Phase 3: shows active browser mode in the startup banner and runs a
         lightweight CDP pre-flight check when BROWSER_MODE=attach.
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
    from src.config import BROWSER_MODE, CDP_URL, CDP_TAB_POLICY

    mode = BROWSER_MODE.lower()

    print()
    print("╔══════════════════════════════════════════════════════╗")
    print("║         Computer-Use Agent  —  Phase 3 (CDP)        ║")
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
    print("  Type 'quit' or 'exit' to stop.")
    print("  Type 'mode' to show the current browser mode.")
    print()


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------

def run():
    _print_banner()

    # CDP pre-flight — abort early if Chrome isn't reachable
    if not _cdp_preflight():
        sys.exit(1)

    from src.graph import app as graph
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

        print()

        initial_state = {
            "task":               task,
            "plan":               None,
            "current_step_index": 0,
            "step_results":       [],
            "retry_count":        0,
            "is_done":            False,
            "is_failed":          False,
            "memory_hints":       None,
            "last_error":         None,
            "ask_user_message":   None,
            "task_start_ms":      None,
            "_last_tool_result":  None,
            "_last_step_result":  None,
            "slots":              None,
        }

        try:
            graph.invoke(initial_state)
        except KeyboardInterrupt:
            print("\n[Agent] Task interrupted.")
            # Make sure we don't leave a dangling browser process in launch mode
            from src.graph import _close_browser_instance
            _close_browser_instance()
        except Exception as e:
            log.error("Unhandled graph error: %s", e, exc_info=True)
            print(f"\n[ERROR] Unhandled error: {e}")
            from src.graph import _close_browser_instance
            _close_browser_instance()

        print()


if __name__ == "__main__":
    run()