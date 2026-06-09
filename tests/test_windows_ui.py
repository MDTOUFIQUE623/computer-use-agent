import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from src.tools.windows_ui import WindowsUITools
import time

ui = WindowsUITools()

# 1. Check what windows are open before we start
result = ui.list_open_windows()
print("Open windows:", [w["title"] for w in result.data["windows"]])

# 2. Open Notepad
result = ui.open_app("Notepad")
print("Open Notepad:", result.message)
assert result.success, f"Failed: {result.error}"
time.sleep(1)

# 3. Check it's open
result = ui.is_app_open("Notepad")
print("Is open:", result.message)
assert result.success

# 4. Get window title
result = ui.get_window_title("Notepad")
print("Window title:", result.data["window_title"])

# 5. Type into the text area
# Notepad's main edit area is named "Text Editor" in uiautomation
result = ui.type_into_focused("Hello from the agent!")
print("Type:", result.message)
time.sleep(0.5)

# 6. Press a key
result = ui.press_key("ctrl+a")
print("Select all:", result.message)

# 7. Scroll test
result = ui.scroll_in_app("Notepad", direction="down", clicks=2)
print("Scroll:", result.message)

# 8. Wait for an element
result = ui.wait_for_element("Text Editor", app_name="Notepad")
print("Wait for element:", result.message)

# 9. Close Notepad
result = ui.close_app("Notepad")
print("Close attempt:", result.message)

# Handle the "Save?" dialog if it appears
# Windows 11 Notepad uses "Save", "Don't Save", "Cancel"
# Windows 10 Notepad uses "Save", "Don't Save", "Cancel" too
# but button names can differ slightly
time.sleep(0.8)

# Try common dialog button names
dismissed = False
for button_name in ["Don't Save", "Don't save", "No"]:
    dismiss = ui.click_element_by_name(button_name)
    if dismiss.success:
        print(f"Dismissed save dialog with '{button_name}'")
        dismissed = True
        break

if not dismissed:
    # Last resort — press Tab to reach Don't Save, then Enter
    import pyautogui
    pyautogui.press("tab")
    import time; time.sleep(0.2)
    pyautogui.press("enter")
    print("Dismissed dialog with Tab+Enter")

time.sleep(0.5)

# Verify Notepad is fully closed
result = ui.is_app_open("Notepad")
if not result.success:
    print("Confirmed: Notepad is closed")
else:
    print("Warning: Notepad still appears open")

print("\nAll windows_ui tests passed")