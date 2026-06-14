import pyautogui
import os
from pathlib import Path

#Base paths
BASE_DIR = Path(__file__).resolve().parent.parent
MEMORY_PATH = BASE_DIR / "memory.db"
LOG_PATH = BASE_DIR / "logs"
LOG_PATH.mkdir(exist_ok=True)

#Models
PLANNER_MODEL="gemini-2.5-flash-lite"
VISION_MODEL="gemini-2.5-flash-lite"

#Response limits
MAX_PLAN_TOKENS = 2000
MAX_VISION_TOKENS = 2000


#Timeouts
APP_OPEN_TIMEOUT = 5 # was 10 — Notepad opens in <2s
ELEMENT_TIMEOUT = 3  # was 5
PAGE_LOAD_TIMEOUT = 15
VERIFICATION_TIMEOUT = 3
ACTION_COOLDOWN = 0.3 # was 0.5 — saves 0.2s per action

#Retry
MAX_RETRIES_PER_STEP=2
MAX_TOTAL_STEPS=25

# #OCR
# OCR_CONFIDENCE_THRESHOLD = 60
# OCR_LANGUGE = 'eng'

#Memory
# MEMORY_DB_PATH = "memory.db"
# SIMILAR_TASK_THRESHOLD = 0.7



#path to tesseract.exe on Windows
TESSERACT_PATH         = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
OCR_CONFIDENCE_MIN     = 60     # discard words below this confidence (0-100)
OCR_LANGUAGE           = "eng"
OCR_SCREENSHOT_WIDTH   = 1280   # downscale before OCR for speed


#Browser
BROWSER_TYPE = "chromium"  # Options: "chromium", "firefox", "webkit"
HEADLESS = False # Set to True = no visible window
DEFAULT_TIMEOUT = 30_000  # in milliseconds

#Memory Settings
MEMORY_DB_PATH = str(MEMORY_PATH)
SIMILAR_TASK_THRESHOLD = 0.70
MAX_MEMORY_HINTS = 3
FAILURE_THRESHOLD = 2

# Screenshot (vision fallback only)
SCREENSHOT_WIDTH = 1280

# PyAutoGUI safety
pyautogui.FAILSAFE = True   # move mouse to corner to abort
pyautogui.PAUSE    = 0.4    # small pause after every pyautogui call


# Spotify Web API
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# Notion API
NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")


# Logging level
LOG_LEVEL = "INFO"   # DEBUG | INFO | WARNING | ERROR

## OLD CODE
# WAIT_SECONDS= 1.5

# SCREENSHOT_WIDTH = 1280

# pyautogui.FAILSAFE = True
# pyautogui.PAUSE = 0.5
