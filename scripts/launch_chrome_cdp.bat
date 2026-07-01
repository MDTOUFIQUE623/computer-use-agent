@echo off
REM =============================================================================
REM  launch_chrome_cdp.bat  —  Phase 3: Start Chrome with the CDP debug port open
REM =============================================================================
REM
REM  Run this script ONCE before starting the computer-use agent in attach mode.
REM  It launches Chrome with --remote-debugging-port=9222 so Playwright can
REM  connect to your real, logged-in session via CDP.
REM
REM  Usage:
REM    1. Double-click this file  (or run it from a terminal)
REM    2. Chrome opens (or reuses an existing window)
REM    3. Log in to Gmail, Google Docs, internal tools — whatever you need
REM    4. Set BROWSER_MODE=attach in your .env
REM    5. Run the agent:  python main.py
REM
REM  Notes:
REM    • This must be the ONLY Chrome instance running. If Chrome is already
REM      open without the debug port, close it first and re-run this script.
REM    • The --user-data-dir flag points at a separate profile directory so
REM      this debug session doesn't interfere with your normal Chrome profile.
REM      Change the path if you prefer a different location.
REM    • The debug port (9222) matches CDP_URL=http://localhost:9222 (default).
REM      If you change the port here, update CDP_URL in your .env to match.
REM    • --remote-debugging-port=0 disables the restriction that prevents
REM      multiple instances from using the same port.
REM
REM =============================================================================

setlocal

REM --- Locate Chrome ---
set CHROME_PATH=
for %%P in (
    "%ProgramFiles%\Google\Chrome\Application\chrome.exe"
    "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
    "%LocalAppData%\Google\Chrome\Application\chrome.exe"
) do (
    if exist %%P (
        set CHROME_PATH=%%P
        goto :found
    )
)

echo [ERROR] Chrome not found. Install Google Chrome or update CHROME_PATH in this script.
pause
exit /b 1

:found
echo [INFO] Found Chrome at: %CHROME_PATH%

REM --- Profile directory for the CDP debug session ---
REM  Using a dedicated profile keeps your normal browsing separate.
REM  Change this path if you prefer a different location.
set CDP_PROFILE=%USERPROFILE%\AppData\Local\GoogleCDP\User Data

echo [INFO] Using profile directory: %CDP_PROFILE%
echo [INFO] Starting Chrome on port 9222 ...
echo.
echo  ^> Log in to the sites you need (Gmail, etc.)
echo  ^> Then run: python main.py
echo  ^> Set BROWSER_MODE=attach in your .env first
echo.

start "" %CHROME_PATH% ^
    --remote-debugging-port=9222 ^
    --user-data-dir="%CDP_PROFILE%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-background-timer-throttling ^
    --disable-renderer-backgrounding ^
    --disable-backgrounding-occluded-windows

echo [INFO] Chrome launched. Waiting for it to be ready ...
timeout /t 2 /nobreak > nul

REM Quick connectivity check via curl (available on Windows 10+)
curl -s --max-time 3 http://localhost:9222/json/version > nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Chrome is reachable at http://localhost:9222
) else (
    echo [WARN] Could not verify Chrome CDP endpoint yet.
    echo        Give Chrome a few more seconds to start, then check:
    echo        http://localhost:9222/json/version
)

endlocal