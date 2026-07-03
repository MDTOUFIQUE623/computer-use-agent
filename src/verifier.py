import os
import time
import logging
from typing import Optional

import psutil

from src.config import VERIFICATION_TIMEOUT, ELEMENT_TIMEOUT
from src.models import (
    Step,
    ToolResult,
    ToolType,
    ActionType,
    VerificationStatus,
    StepResult,
)

log = logging.getLogger(__name__)

#Main entry point

def verify_step(step: Step, tool_result: ToolResult) -> StepResult:
    """
    Given the Step that was attempted and the ToolResult that came back,
    decide whether it succeeded, is uncertain, or failed.

    Decision priority:
      1. If tool_result.success is False → FAILED immediately, no further checks
      2. If requires_verification is False → trust the tool result
      3. Try the cheapest matching verifier for this action type
      4. If no specific verifier matches → UNCERTAIN (not FAILED)
    """
    start = time.monotonic()

    # Step 1 — tool itself reported failure
    if not tool_result.success:
        return _make_result(
            step,
            VerificationStatus.FAILED,
            f"Tool reported failure: {tool_result.error or tool_result.message}",
            start,
        )

    # Step 2 — verification not required for this step
    if not step.requires_verification:
        return _make_result(
            step,
            VerificationStatus.SUCCESS,
            "Verification skipped (not required for this step)",
            start,
        )

    # Step 3 — route to the right verifier
    status, message = _route_verification(step, tool_result)

    return _make_result(step, status, message, start)

# Router picks the right verification strategy

def _route_verification(
    step: Step,
    tool_result: ToolResult,
) -> tuple[VerificationStatus, str]:
    """
    Map action type → verification function.
    Falls through to UNCERTAIN if no specific check exists.
    """
    action = step.action

    # --- app open / focus ---
    if action in (ActionType.OPEN_APP, ActionType.FOCUS_APP):
        return _verify_app_open(step.target)

    # --- app close ---
    if action == ActionType.CLOSE_APP:
        return _verify_app_closed(step.target)

    # --- file operations ---
    if action in (ActionType.MOVE_FILE, ActionType.COPY_FILE):
        dst = tool_result.data.get("destination") if tool_result.data else None
        return _verify_path_exists(dst, "destination")

    if action == ActionType.RENAME_FILE:
        dst = tool_result.data.get("new_path") if tool_result.data else None
        return _verify_path_exists(dst, "renamed file")

    if action == ActionType.DELETE_FILE:
        src = tool_result.data.get("path") if tool_result.data else None
        return _verify_path_gone(src)

    if action in (ActionType.CREATE_FOLDER,):
        dst = tool_result.data.get("path") if tool_result.data else None
        return _verify_folder_exists(dst)

     # --- browser ---
    if action in (ActionType.NAVIGATE, ActionType.SEARCH_WEB):
        # BUGFIX (Phase 2.5b): get_first_result_url() and other future
        # browser actions can set "url" as the deliberately-found target
        # link, distinct from "current_url" (the page we're still on).
        # Prefer "url" when present so verification checks the actual
        # navigation target, not a stale page. Falls back to current_url
        # for actions that only ever report where we currently are.
        data = tool_result.data or {}
        expected_url = data.get("current_url")
        return _verify_browser_url(expected_url, step.target, action)

    if action == ActionType.CLICK_BEST_RESULT:
        return _verify_click_best_result(tool_result, step)

    # --- text typed into a field ---
    if action == ActionType.TYPE_TEXT:
        typed_value   = step.value or ""
        actual_value  = (tool_result.data or {}).get("field_value", "")
        return _verify_text_typed(typed_value, actual_value)

    # --- clipboard ---
    if action == ActionType.CLIPBOARD_COPY:
        return _verify_clipboard_has_content()
    
    if action == ActionType.WRITE_FILE:
        path = tool_result.data.get("path") if tool_result.data else None
        return _verify_path_exists(path, "written file")

    # --- wait / screenshot / system — trust the tool ---
    if action in (ActionType.WAIT, ActionType.SCREENSHOT, ActionType.VOLUME_SET):
        return VerificationStatus.SUCCESS, "Action type does not require verification"

    # After the existing action checks, add:
    if action in (
        ActionType.SPOTIFY_PLAY,
        ActionType.SPOTIFY_PAUSE,
        ActionType.SPOTIFY_NEXT,
        ActionType.SPOTIFY_PLAYLIST,
    ):
        return VerificationStatus.SUCCESS, "Spotify action completed"

    # --- fallback: uncertain but not failed ---
    log.debug(
        "No specific verifier for action=%s, returning UNCERTAIN", action.value
    )
    return (
        VerificationStatus.UNCERTAIN,
        f"No specific verifier for '{action.value}' — tool reported success",
    )

# Individual verifiers

def _verify_app_open(
    app_name: str,
    timeout: float = ELEMENT_TIMEOUT,
) -> tuple[VerificationStatus, str]:
    """
    Poll the process list + window titles for up to `timeout` seconds.
    Cheap — no screenshot needed.
    """
    deadline = time.monotonic() + timeout
    name_lower = app_name.lower()

    while time.monotonic() < deadline:
        # Check running processes
        for proc in psutil.process_iter(["name", "status"]):
            try:
                if name_lower in proc.info["name"].lower():
                    return (
                        VerificationStatus.SUCCESS,
                        f"Process '{proc.info['name']}' is running",
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # Also try window title via uiautomation if available
        win_result = _check_window_title(app_name)
        if win_result:
            return VerificationStatus.SUCCESS, f"Window '{win_result}' is visible"

        time.sleep(0.4)

    return (
        VerificationStatus.FAILED,
        f"App '{app_name}' did not appear within {timeout}s",
    )


def _verify_app_closed(app_name: str) -> tuple[VerificationStatus, str]:
    """
    Check the main app process is no longer running.
    Uses fuzzy matching — skips helper/widget processes.
    """
    name_lower = app_name.lower()

    # These are known Spotify helper processes that stay running
    # even after the main app closes — ignore them
    known_helpers = {
        "spotify": [
            "spotifywidgetprovider",
            "spotifymigrator",
            "spotifycrashservice",
        ]
    }

    helpers = known_helpers.get(name_lower, [])

    for proc in psutil.process_iter(["name"]):
        try:
            proc_name = (proc.info["name"] or "").lower()
            if name_lower in proc_name:
                # Skip if it's a known helper process
                is_helper = any(h in proc_name for h in helpers)
                if not is_helper:
                    return (
                        VerificationStatus.FAILED,
                        f"Process '{proc.info['name']}' is still running",
                    )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return VerificationStatus.SUCCESS, f"'{app_name}' main process closed"

def _verify_path_exists(
    path: Optional[str],
    label: str = "path",
) -> tuple[VerificationStatus, str]:
    if not path:
        return (
            VerificationStatus.UNCERTAIN,
            f"No {label} returned by tool — cannot verify",
        )
    if os.path.exists(path):
        return VerificationStatus.SUCCESS, f"{label.capitalize()} exists: {path}"
    return VerificationStatus.FAILED, f"{label.capitalize()} not found: {path}"


def _verify_path_gone(path: Optional[str]) -> tuple[VerificationStatus, str]:
    if not path:
        return VerificationStatus.UNCERTAIN, "No path returned — cannot verify deletion"
    if not os.path.exists(path):
        return VerificationStatus.SUCCESS, f"File no longer exists: {path}"
    return VerificationStatus.FAILED, f"File still exists after delete: {path}"


def _verify_folder_exists(path: Optional[str]) -> tuple[VerificationStatus, str]:
    if not path:
        return VerificationStatus.UNCERTAIN, "No folder path returned"
    if os.path.isdir(path):
        return VerificationStatus.SUCCESS, f"Folder exists: {path}"
    return VerificationStatus.FAILED, f"Folder not found: {path}"


def _verify_browser_url(
        current_url: Optional[str],
        expected_target: str,
        action: Optional["ActionType"] = None,
    ) -> tuple[VerificationStatus, str]:
        """
        action: which browser action produced this URL. Used to decide
        whether landing on a search engine domain counts as success.
 
        BUGFIX (Phase 2.5b): the search-engine-domain check used to apply
        unconditionally to BOTH search_web and navigate steps. That meant
        a navigate step whose target was a real article/video URL (e.g.
        resolved from {{browser_url}}) could "verify" successfully even
        when the browser never left the search results page — exactly
        the silent-failure pattern that let a broken plan report all-green
        checkmarks while doing nothing useful. The search-engine-domain
        shortcut now only applies when action == SEARCH_WEB, since landing
        on duckduckgo.com/google.com IS the correct, intended outcome for
        that specific action — for navigate, it almost always means the
        navigation failed to go anywhere.
        """
        if not current_url:
            return (
                VerificationStatus.UNCERTAIN,
                "Browser did not return current URL",
            )
 
        def normalize(s: str) -> str:
            return (
                s.lower()
                .replace(" ", "")
                .replace("+", "")
                .replace("%20", "")
                .replace("-", "")
                .rstrip("/")
            )
 
        target_norm  = normalize(expected_target)
        current_norm = normalize(current_url)
 
        target_words = [
            w for w in target_norm.split()
            if len(w) > 3
        ] if " " in expected_target else [target_norm]
 
        is_search_engine_domain = (
            "duckduckgo.com" in current_url or "google.com" in current_url
        )
 
        if is_search_engine_domain:
            if action == ActionType.SEARCH_WEB:
                return (
                    VerificationStatus.SUCCESS,
                    f"Search page loaded: {current_url}"
                )
            # For navigate (or any other action), landing on a search
            # engine domain when we expected to go somewhere else is a
            # signal the navigation didn't actually happen — surface it
            # as UNCERTAIN rather than a false SUCCESS, so retry/replan
            # logic gets a chance to react instead of moving on silently.
            return (
                VerificationStatus.UNCERTAIN,
                f"Still on search engine page, expected to navigate to "
                f"'{expected_target}': {current_url}"
            )
 
        if target_norm in current_norm or current_norm in target_norm:
            return VerificationStatus.SUCCESS, f"Browser is at: {current_url}"
 
        return (
            VerificationStatus.UNCERTAIN,
            f"URL may be correct: {current_url}",
        )


def _verify_click_best_result(
    tool_result: ToolResult,
    step: Step,
) -> tuple[VerificationStatus, str]:
    if not tool_result.success:
        return VerificationStatus.FAILED, f"Tool reported failure: {tool_result.error or tool_result.message}"

    data = tool_result.data or {}
    current_url = data.get("current_url", "")
    page_title = data.get("page_title", "")
    attempted_target = data.get("matched_title", "")

    # Dedicated check for YouTube video clicks. Scoped to youtube.com
    # specifically — "/watch" alone isn't a safe signal (e.g. a
    # "watchlist" page on an unrelated site would wrongly get held to
    # the strict "must have a <video> tag" bar otherwise).
    if "youtube.com" in current_url and "/watch" in current_url:
        try:
            from src.graph import _get_browser_instance
            bt = _get_browser_instance()
            if hasattr(bt, "_page") and bt._page:
                video_count = bt._page.locator("video").count()
                if video_count == 0:
                    return VerificationStatus.FAILED, f"Expected video element on {current_url}, but none found."
                
                return VerificationStatus.SUCCESS, f"Verified YouTube video loaded: {page_title}"
        except Exception as e:
            return VerificationStatus.UNCERTAIN, f"Could not verify video element: {e}"

    # Generic check for other sites
    if current_url:
        return VerificationStatus.SUCCESS, f"Navigated to '{page_title}' at {current_url}"

    return VerificationStatus.UNCERTAIN, "Could not verify click_best_result"


def _verify_text_typed(
    expected: str,
    actual: str,
) -> tuple[VerificationStatus, str]:
    if not actual:
        # Tool didn't return a field read-back — can't be sure
        return (
            VerificationStatus.UNCERTAIN,
            "Field value not returned by tool — cannot confirm text was typed",
        )
    if expected.strip().lower() in actual.strip().lower():
        return VerificationStatus.SUCCESS, f"Field contains expected text"
    return (
        VerificationStatus.FAILED,
        f"Expected '{expected}' in field, found '{actual}'",
    )


def _verify_clipboard_has_content() -> tuple[VerificationStatus, str]:
    try:
        import pyperclip
        content = pyperclip.paste()
        if content and content.strip():
            return VerificationStatus.SUCCESS, "Clipboard has content"
        return VerificationStatus.UNCERTAIN, "Clipboard appears empty"
    except Exception as e:
        return VerificationStatus.UNCERTAIN, f"Could not read clipboard: {e}"


# Window title helper

def _check_window_title(app_name: str) -> Optional[str]:
    """
    Try to find a top-level window whose title contains app_name.
    Returns the window title string if found, None otherwise.
    Safe to call even if uiautomation is not installed yet.
    """
    try:
        import uiautomation as auto
        name_lower = app_name.lower()
        desktop = auto.GetRootControl()
        for window in desktop.GetChildren():
            title = window.Name or ""
            if name_lower in title.lower():
                return title
    except Exception:
        pass
    return None

# Helper

def _make_result(
    step:    Step,
    status:  VerificationStatus,
    message: str,
    start:   float,
) -> StepResult:
    elapsed_ms = int((time.monotonic() - start) * 1000)
    log.info(
        "Step %d verification → %s | %s (took %dms)",
        step.step_number,
        status.value,
        message,
        elapsed_ms,
    )
    return StepResult(
        step_number = step.step_number,
        status      = status,
        message     = message,
        tool_used   = step.tool,
        duration_ms = elapsed_ms,
    )