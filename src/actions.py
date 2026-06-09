import time
import pyautogui

from src.brain import Action
from src.config import WAIT_SECONDS

def execute_action(action: Action) -> str:
    """Execute one Action on the real screen and return a short log string."""
    if action.action == "click":
        width, height = pyautogui.size()
        x = int(action.x / 1000 * width)
        y = int(action.y / 1000 * height)
        pyautogui.click(x, y)
        return f"Clicked at ({x}, {y})"
    
    if action.action == "type":
        pyautogui.write(action.text or "", interval=0.02)
        return f"typed {action.text!r}"
    
    if action.action == "scroll":
        clicks = -500 if action.direction == "down" else 500
        pyautogui.scroll(clicks)
        return f"scrolled {action.direction}"
    
    if action.action == "wait":
        time.sleep(WAIT_SECONDS)
        return f"waited {WAIT_SECONDS}s"
    
    return "done"