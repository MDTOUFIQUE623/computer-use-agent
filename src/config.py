import pyautogui

#Models
PLANNER_MODEL="gemini-2.0-flash"
VISION_MODEL="gemini-2.0-flash"

#Timeouts
APP_OPEN_TIMEOUT = 10
ELEMENT_TIMEOUT = 5
PAGE_LOAD_TIMEOUT = 15
VERIFICATION_TIMEOUT = 10

#OCR
OCR_CONFIDENCE_THRESHOLD = 60
OCR_LANGUGE = 'eng'

#Memory
MEMORY_DB_PATH = "memory.db"
SIMILAR_TASK_THRESHOLD = 0.7

#Retry
MAX_RETRIES_PER_STEPS=2
MAX_TOTAL_RETRIES=15

#Browser
BROWSER_TYPE = "chromium"  # Options: "chromium", "firefox", "webkit"
HEADLESS_BROWSER = False # Set to True = no visible window
## OLD CODE
# WAIT_SECONDS= 1.5

# SCREENSHOT_WIDTH = 1280

# pyautogui.FAILSAFE = True
# pyautogui.PAUSE = 0.5
