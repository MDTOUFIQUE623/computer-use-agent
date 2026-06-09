from typing import Literal, Optional

from google import genai
from google.genai import types
from PIL import Image
from pydantic import BaseModel

from src.config import MODEL

class Action(BaseModel):
    """One step the agent wants to take. Coordinates are normalized 0-1000."""

    reasoning: str
    action: Literal["click", "type", "scroll", "wait", "done"]
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = None
    direction: Optional[Literal["up", "down"]] = None

SYSTEM_PROMPT = (
    "You control a Windows computer to accomplish the user's task. You are qiven"
    "the task and a screenshot. Decide the SINGLE next action that makes progress.\\n"
    "Coordinates x and y are normalized 0-1000 (0,0 = top-lef t, 1000,1000 = bottom-right).\\n"
    "Actions: click (set x,y to the element center), type (te xt into the focused"
    "field), scroll (direction up/down), wait (let an app ope then look again), "
    "done (task complete).\\n"
    "To open an app: click the taskbar Search icon, type the app name, click the top"
    "result. Prefer typing the name over hunting for tiles. I f Search is already open, "
    "do not click the icon again just type.\\n"
    "You are given the actions you have ALREADY taken. Do NOT repeat an action that "
    "worked. If your last action had no visible effect, try a different approach. "
    "Return the 'done' action when the task is visibly accomp lished."
)

def decide_action(
        client: genai.Client, task: str, screenshot: Image.Image, history: list[str]
) -> Action:
    """Send task + history + screenshot to Gemini and return one structured Action."""
    history_text = "\\n".join(f"- {h}" for h in history) or "none yet"
    response = client.models.generate_content(
        model=MODEL,
        contents=[
            f"Task: {task}",
            f"Actions already taken (most recent last):\\n{history_text}",
            "Here is the current screen. Decide the next action:",
            screenshot,
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_schema=Action,
        ),
    )
    return response.parsed
