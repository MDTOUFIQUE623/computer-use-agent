import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))

from src.memory import init_db, save_task_pattern, get_memory_hints, log_failure, get_preference, save_preference, now_iso
from src.models import TaskPattern, FailureRecord, ToolType, ActionType

# 1. Initialize DB
init_db()
print("DB initialized")

# 2. Save a task pattern
pattern = TaskPattern(
    task_description="open spotify and play my playlist",
    tool_sequence=[ToolType.WINDOWS_UI, ToolType.APPS],
    action_sequence=[ActionType.OPEN_APP, ActionType.SPOTIFY_PLAYLIST],
    apps_involved=["Spotify"],
    success_rate=1.0,
    last_used=now_iso(),
    avg_duration_ms=3200
)
save_task_pattern(pattern)
print("Pattern saved")

# 3. Get memory hints for a similar task
hints = get_memory_hints("open spotify and play a playlist")
print("Hints:", hints)

# 4. Log a failure
failure = FailureRecord(
    app_name="Spotify",
    tool_attempted=ToolType.WINDOWS_UI,
    action_attempted=ActionType.CLICK,
    failure_reason="Element not found in accessibility tree",
    timestamp=now_iso()
)
log_failure(failure)
print("Failure logged")

# 5. Save and retrieve a preference
save_preference("default_browser", "chrome")
val = get_preference("default_browser")
print("Preference:", val)

print("All memory tests passed")