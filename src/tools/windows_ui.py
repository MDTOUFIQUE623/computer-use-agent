import time
import logging
from typing import Optional

import uiautomation as auto
import pyautogui
import psutil

from src.models import ToolResult
from src.config import APP_OPEN_TIMEOUT, ELEMENT_TIMEOUT, ACTION_COOLDOWN

log = logging.getLogger(__name__)

class WindowsUITools:
    """
    Native Windows app control via UI Automation.

    Usage:
        ui = WindowsUITools()
        result = ui.open_app("Notepad")
        result = ui.type_into_focused("Hello world")
        result = ui.click_element_by_name("File")
    """

    # -----------------------------------------------------------------------
    # App lifecycle
    # -----------------------------------------------------------------------

    def open_app(self, app_name: str) -> ToolResult:
        """
        Open an application using the Windows Start menu search.

        Steps:
          1. Press the Windows key to open Start
          2. Type the app name
          3. Press Enter to launch the top result
          4. Wait for the app window to appear

        This works for any installed app without needing its path.
        """
        start = time.monotonic()
        try:
            # Check if already open first
            existing = self._find_window_fuzzy(app_name)
            if existing:
                existing.SetFocus()
                return ToolResult(
                    success=True,
                    message=f"'{app_name}' was already open — brought to focus",
                    data={"window_title": existing.Name},
                    duration_ms=_ms(start)
                )

            # Open Start menu and search
            pyautogui.press("win")
            time.sleep(0.6)  # Start menu animation
            pyautogui.write(app_name, interval=0.05)
            time.sleep(0.8)  # Search results load
            pyautogui.press("enter")

            # Wait for the window to appear
            window = self._wait_for_window(app_name, timeout=APP_OPEN_TIMEOUT)

            if window:
                return ToolResult(
                    success=True,
                    message=f"Opened '{app_name}' successfully",
                    data={"window_title": window.Name},
                    duration_ms=_ms(start)
                )

            return ToolResult(
                success=False,
                message=f"'{app_name}' did not open within {APP_OPEN_TIMEOUT}s",
                error="WindowNotFound",
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("open_app failed for '%s': %s", app_name, e)
            return ToolResult(
                success=False,
                message=f"Failed to open '{app_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def close_app(self, app_name: str) -> ToolResult:
        """
        Close an application by finding its window and closing it.
        Uses WindowPattern.Close() which is the correct uiautomation approach.
        Falls back to Alt+F4 if WindowPattern is unavailable.
        """
        start = time.monotonic()
        try:
            window = self._find_window_fuzzy(app_name)

            if not window:
                return ToolResult(
                    success=False,
                    message=f"No window found for '{app_name}'",
                    error="WindowNotFound",
                    duration_ms=_ms(start)
                )

            window_title = window.Name

            # Primary method — WindowPattern.Close()
            closed = False
            try:
                window_pattern = window.GetWindowPattern()
                if window_pattern:
                    window_pattern.Close()
                    closed = True
                    time.sleep(0.5)
            except Exception as e:
                log.debug("WindowPattern.Close() failed: %s — trying Alt+F4", e)

            # Fallback — Alt+F4
            if not closed:
                window.SetFocus()
                time.sleep(0.2)
                pyautogui.hotkey("alt", "f4")
                time.sleep(0.5)

            # Verify it actually closed
            still_open = self._find_window_fuzzy(app_name)
            if still_open:
                return ToolResult(
                    success=False,
                    message=f"'{app_name}' is still open after close attempt",
                    error="CloseFailure",
                    duration_ms=_ms(start)
                )

            return ToolResult(
                success=True,
                message=f"Closed '{window_title}'",
                data={"window_title": window_title},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("close_app failed for '%s': %s", app_name, e)
            return ToolResult(
                success=False,
                message=f"Failed to close '{app_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def focus_app(self, app_name: str) -> ToolResult:
        """
        Bring an already-open application to the foreground.
        """
        start = time.monotonic()
        try:
            window = self._find_window_fuzzy(app_name)

            if not window:
                return ToolResult(
                    success=False,
                    message=f"No open window found for '{app_name}'",
                    error="WindowNotFound",
                    duration_ms=_ms(start)
                )

            # SetActive brings it to foreground, SetFocus gives it keyboard focus
            try:
                window.SetActive()
            except Exception:
                pass
            window.SetFocus()
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Focused '{window.Name}'",
                data={"window_title": window.Name},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("focus_app failed for '%s': %s", app_name, e)
            return ToolResult(
                success=False,
                message=f"Failed to focus '{app_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def is_app_open(self, app_name: str) -> ToolResult:
        """
        Check whether an app window is currently open.
        Returns success=True if found, success=False if not.
        Does not raise — safe to call as a pre-check.
        """
        start = time.monotonic()
        window = self._find_window_fuzzy(app_name)

        if window:
            return ToolResult(
                success=True,
                message=f"'{app_name}' is open",
                data={"window_title": window.Name},
                duration_ms=_ms(start)
            )

        return ToolResult(
            success=False,
            message=f"'{app_name}' is not open",
            duration_ms=_ms(start)
        )

    # -----------------------------------------------------------------------
    # Element interaction
    # -----------------------------------------------------------------------

    def click_element_by_name(
        self,
        element_name: str,
        app_name: Optional[str] = None,
        control_type: Optional[str] = None,
    ) -> ToolResult:
        """
        Find a UI element by its name and click it.

        app_name:     if given, search only inside that app's window
        control_type: narrow search — "Button", "MenuItem", "ListItem",
                      "CheckBox", "RadioButton", "TabItem", "TreeItem"
                      Leave None to search all control types.

        Example:
            ui.click_element_by_name("File", app_name="Notepad")
            ui.click_element_by_name("OK", control_type="Button")
        """
        start = time.monotonic()
        try:
            # Determine search root
            root = self._get_search_root(app_name)
            if root is None:
                return ToolResult(
                    success=False,
                    message=f"Could not find window for '{app_name}'",
                    error="WindowNotFound",
                    duration_ms=_ms(start)
                )

            # Find the element
            element = self._find_element(
                root, element_name, control_type, timeout=ELEMENT_TIMEOUT
            )

            if element is None:
                return ToolResult(
                    success=False,
                    message=(
                        f"Element '{element_name}' not found"
                        + (f" in '{app_name}'" if app_name else "")
                    ),
                    error="ElementNotFound",
                    duration_ms=_ms(start)
                )

            element.Click()
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Clicked '{element_name}'",
                data={"element_name": element_name, "control_type": element.ControlTypeName},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("click_element_by_name failed for '%s': %s", element_name, e)
            return ToolResult(
                success=False,
                message=f"Failed to click '{element_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def double_click_element_by_name(
        self,
        element_name: str,
        app_name: Optional[str] = None,
    ) -> ToolResult:
        """
        Double-click a UI element. Used for opening files/items in lists.
        """
        start = time.monotonic()
        try:
            root    = self._get_search_root(app_name)
            element = self._find_element(root, element_name, None, ELEMENT_TIMEOUT)

            if element is None:
                return ToolResult(
                    success=False,
                    message=f"Element '{element_name}' not found",
                    error="ElementNotFound",
                    duration_ms=_ms(start)
                )

            element.DoubleClick()
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Double-clicked '{element_name}'",
                data={"element_name": element_name},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("double_click_element failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to double-click '{element_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def type_into_element(
        self,
        element_name: str,
        text: str,
        app_name: Optional[str] = None,
        clear_first: bool = True,
    ) -> ToolResult:
        """
        Find an input field by name and type text into it.

        clear_first=True clears existing content before typing.
        Uses SetValue for edit controls (more reliable than SendKeys for forms).
        Falls back to SendKeys if SetValue is not supported.
        """
        start = time.monotonic()
        try:
            root    = self._get_search_root(app_name)
            element = self._find_element(
                root, element_name, "Edit", ELEMENT_TIMEOUT
            )

            # If not found as Edit, try any control with that name
            if element is None:
                element = self._find_element(
                    root, element_name, None, ELEMENT_TIMEOUT
                )

            if element is None:
                return ToolResult(
                    success=False,
                    message=f"Input field '{element_name}' not found",
                    error="ElementNotFound",
                    duration_ms=_ms(start)
                )

            element.SetFocus()
            time.sleep(0.2)

            if clear_first:
                # Select all and delete
                pyautogui.hotkey("ctrl", "a")
                time.sleep(0.1)
                pyautogui.press("delete")
                time.sleep(0.1)

            # Try SetValue first (works for standard edit controls)
            try:
                value_pattern = element.GetValuePattern()
                if value_pattern:
                    value_pattern.SetValue(text)
                    actual = value_pattern.Value
                    return ToolResult(
                        success=True,
                        message=f"Typed into '{element_name}'",
                        data={
                            "element_name": element_name,
                            "field_value": actual
                        },
                        duration_ms=_ms(start)
                    )
            except Exception:
                pass  # Fall through to SendKeys

            # Fallback: SendKeys (works for custom/non-standard inputs)
            element.SendKeys(text)
            time.sleep(0.2)

            return ToolResult(
                success=True,
                message=f"Typed into '{element_name}' via SendKeys",
                data={"element_name": element_name, "field_value": text},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("type_into_element failed for '%s': %s", element_name, e)
            return ToolResult(
                success=False,
                message=f"Failed to type into '{element_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def type_into_focused(self, text: str) -> ToolResult:
        """
        Type text into whatever element currently has focus.
        Use this after clicking a field manually when you know focus is set.
        """
        start = time.monotonic()
        try:
            pyautogui.write(text, interval=0.03)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Typed text into focused element",
                data={"field_value": text},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("type_into_focused failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to type into focused element",
                error=str(e),
                duration_ms=_ms(start)
            )

    def press_key(self, key: str) -> ToolResult:
        """
        Press a keyboard key or combination.

        Single key:  "enter", "escape", "tab", "f5"
        Combination: "ctrl+s", "alt+f4", "ctrl+shift+n"
        """
        start = time.monotonic()
        try:
            if "+" in key:
                keys = [k.strip() for k in key.split("+")]
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(key.strip())

            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Pressed '{key}'",
                data={"key": key},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("press_key failed for '%s': %s", key, e)
            return ToolResult(
                success=False,
                message=f"Failed to press '{key}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def scroll_in_app(
        self,
        app_name: str,
        direction: str = "down",
        clicks: int = 3,
    ) -> ToolResult:
        """
        Scroll inside an app window.
        direction: "up" or "down"
        clicks: number of scroll notches
        """
        start = time.monotonic()
        try:
            window = self._find_window_fuzzy(app_name)

            if not window:
                return ToolResult(
                    success=False,
                    message=f"Window '{app_name}' not found",
                    error="WindowNotFound",
                    duration_ms=_ms(start)
                )

            window.SetFocus()
            time.sleep(0.2)

            amount = -clicks if direction == "down" else clicks
            pyautogui.scroll(amount)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Scrolled {direction} {clicks} times in '{app_name}'",
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("scroll_in_app failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to scroll in '{app_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Reading UI state
    # -----------------------------------------------------------------------

    def get_element_text(
        self,
        element_name: str,
        app_name: Optional[str] = None,
    ) -> ToolResult:
        """
        Read the current text/value of a UI element.
        Useful for reading what's in a text field after typing,
        or reading a label's current value.
        """
        start = time.monotonic()
        try:
            root    = self._get_search_root(app_name)
            element = self._find_element(root, element_name, None, ELEMENT_TIMEOUT)

            if element is None:
                return ToolResult(
                    success=False,
                    message=f"Element '{element_name}' not found",
                    error="ElementNotFound",
                    duration_ms=_ms(start)
                )

            # Try ValuePattern first (edit controls)
            try:
                vp = element.GetValuePattern()
                if vp:
                    return ToolResult(
                        success=True,
                        message=f"Read value of '{element_name}'",
                        data={"text": vp.Value, "element_name": element_name},
                        duration_ms=_ms(start)
                    )
            except Exception:
                pass

            # Fall back to Name property
            text = element.Name or ""
            return ToolResult(
                success=True,
                message=f"Read name of '{element_name}'",
                data={"text": text, "element_name": element_name},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("get_element_text failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to read '{element_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_window_title(self, app_name: str) -> ToolResult:
        """
        Get the full title of an app's window.
        Useful for checking what document is open in an editor.
        """
        start = time.monotonic()
        window = self._find_window_fuzzy(app_name)

        if window:
            return ToolResult(
                success=True,
                message=f"Got window title for '{app_name}'",
                data={"window_title": window.Name},
                duration_ms=_ms(start)
            )

        return ToolResult(
            success=False,
            message=f"No window found for '{app_name}'",
            error="WindowNotFound",
            duration_ms=_ms(start)
        )

    def list_open_windows(self) -> ToolResult:
        """
        List all currently visible top-level windows.
        Useful for Brain to understand what's currently on screen.
        """
        start = time.monotonic()
        try:
            desktop = auto.GetRootControl()
            windows = []

            for child in desktop.GetChildren():
                if child.Name and child.Name.strip():
                    windows.append({
                        "title":        child.Name,
                        "control_type": child.ControlTypeName,
                    })

            return ToolResult(
                success=True,
                message=f"Found {len(windows)} open window(s)",
                data={"windows": windows},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("list_open_windows failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to list windows",
                error=str(e),
                duration_ms=_ms(start)
            )

    def wait_for_element(
        self,
        element_name: str,
        app_name: Optional[str] = None,
        timeout: float = ELEMENT_TIMEOUT,
    ) -> ToolResult:
        """
        Wait until a named element appears in the UI.
        Useful after clicking something that triggers a dialog or new panel.
        """
        start = time.monotonic()
        root    = self._get_search_root(app_name)
        element = self._find_element(root, element_name, None, timeout)

        if element:
            return ToolResult(
                success=True,
                message=f"Element '{element_name}' appeared",
                data={"element_name": element_name},
                duration_ms=_ms(start)
            )

        return ToolResult(
            success=False,
            message=f"Element '{element_name}' did not appear within {timeout}s",
            error="Timeout",
            duration_ms=_ms(start)
        )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _find_window_fuzzy(self, app_name: str) -> Optional[auto.Control]:
        """
        Search all top-level windows for one whose title contains app_name.
        Case-insensitive. Returns the first match or None.
        """
        name_lower = app_name.lower()
        desktop    = auto.GetRootControl()

        for window in desktop.GetChildren():
            title = (window.Name or "").lower()
            if name_lower in title:
                return window

        return None

    def _wait_for_window(
        self,
        app_name: str,
        timeout: float = APP_OPEN_TIMEOUT,
    ) -> Optional[auto.Control]:
        """
        Poll for a window matching app_name until timeout.
        Returns the window control or None.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            window = self._find_window_fuzzy(app_name)
            if window:
                return window
            time.sleep(0.4)

        return None

    def _get_search_root(
        self,
        app_name: Optional[str],
    ) -> auto.Control:
        """
        Return the window control for app_name if given,
        otherwise return the desktop root.
        """
        if app_name:
            window = self._find_window_fuzzy(app_name)
            if window:
                return window
            # Return desktop as fallback — element search will still work
            # but may find elements in other apps
            log.warning(
                "Window '%s' not found — searching from desktop root", app_name
            )

        return auto.GetRootControl()

    def _find_element(
        self,
        root: auto.Control,
        name: str,
        control_type: Optional[str],
        timeout: float,
    ) -> Optional[auto.Control]:
        """
        Search the subtree of root for an element matching name and
        optionally control_type. Polls until timeout.

        control_type examples: "Button", "Edit", "MenuItem",
                               "ListItem", "CheckBox", "TabItem"
        """
        deadline   = time.monotonic() + timeout
        name_lower = name.lower()

        while time.monotonic() < deadline:
            try:
                # Walk the full subtree up to depth 8
                for element in root.GetChildren():
                    found = self._search_subtree(
                        element, name_lower, control_type, depth=8
                    )
                    if found:
                        return found
            except Exception:
                pass

            time.sleep(0.3)

        return None

    def _search_subtree(
        self,
        control: auto.Control,
        name_lower: str,
        control_type: Optional[str],
        depth: int,
    ) -> Optional[auto.Control]:
        """
        Recursive depth-first search through the control tree.
        """
        if depth <= 0:
            return None

        # Check this control
        ctrl_name = (control.Name or "").lower()
        type_ok   = (
            control_type is None
            or control.ControlTypeName == control_type
        )

        if name_lower in ctrl_name and type_ok:
            return control

        # Check children
        try:
            for child in control.GetChildren():
                result = self._search_subtree(
                    child, name_lower, control_type, depth - 1
                )
                if result:
                    return result
        except Exception:
            pass

        return None


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)