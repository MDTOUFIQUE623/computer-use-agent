import time
import logging
import json
from typing import Optional

import pyautogui
import psutil
import pyperclip
import requests

from src.models import ToolResult
from src.config import (
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    NOTION_API_KEY,
    ACTION_COOLDOWN,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Spotify
# ---------------------------------------------------------------------------

class SpotifyTools:
    """
    Spotify control via Windows media keys.
    Works with Spotify desktop app — no API keys needed for basic control.

    For playlist-level control (open specific playlist),
    Spotify Web API is used if credentials are configured.
    """

    def play(self) -> ToolResult:
        """Resume playback."""
        start = time.monotonic()
        try:
            pyautogui.press("playpause")
            time.sleep(ACTION_COOLDOWN)
            return ToolResult(
                success=True,
                message="Spotify: play/resume",
                data={"status": "playing"},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to play Spotify",
                error=str(e),
                duration_ms=_ms(start)
            )

    def pause(self) -> ToolResult:
        """Pause playback."""
        start = time.monotonic()
        try:
            pyautogui.press("playpause")
            time.sleep(ACTION_COOLDOWN)
            return ToolResult(
                success=True,
                message="Spotify: paused",
                data={"status": "paused"},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to pause Spotify",
                error=str(e),
                duration_ms=_ms(start)
            )

    def next_track(self) -> ToolResult:
        """Skip to next track."""
        start = time.monotonic()
        try:
            pyautogui.press("nexttrack")
            time.sleep(ACTION_COOLDOWN)
            return ToolResult(
                success=True,
                message="Spotify: skipped to next track",
                data={"status": "next"},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to skip track",
                error=str(e),
                duration_ms=_ms(start)
            )

    def previous_track(self) -> ToolResult:
        """Go to previous track."""
        start = time.monotonic()
        try:
            pyautogui.press("prevtrack")
            time.sleep(ACTION_COOLDOWN)
            return ToolResult(
                success=True,
                message="Spotify: went to previous track",
                data={"status": "previous"},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to go to previous track",
                error=str(e),
                duration_ms=_ms(start)
            )

    def volume_up(self, steps: int = 3) -> ToolResult:
        """
        Increase Spotify volume.
        steps: how many volume increments (each ~6.25%)
        """
        start = time.monotonic()
        try:
            for _ in range(steps):
                pyautogui.hotkey("ctrl", "up")
                time.sleep(0.1)
            return ToolResult(
                success=True,
                message=f"Spotify: volume up {steps} steps",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to raise volume",
                error=str(e),
                duration_ms=_ms(start)
            )

    def volume_down(self, steps: int = 3) -> ToolResult:
        """Decrease Spotify volume."""
        start = time.monotonic()
        try:
            for _ in range(steps):
                pyautogui.hotkey("ctrl", "down")
                time.sleep(0.1)
            return ToolResult(
                success=True,
                message=f"Spotify: volume down {steps} steps",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to lower volume",
                error=str(e),
                duration_ms=_ms(start)
            )

    def is_running(self) -> ToolResult:
        """Check if Spotify process is running."""
        start = time.monotonic()
        for proc in psutil.process_iter(["name"]):
            try:
                if "spotify" in proc.info["name"].lower():
                    return ToolResult(
                        success=True,
                        message="Spotify is running",
                        data={"status": "running"},
                        duration_ms=_ms(start)
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return ToolResult(
            success=False,
            message="Spotify is not running",
            data={"status": "not_running"},
            duration_ms=_ms(start)
        )

    def open_playlist_by_name(self, playlist_name: str) -> ToolResult:
        """
        Search for and open a playlist by name using Spotify's search.
        Uses keyboard shortcut Ctrl+L to focus search, then types query.
        Works without Web API credentials.
        """
        start = time.monotonic()
        try:
            # Focus Spotify search bar
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.5)

            # Type playlist name
            pyautogui.write(playlist_name, interval=0.05)
            time.sleep(0.8)

            # Press Enter to search
            pyautogui.press("enter")
            time.sleep(1.0)

            return ToolResult(
                success=True,
                message=f"Searched for playlist '{playlist_name}'",
                data={"status": "searched", "query": playlist_name},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("open_playlist_by_name failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to search for '{playlist_name}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_window_title(self) -> ToolResult:
        """
        Get Spotify window title.
        Delegates to get_current_state() which parses the title properly.
        Kept for backwards compatibility — prefer get_current_state() directly.
        """
        return self.get_current_state()
        
    def get_current_state(self) -> ToolResult:
        """Read Spotify state. Uses PID-based window finding."""
        start = time.monotonic()
        try:
            window_info = self._get_spotify_window_info()

            if not window_info:
                if self._is_spotify_process_running():
                    return ToolResult(
                        success=True,
                        message="Spotify running, window not readable",
                        data={
                            "is_playing":   False,
                            "track":        None,
                            "window_title": None,
                        },
                        duration_ms=_ms(start)
                    )
                return ToolResult(
                    success=False,
                    message="Spotify is not running",
                    error="NotRunning",
                    data={
                        "is_playing":   False,
                        "track":        None,
                        "window_title": None,
                    },
                    duration_ms=_ms(start)
                )

            hwnd, window_title = window_info
            return self._parse_spotify_title(window_title, start)

        except Exception as e:
            log.error("get_current_state failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to get Spotify state",
                error=str(e),
                data={
                    "is_playing":   False,
                    "track":        None,
                    "window_title": None,
                },
                duration_ms=_ms(start)
            )

    def _read_taskbar_button(self, app_name: str) -> Optional[str]:
        """
        Read the taskbar button text for an app.
        Taskbar buttons reflect the current window title reliably.
        Returns the button text or None if not found.
        """
        try:
            import uiautomation as auto

            # Find the taskbar
            taskbar = auto.TaskbarControl()
            if not taskbar:
                return None

            # Search through taskbar buttons
            name_lower = app_name.lower()
            for button in taskbar.GetChildren():
                btn_name = button.Name or ""
                if name_lower in btn_name.lower():
                    return btn_name

            # Also try the running apps section
            desktop = auto.GetRootControl()
            for window in desktop.GetChildren():
                if "taskbar" in (window.Name or "").lower():
                    for child in window.GetChildren():
                        child_name = child.Name or ""
                        if name_lower in child_name.lower():
                            return child_name

        except Exception as e:
            log.debug("_read_taskbar_button failed: %s", e)

        return None

    def _parse_spotify_title(
        self,
        title: str,
        start: float,
    ) -> ToolResult:
        """
        Parse Spotify window title into structured state.

        Windows title format: "Artist - Song - Spotify" or just "Artist - Song"
        when actively playing. "Spotify Premium" / "Spotify Free" when idle.
        """
        name = title.strip()

        is_playing = (
            " - " in name
            and name not in ("Spotify Premium", "Spotify Free", "Spotify")
            and "spotify" not in name.lower()
        )

        track_info = None
        if is_playing:
            # Format is "Artist - Song" on Windows
            parts = name.split(" - ")
            track_info = {
                "artist": parts[0].strip() if len(parts) > 0 else None,
                "song":   parts[1].strip() if len(parts) > 1 else None,
            }

        return ToolResult(
            success=True,
            message=(
                f"Playing: {track_info['song']} "
                f"by {track_info['artist']}"
                if track_info
                else "Spotify open, nothing playing"
            ),
            data={
                "is_playing":   is_playing,
                "track":        track_info,
                "window_title": name,
            },
            duration_ms=_ms(start)
        )
    def _is_spotify_process_running(self) -> bool:
        """Quick process check — doesn't need uiautomation."""
        for proc in psutil.process_iter(["name"]):
            try:
                if "spotify" in proc.info["name"].lower():
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    
    
    def get_now_playing_ocr(self) -> ToolResult:
        """
        Read currently playing track from Spotify.
        Uses PID-based window finding — works regardless of window title.
        """
        start = time.monotonic()
        try:
            import win32gui
            import win32process

            # Find Spotify window by PID — not by title
            window_info = self._get_spotify_window_info()

            if not window_info:
                if self._is_spotify_process_running():
                    return ToolResult(
                        success=True,
                        message="Spotify running but no visible window found",
                        data={
                            "is_playing":   False,
                            "track":        None,
                            "window_title": None,
                            "raw_bar_text": "",
                            "lines":        [],
                        },
                        duration_ms=_ms(start)
                    )
                return ToolResult(
                    success=False,
                    message="Spotify is not running",
                    error="NotRunning",
                    data={
                        "is_playing":   False,
                        "track":        None,
                        "window_title": None,
                    },
                    duration_ms=_ms(start)
                )

            hwnd, window_title = window_info

            # Parse track from window title first — most reliable
            # Title format when playing: "Song - Artist"
            # Title format when idle: "Spotify Premium" / "Spotify Free"
            is_playing = (
                " - " in window_title
                and window_title not in ("Spotify Premium", "Spotify Free")
                and "spotify" not in window_title.lower()
            )

            track_from_title = None
            # Find this block in get_now_playing_ocr and fix it
            if is_playing:
                parts = window_title.split(" - ")
                track_from_title = {
                    "artist": parts[0].strip() if len(parts) > 0 else None,
                    "song":   parts[1].strip() if len(parts) > 1 else None,
                }

            # If we got track from title, return immediately — no OCR needed
            if track_from_title:
                return ToolResult(
                    success=True,
                    message=(
                        f"Playing: {track_from_title['song']} "
                        f"by {track_from_title['artist']}"
                    ),
                    data={
                        "is_playing":   True,
                        "track":        track_from_title,
                        "window_title": window_title,
                        "raw_bar_text": "",
                        "lines":        [],
                    },
                    duration_ms=_ms(start)
                )

            # Nothing playing — use OCR to confirm and read bar state
            from tools.ocr import _screenshot_region, _run_ocr, _words_to_text

            rect       = win32gui.GetWindowRect(hwnd)
            left, top, right, bottom = rect
            width      = right  - left
            height     = bottom - top

            if width <= 0 or height <= 0:
                return ToolResult(
                    success=True,
                    message="Spotify open, nothing playing",
                    data={
                        "is_playing":   False,
                        "track":        None,
                        "window_title": window_title,
                        "raw_bar_text": text,
                        "lines":        lines,  # will be empty after filtering
                    },
                    duration_ms=_ms(start)
                )

            # Fixed height now-playing bar at bottom of window
            bar_height = 90
            bar_top    = bottom - bar_height

            # Safety clamp
            screen_w, screen_h = pyautogui.size()
            bar_top  = max(0, min(bar_top, screen_h - bar_height))
            left     = max(0, min(left,    screen_w - width))
            width    = min(width, screen_w - left)

            image = _screenshot_region(left, bar_top, width, bar_height)
            words = _run_ocr(image, offset_x=left, offset_y=bar_top)
            text  = _words_to_text(words)

            # ------------------------------------------------------------------
            # Clean OCR output
            # ------------------------------------------------------------------
            import re

            raw_lines = [l.strip() for l in text.splitlines() if l.strip()]

            lines = [
                l for l in raw_lines
                if len(l) > 3                             # ignore 1-3 character noise
                and not re.match(r'^[^aeiouAEIOU]+$', l)  # ignore strings with no vowels
                and re.search(r'[a-zA-Z]', l)             # must contain at least one letter
            ]
            
            return ToolResult(
                success=True,
                message="Spotify open, nothing playing",
                data={
                    "is_playing":   False,
                    "track":        None,
                    "window_title": window_title,
                    "raw_bar_text": text,
                    "lines":        lines,
                },
                duration_ms=_ms(start)
            )

        except ImportError:
            return self.get_current_state()
        except Exception as e:
            log.error("get_now_playing_ocr failed: %s", e)
            return self.get_current_state()

    def _get_now_playing_ocr_fallback(self, start: float) -> ToolResult:
        """Fallback when win32gui unavailable — uses uiautomation bounds."""
        try:
            from src.tools.ocr import (
                _get_window_bounds, _screenshot_region,
                _run_ocr, _words_to_text
            )

            bounds = _get_window_bounds("Spotify")
            if not bounds:
                return self.get_current_state()

            left, top, right, bottom = bounds
            width      = right  - left
            height     = bottom - top
            bar_top    = top  + int(height * 0.85)
            bar_height = int(height * 0.15)

            image = _screenshot_region(left, bar_top, width, bar_height)
            words = _run_ocr(image, offset_x=left, offset_y=bar_top)
            text  = _words_to_text(words)
            lines = [l.strip() for l in text.splitlines() if l.strip()]

            return ToolResult(
                success=True,
                message=f"Now playing (fallback): {lines[0] if lines else 'unknown'}",
                data={
                    "is_playing":   bool(lines),
                    "raw_bar_text": text,
                    "lines":        lines,
                    "track":        None,
                    "window_title": "spotify",
                },
                duration_ms=_ms(start)
            )
        except Exception as e:
            return self.get_current_state()
    
    def get_now_playing_api(self) -> ToolResult:
        """
        Get currently playing track via Spotify Web API.
        Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env
        Falls back to window title parsing if not configured.
        """
        start = time.monotonic()

        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            # Fall back to window title method
            return self.get_current_state()

        try:
            token = self._get_access_token()
            if not token:
                return self.get_current_state()

            response = requests.get(
                "https://api.spotify.com/v1/me/player/currently-playing",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )

            if response.status_code == 204:
                return ToolResult(
                    success=True,
                    message="Nothing currently playing",
                    data={"is_playing": False, "track": None},
                    duration_ms=_ms(start)
                )

            response.raise_for_status()
            data = response.json()
            item = data.get("item", {})

            track_info = {
                "song":     item.get("name"),
                "artist":   ", ".join(
                    a["name"] for a in item.get("artists", [])
                ),
                "album":    item.get("album", {}).get("name"),
                "duration": item.get("duration_ms"),
                "progress": data.get("progress_ms"),
            }

            return ToolResult(
                success=True,
                message=f"Playing: {track_info['song']} by {track_info['artist']}",
                data={
                    "is_playing": data.get("is_playing", False),
                    "track":      track_info,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("get_now_playing_api failed: %s", e)
            # Always fall back to window title
            return self.get_current_state()
        
    def _get_spotify_window_info(self) -> Optional[tuple[int, str]]:
        """
        Find Spotify's main window handle and title reliably.
        
        Strategy:
        1. Find Spotify PIDs via psutil
        2. Match window handles to those PIDs via win32gui
        3. Return (hwnd, title) of the main visible window
        
        This works regardless of what the window title says —
        when playing, Spotify title is "Song - Artist" not "Spotify".
        """
        import win32gui
        import win32process

        # Step 1 — collect all Spotify PIDs
        spotify_pids = set()
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if "spotify" in proc.info["name"].lower():
                    spotify_pids.add(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not spotify_pids:
            return None

        # Step 2 — find windows belonging to Spotify PIDs
        spotify_windows = []

        def callback(hwnd, results):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in spotify_pids:
                    results.append((hwnd, title))
            except Exception:
                pass

        win32gui.EnumWindows(callback, spotify_windows)

        if not spotify_windows:
            return None

        # Step 3 — pick the most meaningful window
        # Prefer windows with " - " in title (playing state)
        # Fall back to any Spotify window
        for hwnd, title in spotify_windows:
            if " - " in title:
                return (hwnd, title)

        return spotify_windows[0]


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

class NotionTools:
    """
    Notion REST API integration.
    Requires NOTION_API_KEY in .env

    Get your API key: https://www.notion.so/my-integrations
    Make sure your integration has access to the pages you want to use.
    """

    _BASE_URL = "https://api.notion.com/v1"
    _VERSION  = "2022-06-28"

    def __init__(self):
        self._api_key = NOTION_API_KEY
        self._headers = {
            "Authorization":  f"Bearer {self._api_key}",
            "Content-Type":   "application/json",
            "Notion-Version": self._VERSION,
        }

    def _is_configured(self) -> bool:
        return bool(self._api_key)

    def create_page(
        self,
        parent_page_id: str,
        title:          str,
        content:        Optional[str] = None,
    ) -> ToolResult:
        """
        Create a new Notion page as a child of parent_page_id.

        parent_page_id: the ID of the parent page
                        (from the URL: notion.so/Page-Title-{ID})
        title:          page title
        content:        optional plain text content for the first paragraph

        Returns the new page's ID and URL.
        """
        start = time.monotonic()

        if not self._is_configured():
            return ToolResult(
                success=False,
                message="Notion API key not configured in .env",
                error="NotConfigured"
            )

        try:
            # Build page body
            children = []
            if content:
                children.append({
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": content}
                        }]
                    }
                })

            payload = {
                "parent": {
                    "type":    "page_id",
                    "page_id": parent_page_id,
                },
                "properties": {
                    "title": {
                        "title": [{
                            "type": "text",
                            "text": {"content": title}
                        }]
                    }
                },
                "children": children,
            }

            response = requests.post(
                f"{self._BASE_URL}/pages",
                headers=self._headers,
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            page_id  = data["id"]
            page_url = data.get("url", "")

            return ToolResult(
                success=True,
                message=f"Created Notion page '{title}'",
                data={
                    "page_id": page_id,
                    "url":     page_url,
                    "title":   title,
                },
                duration_ms=_ms(start)
            )

        except requests.HTTPError as e:
            log.error("Notion create_page HTTP error: %s", e)
            return ToolResult(
                success=False,
                message=f"Notion API error: {e.response.status_code}",
                error=str(e),
                duration_ms=_ms(start)
            )
        except Exception as e:
            log.error("Notion create_page failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to create Notion page",
                error=str(e),
                duration_ms=_ms(start)
            )

    def append_text(
        self,
        page_id: str,
        text:    str,
    ) -> ToolResult:
        """
        Append a paragraph block to an existing Notion page.

        page_id: the ID of the page to append to
        text:    plain text content to add
        """
        start = time.monotonic()

        if not self._is_configured():
            return ToolResult(
                success=False,
                message="Notion API key not configured in .env",
                error="NotConfigured"
            )

        try:
            payload = {
                "children": [{
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{
                            "type": "text",
                            "text": {"content": text}
                        }]
                    }
                }]
            }

            response = requests.patch(
                f"{self._BASE_URL}/blocks/{page_id}/children",
                headers=self._headers,
                json=payload,
                timeout=15,
            )
            response.raise_for_status()

            return ToolResult(
                success=True,
                message=f"Appended text to Notion page",
                data={"page_id": page_id},
                duration_ms=_ms(start)
            )

        except requests.HTTPError as e:
            log.error("Notion append_text HTTP error: %s", e)
            return ToolResult(
                success=False,
                message=f"Notion API error: {e.response.status_code}",
                error=str(e),
                duration_ms=_ms(start)
            )
        except Exception as e:
            log.error("Notion append_text failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to append to Notion page",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_page(self, page_id: str) -> ToolResult:
        """
        Retrieve a Notion page's metadata and title.
        """
        start = time.monotonic()

        if not self._is_configured():
            return ToolResult(
                success=False,
                message="Notion API key not configured",
                error="NotConfigured"
            )

        try:
            response = requests.get(
                f"{self._BASE_URL}/pages/{page_id}",
                headers=self._headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()

            # Extract title from properties
            title = ""
            props = data.get("properties", {})
            title_prop = props.get("title") or props.get("Name", {})
            title_list = title_prop.get("title", [])
            if title_list:
                title = title_list[0].get("plain_text", "")

            return ToolResult(
                success=True,
                message=f"Got Notion page: '{title}'",
                data={
                    "page_id": page_id,
                    "title":   title,
                    "url":     data.get("url", ""),
                },
                duration_ms=_ms(start)
            )

        except requests.HTTPError as e:
            return ToolResult(
                success=False,
                message=f"Notion API error: {e.response.status_code}",
                error=str(e),
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to get Notion page",
                error=str(e),
                duration_ms=_ms(start)
            )

    def search_pages(self, query: str) -> ToolResult:
        """
        Search for Notion pages matching a query.
        Returns list of matching pages with titles and IDs.
        """
        start = time.monotonic()

        if not self._is_configured():
            return ToolResult(
                success=False,
                message="Notion API key not configured",
                error="NotConfigured"
            )

        try:
            response = requests.post(
                f"{self._BASE_URL}/search",
                headers=self._headers,
                json={
                    "query":  query,
                    "filter": {"property": "object", "value": "page"},
                },
                timeout=15,
            )
            response.raise_for_status()
            data    = response.json()
            results = data.get("results", [])

            pages = []
            for page in results:
                props      = page.get("properties", {})
                title_prop = props.get("title") or props.get("Name", {})
                title_list = title_prop.get("title", [])
                title      = (
                    title_list[0].get("plain_text", "Untitled")
                    if title_list else "Untitled"
                )
                pages.append({
                    "page_id": page["id"],
                    "title":   title,
                    "url":     page.get("url", ""),
                })

            return ToolResult(
                success=True,
                message=f"Found {len(pages)} Notion page(s) for '{query}'",
                data={"pages": pages},
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to search Notion",
                error=str(e),
                duration_ms=_ms(start)
            )


# ---------------------------------------------------------------------------
# System utilities
# ---------------------------------------------------------------------------

class SystemTools:
    """
    System-level utilities used across tasks.
    Clipboard, volume, running processes, basic OS operations.
    """

    def copy_to_clipboard(self, text: str) -> ToolResult:
        """Copy text to clipboard."""
        start = time.monotonic()
        try:
            pyperclip.copy(text)
            return ToolResult(
                success=True,
                message="Copied to clipboard",
                data={"content": text},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to copy to clipboard",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_clipboard(self) -> ToolResult:
        """Read current clipboard content."""
        start = time.monotonic()
        try:
            content = pyperclip.paste()
            return ToolResult(
                success=True,
                message=f"Clipboard has {len(content)} character(s)",
                data={"content": content},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to read clipboard",
                error=str(e),
                duration_ms=_ms(start)
            )

    def set_system_volume(self, level: int) -> ToolResult:
        """
        Set system volume level (0-100).
        Uses Windows keyboard volume keys — no admin rights needed.
        """
        start = time.monotonic()
        try:
            level = max(0, min(100, level))

            # First mute then unmute to reset, then set volume
            # by pressing volume keys proportionally
            # Simpler approach: use nircmd if available,
            # otherwise use key presses

            # Try pycaw for precise control
            try:
                from ctypes import cast, POINTER
                from comtypes import CLSCTX_ALL
                from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

                devices = AudioUtilities.GetSpeakers()
                interface = devices.Activate(
                    IAudioEndpointVolume._iid_,
                    CLSCTX_ALL, None
                )
                volume = cast(interface, POINTER(IAudioEndpointVolume))
                # pycaw uses -65.25 to 0.0 dB scale
                # Convert 0-100 to scalar 0.0-1.0
                volume.SetMasterVolumeLevelScalar(level / 100, None)

                return ToolResult(
                    success=True,
                    message=f"System volume set to {level}%",
                    data={"level": level, "muted": False},
                    duration_ms=_ms(start)
                )

            except ImportError:
                # pycaw not available — use key presses as fallback
                # Press mute twice to ensure unmuted, then adjust
                pyautogui.press("volumemute")
                time.sleep(0.1)
                pyautogui.press("volumemute")
                time.sleep(0.1)

                # Press volume up/down to approximate the level
                # Each keypress ≈ 2 volume units on most Windows systems
                presses = level // 2
                for _ in range(50):  # First go to 0
                    pyautogui.press("volumedown")
                for _ in range(presses):  # Then go to target
                    pyautogui.press("volumeup")
                    time.sleep(0.02)

                return ToolResult(
                    success=True,
                    message=f"System volume approximately {level}%",
                    data={"level": level, "muted": False},
                    duration_ms=_ms(start)
                )

        except Exception as e:
            log.error("set_system_volume failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to set volume",
                error=str(e),
                duration_ms=_ms(start)
            )

    def mute_system(self) -> ToolResult:
        """Toggle system mute."""
        start = time.monotonic()
        try:
            pyautogui.press("volumemute")
            return ToolResult(
                success=True,
                message="System mute toggled",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to toggle mute",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_running_processes(
        self,
        filter_name: Optional[str] = None,
    ) -> ToolResult:
        """
        List running processes.
        filter_name: optional partial name to filter by
        """
        start = time.monotonic()
        try:
            processes = []
            for proc in psutil.process_iter(["name", "pid", "status"]):
                try:
                    name = proc.info["name"] or ""
                    if filter_name is None or filter_name.lower() in name.lower():
                        processes.append({
                            "name":   name,
                            "pid":    proc.info["pid"],
                            "status": proc.info["status"],
                        })
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            return ToolResult(
                success=True,
                message=f"Found {len(processes)} process(es)",
                data={"processes": processes},
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to list processes",
                error=str(e),
                duration_ms=_ms(start)
            )

    def is_process_running(self, process_name: str) -> ToolResult:
        """
        Check if a specific process is running.
        Returns success=True if found, False if not.
        """
        start    = time.monotonic()
        name_low = process_name.lower()

        for proc in psutil.process_iter(["name"]):
            try:
                if name_low in (proc.info["name"] or "").lower():
                    return ToolResult(
                        success=True,
                        message=f"Process '{process_name}' is running",
                        data={"running": True, "process_name": process_name},
                        duration_ms=_ms(start)
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return ToolResult(
            success=False,
            message=f"Process '{process_name}' is not running",
            data={"running": False, "process_name": process_name},
            duration_ms=_ms(start)
        )

    def wait_seconds(self, seconds: float) -> ToolResult:
        """
        Wait for a specified number of seconds.
        Used between actions that need time to settle.
        """
        start = time.monotonic()
        time.sleep(seconds)
        return ToolResult(
            success=True,
            message=f"Waited {seconds}s",
            duration_ms=_ms(start)
        )
    
    


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------

def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)