
import time
import logging
from typing import Optional

from playwright.sync_api import (
    sync_playwright,
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from src.models import ToolResult
from src.config import (
    BROWSER_TYPE,
    HEADLESS,
    DEFAULT_TIMEOUT,
    ACTION_COOLDOWN,
    BROWSER_MODE,
    CDP_URL,
    CDP_ATTACH_TIMEOUT_MS,
    CDP_TAB_POLICY,
)

log = logging.getLogger(__name__)


class BrowserTools:
    """
    Browser automation via Playwright.

    Transparent dual-mode: reads BROWSER_MODE from config at start() time.
      "launch" — existing behaviour, fresh Chromium per task.
      "attach" — connects to your running Chrome over CDP (Phase 3).
    """

    def __init__(self):
        self._playwright  = None
        self._browser: Optional[Browser]        = None
        self._context: Optional[BrowserContext] = None
        self._page:    Optional[Page]           = None
        # Track which mode was actually used so close() can do the right thing.
        self._mode: str = "launch"

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self) -> ToolResult:
        """
        Launch (or attach to) the browser and open/select a page.
        Safe to call multiple times — won't launch/attach a second time
        if already connected.
        """
        start = time.monotonic()
        try:
            if self._browser and self._browser.is_connected():
                return ToolResult(
                    success=True,
                    message=f"Browser already running (mode={self._mode})",
                    duration_ms=_ms(start),
                )

            self._playwright = sync_playwright().start()
            mode = BROWSER_MODE.lower().strip()

            if mode == "attach":
                return self._start_attach(start)
            else:
                return self._start_launch(start)

        except Exception as e:
            log.error("Browser start failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to start browser",
                error=str(e),
                duration_ms=_ms(start),
            )

    def _start_launch(self, start: float) -> ToolResult:
        """Existing launch-mode startup (unchanged from Phase 2.5d)."""
        self._mode = "launch"
        launcher = getattr(self._playwright, BROWSER_TYPE)
        self._browser = launcher.launch(
            headless=HEADLESS,
            args=["--start-maximized"],
        )
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        self._context.set_default_timeout(DEFAULT_TIMEOUT)
        self._page = self._context.new_page()

        log.info("Browser started in launch mode (%s, headless=%s)", BROWSER_TYPE, HEADLESS)
        return ToolResult(
            success=True,
            message=f"Browser started (launch/{BROWSER_TYPE})",
            duration_ms=_ms(start),
        )

    def _start_attach(self, start: float) -> ToolResult:
        """
        Phase 3: attach to an already-running Chrome over CDP.

        Connects to CDP_URL (default http://localhost:9222).
        CDP_TAB_POLICY controls tab behaviour:
          "new"   — always open a fresh tab (default; clean for automation)
          "reuse" — use whichever tab is currently active

        Cleanup contract: close() only disconnects Playwright.
        The real Chrome process is NEVER terminated.

        Raises a clear RuntimeError if Chrome is not reachable at CDP_URL
        so the caller gets a useful error rather than a cryptic timeout.
        """
        self._mode = "attach"

        log.info("Attempting CDP attach to %s …", CDP_URL)

        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                CDP_URL,
                timeout=CDP_ATTACH_TIMEOUT_MS,
            )
        except Exception as e:
            # Give an actionable error — the most common cause is Chrome
            # not running with --remote-debugging-port.
            hint = (
                f"Could not connect to Chrome at {CDP_URL}. "
                "Is Chrome running with --remote-debugging-port=9222? "
                "Run scripts/launch_chrome_cdp.bat (Windows) or "
                "scripts/launch_chrome_cdp.sh (macOS/Linux) first, "
                "or set BROWSER_MODE=launch to use a managed browser."
            )
            log.error("CDP attach failed: %s — %s", e, hint)
            raise RuntimeError(hint) from e

        # In CDP attach mode, Playwright exposes the existing browser
        # contexts. There is always at least one (the default context).
        # We use that context to access real cookies/sessions.
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError(
                "CDP attach succeeded but no browser contexts were found. "
                "Make sure Chrome has at least one window open."
            )
        self._context = contexts[0]
        self._context.set_default_timeout(DEFAULT_TIMEOUT)

        policy = CDP_TAB_POLICY.lower().strip()

        if policy == "reuse":
            # Find the currently-active (foreground) page, falling back
            # to the first available page if none is clearly focused.
            pages = self._context.pages
            if not pages:
                self._page = self._context.new_page()
                log.info("CDP attach: no existing pages — opened new tab")
            else:
                # Heuristic: the last page in the list is usually the most
                # recently focused one in Playwright's CDP view.
                self._page = pages[-1]
                log.info(
                    "CDP attach: reusing existing tab (%s) — %s",
                    self._get_stable_title(),
                    self._page.url,
                )
        else:
            # "new" (default) — always open a fresh tab so we don't
            # disturb whatever the user is currently looking at.
            self._page = self._context.new_page()
            log.info("CDP attach: opened new tab")

        log.info(
            "Browser attached via CDP at %s (tab_policy=%s, %d context(s), %d page(s))",
            CDP_URL,
            policy,
            len(self._browser.contexts),
            len(self._context.pages),
        )
        return ToolResult(
            success=True,
            message=(
                f"Browser attached via CDP at {CDP_URL} "
                f"(tab_policy={policy})"
            ),
            data={
                "mode":       "attach",
                "cdp_url":    CDP_URL,
                "tab_policy": policy,
                "contexts":   len(self._browser.contexts),
                "pages":      len(self._context.pages),
            },
            duration_ms=_ms(start),
        )

    def close(self) -> ToolResult:
        """
        Release browser resources.

        launch mode: closes the page, context, browser, and stops Playwright.
                     The managed Chromium child process is terminated.
        attach mode: closes our page (if we opened a new one) and disconnects
                     Playwright from Chrome. The real Chrome process keeps
                     running — tabs and sessions are preserved.
        """
        start = time.monotonic()
        try:
            if self._mode == "attach":
                return self._close_attach(start)
            else:
                return self._close_launch(start)

        except Exception as e:
            log.error("Browser close failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to close browser cleanly",
                error=str(e),
                duration_ms=_ms(start),
            )

    def _close_launch(self, start: float) -> ToolResult:
        """Existing launch-mode teardown (unchanged)."""
        if self._page:
            self._page.close()
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

        self._page       = None
        self._context    = None
        self._browser    = None
        self._playwright = None

        return ToolResult(
            success=True,
            message="Browser closed (launch mode)",
            duration_ms=_ms(start),
        )

    def _close_attach(self, start: float) -> ToolResult:
        """
        Phase 3 attach-mode teardown.
        Only disconnects; never kills the real Chrome.
        """
        closed_tab = False

        # If we opened a "new" tab for this task, close just that tab
        # to leave Chrome as clean as we found it.
        if self._page and CDP_TAB_POLICY.lower() == "new":
            try:
                self._page.close()
                closed_tab = True
            except Exception:
                pass  # Page may already be gone

        # Disconnect Playwright from Chrome — does NOT close Chrome.
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass

        self._page       = None
        self._context    = None
        self._browser    = None
        self._playwright = None

        detail = "tab closed, " if closed_tab else ""
        log.info("CDP disconnected (%sChrome kept running)", detail)

        return ToolResult(
            success=True,
            message=(
                f"Disconnected from CDP "
                f"({'tab closed, ' if closed_tab else ''}"
                f"Chrome kept running)"
            ),
            duration_ms=_ms(start),
        )

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    @property
    def page(self):
        """
        Expose the raw Playwright Page object.
        Use only when BrowserTools doesn't have a method for what you need.
        """
        self._ensure_started()
        return self._page

    @property
    def mode(self) -> str:
        """Return the active browser mode: 'launch' or 'attach'."""
        return self._mode

    # -----------------------------------------------------------------------
    # Navigation
    # -----------------------------------------------------------------------

    def navigate(self, url: str) -> ToolResult:
        start = time.monotonic()
        try:
            self._ensure_started()

            if not url.startswith(("http://", "https://")):
                url = "https://" + url

            self._page.goto(url, wait_until="domcontentloaded")
            time.sleep(ACTION_COOLDOWN)

            if self._check_for_blocked_page():
                return ToolResult(
                    success=False,
                    message=f"Navigation blocked — bot detection or access denied at '{url}'",
                    error="BotDetected",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start),
                )

            current_url = self._page.url
            page_title  = self._get_stable_title()

            return ToolResult(
                success=True,
                message=f"Navigated to '{page_title}'",
                data={
                    "current_url": current_url,
                    "page_title":  page_title,
                },
                duration_ms=_ms(start),
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Page load timed out for '{url}'",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            log.error("navigate failed for '%s': %s", url, e)
            return ToolResult(
                success=False,
                message=f"Failed to navigate to '{url}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def search_web(self, query: str) -> ToolResult:
        start = time.monotonic()
        try:
            self._ensure_started()

            search_url = f"https://duckduckgo.com/?q={_url_encode(query)}&ia=web"
            self._page.goto(search_url, wait_until="domcontentloaded")
            time.sleep(1.5)

            if self._check_for_blocked_page():
                return ToolResult(
                    success=False,
                    message=f"Search blocked by bot detection for '{query}'",
                    error="BotDetected",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start),
                )

            return ToolResult(
                success=True,
                message=f"Search results loaded for '{query}'",
                data={
                    "current_url": self._page.url,
                    "page_title":  self._get_stable_title(),
                    "query":       query,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("search_web failed for '%s': %s", query, e)
            return ToolResult(
                success=False,
                message=f"Failed to search for '{query}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def go_back(self) -> ToolResult:
        """Navigate back to the previous page."""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.go_back(wait_until="domcontentloaded")
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message="Navigated back",
                data={"current_url": self._page.url},
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to go back",
                error=str(e),
                duration_ms=_ms(start),
            )

    def get_current_url(self) -> ToolResult:
        """Return the current page URL."""
        start = time.monotonic()
        try:
            self._ensure_started()
            return ToolResult(
                success=True,
                message="Got current URL",
                data={
                    "current_url": self._page.url,
                    "page_title":  self._get_stable_title(),
                },
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to get URL",
                error=str(e),
                duration_ms=_ms(start),
            )

    # -----------------------------------------------------------------------
    # Interaction
    # -----------------------------------------------------------------------

    def click_element(
        self,
        text: Optional[str]     = None,
        selector: Optional[str] = None,
    ) -> ToolResult:
        """
        Click an element on the page.
        Provide either text (visible element text) or selector (CSS/XPath).
        text is preferred — more readable and robust to DOM changes.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            if text:
                element = self._page.get_by_text(text, exact=False).first
                element.click(timeout=DEFAULT_TIMEOUT)
                clicked_label = text
            elif selector:
                self._page.click(selector, timeout=DEFAULT_TIMEOUT)
                clicked_label = selector
            else:
                return ToolResult(
                    success=False,
                    message="Provide either text or selector to click",
                    error="MissingArgument",
                )

            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Clicked '{clicked_label}'",
                data={
                    "element_text": clicked_label,
                    "current_url":  self._page.url,
                },
                duration_ms=_ms(start),
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Element not found or not clickable: '{text or selector}'",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            log.error("click_element failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to click '{text or selector}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def fill_field(
        self,
        selector: str,
        value: str,
        clear_first: bool = True,
    ) -> ToolResult:
        """Fill a single input field."""
        start = time.monotonic()
        try:
            self._ensure_started()

            if clear_first:
                self._page.fill(selector, "")

            self._page.fill(selector, value)
            time.sleep(ACTION_COOLDOWN)

            actual = self._page.input_value(selector)

            return ToolResult(
                success=True,
                message=f"Filled field '{selector}'",
                data={
                    "selector":    selector,
                    "field_value": actual,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("fill_field failed for '%s': %s", selector, e)
            return ToolResult(
                success=False,
                message=f"Failed to fill field '{selector}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def fill_form(self, fields: dict[str, str]) -> ToolResult:
        """Fill multiple form fields at once. fields: dict mapping selector → value."""
        start  = time.monotonic()
        filled = 0
        errors = []

        for selector, value in fields.items():
            result = self.fill_field(selector, value)
            if result.success:
                filled += 1
            else:
                errors.append(f"{selector}: {result.error}")

        if errors:
            return ToolResult(
                success=False,
                message=f"Filled {filled}/{len(fields)} fields. Errors: {'; '.join(errors)}",
                data={"fields_filled": filled},
                error="PartialFailure",
                duration_ms=_ms(start),
            )

        return ToolResult(
            success=True,
            message=f"Filled all {filled} form field(s)",
            data={"fields_filled": filled},
            duration_ms=_ms(start),
        )

    def press_key(self, key: str) -> ToolResult:
        """Press a key in the browser context. e.g. 'Enter', 'Tab', 'Control+a'"""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.keyboard.press(key)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Pressed '{key}'",
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to press '{key}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def scroll(
        self,
        direction: str = "down",
        amount: int    = 500,
    ) -> ToolResult:
        """Scroll the page. direction: 'down' or 'up'."""
        start = time.monotonic()
        try:
            self._ensure_started()
            delta = amount if direction == "down" else -amount
            self._page.evaluate(f"window.scrollBy(0, {delta})")
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Scrolled {direction} {amount}px",
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to scroll",
                error=str(e),
                duration_ms=_ms(start),
            )

    def wait_for_text(
        self,
        text: str,
        timeout_ms: int = 10_000,
    ) -> ToolResult:
        """Wait until specific text appears on the page."""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_text(text).wait_for(timeout=timeout_ms)

            return ToolResult(
                success=True,
                message=f"Text '{text}' appeared on page",
                duration_ms=_ms(start),
            )
        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Text '{text}' did not appear within {timeout_ms}ms",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="wait_for_text failed",
                error=str(e),
                duration_ms=_ms(start),
            )

    # -----------------------------------------------------------------------
    # Content extraction
    # -----------------------------------------------------------------------

    def extract_page_text(
        self,
        selector: Optional[str] = None,
        max_chars: int          = 8000,
    ) -> ToolResult:
        """
        Extract visible text from the page as a plain string.
        selector: optional CSS selector to extract from a specific region.
        max_chars: truncate at this length to stay within token limits.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            if selector:
                element = self._page.query_selector(selector)
                if element:
                    raw_text = element.inner_text()
                else:
                    log.warning("Selector '%s' not found — extracting full page", selector)
                    raw_text = self._page.inner_text("body")
            else:
                raw_text = self._page.inner_text("body")

            lines   = [line.strip() for line in raw_text.splitlines()]
            lines   = [line for line in lines if line]
            cleaned = "\n".join(lines)

            if len(cleaned) > max_chars:
                cleaned = cleaned[:max_chars] + "\n\n[... text truncated ...]"

            word_count = len(cleaned.split())

            return ToolResult(
                success=True,
                message=f"Extracted {word_count} words from page",
                data={
                    "text":        cleaned,
                    "word_count":  word_count,
                    "current_url": self._page.url,
                    "page_title":  self._get_stable_title(),
                    "truncated":   len(raw_text) > max_chars,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("extract_page_text failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to extract page text",
                error=str(e),
                duration_ms=_ms(start),
            )

    def get_links(self, selector: Optional[str] = None) -> ToolResult:
        """Get all links on the page (or within a selector). Returns list of {text, href}."""
        start = time.monotonic()
        try:
            self._ensure_started()

            scope   = self._page.query_selector(selector) if selector else self._page
            anchors = (scope or self._page).query_selector_all("a[href]")

            links = []
            for anchor in anchors:
                href = anchor.get_attribute("href") or ""
                text = (anchor.inner_text() or "").strip()

                if (
                    href
                    and text
                    and not href.startswith("javascript:")
                    and not href.startswith("#")
                ):
                    if href.startswith("/"):
                        from urllib.parse import urlparse
                        parsed = urlparse(self._page.url)
                        href   = f"{parsed.scheme}://{parsed.netloc}{href}"

                    links.append({"text": text, "href": href})

            return ToolResult(
                success=True,
                message=f"Found {len(links)} link(s) on page",
                data={"links": links},
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("get_links failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to get links",
                error=str(e),
                duration_ms=_ms(start),
            )

    def get_page_title(self) -> ToolResult:
        """Return the current page title."""
        start = time.monotonic()
        try:
            self._ensure_started()
            title = self._get_stable_title()
            return ToolResult(
                success=True,
                message=f"Page title: '{title}'",
                data={"page_title": title, "current_url": self._page.url},
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to get page title",
                error=str(e),
                duration_ms=_ms(start),
            )

    def take_screenshot(self, save_path: Optional[str] = None) -> ToolResult:
        """Take a screenshot of the current page. Used as a last-resort fallback."""
        start = time.monotonic()
        try:
            self._ensure_started()
            path = save_path or "browser_screenshot.png"
            self._page.screenshot(path=path, full_page=False)

            return ToolResult(
                success=True,
                message=f"Screenshot saved to '{path}'",
                data={"path": path},
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to take screenshot",
                error=str(e),
                duration_ms=_ms(start),
            )

    # -----------------------------------------------------------------------
    # DOM locators
    # -----------------------------------------------------------------------

    def click_by_role(self, role: str, name: Optional[str] = None) -> ToolResult:
        """Click an element by its ARIA role and optional accessible name."""
        start = time.monotonic()
        try:
            self._ensure_started()
            locator = (
                self._page.get_by_role(role, name=name)
                if name
                else self._page.get_by_role(role)
            )
            locator.first.click(timeout=DEFAULT_TIMEOUT)
            time.sleep(ACTION_COOLDOWN)

            label = f"role={role}" + (f" name='{name}'" if name else "")
            return ToolResult(
                success=True,
                message=f"Clicked {label}",
                data={"current_url": self._page.url},
                duration_ms=_ms(start),
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Element not found: role='{role}' name='{name}'",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to click by role",
                error=str(e),
                duration_ms=_ms(start),
            )

    def click_by_label(self, label: str) -> ToolResult:
        """Click an input element associated with a label."""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_label(label).first.click(timeout=DEFAULT_TIMEOUT)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Clicked element with label '{label}'",
                data={"current_url": self._page.url},
                duration_ms=_ms(start),
            )
        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Label '{label}' not found",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to click by label '{label}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def fill_by_label(self, label: str, value: str) -> ToolResult:
        """Fill an input field identified by its visible label text."""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_label(label).fill(value)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Filled '{label}' with value",
                data={"field_value": value},
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to fill '{label}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def fill_by_placeholder(self, placeholder: str, value: str) -> ToolResult:
        """Fill an input field identified by its placeholder text."""
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_placeholder(placeholder).fill(value)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Filled placeholder '{placeholder}'",
                data={"field_value": value},
                duration_ms=_ms(start),
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to fill '{placeholder}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def search_on_page(self, query: str, timeout_ms: int = 5000) -> ToolResult:
        """
        Find and use the search box already present on the current page.

        Two-pass strategy:
          Pass 1: try common search-input selectors directly.
          Pass 2: click a reveal trigger (GitHub-style) then retry pass 1.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            input_selector_candidates = [
                'input[name="search_query"]',
                'input[type="search"]',
                'input[aria-label*="Search" i]',
                'input[placeholder*="Search" i]',
                '[role="search"] input',
                'input#search',
                'input.search',
            ]

            def _find_visible_input() -> Optional[str]:
                for selector in input_selector_candidates:
                    try:
                        locator = self._page.locator(selector).first
                        if locator.count() > 0 and locator.is_visible():
                            return selector
                    except Exception:
                        continue
                return None

            found_selector = _find_visible_input()

            if found_selector is None:
                trigger_strategies = [
                    lambda: self._page.get_by_text("Search or jump to", exact=False).first,
                    lambda: self._page.get_by_role("button", name="Search", exact=False).first,
                    lambda: self._page.locator('button[aria-label*="Search" i]').first,
                    lambda: self._page.locator('[role="button"][aria-label*="Search" i]').first,
                ]

                for get_trigger in trigger_strategies:
                    try:
                        trigger = get_trigger()
                        if trigger.count() == 0 or not trigger.is_visible():
                            continue
                        trigger.click(timeout=DEFAULT_TIMEOUT)
                        time.sleep(ACTION_COOLDOWN)
                        try:
                            self._page.wait_for_timeout(500)
                        except Exception:
                            pass
                        found_selector = _find_visible_input()
                        if found_selector:
                            log.info("search_on_page: revealed search input via click-to-reveal trigger")
                            break
                    except Exception:
                        continue

            if found_selector is None:
                return ToolResult(
                    success=False,
                    message=(
                        f"No search box found on current page "
                        f"({self._page.url}) — tried direct input "
                        f"patterns and click-to-reveal triggers"
                    ),
                    error="NoSearchBoxFound",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start),
                )

            self._page.fill(found_selector, query)
            time.sleep(ACTION_COOLDOWN)
            self._page.locator(found_selector).first.press("Enter")
            time.sleep(ACTION_COOLDOWN)

            try:
                self._page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PlaywrightTimeout:
                pass

            return ToolResult(
                success=True,
                message=f"Searched on-page for '{query}' using {found_selector}",
                data={
                    "current_url":    self._page.url,
                    "page_title":     self._get_stable_title(),
                    "query":          query,
                    "selector_used":  found_selector,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("search_on_page failed for '%s': %s", query, e)
            return ToolResult(
                success=False,
                message=f"Failed to search on-page for '{query}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def click_best_result(self, query: str, max_candidates: int = 8) -> ToolResult:
        """
        Click whichever visible result item best matches `query` by
        keyword-overlap scoring. Falls back to first result on no match.
 
        Fixed (Phase 3.1, 2026-07-01): clicks the inner navigation link
        rather than the outer result container, and verifies the URL
        actually changed before reporting success. Without this, YouTube's
        hover-preview autoplay (which fires on mouse-enter, before any
        click registers) can absorb the click without navigating, causing
        a false-positive success while the page stays on search results.
        """
        start = time.monotonic()
        try:
            self._ensure_started()
 
            selector_candidates = [
                "ytd-video-renderer",
                "ytd-compact-video-renderer",
                '[data-testid*="result" i]',
                ".search-result",
                ".result-item",
                "article",
            ]
 
            found_selector     = None
            candidate_locators = None
 
            for selector in selector_candidates:
                try:
                    locator = self._page.locator(selector)
                    count   = locator.count()
                    if count > 0:
                        found_selector     = selector
                        candidate_locators = locator
                        break
                except Exception:
                    continue
 
            if found_selector is None:
                return ToolResult(
                    success=False,
                    message=(
                        f"No result items found on current page "
                        f"({self._page.url}) — tried {len(selector_candidates)} patterns"
                    ),
                    error="NoResultsFound",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start),
                )
 
            total_count = candidate_locators.count()
            n_to_check  = min(total_count, max_candidates)
            query_words = [w.lower() for w in query.split() if len(w) > 2]
 
            scored: list[tuple[int, int, str]] = []
            for i in range(n_to_check):
                item = candidate_locators.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    text = (item.inner_text() or "").strip()
                except Exception:
                    continue
 
                if not text:
                    continue
 
                text_lower = text.lower()
                score = sum(1 for w in query_words if w in text_lower)
                scored.append((score, i, text))
 
            if not scored:
                return ToolResult(
                    success=False,
                    message=f"Found {found_selector} elements but none had readable visible text",
                    error="NoReadableCandidates",
                    data={"current_url": self._page.url, "selector": found_selector},
                    duration_ms=_ms(start),
                )
 
            scored.sort(key=lambda s: (-s[0], s[1]))
            best_score, best_pos, best_text = scored[0]
            low_confidence = best_score == 0
 
            winner = candidate_locators.nth(best_pos)
            winner.scroll_into_view_if_needed(timeout=DEFAULT_TIMEOUT)
 
            # -----------------------------------------------------------------
            # Click the inner navigation link, not the outer container.
            # YouTube (and similar sites) wrap the actual /watch link in a
            # nested <a>. Clicking the outer <ytd-video-renderer> container
            # triggers hover-preview behaviour and may land on the preview
            # overlay instead of navigating.
            # -----------------------------------------------------------------
            link_selector_candidates = [
                "a#video-title",
                "a#thumbnail",
                'a[href^="/watch"]',
                "h3 a",
                "a",  # last-resort: any anchor inside the matched item
            ]
 
            click_target = None
            for link_sel in link_selector_candidates:
                try:
                    candidate_link = winner.locator(link_sel).first
                    if candidate_link.count() > 0 and candidate_link.is_visible():
                        click_target = candidate_link
                        break
                except Exception:
                    continue
 
            # Fall back to the container itself only if no inner link was
            # found — keeps this working on non-YouTube layouts that don't
            # nest an <a> (e.g. plain <article> result blocks).
            if click_target is None:
                click_target = winner
 
            pre_click_url = self._page.url
 
            click_target.click(timeout=DEFAULT_TIMEOUT)
            time.sleep(ACTION_COOLDOWN)
 
            # -----------------------------------------------------------------
            # Verify navigation actually happened rather than trusting that
            # the click didn't raise. Poll briefly since SPA navigation
            # (history.pushState) doesn't reliably fire Playwright's load
            # events.
            # -----------------------------------------------------------------
            navigated = False
            poll_deadline = time.monotonic() + 3.0
            while time.monotonic() < poll_deadline:
                if self._page.url != pre_click_url:
                    navigated = True
                    break
                time.sleep(0.15)
 
            if not navigated:
                log.warning(
                    "click_best_result: URL did not change after clicking "
                    "'%s' (still on %s) — treating as failed navigation",
                    best_text[:60], pre_click_url,
                )
                return ToolResult(
                    success=False,
                    message=(
                        f"Clicked '{best_text[:60]}' but page did not navigate "
                        f"— likely absorbed by a hover-preview or overlay element"
                    ),
                    error="ClickDidNotNavigate",
                    data={
                        "current_url":      self._page.url,
                        "attempted_target": best_text,
                        "selector_used":    found_selector,
                    },
                    duration_ms=_ms(start),
                )
 
            # -----------------------------------------------------------------
            # Ensure video playback
            # If the page has a video element and it's paused, click it to play.
            # -----------------------------------------------------------------
            try:
                video_locator = self._page.locator("video").first
                try:
                    video_locator.wait_for(state="attached", timeout=1500)
                except Exception:
                    pass
                
                if video_locator.count() > 0:
                    is_paused = self._page.evaluate('''() => { 
                        const v = document.querySelector("video"); 
                        return v ? v.paused : false; 
                    }''')
                    if is_paused:
                        log.info("click_best_result: Video is paused, attempting to click play")
                        video_locator.click(force=True)
                        time.sleep(ACTION_COOLDOWN)
            except Exception as e:
                log.debug("click_best_result: Error ensuring video playback: %s", e)

            return ToolResult(
                success=True,
                message=(
                    f"Clicked best-matching result: '{best_text[:60]}' "
                    f"(score={best_score}{', low confidence' if low_confidence else ''})"
                ),
                data={
                    "current_url":           self._page.url,
                    "page_title":            self._get_stable_title(),
                    "matched_title":         best_text,
                    "score":                 best_score,
                    "low_confidence":        low_confidence,
                    "selector_used":         found_selector,
                    "candidates_considered": len(scored),
                    "navigated":             True,
                },
                duration_ms=_ms(start),
            )
 
        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message="Timed out clicking the best-matching result",
                error="Timeout",
                duration_ms=_ms(start),
            )
        except Exception as e:
            log.error("click_best_result failed for '%s': %s", query, e)
            return ToolResult(
                success=False,
                message=f"Failed to click best result for '{query}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def get_element_text_by_selector(self, selector: str) -> ToolResult:
        """Read the text content of a specific DOM element."""
        start = time.monotonic()
        try:
            self._ensure_started()
            element = self._page.query_selector(selector)

            if not element:
                return ToolResult(
                    success=False,
                    message=f"Selector '{selector}' not found on page",
                    error="ElementNotFound",
                    duration_ms=_ms(start),
                )

            text = element.inner_text().strip()
            return ToolResult(
                success=True,
                message=f"Got text from '{selector}'",
                data={"text": text, "selector": selector},
                duration_ms=_ms(start),
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to get text from '{selector}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def element_exists(self, selector: str) -> ToolResult:
        """Check if a DOM element exists on the current page."""
        start = time.monotonic()
        try:
            self._ensure_started()
            element = self._page.query_selector(selector)
            exists  = element is not None

            return ToolResult(
                success=exists,
                message=(
                    f"Element '{selector}' exists on page"
                    if exists
                    else f"Element '{selector}' not found on page"
                ),
                data={"exists": exists, "selector": selector},
                duration_ms=_ms(start),
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to check element '{selector}'",
                error=str(e),
                duration_ms=_ms(start),
            )

    def extract_and_summarize(
        self,
        selector: Optional[str] = None,
        topic: Optional[str]    = None,
    ) -> ToolResult:
        """Extract page text then use Gemini to produce a clean summary."""
        start = time.monotonic()
        try:
            extract_result = self.extract_page_text(selector=selector, max_chars=6000)
            if not extract_result.success:
                return extract_result

            raw_text = extract_result.data["text"]
            page_url = extract_result.data["current_url"]

            from google import genai
            from google.genai import types
            from src.config import PLANNER_MODEL

            client = genai.Client()
            focus  = f"Focus on: {topic}" if topic else ""
            prompt = f"""
Clean and summarize the following web page content into
a readable, well-structured text document.
Remove all navigation menus, advertisements, cookie notices,
footer text, and other UI noise.
Keep only the meaningful content.
{focus}
Format with clear headings and bullet points where appropriate.

Page URL: {page_url}

Content:
{raw_text}
""".strip()

            response = client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(max_output_tokens=2000),
            )
            summary = response.text or raw_text

            return ToolResult(
                success=True,
                message=f"Extracted and summarized {len(summary.split())} words",
                data={
                    "text":        summary,
                    "word_count":  len(summary.split()),
                    "current_url": page_url,
                    "page_title":  extract_result.data.get("page_title", ""),
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("extract_and_summarize failed: %s", e)
            return self.extract_page_text(selector=selector)

    def get_first_result_url(
        self,
        query: Optional[str]           = None,
        skip_domains: Optional[list[str]] = None,
    ) -> ToolResult:
        """Get the URL of the most relevant search result, scored by keyword relevance."""
        start = time.monotonic()
        try:
            self._ensure_started()

            skip = set(skip_domains or [
                "youtube.com", "reddit.com", "twitter.com",
                "facebook.com", "instagram.com", "tiktok.com",
            ])

            links      = self._page.query_selector_all("a[href]")
            candidates = []

            for link in links:
                href = link.get_attribute("href") or ""
                text = (link.inner_text() or "").strip()

                if not href or not text:
                    continue
                if href.startswith(("javascript:", "#", "/")):
                    continue
                if not href.startswith("http"):
                    continue
                if "duckduckgo.com" in href:
                    continue
                if any(domain in href for domain in skip):
                    continue
                if len(text) < 10:
                    continue

                score = 0
                if query:
                    query_words = query.lower().split()
                    href_lower  = href.lower()
                    text_lower  = text.lower()
                    for word in query_words:
                        if word in href_lower:
                            score += 2
                        if word in text_lower:
                            score += 1

                if "docs." in href or "/docs/" in href:
                    score += 3
                if "official" in text.lower():
                    score += 2

                candidates.append((score, href, text))

            if not candidates:
                return ToolResult(
                    success=False,
                    message="No suitable result URL found",
                    error="NoResults",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start),
                )

            candidates.sort(key=lambda x: x[0], reverse=True)
            best_score, best_url, best_text = candidates[0]

            return ToolResult(
                success=True,
                message=f"Best result: '{best_text[:60]}'",
                data={
                    "url":         best_url,
                    "title":       best_text,
                    "score":       best_score,
                    "current_url": self._page.url,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("get_first_result_url failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to get first result URL",
                error=str(e),
                data={},
                duration_ms=_ms(start),
            )

    def search_and_extract(self, query: str, selector: str = "article") -> ToolResult:
        """Complete research operation: search → first result → navigate → extract → clean."""
        start = time.monotonic()
        try:
            search_result = self.search_web(query)
            if not search_result.success:
                return search_result

            url_result = self.get_first_result_url(query=query)
            if not url_result.success:
                return self.extract_page_text()

            target_url = url_result.data["url"]
            log.info("search_and_extract: navigating to %s", target_url)

            nav_result = self.navigate(target_url)
            if not nav_result.success:
                return self.extract_page_text()

            import time as t
            t.sleep(1.5)

            for sel in [selector, "article", "main", ".content", "body"]:
                extract_result = self.extract_page_text(selector=sel, max_chars=8000)
                if extract_result.success and extract_result.data.get("word_count", 0) > 100:
                    cleaned    = _clean_web_text(extract_result.data["text"])
                    word_count = len(cleaned.split())

                    return ToolResult(
                        success=True,
                        message=f"Research complete: {word_count} words from {target_url}",
                        data={
                            "text":        cleaned,
                            "word_count":  word_count,
                            "source_url":  target_url,
                            "page_title":  extract_result.data.get("page_title", ""),
                            "current_url": self._page.url,
                        },
                        duration_ms=_ms(start),
                    )

            return ToolResult(
                success=False,
                message="Could not extract meaningful content",
                error="ExtractionFailed",
                data={},
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("search_and_extract failed: %s", e)
            return ToolResult(
                success=False,
                message="search_and_extract failed",
                error=str(e),
                data={},
                duration_ms=_ms(start),
            )

    def search_extract_and_summarize(
        self,
        query: str,
        topic: Optional[str] = None,
    ) -> ToolResult:
        """Complete research + summarization: search_and_extract → Gemini summarize."""
        start = time.monotonic()

        extract_result = self.search_and_extract(query)
        if not extract_result.success:
            return extract_result

        raw_text   = extract_result.data["text"]
        source_url = extract_result.data.get("source_url", "")

        try:
            from google import genai
            from google.genai import types
            from src.config import PLANNER_MODEL

            client = genai.Client()
            focus  = f"Focus specifically on: {topic}" if topic else ""

            prompt = f"""
Summarize the following web content into a clean,
readable document with clear structure.

Requirements:
- Use clear headings (##) for major sections
- Use bullet points for lists of features/items
- Remove all navigation, ads, cookie notices, footer text
- Keep only meaningful content relevant to the topic
- Write in plain English, no jargon
- Aim for 300-500 words
{focus}

Source: {source_url}

Content:
{raw_text[:6000]}
""".strip()

            response = client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(max_output_tokens=1500),
            )
            summary    = response.text or raw_text
            word_count = len(summary.split())

            return ToolResult(
                success=True,
                message=f"Research + summary: {word_count} words",
                data={
                    "text":        summary,
                    "word_count":  word_count,
                    "source_url":  source_url,
                    "raw_length":  len(raw_text),
                    "current_url": self._page.url,
                },
                duration_ms=_ms(start),
            )

        except Exception as e:
            log.error("Summarization failed, returning raw: %s", e)
            return extract_result

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """
        Auto-start the browser if not already running.
        In attach mode: reconnects if Chrome dropped the CDP connection.
        """
        if not self._browser or not self._browser.is_connected():
            result = self.start()
            if not result.success:
                raise RuntimeError(f"Browser failed to start: {result.error}")

        if not self._page:
            # In attach mode we always open a new page here; the context
            # already points to the real browser's default context.
            self._page = self._context.new_page()

    def _check_for_blocked_page(self) -> bool:
        """Detect if the current page is a CAPTCHA or bot-detection wall."""
        blocked_signals = [
            "unusual traffic",
            "captcha",
            "are you a robot",
            "verify you are human",
            "access denied",
            "403 forbidden",
            "rate limited",
            "too many requests",
        ]
        try:
            title = self._page.title().lower()
            for signal in blocked_signals:
                if signal in title:
                    return True

            body_text = self._page.inner_text("body").lower()[:1000]
            for signal in blocked_signals:
                if signal in body_text:
                    return True

            return False
        except Exception:
            return False

    def _get_stable_title(
        self,
        max_wait: float = 2.0,
        poll_interval: float = 0.25,
    ) -> str:
        """
        Wait for `document.title` to stabilise after SPA navigation.

        Many sites (YouTube, Gmail, Twitter, etc.) update the page title
        via JavaScript *after* the DOM-content-loaded event.  A naive
        `page.title()` right after `goto()` returns a generic title
        (e.g. "YouTube") rather than the full video/page title.

        This helper polls the title every `poll_interval` seconds.  Once
        the title stays the same for two consecutive reads, or `max_wait`
        seconds elapse, it returns whatever the title currently is.

        Falls back to the immediate `page.title()` on any exception so
        this is always safe to call.
        """
        try:
            prev = self._page.title()
            deadline = time.monotonic() + max_wait

            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                current = self._page.title()
                if current == prev and current:  # stable & non-empty
                    return current
                prev = current

            # Timed out — return whatever we have
            return prev or self._page.title()
        except Exception:
            # Never let title polling break a tool call
            try:
                return self._page.title()
            except Exception:
                return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _url_encode(text: str) -> str:
    from urllib.parse import quote_plus
    return quote_plus(text)


def _clean_web_text(text: str) -> str:
    """Remove common web page UI noise from extracted text."""
    noise_phrases = [
        "upgrade to our browser",
        "download browser",
        "fast. free. private",
        "open menu",
        "search settings",
        "safe search",
        "was this helpful",
        "more results",
        "searches related to",
        "share feedback",
        "privacy policy",
        "terms of service",
        "cookie",
        "advertisement",
        "skip to content",
        "sign up",
        "log in",
        "subscribe",
        "newsletter",
    ]

    lines         = text.splitlines()
    cleaned_lines = []

    for line in lines:
        line_lower = line.lower().strip()

        if not line_lower:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue

        if any(phrase in line_lower for phrase in noise_phrases):
            continue

        if len(line.strip()) < 3:
            continue

        cleaned_lines.append(line)

    return "\n".join(cleaned_lines).strip()


# ---------------------------------------------------------------------------
# Media Controller
# ---------------------------------------------------------------------------

class MediaController:
    """Encapsulates media playback controls for browser automation."""
    def __init__(self, bt: "BrowserTools"):
        self.bt = bt

    def play_video(self) -> "ToolResult":
        try:
            self.bt._ensure_started()
            if self.bt._page is None:
                return ToolResult(success=False, message="Browser not open", error="NoBrowser", data={})
            
            is_paused = self.bt._page.evaluate('''() => { 
                const v = document.querySelector("video"); 
                if (v && v.paused) { v.play(); return true; }
                return false;
            }''')
            if is_paused:
                return ToolResult(success=True, message="Video playback started", data={})
            return ToolResult(success=True, message="Video already playing or not found", data={})
        except Exception as e:
            return ToolResult(success=False, message=f"Failed to play video: {e}", error="MediaPlayFailed", data={})

    def pause_video(self) -> "ToolResult":
        try:
            self.bt._ensure_started()
            if self.bt._page is None:
                return ToolResult(success=False, message="Browser not open", error="NoBrowser", data={})
            
            is_playing = self.bt._page.evaluate('''() => { 
                const v = document.querySelector("video"); 
                if (v && !v.paused) { v.pause(); return true; }
                return false;
            }''')
            if is_playing:
                return ToolResult(success=True, message="Video paused", data={})
            return ToolResult(success=True, message="Video already paused or not found", data={})
        except Exception as e:
            return ToolResult(success=False, message=f"Failed to pause video: {e}", error="MediaPauseFailed", data={})

    def resume_video(self) -> "ToolResult":
        return self.play_video()

    def skip_skippable_ads(self) -> "ToolResult":
        try:
            self.bt._ensure_started()
            if self.bt._page is None:
                return ToolResult(success=False, message="Browser not open", error="NoBrowser", data={})
            
            ad_selectors = [".ytp-ad-skip-button", ".ytp-skip-ad-button", ".ytp-ad-skip-button-modern"]
            for selector in ad_selectors:
                loc = self.bt._page.locator(selector).first
                if loc.count() > 0 and loc.is_visible():
                    loc.click(force=True)
                    return ToolResult(success=True, message="Skipped ad", data={"selector_used": selector})
                    
            return ToolResult(success=True, message="No skippable ads found", data={})
        except Exception as e:
            return ToolResult(success=False, message=f"Error checking for ads: {e}", error="SkipAdFailed", data={})

    def wait_until_finished(self) -> "ToolResult":
        try:
            self.bt._ensure_started()
            if self.bt._page is None:
                return ToolResult(success=False, message="Browser not open", error="NoBrowser", data={})
            
            # Simple blocking wait checking every 5 seconds until video ends
            # Note: The agent framework handles long-running tools by yielding thread/process,
            # but for this MVP blocking wait is acceptable.
            poll_interval = 5.0
            max_duration = 3600.0 # 1 hour max
            start_time = time.monotonic()
            
            while (time.monotonic() - start_time) < max_duration:
                is_ended = self.bt._page.evaluate('''() => { 
                    const v = document.querySelector("video"); 
                    return !v || v.ended;
                }''')
                if is_ended:
                    return ToolResult(success=True, message="Video finished playing", data={"duration_ms": _ms(start_time)})
                time.sleep(poll_interval)
                
            return ToolResult(success=False, message="Video didn't finish within 1 hour limit", error="Timeout", data={})
        except Exception as e:
            return ToolResult(success=False, message=f"Failed while waiting for video: {e}", error="MediaWaitFailed", data={})


# ---------------------------------------------------------------------------
# Registry entry point
# ---------------------------------------------------------------------------

def build_executor():
    """Build the ToolSpec for the `browser` tool."""
    from src.models import ActionType, ToolResult, ToolType as _ToolType
    from src.registry import ToolSpec
    from src.state import resolve_placeholder

    def executor(step, ctx) -> "ToolResult":
        from src.graph import _get_browser_instance
        bt = _get_browser_instance()

        target = step.target
        if ctx.slots is not None:
            target = resolve_placeholder(step.target, ctx.slots)

        action_map = {
            ActionType.NAVIGATE:          lambda: bt.navigate(target),
            ActionType.SEARCH_WEB:        lambda: bt.search_web(target),
            ActionType.SEARCH_ON_PAGE:    lambda: bt.search_on_page(target),
            ActionType.CLICK_ELEMENT:     lambda: bt.click_element(text=target),
            ActionType.CLICK_BEST_RESULT: lambda: bt.click_best_result(target),
            ActionType.FILL_FORM:         lambda: bt.fill_field(target, step.value or ""),
            ActionType.EXTRACT_TEXT:      lambda: bt.extract_page_text(
                selector=target if target not in ("body", "page", "all") else None
            ),
            ActionType.SEARCH_AND_EXTRACT: lambda: bt.search_and_extract(target),
            ActionType.SEARCH_EXTRACT_AND_SUMMARIZE: lambda: bt.search_extract_and_summarize(
                target, step.value
            ),
            ActionType.WAIT_FOR_PAGE:    lambda: bt.wait_for_text(target),
            ActionType.GET_FIRST_RESULT: lambda: bt.get_first_result_url(),
            
            ActionType.MEDIA_PLAY:       lambda: MediaController(bt).play_video(),
            ActionType.MEDIA_PAUSE:      lambda: MediaController(bt).pause_video(),
            ActionType.MEDIA_RESUME:     lambda: MediaController(bt).resume_video(),
            ActionType.MEDIA_SKIP_ADS:   lambda: MediaController(bt).skip_skippable_ads(),
            ActionType.MEDIA_WAIT:       lambda: MediaController(bt).wait_until_finished(),
        }

        handler = action_map.get(step.action)
        if handler is None:
            return ToolResult(
                success=False,
                message=f"Unknown browser action: {step.action.value}",
                error="UnknownAction",
                data={},
            )
        return handler()

    return ToolSpec(tool_type=_ToolType.BROWSER, executor=executor)