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
)

log = logging.getLogger(__name__)

class BrowserTools:
    """
    Browser automation via Playwright.

    Usage:
        bt = BrowserTools()
        bt.start()
        result = bt.navigate("https://google.com")
        result = bt.search("python tutorials")
        text   = bt.extract_page_text()
        bt.close()

    Or use as context manager:
        with BrowserTools() as bt:
            bt.navigate("https://example.com")
            text = bt.extract_page_text()
    """

    def __init__(self):
        self._playwright  = None
        self._browser: Optional[Browser]        = None
        self._context: Optional[BrowserContext] = None
        self._page:    Optional[Page]           = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def start(self) -> ToolResult:
        """
        Launch the browser and open a blank page.
        Must be called before any other method.
        Safe to call multiple times — won't launch a second browser.
        """
        start = time.monotonic()
        try:
            if self._browser and self._browser.is_connected():
                return ToolResult(
                    success=True,
                    message="Browser already running",
                    duration_ms=_ms(start)
                )

            self._playwright = sync_playwright().start()

            # Launch the right browser type from config
            launcher = getattr(self._playwright, BROWSER_TYPE)
            self._browser = launcher.launch(
                headless=HEADLESS,
                args=["--start-maximized"]
            )

            # Context holds cookies/session for the whole task
            self._context = self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                # Pretend to be a real browser — some sites block Playwright
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )

            self._context.set_default_timeout(DEFAULT_TIMEOUT)
            self._page = self._context.new_page()

            log.info("Browser started (%s, headless=%s)", BROWSER_TYPE, HEADLESS)
            return ToolResult(
                success=True,
                message=f"Browser started ({BROWSER_TYPE})",
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("Browser start failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to start browser",
                error=str(e),
                duration_ms=_ms(start)
            )

    def close(self) -> ToolResult:
        """
        Close the browser and release all resources.
        Always call this when the task is done.
        """
        start = time.monotonic()
        try:
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
                message="Browser closed",
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("Browser close failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to close browser cleanly",
                error=str(e),
                duration_ms=_ms(start)
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

            # Detect bot walls immediately after navigation
            if self._check_for_blocked_page():
                return ToolResult(
                    success=False,
                    message=f"Navigation blocked — bot detection or access denied at '{url}'",
                    error="BotDetected",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start)
                )

            current_url = self._page.url
            page_title  = self._page.title()

            return ToolResult(
                success=True,
                message=f"Navigated to '{page_title}'",
                data={
                    "current_url": current_url,
                    "page_title":  page_title,
                },
                duration_ms=_ms(start)
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Page load timed out for '{url}'",
                error="Timeout",
                duration_ms=_ms(start)
            )
        except Exception as e:
            log.error("navigate failed for '%s': %s", url, e)
            return ToolResult(
                success=False,
                message=f"Failed to navigate to '{url}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def search_web(self, query: str) -> ToolResult:
        start = time.monotonic()
        try:
            self._ensure_started()

            # DuckDuckGo — no bot detection issues
            search_url = f"https://duckduckgo.com/?q={_url_encode(query)}&ia=web"
            self._page.goto(search_url, wait_until="domcontentloaded")
            time.sleep(1.5)  # DDG needs a moment to render results

            # Check if we got blocked
            if self._check_for_blocked_page():
                return ToolResult(
                    success=False,
                    message=f"Search blocked by bot detection for '{query}'",
                    error="BotDetected",
                    data={"current_url": self._page.url},
                    duration_ms=_ms(start)
                )

            current_url = self._page.url
            page_title  = self._page.title()

            return ToolResult(
                success=True,
                message=f"Search results loaded for '{query}'",
                data={
                    "current_url": current_url,
                    "page_title":  page_title,
                    "query":       query,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("search_web failed for '%s': %s", query, e)
            return ToolResult(
                success=False,
                message=f"Failed to search for '{query}'",
                error=str(e),
                duration_ms=_ms(start)
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
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to go back",
                error=str(e),
                duration_ms=_ms(start)
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
                    "page_title":  self._page.title(),
                },
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to get URL",
                error=str(e),
                duration_ms=_ms(start)
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

        Provide either:
          text:     visible text of the element — "Sign In", "Submit", "Next"
                    Uses Playwright's getByText which is very reliable
          selector: CSS or XPath selector for precise targeting
                    e.g. "#submit-btn", "button.primary", "//button[@type='submit']"

        text is preferred — it's more readable and robust to DOM changes.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            if text:
                # exact=False allows partial text match
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
                    error="MissingArgument"
                )

            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Clicked '{clicked_label}'",
                data={
                    "element_text": clicked_label,
                    "current_url":  self._page.url,
                },
                duration_ms=_ms(start)
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Element not found or not clickable: '{text or selector}'",
                error="Timeout",
                duration_ms=_ms(start)
            )
        except Exception as e:
            log.error("click_element failed: %s", e)
            return ToolResult(
                success=False,
                message=f"Failed to click '{text or selector}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def fill_field(
        self,
        selector: str,
        value: str,
        clear_first: bool = True,
    ) -> ToolResult:
        """
        Fill a single input field.

        selector: CSS selector or label text
                  e.g. "#email", "input[name='username']", "[placeholder='Email']"
        value:    text to type into the field
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            if clear_first:
                self._page.fill(selector, "")

            self._page.fill(selector, value)
            time.sleep(ACTION_COOLDOWN)

            # Read back the field value to confirm
            actual = self._page.input_value(selector)

            return ToolResult(
                success=True,
                message=f"Filled field '{selector}'",
                data={
                    "selector":    selector,
                    "field_value": actual,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("fill_field failed for '%s': %s", selector, e)
            return ToolResult(
                success=False,
                message=f"Failed to fill field '{selector}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def fill_form(self, fields: dict[str, str]) -> ToolResult:
        """
        Fill multiple form fields at once.

        fields: dict mapping selector → value
        Example:
            bt.fill_form({
                "#username":  "john@example.com",
                "#password":  "mypassword",
            })
        """
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
                duration_ms=_ms(start)
            )

        return ToolResult(
            success=True,
            message=f"Filled all {filled} form field(s)",
            data={"fields_filled": filled},
            duration_ms=_ms(start)
        )

    def press_key(self, key: str) -> ToolResult:
        """
        Press a key in the browser context.
        key examples: "Enter", "Tab", "Escape", "Control+a"
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.keyboard.press(key)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Pressed '{key}'",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to press '{key}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def scroll(
        self,
        direction: str = "down",
        amount: int    = 500,
    ) -> ToolResult:
        """
        Scroll the page.
        direction: "down" or "up"
        amount:    pixels to scroll
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            delta = amount if direction == "down" else -amount
            self._page.evaluate(f"window.scrollBy(0, {delta})")
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Scrolled {direction} {amount}px",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to scroll",
                error=str(e),
                duration_ms=_ms(start)
            )

    def wait_for_text(
        self,
        text: str,
        timeout_ms: int = 10_000,
    ) -> ToolResult:
        """
        Wait until specific text appears on the page.
        Useful after clicking something that triggers a load.
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_text(text).wait_for(timeout=timeout_ms)

            return ToolResult(
                success=True,
                message=f"Text '{text}' appeared on page",
                duration_ms=_ms(start)
            )
        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Text '{text}' did not appear within {timeout_ms}ms",
                error="Timeout",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"wait_for_text failed",
                error=str(e),
                duration_ms=_ms(start)
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

        This is the most important method for research tasks.
        Instead of taking a screenshot and sending it to Gemini (expensive),
        extract the text and send that instead (cheap and more accurate).

        selector: optional CSS selector to extract text from a specific region
                  e.g. "article", "main", "#content"
                  If None, extracts from the whole page body.

        max_chars: truncate at this length to stay within token limits.
                   8000 chars ≈ 2000 tokens, well within Gemini's limit.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            if selector:
                # Extract from specific element
                element = self._page.query_selector(selector)
                if element:
                    raw_text = element.inner_text()
                else:
                    # Fallback to full page if selector not found
                    log.warning(
                        "Selector '%s' not found — extracting full page", selector
                    )
                    raw_text = self._page.inner_text("body")
            else:
                raw_text = self._page.inner_text("body")

            # Clean up whitespace
            lines    = [line.strip() for line in raw_text.splitlines()]
            lines    = [line for line in lines if line]  # remove empty lines
            cleaned  = "\n".join(lines)

            # Truncate if needed
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
                    "page_title":  self._page.title(),
                    "truncated":   len(raw_text) > max_chars,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("extract_page_text failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to extract page text",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_links(
        self,
        selector: Optional[str] = None,
    ) -> ToolResult:
        """
        Get all links on the page (or within a selector).
        Returns list of {text, href} dicts.
        Useful for research tasks — find relevant links to follow.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            scope = self._page.query_selector(selector) if selector else self._page
            anchors = (scope or self._page).query_selector_all("a[href]")

            links = []
            for anchor in anchors:
                href = anchor.get_attribute("href") or ""
                text = (anchor.inner_text() or "").strip()

                # Skip empty, javascript:, and anchor-only links
                if (
                    href
                    and text
                    and not href.startswith("javascript:")
                    and not href.startswith("#")
                ):
                    # Make relative URLs absolute
                    if href.startswith("/"):
                        from urllib.parse import urlparse
                        parsed = urlparse(self._page.url)
                        href   = f"{parsed.scheme}://{parsed.netloc}{href}"

                    links.append({"text": text, "href": href})

            return ToolResult(
                success=True,
                message=f"Found {len(links)} link(s) on page",
                data={"links": links},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("get_links failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to get links",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_page_title(self) -> ToolResult:
        """Return the current page title."""
        start = time.monotonic()
        try:
            self._ensure_started()
            title = self._page.title()
            return ToolResult(
                success=True,
                message=f"Page title: '{title}'",
                data={"page_title": title, "current_url": self._page.url},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to get page title",
                error=str(e),
                duration_ms=_ms(start)
            )

    def take_screenshot(
        self,
        save_path: Optional[str] = None,
    ) -> ToolResult:
        """
        Take a screenshot of the current page.
        Used ONLY as a last resort before falling back to vision.py.
        Not part of the normal flow — most tasks never call this.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            path = save_path or "browser_screenshot.png"
            self._page.screenshot(path=path, full_page=False)

            return ToolResult(
                success=True,
                message=f"Screenshot saved to '{path}'",
                data={"path": path},
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message="Failed to take screenshot",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _ensure_started(self) -> None:
        """
        Auto-start the browser if not already running.
        Means callers don't need to manually call start() first.
        """
        if not self._browser or not self._browser.is_connected():
            result = self.start()
            if not result.success:
                raise RuntimeError(f"Browser failed to start: {result.error}")

        if not self._page:
            self._page = self._context.new_page()

    # captcha detection function

    def _check_for_blocked_page(self) -> bool:
        """
        Detect if the current page is a CAPTCHA or bot-detection wall.
        Returns True if blocked, False if normal page.
        """
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
            # Check page title first — fast
            title = self._page.title().lower()
            for signal in blocked_signals:
                if signal in title:
                    return True

            # Check visible text — slightly slower
            body_text = self._page.inner_text("body").lower()[:1000]
            for signal in blocked_signals:
                if signal in body_text:
                    return True

            return False

        except Exception:
            return False
        

    # DOM Locator - for foster and richer response
    def click_by_role(
        self,
        role: str,
        name: Optional[str] = None,
    ) -> ToolResult:
        """
        Click an element by its ARIA role and optional accessible name.
        This is the most semantically correct way to click UI elements.

        role examples:
        "button"     — any button
        "link"       — any anchor link
        "textbox"    — any text input
        "checkbox"   — any checkbox
        "menuitem"   — any menu item
        "tab"        — any tab element
        "heading"    — any heading (h1-h6)

        Examples:
        bt.click_by_role("button", name="Submit")
        bt.click_by_role("link",   name="Sign in")
        bt.click_by_role("tab",    name="Settings")
        """
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
                duration_ms=_ms(start)
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Element not found: role='{role}' name='{name}'",
                error="Timeout",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to click by role",
                error=str(e),
                duration_ms=_ms(start)
            )

    def click_by_label(self, label: str) -> ToolResult:
        """
        Click an input element associated with a label.
        Useful for forms where inputs have visible labels.

        Example:
        bt.click_by_label("Email address")
        bt.click_by_label("Remember me")  # clicks the checkbox
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_label(label).first.click(timeout=DEFAULT_TIMEOUT)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Clicked element with label '{label}'",
                data={"current_url": self._page.url},
                duration_ms=_ms(start)
            )

        except PlaywrightTimeout:
            return ToolResult(
                success=False,
                message=f"Label '{label}' not found",
                error="Timeout",
                duration_ms=_ms(start)
            )
        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to click by label '{label}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def fill_by_label(self, label: str, value: str) -> ToolResult:
        """
        Fill an input field identified by its visible label text.
        More readable than CSS selectors and robust to DOM changes.

        Example:
        bt.fill_by_label("Email", "john@example.com")
        bt.fill_by_label("Search", "python tutorials")
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_label(label).fill(value)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Filled '{label}' with value",
                data={"field_value": value},
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to fill '{label}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def fill_by_placeholder(self, placeholder: str, value: str) -> ToolResult:
        """
        Fill an input field identified by its placeholder text.

        Example:
        bt.fill_by_placeholder("Search...", "python tutorials")
        bt.fill_by_placeholder("Enter email", "john@example.com")
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            self._page.get_by_placeholder(placeholder).fill(value)
            time.sleep(ACTION_COOLDOWN)

            return ToolResult(
                success=True,
                message=f"Filled placeholder '{placeholder}'",
                data={"field_value": value},
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to fill '{placeholder}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def get_element_text_by_selector(self, selector: str) -> ToolResult:
        """
        Read the text content of a specific DOM element.
        Faster and more precise than extracting the full page.

        selector examples:
        "h1"              — page heading
        ".price"          — price element
        "#error-message"  — error text
        "nav"             — navigation text

        Example:
        result = bt.get_element_text_by_selector("h1")
        print(result.data["text"])  # "Welcome back, John"
        """
        start = time.monotonic()
        try:
            self._ensure_started()
            element = self._page.query_selector(selector)

            if not element:
                return ToolResult(
                    success=False,
                    message=f"Selector '{selector}' not found on page",
                    error="ElementNotFound",
                    duration_ms=_ms(start)
                )

            text = element.inner_text().strip()
            return ToolResult(
                success=True,
                message=f"Got text from '{selector}'",
                data={"text": text, "selector": selector},
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to get text from '{selector}'",
                error=str(e),
                duration_ms=_ms(start)
            )

    def element_exists(self, selector: str) -> ToolResult:
        """
        Check if a DOM element exists on the current page.
        Use this for verification — "did the submit button appear?"

        Returns success=True if element exists, False if not.
        Never raises — safe to use as a check.
        """
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
                duration_ms=_ms(start)
            )

        except Exception as e:
            return ToolResult(
                success=False,
                message=f"Failed to check element '{selector}'",
                error=str(e),
                duration_ms=_ms(start)
            )
        
    
    def extract_and_summarize(
        self,
        selector: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> ToolResult:
        """
        Extract page text then use Gemini to produce a clean summary.
        Much more readable than raw page extraction.
        
        topic: what to focus the summary on
            e.g. "Python release features" or "AI news headlines"
        """
        start = time.monotonic()
        try:
            # First extract raw text
            extract_result = self.extract_page_text(
                selector=selector,
                max_chars=6000
            )
            if not extract_result.success:
                return extract_result

            raw_text = extract_result.data["text"]
            page_url = extract_result.data["current_url"]

            # Use Gemini to clean and summarize
            from google import genai
            from google.genai import types
            from src.config import PLANNER_MODEL

            client = genai.Client()

            focus = f"Focus on: {topic}" if topic else ""
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
                config=types.GenerateContentConfig(
                    max_output_tokens=2000,
                )
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
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("extract_and_summarize failed: %s", e)
            # Fall back to plain extraction
            return self.extract_page_text(selector=selector)


    def get_first_result_url(
        self,
        skip_domains: Optional[list[str]] = None,
    ) -> ToolResult:
        """
        Get the URL of the first organic search result on the current page.
        Call this after search_web, then navigate() to that URL.

        skip_domains: domains to skip e.g. ["reddit.com", "youtube.com"]
        Returns the URL and title of the first good result.
        """
        start = time.monotonic()
        try:
            self._ensure_started()

            # Default domains to skip — not useful for content extraction
            skip = set(skip_domains or [
                "youtube.com",
                "reddit.com",
                "twitter.com",
                "facebook.com",
                "instagram.com",
                "tiktok.com",
            ])

            # Get all links on the page
            links = self._page.query_selector_all("a[href]")

            for link in links:
                href  = link.get_attribute("href") or ""
                text  = (link.inner_text() or "").strip()

                # Skip empty, javascript, anchors
                if not href or not text:
                    continue
                if href.startswith(("javascript:", "#", "/")):
                    continue
                if not href.startswith("http"):
                    continue

                # Skip DuckDuckGo internal links
                if "duckduckgo.com" in href:
                    continue

                # Skip skipped domains
                if any(domain in href for domain in skip):
                    continue

                # Skip very short link text (likely nav elements)
                if len(text) < 10:
                    continue

                return ToolResult(
                    success=True,
                    message=f"First result: '{text[:60]}'",
                    data={
                        "url":   href,
                        "title": text,
                        "current_url": self._page.url,
                    },
                    duration_ms=_ms(start)
                )

            return ToolResult(
                success=False,
                message="No suitable result URL found on page",
                error="NoResults",
                data={"current_url": self._page.url},
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("get_first_result_url failed: %s", e)
            return ToolResult(
                success=False,
                message="Failed to get first result URL",
                error=str(e),
                data={},
                duration_ms=_ms(start)
            )

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _url_encode(text: str) -> str:
    """Simple URL encoding for search queries."""
    from urllib.parse import quote_plus
    return quote_plus(text)