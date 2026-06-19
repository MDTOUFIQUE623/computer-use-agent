from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel

#Enums

class ToolType(str, Enum):
    WINDOWS_UI = "windows_ui"
    BROWSER    = "browser"
    FILES      = "files"
    APPS       = "apps"
    OCR        = "ocr"
    VISION     = "vision"
    SYSTEM     = "system"


class ActionType(str, Enum):
    # App control
    OPEN_APP   = "open_app"
    CLOSE_APP  = "close_app"
    FOCUS_APP  = "focus_app"

    # UI interaction
    CLICK      = "click"
    TYPE_TEXT  = "type_text"
    PRESS_KEY  = "press_key"
    SCROLL     = "scroll"
    SELECT     = "select"
    DRAG       = "drag"

    # Browser
    NAVIGATE      = "navigate"
    SEARCH_WEB    = "search_web"
    SEARCH_ON_PAGE = "search_on_page"
    CLICK_ELEMENT = "click_element"
    FILL_FORM     = "fill_form"
    EXTRACT_TEXT  = "extract_text"
    WAIT_FOR_PAGE = "wait_for_page"
    GET_FIRST_RESULT = "get_first_result"
    SEARCH_AND_EXTRACT          = "search_and_extract"
    SEARCH_EXTRACT_AND_SUMMARIZE = "search_extract_and_summarize"

    # Files
    MOVE_FILE      = "move_file"
    COPY_FILE      = "copy_file"
    RENAME_FILE    = "rename_file"
    DELETE_FILE    = "delete_file"
    CREATE_FOLDER  = "create_folder"
    LIST_FILES     = "list_files"
    FIND_FILES     = "find_files"
    ORGANIZE_FILES = "organize_files"
    WRITE_FILE     = "write_file"

    # App APIs
    SPOTIFY_PLAY     = "spotify_play"
    SPOTIFY_PAUSE    = "spotify_pause"
    SPOTIFY_NEXT     = "spotify_next"
    SPOTIFY_PLAYLIST = "spotify_playlist"
    NOTION_CREATE    = "notion_create_page"
    NOTION_APPEND    = "notion_append"

    # System
    CLIPBOARD_COPY  = "clipboard_copy"
    CLIPBOARD_PASTE = "clipboard_paste"
    VOLUME_SET      = "volume_set"
    SCREENSHOT      = "screenshot"
    WAIT            = "wait"


class VerificationStatus(str, Enum):
    SUCCESS   = "success"
    UNCERTAIN = "uncertain"
    FAILED    = "failed"


#Core Execution models

class Step(BaseModel):
    """One atomic action inside a Plan."""

    step_number:          int
    tool:                 ToolType
    action:               ActionType
    target:               str
    value:                Optional[str]      = None
    description:          str
    expected_outcome:     str
    fallback_tool:        Optional[ToolType] = None
    requires_verification: bool              = True


class Plan(BaseModel):
    """Full task plan returned by Gemini."""

    task_summary:         str
    total_steps:          int
    apps_involved:        list[str]
    estimated_complexity: Literal["simple", "medium", "complex"]
    steps:                list[Step]
    notes:                Optional[str] = None


class ToolResult(BaseModel):
    """Returned by every function in every tool file."""

    success:     bool
    message:     str
    data:        Optional[dict] = None
    error:       Optional[str]  = None
    duration_ms: Optional[int]  = None


class StepResult(BaseModel):
    """Outcome of executing and verifying one Step."""

    step_number:  int
    status:       VerificationStatus
    message:      str
    retry_count:  int      = 0
    tool_used:    ToolType
    duration_ms:  Optional[int] = None

#Memory models
class TaskPattern(BaseModel):
    """Successful task pattern stored for future reuse."""

    task_description:  str
    tool_sequence:     list[ToolType]
    action_sequence:   list[ActionType]
    apps_involved:     list[str]
    success_rate:      float
    last_used:         str            # ISO datetime string
    avg_duration_ms:   Optional[int] = None


class FailureRecord(BaseModel):
    """Tracks what failed so Brain can avoid repeating mistakes."""

    app_name:         str
    tool_attempted:   ToolType
    action_attempted: ActionType
    failure_reason:   str
    timestamp:        str
    resolved:         bool = False


class UserPreference(BaseModel):
    """Things discovered about the user's environment once, reused always."""

    key:           str
    value:         str
    discovered_at: str

#Graph state (used as field types inside the langGraph TypedDict)

class AgentState(BaseModel):
    """Mirrors the LangGraph GraphState but as a typed Pydantic model."""

    # Input
    task: str

    # Plan phase
    plan:                Optional[Plan] = None
    current_step_index:  int            = 0

    # Execution tracking
    step_results:   list[StepResult] = []
    retry_count:    int              = 0
    is_done:        bool             = False
    is_failed:      bool             = False

    # Memory
    memory_hints:   Optional[str]   = None

    # Error / user interaction
    last_error:         Optional[str] = None
    ask_user_message:   Optional[str] = None