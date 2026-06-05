import os
from typing import Optional, TypedDict
from dotenv import load_dotenv

from google import genai
from langgraph.graph import END, START, StateGraph
from PIL import Image

from actions import execute_action
from brain import Action, decide_action
from config import MAX_STEPS
from screen import capture_screen

# Load environment variables (useful if running components independently)
load_dotenv()

class AgentState(TypedDict):
    task: str
    screenshot: Optional[Image.Image]
    action: Optional[Action]
    step: int
    done: bool
    history: list[str]

# Initialize Gemini Client
client = genai.Client()

def see_node(state: AgentState) -> dict:
    """Capture a screenshot of the computer screen."""
    print(f"\n--- [Step {state['step'] + 1}] SEE ---")
    print("Capturing screenshot...")
    screenshot = capture_screen()
    return {"screenshot": screenshot}

def think_node(state: AgentState) -> dict:
    """Analyze the screenshot and decide on the next action using Gemini."""
    print("--- THINK ---")
    print("Sending screenshot to Gemini Flash...")
    if not state["screenshot"]:
        raise ValueError("No screenshot found in state.")
    
    action = decide_action(
        client=client,
        task=state["task"],
        screenshot=state["screenshot"],
        history=state["history"]
    )
    print(f"Reasoning: {action.reasoning}")
    print(f"Action: {action.action} (x={action.x}, y={action.y}, text={action.text}, direction={action.direction})")
    return {"action": action}

def act_node(state: AgentState) -> dict:
    """Execute the decided action using PyAutoGUI."""
    print("--- ACT ---")
    action = state["action"]
    if not action:
        raise ValueError("No action found in state to execute.")
    
    # Run the action and get a log string
    log = execute_action(action)
    print(f"Result: {log}")
    
    # Update execution state
    new_history = list(state["history"]) + [f"{action.action}: {log}"]
    new_step = state["step"] + 1
    done = (action.action == "done")
    
    return {
        "history": new_history,
        "step": new_step,
        "done": done
    }

def should_continue(state: AgentState) -> str:
    """Check if the loop is complete or the max step limit has been reached."""
    if state["done"]:
        print("\n--- DONE ---")
        print("Task is complete.")
        return END
    if state["step"] >= MAX_STEPS:
        print(f"\n--- MAX STEPS REACHED ({MAX_STEPS}) ---")
        return END
    return "see"

# Build the LangGraph workflow
workflow = StateGraph(AgentState)

# Add the nodes
workflow.add_node("see", see_node)
workflow.add_node("think", think_node)
workflow.add_node("act", act_node)

# Set the entry point and edges
workflow.add_edge(START, "see")
workflow.add_edge("see", "think")
workflow.add_edge("think", "act")

# Add conditional edge from act
workflow.add_conditional_edges(
    "act",
    should_continue,
    {
        "see": "see",
        END: END
    }
)

# Compile the graph
app = workflow.compile()