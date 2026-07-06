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
       Actions: navigate, search_web, search_on_page, click_element,
                fill_form, extract_text, wait_for_page, get_first_result,
                search_and_extract, search_extract_and_summarize,
                media_play, media_pause, media_resume, media_skip_ads,
                media_wait
 
       HIGH-LEVEL ACTIONS (prefer these):
       search_and_extract(target=query):
         Searches, finds best result, navigates to it, extracts clean text.
         Use for: "find information about X and save to file"
 
       search_extract_and_summarize(target=query, value=topic_focus):
         Same as above but also summarizes with Gemini.
         Use for: "research X and give me a summary" or
                  "find X and save a readable summary to file"
 
       SITE-NATIVE SEARCH — CRITICAL ROUTING RULE:
       search_on_page(target=query):
         Uses the search box ALREADY ON the current page instead of
         leaving for an external search engine. Fills the box and
         presses Enter.
 
         USE THIS instead of search_web WHENEVER the task says to go to
         a SPECIFIC NAMED SITE and search WITHIN it — e.g. "go to
         youtube.com and search for X", "search GitHub for X", "find X
         on Amazon". The pattern is:
           step 1: browser / navigate / target="<site URL>"
           step 2: browser / search_on_page / target="<query>"
         NEVER follow a navigate-to-a-specific-site step with search_web
         — search_web always exits to an external search engine
         (DuckDuckGo), which abandons the site you just navigated to and
         searches the open web instead. This is wrong whenever the task
         names a specific site to search within.
         CLICKING A SEARCH/RESULT ITEM — CRITICAL ROUTING RULE:
         click_best_result(target=query):
         Use this instead of click_element whenever you need to click
         "the most relevant result" from a list of search/video/product
         results, and you do NOT know the exact visible text of that
         result ahead of time (you usually don't — result titles are
         dynamic content you can't predict from the task description).
         It reads the actual visible candidates on the page, scores them
         against your query, and clicks the best match.
 
         Only use click_element instead when the text to click is
         actually known/fixed — a named button ("Submit", "Sign in"), a
         menu item, a specific link whose label was given in the task.
 
         NEVER use click_element with a generic guess like "video",
         "first result", "play button" as the target text — that text
         almost certainly does not exist verbatim anywhere on the page,
         and click_element will fail with a timeout. Use
         click_best_result for this instead.
 
       MEDIA CONTROLS — CRITICAL ROUTING RULE:
       media_play(target="current_media") / media_pause(target="current_media") /
       media_resume(target="current_media") / media_skip_ads(target="current_media") /
       media_wait(target="current_media"):
         Controls the <video> element already loaded on the CURRENT
         browser page (e.g. a YouTube video the agent or user just
         opened). These act on whatever's on screen — there is no
         specific target, so always pass target="current_media" (or
         omit it; a placeholder is filled in automatically).

         USE THESE for "pause/stop/resume/skip the ad on the video/song
         that's playing IN THE BROWSER" — anything referring to media on
         a webpage that's already open, most commonly a prior YouTube
         step in the same conversation.

         Do NOT use the apps tool's spotify_* actions for this — those
         control the separate Spotify desktop application, not a video
         playing in the browser. If the task doesn't mention Spotify by
         name and a browser/YouTube step happened earlier, it's
         browser/media_*, not apps/spotify_*.

         Note: any plan containing one of these actions automatically
         gets completion_policy forced to keep_open regardless of what
         you set — pausing/resuming media means the tab is still in use,
         not finished with. You don't need to set completion_policy
         specially for these; it's handled for you.

       LOW-LEVEL ACTIONS (only when you need specific control):
       search_web → navigate → extract_text
       Use for: general open-web research where no specific site was
                named — "search for X and tell me about it",
                "find information on X"

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

  "go to youtube.com and search for X and play the video":
    step 1: browser / navigate / target="https://www.youtube.com"
    step 2: browser / search_on_page / target="X"
    step 3: browser / click_best_result / target="X"
        (Do NOT use search_web for step 2 — it would leave YouTube
        entirely and search DuckDuckGo instead. Do NOT use click_element
        for step 3 with a guessed target like "video" or "first result"
        — that text won't exist on the page. click_best_result reads the
        actual result titles and picks the closest match to the search
        query X.)

  "pause/stop the song or video that's playing" (in the browser):
    step 1: browser / media_pause / target="current_media"
        (NOT apps / spotify_pause — that's a different app entirely.
        Use browser/media_pause whenever the media in question is
        playing on a webpage, e.g. a YouTube video opened earlier in
        this conversation.)

  "play <song name> on spotify":
    step 1: apps / spotify_play_track / target="<song name>"
        (NOT browser/media_play, NOT spotify_play — spotify_play only
        resumes whatever Spotify already has queued; it can't search
        for and start a specific new song. spotify_play_track does the
        search-and-play in one step.)

3. files — File and folder operations (pure Python)
   Actions: move_file, copy_file, rename_file, delete_file,
            create_folder, list_files, find_files, organize_files,
            write_file

4. apps — App-specific integrations
       Actions: spotify_play, spotify_pause, spotify_next,
                spotify_playlist, spotify_play_track, notion_create_page,
                notion_append, clipboard_copy, clipboard_paste,
                volume_set, wait

       SPOTIFY ACTIONS — WHICH ONE TO USE:
       spotify_play / spotify_pause / spotify_next: control whatever is
         ALREADY queued/playing (resume, pause, skip). No target needed.
       spotify_play_track(target="<song name>"): search Spotify and
         start playing a SPECIFIC named song. Use this for "play <song>
         on Spotify" — spotify_play alone cannot do this, it only
         resumes an existing queue.
       spotify_playlist(target="<playlist name>"): search Spotify and
         start playing a SPECIFIC named playlist.
       Both spotify_play_track and spotify_playlist search Spotify's
       real catalog via its API — put the actual song/playlist name in
       target, not a description.

       NOTE: "wait" (pausing for N seconds) is ALWAYS tool=apps,
       action=wait, value="<seconds>" — even in the middle of a browser
       or windows_ui sequence. There is no browser/wait or windows_ui/wait
       action. Example: tool=apps / action=wait / value="5" pauses 5
       seconds regardless of what the surrounding steps are doing.

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
        target = full file path, value = {{browser_text}} (or
        {{extracted_content}} as a generic fallback) if content came
        from a previous browser step
      - Browser/web tasks → always use browser tool
      - A task that names a SPECIFIC SITE to search within (YouTube,
        GitHub, Amazon, a docs site, etc.) → navigate to that site,
        THEN use search_on_page, NOT search_web. Only use search_web
        when no specific site was named (general open-web search).
      - Opening native Windows apps → use windows_ui
      - Spotify control (play/pause/next, or playing a SPECIFIC named
        song/playlist) → use apps tool. "Play <song> on Spotify" →
        spotify_play_track, target=<song name>. "Play <playlist> on
        Spotify" → spotify_playlist, target=<playlist name>.
      - Pausing/resuming/skipping a video or song already playing on a
        webpage (e.g. a YouTube video from an earlier step) → use
        browser tool, media_play/media_pause/media_resume/media_skip_ads/
        media_wait, target="current_media". This is NOT the same as
        Spotify control — only use apps/spotify_* if the task explicitly
        names Spotify or a prior step opened the Spotify app itself.
      - Notion tasks → use apps tool
      - Waiting/pausing for N seconds → ALWAYS tool=apps, action=wait
        (never browser/wait or windows_ui/wait — those don't exist)
      - Electron apps (WhatsApp, Spotify UI) → use ocr as primary,
        vision as fallback
      - Unknown UI elements → try windows_ui first, then ocr, then vision

PASSING CONTENT BETWEEN STEPS:
  Steps can pass data forward using {{slot_name}} placeholders in
  `target` or `value`. Available slots, written automatically by
  the matching tool action:
 
    {{browser_text}}    - text extracted by browser/extract_text,
                           search_and_extract, search_extract_and_summarize
    {{browser_url}}      - URL from browser/navigate, search_web,
                           get_first_result
    {{ocr_text}}         - text read by ocr/read_window_text style actions
    {{clipboard_text}}   - content from apps/clipboard_copy or clipboard_paste
    {{vision_result}}    - description/decision from vision tool
    {{extracted_content}} - ALIAS for whichever *_text slot was written
                            most recently. Prefer this when you are not
                            sure which specific slot a prior step filled
                            (e.g. either OCR or browser could have produced
                            the text). Use a specific slot name only when
                            you need to be precise about the source.
 
  Example — write extracted browser text to a file:
    step 1: browser / extract_text / target="article"
    step 2: files / write_file
            target="<Desktop path>\\notes.txt"
            value="{{browser_text}}"
 
  Example — navigate to a URL found by an earlier step:
    step 1: browser / search_web / target="X"
    step 2: browser / get_first_result
    step 3: browser / navigate / target="{{browser_url}}"

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
- Set completion_policy based on the task (exactly one of three values):
  * auto_close (default): Close browser when done. Use for research, lookups,
    form-filling, or any task with no reason for the user to keep looking at the page.
  * keep_open: Leave the browser open, no prompt. Use whenever the task's own
    outcome IS the browser staying open and visible — e.g. "play a video",
    "listen to music", "watch X", "leave it open", or anything where playback
    continuing after the plan finishes is the whole point of the task.
  * ask_user: Pause and ask the user whether to keep the browser open or close
    it. Use ONLY for genuinely ambiguous cases where you can't tell from the
    task whether the user wants to keep watching/using the page afterward.

  Media playback (YouTube, music, video) is NOT ambiguous — always use
  keep_open for it, never ask_user. Reserve ask_user for cases you truly
  can't infer intent for.

CRITICAL: Only use these exact action values:
    open_app, close_app, focus_app, click, type_text, press_key, scroll, select,
    navigate, search_web, search_on_page, click_element, click_best_result, fill_form, extract_text, wait_for_page,
    get_first_result, move_file, copy_file, rename_file, delete_file, create_folder, list_files,
    find_files, organize_files, write_file, spotify_play, spotify_pause, spotify_next,
    spotify_playlist, spotify_play_track, notion_create_page, notion_append, clipboard_copy,
    clipboard_paste, volume_set, wait, screenshot, media_play, media_pause, media_resume, media_skip_ads, media_wait

- Never invent action names. Never use: respond, display, show, open_url, type.
   target must always be a non-empty string. Never set target to null.
   Exception: media_play, media_pause, media_resume, media_skip_ads,
   media_wait, spotify_next, screenshot, and wait act on whatever is
   already on screen/playing — for these, target is not meaningful;
   any short placeholder (or omitting it) is fine.

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
  "completion_policy": "auto_close|keep_open|ask_user (CRITICAL: use keep_open for youtube/media playback tasks, never ask_user)",
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
                    # 800, not 500: this produces one full Step object with
                    # the same free-text fields (description,
                    # expected_outcome) as the main planner, which budgets
                    # 2000 tokens for potentially many such objects. 500 was
                    # tight enough that a moderately verbose response could
                    # get cut mid-string, producing a JSON parse failure
                    # (observed 2026-07-02: "Unterminated string...").
                    max_output_tokens=800,
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

            # Validate required fields — same target-less exemption as
            # _build_plan (see _TARGETLESS_ACTIONS): a replanned media_*
            # step legitimately has no target, and this check used to
            # discard it, making retries for those actions silently fail.
            if not data.get("target"):
                if data.get("action") in self._TARGETLESS_ACTIONS:
                    data["target"] = "current_media"
                else:
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

    # Actions that legitimately operate on "whatever's already there" rather
    # than a specific target — a URL, search query, file path, etc. don't
    # apply to them. Added 2026-07-02: the blanket "target required" check
    # below predates MEDIA_* actions and was silently dropping every
    # media-control step (e.g. "stop the song" -> media_pause), producing
    # an empty plan and a hard failure instead of pausing the video.
    _TARGETLESS_ACTIONS = {
        "media_play", "media_pause", "media_resume",
        "media_skip_ads", "media_wait",
        "spotify_next", "screenshot", "wait",
    }

    # Actions where the whole point is that media is still relevant to the
    # browser session afterward — pausing/resuming/skipping an ad on a
    # video is "hold on a second," not "I'm finished with this page."
    # Forced deterministically rather than left to the LLM's judgment: we
    # already saw the model drift on completion_policy once (the
    # 2026-07-02 "user_decides" incident) purely from prompt wording, and
    # this is a hard rule, not a preference — closing the tab right after
    # a media-control action directly undoes the action's own purpose.
    _MEDIA_CONTROL_ACTIONS = {
        "media_play", "media_pause", "media_resume",
        "media_skip_ads", "media_wait",
    }

    # Maps values the model might emit that aren't in the current
    # CompletionPolicy enum onto the closest valid equivalent. This exists
    # because prompt wording alone can't fully constrain an LLM's enum
    # output — a fast model under load, a stale/regressed prompt edit, a
    # future model-backend swap (Phase 7: Ollama), or a partially-applied
    # patch can all cause a step to arrive with a value that used to be
    # valid or was never valid at all. Previously this hit Plan(**data)
    # directly and raised a raw Pydantic ValidationError that killed the
    # entire plan (see the 2026-07-02 "user_decides" incident). Better to
    # degrade to a sane default than crash the whole task over one field.
    _COMPLETION_POLICY_ALIASES = {
        "auto_close": "auto_close",
        "keep_open":  "keep_open",
        "ask_user":   "ask_user",
        # legacy values from the pre-collapse 5-state enum
        "user_decides":  "ask_user",
        "wait_for_user": "ask_user",
        "background":    "keep_open",
    }

    def _normalize_completion_policy(self, raw_value) -> str:
        """Coerce any completion_policy value to one CompletionPolicy accepts."""
        if not raw_value:
            return "auto_close"

        normalized = self._COMPLETION_POLICY_ALIASES.get(str(raw_value).lower())
        if normalized is not None:
            return normalized

        log.warning(
            "Unrecognized completion_policy '%s' — defaulting to auto_close",
            raw_value,
        )
        return "auto_close"

    def _build_plan(self, data: dict, task: str) -> Optional[Plan]:
        try:
            steps = []
            for i, step_data in enumerate(data.get("steps", [])):
                try:
                    action_str = step_data.get("action")
                    is_targetless = action_str in self._TARGETLESS_ACTIONS

                    # Skip steps with None or missing target — UNLESS the
                    # action is one of the target-less ones above, in which
                    # case default to a short descriptive placeholder. The
                    # placeholder is never read by any executor for these
                    # actions; it exists purely so Step.target (a required
                    # str field) has something readable to print in logs.
                    if not step_data.get("target"):
                        if is_targetless:
                            step_data["target"] = "current_media"
                        else:
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
                completion_policy    = self._normalize_completion_policy(
                    data.get("completion_policy")
                ),
                steps = steps,
                notes = data.get("notes"),
            )

            has_media_control_step = any(
                step.action.value in self._MEDIA_CONTROL_ACTIONS
                for step in plan.steps
            )
            if has_media_control_step and plan.completion_policy != "keep_open":
                log.info(
                    "Forcing completion_policy to keep_open — plan "
                    "includes a media control action (was: %s)",
                    plan.completion_policy,
                )
                plan.completion_policy = "keep_open"

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