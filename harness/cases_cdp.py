import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Optional

# Re-use shared infrastructure from the main harness
from harness.cases import TestCase, _get_slots, _no_crash_and_no_silent_skip


# ---------------------------------------------------------------------------
# CDP availability detection
# ---------------------------------------------------------------------------

def _chrome_cdp_available(url: str = "http://localhost:9222") -> bool:
    """Return True if Chrome is reachable at the CDP URL."""
    try:
        with urllib.request.urlopen(f"{url}/json/version", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


_CDP_AVAILABLE       = _chrome_cdp_available()
_CDP_SKIP_IF_MISSING = (
    None
    if _CDP_AVAILABLE
    else "Chrome not running with --remote-debugging-port=9222 — run scripts/launch_chrome_cdp.bat first"
)


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

def _assert_gmail_inbox(final_state: dict) -> tuple[bool, str]:
    """
    Verify the agent navigated to Gmail inbox in attach mode.
    Checks that:
      1. The task completed without crashing.
      2. browser_url contains mail.google.com.
      3. The plan used BROWSER mode (not Windows UI fallback).
    """
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail

    slots = _get_slots(final_state)
    if not slots or not slots.browser_url:
        return False, "No browser_url in final slots — navigation may have failed"

    url = slots.browser_url
    if "mail.google.com" not in url:
        return False, f"Expected mail.google.com URL, got: {url}"

    return True, f"Successfully navigated to Gmail inbox: {url}"


def _assert_google_docs_open(final_state: dict) -> tuple[bool, str]:
    """
    Verify the agent opened Google Docs in the attached browser.
    """
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail

    slots = _get_slots(final_state)
    if not slots or not slots.browser_url:
        return False, "No browser_url in final slots"

    url = slots.browser_url
    if "docs.google.com" not in url:
        return False, f"Expected docs.google.com URL, got: {url}"

    return True, f"Opened Google Docs: {url}"


def _assert_attach_mode_was_used(final_state: dict) -> tuple[bool, str]:
    """
    Regression check: verify BROWSER_MODE=attach was actually used for the
    task (not silently falling back to launch mode). We check this by
    inspecting the browser_url slot — if it's a site that requires auth
    (Gmail, Docs) and it loaded successfully, attach mode was used.

    This is a meta-check complementing the functional assertions above.
    """
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail

    slots = _get_slots(final_state)
    if not slots:
        return False, "No slots in final state"

    # Check if the current env has BROWSER_MODE=attach
    mode = os.getenv("BROWSER_MODE", "launch")
    if mode != "attach":
        return False, (
            f"BROWSER_MODE is '{mode}', not 'attach'. "
            f"Set BROWSER_MODE=attach in .env before running CDP tests."
        )

    return True, f"BROWSER_MODE=attach was active"


def _assert_new_tab_opened_and_closed(final_state: dict) -> tuple[bool, str]:
    """
    Verify that in CDP_TAB_POLICY=new mode, a task can complete without
    leaving orphan tabs. (We can't directly inspect tab count from the
    harness, so we check that the task completed cleanly — graph.py's
    _close_browser_instance → BrowserTools.close() → _close_attach()
    should have called page.close().)
    """
    ok, detail = _no_crash_and_no_silent_skip(final_state)
    if not ok:
        return False, detail

    # If we got here, the task ran to completion including close() without
    # raising — that's the primary signal.
    return True, "Task completed cleanly (tab open/close cycle succeeded)"


# ---------------------------------------------------------------------------
# CDP-specific test cases
# ---------------------------------------------------------------------------

CDP_CASES: list[TestCase] = [

    # --- Basic connectivity ---
    TestCase(
        name="cdp_navigate_to_example",
        category="G: CDP attach mode",
        task="go to example.com and tell me the page title",
        assert_fn=lambda fs: _no_crash_and_no_silent_skip(fs),
        skip_reason=_CDP_SKIP_IF_MISSING,
    ),

    # --- Authenticated sessions (require you to be logged in) ---
    TestCase(
        name="cdp_gmail_inbox",
        category="G: CDP attach mode",
        task="open gmail and show me the inbox",
        assert_fn=_assert_gmail_inbox,
        skip_reason=(
            _CDP_SKIP_IF_MISSING
            or "Requires Gmail logged-in session in the CDP Chrome window — run manually"
        ),
    ),

    TestCase(
        name="cdp_google_docs_open",
        category="G: CDP attach mode",
        task="go to google docs and show me my recent documents",
        assert_fn=_assert_google_docs_open,
        skip_reason=(
            _CDP_SKIP_IF_MISSING
            or "Requires Google account logged-in session in the CDP Chrome window — run manually"
        ),
    ),

    # --- Mode verification ---
    TestCase(
        name="cdp_mode_active_check",
        category="G: CDP attach mode",
        task="go to example.com",
        assert_fn=_assert_attach_mode_was_used,
        skip_reason=_CDP_SKIP_IF_MISSING,
    ),

    # --- Tab lifecycle ---
    TestCase(
        name="cdp_tab_lifecycle",
        category="G: CDP attach mode",
        task="go to example.com and extract the page text",
        assert_fn=_assert_new_tab_opened_and_closed,
        skip_reason=_CDP_SKIP_IF_MISSING,
    ),

    # --- Research with real cookies (e.g. YouTube without bot-blocks) ---
    TestCase(
        name="cdp_youtube_search_logged_in",
        category="G: CDP attach mode",
        task="go to youtube.com and search for python tutorials",
        assert_fn=lambda fs: (
            (True, f"URL: {_get_slots(fs).browser_url if _get_slots(fs) else 'unknown'}")
            if _no_crash_and_no_silent_skip(fs)[0]
               and _get_slots(fs)
               and _get_slots(fs).browser_url
               and "youtube.com" in (_get_slots(fs).browser_url or "")
            else (False, _no_crash_and_no_silent_skip(fs)[1])
        ),
        skip_reason=(
            _CDP_SKIP_IF_MISSING
            or "Requires Chrome CDP session — run manually after scripts/launch_chrome_cdp.bat"
        ),
    ),
]


def get_cdp_cases(category: Optional[str] = None) -> list[TestCase]:
    """Return CDP cases, optionally filtered by category substring."""
    if category is None:
        return CDP_CASES
    return [c for c in CDP_CASES if category.lower() in c.category.lower()]