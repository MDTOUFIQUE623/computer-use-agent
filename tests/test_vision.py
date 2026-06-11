"""
Tests for VisionTools on WhatsApp.

Vision should:
1. Describe the WhatsApp window.
2. Identify the latest chat.
3. Suggest where to click.
4. Decide the next action.

Vision is used as the LAST RESORT when
UI Automation + OCR cannot solve the task.
"""

import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv()

import time

from google import genai

from src.tools.windows_ui import WindowsUITools
from src.tools.vision import VisionTools

# ----------------------------------------------------------
# Setup
# ----------------------------------------------------------

client = genai.Client()

vision = VisionTools(client)
ui = WindowsUITools()

# ----------------------------------------------------------
# Open WhatsApp
# ----------------------------------------------------------

result = ui.open_app("WhatsApp")
print("Open WhatsApp:", result.message)

assert result.success

time.sleep(2)

result = ui.focus_app("WhatsApp")
print("Focus:", result.message)

time.sleep(1)

# ----------------------------------------------------------
# Test 1
# Describe screen
# ----------------------------------------------------------

result = vision.describe_screen(app_name="WhatsApp")

print("\nDescribe screen:", result.message)

assert result.success

print("Description:")
print(result.data["description"])

print("Vision calls:", result.data["vision_calls"])

# ----------------------------------------------------------
# Test 2
# Analyze chat list
# ----------------------------------------------------------

result = vision.analyze_region(

    question="""
    Look at this WhatsApp window.

    Identify the first (latest) chat that is visible.

    Return:
    - what chat it is
    - approximate click coordinates
    - reasoning
    """,

    app_name="WhatsApp",

    prior_attempts=[
        "UIAutomation: chat list not accessible",
        "OCR: chat boundaries uncertain"
    ]
)

print("\nAnalyze region:", result.message)

if result.success:

    print("Suggestion:")
    print(result.data["suggestion"])

    print()

    print("Target:")
    print(result.data["target"])

    print()

    print("Reasoning:")
    print(result.data["reasoning"])

    print()

    print("Coordinates:")
    print(result.data["coordinates"])

else:

    print(result.error)

# ----------------------------------------------------------
# Test 3
# Decide next action
# ----------------------------------------------------------

result = vision.decide_action(

    task_step="Open the latest WhatsApp conversation",

    app_name="WhatsApp",

    prior_attempts=[

        "UIAutomation: could not find ListItem",

        "OCR: chat list ambiguous"
    ]
)

print("\nDecide action:", result.message)

if result.success:

    print("Action:", result.data["action"])

    print("Target:", result.data["target"])

    print("Reasoning:")

    print(result.data["reasoning"])

    print("Coordinates:")

    print(result.data["coordinates"])

else:

    print(result.error)

# ----------------------------------------------------------
# Test 4
# Circuit breaker
# ----------------------------------------------------------

vision._call_count = 5

result = vision.describe_screen()

print("\nCircuit breaker:", result.message)

assert result.error == "CircuitBreaker"

print("Circuit breaker working correctly")

print("\nAll Vision tests passed")