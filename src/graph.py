import os
import time
import logging
from typing import Optional, TypedDict

from dotenv import load_dotenv
from langgraph.graph import StateGraph, START, END

from src.brain import Brain
from src.verifier import verify_step
from src.state import StateSlots, resolve_placeholder, describe_slots
from src.registry import get_registry, ExecutionContext
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

    # Typed cross-step state (Phase 1)
    slots: StateSlots


# ---------------------------------------------------------------------------
# Browser instance management  (Phase 3: mode-aware logging)
# ---------------------------------------------------------------------------

_browser_instance = None


def _get_browser_instance():
    """
    Return a persistent browser instance for the current task.

    Phase 3: reads BROWSER_MODE from config transparently — BrowserTools.start()
    handles the actual launch-vs-attach decision. We log which mode was used
    so task output clearly shows whether a real session was involved.
    """
    global _browser_instance
    from src.tools.browser import BrowserTools
    from src.config import BROWSER_MODE

    if _browser_instance is None:
        _browser_instance = BrowserTools()
        result = _browser_instance.start()
        if result.success:
            mode = _browser_instance.mode
            if mode == "attach":
                log.info(
                    "[BROWSER] Attached to running Chrome via CDP "
                    "(real cookies/sessions available)"
                )
                print(
                    "[BROWSER] Attached to your running Chrome "
                    "(using real logged-in sessions)"
                )
            else:
                log.info("[BROWSER] Launched fresh managed Chromium (launch mode)")
        else:
            log.error("[BROWSER] Failed to start: %s", result.error)

    return _browser_instance


def _close_browser_instance():
    """
    Close and clear the browser instance.

    Phase 3: in attach mode BrowserTools.close() only disconnects Playwright;
    it never kills the real Chrome. We log this clearly so users aren't
    surprised when Chrome stays open after a task.
    """
    global _browser_instance
    if _browser_instance is not None:
        mode = _browser_instance.mode
        try:
            _browser_instance.close()
            if mode == "attach":
                log.info(
                    "[BROWSER] Disconnected from Chrome CDP "
                    "(Chrome kept running — your tabs are intact)"
                )
                print(
                    "[BROWSER] Disconnected from Chrome "
                    "(Chrome kept running, your tabs are intact)"
                )
        except Exception:
            pass
        _browser_instance = None


# ---------------------------------------------------------------------------
# Route to correct executor (Phase 2: registry lookup)
# ---------------------------------------------------------------------------

def _execute_step(step: Step, brain: Brain, state: GraphState) -> ToolResult:
    """
    Resolve any {{slot_name}} placeholder in step.value, then dispatch
    to whichever tool is registered for step.tool via the registry.
    """
    resolved_step = step

    if step.value and step.value.startswith("{{") and step.value.endswith("}}"):
        resolved_value = resolve_placeholder(step.value, state["slots"])

        if resolved_value == step.value:
            return ToolResult(
                success=False,
                message=f"No content available for placeholder '{step.value}'",
                error="EmptySlot",
                data={}
            )

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

    registry = get_registry()
    executor = registry.get_executor(resolved_step.tool)

    if executor is None:
        return ToolResult(
            success=False,
            message=f"No executor registered for tool: {resolved_step.tool.value}",
            error="UnknownTool",
            data={}
        )

    ctx = ExecutionContext(slots=state["slots"], brain=brain)

    try:
        return executor(resolved_step, ctx)
    except Exception as e:
        log.error(
            "Executor crashed for tool=%s: %s", resolved_step.tool.value, e
        )
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
    """Call Brain to produce a Plan for the task."""
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
    """Check if there are more steps to execute."""
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
        + (f" = '{step.value[:50]}...'" if step.value and len(step.value) > 50
           else f" = '{step.value}'" if step.value else "")
    )

    brain       = Brain()
    tool_result = _execute_step(step, brain, state)

    print(
        f"[EXEC] Result: {'✓' if tool_result.success else '✗'} "
        f"{tool_result.message}"
    )
    if tool_result.error:
        print(f"[EXEC] Error: {tool_result.error}")

    slots = state["slots"]

    if tool_result.success and tool_result.data:
        data = tool_result.data

        if step.tool == ToolType.BROWSER:
            text  = data.get("text")
            title = data.get("page_title")
            url   = data.get("url") or data.get("current_url")
            selector = data.get("selector_used") or data.get("selector")

            if text:
                slots.set_text("browser_text", text)
                print(f"[EXEC] Extracted {len(text.split())} words (cleaned)")
            if url:
                slots.browser.url = url
                
                from urllib.parse import urlparse
                domain = urlparse(url).netloc
                if domain.startswith("www."):
                    domain = domain[4:]
                slots.browser.current_domain = domain

                if not slots.browser.history or slots.browser.history[-1] != url:
                    slots.browser.history.append(url)

                if "youtube.com" in domain:
                    if "/watch" in url:
                        slots.browser.page_type = "watch_page"
                    elif "/results" in url:
                        slots.browser.page_type = "search_results"
                    else:
                        slots.browser.page_type = "home_page"
                elif "google.com" in domain and "/search" in url:
                    slots.browser.page_type = "search_results"
                elif "duckduckgo.com" in domain:
                    slots.browser.page_type = "search_results"
                else:
                    slots.browser.page_type = "content_page"

                if not text:
                    slots.last_text = url
                print(f"[EXEC] URL: {url}")
            if title:
                slots.browser.title = title
            if selector:
                slots.browser.selected_element = selector

        elif step.tool == ToolType.FILES:
            files   = data.get("files")
            matches = data.get("matches")
            moved   = data.get("moved")

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
        "_last_tool_result": tool_result,
        "slots":             slots,
    }


def verify_node(state: GraphState) -> dict:
    """Verify the current step succeeded."""
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

    if retry_count > MAX_RETRIES_PER_STEP:
        brain = Brain()
        brain.record_step_failure(
            app_name=(plan.apps_involved[0] if plan.apps_involved else "unknown"),
            step=step,
            reason=state.get("last_error", "unknown"),
        )

        is_last_step    = index >= len(plan.steps) - 1
        steps_completed = len(state.get("step_results", []))
        majority_done   = steps_completed >= (len(plan.steps) - 1)

        if is_last_step or majority_done:
            print(
                f"[RETRY] Skipping optional step "
                f"{step.step_number} — core task already complete"
            )
            return {
                "retry_count":        retry_count,
                "current_step_index": index + 1,
                "last_error":         None,
            }

        print(
            f"[RETRY] Step {step.step_number} failed "
            f"{MAX_RETRIES_PER_STEP} times — asking user"
        )
        return {
            "retry_count": retry_count,
            "is_failed":   True,
            "ask_user_message": (
                f"Step {step.step_number} failed after "
                f"{MAX_RETRIES_PER_STEP} attempts.\n"
                f"Task: {step.description}\n"
                f"Last error: {state.get('last_error', 'unknown')}"
            ),
        }

    if retry_count == 1 and step.fallback_tool:
        print(f"[RETRY] Trying fallback tool: {step.fallback_tool.value}")
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
        return {"plan": new_plan, "retry_count": retry_count}

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
        return {"plan": new_plan, "retry_count": retry_count}

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
        "retry_count": retry_count,
        "is_failed":   True,
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

    distinct_step_numbers = {r.step_number for r in results}
    last_status_per_step: dict[int, VerificationStatus] = {}
    for r in results:
        last_status_per_step[r.step_number] = r.status
    skipped_step_numbers = {
        num for num, status in last_status_per_step.items()
        if status == VerificationStatus.FAILED
    }

    print(f"\n{'='*50}")
    print(f"  TASK COMPLETE")
    print(f"{'='*50}")
    print(f"  Steps attempted:    {len(distinct_step_numbers)}/{plan.total_steps}")
    if skipped_step_numbers:
        print(f"  Steps skipped:      {sorted(skipped_step_numbers)} (failed after retries, non-blocking)")
    print(f"  Verification calls: {len(results)} (includes retries)")
    print(f"  Total time:         {elapsed}ms")

    slots = state.get("slots")
    if slots:
        print(f"\n{'='*50}")
        print("  RESULT")
        print(f"{'='*50}")
        print(describe_slots(slots))

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

    policy = getattr(plan, "completion_policy", "auto_close") if plan else "auto_close"
    if policy == "auto_close":
        _close_browser_instance()
    elif policy == "user_decides":
        print("\n[ACTION NEEDED]\nEverything is ready. Would you like me to keep the browser open?")
    else:
        print(f"\n[LIFECYCLE] Completion policy is '{policy}'. Leaving browser open.")
        
    return {"is_done": True}


def failed_node(state: GraphState) -> dict:
    """Task failed — print error and clean up."""
    print(f"\n{'='*50}")
    print(f"  TASK FAILED")
    print(f"{'='*50}")

    if state.get("ask_user_message"):
        print(f"\n[ACTION NEEDED]\n{state['ask_user_message']}")
    elif state.get("last_error"):
        print(f"\n[ERROR] {state['last_error']}")

    plan = state.get("plan")
    policy = getattr(plan, "completion_policy", "auto_close") if plan else "auto_close"
    if policy == "auto_close":
        _close_browser_instance()
    elif policy == "user_decides":
        print("\n[ACTION NEEDED]\nTask failed. Would you like me to keep the browser open?")
    else:
        print(f"\n[LIFECYCLE] Completion policy is '{policy}'. Leaving browser open despite failure.")
        
    return {"is_done": True, "is_failed": True}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def should_execute(state: GraphState) -> str:
    if state.get("is_failed"):
        return "failed"
    if state.get("is_done"):
        return "complete"
    return "execute"


def should_verify_or_retry(state: GraphState) -> str:
    return "verify"


def after_verify(state: GraphState) -> str:
    if state.get("is_done"):
        return "complete"
    if state.get("is_failed"):
        return "failed"

    last_result = state.get("_last_step_result")
    if last_result and last_result.status == VerificationStatus.FAILED:
        return "retry"

    plan  = state.get("plan")
    index = state.get("current_step_index", 0)
    if plan and index >= len(plan.steps):
        return "complete"

    return "route"


def after_retry(state: GraphState) -> str:
    if state.get("is_failed"):
        return "failed"
    return "route"


def after_plan(state: GraphState) -> str:
    if state.get("is_failed"):
        return "failed"
    return "route"


# ---------------------------------------------------------------------------
# Build the graph
# ---------------------------------------------------------------------------

def build_graph():
    """Construct and compile the LangGraph workflow."""
    workflow = StateGraph(GraphState)

    workflow.add_node("plan",     plan_node)
    workflow.add_node("route",    route_node)
    workflow.add_node("execute",  execute_node)
    workflow.add_node("verify",   verify_node)
    workflow.add_node("retry",    retry_node)
    workflow.add_node("complete", complete_node)
    workflow.add_node("failed",   failed_node)

    workflow.add_edge(START, "plan")

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
        {"route": "route", "execute": "execute", "failed": "failed"}
    )

    workflow.add_edge("complete", END)
    workflow.add_edge("failed",   END)

    return workflow.compile()


# Compiled graph — imported by main.py
app = build_graph()