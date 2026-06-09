"""
Tests OCRTools + WindowsUITools on WhatsApp.

Flow:
1. Open WhatsApp
2. Focus WhatsApp
3. Open first chat (latest if chats are sorted normally)
4. OCR the visible conversation
"""

import sys
import os
import time
import pyautogui

sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from src.tools.windows_ui import WindowsUITools
from src.tools.ocr import OCRTools

ui = WindowsUITools()
ocr = OCRTools()

# -----------------------------------------------------------------------
# 1. Open WhatsApp
# -----------------------------------------------------------------------

result = ui.open_app("WhatsApp")
print("Open WhatsApp:", result.message)

assert result.success, result.error

time.sleep(2)

# -----------------------------------------------------------------------
# 2. Focus WhatsApp
# -----------------------------------------------------------------------

result = ui.focus_app("WhatsApp")
print("Focus:", result.message)

time.sleep(1)

# -----------------------------------------------------------------------
# 3. Move focus to chat list
# -----------------------------------------------------------------------

# Escape clears search boxes if active
pyautogui.press("esc")
time.sleep(0.3)

# Go to first chat (usually most recent)
pyautogui.press("home")
time.sleep(0.3)

# Open it
pyautogui.press("enter")

print("Opened first chat")

time.sleep(1.5)

# -----------------------------------------------------------------------
# 4. OCR the WhatsApp window
# -----------------------------------------------------------------------

result = ocr.read_window_text("WhatsApp")

print("\nOCR Result:", result.message)

assert result.success, result.error

text = result.data["text"]

# -----------------------------------------------------------------------
# 5. Print OCR output
# -----------------------------------------------------------------------

print("\n==================== OCR OUTPUT ====================\n")
print(text)
print("\n====================================================")

print("\nWord count:", result.data["word_count"])

# -----------------------------------------------------------------------
# 6. Simple parsing
# -----------------------------------------------------------------------

lines = [
    line.strip()
    for line in text.splitlines()
    if line.strip()
]

print("\nDetected lines:\n")

for line in lines:
    print(line)

if lines:
    print("\nLast OCR line:")
    print(lines[-1])

print("\nWhatsApp OCR test completed")