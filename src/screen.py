import pyautogui
from PIL import Image

from src.config import SCREENSHOT_WIDTH

def capture_screen() -> Image.Image:
    """Capture the full primary screen and downscale it for the vision model."""
    screenshot = pyautogui.screenshot()

    if screenshot.width > SCREENSHOT_WIDTH:
        ratio = SCREENSHOT_WIDTH / screenshot.width
        new_height = int(screenshot.height * ratio)
        screenshot = screenshot.resize((SCREENSHOT_WIDTH, new_height))
        
    return screenshot
