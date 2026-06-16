import os
import time
import logging
from typing import Optional, TypedDict
 
from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END
 
from src.brain import Brain
from src.verifier import verify_step
from src.state import StateSlots, resolve_placeholder, describe_slots
from src.models import (
    Plan,
    Step,
    StepResult,
    ToolType,
    ActionType,
    VerificationStatus,
    ToolResult,
)
from src.config import MAX_RETRIES_PER_STEP, MAX_TOTAL_STEPS
 
load_dotenv()
log = logging.getLogger(__name__)
 
# Graph state
class GraphState(TypedDict):
    # Input
    task: str
 
    # Planning
    plan:               Optional[Plan]
    current_step_index: int
 
    # Execution tracking
    step_results:  list[StepResult]
    retry_count:   int
    is_done:       bool
    is_failed:     bool
 
    # Memory
    memory_hints:  Optional[str]
 
    # Error handling
    last_error:       Optional[str]
    ask_user_message: Optional[str]
 
    # Timing
    task_start_ms: Optional[int]
 
    # Inter-node communication (temporary, reset each step)
    _last_tool_result:  Optional[ToolResult]
    _last_step_result:  Optional[StepResult]
 
    # Typed cross-step state (replaces v2's single extracted_content str)
    slots: StateSlots
 
# Tool executors - one function per tool type
 
def _execute_files(step: Step) -> ToolResult:
    from src.tools.files import FileTools
    import os
 
    ft = FileTools()
 
    # Resolve Desktop/Documents/Downloads properly and fix wrong user paths
    def resolve_path(path: str) -> str:
        import os
        import re
 
        # Fix wrong user paths — Gemini sometimes generates \Default\ or \User\
        if "\\Default\\" in path or "/Default/" in path:
            actual_user = os.path.expanduser("~")
            # Replace the wrong user segment
            path = re.sub(
                r'C:\\Users\\[^\\]+\\',
                actual_user.rstrip("\\") + "\\",
                path
            )
 
        # Resolve shorthand names
        home = os.path.expanduser("~")
        lower = path.lower().strip()
 
        if lower == "desktop":
            onedrive = os.path.join(home, "OneDrive", "Desktop")
            regular = os.path.join(home, "Desktop")
            return onedrive if os.path.exists(onedrive) else regular
 
        if lower == "downloads":
            return os.path.join(home, "Downloads")
 
        if lower == "documents":
            onedrive = os.path.join(home, "OneDrive", "Documents")
            regular = os.path.join(home, "Documents")
            return onedrive if os.path.exists(onedrive) else regular
 
        if lower == "pictures":
            onedrive = os.path.join(home, "OneDrive", "Pictures")
            regular = os.path.join(home, "Pictures")
            return onedrive if os.path.exists(onedrive) else regular
 
        # If path starts with C:\Users\ but has wrong username
        if path.startswith("C:\\Users\\"):
            parts = path.split("\\")
            if len(parts) > 2 and parts[2] != os.path.basename(home):
                parts[2] = os.path.basename(home)
                path = "\\".join(parts)
 
        return path
 
    target = resolve_path(step.target)
    value = step.value or ""
 
    action_map = {
        ActionType.MOVE_FILE: lambda: ft.move_file(
            target, resolve_path(value)
        ),
        ActionType.COPY_FILE: lambda: ft.copy_file(
            target, resolve_path(value)
        ),
        ActionType.RENAME_FILE: lambda: ft.rename_file(
            target, value
        ),
        ActionType.DELETE_FILE: lambda: ft.delete_file(target),
        ActionType.CREATE_FOLDER: lambda: ft.create_folder(
            os.path.join(target, value) if value else target
        ),
        ActionType.LIST_FILES: lambda: ft.list_files(target),
        ActionType.FIND_FILES: lambda: ft.find_files(
            target, value or "*"
        ),
        ActionType.ORGANIZE_FILES: lambda: ft.organize_by_type(target),
        ActionType.WRITE_FILE: lambda: ft.write_file(
            target,
            value or ""
        ),
    }
 
    handler = action_map.get(step.action)
    if handler is None:
        return ToolResult(
            success=False,
            message=f"Unknown files action: {step.action.value}",
            error="UnknownAction",
            data={}
        )
 
    return handler()
 
 
def _execute_windows_ui(step: Step) -> ToolResult:
    from src.tools.windows_ui import WindowsUITools
    ui = WindowsUITools()
 
    action_map = {
        ActionType.OPEN_APP:   lambda: ui.open_app(step.target),
        ActionType.CLOSE_APP:  lambda: ui.close_app(step.target),
        ActionType.FOCUS_APP:  lambda: ui.focus_app(step.target),
        ActionType.CLICK:      lambda: ui.click_element_by_name(
            step.target, control_type=None
        ),
        ActionType.TYPE_TEXT:  lambda: ui.type_into_focused(step.value or ""),
        ActionType.PRESS_KEY:  lambda: ui.press_key(step.target),
        ActionType.SCROLL:     lambda: ui.scroll_in_app(
            step.target,
            direction=step.value or "down"
        ),
        ActionType.SELECT:     lambda: ui.click_element_by_name(
            step.target, control_type="ListItem"
        ),
        ActionType.PRESS_KEY: lambda: ui.press_key(
            step.value or step.target  # key is in value, target is the app
        ),
    }
 
    handler = action_map.get(step.action)
    if handler is None:
        return ToolResult(
            success=False,
            message=f"Unknown windows_ui action: {step.action.value}",
            error="UnknownAction",
            data={}
        )
    return handler()
 
 
def _execute_browser(step: Step, state: GraphState) -> ToolResult:
    bt = _get_browser_instance()
 
    # Resolve {{slot_name}} placeholders (e.g. {{browser_url}},
    # {{extracted_content}}) in target. Falls back to the raw target
    # string if it isn't a placeholder or the slot is empty.
    target = resolve_placeholder(step.target, state["slots"])
 
    action_map = {
        ActionType.NAVIGATE:          lambda: bt.navigate(target),
        ActionType.SEARCH_WEB:        lambda: bt.search_web(target),
        ActionType.CLICK_ELEMENT:     lambda: bt.click_element(text=target),
        ActionType.FILL_FORM:         lambda: bt.fill_field(
            target, step.value or ""
        ),
        ActionType.EXTRACT_TEXT:      lambda: bt.extract_page_text(
            selector=target
            if target not in ("body", "page", "all")
            else None
        ),
        ActionType.SEARCH_AND_EXTRACT: lambda: bt.search_and_extract(
            target
        ),
        ActionType.SEARCH_EXTRACT_AND_SUMMARIZE: lambda: bt.search_extract_and_summarize(
            target, step.value
        ),
        ActionType.WAIT_FOR_PAGE:     lambda: bt.wait_for_text(target),
        ActionType.GET_FIRST_RESULT:  lambda: bt.get_first_result_url(),
    }
 
    handler = action_map.get(step.action)
    if handler is None:
        return ToolResult(
            success=False,
            message=f"Unknown browser action: {step.action.value}",
            error="UnknownAction",
            data={}
        )
    return handler()
 
 
def _execute_apps(step: Step) -> ToolResult:
    from src.tools.apps import SpotifyTools, NotionTools, SystemTools
 
    spotify = SpotifyTools()
    system  = SystemTools()
    notion  = NotionTools()
 
    action_map = {
        ActionType.SPOTIFY_PLAY:     lambda: spotify.play(),
        ActionType.SPOTIFY_PAUSE:    lambda: spotify.pause(),
        ActionType.SPOTIFY_NEXT:     lambda: spotify.next_track(),
        ActionType.SPOTIFY_PLAYLIST: lambda: spotify.open_playlist_by_name(
            step.target
        ),
        ActionType.CLIPBOARD_COPY:   lambda: system.copy_to_clipboard(
            step.value or step.target
        ),
        ActionType.CLIPBOARD_PASTE:  lambda: system.get_clipboard(),
        ActionType.VOLUME_SET:       lambda: system.set_system_volume(
            int(step.value or "50")
        ),
        ActionType.WAIT:             lambda: system.wait_seconds(
            float(step.value or "1")
        ),
        ActionType.NOTION_CREATE:    lambda: notion.create_page(
            parent_page_id=step.target,
            title=step.value or "New Page",
        ),
        ActionType.NOTION_APPEND:    lambda: notion.append_text(
            page_id=step.target,
            text=step.value or "",
        ),
    }
 
    handler = action_map.get(step.action)
    if handler is None:
        return ToolResult(
            success=False,
            message=f"Unknown apps action: {step.action.value}",
            error="UnknownAction",
            data={}
        )
    return handler()
 
 
def _execute_ocr(step: Step) -> ToolResult:
    from src.tools.ocr import OCRTools
    import pyautogui
 
    ocr = OCRTools()
 
    if step.action == ActionType.CLICK:
        # step.target is the text to find on screen
        # step.value is the app window to search in (optional)
        if step.value:
            # Search in specific window
            result = ocr.find_text_in_window(step.value, step.target)
        else:
            # Search full screen
            result = ocr.find_text_on_screen(step.target)
 
        if result.success:
            pyautogui.click(
                result.data["center_x"],
                result.data["center_y"]
            )
        return result
 
    if step.action == ActionType.TYPE_TEXT:
        pyautogui.write(step.value or step.target, interval=0.05)
        return ToolResult(
            success=True,
            message=f"Typed via OCR fallback",
            data={"field_value": step.value or step.target}
        )
 
    return ToolResult(
        success=False,
        message=f"Unknown OCR action: {step.action.value}",
        error="UnknownAction",
        data={}
    )
 
 
def _execute_vision(step: Step, brain: Brain) -> ToolResult:
    from src.tools.vision import VisionTools
    from google import genai
 
    client = genai.Client()
    vt     = VisionTools(client)
 
    result = vt.decide_action(
        task_step      = step.description,
        app_name       = step.target,
        prior_attempts = [f"Primary tool {step.tool.value} failed"],
    )
 
    if result.success and result.data.get("coordinates"):
        import pyautogui
        coords = result.data["coordinates"]
        pyautogui.click(coords["x"], coords["y"])
 
    return result
 
 
# ---------------------------------------------------------------------------
# Browser instance management
# ---------------------------------------------------------------------------
 
_browser_instance = None
 
def _get_browser_instance():
    """Return a persistent browser instance for the current task."""
    global _browser_instance
    from src.tools.browser import BrowserTools
 
    if _browser_instance is None:
        _browser_instance = BrowserTools()
        _browser_instance.start()
 
    return _browser_instance
 
def _close_browser_instance():
    """Close and clear the browser instance."""
    global _browser_instance
    if _browser_instance is not None:
        try:
            _browser_instance.close()
        except Exception:
            pass
        _browser_instance = None
 
 
# ---------------------------------------------------------------------------
# Route to correct executor
# ---------------------------------------------------------------------------
 
def _execute_step(step: Step, brain: Brain, state: GraphState) -> ToolResult:
    """
    Route a step to the correct tool executor.
    Resolves {{slot_name}} placeholders (including the {{extracted_content}}
    alias) in step.value before dispatch. step.target placeholder
    resolution for browser steps happens inside _execute_browser since
    that's currently the only tool whose target commonly references a
    prior slot (e.g. navigating to {{browser_url}}); other tools mostly
    use value for substituted content.
    """
    resolved_step = step
 
    if step.value and step.value.startswith("{{") and step.value.endswith("}}"):
        resolved_value = resolve_placeholder(step.value, state["slots"])
 
        # If resolve_placeholder returned the placeholder unchanged, the
        # slot was empty/unknown — that's a hard failure for steps that
        # need real content (e.g. write_file).
        if resolved_value == step.value:
            return ToolResult(
                success=False,
                message=f"No content available for placeholder '{step.value}'",
                error="EmptySlot",
                data={}
            )
 
        # Truncate to a reasonable file size, same cap as v2 had
        if isinstance(resolved_value, str):
            resolved_value = resolved_value[:10000]
 
        resolved_step = Step(
            step_number           = step.step_number,
            tool                  = step.tool,
            action                = step.action,
            target                = step.target,
            value                 = resolved_value,
            description           = step.description,
            expected_outcome      = step.expected_outcome,
            fallback_tool         = step.fallback_tool,
            requires_verification = step.requires_verification,
        )
 
    tool = resolved_step.tool
 
    try:
        if tool == ToolType.FILES:
            return _execute_files(resolved_step)
        elif tool == ToolType.WINDOWS_UI:
            return _execute_windows_ui(resolved_step)
        elif tool == ToolType.BROWSER:
            return _execute_browser(resolved_step, state)
        elif tool == ToolType.APPS:
            return _execute_apps(resolved_step)
        elif tool == ToolType.OCR:
            return _execute_ocr(resolved_step)
        elif tool == ToolType.VISION:
            return _execute_vision(resolved_step, brain)
    
        else:
            return ToolResult(
                success=False,
                message=f"No executor for tool: {tool.value}",
                error="UnknownTool",
                data={}
            )
 
    except Exception as e:
        log.error("Executor crashed for tool=%s: %s", tool.value, e)
        return ToolResult(
            success=False,
            message=f"Executor crashed: {e}",
            error="ExecutorCrash",
            data={}
        )
 
 
# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------
 
def plan_node(state: GraphState) -> dict:
    """
    Call Brain to produce a Plan for the task.
    Stores the plan and initializes execution tracking.
    """
    print(f"\n{'='*50}")
    print(f"  TASK: {state['task']}")
    print(f"{'='*50}")
    print("\n[PLAN] Generating execution plan...")
 
    brain = Brain()
    plan  = brain.plan_task(state["task"])
 
    if plan is None:
        print("[PLAN] Failed to generate plan")
        return {
            "is_failed":  True,
            "last_error": "Brain could not generate a plan for this task",
            "task_start_ms": int(time.monotonic() * 1000),
        }
 
    print(f"[PLAN] {plan.total_steps} steps planned ({plan.estimated_complexity})")
    if plan.notes:
        print(f"[PLAN] Note: {plan.notes}")
 
    for step in plan.steps:
        print(
            f"  Step {step.step_number}: [{step.tool.value}] "
            f"{step.action.value} → '{step.target}'"
        )
 
    return {
        "plan":           plan,
        "current_step_index": 0,
        "step_results":   [],
        "retry_count":    0,
        "is_done":        False,
        "is_failed":      False,
        "last_error":     None,
        "task_start_ms":  int(time.monotonic() * 1000),
        "slots":          StateSlots(),
    }
 
 
def route_node(state: GraphState) -> dict:
    """
    Check if there are more steps to execute.
    Determines next action — execute, complete, or fail.
    """
    plan  = state["plan"]
    index = state["current_step_index"]
 
    if state["is_failed"]:
        return {}
 
    if plan is None or index >= len(plan.steps):
        return {"is_done": True}
 
    if index >= MAX_TOTAL_STEPS:
        print(f"\n[ROUTE] Max steps reached ({MAX_TOTAL_STEPS})")
        return {"is_done": True}
 
    step = plan.steps[index]
    print(
        f"\n[ROUTE] Step {step.step_number}/{plan.total_steps}: "
        f"{step.description}"
    )
 
    return {}
 
 
def execute_node(state: GraphState) -> dict:
    plan        = state["plan"]
    index       = state["current_step_index"]
    step        = plan.steps[index]
 
    print(
        f"[EXEC] [{step.tool.value}] "
        f"{step.action.value} → '{step.target}'"
        + (f" = '{step.value[:50]}...'" if step.value and len(step.value) > 50 else f" = '{step.value}'" if step.value else "")
    )
 
    brain       = Brain()
    tool_result = _execute_step(step, brain, state)
 
    print(
        f"[EXEC] Result: {'✓' if tool_result.success else '✗'} "
        f"{tool_result.message}"
    )
    if tool_result.error:
        print(f"[EXEC] Error: {tool_result.error}")
 
    # Capture output into the correct typed slot based on tool + action,
    # rather than one shared `extracted` string. This is the core Phase 1
    # change: each output type gets its own home so a later step can
    # reference it precisely (e.g. {{browser_url}} vs {{ocr_text}}) while
    # {{extracted_content}} / last_text still works as a generic fallback.
    slots = state["slots"]
 
    if tool_result.success and tool_result.data:
        data = tool_result.data
 
        if step.tool == ToolType.BROWSER:
            text = data.get("text")
            url  = data.get("current_url") or data.get("url")
            title = data.get("page_title")
 
            if text:
                slots.set_text("browser_text", text)
                print(f"[EXEC] Extracted {len(text.split())} words (cleaned)")
            if url:
                slots.browser_url = url
                # A bare URL is also reasonable "last_text" content if
                # nothing else has been captured yet (e.g. get_first_result
                # feeding straight into navigate).
                if not text:
                    slots.last_text = url
                print(f"[EXEC] URL: {url}")
            if title:
                slots.browser_title = title
 
        elif step.tool == ToolType.FILES:
            files = data.get("files")
            matches = data.get("matches")
            moved = data.get("moved")
 
            if files:
                slots.file_list = files
                slots.set_text("last_text", f"Files found ({len(files)}):\n" + "\n".join(files))
                print(f"[EXEC] Found {len(files)} file(s)")
            elif matches:
                slots.file_list = matches
                slots.set_text("last_text", f"Matches found ({len(matches)}):\n" + "\n".join(matches))
                print(f"[EXEC] Found {len(matches)} match(es)")
            elif moved:
                slots.moved_files = moved
                lines = [f"{src} → {dst}" for src, dst in moved.items()]
                slots.set_text("last_text", f"Files organized ({len(moved)}):\n" + "\n".join(lines))
                print(f"[EXEC] Organized {len(moved)} file(s)")
 
        elif step.tool == ToolType.OCR:
            text = data.get("text") or data.get("full_text")
            if text:
                slots.set_text("ocr_text", text)
                print(f"[EXEC] OCR captured {len(text.split())} word(s)")
 
        elif step.tool == ToolType.APPS:
            content = data.get("content")
            if content:
                slots.clipboard_text = content
                slots.last_text = content
 
        elif step.tool == ToolType.VISION:
            description = (
                data.get("description")
                or data.get("reasoning")
                or data.get("target")
            )
            if description:
                slots.vision_result = description
                slots.last_text = description
 
    return {
        "_last_tool_result":  tool_result,
        "slots":              slots,
    }
 
 
def verify_node(state: GraphState) -> dict:
    """
    Verify the current step succeeded.
    Uses verifier.py to check expected_outcome without screenshots.
    """
    plan        = state["plan"]
    index       = state["current_step_index"]
    step        = plan.steps[index]
    tool_result = state.get("_last_tool_result")
 
    if tool_result is None:
        tool_result = ToolResult(
            success=False,
            message="No tool result found",
            error="MissingResult",
            data={}
        )
 
    step_result = verify_step(step, tool_result)
    step_result.retry_count = state["retry_count"]
 
    status_symbol = {
        VerificationStatus.SUCCESS:   "✓",
        VerificationStatus.UNCERTAIN: "?",
        VerificationStatus.FAILED:    "✗",
    }
 
    print(
        f"[VERIFY] {status_symbol[step_result.status]} "
        f"{step_result.message}"
    )
 
    new_results = list(state["step_results"]) + [step_result]
 
    if step_result.status == VerificationStatus.FAILED:
        return {
            "step_results": new_results,
            "last_error":   step_result.message,
            "_last_step_result": step_result,
        }
 
    # Success or uncertain — advance to next step
    return {
        "step_results":       new_results,
        "current_step_index": index + 1,
        "retry_count":        0,
        "last_error":         None,
        "_last_step_result":  step_result,
    }
 
 
def retry_node(state: GraphState) -> dict:
    plan        = state["plan"]
    index       = state["current_step_index"]
    step        = plan.steps[index]
    retry_count = state["retry_count"] + 1
 
    print(f"[RETRY] Attempt {retry_count} for step {step.step_number}")
 
    # Too many retries
    if retry_count > MAX_RETRIES_PER_STEP:
        brain = Brain()
        brain.record_step_failure(
            app_name = (
                plan.apps_involved[0]
                if plan.apps_involved else "unknown"
            ),
            step   = step,
            reason = state.get("last_error", "unknown"),
        )
 
        # Check if this is the LAST step and core work is done
        # If we have extracted content or completed most steps,
        # skip this step rather than failing the whole task
        is_last_step = index >= len(plan.steps) - 1
        steps_completed = len(state.get("step_results", []))
        majority_done = steps_completed >= (len(plan.steps) - 1)
 
        if is_last_step or majority_done:
            print(
                f"[RETRY] Skipping optional step "
                f"{step.step_number} — core task already complete"
            )
            return {
                "retry_count":        retry_count,
                "current_step_index": index + 1,  # advance past failed step
                "last_error":         None,
            }
 
        print(
            f"[RETRY] Step {step.step_number} failed "
            f"{MAX_RETRIES_PER_STEP} times — asking user"
        )
        return {
            "retry_count":    retry_count,
            "is_failed":      True,
            "ask_user_message": (
                f"Step {step.step_number} failed after "
                f"{MAX_RETRIES_PER_STEP} attempts.\n"
                f"Task: {step.description}\n"
                f"Last error: {state.get('last_error', 'unknown')}"
            ),
        }
 
    # Retry 1 — try fallback tool
    if retry_count == 1 and step.fallback_tool:
        print(
            f"[RETRY] Trying fallback tool: "
            f"{step.fallback_tool.value}"
        )
        fallback_step = Step(
            step_number           = step.step_number,
            tool                  = step.fallback_tool,
            action                = step.action,
            target                = step.target,
            value                 = step.value,
            description           = step.description + " (fallback)",
            expected_outcome      = step.expected_outcome,
            fallback_tool         = None,
            requires_verification = step.requires_verification,
        )
        new_steps        = list(plan.steps)
        new_steps[index] = fallback_step
        new_plan = Plan(
            task_summary         = plan.task_summary,
            total_steps          = plan.total_steps,
            apps_involved        = plan.apps_involved,
            estimated_complexity = plan.estimated_complexity,
            steps                = new_steps,
            notes                = plan.notes,
        )
        return {
            "plan":        new_plan,
            "retry_count": retry_count,
        }
 
    # Retry 2 — ask Brain to replan
    print(f"[RETRY] Asking Brain to replan step {step.step_number}")
    brain    = Brain()
    new_step = brain.replan_step(
        original_step  = step,
        failure_reason = state.get("last_error", "unknown"),
        attempt        = retry_count,
    )
 
    if new_step:
        print(
            f"[RETRY] Replanned: [{new_step.tool.value}] "
            f"{new_step.action.value} → '{new_step.target}'"
        )
        new_steps        = list(plan.steps)
        new_steps[index] = new_step
        new_plan = Plan(
            task_summary         = plan.task_summary,
            total_steps          = plan.total_steps,
            apps_involved        = plan.apps_involved,
            estimated_complexity = plan.estimated_complexity,
            steps                = new_steps,
            notes                = plan.notes,
        )
        return {
            "plan":        new_plan,
            "retry_count": retry_count,
        }
 
    # No replan — skip if majority of work is done
    steps_completed = len(state.get("step_results", []))
    majority_done   = steps_completed >= (len(plan.steps) - 1)
 
    if majority_done:
        print(
            f"[RETRY] No replan available — "
            f"skipping step {step.step_number} (majority done)"
        )
        return {
            "retry_count":        retry_count,
            "current_step_index": index + 1,
            "last_error":         None,
        }
 
    return {
        "retry_count":    retry_count,
        "is_failed":      True,
        "ask_user_message": (
            f"Could not find an alternative for step "
            f"{step.step_number}: {step.description}"
        ),
    }
 
 
def complete_node(state: GraphState) -> dict:
    plan     = state["plan"]
    results  = state["step_results"]
    start_ms = state.get("task_start_ms", 0)
    elapsed  = int(time.monotonic() * 1000) - start_ms
 
    print(f"\n{'='*50}")
    print(f"  TASK COMPLETE")
    print(f"{'='*50}")
    print(f"  Steps executed: {len(results)}/{plan.total_steps}")
    print(f"  Total time:     {elapsed}ms")
 
    # Show final slot state (replaces v2's single extracted_content dump)
    slots = state.get("slots")
    if slots:
        print(f"\n{'='*50}")
        print("  RESULT")
        print(f"{'='*50}")
        print(describe_slots(slots))
 
        # Preserve v2's behavior of printing the main text result in full
        if slots.last_text:
            preview = slots.last_text[:2000]
            print(f"\n{preview}")
            if len(slots.last_text) > 2000:
                print("\n[... truncated ...]")
 
    brain = Brain()
    brain.record_success(
        task        = state["task"],
        plan        = plan,
        duration_ms = elapsed,
    )
 
    _close_browser_instance()
    return {"is_done": True}
 
def failed_node(state: GraphState) -> dict:
    """
    Task failed — print error and clean up.
    """
    print(f"\n{'='*50}")
    print(f"  TASK FAILED")
    print(f"{'='*50}")
 
    if state.get("ask_user_message"):
        print(f"\n[ACTION NEEDED]\n{state['ask_user_message']}")
    elif state.get("last_error"):
        print(f"\n[ERROR] {state['last_error']}")
 
    _close_browser_instance()
 
    return {"is_done": True, "is_failed": True}
 
 
# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------
 
def should_execute(state: GraphState) -> str:
    """After route_node — decide whether to execute or finish."""
    if state.get("is_failed"):
        return "failed"
    if state.get("is_done"):
        return "complete"
    return "execute"
 
 
def should_verify_or_retry(state: GraphState) -> str:
    """After execute_node — always verify."""
    return "verify"
 
 
def after_verify(state: GraphState) -> str:
    """After verify_node — continue, retry, or finish."""
    if state.get("is_done"):
        return "complete"
    if state.get("is_failed"):
        return "failed"
 
    last_result = state.get("_last_step_result")
    if last_result and last_result.status == VerificationStatus.FAILED:
        return "retry"
 
    # Check if all steps are done
    plan  = state.get("plan")
    index = state.get("current_step_index", 0)
    if plan and index >= len(plan.steps):
        return "complete"
 
    return "route"
 
 
def after_retry(state: GraphState) -> str:
    """After retry_node — re-execute or fail."""
    if state.get("is_failed"):
        return "failed"
    return "execute"
 
 
def after_plan(state: GraphState) -> str:
    """After plan_node — route or fail."""
    if state.get("is_failed"):
        return "failed"
    return "route"
 
 
# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------
 
def build_graph():
    """Construct and compile the LangGraph workflow."""
    workflow = StateGraph(GraphState)
 
    # Add nodes
    workflow.add_node("plan",     plan_node)
    workflow.add_node("route",    route_node)
    workflow.add_node("execute",  execute_node)
    workflow.add_node("verify",   verify_node)
    workflow.add_node("retry",    retry_node)
    workflow.add_node("complete", complete_node)
    workflow.add_node("failed",   failed_node)
 
    # Entry point
    workflow.add_edge(START, "plan")
 
    # Conditional edges
    workflow.add_conditional_edges(
        "plan",
        after_plan,
        {"route": "route", "failed": "failed"}
    )
    workflow.add_conditional_edges(
        "route",
        should_execute,
        {"execute": "execute", "complete": "complete", "failed": "failed"}
    )
    workflow.add_conditional_edges(
        "execute",
        should_verify_or_retry,
        {"verify": "verify"}
    )
    workflow.add_conditional_edges(
        "verify",
        after_verify,
        {
            "route":    "route",
            "retry":    "retry",
            "complete": "complete",
            "failed":   "failed",
        }
    )
    workflow.add_conditional_edges(
        "retry",
        after_retry,
        {"execute": "execute", "failed": "failed"}
    )
 
    # Terminal nodes go to END
    workflow.add_edge("complete", END)
    workflow.add_edge("failed",   END)
 
    return workflow.compile()
 
 
 
 
# Compiled graph — imported by main.py
app = build_graph()