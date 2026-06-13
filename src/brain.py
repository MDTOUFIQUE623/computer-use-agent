import os
import json
import logging
import time
from typing import Optional

from google import genai
from google.genai import types
from dotenv import load_dotenv

from src.models import (
    Plan,
    Step,
    ToolType,
    ActionType,
    StepResult,
    VerificationStatus,
)
from src.config import PLANNER_MODEL, MAX_VISION_TOKENS
import src.memory as mem

load_dotenv()
log = logging.getLogger(__name__)

# How many times Brain can replan a single failed step
MAX_REPLAN_ATTEMPTS = 2

# Tool + Action catalogue sent to Gemini in the planning prompt
# ---------------------------------------------------------------------------

TOOL_CATALOGUE = """
AVAILABLE TOOLS AND ACTIONS:

1. windows_ui — Native Windows app control via UI Automation
   Actions: open_app, close_app, focus_app, click, type_text,
            press_key, scroll, select

2. browser — Browser automation via Playwright
   Actions: navigate, search_web, click_element, fill_form,
            extract_text, wait_for_page

3. files — File and folder operations (pure Python)
   Actions: move_file, copy_file, rename_file, delete_file,
            create_folder, list_files, find_files, organize_files

4. apps — App-specific integrations
   Actions: spotify_play, spotify_pause, spotify_next,
            spotify_playlist, notion_create_page, notion_append,
            clipboard_copy, clipboard_paste, volume_set, wait

5. ocr — Screen text reading via Tesseract (fallback)
   Actions: click, type_text
   Use when: windows_ui cannot find an element in an Electron app

6. vision — Gemini vision analysis (last resort only)
   Actions: click, scroll, wait
   Use when: ALL other tools have failed for a step
   Cost: HIGH — avoid unless necessary

TOOL PRIORITY ORDER (use the first tool that can handle the step):
  files → browser → windows_ui → apps → ocr → vision

ROUTING RULES:
  - File/folder tasks → always use files tool
  - Browser/web tasks → always use browser tool
  - Opening native Windows apps → use windows_ui
  - Spotify control → use apps tool
  - Notion tasks → use apps tool
  - Electron apps (WhatsApp, Spotify UI) → use ocr as primary,
    vision as fallback
  - Unknown UI elements → try windows_ui first, then ocr, then vision
""".strip()


# ---------------------------------------------------------------------------
# System prompt for the planner
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """
You are a Windows computer automation planner.
Your job is to break down a user task into a precise,
step-by-step execution plan.

You have access to specific tools — use ONLY the tools and
actions listed in the tool catalogue. Do not invent tools
or actions that aren't listed.

Rules:
- Each step must be atomic — one action, one target
- Use the simplest tool that can accomplish each step
- File operations never need UI tools — use the files tool directly
- Web research: use browser tool + extract_text to get page content
  as text. Never use vision for web pages.
- Always set expected_outcome — this is how the verifier checks success
- Set fallback_tool when a step might fail on the primary tool
- Set requires_verification=false only for wait and volume actions
- Keep total_steps under 20
- If the task is impossible or unclear, set total_steps=0
  and explain in notes

Respond ONLY with valid JSON matching the Plan schema.
No markdown, no explanation outside the JSON.
""".strip()


# ---------------------------------------------------------------------------
# Plan schema description sent to Gemini
# ---------------------------------------------------------------------------

PLAN_SCHEMA = """
Return a JSON object with this exact structure:
{
  "task_summary": "brief restatement of what was asked",
  "total_steps": <integer>,
  "apps_involved": ["list", "of", "app", "names"],
  "estimated_complexity": "simple|medium|complex",
  "steps": [
    {
      "step_number": 1,
      "tool": "windows_ui|browser|files|apps|ocr|vision|system",
      "action": "open_app|navigate|move_file|...",
      "target": "what to act on — app name, url, file path, element name",
      "value": "text to type, volume level, etc (or null)",
      "description": "human readable description of this step",
      "expected_outcome": "what should be true after this step succeeds",
      "fallback_tool": "tool to try if primary fails (or null)",
      "requires_verification": true
    }
  ],
  "notes": "anything important to flag (or null)"
}
""".strip()


# ---------------------------------------------------------------------------
# Main Brain class
# ---------------------------------------------------------------------------

class Brain:
    """
    The planning and routing brain of the agent.

    Usage:
        brain = Brain()
        plan  = brain.plan_task("open Spotify and play my liked songs")
        tool  = brain.route_step(plan.steps[0])
        # tool is a ToolType enum value — graph.py uses it to call the right executor
    """

    def __init__(self):
        self._client = genai.Client()
        mem.init_db()
        log.info("Brain initialized with model: %s", PLANNER_MODEL)

    # -----------------------------------------------------------------------
    # Planning
    # -----------------------------------------------------------------------

    def plan_task(self, task: str) -> Optional[Plan]:
        """
        Call Gemini once to produce a full Plan for the task.

        Steps:
          1. Load memory hints for this task
          2. Build the planning prompt
          3. Call Gemini with text-only prompt
          4. Parse and validate the response as a Plan
          5. Return the Plan or None if planning failed

        This is the ONLY place Gemini is called during planning.
        All subsequent routing is deterministic.
        """
        start = time.monotonic()
        log.info("Planning task: %s", task)

        # Step 1 — load memory hints
        hints = mem.get_memory_hints(task)
        if hints:
            log.info("Memory hints loaded for task")

        # Step 2 — build prompt
        prompt = self._build_planning_prompt(task, hints)

        # Step 3 — call Gemini
        try:
            response = self._client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=2000,
                    system_instruction=PLANNER_SYSTEM_PROMPT,
                )
            )

            raw_text = response.text or ""
            log.debug("Raw planner response: %s", raw_text[:500])

        except Exception as e:
            log.error("Gemini planning call failed: %s", e)
            return None

        # Step 4 — parse the response
        plan = self._parse_plan_response(raw_text, task)

        if plan:
            elapsed = int((time.monotonic() - start) * 1000)
            log.info(
                "Plan created: %d steps, complexity=%s (%dms)",
                plan.total_steps,
                plan.estimated_complexity,
                elapsed,
            )

        return plan

    def replan_step(
        self,
        original_step: Step,
        failure_reason: str,
        attempt: int,
    ) -> Optional[Step]:
        """
        Ask Gemini to suggest an alternative approach for a failed step.

        Called by graph.py when a step fails and retries are exhausted.
        Returns a new Step with a different tool/action, or None if
        no alternative is possible.

        attempt: which replan attempt this is (1 or 2)
        """
        if attempt > MAX_REPLAN_ATTEMPTS:
            log.warning("Max replan attempts reached for step %d", original_step.step_number)
            return None

        log.info(
            "Replanning step %d (attempt %d): %s",
            original_step.step_number,
            attempt,
            failure_reason,
        )

        prompt = f"""
A step in an automation task has failed. Suggest ONE alternative step.

Original step:
  Tool:    {original_step.tool.value}
  Action:  {original_step.action.value}
  Target:  {original_step.target}
  Value:   {original_step.value}

Failure reason: {failure_reason}

{TOOL_CATALOGUE}

Return ONLY a JSON object for the replacement step:
{{
  "step_number": {original_step.step_number},
  "tool": "different_tool_than_{original_step.tool.value}",
  "action": "action_name",
  "target": "what to act on",
  "value": null,
  "description": "what this alternative step does",
  "expected_outcome": "what should be true if this succeeds",
  "fallback_tool": null,
  "requires_verification": true
}}

Rules:
- Do NOT suggest the same tool that just failed
- Use the simplest alternative approach
- If no alternative exists, return {{"impossible": true}}
""".strip()

        try:
            response = self._client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=500,
                )
            )

            raw = response.text or ""
            raw = _strip_markdown(raw)

            data = json.loads(raw)

            if data.get("impossible"):
                log.info("Replan determined step is impossible")
                return None

            return Step(**data)

        except Exception as e:
            log.error("replan_step failed: %s", e)
            return None

    # -----------------------------------------------------------------------
    # Routing
    # -----------------------------------------------------------------------

    def route_step(self, step: Step) -> ToolType:
        """
        Determine which tool to use for a step.

        This is deterministic — reads the step.tool field directly.
        The routing decision was already made during planning.

        Returns the ToolType that graph.py should invoke.
        """
        return step.tool

    def route_fallback(self, step: Step) -> Optional[ToolType]:
        """
        Return the fallback tool for a step, if one was specified.
        Returns None if no fallback was planned.
        """
        return step.fallback_tool

    # -----------------------------------------------------------------------
    # Memory integration
    # -----------------------------------------------------------------------

    def record_success(
        self,
        task:        str,
        plan:        Plan,
        duration_ms: int,
    ) -> None:
        """
        Save a successful task pattern to memory.
        Called by graph.py when all steps complete successfully.
        """
        try:
            from models import TaskPattern
            pattern = TaskPattern(
                task_description = task,
                tool_sequence    = [s.tool   for s in plan.steps],
                action_sequence  = [s.action for s in plan.steps],
                apps_involved    = plan.apps_involved,
                success_rate     = 1.0,
                last_used        = mem.now_iso(),
                avg_duration_ms  = duration_ms,
            )
            mem.save_task_pattern(pattern)
            log.info("Task pattern saved to memory")
        except Exception as e:
            log.warning("Failed to save task pattern: %s", e)

    def record_step_failure(
        self,
        app_name:  str,
        step:      Step,
        reason:    str,
    ) -> None:
        """
        Log a step failure to memory so Brain avoids this path next time.
        Called by graph.py when a step fails all retries.
        """
        try:
            from models import FailureRecord
            record = FailureRecord(
                app_name         = app_name,
                tool_attempted   = step.tool,
                action_attempted = step.action,
                failure_reason   = reason,
                timestamp        = mem.now_iso(),
            )
            mem.log_failure(record)
            log.info(
                "Failure recorded: %s / %s in %s",
                step.tool.value, step.action.value, app_name
            )
        except Exception as e:
            log.warning("Failed to log failure: %s", e)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _build_planning_prompt(
        self,
        task:  str,
        hints: str,
    ) -> str:
        """Build the full planning prompt sent to Gemini."""
        parts = []

        # Task
        parts.append(f"TASK: {task}")

        # Memory hints (if any)
        if hints:
            parts.append(f"\n{hints}")

        # Tool catalogue
        parts.append(f"\n{TOOL_CATALOGUE}")

        # Schema
        parts.append(f"\n{PLAN_SCHEMA}")

        return "\n\n".join(parts)

    def _parse_plan_response(
        self,
        raw_text: str,
        task:     str,
    ) -> Optional[Plan]:
        """
        Parse Gemini's response into a validated Plan object.

        Attempts:
          1. Direct JSON parse
          2. Strip markdown fences and retry
          3. Return None if both fail
        """
        raw = raw_text.strip()

        # Attempt 1 — direct parse
        try:
            data = json.loads(raw)
            return self._build_plan(data, task)
        except json.JSONDecodeError:
            pass

        # Attempt 2 — strip markdown fences
        try:
            cleaned = _strip_markdown(raw)
            data    = json.loads(cleaned)
            return self._build_plan(data, task)
        except (json.JSONDecodeError, Exception) as e:
            log.error("Plan parse failed after markdown strip: %s", e)
            log.error("Raw response was: %s", raw[:500])
            return None

    def _build_plan(self, data: dict, task: str) -> Optional[Plan]:
        """
        Build and validate a Plan from a parsed dict.
        Handles missing fields gracefully.
        """
        try:
            # Validate and coerce steps
            steps = []
            for i, step_data in enumerate(data.get("steps", [])):
                try:
                    # Coerce string enums
                    step_data["tool"]   = ToolType(step_data["tool"])
                    step_data["action"] = ActionType(step_data["action"])
                    if step_data.get("fallback_tool"):
                        step_data["fallback_tool"] = ToolType(
                            step_data["fallback_tool"]
                        )

                    steps.append(Step(**step_data))

                except Exception as e:
                    log.warning(
                        "Skipping invalid step %d: %s", i + 1, e
                    )
                    continue

            if not steps:
                log.error("Plan has no valid steps")
                return None

            plan = Plan(
                task_summary         = data.get("task_summary", task),
                total_steps          = len(steps),
                apps_involved        = data.get("apps_involved", []),
                estimated_complexity = data.get(
                    "estimated_complexity", "medium"
                ),
                steps = steps,
                notes = data.get("notes"),
            )

            return plan

        except Exception as e:
            log.error("_build_plan failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """
    Remove markdown code fences from a string.
    Handles ```json ... ``` and ``` ... ``` formats.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first line (```json or ```) and last line (```)
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1])
    return text.strip()