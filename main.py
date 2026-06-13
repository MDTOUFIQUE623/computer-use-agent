import os
import sys
import logging
from dotenv import load_dotenv

# Ensure src is in path
src_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
sys.path.append(src_path)

from src.graph import app

# Configure logging
logging.basicConfig(
    level=logging.WARNING,  # Only show warnings+ in console
    format="%(levelname)s | %(name)s | %(message)s"
)

def main():
    load_dotenv()

    if not os.getenv("GEMINI_API_KEY"):
        print("Error: GEMINI_API_KEY not found in .env")
        sys.exit(1)

    print("\n" + "="*50)
    print("    Computer Use Agent v2")
    print("="*50)

    task = input("\nWhat do you want me to do? ").strip()
    if not task:
        print("No task entered.")
        return

    # Initialize state
    initial_state = {
        "task":               task,
        "plan":               None,
        "current_step_index": 0,
        "step_results":       [],
        "retry_count":        0,
        "is_done":            False,
        "is_failed":          False,
        "memory_hints":       None,
        "last_error":         None,
        "ask_user_message":   None,
        "task_start_ms":      None,
        "_last_tool_result":  None,
        "_last_step_result":  None,
        "extracted_content":  None,   # adding the extracted content for browser automation
    }

    try:
        final_state = app.invoke(initial_state)
        if final_state.get("is_failed"):
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nStopped by user.")
        sys.exit(0)
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()