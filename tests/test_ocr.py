"""
Tests for OCRTools.
Tests window OCR, text finding, and region OCR.
Make sure at least one app with visible text is open before running.
Notepad works perfectly for this.
"""

import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

import subprocess
import time
import pyautogui

from src.tools.ocr import OCRTools

ocr = OCRTools()

# -----------------------------------------------------------------------
# Setup — open Notepad with known text so OCR has something to find
# -----------------------------------------------------------------------
subprocess.Popen(["notepad.exe"])
time.sleep(1.5)

# Type known text into Notepad
pyautogui.write("Hello from OCR test", interval=0.05)
pyautogui.press("enter")
pyautogui.write("Testing pytesseract confidence", interval=0.05)
time.sleep(0.5)

# -----------------------------------------------------------------------
# Test 1 — read all text from Notepad window
# -----------------------------------------------------------------------
result = ocr.read_window_text("Notepad")
print("Read window text:", result.message)
print("  Text found:", result.data["text"][:200])
assert result.success
assert result.data["word_count"] > 0

# -----------------------------------------------------------------------
# Test 2 — find specific text in Notepad
# -----------------------------------------------------------------------
result = ocr.find_text_in_window("Notepad", "Hello")
print("\nFind 'Hello':", result.message)
if result.success:
    print(f"  Coordinates: ({result.data['center_x']}, {result.data['center_y']})")
    print(f"  Confidence:  {result.data['confidence']:.1f}")
else:
    print("  Not found — OCR may need Tesseract path check")

# -----------------------------------------------------------------------
# Test 3 — find multi-word text
# -----------------------------------------------------------------------
result = ocr.find_text_in_window("Notepad", "OCR test")
print("\nFind 'OCR test':", result.message)

# -----------------------------------------------------------------------
# Test 4 — region sub-crop (top half of Notepad)
# -----------------------------------------------------------------------
result = ocr.read_window_text("Notepad", region="top_half")
print("\nTop half OCR:", result.message)
print("  Text:", result.data["text"][:100])

# -----------------------------------------------------------------------
# Test 5 — scan a specific screen region
# -----------------------------------------------------------------------
# Scan top-left 400x200 pixels of screen
from src.tools.windows_ui import WindowsUITools
import uiautomation as auto

ui     = WindowsUITools()
window = ui._find_window_fuzzy("Notepad")
if window:
    rect = window.BoundingRectangle
    result = ocr.scan_region(
        rect.left,
        rect.top,
        rect.right  - rect.left,
        rect.bottom - rect.top,
    )
    print("\nScan Notepad region:", result.message)
    print("  Text:", result.data["full_text"][:100])
    assert result.success
    assert result.data["word_count"] > 0
else:
    print("\nScan region: skipped (Notepad not found)")

# -----------------------------------------------------------------------
# Test 6 — find text on full screen
# -----------------------------------------------------------------------
# Test 6 — find text on full screen
# Search for text we know is in Notepad's content area
result = ocr.find_text_on_screen("Hello")
print("\nFind 'Hello' on screen:", result.message)
if result.success:
    print(f"  Found at: ({result.data['center_x']}, {result.data['center_y']})")
    assert result.data["center_x"] > 0
    assert result.data["center_y"] > 0
else:
    print("  Not found — make sure Notepad is visible on screen")
# -----------------------------------------------------------------------
# Cleanup — close Notepad without saving
# -----------------------------------------------------------------------
import pyautogui
pyautogui.hotkey("alt", "f4")
time.sleep(0.5)
pyautogui.press("tab")
time.sleep(0.2)
pyautogui.press("enter")

print("\nAll OCR tests passed")