import argparse
import json
import sys
import urllib.request
import urllib.error
 
 
def check_http(cdp_url: str) -> tuple[bool, str, dict]:
    """
    Check the /json/version endpoint — the simplest indicator that
    Chrome is running with the debug port open.
    """
    version_url = cdp_url.rstrip("/") + "/json/version"
    try:
        with urllib.request.urlopen(version_url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        browser = data.get("Browser", "unknown")
        ws_url  = data.get("webSocketDebuggerUrl", "")
        return True, f"Chrome responding: {browser}", data
    except urllib.error.URLError as e:
        return False, f"Cannot reach {version_url}: {e.reason}", {}
    except Exception as e:
        return False, f"Unexpected error hitting {version_url}: {e}", {}
 
 
def check_playwright(cdp_url: str, open_page: bool) -> tuple[bool, str]:
    """
    Attempt an actual Playwright CDP connect — mirrors exactly what the
    agent does in attach mode so we catch any Playwright-level issues
    (missing browser binary, version mismatch, etc.) before running a task.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, "playwright not installed (run: pip install playwright)"
 
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=8000)
            contexts = browser.contexts
            n_pages  = sum(len(c.pages) for c in contexts)
 
            if open_page:
                ctx  = contexts[0] if contexts else browser.new_context()
                page = ctx.new_page()
                page.goto("https://example.com", wait_until="domcontentloaded")
                title = page.title()
                page.close()
                detail = (
                    f"Playwright attached OK — {len(contexts)} context(s), "
                    f"{n_pages} page(s). Opened test page: '{title}'"
                )
            else:
                detail = (
                    f"Playwright attached OK — {len(contexts)} context(s), "
                    f"{n_pages} page(s)"
                )
 
            browser.close()
            return True, detail
 
    except Exception as e:
        return False, f"Playwright attach failed: {e}"
 
 
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check that Chrome CDP is reachable for Phase 3 attach mode"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:9222",
        help="CDP URL to check (default: http://localhost:9222)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open a test page (example.com) to do a deeper connectivity check",
    )
    args = parser.parse_args()
 
    cdp_url = args.url.rstrip("/")
 
    print(f"\n{'='*60}")
    print(f"  Phase 3 CDP Diagnostic")
    print(f"  Target: {cdp_url}")
    print(f"{'='*60}\n")
 
    all_ok = True
 
    # --- Check 1: HTTP endpoint ---
    ok, msg, version_data = check_http(cdp_url)
    symbol = "✓" if ok else "✗"
    print(f"  {symbol} HTTP check: {msg}")
 
    if ok and version_data:
        for key in ("Browser", "Protocol-Version", "V8-Version"):
            if key in version_data:
                print(f"      {key}: {version_data[key]}")
    
    if not ok:
        all_ok = False
        print()
        print("  Chrome is not reachable. To fix:")
        print("  1. Close any existing Chrome windows")
        print("  2. Run: scripts/launch_chrome_cdp.bat (Windows)")
        print("          scripts/launch_chrome_cdp.sh  (macOS/Linux)")
        print(f"  3. Verify: curl {cdp_url}/json/version")
        print()
 
    # --- Check 2: Playwright attach ---
    print()
    ok2, msg2 = check_playwright(cdp_url, open_page=args.open)
    symbol2 = "✓" if ok2 else "✗"
    print(f"  {symbol2} Playwright check: {msg2}")
    if not ok2:
        all_ok = False
 
    # --- Summary ---
    print()
    print(f"{'='*60}")
    if all_ok:
        print("  ✓ All checks passed — ready for BROWSER_MODE=attach")
        print()
        print("  Next steps:")
        print("  1. Log in to the sites you need in the Chrome window")
        print("  2. Add to your .env:  BROWSER_MODE=attach")
        print("  3. Run: python main.py")
    else:
        print("  ✗ One or more checks failed — see details above")
    print(f"{'='*60}\n")
 
    return 0 if all_ok else 1
 
 
if __name__ == "__main__":
    sys.exit(main())