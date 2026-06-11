import time
import logging
import base64
from typing import Optional
from io import BytesIO

import pyautogui
from PIL import Image
from google import genai
from google.genai import types
from pydantic import BaseModel

from src.models import ToolResult
from src.config import (
    VISION_MODEL,
    MAX_VISION_TOKENS,
    SCREENSHOT_WIDTH,
)

log = logging.getLogger(__name__)

# Structured response models for Gemini vision

class CoordinateHint(BaseModel):
    """
    Optional pixel coordinates Gemini thinks the target is at.
    Normalized 0-1000 like v1, converted to screen coords after.
    """
    x: int  # 0-1000 normalized
    y: int  # 0-1000 normalized
    confidence: str  # "high" | "medium" | "low"


class VisionDecision(BaseModel):
    """
    Structured response from Gemini vision.
    Always includes reasoning so we can log why it decided what it did.
    """
    reasoning:         str
    suggested_action:  str   # "click", "type", "scroll", "wait", "give_up"
    target_description: str  # human readable — "the blue Submit button"
    coordinates:       Optional[CoordinateHint] = None
    alternative:       Optional[str] = None  # if primary fails, try this


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VisionTools:
    """
    Gemini vision as last-resort fallback.

    Usage:
        vt = VisionTools(client)

        # Analyze a specific region
        result = vt.analyze_region(
            left=100, top=200, width=400, height=300,
            question="Where is the Submit button?",
            prior_attempts=["uiautomation: element not found",
                           "OCR: text not found"]
        )

        # Full decision when everything else failed
        result = vt.decide_action(
            task_step="Click the play button",
            app_name="Spotify",
            prior_attempts=["uiautomation: Play button not found",
                           "OCR: could not find play text"]
        )
    """

    def __init__(self, client: genai.Client):
        self._client        = client
        self._call_count    = 0   # track API calls for cost awareness
        self._max_calls     = 5   # per task — circuit breaker

    def reset_call_count(self) -> None:
        """Call this at the start of each new task."""
        self._call_count = 0

    # -----------------------------------------------------------------------
    # Analyze a specific screen region
    # -----------------------------------------------------------------------

    def analyze_region(
        self,
        question:       str,
        prior_attempts: Optional[list[str]] = None,
        # Option A — pass explicit pixel coordinates
        left:           Optional[int] = None,
        top:            Optional[int] = None,
        width:          Optional[int] = None,
        height:         Optional[int] = None,
        # Option B — pass app name, we find the window bounds automatically
        app_name:       Optional[str] = None,
    ) -> ToolResult:
        """
        Capture a screen region and ask Gemini a question about it.

        Two ways to specify what to capture:
        1. Explicit coordinates: pass left, top, width, height
        2. App window:          pass app_name — we find the window bounds

        question:        what to ask about the region
        prior_attempts:  what was already tried and failed
        """
        start = time.monotonic()

        if self._call_count >= self._max_calls:
            return ToolResult(
                success=False,
                message=(
                    f"Vision circuit breaker: {self._call_count} calls "
                    f"already made this task."
                ),
                error="CircuitBreaker",
                duration_ms=_ms(start)
            )

        try:
            # Resolve region — app_name takes priority over coordinates
            if app_name:
                bounds = _get_app_window_bounds(app_name)
                if bounds is None:
                    return ToolResult(
                        success=False,
                        message=f"Window '{app_name}' not found",
                        error="WindowNotFound",
                        duration_ms=_ms(start)
                    )
                r_left, r_top, r_right, r_bottom = bounds
                r_width  = r_right  - r_left
                r_height = r_bottom - r_top

            elif all(v is not None for v in [left, top, width, height]):
                r_left, r_top, r_width, r_height = left, top, width, height

            else:
                # No region specified — capture full screen
                w, h = pyautogui.size()
                r_left, r_top, r_width, r_height = 0, 0, w, h

            # Capture the region
            image = _capture_region(r_left, r_top, r_width, r_height)

            # Build prompt
            prompt = _build_region_prompt(question, prior_attempts)

            # Call Gemini
            self._call_count += 1
            log.info(
                "Vision call #%d — region (%d,%d,%d,%d)",
                self._call_count, r_left, r_top, r_width, r_height
            )

            response = self._client.models.generate_content(
                model=VISION_MODEL,
                contents=[prompt, _image_to_part(image)],
                config=types.GenerateContentConfig(
                    max_output_tokens=MAX_VISION_TOKENS,
                    system_instruction=_REGION_SYSTEM_PROMPT,
                )
            )

            decision = _parse_vision_response(response)

            if decision is None:
                return ToolResult(
                    success=False,
                    message="Vision returned unparseable response",
                    error="ParseError",
                    duration_ms=_ms(start)
                )

            # Convert normalized coords to screen coords
            screen_coords = None
            if decision.coordinates:
                screen_coords = _normalize_to_screen(
                    decision.coordinates.x,
                    decision.coordinates.y,
                    r_left, r_top, r_width, r_height
                )

            return ToolResult(
                success=True,
                message=(
                    f"Vision analyzed region — "
                    f"suggests: {decision.suggested_action} "
                    f"on '{decision.target_description}'"
                ),
                data={
                    "suggestion":   decision.suggested_action,
                    "target":       decision.target_description,
                    "reasoning":    decision.reasoning,
                    "coordinates":  screen_coords,
                    "alternative":  decision.alternative,
                    "confidence":   (
                        decision.coordinates.confidence
                        if decision.coordinates else "none"
                    ),
                    "vision_calls": self._call_count,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("analyze_region failed: %s", e)
            return ToolResult(
                success=False,
                message="Vision analysis failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Full decision when a step has completely failed
    # -----------------------------------------------------------------------

    def decide_action(
        self,
        task_step:      str,
        app_name:       str,
        prior_attempts: Optional[list[str]] = None,
        region:         Optional[tuple[int, int, int, int]] = None,
    ) -> ToolResult:
        """
        When all tools failed, ask Gemini to look at the screen
        and decide what to do next.

        task_step:      the step that failed e.g. "Click the Play button"
        app_name:       which app we're working with e.g. "Spotify"
        prior_attempts: everything that was tried and failed
        region:         optional (left, top, width, height) to focus on
                        if None, captures the app window or full screen

        This is the most expensive vision call — use only when stuck.
        """
        start = time.monotonic()

        if self._call_count >= self._max_calls:
            return ToolResult(
                success=False,
                message=(
                    f"Vision circuit breaker triggered after "
                    f"{self._call_count} calls"
                ),
                error="CircuitBreaker",
                duration_ms=_ms(start)
            )

        try:
            # Capture region or app window or full screen
            if region:
                left, top, width, height = region
                image = _capture_region(left, top, width, height)
            else:
                window_bounds = _get_app_window_bounds(app_name)
                if window_bounds:
                    l, t, r, b = window_bounds
                    image = _capture_region(l, t, r - l, b - t)
                    left, top, width, height = l, t, r - l, b - t
                else:
                    # Full screen fallback
                    image = _capture_full_screen()
                    w, h  = pyautogui.size()
                    left, top, width, height = 0, 0, w, h

            prompt = _build_decision_prompt(
                task_step, app_name, prior_attempts
            )

            self._call_count += 1
            log.info(
                "Vision call #%d (decide_action) — task: '%s'",
                self._call_count, task_step
            )

            response = self._client.models.generate_content(
                model=VISION_MODEL,
                contents=[prompt, _image_to_part(image)],
                config=types.GenerateContentConfig(
                    max_output_tokens=MAX_VISION_TOKENS,
                    system_instruction=_DECISION_SYSTEM_PROMPT,
                )
            )

            decision = _parse_vision_response(response)

            if decision is None:
                return ToolResult(
                    success=False,
                    message="Vision returned unparseable response",
                    error="ParseError",
                    duration_ms=_ms(start)
                )

            screen_coords = None
            if decision.coordinates:
                screen_coords = _normalize_to_screen(
                    decision.coordinates.x,
                    decision.coordinates.y,
                    left, top, width, height
                )

            return ToolResult(
                success=True,
                message=(
                    f"Vision decision: {decision.suggested_action} "
                    f"on '{decision.target_description}'"
                ),
                data={
                    "action":       decision.suggested_action,
                    "target":       decision.target_description,
                    "reasoning":    decision.reasoning,
                    "coordinates":  screen_coords,
                    "alternative":  decision.alternative,
                    "vision_calls": self._call_count,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("decide_action failed: %s", e)
            return ToolResult(
                success=False,
                message="Vision decision failed",
                error=str(e),
                duration_ms=_ms(start)
            )

    # -----------------------------------------------------------------------
    # Simple screen description
    # -----------------------------------------------------------------------

    def describe_screen(
        self,
        app_name: Optional[str] = None,
    ) -> ToolResult:
        """
        Get a plain English description of what's currently on screen.

        Useful for:
          - Debugging why the agent is stuck
          - Building context before a complex decision
          - Logging what the agent saw at a decision point

        app_name: if given, captures only that window
                  if None, captures full screen
        """
        start = time.monotonic()

        if self._call_count >= self._max_calls:
            return ToolResult(
                success=False,
                message="Vision circuit breaker triggered",
                error="CircuitBreaker",
                duration_ms=_ms(start)
            )

        try:
            if app_name:
                bounds = _get_app_window_bounds(app_name)
                if bounds:
                    l, t, r, b = bounds
                    image = _capture_region(l, t, r - l, b - t)
                else:
                    image = _capture_full_screen()
            else:
                image = _capture_full_screen()

            self._call_count += 1

            response = self._client.models.generate_content(
                model=VISION_MODEL,
                contents=[
                    "Describe what you see on this screen in 2-3 sentences. "
                    "Focus on what application is open and what the user "
                    "would need to interact with next.",
                    _image_to_part(image),
                ],
                config=types.GenerateContentConfig(
                    max_output_tokens=300,
                )
            )

            description = response.text or "No description returned"

            return ToolResult(
                success=True,
                message="Screen described",
                data={
                    "description": description,
                    "vision_calls": self._call_count,
                },
                duration_ms=_ms(start)
            )

        except Exception as e:
            log.error("describe_screen failed: %s", e)
            return ToolResult(
                success=False,
                message="Screen description failed",
                error=str(e),
                duration_ms=_ms(start)
            )


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_REGION_SYSTEM_PROMPT = """
You are a Windows computer vision assistant helping an automation agent.
You are given a screenshot of a specific UI region and a question about it.
You also receive context about what was already tried and failed.
Use this context — do not suggest things that were already attempted.

Rules:
- Be specific about where elements are
- Provide normalized coordinates (0-1000) where visible
- suggested_action must be one of: click, type, scroll, wait, give_up
- give_up only if element genuinely cannot be found
- Keep reasoning under 100 words — be concise

Respond ONLY with valid JSON in this exact format, no markdown, no extra text:
{
  "reasoning": "brief explanation under 100 words",
  "suggested_action": "click|type|scroll|wait|give_up",
  "target_description": "description of what to interact with",
  "coordinates": {"x": 0-1000, "y": 0-1000, "confidence": "high|medium|low"},
  "alternative": "fallback suggestion or null"
}
""".strip()

_DECISION_SYSTEM_PROMPT = """
You are a Windows computer vision assistant helping an automation agent
that is stuck and needs help deciding what to do next.

You are given:
1. The task step that failed
2. What was already attempted
3. A screenshot of the current app state

Your job is to suggest the best next action to make progress.

Rules:
- Consider the prior attempts — do not repeat what already failed
- Coordinates are normalized 0-1000 (0,0=top-left, 1000,1000=bottom-right)
- Be conservative — suggest the most reliable action
- suggested_action must be one of: click, type, scroll, wait, give_up
- Keep reasoning under 50 words — be concise

Respond ONLY with valid JSON in this exact format, no markdown, no extra text:
{
  "reasoning": "brief explanation under 100 words",
  "suggested_action": "click|type|scroll|wait|give_up",
  "target_description": "description of what to interact with",
  "coordinates": {"x": 0-1000, "y": 0-1000, "confidence": "high|medium|low"},
  "alternative": "fallback suggestion or null"
}
""".strip()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _capture_region(
    left: int,
    top: int,
    width: int,
    height: int,
) -> Image.Image:
    """Capture a specific screen region, downscale for token efficiency."""
    image = pyautogui.screenshot(region=(left, top, width, height))

    if image.width > SCREENSHOT_WIDTH:
        ratio      = SCREENSHOT_WIDTH / image.width
        new_height = int(image.height * ratio)
        image      = image.resize((SCREENSHOT_WIDTH, new_height), Image.LANCZOS)

    return image


def _capture_full_screen() -> Image.Image:
    """Capture full screen, downscale."""
    image = pyautogui.screenshot()

    if image.width > SCREENSHOT_WIDTH:
        ratio      = SCREENSHOT_WIDTH / image.width
        new_height = int(image.height * ratio)
        image      = image.resize((SCREENSHOT_WIDTH, new_height), Image.LANCZOS)

    return image


def _image_to_part(image: Image.Image) -> types.Part:
    """Convert PIL Image to Gemini-compatible Part object."""
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    image_bytes = buffer.getvalue()

    return types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/png",
    )


def _normalize_to_screen(
    norm_x: int,
    norm_y: int,
    region_left: int,
    region_top:  int,
    region_width: int,
    region_height: int,
) -> dict:
    """
    Convert normalized 0-1000 coordinates to absolute screen pixels.
    norm_x, norm_y are relative to the captured region.
    """
    screen_x = region_left + int((norm_x / 1000) * region_width)
    screen_y = region_top  + int((norm_y / 1000) * region_height)

    return {
        "x": screen_x,
        "y": screen_y,
    }


def _build_region_prompt(
    question:       str,
    prior_attempts: Optional[list[str]],
) -> str:
    """Build the prompt for analyze_region."""
    parts = [f"Question: {question}"]

    if prior_attempts:
        parts.append("\nWhat was already tried and failed:")
        for attempt in prior_attempts:
            parts.append(f"  - {attempt}")
        parts.append(
            "\nDo NOT suggest these approaches again. "
            "Suggest something different."
        )

    return "\n".join(parts)


def _build_decision_prompt(
    task_step:      str,
    app_name:       str,
    prior_attempts: Optional[list[str]],
) -> str:
    """Build the prompt for decide_action."""
    parts = [
        f"App: {app_name}",
        f"Task step that failed: {task_step}",
    ]

    if prior_attempts:
        parts.append("\nWhat was already tried:")
        for attempt in prior_attempts:
            parts.append(f"  - {attempt}")

    parts.append(
        "\nLook at the screenshot and suggest the best action "
        "to complete this step."
    )

    return "\n".join(parts)


def _get_app_window_bounds(
    app_name: str,
) -> Optional[tuple[int, int, int, int]]:
    """Get window bounds — reuse the same helper pattern as ocr.py."""
    try:
        import uiautomation as auto
        name_lower = app_name.lower()
        desktop    = auto.GetRootControl()

        for window in desktop.GetChildren():
            title = (window.Name or "").lower()
            if name_lower in title:
                rect = window.BoundingRectangle
                return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    return None

def _parse_vision_response(response) -> Optional[VisionDecision]:
    """
    Parse Gemini response into VisionDecision.
    Tries response.parsed first, falls back to manual JSON parsing.
    Also attempts to recover from truncated JSON responses.
    """
    import json
    import re

    # Try structured output first
    if response.parsed is not None:
        return response.parsed

    raw_text = response.text or ""

    # Strip markdown code blocks if present
    raw_text = raw_text.strip()
    if raw_text.startswith("```"):
        lines    = raw_text.splitlines()
        raw_text = "\n".join(lines[1:-1])

    # Attempt 1 — direct parse
    try:
        data = json.loads(raw_text)
        return _build_vision_decision(data)
    except json.JSONDecodeError:
        pass

    # Attempt 2 — truncated JSON recovery
    # Find the last complete field before truncation and close the JSON
    try:
        # Try to find all complete key-value pairs using regex
        reasoning = re.search(
            r'"reasoning"\s*:\s*"([^"]*)"', raw_text
        )
        action = re.search(
            r'"suggested_action"\s*:\s*"([^"]*)"', raw_text
        )
        target = re.search(
            r'"target_description"\s*:\s*"([^"]*)"', raw_text
        )

        if action:
            # Build a minimal valid decision from what we could extract
            return VisionDecision(
                reasoning=(
                    reasoning.group(1) if reasoning
                    else "Response was truncated"
                ),
                suggested_action=action.group(1),
                target_description=(
                    target.group(1) if target
                    else "unknown"
                ),
                coordinates=None,
                alternative=None,
            )
    except Exception as e:
        log.error("Truncation recovery failed: %s", e)

    # Attempt 3 — extract coordinates if nothing else worked
    log.error(
        "Manual JSON parse failed: all attempts exhausted | raw: %s",
        raw_text[:200]
    )
    return None


def _build_vision_decision(data: dict) -> VisionDecision:
    """Build VisionDecision from a parsed dict."""
    coords = None
    if data.get("coordinates"):
        coords = CoordinateHint(
            x          = data["coordinates"].get("x", 500),
            y          = data["coordinates"].get("y", 500),
            confidence = data["coordinates"].get("confidence", "low"),
        )
    return VisionDecision(
        reasoning          = data.get("reasoning", "No reasoning provided"),
        suggested_action   = data.get("suggested_action", "give_up"),
        target_description = data.get("target_description", "unknown"),
        coordinates        = coords,
        alternative        = data.get("alternative"),
    )

def _ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)

