import os
import re
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
            extract_text, wait_for_page, get_first_result,
            search_and_extract, search_extract_and_summarize

   HIGH-LEVEL ACTIONS (prefer these):
   search_and_extract(target=query):
     Searches, finds best result, navigates to it, extracts clean text.
     Use for: "find information about X and save to file"

   search_extract_and_summarize(target=query, value=topic_focus):
     Same as above but also summarizes with Gemini.
     Use for: "research X and give me a summary" or
              "find X and save a readable summary to file"

   LOW-LEVEL ACTIONS (only when you need specific control):
   search_web → navigate → extract_text
   Use for: when you need to visit a specific URL you already know

EXAMPLES OF CORRECT PLANS:
  "search for X and save to file Y.txt on Desktop":
    step 1: browser / search_and_extract / target="X"
    step 2: files / write_file
            target="<Desktop path>\\Y.txt"
            value="{{extracted_content}}"

  "research X and save a summary to file Y.txt":
    step 1: browser / search_extract_and_summarize
            target="X"
            value="key features and highlights"
    step 2: files / write_file
            target="<Desktop path>\\Y.txt"
            value="{{extracted_content}}"

3. files — File and folder operations (pure Python)
   Actions: move_file, copy_file, rename_file, delete_file,
            create_folder, list_files, find_files, organize_files,
            write_file

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
  - Writing content to a file → files / write_file
    target = full file path, value = {{extracted_content}} if content
    came from a previous browser extract_text step
  - Browser/web tasks → always use browser tool
  - Opening native Windows apps → use windows_ui
  - Spotify control → use apps tool
  - Notion tasks → use apps tool
  - Electron apps (WhatsApp, Spotify UI) → use ocr as primary,
    vision as fallback
  - Unknown UI elements → try windows_ui first, then ocr, then vision

PASSING CONTENT BETWEEN STEPS:
  If a step extracts text (browser/extract_text) and a later step
  needs to write that text to a file (files/write_file),
  set the write_file value to exactly: {{extracted_content}}
  The agent will automatically substitute the extracted text.

EXAMPLES OF CORRECT PLANS:
  "play music on Spotify":
    step 1: windows_ui / open_app / target="Spotify"
    step 2: apps / spotify_play / target="Spotify"

  "search for X online":
    step 1: browser / search_web / target="X"
    step 2: browser / extract_text / target="body"

  "create folder X on Desktop":
    step 1: files / create_folder / target="<full Desktop path>"
            value="X"

  "search for X and save to file Y.txt on Desktop":
    step 1: browser / search_web / target="X"
    step 2: browser / extract_text / target="body"
    step 3: files / write_file
            target="<full Desktop path>\\Y.txt"
            value="{{extracted_content}}"

  "move PDFs from Downloads to Documents":
    step 1: files / find_files / target="<Downloads path>"
            value="*.pdf"
    step 2: files / move_file / target="<Downloads path>"
            value="<Documents path>"

  "search for X and save to file Y.txt on Desktop":
    step 1: browser / search_web / target="X"
    step 2: browser / navigate / target="URL of the most relevant result"
            (use the actual article/docs URL, not the search results page)
    step 3: browser / extract_text / target="article" or "main" or "body"
    step 4: files / write_file
            target="<full Desktop path>\\Y.txt"
            value="{{extracted_content}}"
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
- Web research: use browser / search_web then browser / extract_text
  Do NOT add wait_for_page steps — search_web already waits for load
- Always set expected_outcome — this is how the verifier checks success
- Set fallback_tool when a step might fail on the primary tool
- Set requires_verification=false only for wait and volume actions
- Keep total_steps under 20
- ONLY plan what was explicitly asked — do not add extra steps
  like saving files, closing apps, or confirming dialogs unless asked
- If the task says "write X in Notepad" — just open and type, nothing else
- If the task says "search for X" — just search and extract, nothing else
- If the task is impossible or unclear, set total_steps=0
  and explain in notes

CRITICAL: Only use these exact action values:
open_app, close_app, focus_app, click, type_text, press_key, scroll, select,
navigate, search_web, click_element, fill_form, extract_text, wait_for_page,
get_first_result, move_file, copy_file, rename_file, delete_file, create_folder, list_files,
find_files, organize_files, write_file, spotify_play, spotify_pause, spotify_next,
spotify_playlist, notion_create_page, notion_append, clipboard_copy,
clipboard_paste, volume_set, wait, screenshot

- Never invent action names. Never use: respond, display, show, open_url, type.
   target must always be a non-empty string. Never set target to null.

- NEVER add organize_files as a final step after write_file
- NEVER add steps to "ensure" or "confirm" or "verify" file operations
  — the verifier handles that automatically
- After writing a file with write_file, the task is complete
- Do not add cleanup or organization steps unless explicitly asked

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
      "tool": "windows_ui|browser|files|apps|ocr|vision",
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

        preprocessed = self._preprocess_task(task)
        if preprocessed:
            log.info("Using preprocessed plan for task")
            return preprocessed

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
    

    def _preprocess_task(self, task: str) -> Optional[Plan]:
        """
        Detect common task patterns and return a Plan directly
        without calling Gemini. This avoids low-level planning
        for well-understood task types.

        Patterns detected:
        - research + save: "search for X and save to file Y"
        - research + summarize: "search for X and summarize"
        - research only: "search for X and tell me"
        """
        import os
        import re

        task_lower = task.lower()

        # Detect: research + save to file
        save_to_file = (
            "save" in task_lower
            and ("file" in task_lower or ".txt" in task_lower or ".md" in task_lower)
            and any(w in task_lower for w in ["search", "find", "look up", "research"])
        )

        # Detect: research + summarize (no save)
        summarize_only = (
            any(w in task_lower for w in ["search", "find", "look up", "research"])
            and any(w in task_lower for w in ["summarize", "summary", "tell me", "what is"])
            and not save_to_file
        )

        if not save_to_file and not summarize_only:
            return None

        # Extract file name if saving
        file_name = None
        if save_to_file:
            # Look for .txt or .md filename
            match = re.search(r'[\w_-]+\.(txt|md|csv)', task_lower)
            if match:
                file_name = match.group(0)
            else:
                file_name = "research_output.txt"

        # Extract search query — everything between "search for" and action words
        query = task
        for prefix in ["search for", "look up", "find", "research"]:
            if prefix in task_lower:
                idx   = task_lower.index(prefix) + len(prefix)
                query = task[idx:].strip()
                # Trim at action words
                for stop in [", extract", ", summarize", ", save", " and save",
                            " and tell", " and summarize", " and extract"]:
                    if stop in query.lower():
                        query = query[:query.lower().index(stop)].strip()
                break

        home    = os.path.expanduser("~")
        desktop = (
            os.path.join(home, "OneDrive", "Desktop")
            if os.path.exists(os.path.join(home, "OneDrive", "Desktop"))
            else os.path.join(home, "Desktop")
        )

        if save_to_file and file_name:
            file_path = os.path.join(desktop, file_name)

            steps = [
                Step(
                    step_number           = 1,
                    tool                  = ToolType.BROWSER,
                    action                = ActionType.SEARCH_EXTRACT_AND_SUMMARIZE,
                    target                = query,
                    value                 = "key features and highlights",
                    description           = f"Research: {query}",
                    expected_outcome      = "Clean summary text extracted",
                    fallback_tool         = None,
                    requires_verification = False,
                ),
                Step(
                    step_number           = 2,
                    tool                  = ToolType.FILES,
                    action                = ActionType.WRITE_FILE,
                    target                = file_path,
                    value                 = "{{extracted_content}}",
                    description           = f"Save research to {file_name}",
                    expected_outcome      = f"File {file_name} exists on Desktop",
                    fallback_tool         = None,
                    requires_verification = True,
                ),
            ]

            return Plan(
                task_summary         = f"Research '{query}' and save to {file_name}",
                total_steps          = 2,
                apps_involved        = ["browser"],
                estimated_complexity = "simple",
                steps                = steps,
                notes                = None,
            )

        if summarize_only:
            steps = [
                Step(
                    step_number           = 1,
                    tool                  = ToolType.BROWSER,
                    action                = ActionType.SEARCH_EXTRACT_AND_SUMMARIZE,
                    target                = query,
                    value                 = None,
                    description           = f"Research and summarize: {query}",
                    expected_outcome      = "Summary text ready to display",
                    fallback_tool         = None,
                    requires_verification = False,
                ),
            ]

            return Plan(
                task_summary         = f"Research and summarize: {query}",
                total_steps          = 1,
                apps_involved        = ["browser"],
                estimated_complexity = "simple",
                steps                = steps,
                notes                = None,
            )

        return None

    def replan_step(
        self,
        original_step: Step,
        failure_reason: str,
        attempt: int,
    ) -> Optional[Step]:
        if attempt > MAX_REPLAN_ATTEMPTS:
            log.warning(
                "Max replan attempts reached for step %d",
                original_step.step_number
            )
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

    Return ONLY a JSON object for the replacement step.
    If no alternative exists, return {{"impossible": true}}

    {{
    "step_number": {original_step.step_number},
    "tool": "tool_name",
    "action": "action_name",
    "target": "what to act on",
    "value": null,
    "description": "what this does",
    "expected_outcome": "what success looks like",
    "fallback_tool": null,
    "requires_verification": true
    }}
    """.strip()

        try:
            response = self._client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    max_output_tokens=500,
                )
            )

            raw = (response.text or "").strip()

            # Handle empty response
            if not raw:
                log.info("Replan returned empty response — step impossible")
                return None

            raw = _strip_markdown(raw)

            # Handle empty after stripping
            if not raw:
                log.info("Replan returned empty after strip — step impossible")
                return None

            data = json.loads(raw)

            if data.get("impossible"):
                log.info("Replan determined step is impossible")
                return None

            # Validate required fields
            if not data.get("target"):
                log.warning("Replan returned no target")
                return None

            if data.get("value") is not None:
                data["value"] = str(data["value"])

            data["tool"]   = ToolType(data["tool"])
            data["action"] = ActionType(data["action"])

            if data.get("fallback_tool"):
                data["fallback_tool"] = ToolType(data["fallback_tool"])
            else:
                data["fallback_tool"] = None

            return Step(**data)

        except json.JSONDecodeError as e:
            log.error("Invalid replan response: %s", e)
            return None
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
            from src.models import TaskPattern
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
            from src.models import FailureRecord
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

    def _build_planning_prompt(self, task: str, hints: str) -> str:
        import os
        home = os.path.expanduser("~")

        # Detect actual desktop path
        onedrive_desktop = os.path.join(home, "OneDrive", "Desktop")
        regular_desktop  = os.path.join(home, "Desktop")
        desktop_path     = (
            onedrive_desktop
            if os.path.exists(onedrive_desktop)
            else regular_desktop
        )

        user_context = f"""
    USER ENVIRONMENT:
    Home directory: {home}
    Desktop path:   {desktop_path}
    Downloads:      {os.path.join(home, "Downloads")}
    Documents:      {os.path.join(home, "OneDrive", "Documents") if os.path.exists(os.path.join(home, "OneDrive", "Documents")) else os.path.join(home, "Documents")}
    Username:       {os.path.basename(home)}
    OS:             Windows 11
    """.strip()

        parts = [
            f"TASK: {task}",
            user_context,
        ]

        if hints:
            parts.append(hints)

        parts.append(TOOL_CATALOGUE)
        parts.append(PLAN_SCHEMA)

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
        try:
            steps = []
            for i, step_data in enumerate(data.get("steps", [])):
                try:
                    # Skip steps with None or missing target
                    if not step_data.get("target"):
                        log.warning(
                            "Skipping step %d: missing target", i + 1
                        )
                        continue

                    # Coerce string enums
                    step_data["tool"]   = ToolType(step_data["tool"])
                    step_data["action"] = ActionType(step_data["action"])

                    if step_data.get("fallback_tool"):
                        step_data["fallback_tool"] = ToolType(
                            step_data["fallback_tool"]
                        )
                    else:
                        step_data["fallback_tool"] = None

                    # Ensure value is string or None, never other types
                    if step_data.get("value") is not None:
                        step_data["value"] = str(step_data["value"])

                    steps.append(Step(**step_data))

                except Exception as e:
                    log.warning("Skipping invalid step %d: %s", i + 1, e)
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

            # Renumber steps after filtering
            for idx, step in enumerate(plan.steps):
                object.__setattr__(step, 'step_number', idx + 1) \
                    if hasattr(step, '__setattr__') \
                    else None

            return plan

        except Exception as e:
            log.error("_build_plan failed: %s", e)
            return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """
    Extract the first JSON object from a Gemini response.
    Works whether the model returns:
      - raw JSON
      - ```json fenced JSON
      - explanations before/after JSON
    """
    text = text.strip()

    # remove markdown fences
    text = re.sub(r"```json", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```", "", text)

    # extract first complete JSON object
    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        return text[start:end + 1].strip()

    return text.strip()