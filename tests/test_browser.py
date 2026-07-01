"""
tests/test_browser.py
---------------------
Unit / integration tests for src/tools/browser.py.

Phase 3 additions:
  - TestCDPAttachMode    — verifies the attach-mode lifecycle (mock CDP)
  - TestBrowserModeSwitch — verifies config-driven mode selection
  - TestCloseSemantics   — verifies launch close kills browser; attach close doesn't

The existing Phase 1/2 tests are preserved unchanged below.
"""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# Helpers
# ===========================================================================

def _make_mock_page(url="https://example.com", title="Example"):
    page = MagicMock()
    page.url   = url
    page.title = MagicMock(return_value=title)
    page.inner_text = MagicMock(return_value="Hello world test content here")
    page.query_selector = MagicMock(return_value=None)
    page.count = MagicMock(return_value=0)
    return page


def _make_mock_context(page=None):
    ctx  = MagicMock()
    page = page or _make_mock_page()
    ctx.new_page  = MagicMock(return_value=page)
    ctx.pages     = [page]
    ctx.set_default_timeout = MagicMock()
    return ctx


def _make_mock_browser(context=None, connected=True):
    browser  = MagicMock()
    context  = context or _make_mock_context()
    browser.is_connected = MagicMock(return_value=connected)
    browser.new_context  = MagicMock(return_value=context)
    browser.contexts     = [context]
    browser.close        = MagicMock()
    return browser, context


# ===========================================================================
# Phase 3: CDP Attach Mode Tests
# ===========================================================================

class TestCDPAttachMode:
    """Verify attach-mode lifecycle without requiring a real Chrome instance."""

    def test_start_attach_success(self):
        """_start_attach should connect via CDP and pick a page per tab policy."""
        from src.tools.browser import BrowserTools

        mock_page    = _make_mock_page(url="https://mail.google.com", title="Gmail")
        mock_context = _make_mock_context(page=mock_page)
        mock_browser, _ = _make_mock_browser(context=mock_context)

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp = MagicMock(return_value=mock_browser)

        with patch("src.tools.browser.sync_playwright") as mock_sp, \
             patch("src.tools.browser.BROWSER_MODE", "attach"), \
             patch("src.tools.browser.CDP_URL", "http://localhost:9222"), \
             patch("src.tools.browser.CDP_TAB_POLICY", "new"):

            mock_sp.return_value.__enter__ = MagicMock(return_value=mock_pw)
            mock_sp.return_value.__exit__  = MagicMock(return_value=False)
            mock_sp.return_value.start     = MagicMock(return_value=mock_pw)

            bt = BrowserTools()
            bt._playwright = mock_pw

            result = bt._start_attach(start=0.0)

        assert result.success, f"Expected success, got: {result.message}"
        assert "attach" in result.message.lower() or "cdp" in result.message.lower()
        assert bt._mode == "attach"

    def test_start_attach_chrome_not_running(self):
        """_start_attach should raise RuntimeError with a helpful message when Chrome is not up."""
        from src.tools.browser import BrowserTools

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp = MagicMock(
            side_effect=Exception("Connection refused")
        )

        with patch("src.tools.browser.BROWSER_MODE", "attach"), \
             patch("src.tools.browser.CDP_URL", "http://localhost:9222"), \
             patch("src.tools.browser.CDP_ATTACH_TIMEOUT_MS", 1000):

            bt = BrowserTools()
            bt._playwright = mock_pw

            with pytest.raises(RuntimeError) as exc_info:
                bt._start_attach(start=0.0)

        msg = str(exc_info.value).lower()
        assert "9222" in msg or "cdp" in msg or "chrome" in msg, (
            f"Error message should mention CDP/port/Chrome, got: {exc_info.value}"
        )

    def test_start_attach_no_contexts(self):
        """_start_attach should raise RuntimeError when Chrome has no open contexts."""
        from src.tools.browser import BrowserTools

        mock_browser = MagicMock()
        mock_browser.contexts = []  # No contexts

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp = MagicMock(return_value=mock_browser)

        with patch("src.tools.browser.BROWSER_MODE", "attach"), \
             patch("src.tools.browser.CDP_URL", "http://localhost:9222"), \
             patch("src.tools.browser.CDP_ATTACH_TIMEOUT_MS", 1000):

            bt = BrowserTools()
            bt._playwright = mock_pw

            with pytest.raises(RuntimeError) as exc_info:
                bt._start_attach(start=0.0)

        assert "context" in str(exc_info.value).lower()

    def test_close_attach_does_not_kill_browser(self):
        """
        _close_attach must NOT call _browser.close() in a way that terminates
        Chrome — it should only disconnect Playwright.
        In practice: browser.close() on a CDP connection disconnects, not kills.
        We verify page.close() is called when tab_policy=new, and that
        _playwright.stop() is called to clean up the Playwright process.
        """
        from src.tools.browser import BrowserTools

        mock_page = _make_mock_page()
        mock_browser, mock_context = _make_mock_browser()

        bt = BrowserTools()
        bt._mode       = "attach"
        bt._page       = mock_page
        bt._context    = mock_context
        bt._browser    = mock_browser
        mock_pw        = MagicMock()
        bt._playwright = mock_pw

        with patch("src.tools.browser.CDP_TAB_POLICY", "new"):
            result = bt._close_attach(start=0.0)

        assert result.success
        # Page should be closed (we opened it)
        mock_page.close.assert_called_once()
        # Playwright should be stopped (frees the Playwright subprocess)
        mock_pw.stop.assert_called_once()
        # Internal state must be cleared
        assert bt._page    is None
        assert bt._browser is None


    def test_close_attach_reuse_does_not_close_page(self):
        """In reuse tab policy, we shouldn't close the user's existing tab."""
        from src.tools.browser import BrowserTools

        mock_page = _make_mock_page()
        mock_browser, mock_context = _make_mock_browser()

        bt = BrowserTools()
        bt._mode       = "attach"
        bt._page       = mock_page
        bt._context    = mock_context
        bt._browser    = mock_browser
        bt._playwright = MagicMock()

        with patch("src.tools.browser.CDP_TAB_POLICY", "reuse"):
            result = bt._close_attach(start=0.0)

        assert result.success
        # Page must NOT be closed when reusing
        mock_page.close.assert_not_called()

    def test_attach_result_data_fields(self):
        """start() in attach mode should return rich data about the session."""
        from src.tools.browser import BrowserTools

        mock_page    = _make_mock_page()
        mock_context = _make_mock_context(page=mock_page)
        mock_context.pages = [mock_page, mock_page]  # 2 pages for richer data

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]
        mock_browser.is_connected = MagicMock(return_value=True)

        mock_pw = MagicMock()
        mock_pw.chromium.connect_over_cdp = MagicMock(return_value=mock_browser)

        with patch("src.tools.browser.BROWSER_MODE", "attach"), \
             patch("src.tools.browser.CDP_URL", "http://localhost:9222"), \
             patch("src.tools.browser.CDP_TAB_POLICY", "new"), \
             patch("src.tools.browser.CDP_ATTACH_TIMEOUT_MS", 5000):

            bt = BrowserTools()
            bt._playwright = mock_pw
            result = bt._start_attach(start=0.0)

        assert result.success
        assert result.data is not None
        assert result.data.get("mode") == "attach"
        assert "cdp_url" in result.data
        assert "contexts" in result.data
        assert "pages" in result.data


# ===========================================================================
# Phase 3: Mode Selection Tests
# ===========================================================================

class TestBrowserModeSwitch:
    """Verify that BROWSER_MODE config correctly routes to launch vs attach."""

    def test_launch_mode_is_default(self):
        """With BROWSER_MODE=launch, start() should call _start_launch."""
        from src.tools.browser import BrowserTools

        bt = BrowserTools()

        with patch.object(bt, "_start_launch", return_value=MagicMock(success=True)) as mock_launch, \
             patch.object(bt, "_start_attach", return_value=MagicMock(success=True)) as mock_attach, \
             patch("src.tools.browser.sync_playwright") as mock_sp, \
             patch("src.tools.browser.BROWSER_MODE", "launch"):

            mock_sp.return_value.start = MagicMock(return_value=MagicMock())
            bt._playwright = MagicMock()  # skip actual playwright start

            bt.start()

        mock_launch.assert_called_once()
        mock_attach.assert_not_called()

    def test_attach_mode_selected_by_config(self):
        """With BROWSER_MODE=attach, start() should call _start_attach."""
        from src.tools.browser import BrowserTools

        bt = BrowserTools()

        with patch.object(bt, "_start_launch", return_value=MagicMock(success=True)) as mock_launch, \
             patch.object(bt, "_start_attach", return_value=MagicMock(success=True)) as mock_attach, \
             patch("src.tools.browser.sync_playwright") as mock_sp, \
             patch("src.tools.browser.BROWSER_MODE", "attach"):

            mock_sp.return_value.start = MagicMock(return_value=MagicMock())
            bt._playwright = MagicMock()

            bt.start()

        mock_attach.assert_called_once()
        mock_launch.assert_not_called()

    def test_mode_property_reflects_active_mode(self):
        """bt.mode should reflect which mode was actually used."""
        from src.tools.browser import BrowserTools

        bt = BrowserTools()
        assert bt.mode == "launch"  # default before start()

        bt._mode = "attach"
        assert bt.mode == "attach"

    def test_unknown_mode_falls_through_to_launch(self):
        """Unrecognised BROWSER_MODE values should fall back to launch mode."""
        from src.tools.browser import BrowserTools

        bt = BrowserTools()

        with patch.object(bt, "_start_launch", return_value=MagicMock(success=True)) as mock_launch, \
             patch.object(bt, "_start_attach", return_value=MagicMock(success=True)) as mock_attach, \
             patch("src.tools.browser.sync_playwright") as mock_sp, \
             patch("src.tools.browser.BROWSER_MODE", "bogus_value"):

            mock_sp.return_value.start = MagicMock(return_value=MagicMock())
            bt._playwright = MagicMock()

            bt.start()

        # Unknown mode → treated as launch (not attach)
        mock_launch.assert_called_once()
        mock_attach.assert_not_called()


# ===========================================================================
# Phase 3: Close Semantics Tests
# ===========================================================================

class TestCloseSemantics:
    """Verify that close() delegates correctly based on mode."""

    def test_close_launch_calls_close_launch(self):
        from src.tools.browser import BrowserTools

        bt = BrowserTools()
        bt._mode = "launch"

        with patch.object(bt, "_close_launch", return_value=MagicMock(success=True)) as mock_cl, \
             patch.object(bt, "_close_attach", return_value=MagicMock(success=True)) as mock_ca:
            bt.close()

        mock_cl.assert_called_once()
        mock_ca.assert_not_called()

    def test_close_attach_calls_close_attach(self):
        from src.tools.browser import BrowserTools

        bt = BrowserTools()
        bt._mode = "attach"

        with patch.object(bt, "_close_launch", return_value=MagicMock(success=True)) as mock_cl, \
             patch.object(bt, "_close_attach", return_value=MagicMock(success=True)) as mock_ca:
            bt.close()

        mock_ca.assert_called_once()
        mock_cl.assert_not_called()

    def test_close_launch_clears_internal_state(self):
        from src.tools.browser import BrowserTools

        bt = BrowserTools()
        bt._mode       = "launch"
        bt._page       = MagicMock()
        bt._context    = MagicMock()
        bt._browser    = MagicMock()
        bt._playwright = MagicMock()

        bt._close_launch(start=0.0)

        assert bt._page       is None
        assert bt._context    is None
        assert bt._browser    is None
        assert bt._playwright is None

    def test_close_attach_clears_internal_state(self):
        from src.tools.browser import BrowserTools

        bt = BrowserTools()
        bt._mode       = "attach"
        bt._page       = MagicMock()
        bt._context    = MagicMock()
        bt._browser    = MagicMock()
        bt._playwright = MagicMock()

        with patch("src.tools.browser.CDP_TAB_POLICY", "new"):
            bt._close_attach(start=0.0)

        assert bt._page       is None
        assert bt._context    is None
        assert bt._browser    is None
        assert bt._playwright is None


# ===========================================================================
# Existing Phase 1/2 Tests (unchanged)
# ===========================================================================

class TestBrowserToolsLaunchMode:
    """Smoke tests for launch-mode BrowserTools (mocked Playwright)."""

    def _make_bt_with_mocks(self):
        from src.tools.browser import BrowserTools
        mock_page    = _make_mock_page()
        mock_context = _make_mock_context(page=mock_page)
        mock_browser, _ = _make_mock_browser(context=mock_context)

        bt = BrowserTools()
        bt._mode       = "launch"
        bt._browser    = mock_browser
        bt._context    = mock_context
        bt._page       = mock_page
        bt._playwright = MagicMock()
        return bt, mock_page

    def test_extract_page_text_returns_content(self):
        bt, mock_page = self._make_bt_with_mocks()
        mock_page.inner_text = MagicMock(return_value="Hello world this is a test page")
        result = bt.extract_page_text()
        assert result.success
        assert result.data is not None
        assert "text" in result.data
        assert len(result.data["text"]) > 0

    def test_navigate_success(self):
        bt, mock_page = self._make_bt_with_mocks()
        mock_page.goto = MagicMock()
        mock_page.url  = "https://example.com"
        mock_page.title = MagicMock(return_value="Example Domain")

        with patch("src.tools.browser.ACTION_COOLDOWN", 0):
            result = bt.navigate("https://example.com")

        assert result.success
        assert result.data["current_url"] == "https://example.com"

    def test_navigate_adds_https_prefix(self):
        bt, mock_page = self._make_bt_with_mocks()
        captured_urls = []

        def fake_goto(url, **kwargs):
            captured_urls.append(url)
        mock_page.goto  = fake_goto
        mock_page.url   = "https://example.com"
        mock_page.title = MagicMock(return_value="Example")

        with patch("src.tools.browser.ACTION_COOLDOWN", 0):
            bt.navigate("example.com")

        assert captured_urls[0].startswith("https://")

    def test_search_web_uses_duckduckgo(self):
        bt, mock_page = self._make_bt_with_mocks()
        captured_urls = []

        def fake_goto(url, **kwargs):
            captured_urls.append(url)
            mock_page.url = url

        mock_page.goto  = fake_goto
        mock_page.title = MagicMock(return_value="DuckDuckGo")

        with patch("src.tools.browser.ACTION_COOLDOWN", 0), \
             patch("time.sleep"):
            result = bt.search_web("test query")

        assert result.success
        assert any("duckduckgo.com" in u for u in captured_urls)

    def test_extract_page_text_truncates_long_content(self):
        bt, mock_page = self._make_bt_with_mocks()
        long_text = "word " * 10000
        mock_page.inner_text = MagicMock(return_value=long_text)

        result = bt.extract_page_text(max_chars=100)
        assert result.success
        assert len(result.data["text"]) <= 150  # some slack for the truncation marker

    def test_check_for_blocked_page_detects_captcha(self):
        bt, mock_page = self._make_bt_with_mocks()
        mock_page.title     = MagicMock(return_value="CAPTCHA required")
        mock_page.inner_text = MagicMock(return_value="Please verify you are human")

        assert bt._check_for_blocked_page() is True

    def test_check_for_blocked_page_passes_normal(self):
        bt, mock_page = self._make_bt_with_mocks()
        mock_page.title     = MagicMock(return_value="Welcome to Example.com")
        mock_page.inner_text = MagicMock(return_value="Normal page content here")

        assert bt._check_for_blocked_page() is False

    def test_fill_form_success(self):
        bt, mock_page = self._make_bt_with_mocks()
        mock_page.fill        = MagicMock()
        mock_page.input_value = MagicMock(return_value="typed_value")

        with patch("src.tools.browser.ACTION_COOLDOWN", 0):
            result = bt.fill_form({"#email": "test@example.com"})

        assert result.success
        assert result.data["fields_filled"] == 1

    def test_get_links_filters_javascript_hrefs(self):
        bt, mock_page = self._make_bt_with_mocks()

        real_anchor   = MagicMock()
        real_anchor.get_attribute = MagicMock(return_value="https://example.com/page")
        real_anchor.inner_text    = MagicMock(return_value="Real Link")

        js_anchor   = MagicMock()
        js_anchor.get_attribute = MagicMock(return_value="javascript:void(0)")
        js_anchor.inner_text    = MagicMock(return_value="JS Link")

        mock_page.query_selector_all = MagicMock(return_value=[real_anchor, js_anchor])

        result = bt.get_links()
        assert result.success
        links = result.data["links"]
        assert all("javascript:" not in link["href"] for link in links)
        assert len(links) == 1
        assert links[0]["text"] == "Real Link"


# ===========================================================================
# build_executor smoke test
# ===========================================================================

def test_build_executor_returns_tool_spec():
    from src.tools.browser import build_executor
    from src.registry import ToolSpec
    from src.models import ToolType

    spec = build_executor()
    assert isinstance(spec, ToolSpec)
    assert spec.tool_type == ToolType.BROWSER
    assert callable(spec.executor)


# ===========================================================================
# Phase 4: _get_stable_title tests
# ===========================================================================

class TestGetStableTitle:
    """Verify _get_stable_title waits for SPA titles to settle."""

    def _make_bt(self):
        from src.tools.browser import BrowserTools
        bt = BrowserTools()
        bt._mode       = "launch"
        bt._browser    = MagicMock()
        bt._browser.is_connected.return_value = True
        bt._context    = MagicMock()
        bt._page       = MagicMock()
        bt._playwright = MagicMock()
        return bt

    def test_stable_title_returns_immediately_when_unchanged(self):
        """If the title is already stable, return it after one poll cycle."""
        bt = self._make_bt()
        bt._page.title = MagicMock(return_value="Example Domain")

        with patch("src.tools.browser.time") as mock_time:
            # Make monotonic return increasing values so the loop runs
            mock_time.monotonic = MagicMock(side_effect=[0, 0, 0.25, 3])
            mock_time.sleep = MagicMock()

            title = bt._get_stable_title(max_wait=2.0, poll_interval=0.25)

        assert title == "Example Domain"

    def test_stable_title_waits_for_spa_update(self):
        """Simulate YouTube: first call returns 'YouTube', then full title."""
        bt = self._make_bt()
        # First read: generic SPA title; second read: still generic;
        # third read: JS has updated the title; fourth read: confirms stable
        bt._page.title = MagicMock(side_effect=[
            "YouTube",                                          # initial read
            "Charlie Puth - Attention (Official Video) - YouTube",  # after 0.25s
            "Charlie Puth - Attention (Official Video) - YouTube",  # confirms stable
        ])

        with patch("src.tools.browser.time") as mock_time:
            mock_time.monotonic = MagicMock(side_effect=[0, 0, 0.25, 0.5, 3])
            mock_time.sleep = MagicMock()

            title = bt._get_stable_title(max_wait=2.0, poll_interval=0.25)

        assert title == "Charlie Puth - Attention (Official Video) - YouTube"

    def test_stable_title_returns_on_timeout(self):
        """If the title keeps changing, return whatever we have at timeout."""
        bt = self._make_bt()
        bt._page.title = MagicMock(side_effect=[
            "YouTube",
            "Loading...",
            "Still Loading...",
        ])

        with patch("src.tools.browser.time") as mock_time:
            # After initial read + 2 polls the deadline is passed
            mock_time.monotonic = MagicMock(side_effect=[0, 0, 0.3, 0.6, 3])
            mock_time.sleep = MagicMock()

            title = bt._get_stable_title(max_wait=0.5, poll_interval=0.25)

        # Should return the last value it read
        assert title == "Still Loading..."

    def test_navigate_uses_stable_title(self):
        """navigate() result should contain the fully-resolved SPA title."""
        bt = self._make_bt()
        bt._page.goto = MagicMock()
        bt._page.url  = "https://www.youtube.com/watch?v=nfs8NYg7yQM"
        # Simulate title updating mid-poll
        bt._page.title = MagicMock(side_effect=[
            "YouTube",          # _check_for_blocked_page (uses raw title)
            "YouTube",          # _get_stable_title: initial read
            "Charlie Puth - Attention (Official Video) - YouTube",
            "Charlie Puth - Attention (Official Video) - YouTube",
        ])
        bt._page.inner_text = MagicMock(return_value="page body text")

        with patch("src.tools.browser.ACTION_COOLDOWN", 0), \
             patch("src.tools.browser.time") as mock_time:
            mock_time.monotonic = MagicMock(side_effect=[
                0,     # navigate start
                0,     # _get_stable_title: deadline calc
                0,     # first poll monotonic
                0.25,  # second poll monotonic
                0.5,   # loop check → stable
                3,     # _ms end
            ])
            mock_time.sleep = MagicMock()

            result = bt.navigate("https://www.youtube.com/watch?v=nfs8NYg7yQM")

        assert result.success
        assert result.data["page_title"] == "Charlie Puth - Attention (Official Video) - YouTube"